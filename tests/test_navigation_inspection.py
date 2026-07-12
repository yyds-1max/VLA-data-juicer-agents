from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.inspection import (
    infer_navigation_sensor_bindings,
    infer_navigation_sensor_bindings_tool,
    infer_navigation_topic_params,
    infer_navigation_topic_params_tool,
    inspect_gridmap_artifacts,
    inspect_gridmap_artifacts_tool,
    inspect_navigation_sensor_candidates,
    inspect_navigation_topic_candidates,
    inspect_processing_state,
    inspect_processing_state_tool,
    inspect_raw_date,
    inspect_runtime_assets,
    inspect_runtime_assets_tool,
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


def test_navigation_investigation_tools_allow_structured_or_json_encoded_segments():
    tools = [
        infer_navigation_topic_params_tool,
        infer_navigation_sensor_bindings_tool,
        inspect_processing_state_tool,
        inspect_gridmap_artifacts_tool,
    ]

    for tool in tools:
        segments_schema = tool.input_schema["properties"]["segments"]
        schema_options = segments_schema.get("anyOf", [segments_schema])
        assert {"type": "string"} in schema_options


def test_infer_navigation_topic_params_detects_u_like_fixture():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = infer_navigation_topic_params("20270515", settings=settings)

    assert result.profile_hint == "u_like"
    assert result.confidence == 1.0
    assert result.topic_whitelist == [
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/utlidar/robot_odom_systime",
    ]
    assert result.topic_map == {
        "cam_video5": "fisheye_front",
        "lidar_points": "r32_rslidar_points",
        "utlidar": "odom",
    }
    assert result.query_dir == "lidar_points"
    assert result.blocking_issues == []


def test_infer_navigation_topic_params_detects_go2w_fixture():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = infer_navigation_topic_params("20270605", settings=settings)

    assert result.profile_hint == "go2w_like"
    assert result.topic_whitelist == [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    ]
    assert result.topic_map == {
        "cam_video4": "fisheye_front",
        "rs32_lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }
    assert result.query_dir == "rs32_lidar_points"


def test_infer_navigation_topic_params_detects_hybrid_fixture(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270606" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        """
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 10
    - topic_metadata:
        name: /sport_odom
        type: nav_msgs/msg/Odometry
      message_count: 10
""",
        encoding="utf-8",
    )
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = infer_navigation_topic_params("20270606", settings=settings)

    assert result.profile_hint == "hybrid"
    assert result.topic_whitelist == [
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/sport_odom",
    ]
    assert result.topic_map == {
        "cam_video5": "fisheye_front",
        "lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }
    assert result.query_dir == "lidar_points"
    assert result.blocking_issues == []


@pytest.mark.parametrize(
    ("camera_topic", "lidar_topic", "localization_topic", "expected_topic_map", "expected_query_dir"),
    [
        (
            "/cam_video4/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/utlidar/robot_odom_systime",
            {
                "cam_video4": "fisheye_front",
                "lidar_points": "r32_rslidar_points",
                "utlidar": "odom",
            },
            "lidar_points",
        ),
        (
            "/cam_video4/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/sport_odom",
            {
                "cam_video4": "fisheye_front",
                "lidar_points": "r32_rslidar_points",
                "sport_odom": "odom",
            },
            "lidar_points",
        ),
        (
            "/cam_video5/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/utlidar/robot_odom_systime",
            {
                "cam_video5": "fisheye_front",
                "rs32_lidar_points": "r32_rslidar_points",
                "utlidar": "odom",
            },
            "rs32_lidar_points",
        ),
        (
            "/cam_video5/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
            {
                "cam_video5": "fisheye_front",
                "rs32_lidar_points": "r32_rslidar_points",
                "sport_odom": "odom",
            },
            "rs32_lidar_points",
        ),
        (
            "/cam_video5/csi_cam/image_raw/compressed",
            "/r32_rslidar_points",
            "/drivers/ins/Ins",
            {
                "cam_video5": "fisheye_front",
                "r32_rslidar_points": "r32_rslidar_points",
                "Ins": "ins",
            },
            "r32_rslidar_points",
        ),
    ],
)
def test_infer_navigation_topic_params_supports_role_based_mixed_topics(
    tmp_path,
    camera_topic,
    lidar_topic,
    localization_topic,
    expected_topic_map,
    expected_query_dir,
):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270608" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    localization_type = (
        "custom_msgs/msg/Ins"
        if localization_topic == "/drivers/ins/Ins"
        else "nav_msgs/msg/Odometry"
    )
    metadata_path.write_text(
        f"""
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: {camera_topic}
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: {lidar_topic}
        type: sensor_msgs/msg/PointCloud2
      message_count: 10
    - topic_metadata:
        name: {localization_topic}
        type: {localization_type}
      message_count: 10
""",
        encoding="utf-8",
    )
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = infer_navigation_topic_params("20270608", settings=settings)

    assert result.profile_hint == "hybrid"
    assert result.confidence == 1.0
    assert result.topic_whitelist == [camera_topic, lidar_topic, localization_topic]
    assert result.topic_map == expected_topic_map
    assert result.query_dir == expected_query_dir
    assert result.blocking_issues == []


def test_infer_navigation_topic_params_reports_blocking_issue_when_required_topic_missing(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270606" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        """
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 10
""",
        encoding="utf-8",
    )
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = infer_navigation_topic_params("20270606", settings=settings)

    assert result.profile_hint is None
    assert result.topic_whitelist == [
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
    ]
    assert result.query_dir == "lidar_points"
    assert result.blocking_issues
    assert any(issue.type == "missing_navigation_topic_params" for issue in result.blocking_issues)


def test_infer_navigation_topic_params_blocks_selected_metadata_errors(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270610" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        """
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 10
    - topic_metadata:
        name: /utlidar/robot_odom_systime
        type: nav_msgs/msg/Odometry
      message_count: 10
""",
        encoding="utf-8",
    )
    bad_segment = tmp_path / "VLADatasets" / "raw_data" / "20270610" / "segment_b"
    bad_segment.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = infer_navigation_topic_params(
        "20270610",
        segments=["segment_a", "segment_b"],
        settings=settings,
    )

    assert result.blocking_issues
    assert result.confidence == 0.0
    assert any(
        issue.type == "raw_metadata_error" and "segment_b" in " ".join(issue.evidence)
        for issue in result.blocking_issues
    )


def test_infer_navigation_topic_params_tool_is_read_only():
    assert infer_navigation_topic_params_tool.name == "infer_navigation_topic_params_tool"
    assert infer_navigation_topic_params_tool.is_read_only is True


def test_infer_navigation_sensor_bindings_prefers_unique_ins_over_ambiguous_odom(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270608" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        """
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 10
    - topic_metadata:
        name: /drivers/ins/Ins
        type: custom_msgs/msg/Ins
      message_count: 10
    - topic_metadata:
        name: /sport_odom
        type: nav_msgs/msg/Odometry
      message_count: 10
    - topic_metadata:
        name: /utlidar/robot_odom_systime
        type: nav_msgs/msg/Odometry
      message_count: 10
""",
        encoding="utf-8",
    )
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = infer_navigation_sensor_bindings("20270608", settings=settings)

    assert result.localization is not None
    assert result.localization.kind == "ins"
    assert not any(
        issue.type == "ambiguous_navigation_topic_role" and issue.evidence == [
            "/sport_odom",
            "/utlidar/robot_odom_systime",
        ]
        for issue in result.blocking_issues
    )


def test_infer_navigation_sensor_bindings_blocks_ambiguous_camera_role(tmp_path):
    metadata_path = tmp_path / "VLADatasets" / "raw_data" / "20270608" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        """
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video4/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 10
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 10
    - topic_metadata:
        name: /sport_odom
        type: nav_msgs/msg/Odometry
      message_count: 10
""",
        encoding="utf-8",
    )
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    result = infer_navigation_sensor_bindings("20270608", settings=settings)

    assert result.fisheye_front is not None
    assert result.fisheye_front.candidates == [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/cam_video5/csi_cam/image_raw/compressed",
    ]
    assert any(issue.type == "ambiguous_navigation_topic_role" for issue in result.blocking_issues)


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


def test_new_plan_agent_inspection_tools_are_read_only():
    assert inspect_processing_state_tool.name == "inspect_processing_state_tool"
    assert inspect_gridmap_artifacts_tool.name == "inspect_gridmap_artifacts_tool"
    assert inspect_runtime_assets_tool.name == "inspect_runtime_assets_tool"
    assert inspect_processing_state_tool.is_read_only is True
    assert inspect_gridmap_artifacts_tool.is_read_only is True
    assert inspect_runtime_assets_tool.is_read_only is True
