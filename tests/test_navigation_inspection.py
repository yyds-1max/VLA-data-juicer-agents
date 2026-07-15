from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.inspection import (
    inspect_gridmap_artifacts,
    inspect_navigation_sensor_candidates,
    inspect_navigation_topic_candidates,
    inspect_processing_state,
    inspect_raw_date,
    inspect_runtime_assets,
    list_navigation_dates,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"


def test_sensor_candidate_inspection_does_not_select_binding():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = inspect_navigation_sensor_candidates("20270605", settings=settings)

    assert result.candidates
    assert {candidate.role for candidate in result.candidates} >= {
        "fisheye_front",
        "lidar",
        "odom",
        "localization",
    }
    assert not hasattr(result, "sensor_bindings")


def test_topic_candidate_inspection_does_not_select_final_params():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = inspect_navigation_topic_candidates("20270605", settings=settings)

    payload = result.model_dump(mode="json")
    assert "/rs32_lidar_points" in result.available_topics
    assert result.suggested_role_names["lidar"] == ["/rs32_lidar_points"]
    lidar_route = next(
        route
        for route in result.routes
        if route.role == "lidar" and route.topic == "/rs32_lidar_points"
    )
    assert lidar_route.extracted_dir == "rs32_lidar_points"
    assert lidar_route.output_dir == "r32_rslidar_points"
    assert lidar_route.sync_reference_eligible is True
    assert "topic_whitelist" not in payload
    assert "topic_map" not in payload
    assert "query_dir" not in payload


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


def test_inspect_processing_state_summarizes_existing_intermediate_outputs(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270605" / "segment_a" / "sync_data").mkdir(parents=True)
    (root / "finish_data" / "20270605_temp" / "samples" / "20270605" / "clip_a").mkdir(parents=True)
    (root / "finish_data" / "20270605" / "segment_a" / "clip_a" / "grid_map").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    result = inspect_processing_state("20270605", ["segment_a"], settings=settings)

    assert result == {
        "date": "20270605",
        "segments": ["segment_a"],
        "has_raw_temp": True,
        "has_clip_sync_data": True,
        "has_finish_temp_samples": True,
        "has_final_outputs": True,
        "has_final_grid_map": True,
    }


def test_inspect_gridmap_artifacts_reports_projection_ready_before_generation(tmp_path):
    root = tmp_path / "VLADatasets"
    gridmap_dir = root / "finish_data" / "20270605_temp" / "samples" / "20270605" / "clip_a" / "grid_map"
    gridmap_dir.mkdir(parents=True)
    (gridmap_dir / "grid_map.json").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root)

    result = inspect_gridmap_artifacts("20270605", ["segment_a"], settings=settings)

    assert result["gridmap_source"] == "projection_ready"
    assert result["projection_input_ready"] is True
    assert result["available_gridmap_paths"]


def test_inspect_gridmap_artifacts_reports_existing_clip_gridmap(tmp_path):
    root = tmp_path / "VLADatasets"
    gridmap_dir = root / "clip_data" / "20270605" / "segment_a" / "sync_data" / "clip_a" / "grid_map"
    gridmap_dir.mkdir(parents=True)
    (gridmap_dir / "grid_map.json").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root)

    result = inspect_gridmap_artifacts("20270605", ["segment_a"], settings=settings)

    assert result["gridmap_source"] == "existing_gridmap"
    assert result["projection_input_ready"] is False
    assert str(gridmap_dir) in result["available_gridmap_paths"]


def test_inspect_gridmap_artifacts_ignores_empty_gridmap_dirs(tmp_path):
    root = tmp_path / "VLADatasets"
    empty_gridmap_dir = root / "clip_data" / "20270605" / "segment_a" / "sync_data" / "clip_a" / "grid_map"
    empty_gridmap_dir.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    result = inspect_gridmap_artifacts("20270605", ["segment_a"], settings=settings)

    assert result["gridmap_source"] == "unknown"
    assert result["available_gridmap_paths"] == []


def test_inspect_gridmap_artifacts_reports_pcd_sources(tmp_path):
    root = tmp_path / "VLADatasets"
    pcd_path = (
        root
        / "clip_data"
        / "20270605"
        / "segment_a"
        / "sync_data"
        / "clip_a"
        / "r32_rslidar_points"
        / "000001.pcd"
    )
    pcd_path.parent.mkdir(parents=True)
    pcd_path.write_text("pcd", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root)

    result = inspect_gridmap_artifacts("20270605", ["segment_a"], settings=settings)

    assert result["pcd_sources"] == [str(pcd_path)]


def test_inspect_runtime_assets_reports_variant_supporting_scripts(tmp_path):
    processing_root = tmp_path / "processing"
    (processing_root / "other_code").mkdir(parents=True)
    (processing_root / "0_1th_box").mkdir(parents=True)
    (processing_root / "2_pt_project").mkdir(parents=True)
    (processing_root / "other_code" / "pcd_to_grid.py").write_text("# pcd\n", encoding="utf-8")
    (processing_root / "0_1th_box" / "gen_box.py").write_text("# gui\n", encoding="utf-8")
    (processing_root / "2_pt_project" / "2_othermethod_cjl.py").write_text("# legacy\n", encoding="utf-8")
    (processing_root / "2_pt_project" / "2_othermethod_cjl_0525.py").write_text("# go2w\n", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets", processing_root=processing_root)

    result = inspect_runtime_assets(settings=settings)

    assert result["pcd_gridmap_tool_available"] is True
    assert result["manual_annotation_gui_available"] is True
    assert result["projection_variants"] == {
        "cjl_with_gridmap": True,
        "cjl_0525_with_gridmap": True,
    }
