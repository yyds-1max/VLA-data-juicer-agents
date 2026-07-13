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
    assert payload["sync_data_exists"] is True
    assert payload["sync_data_by_segment"] == {"segment_a": True}
    assert not ({"phase", "status", "next_tool", "recommended"} & set(payload))
