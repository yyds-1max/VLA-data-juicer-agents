import json
import re
from pathlib import Path
from typing import Any

from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import UserMsg
from pydantic import ValidationError

from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.plan_draft import build_plan_from_draft
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore


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


def build_deterministic_plan_template(
    date: str,
    dataset_profile: str,
    segments: list[str] | None,
) -> WorkflowPlan:
    finish_temp_path = _finish_temp_path(date)
    finish_path = _finish_path(date)
    common_arguments = {"date": date, "segments": segments}

    return WorkflowPlan(
        date=date,
        segments=segments,
        dataset_profile=dataset_profile,
        steps=[
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
            ),
            WorkflowStep(
                step_id="generate_gridmap_from_pcd",
                tool_name="generate_gridmap_from_pcd",
                arguments=common_arguments,
                preconditions=["extract_and_sync_navigation_data"],
                expected_outputs=[f"gridmap/{date}"],
                failure_behavior="skip_if_gridmap_exists",
            ),
            WorkflowStep(
                step_id="assemble_finish_temp",
                tool_name="assemble_finish_temp",
                arguments=common_arguments,
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
                step_id="run_tracking_and_projection",
                tool_name="run_tracking_and_projection",
                arguments={"finish_temp_path": finish_temp_path, "finish_path": finish_path},
                preconditions=["run_initial_annotation_gui"],
                expected_outputs=[finish_path],
            ),
            WorkflowStep(
                step_id="validate_navigation_outputs",
                tool_name="validate_navigation_outputs",
                arguments={"date": date},
                preconditions=["run_tracking_and_projection"],
                expected_outputs=[finish_path],
            ),
        ],
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
) -> str:
    output_chunks: list[str] = []
    tool_output_chunks: list[str] = []
    next_input: object = UserMsg(name="user", content=prompt)
    for _ in range(10):
        confirm_results: list[ConfirmResult] = []
        reply_id: str | None = None
        async for event in agent.reply_stream(next_input):
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
        if not confirm_results:
            return "".join(output_chunks) or "".join(tool_output_chunks)
        if reply_id is None:
            raise RuntimeError("AgentScope requested tool confirmation without a reply id.")
        next_input = UserConfirmResultEvent(reply_id=reply_id, confirm_results=confirm_results)
    raise RuntimeError("AgentScope tool confirmation loop exceeded 10 iterations.")


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
            "\n\nCurrent internal WorkflowPlan draft schema/status:\n"
            f"{json.dumps(draft_state.schema_snapshot(), ensure_ascii=False)}"
        )
    prompt = (
        "Use the inspect_raw_date_tool and classify_navigation_dataset_tool read-only tools to build a "
        "stage-one navigation WorkflowPlan. After classification, call update_workflow_plan_draft_tool "
        "with dataset_profile, then call finalize_workflow_plan_tool. Do not write script-level steps; "
        "final strict WorkflowPlan JSON must come from finalize_workflow_plan_tool. Stage one covers "
        "prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh. Default to all raw "
        "segments when segments are not specified. Supported dataset profiles are u_legacy_like and "
        "go2w_like. The only human-blocking step is gen_box.py via run_initial_annotation_gui.\n\n"
        f"NavigationRequest JSON:\n{request.model_dump_json()}"
        f"{draft_prompt}"
    )
    output = await _run_agent_stream(agent, prompt, run_store=run_store, run_dir=run_dir)
    if draft_state is not None and draft_state.finalized_plan is not None:
        return draft_state.finalized_plan
    if draft_state is not None and not output.strip() and draft_state.dataset_profile is not None:
        return build_plan_from_draft(draft_state)
    return _parse_workflow_plan_output(output)


async def run_executor_agent(
    agent,
    plan: WorkflowPlan,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
) -> str:
    prompt = (
        "Execute this WorkflowPlan JSON step-by-step using the matching execution tools. Stop on any "
        "failed tool result. The gen_box.py GUI step is human-blocking via run_initial_annotation_gui; "
        "wait until the human finishes before continuing. Return a concise final execution summary.\n\n"
        f"WorkflowPlan JSON:\n{plan.model_dump_json()}"
    )
    return await _run_agent_stream(agent, prompt, run_store=run_store, run_dir=run_dir)
