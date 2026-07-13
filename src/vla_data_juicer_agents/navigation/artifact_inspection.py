from __future__ import annotations

from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.task_state import NavigationArtifactSnapshot


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
    """Report current navigation artifact facts without choosing workflow state."""
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
        finish_temp_samples_exists=(
            finish_temp_samples.exists() and any(finish_temp_samples.iterdir())
        ),
        final_outputs_exist=final_outputs_exist,
        final_grid_map_exists=final_grid_map_exists,
        sync_image_samples=sample_images,
    )
