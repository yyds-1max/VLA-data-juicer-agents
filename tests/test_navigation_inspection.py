from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.inspection import (
    classify_navigation_dataset,
    classify_navigation_dataset_tool,
    inspect_raw_date,
    list_navigation_dates,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"


def test_list_navigation_dates_finds_raw_dates():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    dates = list_navigation_dates("raw_data", settings=settings)

    assert dates == ["20270515", "20270605"]


def test_inspect_raw_date_reads_topics():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = inspect_raw_date("20270605", settings=settings)

    assert result.exists is True
    assert result.segments[0].name == "20260605_152856"
    topic_names = {topic.name for topic in result.segments[0].topics}
    assert "/cam_video4/csi_cam/image_raw/compressed" in topic_names
    assert "/sport_odom" in topic_names


def test_inspect_raw_date_records_missing_metadata_root_error(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270605" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text("not_rosbag2_bagfile_information: {}\n", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = inspect_raw_date("20270605", settings=settings)

    assert result.segments[0].errors


def test_inspect_raw_date_records_topic_entry_missing_name_error(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270605" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        """
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        type: sensor_msgs/msg/CompressedImage
      message_count: 1
""",
        encoding="utf-8",
    )
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = inspect_raw_date("20270605", settings=settings)

    assert result.segments[0].errors


def test_classify_navigation_dataset_rejects_missing_requested_segment():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    with pytest.raises(FileNotFoundError, match="missing"):
        classify_navigation_dataset("20270605", ["missing"], settings=settings)


def test_classify_navigation_dataset_tool_schema_allows_omitting_segments():
    required = classify_navigation_dataset_tool.input_schema.get("required", [])

    assert "segments" not in required
