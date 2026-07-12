from __future__ import annotations

from typing import Any

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.task_reconciliation import (
    reconcile_navigation_task,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTaskPhase,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskOwnershipError,
    NavigationTaskStateRevisionError,
    SqliteNavigationTaskStore,
    normalize_segments,
)


def _task_payload(task: Any) -> dict[str, Any]:
    return task.model_dump(mode="json")


def _task_anchor(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "phase": task.phase.value,
        "status": task.status.value,
    }


def build_navigation_task_tools(
    *,
    store: SqliteNavigationTaskStore,
    session_id: str,
    web_session_id: str | None,
    settings: NavigationSettings | None = None,
    draft_store: Any | None = None,
    bound_task: Any | None = None,
) -> list[FunctionTool]:
    settings = settings or NavigationSettings()

    def ownership_error(task_id: str) -> dict[str, Any] | None:
        live = store.get_task(task_id)
        valid = (
            live is not None
            and (bound_task is None or task_id == bound_task.task_id)
            and (
                bound_task is None
                or live.created_by_web_session_id
                == bound_task.created_by_web_session_id
            )
            and live.created_by_web_session_id == web_session_id
            and live.latest_web_session_id == web_session_id
            and live.agentscope_session_id == session_id
        )
        if valid:
            return None
        return {
            "ok": False,
            "error_type": "navigation_task_session_mismatch",
            "message": "The task is not bound to the current durable session.",
        }

    def save_task(task_id: str, **changes: Any) -> tuple[Any | None, dict[str, Any] | None]:
        try:
            current = store.get_task(task_id)
            if current is None:
                raise KeyError(task_id)
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
            return store.update_task_for_session(
                task_id,
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
                expected_state_revision=current.state_revision,
                **changes,
            ), None
        except (NavigationTaskOwnershipError, NavigationTaskStateRevisionError):
            return None, {
                "ok": False,
                "error_type": "navigation_task_session_mismatch",
                "message": "The task session changed before the update committed.",
            }

    def reconcile_and_save(task: Any) -> tuple[Any | None, dict[str, Any] | None]:
        reconciled = reconcile_navigation_task(task, settings=settings)
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
        return save_task(task.task_id, **changes)

    def get_or_create_navigation_task_tool(
        date: str,
        segments: list[str] | str | None = None,
        scene_mode: str | None = None,
    ) -> dict[str, Any]:
        """Create or load a durable navigation task for date/segments."""
        normalized_segments = _normalize_segments(segments)
        if bound_task is not None and (
            date != bound_task.date
            or normalized_segments != normalize_segments(bound_task.segments)
        ):
            return {
                "ok": False,
                "error_type": "navigation_task_session_mismatch",
                "message": "The request does not match the bound durable task.",
            }
        try:
            task = store.create_or_update_task(
                date=date,
                segments=normalized_segments,
                scene_mode=scene_mode,
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
            )
        except NavigationTaskOwnershipError:
            return {
                "ok": False,
                "error_type": "navigation_task_session_mismatch",
                "message": "The durable navigation task belongs to another Web session.",
            }
        saved, error = reconcile_and_save(task)
        if error is not None:
            return error
        return {"ok": True, "task": _task_anchor(saved)}

    def reconcile_navigation_task_tool(task_id: str) -> dict[str, Any]:
        """Reconcile persisted navigation task state with current filesystem artifacts."""
        if error := ownership_error(task_id):
            return error
        task = store.get_task(task_id)
        if task is None:
            return {
                "ok": False,
                "error_type": "navigation_task_not_found",
                "task_id": task_id,
            }
        saved, error = reconcile_and_save(task)
        if error is not None:
            return error
        return {"ok": True, "task": _task_anchor(saved)}

    def list_resumable_navigation_tasks_tool(
        date: str | None = None,
    ) -> dict[str, Any]:
        """List navigation tasks that can be resumed or require user input."""
        return {
            "ok": True,
            "tasks": [_task_payload(task) for task in store.list_resumable(date=date)],
        }

    def update_navigation_task_scene_mode_tool(
        task_id: str,
        scene_mode: str,
    ) -> dict[str, Any]:
        """Set scene mode and move a waiting task into finish-processing planning."""
        if error := ownership_error(task_id):
            return error
        if scene_mode not in {"in", "out"}:
            return {
                "ok": False,
                "error_type": "invalid_scene_mode",
                "message": "scene_mode must be in or out.",
            }
        existing = store.get_task(task_id)
        if existing is None:
            return {
                "ok": False,
                "error_type": "navigation_task_not_found",
                "task_id": task_id,
            }
        reconciled = reconcile_navigation_task(existing, settings=settings)
        if not reconciled.artifact_snapshot.sync_data_exists:
            changes = reconciled.model_dump(mode="json")
            changes.pop("task_id", None)
            saved, error = save_task(task_id, **changes)
            if error is not None:
                return error
            return {
                "ok": False,
                "error_type": "navigation_task_reconcile_required",
                "message": (
                    "Cannot continue finish_processing because selected sync_data "
                    "artifacts are incomplete. Use reconcile_navigation_task_tool "
                    "and rerun extract_sync for missing segments before setting scene_mode."
                ),
                "task": _task_payload(saved),
                "next_tool_candidates": [
                    "reconcile_navigation_task_tool",
                    "finalize_extract_sync_plan_tool",
                ],
            }
        task, error = save_task(
            task_id,
            scene_mode=scene_mode,
            phase=NavigationTaskPhase.FINISH_PROCESSING,
            status=NavigationTaskStatus.PENDING,
            waiting_reason=None,
            next_required_input=None,
            artifact_snapshot=(
                reconciled.artifact_snapshot.model_dump(mode="json")
                if reconciled.artifact_snapshot is not None
                else None
            ),
            drift=None,
        )
        if error is not None:
            return error
        return {"ok": True, "task": _task_payload(task)}

    def update_navigation_task_state_tool(
        task_id: str,
        phase: str,
        status: str,
        waiting_reason: str | None = None,
        next_required_input: str | None = None,
        last_completed_step: str | None = None,
    ) -> dict[str, Any]:
        """Update task phase/status after planning or execution progress."""
        if error := ownership_error(task_id):
            return error
        existing = store.get_task(task_id)
        if existing is None:
            return {
                "ok": False,
                "error_type": "navigation_task_not_found",
                "task_id": task_id,
            }
        requested_phase = NavigationTaskPhase(phase)
        requested_status = NavigationTaskStatus(status)
        reconciled, error = reconcile_and_save(existing)
        if error is not None:
            return error
        completion_request_is_valid = (
            requested_phase == NavigationTaskPhase.COMPLETED
            and requested_status == NavigationTaskStatus.COMPLETED
            and reconciled.phase == NavigationTaskPhase.COMPLETED
            and reconciled.status == NavigationTaskStatus.COMPLETED
        )
        requests_completion = (
            requested_phase == NavigationTaskPhase.COMPLETED
            or requested_status == NavigationTaskStatus.COMPLETED
        )
        if requested_phase != reconciled.phase or (
            requests_completion and not completion_request_is_valid
        ):
            return {
                "ok": False,
                "error_type": "navigation_task_reconcile_required",
                "message": (
                    "Requested task state is not supported by the live artifact "
                    "snapshot. Continue from the reconciled phase."
                ),
                "task": _task_payload(reconciled),
            }
        task, error = save_task(
            task_id,
            phase=requested_phase,
            status=requested_status,
            waiting_reason=waiting_reason,
            next_required_input=next_required_input,
            last_completed_step=last_completed_step,
        )
        if error is not None:
            return error
        return {"ok": True, "task": _task_payload(task)}

    def record_navigation_task_step_tool(
        task_id: str,
        phase: str,
        step_id: str,
        tool_name: str,
        status: str,
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        produced_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record one navigation execution step result in the durable task ledger."""
        if error := ownership_error(task_id):
            return error
        try:
            fields = {
                "task_id": task_id,
                "phase": NavigationTaskPhase(phase),
                "step_id": step_id,
                "tool_name": tool_name,
                "status": NavigationTaskStatus(status),
                "arguments": arguments,
                "result": result,
                "produced_paths": produced_paths,
            }
            step = (
                store.record_step_for_session(
                    web_session_id=web_session_id,
                    agentscope_session_id=session_id,
                    **fields,
                )
                if bound_task is not None
                else store.record_step(**fields)
            )
        except NavigationTaskOwnershipError:
            return {
                "ok": False,
                "error_type": "navigation_task_session_mismatch",
                "message": "The task session changed before the step was recorded.",
            }
        return {"ok": True, "step": step.model_dump(mode="json")}

    return [
        FunctionTool(
            get_or_create_navigation_task_tool,
            name="get_or_create_navigation_task_tool",
            is_read_only=False,
        ),
        FunctionTool(
            reconcile_navigation_task_tool,
            name="reconcile_navigation_task_tool",
            is_read_only=False,
        ),
        FunctionTool(
            list_resumable_navigation_tasks_tool,
            name="list_resumable_navigation_tasks_tool",
            is_read_only=True,
        ),
        FunctionTool(
            update_navigation_task_scene_mode_tool,
            name="update_navigation_task_scene_mode_tool",
            is_read_only=False,
        ),
        FunctionTool(
            update_navigation_task_state_tool,
            name="update_navigation_task_state_tool",
            is_read_only=False,
        ),
        FunctionTool(
            record_navigation_task_step_tool,
            name="record_navigation_task_step_tool",
            is_read_only=False,
        ),
    ]


def _normalize_segments(value: list[str] | str | None) -> list[str] | None:
    return normalize_segments(value)
