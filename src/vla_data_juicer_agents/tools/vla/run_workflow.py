from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import EventEmitter, JsonlEventSink
from vla_data_juicer_agents.core.tool import ToolContext, ToolSpec
from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.workflow import run_executor_agent, run_plan_agent

_CALIBRATION_CONFIRMATION_PAUSE_TOKEN = "calibration_params_not_confirmed"
_CJK_LANGUAGE_RE = r"[\u3400-\u4dbf\u4e00-\u9fff]"
_LEGACY_WORKFLOW_RESUME_DISABLED_ERROR_TYPE = "legacy_workflow_resume_disabled"
_LEGACY_WORKFLOW_RESUME_DISABLED_MESSAGE = (
    "Legacy VLA workflow resume is disabled. Use the AgentScope NavigationDataAgent session instead."
)


class RunVLAWorkflowInput(BaseModel):
    date: str
    segments: str | list[str] | None = None
    scene_mode: Literal["in", "out"] | None = Field(
        default=None,
        description=(
            "Required scene type for navigation data. Use 'in' for indoor/室内 scenes and 'out' for outdoor/室外 scenes."
            " Do not interpret as entering/leaving a warehouse."
        )
    )
    dry_run: bool = False
    approve: bool = True
    model: str | None = None
    response_language: str | None = Field(
        default=None,
        description=(
            "Language to use for user-facing progress summaries and final workflow summaries. "
            "Set this to the user's language, for example 'Chinese' for Chinese requests."
        ),
    )
    scenario: Literal["navigation_vla"] = "navigation_vla"


class ContinueVLAWorkflowInput(BaseModel):
    user_input: str
    run_dir: str | None = None
    model: str | None = None
    response_language: str | None = None


class RunVLAWorkflowOutput(BaseModel):
    ok: bool
    status: str
    run_dir: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    final_output: str = ""
    error_type: str | None = None
    message: str = ""


def _normalize_segments(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or None
    raw = value.strip()
    if not raw or raw.lower() == "all":
        return None
    if raw.startswith("["):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            items = [str(item).strip() for item in payload if str(item).strip()]
            return items or None
    return [item.strip() for item in raw.split(",") if item.strip()] or None


def _normalize_model(value: str | None) -> str | None:
    if value is None:
        return None
    model = str(value).strip()
    if not model or model.lower() in {"none", "null"}:
        return None
    return model


def _latest_user_message(ctx: ToolContext) -> str:
    runtime = ctx.runtime_values.get("session_runtime")
    history = getattr(getattr(runtime, "state", None), "history", None)
    if not isinstance(history, list):
        return ""
    for item in reversed(history):
        if isinstance(item, Mapping) and item.get("role") == "user":
            content = item.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _infer_response_language(text: str) -> str | None:
    if not text:
        return None
    import re

    return "Chinese" if re.search(_CJK_LANGUAGE_RE, text) else None


def _resolve_response_language(ctx: ToolContext, explicit: str | None) -> str | None:
    inferred = _infer_response_language(_latest_user_message(ctx))
    if inferred:
        return inferred
    language = str(explicit or "").strip()
    if language:
        return language
    return None


def _artifact_paths(run_dir) -> dict[str, str]:
    return {
        "request": str(run_dir / "request.json"),
        "plan": str(run_dir / "plan.json"),
        "final_report": str(run_dir / "final_report.json"),
        "events": str(run_dir / "events.jsonl"),
    }


def _legacy_workflow_resume_disabled_output(
    *,
    run_dir: Path | None = None,
    artifacts: dict[str, str] | None = None,
    final_output: str = "",
) -> RunVLAWorkflowOutput:
    return RunVLAWorkflowOutput(
        ok=False,
        status="disabled",
        run_dir=str(run_dir) if run_dir is not None else None,
        artifacts=artifacts or {},
        final_output=final_output,
        error_type=_LEGACY_WORKFLOW_RESUME_DISABLED_ERROR_TYPE,
        message=_LEGACY_WORKFLOW_RESUME_DISABLED_MESSAGE,
    )


def _contains_calibration_confirmation_pause(value: Any) -> bool:
    if isinstance(value, str):
        return _CALIBRATION_CONFIRMATION_PAUSE_TOKEN in value
    if isinstance(value, Mapping):
        return any(
            _contains_calibration_confirmation_pause(item)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_calibration_confirmation_pause(item) for item in value)
    return False


def _is_calibration_confirmation_pause(final_output: str) -> bool:
    return _contains_calibration_confirmation_pause(final_output)


class _CalibrationConfirmationPauseSink:
    def __init__(self, source_prefix: str = "navigation.executor") -> None:
        self.detected = False
        self._source_prefix = source_prefix

    def publish(self, event: Mapping[str, Any]) -> None:
        source = event.get("source")
        if not isinstance(source, str):
            return
        if source != self._source_prefix and not source.startswith(f"{self._source_prefix}."):
            return
        payload = event.get("payload", {})
        if (
            isinstance(payload, Mapping)
            and payload.get("error_type") == _CALIBRATION_CONFIRMATION_PAUSE_TOKEN
        ):
            self.detected = True
            return
        if _contains_calibration_confirmation_pause(payload):
            self.detected = True


async def run_vla_workflow(ctx: ToolContext, raw_args: RunVLAWorkflowInput | dict[str, Any]) -> dict[str, Any]:
    args = raw_args if isinstance(raw_args, RunVLAWorkflowInput) else RunVLAWorkflowInput.model_validate(raw_args)
    if args.scene_mode is None:
        return RunVLAWorkflowOutput(
            ok=False,
            status="needs_user_input",
            error_type="missing_scene_mode",
            message=(
                "Please provide scene_mode as either 'in' or 'out' before running the VLA navigation workflow."
                "'in' means indoor/室内, and 'out' means outdoor/室外."
            ),
        ).model_dump(mode="json")

    model = _normalize_model(args.model)
    response_language = _resolve_response_language(ctx, args.response_language)
    request = NavigationRequest(
        date=args.date,
        segments=_normalize_segments(args.segments),
        scene_mode=args.scene_mode,
        dry_run=args.dry_run,
    )
    settings = NavigationSettings()
    run_store = WorkflowRunStore(settings.runs_root)
    run_dir = run_store.create_run(request.date)
    run_store.write_json(run_dir, "request.json", request.model_dump(mode="json"))
    runtime_values = ctx.runtime_values
    incoming_scope = runtime_values.get("event_scope")
    incoming_emitter = runtime_values.get("event_emitter")
    emitter = getattr(incoming_scope, "emitter", None) or incoming_emitter or EventEmitter()
    calibration_pause_sink = _CalibrationConfirmationPauseSink()
    emitter = emitter.with_sink(JsonlEventSink(run_dir / "events.jsonl")).with_sink(calibration_pause_sink)
    workflow_scope = emitter.scope(
        "navigation.workflow",
        parent_run_id=getattr(incoming_scope, "run_id", None),
    )
    plan_scope = workflow_scope.child("navigation.plan")
    executor_scope = workflow_scope.child("navigation.executor")
    cancellation = runtime_values.get("cancellation") or CancellationContext()
    workflow_scope.emit("agent_start")
    terminal_status: str | None = None

    def emit_terminal(status: str) -> None:
        nonlocal terminal_status
        if terminal_status is None:
            terminal_status = status
            workflow_scope.emit("agent_end", status=status)

    try:
        cancellation.raise_if_cancelled()
        plan_agent = create_plan_agent(model=model, request=request)
        plan = await run_plan_agent(
            plan_agent,
            request,
            run_store=run_store,
            run_dir=run_dir,
            event_scope=plan_scope,
            cancellation=cancellation,
            response_language=response_language,
        )
        run_store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))

        if not args.approve:
            payload = RunVLAWorkflowOutput(
                ok=True,
                status="awaiting_confirmation",
                run_dir=str(run_dir),
                artifacts=_artifact_paths(run_dir),
                message="VLA workflow plan is awaiting approval before execution.",
            ).model_dump(mode="json")
            run_store.write_json(run_dir, "final_report.json", payload)
            emit_terminal("completed")
            return payload

        executor_agent = create_executor_agent(
            model=model,
            dry_run=args.dry_run,
            cancellation=cancellation,
        )
        final_output = await run_executor_agent(
            executor_agent,
            plan,
            run_store=run_store,
            run_dir=run_dir,
            event_scope=executor_scope,
            cancellation=cancellation,
            response_language=response_language,
        )
        if calibration_pause_sink.detected or _is_calibration_confirmation_pause(final_output):
            payload = _legacy_workflow_resume_disabled_output(
                run_dir=run_dir,
                artifacts=_artifact_paths(run_dir),
                final_output=final_output,
            ).model_dump(mode="json")
            run_store.write_json(run_dir, "final_report.json", payload)
            emit_terminal("disabled")
            return payload
        payload = RunVLAWorkflowOutput(
            ok=True,
            status="completed",
            run_dir=str(run_dir),
            artifacts=_artifact_paths(run_dir),
            final_output=final_output,
            message=final_output,
        ).model_dump(mode="json")
        run_store.write_json(run_dir, "final_report.json", payload)
        emit_terminal("completed")
        return payload
    except TurnCancelled as exc:
        payload = RunVLAWorkflowOutput(
            ok=False,
            status="interrupted",
            run_dir=str(run_dir),
            artifacts=_artifact_paths(run_dir),
            error_type=type(exc).__name__,
            message=str(exc),
        ).model_dump(mode="json")
        run_store.write_json(run_dir, "final_report.json", payload)
        emit_terminal("interrupted")
        raise
    except Exception as exc:
        payload = RunVLAWorkflowOutput(
            ok=False,
            status="failed",
            run_dir=str(run_dir),
            artifacts=_artifact_paths(run_dir),
            error_type=type(exc).__name__,
            message=str(exc),
        ).model_dump(mode="json")
        run_store.write_json(run_dir, "final_report.json", payload)
        emit_terminal("failed")
        return payload


async def continue_vla_workflow(
    ctx: ToolContext,
    raw_args: ContinueVLAWorkflowInput | dict[str, Any],
) -> dict[str, Any]:
    return _legacy_workflow_resume_disabled_output().model_dump(mode="json")


VLA_RUN_WORKFLOW = ToolSpec(
    name="vla_run_workflow",
    description=(
        "Run the structured navigation VLA data workflow. Prefer this tool for complex VLA, "
        "navigation, ROS bag, db3, odom, trajectory, or manual annotation processing requests. "
        "It invokes the Plan-Agent and then the Executor-Agent; do not replace it with deterministic routing."
    ),
    input_model=RunVLAWorkflowInput,
    executor=run_vla_workflow,
    tags=("vla", "workflow", "execute"),
    effects="execute",
    confirmation="required",
)


VLA_CONTINUE_WORKFLOW = ToolSpec(
    name="vla_continue_workflow",
    description=(
        "Legacy-disabled VLA workflow resume artifact. This tool no longer continues or pauses "
        "navigation workflows; use the AgentScope NavigationDataAgent session with durable "
        "human-decision events instead."
    ),
    input_model=ContinueVLAWorkflowInput,
    executor=continue_vla_workflow,
    tags=("vla", "workflow", "execute", "resume"),
    effects="execute",
    confirmation="required",
)
