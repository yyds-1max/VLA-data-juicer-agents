import json
import re
from typing import Any

from pydantic import ValidationError
from agents import Runner

from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep


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


async def run_plan_agent(agent, request: NavigationRequest) -> WorkflowPlan:
    prompt = (
        "Use the inspect_raw_date_tool and classify_navigation_dataset_tool read-only tools to build a "
        "stage-one navigation WorkflowPlan. Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; "
        "do not include run_fix.sh. Default to all raw segments when segments are not specified. "
        "Supported dataset profiles are u_legacy_like and go2w_like. The only human-blocking step is "
        "gen_box.py via run_initial_annotation_gui. Return valid WorkflowPlan JSON only.\n\n"
        f"NavigationRequest JSON:\n{request.model_dump_json()}"
    )
    result = await Runner.run(agent, prompt)
    return _parse_workflow_plan_output(result.final_output)


async def run_executor_agent(agent, plan: WorkflowPlan) -> str:
    prompt = (
        "Execute this WorkflowPlan JSON step-by-step using the matching execution tools. Stop on any "
        "failed tool result. The gen_box.py GUI step is human-blocking via run_initial_annotation_gui; "
        "wait until the human finishes before continuing. Return a concise final execution summary.\n\n"
        f"WorkflowPlan JSON:\n{plan.model_dump_json()}"
    )
    result = await Runner.run(agent, prompt)
    return str(result.final_output)
