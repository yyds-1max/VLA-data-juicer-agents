from __future__ import annotations

import json
from typing import Any

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState, build_plan_from_draft
from vla_data_juicer_agents.navigation.plan_draft_store import NavigationPlanDraftStore
from vla_data_juicer_agents.navigation.plan_validation import validate_workflow_plan


def build_session_plan_draft_tools(
    *,
    store: NavigationPlanDraftStore,
    session_id: str,
) -> list[FunctionTool]:
    def get_workflow_plan_draft_tool(
        date: str | None = None,
        scene_mode: str | None = None,
    ) -> dict[str, Any]:
        """Create or read the session WorkflowPlan draft.

        The normal AgentScope web path pre-creates the draft from the
        structured handoff payload, so this tool usually reads the current
        session draft without arguments. date and scene_mode are a fallback for
        non-web callers that need to initialize an all-clips draft.
        """
        state = store.load(session_id)
        if state is None:
            state = _initial_state(
                date=date,
                scene_mode=scene_mode,
            )
            if state is None:
                return _missing_initial_request()
            store.save(session_id, state)
        elif _request_mismatch(
            state,
            date=date,
            scene_mode=scene_mode,
        ):
            return _request_mismatch_error(
                state,
                date=date,
                scene_mode=scene_mode,
            )
        return state.status()

    def update_workflow_plan_draft_tool(
        data_profile_patch: dict[str, Any],
        observation_id: str,
        used_tool: str,
    ) -> dict[str, Any]:
        """Merge only newly observed NavigationDataProfile facts.

        Use data_profile_patch for partial profile facts learned from exactly
        one inspection tool, plus observation_id and used_tool for traceability.
        Put processing_profile, platform_hint, topic_params, and stage_variants
        inside data_profile_patch rather than as separate tool arguments.
        data_profile_patch must be a non-empty JSON object, not a JSON string.
        Do not pass observed profile facts as separate tool arguments.
        """
        state = store.load(session_id)
        if state is None:
            return _missing_initial_request()
        if not isinstance(data_profile_patch, dict):
            return {
                "ok": False,
                "error_type": "invalid_data_profile_patch",
                "message": "data_profile_patch must be a JSON object, not a string or scalar.",
                "draft": state.schema_snapshot(),
            }
        if not data_profile_patch:
            return {
                "ok": False,
                "error_type": "empty_data_profile_patch",
                "message": (
                    "data_profile_patch must contain newly observed facts from "
                    "the completed read-only investigation tool."
                ),
                "draft": state.schema_snapshot(),
            }
        result = state.update(
            data_profile_patch=data_profile_patch,
            observation_id=observation_id,
            used_tool=used_tool,
        )
        store.save(session_id, state)
        return result

    def finalize_workflow_plan_tool() -> dict[str, Any]:
        """Finalize and return WorkflowPlan JSON only after all required profile facts and variants are complete."""
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
        validation = validate_workflow_plan(plan, data_profile=state.data_profile)
        if validation["errors"]:
            return {
                "ok": False,
                "error_type": "workflow_plan_validation_failed",
                "message": "WorkflowPlan validation failed before finalization.",
                "validation_errors": validation["errors"],
                "validation_warnings": validation.get("warnings", []),
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
) -> WorkflowPlanDraftState | None:
    if not date or scene_mode not in {"in", "out"}:
        return None
    return WorkflowPlanDraftState(
        request=NavigationRequest(
            date=date,
            scene_mode=scene_mode,
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
) -> bool:
    if date is not None and date != state.request.date:
        return True
    if scene_mode is not None and scene_mode != state.request.scene_mode:
        return True
    return False


def _request_mismatch_error(
    state: WorkflowPlanDraftState,
    *,
    date: str | None,
    scene_mode: str | None,
) -> dict[str, Any]:
    requested_request: dict[str, Any] = {}
    if date is not None:
        requested_request["date"] = date
    if scene_mode is not None:
        requested_request["scene_mode"] = scene_mode
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
