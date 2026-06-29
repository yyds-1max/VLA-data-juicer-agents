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
    infer_navigation_processing_profile_tool,
    infer_navigation_sensor_bindings_tool,
    infer_navigation_topic_params_tool,
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
Use the explicit response_language from the current workflow prompt when provided; otherwise answer in the user's language.
""".strip()



PLAN_AGENT_INSTRUCTIONS = """
You are the ReAct Plan-Agent for a VLA multi-scenario data processing agent.
Use only read-only tools to inspect navigation datasets.
Read and follow docs/navigation-plan-agent-guidance.md (navigation-plan-agent-guidance).
Build a lightweight NavigationDataProfile from sensor bindings and processing_profile, not a large data inventory.
First inspect raw metadata topics with inspect_raw_date_tool, then call infer_navigation_sensor_bindings_tool
and infer_navigation_processing_profile_tool.
Call infer_navigation_topic_params_tool before finalizing extract_and_sync_navigation_data parameters.
Do not require data to match fixed profiles such as u_legacy_like or go2w_like.
Do not invent TOPIC_WHITELIST, topic_map, query_dir, localization policy, or calibration policy; use tool results.
Only finalize when processing_profile has no blocking_issues.
Always include confirm_navigation_calibration_params as the first step before any processing and before prepare_raw_data.
Use stage_variants, and choose only the variants exposed by list_navigation_tool_capabilities_tool.
Default to all raw segments if not specified.
scene_mode is required and must be either "in" or "out". It represents "indoor" and "outdoor", respectively.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
Calibration confirmation and gen_box.py are the human-blocking user intervention points.
confirm_navigation_calibration_params must run first before prepare_raw_data and any processing.
Prepare gridmap after run_tracking and before run_projection_and_trajectory.
Supported execution tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory.
""".strip() + "\n" + PUBLIC_PROGRESS_INSTRUCTIONS


DRAFT_PLAN_AGENT_INSTRUCTIONS = """
Maintain the internal WorkflowPlan draft with get_workflow_plan_draft_tool,
update_workflow_plan_draft_tool, and finalize_workflow_plan_tool.
Before each SDK tool call, inspect the current draft state: navigation_data_profile_schema,
data_profile_draft, filled_fields, missing_fields, next_tool_candidates, and ready_to_finish.
Each planning step must do exactly one step: call one read-only inspection SDK tool,
then merge only the newly learned facts with update_workflow_plan_draft_tool(data_profile_patch=...).
Use infer_navigation_sensor_bindings_tool for sensor_bindings and infer_navigation_processing_profile_tool for
processing_profile, localization_policy, calibration_policy, platform_hint, and stage_variants.
When topic_params is missing, call infer_navigation_topic_params_tool and merge its structured result.
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
The first WorkflowPlan step must be confirm_navigation_calibration_params; confirm camera and sensor parameters before prepare_raw_data or any processing.
When executing confirm_navigation_calibration_params, stop and wait for exact user input.
Do not provide user_confirmation yourself during the initial workflow turn; call the tool without user_confirmation, show the confirmation prompt, and stop.
Continue only when user_confirmation is exactly `确认`.
If the user enters `终止` or anything else, stop workflow and report calibration_params_not_confirmed.
run_noobscene_preprocessing receives localization_source and localization_conversion from WorkflowPlan.
The gen_box.py GUI step is human-blocking via run_initial_annotation_gui and blocks until the human finishes.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
Default all raw segments if not specified.
scene_mode is required and must be either "in" or "out". It represents "indoor" and "outdoor", respectively.
Prepare gridmap after run_tracking and before run_projection_and_trajectory.
Supported execution tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory.
""".strip() + "\n" + PUBLIC_PROGRESS_INSTRUCTIONS


RESUME_EXECUTOR_AGENT_INSTRUCTIONS = """
You are the ReAct Executor-Agent for a VLA multi-scenario data processing agent.
Read WorkflowPlan JSON and execute matching tools step-by-step.
For each WorkflowStep.tool_name, call the SDK tool with the same name plus "_tool"; for example,
prepare_raw_data maps to prepare_raw_data_tool and run_initial_annotation_gui maps to run_initial_annotation_gui_tool.
Stop on any failed tool result.
This is a resumed execution from an already-confirmed checkpoint.
confirm_navigation_calibration_params was completed in the previous turn and is intentionally omitted from the remaining WorkflowPlan.
Do not call confirm_navigation_calibration_params again, do not wait for calibration confirmation again, and execute the supplied remaining WorkflowPlan as given.
run_noobscene_preprocessing receives localization_source and localization_conversion from WorkflowPlan.
The gen_box.py GUI step remains human-blocking via run_initial_annotation_gui and blocks until the human finishes.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh only; do not include run_fix.sh.
Default all raw segments if not specified.
scene_mode is required and must be either "in" or "out". It represents "indoor" and "outdoor", respectively.
Prepare gridmap after run_tracking and before run_projection_and_trajectory.
Supported execution tool names include run_tracking, prepare_gridmap_for_projection, and run_projection_and_trajectory.
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
            infer_navigation_sensor_bindings_tool,
            infer_navigation_processing_profile_tool,
            infer_navigation_topic_params_tool,
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
    resume_from_checkpoint: bool = False,
) -> Agent:
    instructions = RESUME_EXECUTOR_AGENT_INSTRUCTIONS if resume_from_checkpoint else EXECUTOR_AGENT_INSTRUCTIONS
    if dry_run:
        instructions = f"{instructions}\nDry-run mode is enabled; report planned actions without real mutations."

    return _create_navigation_agent(
        name="Navigation ReAct Executor-Agent",
        instructions=instructions,
        tools=build_execution_tools(dry_run=dry_run, cancellation=cancellation),
        model=model,
    )
