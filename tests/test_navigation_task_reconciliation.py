from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.task_reconciliation import (
    build_navigation_artifact_snapshot,
    reconcile_navigation_task,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTask,
    NavigationTaskPhase,
    NavigationTaskStatus,
)


def test_snapshot_detects_sync_data(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (
        root
        / "clip_data"
        / "20270623"
        / "segment_a"
        / "sync_data"
        / "clip_0"
        / "fisheye_front"
    ).mkdir(parents=True)
    (
        root
        / "clip_data"
        / "20270623"
        / "segment_a"
        / "sync_data"
        / "clip_0"
        / "fisheye_front"
        / "000001.jpg"
    ).write_bytes(b"jpg")
    settings = NavigationSettings(vladatasets_root=root)

    snapshot = build_navigation_artifact_snapshot("20270623", ["segment_a"], settings=settings)

    assert snapshot.raw_input_exists is True
    assert snapshot.sync_data_exists is True
    assert snapshot.sync_image_samples == [
        "clip_data/20270623/segment_a/sync_data/clip_0/fisheye_front/000001.jpg"
    ]
    assert snapshot.sync_data_by_segment == {"segment_a": True}


def test_snapshot_requires_sync_data_for_all_selected_segments(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "raw_data" / "20270623" / "segment_b").mkdir(parents=True)
    (
        root
        / "clip_data"
        / "20270623"
        / "segment_a"
        / "sync_data"
        / "clip_0"
        / "fisheye_front"
    ).mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    snapshot = build_navigation_artifact_snapshot(
        "20270623",
        ["segment_a", "segment_b"],
        settings=settings,
    )

    assert snapshot.sync_data_exists is False
    assert snapshot.sync_data_by_segment == {"segment_a": True, "segment_b": False}


def test_reconcile_waiting_scene_mode_missing_sync_marks_needs_rerun(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a"],
        phase=NavigationTaskPhase.WAITING_SCENE_MODE,
        status=NavigationTaskStatus.WAITING_USER,
    )

    reconciled = reconcile_navigation_task(task, settings=settings)

    assert reconciled.phase == NavigationTaskPhase.EXTRACT_SYNC
    assert reconciled.status == NavigationTaskStatus.NEEDS_RERUN
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"
    assert reconciled.artifact_snapshot.sync_data_exists is False


def test_reconcile_waiting_scene_mode_partial_sync_marks_needs_reconcile(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "raw_data" / "20270623" / "segment_b").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data" / "clip_0").mkdir(
        parents=True
    )
    settings = NavigationSettings(vladatasets_root=root)
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a", "segment_b"],
        phase=NavigationTaskPhase.WAITING_SCENE_MODE,
        status=NavigationTaskStatus.WAITING_USER,
    )

    reconciled = reconcile_navigation_task(task, settings=settings)

    assert reconciled.phase == NavigationTaskPhase.WAITING_SCENE_MODE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "partial_artifact"
    assert reconciled.artifact_snapshot.sync_data_exists is False


def test_reconcile_intake_with_existing_sync_recovers_waiting_scene_mode(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data" / "clip_0").mkdir(
        parents=True
    )
    settings = NavigationSettings(vladatasets_root=root)
    task = NavigationTask(task_id="nav-1", date="20270623", segments=["segment_a"])

    reconciled = reconcile_navigation_task(task, settings=settings)

    assert reconciled.phase == NavigationTaskPhase.WAITING_SCENE_MODE
    assert reconciled.status == NavigationTaskStatus.WAITING_USER
    assert reconciled.drift.type == "unexpected_existing_artifact"


def test_reconcile_completed_missing_final_outputs_marks_needs_reconcile(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    settings = NavigationSettings(vladatasets_root=root)
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )

    reconciled = reconcile_navigation_task(task, settings=settings)

    assert reconciled.phase == NavigationTaskPhase.COMPLETED
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"


def test_reconcile_running_finish_processing_keeps_partial_final_artifacts_non_blocking(
    tmp_path: Path,
):
    root = tmp_path / "VLADatasets"
    (root / "finish_data" / "20270623" / "segment_a" / "clip_a").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        status=NavigationTaskStatus.RUNNING,
    )

    reconciled = reconcile_navigation_task(task, settings=settings)

    assert reconciled.phase == NavigationTaskPhase.FINISH_PROCESSING
    assert reconciled.status == NavigationTaskStatus.RUNNING
    assert reconciled.drift is None
    assert reconciled.artifact_snapshot.final_outputs_exist is True
    assert reconciled.artifact_snapshot.final_grid_map_exists is False


def test_reconcile_completed_partial_final_artifacts_marks_needs_reconcile(
    tmp_path: Path,
):
    root = tmp_path / "VLADatasets"
    (root / "finish_data" / "20270623" / "segment_a" / "clip_a").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )

    reconciled = reconcile_navigation_task(task, settings=settings)

    assert reconciled.phase == NavigationTaskPhase.COMPLETED
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "partial_artifact"
    assert reconciled.artifact_snapshot.final_outputs_exist is True
    assert reconciled.artifact_snapshot.final_grid_map_exists is False
