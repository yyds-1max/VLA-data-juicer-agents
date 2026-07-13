from __future__ import annotations

import json
from typing import Any

from vla_data_juicer_agents.navigation.artifact_inspection import (
    build_navigation_artifact_snapshot,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    EvidenceWrite,
    UserGuidanceObservation,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationArtifactSnapshot,
    NavigationTask,
    NavigationTaskDrift,
    NavigationTaskPhase,
    NavigationTaskStatus,
    utc_now,
)


def _has_partial_sync(snapshot: NavigationArtifactSnapshot) -> bool:
    values = list(snapshot.sync_data_by_segment.values())
    return bool(values) and any(values) and not all(values)


def _sync_drift(snapshot: NavigationArtifactSnapshot) -> NavigationTaskDrift:
    if _has_partial_sync(snapshot):
        missing = [
            f"clip_data/{snapshot.date}/{segment}/sync_data"
            for segment, exists in snapshot.sync_data_by_segment.items()
            if not exists
        ]
        return NavigationTaskDrift(
            type="partial_artifact",
            message=(
                "Only some selected segments have sync_data. Reconcile the task "
                "or rerun extract_sync for the missing segments before finish processing."
            ),
            evidence=missing or ["clip_data/<date>/<segment>/sync_data"],
        )
    return NavigationTaskDrift(
        type="missing_expected_artifact",
        message="Stored task expects sync_data, but sync_data is missing.",
        evidence=["clip_data/<date>/<segment>/sync_data"],
    )


def _navigation_scene_mode_for_request(scene_mode: str | None) -> str | None:
    return {
        "indoor": "in",
        "in": "in",
        "室内": "in",
        "outdoor": "out",
        "out": "out",
        "室外": "out",
    }.get(scene_mode or "")


def _structured_handoff_payload_from_message(message: str) -> dict[str, Any] | None:
    marker = "Structured handoff JSON:"
    if marker not in message:
        return None
    lines = message.split(marker, 1)[1].strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _task_changes(task: NavigationTask) -> dict[str, Any]:
    changes = task.model_dump(mode="json")
    changes.pop("task_id", None)
    return changes


def reconcile_and_save_navigation_task(
    task: NavigationTask,
    *,
    task_store: Any,
    settings: NavigationSettings | None = None,
) -> NavigationTask:
    reconciled = reconcile_navigation_task(task, settings=settings)
    return task_store.update_task(task.task_id, **_task_changes(reconciled))


def prepare_navigation_task_entry(
    *,
    task_store: Any,
    observation_store: Any,
    evidence_store: Any,
    message: str,
    web_session_id: str | None,
    agentscope_session_id: str,
    settings: NavigationSettings,
) -> NavigationTask:
    handoff = _structured_handoff_payload_from_message(message)
    if handoff is None or not isinstance(handoff.get("date"), str):
        raise ValueError("navigation task entry requires a structured handoff with date")
    segments = handoff.get("segments")
    if segments is not None and not isinstance(segments, list):
        raise ValueError("structured navigation handoff segments must be a list or null")
    guidance_value = handoff.get("request")
    guidance = guidance_value.strip() if isinstance(guidance_value, str) else ""

    normalized_segments = (
        [str(segment) for segment in segments] if segments is not None else None
    )
    previous_task = task_store.find_latest_by_date(
        handoff["date"],
        normalized_segments,
    )

    task = None
    saved = None
    try:
        task = task_store.create_or_update_task(
            date=handoff["date"],
            segments=normalized_segments,
            scene_mode=_navigation_scene_mode_for_request(handoff.get("scene_mode")),
            dry_run=bool(handoff.get("dry_run", False)),
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
        )
        reconciled = reconcile_navigation_task(task, settings=settings)
        changes = _task_changes(reconciled)
        for field in ("created_by_web_session_id", "latest_web_session_id", "agentscope_session_id"):
            changes.pop(field, None)
        if guidance:
            changes["guidance_revision"] = task.guidance_revision + 1
        saved = task_store.update_task_for_session(
            task.task_id, web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id, **changes,
        )
        if saved.artifact_snapshot is None:
            raise RuntimeError("task entry reconciliation did not create an artifact snapshot")

        artifact_observation = ArtifactStateObservation(snapshot=saved.artifact_snapshot)
        payloads: list[Any] = [artifact_observation]
        evidence_writes = [
            EvidenceWrite(
                kind="artifact_state",
                source_tool="task_entry_reconciliation",
                payload=saved.artifact_snapshot.model_dump(mode="json"),
                summary="task-entry artifact snapshot",
            )
        ]
        if guidance:
            guidance_observation = UserGuidanceObservation(
                guidance_revision=saved.guidance_revision,
                text=guidance,
            )
            payloads.append(guidance_observation)
            evidence_writes.append(
                EvidenceWrite(
                    kind="user_guidance",
                    source_tool="task_entry_reconciliation",
                    payload=guidance_observation.model_dump(mode="json"),
                    summary=f"task-entry user guidance revision {saved.guidance_revision}",
                )
            )
        observation_store.append(
            saved.task_id,
            "artifact_state",
            payloads,
            evidence_writes,
            evidence_store,
            expected_web_session_id=(
                web_session_id
                if getattr(observation_store, "db_path", None)
                == getattr(task_store, "db_path", object())
                else None
            ),
            expected_agentscope_session_id=(
                agentscope_session_id
                if getattr(observation_store, "db_path", None)
                == getattr(task_store, "db_path", object())
                else None
            ),
        )
        return task_store.get_task(saved.task_id) or saved
    except Exception as entry_error:
        try:
            if task is None:
                pass
            elif previous_task is None:
                current = saved or task
                if not task_store.delete_task_if_current(
                    task.task_id, expected_state_revision=current.state_revision,
                    expected_web_session_id=web_session_id,
                    expected_agentscope_session_id=agentscope_session_id,
                ):
                    entry_error.add_note("navigation task entry compensation skipped: task changed")
            else:
                current = saved or task
                if not task_store.restore_task_exact_if_current(
                    previous_task, expected_state_revision=current.state_revision,
                    expected_web_session_id=web_session_id,
                    expected_agentscope_session_id=agentscope_session_id,
                ):
                    entry_error.add_note("navigation task entry compensation skipped: task changed")
        except Exception as compensation_error:
            entry_error.add_note(
                "navigation task entry compensation failed: "
                f"{compensation_error!r}"
            )
        raise


def reconcile_navigation_task(
    task: NavigationTask,
    settings: NavigationSettings | None = None,
) -> NavigationTask:
    snapshot = build_navigation_artifact_snapshot(task.date, task.segments, settings=settings)
    payload = task.model_dump(mode="json")
    payload["artifact_snapshot"] = snapshot.model_dump(mode="json")
    payload["updated_at"] = utc_now()

    if snapshot.final_outputs_exist and snapshot.final_grid_map_exists:
        payload.update(
            {
                "phase": NavigationTaskPhase.COMPLETED.value,
                "status": NavigationTaskStatus.COMPLETED.value,
                "waiting_reason": None,
                "next_required_input": None,
                "drift": None,
            }
        )
        return NavigationTask.model_validate(payload)

    if not snapshot.raw_input_exists and not snapshot.raw_temp_exists:
        payload.update(
            {
                "phase": NavigationTaskPhase.INTAKE.value,
                "status": NavigationTaskStatus.NEEDS_RECONCILE.value,
                "waiting_reason": None,
                "next_required_input": None,
                "drift": NavigationTaskDrift(
                    type="missing_expected_artifact",
                    message="Raw navigation input is missing; task phase cannot advance.",
                    evidence=[
                        f"raw_data/{snapshot.date}",
                        f"raw_data/{snapshot.date}_temp",
                    ],
                ).model_dump(mode="json"),
            }
        )
        return NavigationTask.model_validate(payload)

    if snapshot.sync_data_exists:
        if task.scene_mode is None:
            payload.update(
                {
                    "phase": NavigationTaskPhase.WAITING_SCENE_MODE.value,
                    "status": NavigationTaskStatus.WAITING_USER.value,
                    "waiting_reason": "scene_mode_required_after_extract_sync",
                    "next_required_input": "scene_mode",
                    "drift": NavigationTaskDrift(
                        type="unexpected_existing_artifact",
                        message="Selected sync_data already exists and scene mode is required.",
                        evidence=snapshot.sync_image_samples
                        or ["clip_data/<date>/<segment>/sync_data"],
                    ).model_dump(mode="json"),
                }
            )
        else:
            payload.update(
                {
                    "phase": NavigationTaskPhase.FINISH_PROCESSING.value,
                    "status": NavigationTaskStatus.PENDING.value,
                    "waiting_reason": None,
                    "next_required_input": None,
                    "drift": None,
                }
            )
        return NavigationTask.model_validate(payload)

    if _has_partial_sync(snapshot):
        payload.update(
            {
                "phase": NavigationTaskPhase.EXTRACT_SYNC.value,
                "status": NavigationTaskStatus.NEEDS_RECONCILE.value,
                "waiting_reason": None,
                "next_required_input": None,
                "drift": _sync_drift(snapshot).model_dump(mode="json"),
            }
        )
        return NavigationTask.model_validate(payload)

    payload.update(
        {
            "phase": NavigationTaskPhase.EXTRACT_SYNC.value,
            "status": NavigationTaskStatus.NEEDS_RERUN.value,
            "waiting_reason": None,
            "next_required_input": None,
            "drift": _sync_drift(snapshot).model_dump(mode="json"),
        }
    )
    return NavigationTask.model_validate(payload)
