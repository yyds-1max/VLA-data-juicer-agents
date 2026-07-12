from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def _relative_to_root(path: Path, settings: NavigationSettings) -> str:
    root = settings.vladatasets_root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(path)


def _selected_segments(root: Path, segments: list[str] | None) -> list[str]:
    if segments is not None:
        return segments
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def _has_segment_directories(root: Path) -> bool:
    return root.exists() and any(path.is_dir() for path in root.iterdir())


def _all_selected_segments_exist(root: Path, segments: list[str]) -> bool:
    return bool(segments) and all((root / segment).is_dir() for segment in segments)


def _all_selected_segments_have_grid_map(root: Path, segments: list[str]) -> bool:
    return bool(segments) and all(
        any(path.is_dir() for path in (root / segment).glob("*/grid_map"))
        for segment in segments
    )


def build_navigation_artifact_snapshot(
    date: str,
    segments: list[str] | None,
    settings: NavigationSettings | None = None,
) -> NavigationArtifactSnapshot:
    settings = settings or NavigationSettings()
    raw_date = settings.raw_data_root / date
    raw_temp = settings.raw_data_root / f"{date}_temp"
    clip_date = settings.clip_data_root / date
    finish_temp_samples = settings.finish_data_root / f"{date}_temp" / "samples" / date
    final_root = settings.finish_data_root / date
    selection_root = next(
        (
            root
            for root in (raw_date, raw_temp, clip_date, final_root)
            if _has_segment_directories(root)
        ),
        raw_date,
    )
    selected = _selected_segments(selection_root, segments)

    sync_roots = {segment: clip_date / segment / "sync_data" for segment in selected}
    sync_data_by_segment = {
        segment: sync_root.exists()
        for segment, sync_root in sync_roots.items()
    }
    sync_exists = bool(sync_data_by_segment) and all(sync_data_by_segment.values())
    sample_images: list[str] = []
    for sync_root in sync_roots.values():
        if not sync_root.exists():
            continue
        for candidate in sorted(sync_root.glob("*/fisheye_front/*")):
            if candidate.is_file():
                sample_images.append(_relative_to_root(candidate, settings))
                break
        if sample_images:
            break

    raw_input_exists = _all_selected_segments_exist(raw_date, selected)
    raw_temp_exists = _all_selected_segments_exist(raw_temp, selected)
    final_outputs_exist = _all_selected_segments_exist(final_root, selected)
    final_grid_map_exists = _all_selected_segments_have_grid_map(final_root, selected)
    return NavigationArtifactSnapshot(
        date=date,
        segments=selected or segments,
        raw_input_exists=raw_input_exists,
        raw_temp_exists=raw_temp_exists,
        sync_data_exists=sync_exists,
        sync_data_by_segment=sync_data_by_segment,
        finish_temp_samples_exists=finish_temp_samples.exists() and any(finish_temp_samples.iterdir()),
        final_outputs_exist=final_outputs_exist,
        final_grid_map_exists=final_grid_map_exists,
        sync_image_samples=sample_images,
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
        observation_fence = (
            {
                "expected_web_session_id": web_session_id,
                "expected_agentscope_session_id": agentscope_session_id,
            }
            if getattr(observation_store, "db_path", None)
            == getattr(task_store, "db_path", object())
            else {}
        )
        observation_store.append(
            saved.task_id,
            saved.phase,
            "artifact_state",
            payloads,
            evidence_writes,
            evidence_store,
            **observation_fence,
        )
        return saved
    except Exception as entry_error:
        try:
            if task is None:
                pass
            elif previous_task is None:
                current = saved or task
                if not task_store.delete_task_if_current(
                    task.task_id, expected_updated_at=current.updated_at,
                    expected_web_session_id=web_session_id,
                    expected_agentscope_session_id=agentscope_session_id,
                ):
                    entry_error.add_note("navigation task entry compensation skipped: task changed")
            else:
                current = saved or task
                if not task_store.restore_task_exact_if_current(
                    previous_task, expected_updated_at=current.updated_at,
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
