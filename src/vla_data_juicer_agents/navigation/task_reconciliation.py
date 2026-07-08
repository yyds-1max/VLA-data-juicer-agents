from __future__ import annotations

from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
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
    selected = _selected_segments(raw_date if raw_date.exists() else raw_temp, segments)

    sync_roots = [clip_date / segment / "sync_data" for segment in selected]
    sync_exists = any(path.exists() for path in sync_roots)
    sample_images: list[str] = []
    for sync_root in sync_roots:
        if not sync_root.exists():
            continue
        for candidate in sorted(sync_root.glob("*/fisheye_front/*")):
            if candidate.is_file():
                sample_images.append(_relative_to_root(candidate, settings))
                break
        if sample_images:
            break

    final_grid_map_exists = any(final_root.glob("*/*/grid_map")) if final_root.exists() else False
    return NavigationArtifactSnapshot(
        date=date,
        segments=selected or segments,
        raw_input_exists=raw_date.exists(),
        raw_temp_exists=raw_temp.exists(),
        sync_data_exists=sync_exists,
        finish_temp_samples_exists=finish_temp_samples.exists() and any(finish_temp_samples.iterdir()),
        final_outputs_exist=final_root.exists(),
        final_grid_map_exists=final_grid_map_exists,
        sync_image_samples=sample_images,
    )


def reconcile_navigation_task(
    task: NavigationTask,
    settings: NavigationSettings | None = None,
) -> NavigationTask:
    snapshot = build_navigation_artifact_snapshot(task.date, task.segments, settings=settings)
    payload = task.model_dump(mode="json")
    payload["artifact_snapshot"] = snapshot.model_dump(mode="json")
    payload["updated_at"] = utc_now()

    if task.phase == NavigationTaskPhase.WAITING_SCENE_MODE and not snapshot.sync_data_exists:
        payload.update(
            {
                "phase": NavigationTaskPhase.EXTRACT_SYNC.value,
                "status": NavigationTaskStatus.NEEDS_RERUN.value,
                "waiting_reason": None,
                "next_required_input": None,
                "drift": NavigationTaskDrift(
                    type="missing_expected_artifact",
                    message="Stored task was waiting for scene mode, but sync_data is missing.",
                    evidence=["clip_data/<date>/<segment>/sync_data"],
                ).model_dump(mode="json"),
            }
        )
        return NavigationTask.model_validate(payload)

    if task.phase == NavigationTaskPhase.INTAKE and snapshot.sync_data_exists:
        payload.update(
            {
                "phase": NavigationTaskPhase.WAITING_SCENE_MODE.value,
                "status": NavigationTaskStatus.WAITING_USER.value,
                "waiting_reason": "scene_mode_required_after_extract_sync",
                "next_required_input": "scene_mode",
                "drift": NavigationTaskDrift(
                    type="unexpected_existing_artifact",
                    message="No completed task state was recorded, but sync_data already exists.",
                    evidence=snapshot.sync_image_samples
                    or ["clip_data/<date>/<segment>/sync_data"],
                ).model_dump(mode="json"),
            }
        )
        return NavigationTask.model_validate(payload)

    if (
        task.phase == NavigationTaskPhase.FINISH_PROCESSING
        and snapshot.final_outputs_exist
        and snapshot.final_grid_map_exists
    ):
        payload.update(
            {
                "phase": NavigationTaskPhase.COMPLETED.value,
                "status": NavigationTaskStatus.COMPLETED.value,
                "drift": None,
            }
        )
        return NavigationTask.model_validate(payload)

    payload["drift"] = task.drift.model_dump(mode="json") if task.drift else None
    return NavigationTask.model_validate(payload)
