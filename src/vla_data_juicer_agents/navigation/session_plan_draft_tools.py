from __future__ import annotations

import json
from typing import Any

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState, build_plan_from_draft
from vla_data_juicer_agents.navigation.plan_draft_store import NavigationPlanDraftStore


def build_session_plan_draft_tools(
    *,
    store: NavigationPlanDraftStore,
    session_id: str,
) -> list[FunctionTool]:
    def get_workflow_plan_draft_tool(
        date: str | None = None,
        scene_mode: str | None = None,
        segments: list[str] | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        state = store.load(session_id)
        if state is None:
            state = _initial_state(
                date=date,
                scene_mode=scene_mode,
                segments=segments,
                dry_run=dry_run,
            )
            if state is None:
                return _missing_initial_request()
            store.save(session_id, state)
        elif _request_mismatch(
            state,
            date=date,
            scene_mode=scene_mode,
            segments=segments,
            dry_run=dry_run,
        ):
            return _request_mismatch_error(
                state,
                date=date,
                scene_mode=scene_mode,
                segments=segments,
                dry_run=dry_run,
            )
        return state.status()

    def update_workflow_plan_draft_tool(
        dataset_profile: str | None = None,
        profile: str | None = None,
        processing_profile: str | dict[str, Any] | None = None,
        platform_hint: str | None = None,
        data_profile: dict[str, Any] | str | None = None,
        data_profile_patch: dict[str, Any] | str | None = None,
        observation_id: str | None = None,
        used_tool: str | None = None,
    ) -> dict[str, Any]:
        state = store.load(session_id)
        if state is None:
            return _missing_initial_request()
        result = state.update(
            dataset_profile=dataset_profile,
            profile=profile,
            processing_profile=processing_profile,
            platform_hint=platform_hint,
            data_profile=data_profile,
            data_profile_patch=data_profile_patch,
            observation_id=observation_id,
            used_tool=used_tool,
        )
        store.save(session_id, state)
        return result

    def finalize_workflow_plan_tool() -> dict[str, Any]:
        state = store.load(session_id)
        if state is None:
            return _missing_initial_request()
        try:
            plan = build_plan_from_draft(state)
        except ValueError as exc:
            return {
                "ok": False,
                "error_type": "workflow_plan_draft_incomplete",
                "message": str(exc),
                "missing_fields": state.missing_fields(),
                "next_tool_candidates": state.next_tool_candidates(),
                "draft": state.schema_snapshot(),
            }
        state.finalized_plan = plan
        store.save(session_id, state)
        return {
            "ok": True,
            "workflow_plan_json": json.loads(plan.model_dump_json()),
            "draft": state.schema_snapshot(),
        }

    return [
        FunctionTool(
            get_workflow_plan_draft_tool,
            name="get_workflow_plan_draft_tool",
            is_read_only=False,
        ),
        FunctionTool(
            update_workflow_plan_draft_tool,
            name="update_workflow_plan_draft_tool",
            is_read_only=False,
        ),
        FunctionTool(
            finalize_workflow_plan_tool,
            name="finalize_workflow_plan_tool",
            is_read_only=False,
        ),
    ]


def _initial_state(
    *,
    date: str | None,
    scene_mode: str | None,
    segments: list[str] | None,
    dry_run: bool | None,
) -> WorkflowPlanDraftState | None:
    if not date or scene_mode not in {"in", "out"}:
        return None
    return WorkflowPlanDraftState(
        request=NavigationRequest(
            date=date,
            scene_mode=scene_mode,
            segments=segments,
            dry_run=bool(dry_run),
        )
    )


def _missing_initial_request() -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "missing_initial_navigation_request",
        "message": (
            "No workflow plan draft exists for this AgentScope session. "
            "Call get_workflow_plan_draft_tool with date and scene_mode first."
        ),
        "missing_fields": ["date", "scene_mode"],
        "next_tool_candidates": ["get_workflow_plan_draft_tool"],
    }


def _request_mismatch(
    state: WorkflowPlanDraftState,
    *,
    date: str | None,
    scene_mode: str | None,
    segments: list[str] | None,
    dry_run: bool | None,
) -> bool:
    if date is not None and date != state.request.date:
        return True
    if scene_mode is not None and scene_mode != state.request.scene_mode:
        return True
    if segments is not None and segments != state.request.segments:
        return True
    return dry_run is not None and dry_run != state.request.dry_run


def _request_mismatch_error(
    state: WorkflowPlanDraftState,
    *,
    date: str | None,
    scene_mode: str | None,
    segments: list[str] | None,
    dry_run: bool | None,
) -> dict[str, Any]:
    requested_request: dict[str, Any] = {}
    if date is not None:
        requested_request["date"] = date
    if segments is not None:
        requested_request["segments"] = segments
    if scene_mode is not None:
        requested_request["scene_mode"] = scene_mode
    if dry_run is not None:
        requested_request["dry_run"] = dry_run
    return {
        "ok": False,
        "error_type": "workflow_plan_draft_request_mismatch",
        "message": (
            "A workflow plan draft already exists for this AgentScope session, "
            "but the requested navigation task does not match it. Start a new "
            "AgentScope navigation session or clear the draft before planning a "
            "different target."
        ),
        "existing_request": state.request.model_dump(mode="json"),
        "requested_request": requested_request,
        "draft": state.schema_snapshot(),
    }
