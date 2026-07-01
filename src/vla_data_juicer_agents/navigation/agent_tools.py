"""AgentScope tools used by the navigation agents."""

from __future__ import annotations

from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.execution_tools import create_navigation_execution_tools


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
) -> list[Any]:
    return [
        HumanDecisionTool(),
        *create_navigation_execution_tools(
            dry_run=dry_run,
            cancellation=cancellation,
        ),
    ]
