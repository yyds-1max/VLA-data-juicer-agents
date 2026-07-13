"""Activity-driven AgentScope tools for one bound navigation attempt."""

from __future__ import annotations

import inspect
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolBase

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.catalog import list_navigation_tool_capabilities
from vla_data_juicer_agents.navigation.context_budget import ensure_payload_within_limit
from vla_data_juicer_agents.navigation.observation_tools import build_navigation_observation_tools
from vla_data_juicer_agents.navigation.plan_execution import build_plan_bound_execution_tools
from vla_data_juicer_agents.navigation.plan_submission_tools import build_navigation_plan_submission_tools
from vla_data_juicer_agents.navigation.services import NavigationServices
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools


class PlanBoundHumanDecisionTool(ToolBase):
    name = "request_human_decision"
    description = (
        "Pause the current immutable navigation-plan step; provide only plan_id and step_id."
    )
    input_schema = {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}, "step_id": {"type": "string"}},
        "required": ["plan_id", "step_id"],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = True

    def __init__(self, *, gate: Any | None = None) -> None:
        self._gate = gate

    async def check_permissions(self, tool_input: dict[str, Any], context: object) -> PermissionDecision:
        if self._gate is not None:
            error = self._gate(tool_input)
            if error is not None:
                return PermissionDecision(behavior=PermissionBehavior.DENY, message=error["message"])
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Plan-bound human decision requests are allowed.",
        )


class _TrustedNavigationTool(ToolBase):
    def __init__(self, tool: Any) -> None:
        self._tool = tool
        for name in (
            "name", "description", "input_schema", "is_concurrency_safe", "is_read_only",
            "is_external_tool", "is_state_injected", "is_mcp", "mcp_name",
        ):
            if hasattr(tool, name):
                setattr(self, name, getattr(tool, name))

    async def check_permissions(self, tool_input: dict[str, Any], context: object) -> PermissionDecision:
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
        return await result if inspect.isawaitable(result) else result


def _trust(tools: list[Any]) -> list[Any]:
    return [tool if getattr(tool, "is_external_tool", False) else _TrustedNavigationTool(tool) for tool in tools]


def _execution_state_tools(*, services: NavigationServices, plan: Any) -> list[ToolBase]:
    def get_plan_execution_overview_tool(plan_id: str) -> dict[str, Any]:
        if plan_id != plan.plan_id:
            return {"ok": False, "error_type": "inactive_navigation_plan"}
        return ensure_payload_within_limit(
            services.plan_store.get_execution_overview(plan_id).model_dump(mode="json"),
            max_chars=4_000,
            label="resolved_execution_overview",
        )

    def get_current_plan_step_tool(plan_id: str) -> dict[str, Any] | None:
        if plan_id != plan.plan_id:
            return {"ok": False, "error_type": "inactive_navigation_plan"}
        current = services.plan_store.get_current_step(plan_id)
        return None if current is None else ensure_payload_within_limit(current, max_chars=4_000, label="resolved_current_plan_step")

    return [FunctionTool(get_plan_execution_overview_tool, is_read_only=True), FunctionTool(get_current_plan_step_tool, is_read_only=True)]


def resolve_navigation_agent_tools(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
) -> list[ToolBase]:
    if web_session_id is None or not (
        agentscope_session_id == web_session_id or agentscope_session_id.startswith(f"{web_session_id}__")
    ):
        return []
    task = services.task_store.find_by_session(
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    )
    if task is None:
        return []
    if (
        task.created_by_web_session_id != web_session_id
        or task.latest_web_session_id != web_session_id
        or task.agentscope_session_id != agentscope_session_id
    ):
        return []

    observation_tools = build_navigation_observation_tools(
        task=task,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        settings=services.settings,
        expected_web_session_id=web_session_id,
        expected_agentscope_session_id=agentscope_session_id,
    )
    active_plan = services.plan_store.get_active_for_task(task.task_id)
    current = (
        services.plan_store.get_current_step(active_plan.plan_id)
        if active_plan is not None
        else None
    )
    current_status = ((current or {}).get("step") or {}).get("status")
    if active_plan is not None and current_status in {
        "pending",
        "running",
        "waiting_user",
    }:
        execution_state = _execution_state_tools(services=services, plan=active_plan)
        execution = build_plan_bound_execution_tools(
            task=task,
            plan_store=services.plan_store,
            evidence_store=services.evidence_store,
            settings=services.settings,
            dry_run=task.dry_run,
            cancellation=cancellation,
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
        )
        step = (current or {}).get("step") or {}
        if step.get("action") == "confirm_navigation_calibration_params":
            handoff = services.plan_store.get_human_decision_handoff(active_plan.plan_id, step.get("step_id", ""))
            if handoff is not None and handoff.status == "recovery_required":
                return _trust(execution_state)
        return _trust([*execution_state, *execution])

    guidance = build_navigation_task_tools(
        store=services.task_store,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        session_id=agentscope_session_id,
        web_session_id=web_session_id,
        settings=services.settings,
        bound_task=task,
    )
    submission = build_navigation_plan_submission_tools(
        task=task,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        plan_store=services.plan_store,
        capabilities=list_navigation_tool_capabilities(),
        expected_web_session_id=web_session_id,
        expected_agentscope_session_id=agentscope_session_id,
    )
    return _trust([*observation_tools, *guidance, *submission])
