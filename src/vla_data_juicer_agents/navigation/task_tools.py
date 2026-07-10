from __future__ import annotations

from typing import Any

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.task_reconciliation import (
    reconcile_and_save_navigation_task,
    reconcile_navigation_task,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTaskPhase,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import (
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
) -> list[FunctionTool]:
    settings = settings or NavigationSettings()

    def get_or_create_navigation_task_tool(
        date: str,
        segments: list[str] | str | None = None,
        scene_mode: str | None = None,
    ) -> dict[str, Any]:
        """Create or load a durable navigation task for date/segments."""
        task = store.create_or_update_task(
            date=date,
            segments=_normalize_segments(segments),
            scene_mode=scene_mode,
            web_session_id=web_session_id,
            agentscope_session_id=session_id,
        )
        saved = reconcile_and_save_navigation_task(
            task,
            task_store=store,
            settings=settings,
        )
        return {"ok": True, "task": _task_anchor(saved)}

    def reconcile_navigation_task_tool(task_id: str) -> dict[str, Any]:
        """Reconcile persisted navigation task state with current filesystem artifacts."""
        task = store.get_task(task_id)
        if task is None:
            return {
                "ok": False,
                "error_type": "navigation_task_not_found",
                "task_id": task_id,
            }
        saved = reconcile_and_save_navigation_task(
            task,
            task_store=store,
            settings=settings,
        )
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
            saved = store.update_task(task_id, **changes)
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
        task = store.update_task(
            task_id,
            scene_mode=scene_mode,
            phase=NavigationTaskPhase.FINISH_PROCESSING,
            status=NavigationTaskStatus.PENDING,
            waiting_reason=None,
            next_required_input=None,
            latest_web_session_id=web_session_id,
            agentscope_session_id=session_id,
            artifact_snapshot=(
                reconciled.artifact_snapshot.model_dump(mode="json")
                if reconciled.artifact_snapshot is not None
                else None
            ),
            drift=None,
        )
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
        task = store.update_task(
            task_id,
            phase=NavigationTaskPhase(phase),
            status=NavigationTaskStatus(status),
            waiting_reason=waiting_reason,
            next_required_input=next_required_input,
            last_completed_step=last_completed_step,
        )
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
        step = store.record_step(
            task_id=task_id,
            phase=NavigationTaskPhase(phase),
            step_id=step_id,
            tool_name=tool_name,
            status=NavigationTaskStatus(status),
            arguments=arguments,
            result=result,
            produced_paths=produced_paths,
        )
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
