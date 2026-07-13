"""Durable phase-aware AgentScope tools for navigation processing."""

from __future__ import annotations

import inspect
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolBase

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.catalog import list_navigation_tool_capabilities
from vla_data_juicer_agents.navigation.context_budget import ensure_payload_within_limit, serialized_chars
from vla_data_juicer_agents.navigation.observation_tools import build_navigation_observation_tools
from vla_data_juicer_agents.navigation.plan_execution import build_plan_bound_execution_tools
from vla_data_juicer_agents.navigation.plan_submission_tools import build_navigation_plan_submission_tools
from vla_data_juicer_agents.navigation.planning_context import PHASE_REQUIRED_OBSERVATIONS
from vla_data_juicer_agents.navigation.services import NavigationServices
from vla_data_juicer_agents.navigation.task_reconciliation import reconcile_navigation_task
from vla_data_juicer_agents.navigation.task_store import NavigationTaskStateRevisionError
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


_INSPECTION_TOOL_BY_KIND = {
    "raw_metadata": "inspect_navigation_raw_metadata_tool",
    "sensor_candidates": "inspect_navigation_sensor_candidates_tool",
    "topic_candidates": "inspect_navigation_topic_candidates_tool",
    "artifact_state": "inspect_navigation_artifact_state_tool",
    "gridmap_artifacts": "inspect_navigation_gridmap_artifacts_tool",
    "runtime_assets": "inspect_navigation_runtime_assets_tool",
    "calibration_inventory": "inspect_navigation_calibration_inventory_tool",
    "localization_sources": "inspect_navigation_localization_sources_tool",
}
_COGNITIVE_TOOL_NAMES = {
    "get_navigation_task_context_tool", "list_observation_evidence_tool",
    "read_observation_evidence_tool", "describe_processing_action_tool",
}


def _compact_task_tools(*, services: NavigationServices, agentscope_session_id: str, web_session_id: str | None) -> list[ToolBase]:
    tools = build_navigation_task_tools(
        store=services.task_store,
        session_id=agentscope_session_id,
        web_session_id=web_session_id,
        settings=services.settings,
    )
    return [tool for tool in tools if tool.name == "get_or_create_navigation_task_tool"]


def _durable_state_tools(*, services: NavigationServices, task: Any) -> list[ToolBase]:
    def get_navigation_task_state_tool() -> dict[str, Any]:
        current = services.task_store.get_task(task.task_id)
        if current is None:
            return {"ok": False, "error_type": "navigation_task_not_found"}
        return {"ok": True, "task_id": current.task_id, "phase": current.phase.value, "status": current.status.value}

    def list_navigation_task_evidence_tool(cursor: int = 0, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        rows = services.observation_store.list_evidence(task.task_id, cursor=cursor, limit=limit + 1)
        result: dict[str, Any] = {"evidence": [], "next_cursor": None}
        for row in rows[:limit]:
            evidence = [*result["evidence"], row.model_dump(mode="json")]
            candidate = {"evidence": evidence, "next_cursor": cursor + len(evidence) if len(rows) > len(evidence) else None}
            if serialized_chars(candidate) > 4_000:
                break
            result = candidate
        return ensure_payload_within_limit(result, max_chars=4_000, label="completed_navigation_evidence_list")

    def read_navigation_task_evidence_tool(ref: str, fields: list[str] | None = None, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
        return ensure_payload_within_limit(
            services.evidence_store.read(task.task_id, ref, fields=fields, cursor=cursor, limit=limit, max_chars=4_000),
            max_chars=4_000,
            label="completed_navigation_evidence_read",
        )

    return [
        FunctionTool(get_navigation_task_state_tool, is_read_only=True),
        FunctionTool(list_navigation_task_evidence_tool, is_read_only=True),
        FunctionTool(read_navigation_task_evidence_tool, is_read_only=True),
    ]


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
    if web_session_id is not None and not (
        agentscope_session_id == web_session_id or agentscope_session_id.startswith(f"{web_session_id}__")
    ):
        return []
    task = services.task_store.find_latest_by_agentscope_session(agentscope_session_id)
    if task is None:
        return _trust(_compact_task_tools(services=services, agentscope_session_id=agentscope_session_id, web_session_id=web_session_id))

    reconciled = reconcile_navigation_task(task, settings=services.settings)
    changes = reconciled.model_dump(mode="json")
    for field in ("task_id", "created_by_web_session_id", "latest_web_session_id", "agentscope_session_id", "created_at", "updated_at", "state_revision"):
        changes.pop(field, None)
    try:
        task = services.task_store.update_task_for_session(
            task.task_id,
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
            expected_state_revision=task.state_revision,
            **changes,
        )
    except (PermissionError, NavigationTaskStateRevisionError):
        return []

    if task.phase.value == "completed" or task.status.value == "completed":
        return _trust(_durable_state_tools(services=services, task=task))
    if task.phase.value not in PHASE_REQUIRED_OBSERVATIONS:
        task_tools = build_navigation_task_tools(
            store=services.task_store,
            session_id=agentscope_session_id,
            web_session_id=web_session_id,
            settings=services.settings,
            bound_task=task,
        )
        allowed = {"get_or_create_navigation_task_tool", "reconcile_navigation_task_tool", "update_navigation_task_scene_mode_tool"}
        return _trust([tool for tool in task_tools if tool.name in allowed])

    observation = services.observation_store.latest(task.task_id)
    completed = set(observation.completed_kinds) if observation is not None else set()
    missing = [kind for kind in PHASE_REQUIRED_OBSERVATIONS[task.phase.value] if kind not in completed]
    observation_tools = build_navigation_observation_tools(
        task=task,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        settings=services.settings,
        expected_web_session_id=web_session_id,
        expected_agentscope_session_id=agentscope_session_id,
    )
    cognitive = [tool for tool in observation_tools if tool.name in _COGNITIVE_TOOL_NAMES]
    active_plan = services.plan_store.get_active(task.task_id, task.phase.value)
    if active_plan is not None:
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
        current = services.plan_store.get_current_step(active_plan.plan_id)
        step = (current or {}).get("step") or {}
        if step.get("action") == "confirm_navigation_calibration_params":
            handoff = services.plan_store.get_human_decision_handoff(active_plan.plan_id, step.get("step_id", ""))
            if handoff is not None and handoff.status == "recovery_required":
                execution = [tool for tool in execution if tool.name != "request_human_decision"]
        return _trust([*_execution_state_tools(services=services, plan=active_plan), *execution])
    if missing:
        names = {_INSPECTION_TOOL_BY_KIND[kind] for kind in missing}
        return _trust([*[tool for tool in observation_tools if tool.name in names], *cognitive])
    submission = build_navigation_plan_submission_tools(
        task=task,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        plan_store=services.plan_store,
        capabilities=list_navigation_tool_capabilities(),
        expected_web_session_id=web_session_id,
        expected_agentscope_session_id=agentscope_session_id,
    )
    return _trust([*cognitive, *submission])
