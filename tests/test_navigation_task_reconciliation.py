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
