import os

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from agentscope.tool import Toolkit

from vla_data_juicer_agents.navigation.execution_tools import (
    build_execution_tools,
)
from vla_data_juicer_agents.navigation.inspection import (
    classify_navigation_dataset_tool,
    inspect_raw_date_tool,
)
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState, build_plan_draft_tools


DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_NAVIGATION_MODEL = "qwen3.5-plus"


PLAN_AGENT_INSTRUCTIONS = """
You are the Navigation ReAct Plan-Agent.
Use only read-only tools to inspect and classify navigation datasets.
Maintain the internal WorkflowPlan draft with get_workflow_plan_draft_tool,
update_workflow_plan_draft_tool, and finalize_workflow_plan_tool.
After inspecting and classifying the dataset, call update_workflow_plan_draft_tool
with the inferred dataset_profile, then call finalize_workflow_plan_tool.
Do not hand-write script-level plans; final WorkflowPlan JSON must come from finalize_workflow_plan_tool.
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


def create_qwen_model(model: str | None = None) -> DashScopeChatModel:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required to create the navigation Qwen model.")

    base_url = os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL)
    model_name = model or os.environ.get("VLA_AGENT_MODEL", DEFAULT_NAVIGATION_MODEL)
    return DashScopeChatModel(
        credential=DashScopeCredential(api_key=api_key, base_url=base_url),
        model=model_name,
        stream=True,
    )


def _create_navigation_agent(name: str, instructions: str, tools: list, model: str | None = None) -> Agent:
    agent = Agent(
        name=name,
        system_prompt=instructions,
        model=create_qwen_model(model),
        toolkit=Toolkit(tools=tools),
    )
    # Keep the small compatibility surface used by tests and CLI diagnostics.
    agent.tools = tools
    agent.instructions = instructions
    return agent


def create_plan_agent(model: str | None = None, request: NavigationRequest | None = None) -> Agent:
    draft_state = WorkflowPlanDraftState(request=request) if request is not None else None
    draft_tools = build_plan_draft_tools(draft_state) if draft_state is not None else []
    agent = _create_navigation_agent(
        name="Navigation ReAct Plan-Agent",
        tools=[
            inspect_raw_date_tool,
            classify_navigation_dataset_tool,
            *draft_tools,
        ],
        instructions=PLAN_AGENT_INSTRUCTIONS,
        model=model,
    )
    agent.workflow_plan_draft_state = draft_state
    return agent


def create_executor_agent(model: str | None = None, dry_run: bool = False) -> Agent:
    instructions = EXECUTOR_AGENT_INSTRUCTIONS
    if dry_run:
        instructions = f"{instructions}\nDry-run mode is enabled; report planned actions without real mutations."

    return _create_navigation_agent(
        name="Navigation ReAct Executor-Agent",
        instructions=instructions,
        tools=build_execution_tools(dry_run=dry_run),
        model=model,
    )
