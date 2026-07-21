from typing import Any, Literal

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.evidence_store import (
    FileNavigationEvidenceStore,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_store import (
    SqliteNavigationPlanRepository,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTask,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskStateRevisionError,
    SqliteNavigationTaskStore,
)


def build_navigation_task_tools(
    *,
    store: SqliteNavigationTaskStore,
    session_id: str,
    web_session_id: str | None,
    observation_store: SqliteNavigationObservationStore | None = None,
    evidence_store: FileNavigationEvidenceStore | None = None,
    plan_store: SqliteNavigationPlanRepository | None = None,
    bound_task: NavigationTask | None = None,
    settings: Any | None = None,
) -> list[FunctionTool]:
    """Build bounded model-facing mutations for an already-bound attempt."""
    del settings
    if bound_task is None:
        return []
    if observation_store is None or evidence_store is None or plan_store is None:
        raise ValueError("bound guidance requires observation and evidence stores")

    def record_navigation_user_guidance_tool(
        text: str,
        scene_mode: Literal["in", "out"] | None = None,
    ) -> dict[str, Any]:
        """Record user guidance without inspecting artifacts or selecting a stage."""
        guidance = text.strip()
        if not guidance:
            return {
                "ok": False,
                "error_type": "invalid_navigation_user_guidance",
                "message": "Guidance text must not be empty.",
            }
        if len(guidance) > 4_000:
            return {
                "ok": False,
                "error_type": "invalid_navigation_user_guidance",
                "message": "Guidance text exceeds 4000 characters.",
            }
        current = store.get_task(bound_task.task_id)
        if current is None:
            return {
                "ok": False,
                "error_type": "navigation_task_not_found",
                "message": "The bound navigation task no longer exists.",
            }
        try:
            guidance_revision, observation = observation_store.append_user_guidance(
                current.task_id,
                text=guidance,
                scene_mode=scene_mode,
                expected_state_revision=current.state_revision,
                evidence_store=evidence_store,
                expected_web_session_id=web_session_id,
                expected_agentscope_session_id=session_id,
            )
        except (PermissionError, NavigationTaskStateRevisionError):
            return {
                "ok": False,
                "error_type": "navigation_task_session_mismatch",
                "message": "The task session changed before guidance was recorded.",
            }
        except Exception:
            return {
                "ok": False,
                "error_type": "navigation_guidance_persistence_failed",
                "message": "Guidance could not be recorded; task state was not advanced.",
            }
        return {
            "ok": True,
            "guidance_revision": guidance_revision,
            "observation_revision": observation.revision,
        }

    tool = FunctionTool(
        record_navigation_user_guidance_tool,
        name="record_navigation_user_guidance_tool",
        is_read_only=False,
    )
    tool.input_schema["additionalProperties"] = False

    def complete_navigation_task_tool() -> dict[str, Any]:
        """Close after verified extract/sync when the user declines later processing."""
        current = store.get_task(bound_task.task_id)
        if current is None:
            return {
                "ok": False,
                "error_type": "navigation_task_not_found",
                "message": "The bound navigation task no longer exists.",
            }
        if current.accepted_plan_phase != "extract_sync":
            return {
                "ok": False,
                "error_type": "navigation_task_not_ready_to_complete",
                "message": (
                    "The task may close here only after the accepted extract/sync "
                    "stage has completed."
                ),
            }
        accepted_plan = plan_store.get_latest_accepted_for_task(current.task_id)
        if accepted_plan is None or accepted_plan.status != "completed":
            return {
                "ok": False,
                "error_type": "navigation_task_not_ready_to_complete",
                "message": (
                    "The accepted extract/sync Plan must be durably completed before "
                    "the task can close here."
                ),
            }
        if current.status not in {
            NavigationTaskStatus.ACTIVE,
            NavigationTaskStatus.WAITING_USER,
        }:
            return {
                "ok": False,
                "error_type": "navigation_task_not_ready_to_complete",
                "message": "The task cannot be completed from its current state.",
            }
        try:
            completed = store.update_task_for_session(
                current.task_id,
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
                expected_state_revision=current.state_revision,
                status=NavigationTaskStatus.COMPLETED,
            )
        except (PermissionError, NavigationTaskStateRevisionError):
            return {
                "ok": False,
                "error_type": "navigation_task_session_mismatch",
                "message": "The task session changed before it could be completed.",
            }
        return {
            "ok": True,
            "status": completed.status.value,
            "completed_stage": "extract_sync",
            "retained_products": True,
        }

    completion_tool = FunctionTool(
        complete_navigation_task_tool,
        name="complete_navigation_task_tool",
        is_read_only=False,
    )
    completion_tool.input_schema["required"] = []
    completion_tool.input_schema["additionalProperties"] = False
    return [tool, completion_tool]
