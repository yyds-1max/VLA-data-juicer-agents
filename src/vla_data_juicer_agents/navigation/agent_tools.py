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
from vla_data_juicer_agents.navigation.plan_store import NavigationExecutionSnapshot
from vla_data_juicer_agents.navigation.plan_submission_tools import build_navigation_plan_submission_tools
from vla_data_juicer_agents.navigation.services import NavigationServices
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools
from vla_data_juicer_agents.navigation.tool_groups import (
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_DIAGNOSTICS,
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_PLAN_AUTHORING,
    NavigationToolGroupDefinition,
    NavigationToolSurface,
    NavigationToolSurfacePolicy,
    classify_fixed_navigation_tools,
)


_OBSERVATION_FIXED_TOOL_NAMES = {
    "list_observation_evidence_tool",
    "read_observation_evidence_tool",
    "inspect_navigation_raw_metadata_tool",
    "inspect_navigation_sensor_candidates_tool",
    "inspect_navigation_topic_candidates_tool",
    "inspect_navigation_runtime_assets_tool",
    "inspect_navigation_calibration_inventory_tool",
    "inspect_navigation_localization_sources_tool",
    "inspect_navigation_artifact_state_tool",
    "inspect_navigation_gridmap_artifacts_tool",
    "get_navigation_task_context_tool",
    "describe_processing_action_tool",
}
_EXECUTION_STATE_TOOL_NAMES = {
    "get_plan_execution_overview_tool",
    "get_current_plan_step_tool",
}
_FAILED_RESULT_TOOL_NAMES = {"read_navigation_step_result_tool"}
_FIXED_TOOL_NAMES_BY_ACTIVITY = {
    "planning": _OBSERVATION_FIXED_TOOL_NAMES
    | {
        "record_navigation_user_guidance_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    },
    "execution": _OBSERVATION_FIXED_TOOL_NAMES | _EXECUTION_STATE_TOOL_NAMES,
    "recovery_required": _OBSERVATION_FIXED_TOOL_NAMES
    | _EXECUTION_STATE_TOOL_NAMES,
    "failed_recovery": _OBSERVATION_FIXED_TOOL_NAMES
    | _EXECUTION_STATE_TOOL_NAMES
    | _FAILED_RESULT_TOOL_NAMES,
}

_GROUP_DESCRIPTIONS = {
    NAVIGATION_EVIDENCE_READ: "Read bounded evidence captured for this navigation task.",
    NAVIGATION_INVESTIGATION: "Inspect navigation inputs and runtime candidates.",
    NAVIGATION_ARTIFACT_CHECKS: "Inspect current navigation artifact state.",
    NAVIGATION_PLAN_AUTHORING: "Inspect planning context, record guidance, and submit a complete Plan.",
    NAVIGATION_EXECUTION_STATE: "Read the active Plan and current step.",
    NAVIGATION_EXECUTION_ACTIONS: "Execute actions authorized by the active Plan.",
    NAVIGATION_DIAGNOSTICS: "Read bounded failed-step diagnostics.",
}


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


def _execution_state_tools(
    *,
    services: NavigationServices,
    snapshot: Any,
    web_session_id: str,
    agentscope_session_id: str,
) -> list[ToolBase]:
    plan = snapshot.active_plan

    def authorized_snapshot(plan_id: str) -> Any | None:
        if plan is None or plan_id != plan.plan_id:
            return None
        current = services.plan_store.read_execution_snapshot(
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
            task_id=snapshot.task.task_id,
        )
        if (
            current is None
            or current.active_plan is None
            or current.active_plan.plan_id != plan_id
        ):
            return None
        return current

    def get_plan_execution_overview_tool(plan_id: str) -> dict[str, Any]:
        current = authorized_snapshot(plan_id)
        if current is None or current.overview is None:
            return {"ok": False, "error_type": "inactive_navigation_plan"}
        return ensure_payload_within_limit(
            current.overview.model_dump(mode="json"),
            max_chars=4_000,
            label="resolved_execution_overview",
        )

    def get_current_plan_step_tool(plan_id: str) -> dict[str, Any] | None:
        current = authorized_snapshot(plan_id)
        if current is None:
            return {"ok": False, "error_type": "inactive_navigation_plan"}
        if current.current is None:
            return None
        payload = {
            **current.current,
            "step": {**current.current["step"]},
        }
        payload["step"].pop("result_ref", None)
        return ensure_payload_within_limit(
            payload,
            max_chars=4_000,
            label="resolved_current_plan_step",
        )

    return [FunctionTool(get_plan_execution_overview_tool, is_read_only=True), FunctionTool(get_current_plan_step_tool, is_read_only=True)]


def _failed_step_side_effect_state(snapshot: NavigationExecutionSnapshot) -> str:
    summary = ((snapshot.current or {}).get("step") or {}).get("result_summary")
    if isinstance(summary, dict) and summary.get("side_effect_state") == "not_started":
        return "not_started"
    return "partial_or_unknown"


def _failed_step_result_tools(
    *,
    services: NavigationServices,
    snapshot: NavigationExecutionSnapshot,
    web_session_id: str,
    agentscope_session_id: str,
) -> list[ToolBase]:
    def unavailable() -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": "navigation_step_result_unavailable",
        }

    def invalid_request() -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": "navigation_step_result_request_invalid",
        }

    def read_navigation_step_result_tool(
        plan_id: str,
        step_id: str,
        fields: list[str] | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read selected, paginated evidence for the current failed Plan step."""
        if (
            not isinstance(plan_id, str)
            or not isinstance(step_id, str)
            or not plan_id
            or not step_id
            or len(plan_id) > 200
            or len(step_id) > 200
            or fields is not None
            and (
                not isinstance(fields, list)
                or len(fields) > 20
                or any(
                    not isinstance(field, str) or not field or len(field) > 200
                    for field in fields
                )
            )
            or not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < 0
            or cursor > 1_000_000_000
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 100
        ):
            return invalid_request()
        current = services.plan_store.read_execution_snapshot(
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
            task_id=snapshot.task.task_id,
        )
        current_step = ((current.current if current is not None else None) or {}).get(
            "step"
        )
        if (
            current is None
            or current.activity != "failed_recovery"
            or current.active_plan is None
            or current.active_plan.plan_id != plan_id
            or not isinstance(current_step, dict)
            or current_step.get("step_id") != step_id
            or current_step.get("status") not in {"failed", "needs_replan"}
            or not isinstance(current_step.get("result_ref"), str)
        ):
            return unavailable()
        try:
            return services.evidence_store.read(
                current.task.task_id,
                current_step["result_ref"],
                fields=fields,
                cursor=cursor,
                limit=limit,
            )
        except Exception:
            return unavailable()

    tool = FunctionTool(read_navigation_step_result_tool, is_read_only=True)
    tool.input_schema["additionalProperties"] = False
    return [tool]


def build_navigation_tool_groups(
    *,
    services: NavigationServices,
    snapshot: NavigationExecutionSnapshot,
    cancellation: CancellationContext | None,
    web_session_id: str,
    agentscope_session_id: str,
) -> dict[str, NavigationToolGroupDefinition]:
    task = snapshot.task
    observation_tools = build_navigation_observation_tools(
        task=task,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        settings=services.settings,
        expected_web_session_id=web_session_id,
        expected_agentscope_session_id=agentscope_session_id,
    )
    fixed_tools: list[ToolBase] = list(observation_tools)
    execution_tools: list[ToolBase] = []
    side_effect_state = _failed_step_side_effect_state(snapshot)

    if snapshot.activity == "planning" or (
        snapshot.activity == "failed_recovery"
        and side_effect_state == "not_started"
    ):
        fixed_tools.extend(
            build_navigation_task_tools(
                store=services.task_store,
                observation_store=services.observation_store,
                evidence_store=services.evidence_store,
                session_id=agentscope_session_id,
                web_session_id=web_session_id,
                settings=services.settings,
                bound_task=task,
            )
        )
        fixed_tools.extend(
            build_navigation_plan_submission_tools(
                task=task,
                observation_store=services.observation_store,
                evidence_store=services.evidence_store,
                plan_store=services.plan_store,
                capabilities=list_navigation_tool_capabilities(),
                expected_web_session_id=web_session_id,
                expected_agentscope_session_id=agentscope_session_id,
            )
        )
    if snapshot.active_plan is not None and snapshot.activity in {
        "execution",
        "failed_recovery",
        "recovery_required",
    }:
        fixed_tools.extend(
            _execution_state_tools(
                services=services,
                snapshot=snapshot,
                web_session_id=web_session_id,
                agentscope_session_id=agentscope_session_id,
            )
        )
    if snapshot.active_plan is not None and snapshot.activity == "execution":
        execution_tools = build_plan_bound_execution_tools(
            task=task,
            snapshot=snapshot,
            plan_store=services.plan_store,
            evidence_store=services.evidence_store,
            settings=services.settings,
            dry_run=task.dry_run,
            cancellation=cancellation,
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
        )
    if snapshot.activity == "failed_recovery":
        fixed_tools.extend(
            _failed_step_result_tools(
                services=services,
                snapshot=snapshot,
                web_session_id=web_session_id,
                agentscope_session_id=agentscope_session_id,
            )
        )

    trusted_fixed_tools = _trust(fixed_tools)
    classified = classify_fixed_navigation_tools(trusted_fixed_tools)
    actual_fixed_names = {tool.name for tool in trusted_fixed_tools}
    expected_fixed_names = _FIXED_TOOL_NAMES_BY_ACTIVITY[snapshot.activity]
    if (
        snapshot.activity == "failed_recovery"
        and side_effect_state == "not_started"
    ):
        expected_fixed_names = (
            expected_fixed_names
            | _FIXED_TOOL_NAMES_BY_ACTIVITY["planning"]
        )
    if actual_fixed_names != expected_fixed_names:
        missing = sorted(expected_fixed_names - actual_fixed_names)
        unexpected = sorted(actual_fixed_names - expected_fixed_names)
        raise ValueError(
            "unexpected fixed navigation tool set for "
            f"{snapshot.activity}: missing={missing}, unexpected={unexpected}"
        )

    groups: dict[str, NavigationToolGroupDefinition] = {}
    fixed_group_names = {
        "planning": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_INVESTIGATION,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_PLAN_AUTHORING,
        ),
        "execution": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_EXECUTION_STATE,
        ),
        "recovery_required": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_EXECUTION_STATE,
        ),
        "failed_recovery": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_EXECUTION_STATE,
            NAVIGATION_PLAN_AUTHORING,
        ),
    }[snapshot.activity]
    for group_name in fixed_group_names:
        group_tools = classified[group_name]
        if (
            snapshot.activity == "failed_recovery"
            and group_name == NAVIGATION_PLAN_AUTHORING
            and side_effect_state != "not_started"
        ):
            group_tools = ()
        groups[group_name] = NavigationToolGroupDefinition(
            name=group_name,
            description=_GROUP_DESCRIPTIONS[group_name],
            tools=group_tools,
        )

    if snapshot.activity in {"execution", "recovery_required"}:
        fixed_names = {tool.name for tool in fixed_tools}
        collisions = sorted(
            {tool.name for tool in execution_tools if tool.name in fixed_names}
        )
        if collisions:
            raise ValueError(
                "execution action collides with fixed navigation tool: "
                + ", ".join(collisions)
            )
        groups[NAVIGATION_EXECUTION_ACTIONS] = NavigationToolGroupDefinition(
            name=NAVIGATION_EXECUTION_ACTIONS,
            description=_GROUP_DESCRIPTIONS[NAVIGATION_EXECUTION_ACTIONS],
            tools=tuple(_trust(execution_tools)),
        )

    groups[NAVIGATION_DIAGNOSTICS] = NavigationToolGroupDefinition(
        name=NAVIGATION_DIAGNOSTICS,
        description=_GROUP_DESCRIPTIONS[NAVIGATION_DIAGNOSTICS],
        tools=classified[NAVIGATION_DIAGNOSTICS],
    )
    return groups


def resolve_navigation_tool_surface(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
) -> NavigationToolSurface | None:
    if web_session_id is None:
        return None
    snapshot = services.plan_store.read_execution_snapshot(
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    )
    if snapshot is None:
        return None
    groups = build_navigation_tool_groups(
        services=services,
        snapshot=snapshot,
        cancellation=cancellation,
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    )
    return NavigationToolSurfacePolicy.resolve(snapshot.activity, groups)


def resolve_navigation_agent_tools(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
) -> list[ToolBase]:
    surface = resolve_navigation_tool_surface(
        services=services,
        agentscope_session_id=agentscope_session_id,
        cancellation=cancellation,
        web_session_id=web_session_id,
    )
    return [] if surface is None else surface.flatten_active_tools()
