import os
from pathlib import Path

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from agentscope.tool import Toolkit

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.catalog import list_navigation_tool_capabilities_tool
from vla_data_juicer_agents.navigation.execution_tools import (
    build_execution_tools,
)
from vla_data_juicer_agents.navigation.inspection import (
    classify_navigation_dataset_tool,
    inspect_gridmap_artifacts_tool,
    inspect_processing_state_tool,
    inspect_raw_date_tool,
    inspect_runtime_assets_tool,
)
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState, build_plan_draft_tools


DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_NAVIGATION_MODEL = "qwen3.5-plus"


PUBLIC_PROGRESS_INSTRUCTIONS = """
Before each tool call, emit exactly one public progress update line:
Progress: <one or two concise, action-oriented sentences stating a reasoning summary and the next action>

This is a user-facing summary, not the full hidden chain-of-thought.
Do not reveal draft notes, prompts, or raw tool results.
The following SDK tool call is the actual action; do not print textual ReAct labels such as Thought: or Action:.
Never write tool calls as plain text such as ToolName[arguments]; use the registered SDK tool call interface.
""".strip()


PLAN_AGENT_INSTRUCTIONS = """
You are the ReAct Plan-Agent for a VLA multi-scenario data processing agent.
Use only read-only tools to inspect and classify navigation datasets.
Read and follow docs/navigation-plan-agent-guidance.md (navigation-plan-agent-guidance).
Build a lightweight NavigationDataProfile, not a large data inventory.
Use stage_variants, and choose only the variants exposed by list_navigation_tool_capabilities_tool.
Default to all raw segments if not specified.
scene_mode is required and must be either "in" or "out". It represents "indoor" and "outdoor", respectively.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
The only human-blocking step is gen_box.py, executed via run_initial_annotation_gui.
Prepare gridmap after run_tracking and before run_projection_and_trajectory.
Supported execution tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory.
Supported profiles are u_legacy_like and go2w_like.
""".strip() + "\n" + PUBLIC_PROGRESS_INSTRUCTIONS


DRAFT_PLAN_AGENT_INSTRUCTIONS = """
Maintain the internal WorkflowPlan draft with get_workflow_plan_draft_tool,
update_workflow_plan_draft_tool, and finalize_workflow_plan_tool.
Before each SDK tool call, inspect the current draft state: navigation_data_profile_schema,
data_profile_draft, filled_fields, missing_fields, next_tool_candidates, and ready_to_finish.
Each planning step must do exactly one step: call one read-only inspection/classification SDK tool,
then merge only the newly learned facts with update_workflow_plan_draft_tool(data_profile_patch=...).
Use data_profile_patch for partial NavigationDataProfile facts; do not invent a complete profile in one shot.
Only call finalize_workflow_plan_tool after ready_to_finish is true and missing_fields is empty.
Do not hand-write script-level plans; final WorkflowPlan JSON must come from finalize_workflow_plan_tool.
Do not output textual Action: lines or ToolName[arguments] strings.
""".strip()


def _load_plan_agent_guidance() -> str:
    guidance_path = Path(__file__).resolve().parents[3] / "docs" / "navigation-plan-agent-guidance.md"
    try:
        return guidance_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "navigation-plan-agent-guidance: use lightweight NavigationDataProfile and stage_variants."


def _plan_agent_instructions(*, include_draft_tools: bool) -> str:
    parts = [PLAN_AGENT_INSTRUCTIONS]
    if include_draft_tools:
        parts.append(DRAFT_PLAN_AGENT_INSTRUCTIONS)
        parts.append(f"Guidance excerpt:\n{_load_plan_agent_guidance()}")
    else:
        parts.append(
            "No request-bound draft tools are registered; return strict WorkflowPlan JSON if asked to plan directly. "
            "Guidance reference: navigation-plan-agent-guidance; use lightweight NavigationDataProfile, "
            "stage_variants, and list_navigation_tool_capabilities_tool."
        )
    return "\n\n".join(parts)


EXECUTOR_AGENT_INSTRUCTIONS = """
You are the ReAct Executor-Agent for a VLA multi-scenario data processing agent.
Read WorkflowPlan JSON and execute matching tools step-by-step.
For each WorkflowStep.tool_name, call the SDK tool with the same name plus "_tool"; for example,
prepare_raw_data maps to prepare_raw_data_tool and run_initial_annotation_gui maps to run_initial_annotation_gui_tool.
Stop on any failed tool result.
The gen_box.py GUI step is human-blocking via run_initial_annotation_gui and blocks until the human finishes.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
Default all raw segments if not specified.
scene_mode is required and must be either "in" or "out". It represents "indoor" and "outdoor", respectively.
Prepare gridmap after run_tracking and before run_projection_and_trajectory.
Supported execution tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory.
Supported profiles are u_legacy_like and go2w_like.
""".strip() + "\n" + PUBLIC_PROGRESS_INSTRUCTIONS


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
            inspect_processing_state_tool,
            inspect_gridmap_artifacts_tool,
            inspect_runtime_assets_tool,
            list_navigation_tool_capabilities_tool,
            *draft_tools,
        ],
        instructions=_plan_agent_instructions(include_draft_tools=draft_state is not None),
        model=model,
    )
    agent.workflow_plan_draft_state = draft_state
    return agent


def create_executor_agent(
    model: str | None = None,
    dry_run: bool = False,
    cancellation: CancellationContext | None = None,
) -> Agent:
    instructions = EXECUTOR_AGENT_INSTRUCTIONS
    if dry_run:
        instructions = f"{instructions}\nDry-run mode is enabled; report planned actions without real mutations."

    return _create_navigation_agent(
        name="Navigation ReAct Executor-Agent",
        instructions=instructions,
        tools=build_execution_tools(dry_run=dry_run, cancellation=cancellation),
        model=model,
    )
