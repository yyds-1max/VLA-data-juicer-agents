from pathlib import Path

import pytest
import vla_data_juicer_agents.navigation.task_reconciliation as task_reconciliation_module
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.task_reconciliation import (
    build_navigation_artifact_snapshot,
    reconcile_navigation_task,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTask,
    NavigationTaskPhase,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


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


def test_snapshot_discovers_date_wide_sync_segments_after_raw_is_removed(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data").mkdir(
        parents=True
    )

    snapshot = build_navigation_artifact_snapshot(
        "20270623",
        None,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert snapshot.segments == ["segment_a"]
    assert snapshot.sync_data_exists is True
    assert snapshot.sync_data_by_segment == {"segment_a": True}


def test_empty_raw_date_root_does_not_hide_clip_segments_and_reports_missing_raw(
    tmp_path: Path,
):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data").mkdir(
        parents=True
    )

    reconciled = reconcile_navigation_task(
        NavigationTask(task_id="nav-1", date="20270623"),
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.artifact_snapshot.segments == ["segment_a"]
    assert reconciled.artifact_snapshot.raw_input_exists is False
    assert reconciled.artifact_snapshot.sync_data_exists is True
    assert reconciled.phase == NavigationTaskPhase.INTAKE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.evidence == [
        "raw_data/20270623",
        "raw_data/20270623_temp",
    ]


def test_missing_selected_raw_segment_cannot_be_hidden_by_other_raw_segment_or_sync(
    tmp_path: Path,
):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_b").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data").mkdir(
        parents=True
    )
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
    )

    reconciled = reconcile_navigation_task(
        task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.artifact_snapshot.raw_input_exists is False
    assert reconciled.artifact_snapshot.sync_data_exists is True
    assert reconciled.phase == NavigationTaskPhase.INTAKE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"


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

    assert reconciled.phase == NavigationTaskPhase.EXTRACT_SYNC
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "partial_artifact"
    assert reconciled.artifact_snapshot.sync_data_exists is False


def test_partial_selected_sync_reports_missing_raw_when_raw_was_removed(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data").mkdir(
        parents=True
    )
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a", "segment_b"],
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )

    reconciled = reconcile_navigation_task(
        task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.phase == NavigationTaskPhase.INTAKE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"


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

    assert reconciled.phase == NavigationTaskPhase.INTAKE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"


def test_reconcile_running_finish_processing_with_no_raw_marks_needs_reconcile(
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

    assert reconciled.phase == NavigationTaskPhase.INTAKE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"
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

    assert reconciled.phase == NavigationTaskPhase.INTAKE
    assert reconciled.status == NavigationTaskStatus.NEEDS_RECONCILE
    assert reconciled.drift is not None
    assert reconciled.drift.type == "missing_expected_artifact"
    assert reconciled.artifact_snapshot.final_outputs_exist is True
    assert reconciled.artifact_snapshot.final_grid_map_exists is False


def test_raw_only_entry_selects_extract_sync_from_completed_state(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a"],
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )

    reconciled = reconcile_navigation_task(
        task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.phase == NavigationTaskPhase.EXTRACT_SYNC
    assert reconciled.status == NavigationTaskStatus.NEEDS_RERUN


def test_existing_sync_selects_finish_processing_when_scene_known(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data").mkdir(
        parents=True
    )
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )

    reconciled = reconcile_navigation_task(
        task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.phase == NavigationTaskPhase.FINISH_PROCESSING
    assert reconciled.status == NavigationTaskStatus.PENDING


def test_valid_final_outputs_select_completed_from_intake(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "finish_data" / "20270623" / "segment_a" / "clip_a" / "grid_map").mkdir(
        parents=True
    )
    task = NavigationTask(task_id="nav-1", date="20270623")

    reconciled = reconcile_navigation_task(
        task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.phase == NavigationTaskPhase.COMPLETED
    assert reconciled.status == NavigationTaskStatus.COMPLETED


def test_other_segment_final_markers_do_not_complete_selected_segment(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "finish_data" / "20270623" / "segment_b" / "clip_b" / "grid_map").mkdir(
        parents=True
    )
    task = NavigationTask(
        task_id="nav-1",
        date="20270623",
        segments=["segment_a"],
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        status=NavigationTaskStatus.RUNNING,
    )

    reconciled = reconcile_navigation_task(
        task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert reconciled.artifact_snapshot.final_outputs_exist is False
    assert reconciled.artifact_snapshot.final_grid_map_exists is False
    assert reconciled.phase != NavigationTaskPhase.COMPLETED
    assert reconciled.status != NavigationTaskStatus.COMPLETED


def test_prepare_task_entry_persists_artifact_and_user_guidance_in_one_revision(
    tmp_path: Path,
):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    observation_store = SqliteNavigationObservationStore(tmp_path / "observations.sqlite")
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    message = "\n".join(
        [
            "Structured handoff JSON:",
            '{"date":"20270623","segments":["segment_a"],"scene_mode":null,'
            '"dry_run":true,"request":"Prefer measured timestamp facts."}',
        ]
    )

    task = task_reconciliation_module.prepare_navigation_task_entry(
        task_store=task_store,
        observation_store=observation_store,
        evidence_store=evidence_store,
        message=message,
        web_session_id="web-1",
        agentscope_session_id="as-1",
        settings=NavigationSettings(vladatasets_root=root),
    )
    revision = observation_store.latest(task.task_id)

    assert task.phase == NavigationTaskPhase.EXTRACT_SYNC
    assert task.status == NavigationTaskStatus.NEEDS_RERUN
    assert task.dry_run is True
    assert task.guidance_revision == 1
    assert task_store.find_latest_by_agentscope_session("as-1").task_id == task.task_id
    assert revision is not None
    assert revision.revision == 1
    assert {payload.kind for payload in revision.payloads} == {
        "artifact_state",
        "user_guidance",
    }
    assert len(revision.evidence_refs) == 2


class _FailingEvidenceStore:
    def write(self, *args, **kwargs):
        raise RuntimeError("evidence write failed")

    def delete(self, task_id: str, ref: str) -> None:
        raise AssertionError("no evidence descriptor should have been written")


def test_prepare_task_entry_restores_existing_task_when_evidence_append_fails(
    tmp_path: Path,
):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    observation_store = SqliteNavigationObservationStore(tmp_path / "observations.sqlite")
    original = task_store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode=None,
        web_session_id="web-old",
        agentscope_session_id="as-old",
    )
    original = task_store.update_task(
        original.task_id,
        guidance_revision=7,
        phase=NavigationTaskPhase.COMPLETED,
        status=NavigationTaskStatus.COMPLETED,
    )
    message = "\n".join(
        [
            "Structured handoff JSON:",
            '{"date":"20270623","segments":["segment_a"],"scene_mode":null,'
            '"dry_run":false,"request":"new guidance"}',
        ]
    )

    with pytest.raises(RuntimeError, match="evidence write failed"):
        task_reconciliation_module.prepare_navigation_task_entry(
            task_store=task_store,
            observation_store=observation_store,
            evidence_store=_FailingEvidenceStore(),
            message=message,
            web_session_id="web-new",
            agentscope_session_id="as-new",
            settings=NavigationSettings(vladatasets_root=root),
        )

    restored = task_store.get_task(original.task_id)
    assert restored is not None
    assert restored.guidance_revision == 7
    assert restored.phase == NavigationTaskPhase.COMPLETED
    assert restored.status == NavigationTaskStatus.COMPLETED
    assert restored.artifact_snapshot is None
    assert restored.latest_web_session_id == "web-old"
    assert restored.agentscope_session_id == "as-old"
    assert observation_store.latest(original.task_id) is None


def test_prepare_task_entry_removes_new_task_when_evidence_append_fails(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    observation_store = SqliteNavigationObservationStore(tmp_path / "observations.sqlite")
    message = "\n".join(
        [
            "Structured handoff JSON:",
            '{"date":"20270623","segments":["segment_a"],"scene_mode":null,'
            '"dry_run":false,"request":"new guidance"}',
        ]
    )

    with pytest.raises(RuntimeError, match="evidence write failed"):
        task_reconciliation_module.prepare_navigation_task_entry(
            task_store=task_store,
            observation_store=observation_store,
            evidence_store=_FailingEvidenceStore(),
            message=message,
            web_session_id="web-new",
            agentscope_session_id="as-new",
            settings=NavigationSettings(vladatasets_root=root),
        )

    assert task_store.find_latest_by_date("20270623", ["segment_a"]) is None
