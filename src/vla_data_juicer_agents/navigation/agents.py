import os

from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI

from vla_data_juicer_agents.navigation.execution_tools import (
    assemble_finish_temp_tool,
    extract_and_sync_navigation_data_tool,
    generate_gridmap_from_pcd_tool,
    prepare_raw_data_tool,
    run_initial_annotation_gui_tool,
    run_noobscene_preprocessing_tool,
    run_tracking_and_projection_tool,
    validate_navigation_outputs_tool,
)
from vla_data_juicer_agents.navigation.inspection import (
    classify_navigation_dataset_tool,
    inspect_raw_date_tool,
)


DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_NAVIGATION_MODEL = "qwen3.5-plus"


PLAN_AGENT_INSTRUCTIONS = """
You are the Navigation ReAct Plan-Agent.
Use only read-only tools to inspect and classify navigation datasets.
Return valid WorkflowPlan JSON and no prose.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
Only human-blocking step is gen_box.py via run_initial_annotation_gui.
Default all raw segments if not specified.
Supported profiles are u_legacy_like and go2w_like.
""".strip()


EXECUTOR_AGENT_INSTRUCTIONS = """
You are the Navigation ReAct Executor-Agent.
Read WorkflowPlan JSON and execute matching tools step-by-step.
For each WorkflowStep.tool_name, call the SDK tool with the same name plus "_tool"; for example,
prepare_raw_data maps to prepare_raw_data_tool and run_initial_annotation_gui maps to run_initial_annotation_gui_tool.
Stop on any failed tool result.
The gen_box.py GUI step is human-blocking via run_initial_annotation_gui and blocks until the human finishes.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
Default all raw segments if not specified.
Supported profiles are u_legacy_like and go2w_like.
""".strip()


def create_qwen_model(model: str | None = None) -> OpenAIChatCompletionsModel:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required to create the navigation Qwen model.")

    base_url = os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL)
    model_name = model or os.environ.get("VLA_AGENT_MODEL", DEFAULT_NAVIGATION_MODEL)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def create_plan_agent(model: str | None = None) -> Agent:
    set_tracing_disabled(True)
    return Agent(
        name="Navigation ReAct Plan-Agent",
        instructions=PLAN_AGENT_INSTRUCTIONS,
        model=create_qwen_model(model),
        tools=[
            inspect_raw_date_tool,
            classify_navigation_dataset_tool,
        ],
    )


def create_executor_agent(model: str | None = None, dry_run: bool = False) -> Agent:
    set_tracing_disabled(True)
    instructions = EXECUTOR_AGENT_INSTRUCTIONS
    if dry_run:
        instructions = f"{instructions}\nDry-run mode is enabled; report planned actions without real mutations."

    return Agent(
        name="Navigation ReAct Executor-Agent",
        instructions=instructions,
        model=create_qwen_model(model),
        tools=[
            prepare_raw_data_tool,
            extract_and_sync_navigation_data_tool,
            generate_gridmap_from_pcd_tool,
            assemble_finish_temp_tool,
            run_noobscene_preprocessing_tool,
            run_initial_annotation_gui_tool,
            run_tracking_and_projection_tool,
            validate_navigation_outputs_tool,
        ],
    )
