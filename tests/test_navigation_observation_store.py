import sqlite3

import pytest

from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    EvidenceWrite,
    RawMetadataObservation,
    SensorCandidatesObservation,
    SensorRoleCandidate,
    TopicMeasurement,
)
from vla_data_juicer_agents.navigation.observation_store import (
    ObservationRollbackCleanupError,
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


def _raw_observation() -> RawMetadataObservation:
    return RawMetadataObservation(
        segments=["20260710_120000"],
        topics=[TopicMeasurement(topic="/lidar/points", message_count=20)],
    )


def _sensor_observation() -> SensorCandidatesObservation:
    return SensorCandidatesObservation(
        candidates=[SensorRoleCandidate(role="lidar", topic="/lidar/points", confidence=0.9)]
    )


def test_observation_revision_is_monotonic_and_carries_forward_facts(tmp_path):
    store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    evidence = FileNavigationEvidenceStore(tmp_path / "evidence")

    first = store.append("nav-1", "extract_sync", "raw_metadata", [_raw_observation()], [], evidence)
    second = store.append(
        "nav-1", "extract_sync", "sensor_candidates", [_sensor_observation()], [], evidence
    )

    assert (first.revision, second.revision) == (1, 2)
    assert second.completed_kinds == ["raw_metadata", "sensor_candidates"]
    assert [payload.kind for payload in second.payloads] == ["raw_metadata", "sensor_candidates"]
    assert store.latest("nav-1") == second
    assert store.get("nav-1", 1) == first
    assert store.latest("nav-missing") is None


def test_observation_revisions_are_allocated_per_task(tmp_path):
    store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    evidence = FileNavigationEvidenceStore(tmp_path / "evidence")

    nav_one = store.append("nav-1", "extract_sync", "raw_metadata", [_raw_observation()], [], evidence)
    nav_two = store.append("nav-2", "extract_sync", "raw_metadata", [_raw_observation()], [], evidence)

    assert nav_one.revision == nav_two.revision == 1


def test_append_persists_evidence_metadata_and_filters_by_task_kind_and_revision(tmp_path):
    store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    evidence = FileNavigationEvidenceStore(tmp_path / "evidence")
    first_write = EvidenceWrite(
        kind="raw_metadata",
        source_tool="inspect_raw_date_tool",
        payload={"rows": [1, 2, 3]},
        summary="three raw rows",
    )
    second_write = EvidenceWrite(
        kind="sensor_candidates",
        source_tool="inspect_navigation_sensor_candidates_tool",
        payload={"candidates": ["/lidar/points"]},
        summary="one candidate",
    )

    first = store.append(
        "nav-1", "extract_sync", "raw_metadata", [_raw_observation()], [first_write], evidence
    )
    second = store.append(
        "nav-1",
        "extract_sync",
        "sensor_candidates",
        [_sensor_observation()],
        [second_write],
        evidence,
    )

    all_metadata = store.list_evidence("nav-1")
    sensor_metadata = store.list_evidence("nav-1", kind="sensor_candidates")
    revision_one_metadata = store.list_evidence("nav-1", observation_revision=1)

    assert [item.ref for item in all_metadata] == [*first.evidence_refs, *second.evidence_refs]
    assert [item.ref for item in sensor_metadata] == second.evidence_refs
    assert [item.ref for item in revision_one_metadata] == first.evidence_refs
    assert store.list_evidence("nav-2") == []
    assert evidence.read("nav-1", first.evidence_refs[0])["data"] == {"rows": [1, 2, 3]}


def test_observation_and_evidence_commits_advance_task_aggregate_revision(tmp_path):
    db_path = tmp_path / "state.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20260710", segments=["20260710_120000"], scene_mode=None,
    )
    store = SqliteNavigationObservationStore(db_path)
    evidence = FileNavigationEvidenceStore(tmp_path / "evidence")

    store.append(
        task.task_id,
        "extract_sync",
        "raw_metadata",
        [_raw_observation()],
        [EvidenceWrite(
            kind="raw_metadata",
            source_tool="inspect_raw_date_tool",
            payload={"rows": [1]},
            summary="one raw row",
        )],
        evidence,
    )

    current = task_store.get_task(task.task_id)
    assert current.state_revision == task.state_revision + 2


def test_task_initializer_installs_triggers_for_preexisting_observation_tables(tmp_path):
    db_path = tmp_path / "state.sqlite"
    store = SqliteNavigationObservationStore(db_path)
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20260710", segments=["20260710_120000"], scene_mode=None,
    )

    store.append(
        task.task_id,
        "extract_sync",
        "raw_metadata",
        [_raw_observation()],
        [],
        FileNavigationEvidenceStore(tmp_path / "evidence"),
    )

    assert task_store.get_task(task.task_id).state_revision == task.state_revision + 1


def test_owned_task_observation_append_rejects_omitted_session(tmp_path):
    db_path = tmp_path / "state.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20260710",
        segments=["20260710_120000"],
        scene_mode=None,
        web_session_id="web-owner",
        agentscope_session_id="as-owner",
    )
    store = SqliteNavigationObservationStore(db_path)

    with pytest.raises(PermissionError, match="session mismatch"):
        store.append(
            task.task_id,
            "extract_sync",
            "raw_metadata",
            [_raw_observation()],
            [],
            FileNavigationEvidenceStore(tmp_path / "evidence"),
        )

    assert store.latest(task.task_id) is None


def test_append_rolls_back_database_and_written_evidence_on_failure(tmp_path):
    root = tmp_path / "evidence"
    underlying = FileNavigationEvidenceStore(root)

    class FailingEvidenceStore:
        def __init__(self):
            self.write_count = 0

        def write(self, *args, **kwargs):
            self.write_count += 1
            if self.write_count == 2:
                raise RuntimeError("write failed")
            return underlying.write(*args, **kwargs)

        def delete(self, task_id, ref):
            underlying.delete(task_id, ref)

    store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    writes = [
        EvidenceWrite(kind="one", source_tool="inspect", payload={"one": 1}, summary="one"),
        EvidenceWrite(kind="two", source_tool="inspect", payload={"two": 2}, summary="two"),
    ]

    with pytest.raises(RuntimeError, match="write failed"):
        store.append("nav-1", "extract_sync", "raw_metadata", [_raw_observation()], writes, FailingEvidenceStore())

    assert store.latest("nav-1") is None
    assert list(root.rglob("*.json")) == []
    with sqlite3.connect(tmp_path / "state.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM navigation_evidence").fetchone()[0] == 0


def test_append_surfaces_all_evidence_cleanup_failures_after_database_rollback(tmp_path):
    underlying = FileNavigationEvidenceStore(tmp_path / "evidence")

    class CleanupFailingEvidenceStore:
        def __init__(self):
            self.write_count = 0
            self.delete_attempts = []

        def write(self, *args, **kwargs):
            self.write_count += 1
            if self.write_count == 3:
                raise RuntimeError("third write failed")
            return underlying.write(*args, **kwargs)

        def delete(self, task_id, ref):
            self.delete_attempts.append((task_id, ref))
            raise OSError(f"cannot delete {ref}")

    evidence = CleanupFailingEvidenceStore()
    store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    writes = [
        EvidenceWrite(
            kind=str(index),
            source_tool="inspect",
            payload={"index": index},
            summary=str(index),
        )
        for index in range(3)
    ]

    with pytest.raises(ObservationRollbackCleanupError) as captured:
        store.append(
            "nav-1",
            "extract_sync",
            "raw_metadata",
            [_raw_observation()],
            writes,
            evidence,
        )

    error = captured.value
    assert isinstance(error.original_error, RuntimeError)
    assert str(error.original_error) == "third write failed"
    assert len(error.cleanup_errors) == 2
    assert all(isinstance(cleanup_error, OSError) for cleanup_error in error.cleanup_errors)
    assert error.__cause__ is error.original_error
    assert len(evidence.delete_attempts) == 2
    assert store.latest("nav-1") is None
    with sqlite3.connect(tmp_path / "state.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM navigation_observation_revisions"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM navigation_evidence").fetchone()[0] == 0
