from agents import Runner

from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep


def _finish_temp_path(date: str) -> str:
    return f"finish_temp/{date}"


def _finish_path(date: str) -> str:
    return f"finish/{date}"


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
    return WorkflowPlan.model_validate_json(result.final_output)


async def run_executor_agent(agent, plan: WorkflowPlan) -> str:
    prompt = (
        "Execute this WorkflowPlan JSON step-by-step using the matching execution tools. Stop on any "
        "failed tool result. The gen_box.py GUI step is human-blocking via run_initial_annotation_gui; "
        "wait until the human finishes before continuing. Return a concise final execution summary.\n\n"
        f"WorkflowPlan JSON:\n{plan.model_dump_json()}"
    )
    result = await Runner.run(agent, prompt)
    return str(result.final_output)
