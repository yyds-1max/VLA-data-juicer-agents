"""AgentScope tools used by the navigation agents."""

from __future__ import annotations

import inspect
import json
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolBase

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
    list_navigation_tool_capabilities_tool,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.context_budget import (
    ensure_payload_within_limit,
    serialized_chars,
)
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
from vla_data_juicer_agents.navigation.observation_tools import (
    build_navigation_observation_tools,
)
from vla_data_juicer_agents.navigation.plan_execution import (
    build_plan_bound_execution_tools,
)
from vla_data_juicer_agents.navigation.plan_submission_tools import (
    build_navigation_plan_submission_tools,
)
from vla_data_juicer_agents.navigation.planning_context import (
    PHASE_REQUIRED_OBSERVATIONS,
)
from vla_data_juicer_agents.navigation.services import NavigationServices
from vla_data_juicer_agents.navigation.session_plan_draft_tools import (
    build_session_plan_draft_tools,
)
from vla_data_juicer_agents.navigation.task_reconciliation import reconcile_navigation_task
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskStateRevisionError,
    SqliteNavigationTaskStore,
)
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools


EXTRACT_SYNC_TOOLS = {"prepare_raw_data", "extract_and_sync_navigation_data"}
FINISH_PROCESSING_TOOLS = {
    "generate_gridmap_from_pcd",
    "assemble_finish_temp",
    "run_noobscene_preprocessing",
    "run_initial_annotation_gui",
    "run_tracking",
    "prepare_gridmap_for_projection",
    "run_projection_and_trajectory",
    "run_tracking_and_projection",
    "validate_navigation_outputs",
}


class HumanDecisionTool(ToolBase):
    """External tool that pauses navigation for a durable human decision."""

    name = "request_human_decision"
    description = (
        "Pause navigation workflow execution and ask the frontend to show a "
        "human decision dialog for calibration confirmation, overwrite/delete "
        "approval, stop, or user guidance."
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


class PlanBoundHumanDecisionTool(ToolBase):
    """External decision tool scoped to one stored plan ledger step."""

    name = "request_human_decision"
    description = (
        "Pause the current immutable navigation-plan step. The server derives "
        "dialog metadata from the stored plan; provide only plan_id and step_id."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "step_id": {"type": "string"},
        },
        "required": ["plan_id", "step_id"],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = True

    def __init__(self, *, gate: Any | None = None) -> None:
        self._gate = gate

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        if self._gate is not None:
            gate_error = self._gate(tool_input)
            if gate_error is not None:
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    message=gate_error["message"],
                )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Plan-bound human decision requests are allowed.",
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
        task_store: SqliteNavigationTaskStore | None = None,
        settings: NavigationSettings | None = None,
    ) -> None:
        self._tool = tool
        self._session_id = session_id
        self._draft_store = draft_store
        self._task_store = task_store
        self._settings = settings
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
        gate_error = _phase_plan_gate_error(
            session_id=self._session_id,
            draft_store=self._draft_store,
            tool_name=self.name,
            tool_input=tool_input,
            check_segments=_tool_accepts_segments(self),
            task_store=self._task_store,
            settings=self._settings,
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
        gate_error = _phase_plan_gate_error(
            session_id=self._session_id,
            draft_store=self._draft_store,
            tool_name=self.name,
            tool_input=kwargs,
            check_segments=_tool_accepts_segments(self),
            task_store=self._task_store,
            settings=self._settings,
        )
        if gate_error is not None:
            return gate_error
        result = self._tool(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


def _base_tool_name(tool_name: str) -> str:
    return tool_name[:-5] if tool_name.endswith("_tool") else tool_name


def _phase_plan_gate_error(
    *,
    session_id: str,
    draft_store: NavigationPlanDraftStore,
    tool_name: str,
    tool_input: dict[str, Any],
    check_segments: bool = True,
    task_store: SqliteNavigationTaskStore | None = None,
    settings: NavigationSettings | None = None,
) -> dict[str, Any] | None:
    state = draft_store.load(session_id)
    if state is None:
        return {
            "ok": False,
            "error_type": "navigation_plan_not_finalized",
            "message": "Navigation execution is blocked until a phase workflow plan has been finalized.",
            "missing_fields": ["workflow_plan_draft"],
            "next_tool_candidates": ["get_workflow_plan_draft_tool"],
        }
    if state.finalized_plan is None:
        return {
            "ok": False,
            "error_type": "navigation_plan_not_finalized",
            "message": "Navigation execution is blocked until a phase workflow plan has been finalized.",
            "missing_fields": state.missing_fields(),
            "next_tool_candidates": state.next_tool_candidates(),
            "draft": state.schema_snapshot(),
        }
    base_name = _base_tool_name(tool_name)
    plan_phase = state.finalized_plan.phase
    if base_name in EXTRACT_SYNC_TOOLS and plan_phase in {"extract_sync", "full"}:
        return _request_match_error_if_any(state, tool_input, check_segments)
    if base_name in FINISH_PROCESSING_TOOLS and plan_phase in {"finish_processing", "full"}:
        request_error = _request_match_error_if_any(state, tool_input, check_segments)
        if request_error is not None:
            return request_error
        return _finish_processing_task_gate_error(
            state=state,
            task_store=task_store,
            settings=settings,
        )
    return {
        "ok": False,
        "error_type": "navigation_phase_plan_required",
        "message": f"Tool {base_name} is not allowed by finalized navigation plan phase {plan_phase}.",
        "finalized_phase": plan_phase,
        "tool_name": base_name,
        "next_tool_candidates": [
            "finalize_extract_sync_plan_tool",
            "finalize_finish_processing_plan_tool",
            "finalize_workflow_plan_tool",
        ],
    }


def _request_match_error_if_any(
    state: Any,
    tool_input: dict[str, Any],
    check_segments: bool = True,
) -> dict[str, Any] | None:
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


def _finish_processing_task_gate_error(
    *,
    state: Any,
    task_store: SqliteNavigationTaskStore | None,
    settings: NavigationSettings | None = None,
) -> dict[str, Any] | None:
    if task_store is None:
        return None
    task = task_store.find_latest_by_date(state.request.date, state.request.segments)
    if task is None:
        return {
            "ok": False,
            "error_type": "navigation_task_reconcile_required",
            "message": (
                "Finish-processing is blocked until a durable navigation task "
                "exists and has been reconciled. Call get_or_create_navigation_task_tool "
                "then reconcile_navigation_task_tool."
            ),
            "next_tool_candidates": [
                "get_or_create_navigation_task_tool",
                "reconcile_navigation_task_tool",
            ],
        }
    live_task = reconcile_navigation_task(task, settings=settings)
    snapshot = live_task.artifact_snapshot
    sync_complete = bool(snapshot is not None and snapshot.sync_data_exists)
    if (
        live_task.phase != "finish_processing"
        or live_task.scene_mode not in {"in", "out"}
        or live_task.status in {"needs_reconcile", "needs_rerun", "failed"}
        or not sync_complete
    ):
        missing: list[str] = []
        if live_task.phase != "finish_processing":
            missing.append("task.phase=finish_processing")
        if live_task.scene_mode not in {"in", "out"}:
            missing.append("scene_mode")
        if snapshot is None:
            missing.append("artifact_snapshot")
        elif not snapshot.sync_data_exists:
            missing.append("complete sync_data for selected segments")
        if live_task.status in {"needs_reconcile", "needs_rerun", "failed"}:
            missing.append(f"task.status={live_task.status}")
        return {
            "ok": False,
            "error_type": "navigation_task_reconcile_required",
            "message": (
                "Finish-processing is blocked until the matching navigation task "
                "has scene_mode, a reconciled snapshot, and complete selected "
                "sync_data artifacts. Use reconcile_navigation_task_tool, rerun extract_sync "
                "if sync_data is missing, or set scene_mode before continuing."
            ),
            "missing_fields": missing,
            "task": live_task.model_dump(mode="json"),
            "next_tool_candidates": [
                "reconcile_navigation_task_tool",
                "update_navigation_task_scene_mode_tool",
                "finalize_extract_sync_plan_tool",
            ],
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
    task_store: SqliteNavigationTaskStore | None = None,
    settings: NavigationSettings | None = None,
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
            task_store=task_store,
            settings=settings,
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
            draft_store=draft_store,
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
            task_store=task_store,
            settings=settings,
        ),
    ])


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
    "get_phase_planning_context_tool",
    "list_observation_evidence_tool",
    "read_observation_evidence_tool",
    "describe_processing_action_tool",
}


def _compact_task_tools(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    web_session_id: str | None,
) -> list[ToolBase]:
    tools = build_navigation_task_tools(
        store=services.task_store,
        session_id=agentscope_session_id,
        web_session_id=web_session_id,
        settings=services.settings,
    )
    return [tool for tool in tools if tool.name == "get_or_create_navigation_task_tool"]


def _durable_state_tools(
    *,
    services: NavigationServices,
    task: Any,
) -> list[ToolBase]:
    def get_navigation_task_state_tool() -> dict[str, Any]:
        """Return the compact authoritative state of the bound navigation task."""
        current = services.task_store.get_task(task.task_id)
        if current is None:
            return {"ok": False, "error_type": "navigation_task_not_found"}
        return {
            "ok": True,
            "task_id": current.task_id,
            "phase": current.phase.value,
            "status": current.status.value,
        }

    def list_navigation_task_evidence_tool(
        cursor: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List compact evidence descriptors owned by the bound completed task."""
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        rows = services.observation_store.list_evidence(
            task.task_id,
            cursor=cursor,
            limit=limit + 1,
        )
        result: dict[str, Any] = {"evidence": [], "next_cursor": None}
        for row in rows[:limit]:
            evidence = [*result["evidence"], row.model_dump(mode="json")]
            candidate = {
                "evidence": evidence,
                "next_cursor": (
                    cursor + len(evidence) if len(rows) > len(evidence) else None
                ),
            }
            if serialized_chars(candidate) > 4_000:
                break
            result = candidate
        if rows and not result["evidence"]:
            raise ValueError("first evidence descriptor exceeds response character budget")
        return ensure_payload_within_limit(
            result,
            max_chars=4_000,
            label="completed_navigation_evidence_list",
        )

    def read_navigation_task_evidence_tool(
        ref: str,
        fields: list[str] | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read bounded evidence owned by the bound completed task."""
        result = services.evidence_store.read(
            task.task_id,
            ref,
            fields=fields,
            cursor=cursor,
            limit=limit,
            max_chars=4_000,
        )
        return ensure_payload_within_limit(
            result,
            max_chars=4_000,
            label="completed_navigation_evidence_read",
        )

    return [
        FunctionTool(get_navigation_task_state_tool, is_read_only=True),
        FunctionTool(list_navigation_task_evidence_tool, is_read_only=True),
        FunctionTool(read_navigation_task_evidence_tool, is_read_only=True),
    ]


def _execution_state_tools(
    *,
    services: NavigationServices,
    task: Any,
    plan: Any,
) -> list[ToolBase]:
    def get_plan_execution_overview_tool(plan_id: str) -> dict[str, Any]:
        """Read the compact execution overview for the bound active plan."""
        if plan_id != plan.plan_id:
            return {"ok": False, "error_type": "inactive_navigation_plan"}
        return ensure_payload_within_limit(
            services.plan_store.get_execution_overview(plan_id).model_dump(mode="json"),
            max_chars=4_000,
            label="resolved_execution_overview",
        )

    def get_current_plan_step_tool(plan_id: str) -> dict[str, Any] | None:
        """Read the current step and concise decision refs for the bound active plan."""
        if plan_id != plan.plan_id:
            return {"ok": False, "error_type": "inactive_navigation_plan"}
        current = services.plan_store.get_current_step(plan_id)
        if current is None:
            return None
        return ensure_payload_within_limit(
            current,
            max_chars=4_000,
            label="resolved_current_plan_step",
        )

    return [
        FunctionTool(get_plan_execution_overview_tool, is_read_only=True),
        FunctionTool(get_current_plan_step_tool, is_read_only=True),
    ]


def resolve_navigation_agent_tools(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
) -> list[ToolBase]:
    """Resolve the minimal AgentScope toolkit from authoritative durable state."""
    if web_session_id is not None and not (
        agentscope_session_id == web_session_id
        or agentscope_session_id.startswith(f"{web_session_id}__")
    ):
        return []
    task = services.task_store.find_latest_by_agentscope_session(agentscope_session_id)
    if task is None:
        return _trust_internal_navigation_tools(
            _compact_task_tools(
                services=services,
                agentscope_session_id=agentscope_session_id,
                web_session_id=web_session_id,
            )
        )

    reconciled = reconcile_navigation_task(task, settings=services.settings)
    changes = reconciled.model_dump(mode="json")
    for field in (
        "task_id",
        "created_by_web_session_id",
        "latest_web_session_id",
        "agentscope_session_id",
        "created_at",
        "updated_at",
        "state_revision",
    ):
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
        return _trust_internal_navigation_tools(
            _durable_state_tools(services=services, task=task)
        )

    if task.phase.value not in PHASE_REQUIRED_OBSERVATIONS:
        task_tools = build_navigation_task_tools(
            store=services.task_store,
            session_id=agentscope_session_id,
            web_session_id=web_session_id,
            settings=services.settings,
            bound_task=task,
        )
        allowed = {
            "get_or_create_navigation_task_tool",
            "reconcile_navigation_task_tool",
            "update_navigation_task_scene_mode_tool",
        }
        return _trust_internal_navigation_tools(
            [tool for tool in task_tools if tool.name in allowed]
        )

    observation = services.observation_store.latest(task.task_id)
    completed = (
        set(observation.completed_kinds)
        if observation is not None and observation.phase.value == task.phase.value
        else set()
    )
    required = PHASE_REQUIRED_OBSERVATIONS[task.phase.value]
    missing = [kind for kind in required if kind not in completed]
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
        execution_tools = build_plan_bound_execution_tools(
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
        current_step = (current or {}).get("step") or {}
        if current_step.get("action") == "confirm_navigation_calibration_params":
            handoff = services.plan_store.get_human_decision_handoff(
                active_plan.plan_id,
                current_step.get("step_id", ""),
            )
            if handoff is not None and handoff.status == "recovery_required":
                execution_tools = [
                    tool
                    for tool in execution_tools
                    if tool.name != "request_human_decision"
                ]
        tools = [
            *_execution_state_tools(
                services=services,
                task=task,
                plan=active_plan,
            ),
            *execution_tools,
        ]
        return _trust_internal_navigation_tools(tools)

    if missing:
        missing_names = {_INSPECTION_TOOL_BY_KIND[kind] for kind in missing}
        inspections = [tool for tool in observation_tools if tool.name in missing_names]
        return _trust_internal_navigation_tools([*inspections, *cognitive])

    submission = build_navigation_plan_submission_tools(
        task=task,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        plan_store=services.plan_store,
        capabilities=list_navigation_tool_capabilities(),
        expected_web_session_id=web_session_id,
        expected_agentscope_session_id=agentscope_session_id,
    )
    return _trust_internal_navigation_tools([*cognitive, *submission])
