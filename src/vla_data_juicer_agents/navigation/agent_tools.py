"""AgentScope tools used by the navigation agents."""

from __future__ import annotations

from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.catalog import list_navigation_tool_capabilities_tool
from vla_data_juicer_agents.navigation.execution_tools import create_navigation_execution_tools
from vla_data_juicer_agents.navigation.inspection import (
    infer_navigation_processing_profile_tool,
    infer_navigation_sensor_bindings_tool,
    infer_navigation_topic_params_tool,
    inspect_gridmap_artifacts_tool,
    inspect_processing_state_tool,
    inspect_raw_date_tool,
    inspect_runtime_assets_tool,
)
from vla_data_juicer_agents.navigation.plan_draft_store import NavigationPlanDraftStore
from vla_data_juicer_agents.navigation.session_plan_draft_tools import (
    build_session_plan_draft_tools,
)


class HumanDecisionTool(ToolBase):
    """External tool that pauses navigation for a durable human decision."""

    name = "request_human_decision"
    description = (
        "Pause navigation workflow execution and ask the frontend to show a "
        "human decision dialog. The dialog lets the user confirm the action, "
        "stop the workflow, or provide guidance before the agent continues."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "decision_type": {
                "type": "string",
                "enum": ["camera_params", "overwrite", "delete", "other"],
            },
            "request_id": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["decision_type", "request_id", "summary"],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = True

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Human decision requests are allowed.",
        )


def build_navigation_agent_tools(
    *,
    dry_run: bool = False,
    cancellation: CancellationContext | None = None,
    session_id: str | None = None,
    draft_store: NavigationPlanDraftStore | None = None,
) -> list[Any]:
    planning_tools: list[Any] = [
        inspect_raw_date_tool,
        infer_navigation_sensor_bindings_tool,
        infer_navigation_processing_profile_tool,
        infer_navigation_topic_params_tool,
        inspect_processing_state_tool,
        inspect_gridmap_artifacts_tool,
        inspect_runtime_assets_tool,
        list_navigation_tool_capabilities_tool,
    ]
    draft_tools: list[Any] = []
    if session_id is not None and draft_store is not None:
        draft_tools = build_session_plan_draft_tools(
            store=draft_store,
            session_id=session_id,
        )
    return [
        HumanDecisionTool(),
        *planning_tools,
        *draft_tools,
        *create_navigation_execution_tools(
            dry_run=dry_run,
            cancellation=cancellation,
        ),
    ]
