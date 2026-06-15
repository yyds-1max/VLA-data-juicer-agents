from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from vla_data_juicer_agents.core.tool import ToolContext, ToolSpec
from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.workflow import run_executor_agent, run_plan_agent


class RunVLAWorkflowInput(BaseModel):
    date: str
    segments: str | list[str] | None = None
    dry_run: bool = False
    approve: bool = True
    model: str | None = None
    scenario: Literal["navigation_vla"] = "navigation_vla"


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
    return [item.strip() for item in raw.split(",") if item.strip()] or None


def _artifact_paths(run_dir) -> dict[str, str]:
    return {
        "request": str(run_dir / "request.json"),
        "plan": str(run_dir / "plan.json"),
        "final_report": str(run_dir / "final_report.json"),
        "events": str(run_dir / "events.jsonl"),
    }


async def run_vla_workflow(ctx: ToolContext, raw_args: RunVLAWorkflowInput | dict[str, Any]) -> dict[str, Any]:
    args = raw_args if isinstance(raw_args, RunVLAWorkflowInput) else RunVLAWorkflowInput.model_validate(raw_args)
    request = NavigationRequest(
        date=args.date,
        segments=_normalize_segments(args.segments),
        dry_run=args.dry_run,
    )
    settings = NavigationSettings()
    run_store = WorkflowRunStore(settings.runs_root)
    run_dir = run_store.create_run(request.date)
    run_store.write_json(run_dir, "request.json", request.model_dump(mode="json"))

    try:
        plan_agent = create_plan_agent(model=args.model, request=request)
        plan = await run_plan_agent(plan_agent, request, run_store=run_store, run_dir=run_dir)
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
            return payload

        executor_agent = create_executor_agent(model=args.model, dry_run=args.dry_run)
        final_output = await run_executor_agent(executor_agent, plan, run_store=run_store, run_dir=run_dir)
        payload = RunVLAWorkflowOutput(
            ok=True,
            status="completed",
            run_dir=str(run_dir),
            artifacts=_artifact_paths(run_dir),
            final_output=final_output,
            message=final_output,
        ).model_dump(mode="json")
        run_store.write_json(run_dir, "final_report.json", payload)
        return payload
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
        return payload


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

