"""AgentScope tools used by the navigation agents."""

from __future__ import annotations

import inspect
import json
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.catalog import list_navigation_tool_capabilities_tool
from vla_data_juicer_agents.navigation.config import NavigationSettings
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
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools


class HumanDecisionTool(ToolBase):
    """External tool that pauses navigation for a durable human decision."""

    name = "request_human_decision"
    description = (
        "Pause navigation workflow execution and ask the frontend to show a "
        "human decision dialog for calibration confirmation, overwrite/delete "
        "approval, stop, or user guidance. The dialog lets the user confirm "
        "the action, stop the workflow, or provide guidance before the agent "
        "continues."
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


class _TrustedNavigationTool(ToolBase):
    """Allow project-owned navigation tools to run without AgentScope prompts."""

    def __init__(self, tool: Any) -> None:
        self._tool = tool
        self.name = tool.name
        self.description = tool.description
        self.input_schema = tool.input_schema
        self.is_concurrency_safe = tool.is_concurrency_safe
        self.is_read_only = tool.is_read_only
        self.is_external_tool = tool.is_external_tool
        self.is_state_injected = getattr(tool, "is_state_injected", False)
        self.is_mcp = getattr(tool, "is_mcp", False)
        self.mcp_name = getattr(tool, "mcp_name", None)

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        decision = await self._tool.check_permissions(tool_input, context)
        if decision.behavior is PermissionBehavior.DENY:
            return decision
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"Navigation internal tool {self.name} is allowed.",
        )

    async def check_read_only(self, tool_input: dict[str, Any]) -> bool:
        return bool(await self._tool.check_read_only(tool_input))

    def match_rule(self, rule_content: str | None, tool_input: dict[str, Any]) -> bool:
        return bool(self._tool.match_rule(rule_content, tool_input))

    def generate_suggestions(self, tool_input: dict[str, Any]):
        return self._tool.generate_suggestions(tool_input)

    async def __call__(self, *args: Any, **kwargs: Any):
        result = self._tool(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


class _FinalizedPlanRequiredTool(ToolBase):
    """Block execution tools until the session draft has a finalized plan."""

    def __init__(
        self,
        tool: Any,
        *,
        session_id: str,
        draft_store: NavigationPlanDraftStore,
    ) -> None:
        self._tool = tool
        self._session_id = session_id
        self._draft_store = draft_store
        self.name = tool.name
        self.description = tool.description
        self.input_schema = tool.input_schema
        self.is_concurrency_safe = tool.is_concurrency_safe
        self.is_read_only = tool.is_read_only
        self.is_external_tool = tool.is_external_tool
        self.is_state_injected = getattr(tool, "is_state_injected", False)
        self.is_mcp = getattr(tool, "is_mcp", False)
        self.mcp_name = getattr(tool, "mcp_name", None)

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        gate_error = _finalized_plan_gate_error(
            session_id=self._session_id,
            draft_store=self._draft_store,
            tool_input=tool_input,
            check_segments=_tool_accepts_segments(self),
        )
        if gate_error is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=gate_error["message"],
            )
        return await self._tool.check_permissions(tool_input, context)

    async def check_read_only(self, tool_input: dict[str, Any]) -> bool:
        return bool(await self._tool.check_read_only(tool_input))

    def match_rule(self, rule_content: str | None, tool_input: dict[str, Any]) -> bool:
        return bool(self._tool.match_rule(rule_content, tool_input))

    def generate_suggestions(self, tool_input: dict[str, Any]):
        return self._tool.generate_suggestions(tool_input)

    async def __call__(self, *args: Any, **kwargs: Any):
        gate_error = _finalized_plan_gate_error(
            session_id=self._session_id,
            draft_store=self._draft_store,
            tool_input=kwargs,
            check_segments=_tool_accepts_segments(self),
        )
        if gate_error is not None:
            return gate_error
        result = self._tool(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


def _finalized_plan_gate_error(
    *,
    session_id: str,
    draft_store: NavigationPlanDraftStore,
    tool_input: dict[str, Any],
    check_segments: bool = True,
) -> dict[str, Any] | None:
    state = draft_store.load(session_id)
    if state is None:
        return {
            "ok": False,
            "error_type": "navigation_plan_not_finalized",
            "message": (
                "Navigation execution is blocked until a session workflow plan "
                "draft exists and has been finalized."
            ),
            "missing_fields": ["workflow_plan_draft"],
            "next_tool_candidates": ["get_workflow_plan_draft_tool"],
        }
    requested_date = tool_input.get("date")
    if isinstance(requested_date, str) and requested_date != state.request.date:
        return {
            "ok": False,
            "error_type": "navigation_plan_request_mismatch",
            "message": (
                "Navigation execution date does not match the finalized "
                "workflow plan draft for this AgentScope session."
            ),
            "existing_request": state.request.model_dump(mode="json"),
            "requested_request": {"date": requested_date},
            "draft": state.schema_snapshot(),
        }
    if state.finalized_plan is None:
        return {
            "ok": False,
            "error_type": "navigation_plan_not_finalized",
            "message": (
                "Navigation execution is blocked until finalize_workflow_plan_tool "
                "returns ok=true for this AgentScope session."
            ),
            "missing_fields": state.missing_fields(),
            "next_tool_candidates": state.next_tool_candidates(),
            "draft": state.schema_snapshot(),
        }
    if check_segments and isinstance(requested_date, str):
        requested_segments = _normalize_optional_string_list(tool_input.get("segments"))
        expected_segments = state.request.segments
        if requested_segments != expected_segments:
            return {
                "ok": False,
                "error_type": "navigation_plan_request_mismatch",
                "message": (
                    "Navigation execution segments do not match the finalized "
                    "workflow plan draft for this AgentScope session."
                ),
                "existing_request": state.request.model_dump(mode="json"),
                "requested_request": {
                    "date": requested_date,
                    "segments": requested_segments,
                },
                "draft": state.schema_snapshot(),
            }
    return None


def _tool_accepts_segments(tool: Any) -> bool:
    schema = getattr(tool, "input_schema", None)
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    return isinstance(properties, dict) and "segments" in properties


def _normalize_optional_string_list(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("["):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if isinstance(payload, list) and all(isinstance(item, str) for item in payload):
                return payload
        return [stripped]
    return value


def _trust_internal_navigation_tools(tools: list[Any]) -> list[Any]:
    return [
        tool
        if getattr(tool, "is_external_tool", False)
        else _TrustedNavigationTool(tool)
        for tool in tools
    ]


def _execution_tools_for_navigation_agent(
    *,
    dry_run: bool,
    cancellation: CancellationContext | None,
    session_id: str | None,
    draft_store: NavigationPlanDraftStore | None,
) -> list[Any]:
    tools = create_navigation_execution_tools(
        dry_run=dry_run,
        cancellation=cancellation,
    )
    if session_id is None or draft_store is None:
        return tools
    return [
        _FinalizedPlanRequiredTool(
            tool,
            session_id=session_id,
            draft_store=draft_store,
        )
        for tool in tools
    ]


def build_navigation_agent_tools(
    *,
    dry_run: bool = False,
    cancellation: CancellationContext | None = None,
    session_id: str | None = None,
    draft_store: NavigationPlanDraftStore | None = None,
    task_store: SqliteNavigationTaskStore | None = None,
    web_session_id: str | None = None,
    settings: NavigationSettings | None = None,
) -> list[Any]:
    task_tools: list[Any] = []
    if task_store is not None and session_id is not None:
        task_tools = build_navigation_task_tools(
            store=task_store,
            session_id=session_id,
            web_session_id=web_session_id,
            settings=settings,
        )
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
    return _trust_internal_navigation_tools([
        *task_tools,
        HumanDecisionTool(),
        *planning_tools,
        *draft_tools,
        *_execution_tools_for_navigation_agent(
            dry_run=dry_run,
            cancellation=cancellation,
            session_id=session_id,
            draft_store=draft_store,
        ),
    ])
