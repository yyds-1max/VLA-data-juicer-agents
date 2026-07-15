from pathlib import Path

from vla_data_juicer_agents.navigation.artifact_inspection import (
    build_navigation_artifact_snapshot,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings


def test_artifact_inspection_reports_facts_without_workflow_decisions(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data").mkdir(
        parents=True
    )

    snapshot = build_navigation_artifact_snapshot(
        "20270623",
        ["segment_a"],
        settings=NavigationSettings(vladatasets_root=root),
    )
    payload = snapshot.model_dump(mode="json")

    assert payload["raw_input_exists"] is True
    assert payload["sync_data_exists"] is False
    assert payload["sync_data_by_segment"] == {"segment_a": False}
    assert not ({"phase", "status", "next_tool", "recommended"} & set(payload))


def test_artifact_inspection_requires_nonempty_camera_and_lidar_sync_outputs(tmp_path: Path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    sequence = (
        root
        / "clip_data"
        / "20270623"
        / "segment_a"
        / "sync_data"
        / "sequence_a"
    )
    for dirname in ("fisheye_front", "r32_rslidar_points"):
        sensor_dir = sequence / dirname
        sensor_dir.mkdir(parents=True)
        (sensor_dir / "1000.000000.data").write_text("data", encoding="utf-8")

    snapshot = build_navigation_artifact_snapshot(
        "20270623",
        ["segment_a"],
        settings=NavigationSettings(vladatasets_root=root),
    )

    assert snapshot.sync_data_exists is True
    assert snapshot.sync_data_by_segment == {"segment_a": True}
