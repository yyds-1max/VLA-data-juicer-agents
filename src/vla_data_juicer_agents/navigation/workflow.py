import asyncio
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import UserMsg
from pydantic import ValidationError

from vla_data_juicer_agents.adapters.agentscope.events import AgentScopeEventAdapter
from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    TurnCancelled,
    current_cancellation,
)
from vla_data_juicer_agents.core.events import EventEmitter, EventScope
from vla_data_juicer_agents.navigation.models import NavigationDataProfile, NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.plan_validation import validate_workflow_plan
from vla_data_juicer_agents.navigation.plan_draft import build_plan_from_draft
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore


MAX_AGENT_TOOL_CONFIRMATION_ROUNDS = 30


class _SceneModeMissing:
    pass


_SCENE_MODE_MISSING = _SceneModeMissing()


def _finish_temp_path(date: str) -> str:
    return f"finish_data/{date}_temp"


def _finish_path(date: str) -> str:
    return f"finish_data/{date}"


def _raw_output_excerpt(output: object, max_length: int = 120) -> str:
    raw = "" if output is None else str(output)
    raw = raw.replace("\n", "\\n")
    return raw[:max_length]


def _json_text_from_output(output: str) -> str:
    text = output.strip()
    fenced_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        return fenced_match.group(1).strip()
    return text


def _parse_workflow_plan_output(output: object) -> WorkflowPlan:
    if isinstance(output, WorkflowPlan):
        return output

    try:
        if isinstance(output, dict):
            return WorkflowPlan.model_validate(output)

        if isinstance(output, str) and output.strip():
            payload: Any = json.loads(_json_text_from_output(output))
            return WorkflowPlan.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
        raise ValueError(
            f"Unable to parse WorkflowPlan output: {exc}; raw output excerpt={_raw_output_excerpt(output)!r}"
        ) from exc

    raise ValueError(f"Unable to parse WorkflowPlan output: empty or unsupported output; raw output excerpt={_raw_output_excerpt(output)!r}")


def _validated_workflow_plan(plan: WorkflowPlan, data_profile: NavigationDataProfile | None = None) -> WorkflowPlan:
    validation = validate_workflow_plan(plan, data_profile=data_profile)
    if not validation["ok"]:
        raise ValueError(f"WorkflowPlan validation failed: {validation['errors']}")
    return plan


def build_deterministic_plan_template(
    date: str,
    dataset_profile: str,
    segments: list[str] | None,
    *,
    scene_mode: Literal["in", "out"] | _SceneModeMissing = _SCENE_MODE_MISSING,
    data_profile: NavigationDataProfile | None = None,
) -> WorkflowPlan:
    if scene_mode not in {"in", "out"}:
        raise ValueError("scene_mode is required before building WorkflowPlan; expected 'in' or 'out'.")

    finish_temp_path = _finish_temp_path(date)
    finish_path = _finish_path(date)
    common_arguments = {"date": date, "segments": segments}

    gridmap_decision = (
        data_profile.stage_variants.get("prepare_gridmap_for_projection")
        if data_profile is not None
        else None
    )
    extract_decision = (
        data_profile.stage_variants.get("extract_and_sync_navigation_data")
        if data_profile is not None
        else None
    )
    projection_decision = (
        data_profile.stage_variants.get("run_projection_and_trajectory")
        if data_profile is not None
        else None
    )
    extract_variant = extract_decision.variant if extract_decision is not None else dataset_profile
    projection_variant = (
        projection_decision.variant
        if projection_decision is not None
        else "cjl_0525_with_gridmap"
        if dataset_profile == "go2w_like"
        else "cjl_with_gridmap"
    )
    skip_gridmap = bool(
        data_profile is not None
        and data_profile.projection_input_ready
        and gridmap_decision is not None
        and gridmap_decision.variant == "skip_if_projection_ready"
    )

    steps = [
        WorkflowStep(
            step_id="prepare_raw_data",
            tool_name="prepare_raw_data",
            arguments=common_arguments,
            expected_outputs=[f"raw_data/{date}_temp"],
        ),
        WorkflowStep(
            step_id="extract_and_sync_navigation_data",
            tool_name="extract_and_sync_navigation_data",
            arguments={**common_arguments, "dataset_profile": dataset_profile},
            preconditions=["prepare_raw_data"],
            expected_outputs=[f"clip_data/{date}"],
            variant=extract_variant,
            effects="execute",
            decision_ref=(
                "data_profile.stage_variants.extract_and_sync_navigation_data"
                if extract_decision is not None
                else None
            ),
            evidence=list(extract_decision.evidence) if extract_decision is not None else [],
        ),
        WorkflowStep(
            step_id="assemble_finish_temp",
            tool_name="assemble_finish_temp",
            arguments={**common_arguments, "dataset_profile": dataset_profile},
            preconditions=["extract_and_sync_navigation_data"],
            expected_outputs=[finish_temp_path],
        ),
        WorkflowStep(
            step_id="run_noobscene_preprocessing",
            tool_name="run_noobscene_preprocessing",
            arguments={"finish_temp_path": finish_temp_path},
            preconditions=["assemble_finish_temp"],
            expected_outputs=[finish_temp_path],
        ),
        WorkflowStep(
            step_id="run_initial_annotation_gui",
            tool_name="run_initial_annotation_gui",
            arguments={"finish_temp_path": finish_temp_path},
            preconditions=["run_noobscene_preprocessing"],
            expected_outputs=[finish_temp_path],
            human_blocking=True,
        ),
        WorkflowStep(
            step_id="run_tracking",
            tool_name="run_tracking",
            arguments={"finish_temp_path": finish_temp_path},
            preconditions=["run_initial_annotation_gui"],
            expected_outputs=[finish_path],
        ),
    ]
    if not skip_gridmap:
        steps.append(
            WorkflowStep(
                step_id="prepare_gridmap_for_projection",
                tool_name="prepare_gridmap_for_projection",
                arguments={
                    **common_arguments,
                    "finish_temp_path": finish_temp_path,
                    **(
                        {"gridmap_variant": gridmap_decision.variant}
                        if gridmap_decision is not None
                        else {}
                    ),
                },
                preconditions=["run_tracking"],
                expected_outputs=[f"gridmap/{date}"],
                failure_behavior="skip_if_gridmap_exists",
                variant=gridmap_decision.variant if gridmap_decision is not None else None,
                effects="execute" if gridmap_decision is not None else None,
                decision_ref=(
                    "data_profile.stage_variants.prepare_gridmap_for_projection"
                    if gridmap_decision is not None
                    else None
                ),
                evidence=list(gridmap_decision.evidence) if gridmap_decision is not None else [],
            )
        )
    projection_preconditions = ["run_tracking"] if skip_gridmap else ["prepare_gridmap_for_projection"]
    steps.extend(
        [
            WorkflowStep(
                step_id="run_projection_and_trajectory",
                tool_name="run_projection_and_trajectory",
                arguments={
                    "finish_temp_path": finish_temp_path,
                    "finish_path": finish_path,
                    "dataset_profile": dataset_profile,
                },
                preconditions=projection_preconditions,
                expected_outputs=[finish_path],
                variant=projection_variant,
                effects="execute",
                decision_ref=(
                    "data_profile.stage_variants.run_projection_and_trajectory"
                    if projection_decision is not None
                    else None
                ),
                evidence=list(projection_decision.evidence) if projection_decision is not None else [],
            ),
            WorkflowStep(
                step_id="validate_navigation_outputs",
                tool_name="validate_navigation_outputs",
                arguments={"date": date},
                preconditions=["run_projection_and_trajectory"],
                expected_outputs=[finish_path],
            ),
        ]
    )

    return WorkflowPlan(
        date=date,
        segments=segments,
        scene_mode=scene_mode,
        dataset_profile=dataset_profile,
        steps=steps,
    )


def _event_type(event: object) -> str:
    event_type = getattr(event, "type", None)
    if hasattr(event_type, "value"):
        return str(event_type.value)
    if event_type is not None:
        return str(event_type)
    return type(event).__name__


def _event_payload(event: object) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if hasattr(event, "__dict__"):
        return dict(vars(event))
    return {"repr": repr(event)}


def _event_text_delta(event: object) -> str:
    if _event_type(event) != "TEXT_BLOCK_DELTA":
        return ""
    delta = getattr(event, "delta", "")
    return delta if isinstance(delta, str) else str(delta)


def _event_tool_result_delta(event: object) -> str:
    if _event_type(event) != "TOOL_RESULT_TEXT_DELTA":
        return ""
    delta = getattr(event, "delta", "")
    return delta if isinstance(delta, str) else str(delta)


def _confirmation_results(event: object) -> list[ConfirmResult]:
    if _event_type(event) != "REQUIRE_USER_CONFIRM":
        return []
    return [
        ConfirmResult(confirmed=True, tool_call=tool_call)
        for tool_call in getattr(event, "tool_calls", [])
    ]


async def _run_agent_stream(
    agent,
    prompt: str,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    *,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    emit_tool_events: bool = True,
) -> str:
    scope = event_scope or EventEmitter().scope("agent")
    adapter = AgentScopeEventAdapter(scope, emit_tool_events=emit_tool_events)
    active_cancellation = cancellation or current_cancellation()
    output_chunks: list[str] = []
    tool_output_chunks: list[str] = []
    next_input: object = UserMsg(name="user", content=prompt)
    scope.emit("agent_start")
    try:
        async with AsyncExitStack() as stack:
            if active_cancellation is not None:
                await stack.enter_async_context(active_cancellation.track_agent(agent))
            for _ in range(MAX_AGENT_TOOL_CONFIRMATION_ROUNDS):
                if active_cancellation is not None:
                    active_cancellation.raise_if_cancelled()
                confirm_results: list[ConfirmResult] = []
                reply_id: str | None = None
                async for event in agent.reply_stream(next_input):
                    adapter.accept(event)
                    event_record = {
                        "event_type": _event_type(event),
                        "payload": _event_payload(event),
                    }
                    if run_store is not None and run_dir is not None:
                        run_store.append_jsonl(run_dir, "events.jsonl", event_record)
                    output_chunks.append(_event_text_delta(event))
                    tool_output_chunks.append(_event_tool_result_delta(event))
                    if _event_type(event) == "REQUIRE_USER_CONFIRM":
                        reply_id = getattr(event, "reply_id", None)
                        confirm_results.extend(_confirmation_results(event))
                if active_cancellation is not None:
                    active_cancellation.raise_if_cancelled()
                if not confirm_results:
                    adapter.close_active_tools("completed")
                    scope.emit("agent_end", status="completed")
                    return "".join(output_chunks) or "".join(tool_output_chunks)
                if reply_id is None:
                    raise RuntimeError("AgentScope requested tool confirmation without a reply id.")
                next_input = UserConfirmResultEvent(reply_id=reply_id, confirm_results=confirm_results)
        raise RuntimeError(
            "AgentScope tool confirmation loop exceeded "
            f"{MAX_AGENT_TOOL_CONFIRMATION_ROUNDS} iterations."
        )
    except TurnCancelled:
        adapter.close_active_tools("interrupted")
        scope.emit("agent_end", status="interrupted")
        raise
    except asyncio.CancelledError as exc:
        adapter.close_active_tools("interrupted")
        scope.emit("agent_end", status="interrupted")
        if active_cancellation is not None and active_cancellation.cancelled:
            raise TurnCancelled("The current turn was interrupted.") from exc
        raise
    except BaseException:
        adapter.close_active_tools("failed")
        scope.emit("agent_end", status="failed")
        raise


async def run_plan_agent(
    agent,
    request: NavigationRequest,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
) -> WorkflowPlan:
    draft_state = getattr(agent, "workflow_plan_draft_state", None)
    draft_prompt = ""
    if draft_state is not None:
        draft_prompt = (
            "\n\nCurrent internal ReAct planning state panel. It includes the NavigationDataProfile schema, "
            "the current data_profile_draft, filled_fields, missing_fields, next_tool_candidates, and "
            "ready_to_finish:\n"
            f"{json.dumps(draft_state.schema_snapshot(), ensure_ascii=False)}"
        )
    prompt = (
        "You are a Navigation ReAct Plan-Agent. Use the inspect_raw_date_tool, "
        "classify_navigation_dataset_tool, inspect_processing_state_tool, inspect_gridmap_artifacts_tool, "
        "inspect_runtime_assets_tool, and list_navigation_tool_capabilities_tool read-only tools to build a "
        "lightweight NavigationDataProfile and stage-one navigation WorkflowPlan.\n\n"
        "Strict ReAct loop:\n"
        "Thought: read the current NavigationDataProfile schema, data_profile_draft, filled_fields, "
        "missing_fields, validation_errors, and next_tool_candidates; decide exactly one next step.\n"
        "Action: choose exactly one of these actions:\n"
        "- Call one read-only tool with ToolName[arguments] to inspect one missing fact. After the tool result, "
        "call update_workflow_plan_draft_tool with data_profile_patch containing only newly learned "
        "NavigationDataProfile facts, plus observation_id and used_tool.\n"
        "- Finish by calling finalize_workflow_plan_tool, but only when ready_to_finish is true and "
        "missing_fields is empty.\n\n"
        "The NavigationDataProfile schema is authoritative. data_profile_patch must be JSON-compatible and "
        "must use only schema fields and valid enum values. Do not output a complete data_profile unless it is "
        "already fully supported by previous tool observations. Do not write script-level steps; "
        "final strict WorkflowPlan JSON must come from finalize_workflow_plan_tool. Stage one covers "
        "prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh. Default to all raw "
        "segments when segments are not specified. Supported dataset profiles are u_legacy_like and "
        "go2w_like. scene_mode is required and must be either in or out. Gridmap preparation must happen "
        "after run_tracking and before projection. Supported execution tool names include run_tracking, "
        "prepare_gridmap_for_projection, and run_projection_and_trajectory. The only human-blocking step "
        "is gen_box.py via run_initial_annotation_gui.\n\n"
        f"NavigationRequest JSON:\n{request.model_dump_json()}"
        f"{draft_prompt}"
    )
    output = await _run_agent_stream(agent, prompt, run_store=run_store, run_dir=run_dir)
    if draft_state is not None and draft_state.finalized_plan is not None:
        return _validated_workflow_plan(draft_state.finalized_plan, data_profile=draft_state.data_profile)
    if draft_state is not None and not output.strip() and draft_state.dataset_profile is not None:
        return _validated_workflow_plan(build_plan_from_draft(draft_state), data_profile=draft_state.data_profile)
    return _validated_workflow_plan(_parse_workflow_plan_output(output))


async def run_executor_agent(
    agent,
    plan: WorkflowPlan,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
) -> str:
    prompt = (
        "Execute this WorkflowPlan JSON step-by-step using the matching execution tools. Stop on any "
        "failed tool result. The gen_box.py GUI step is human-blocking via run_initial_annotation_gui; "
        "wait until the human finishes before continuing. scene_mode is required and must be either in "
        "or out. Prepare gridmap after run_tracking and before run_projection_and_trajectory. Supported "
        "tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory. "
        "Return a concise final execution summary.\n\n"
        f"WorkflowPlan JSON:\n{plan.model_dump_json()}"
    )
    return await _run_agent_stream(agent, prompt, run_store=run_store, run_dir=run_dir)
