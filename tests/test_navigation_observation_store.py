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
from vla_data_juicer_agents.navigation.observation_store import SqliteNavigationObservationStore


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
