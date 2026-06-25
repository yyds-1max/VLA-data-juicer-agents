import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import (
    NavigationCalibrationPolicy,
    NavigationGridmapPolicy,
    NavigationLocalizationPolicy,
    NavigationProcessingProfile,
    NavigationRequest,
    NavigationSensorBinding,
    NavigationSensorBindings,
    NavigationTopicParams,
    ProfileClassification,
    WorkflowPlan,
    WorkflowStep,
)


def test_navigation_request_defaults_to_all_segments():
    request = NavigationRequest(date="20270605")

    assert request.date == "20270605"
    assert request.segments is None
    assert request.dry_run is False
    assert request.scene_mode is None


def test_navigation_request_accepts_scene_mode():
    request = NavigationRequest(date="20270605", scene_mode="in")

    assert request.scene_mode == "in"


def test_navigation_request_rejects_unknown_scene_mode():
    with pytest.raises(ValueError):
        NavigationRequest(date="20270605", scene_mode="indoor")


def test_navigation_request_rejects_bad_date():
    with pytest.raises(ValueError):
        NavigationRequest(date="2026-06-05")


def test_navigation_settings_derives_data_roots(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    assert settings.raw_data_root == tmp_path / "VLADatasets" / "raw_data"
    assert settings.clip_data_root == tmp_path / "VLADatasets" / "clip_data"
    assert settings.finish_data_root == tmp_path / "VLADatasets" / "finish_data"


def test_navigation_settings_derives_processing_scripts(tmp_path):
    settings = NavigationSettings(processing_root=tmp_path / "processing")

    assert settings.pcd_to_grid_script == tmp_path / "processing" / "other_code" / "pcd_to_grid.py"
    assert settings.gen_box_script == tmp_path / "processing" / "0_1th_box" / "gen_box.py"


def test_profile_classification_accepts_missing_profile():
    classification = ProfileClassification(profile_name=None, confidence=0.0)

    assert classification.profile_name is None


def test_navigation_processing_profile_accepts_mixed_topic_bindings():
    topic_params = NavigationTopicParams(
        profile_hint="mixed",
        confidence=1.0,
        topic_whitelist=[
            "/cam_video5/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/sport_odom",
        ],
        topic_map={
            "cam_video5": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        query_dir="lidar_points",
    )
    bindings = NavigationSensorBindings(
        fisheye_front=NavigationSensorBinding(
            role="fisheye_front",
            topic="/cam_video5/csi_cam/image_raw/compressed",
            message_type="sensor_msgs/msg/CompressedImage",
        ),
        lidar=NavigationSensorBinding(
            role="lidar",
            topic="/lidar_points",
            message_type="sensor_msgs/msg/PointCloud2",
        ),
        localization=NavigationSensorBinding(
            role="localization",
            topic="/sport_odom",
            message_type="nav_msgs/msg/Odometry",
            kind="odom",
        ),
    )
    profile = NavigationProcessingProfile(
        id="parameterized_navigation_v1",
        platform_hint="unknown",
        sensor_bindings=bindings,
        topic_params=topic_params,
        localization_policy=NavigationLocalizationPolicy(
            source="odom",
            conversion="odom_to_ins",
        ),
        gridmap_policy=NavigationGridmapPolicy(source="generated_from_pcd"),
        calibration_policy=NavigationCalibrationPolicy(
            mode="hardcoded_with_user_confirmation",
            requires_user_confirmation=True,
        ),
    )

    assert profile.platform_hint == "unknown"
    assert profile.topic_params.query_dir == "lidar_points"
    assert profile.localization_policy.conversion == "odom_to_ins"


def test_navigation_sensor_bindings_reject_role_mismatch():
    with pytest.raises(ValueError, match="sensor binding role mismatch for lidar"):
        NavigationSensorBindings(
            lidar=NavigationSensorBinding(
                role="fisheye_front",
                topic="/cam_video5/csi_cam/image_raw/compressed",
            )
        )


def test_workflow_plan_uses_processing_profile_instead_of_dataset_profile():
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        processing_profile="parameterized_navigation_v1",
        platform_hint="unknown",
        steps=[],
    )

    payload = plan.model_dump(mode="json")
    assert payload["processing_profile"] == "parameterized_navigation_v1"
    assert payload["platform_hint"] == "unknown"
    assert "dataset_profile" not in payload


def test_workflow_plan_keeps_ordered_steps():
    plan = WorkflowPlan(
        date="20270605",
        segments=["20260605_152856"],
        scene_mode="out",
        processing_profile="parameterized_navigation_v1",
        steps=[
            WorkflowStep(
                step_id="prepare_raw_data",
                tool_name="prepare_raw_data",
                arguments={"date": "20270605", "segments": ["20260605_152856"]},
                expected_outputs=["raw_data/20270605_temp/20260605_152856"],
            )
        ],
    )

    assert plan.steps[0].tool_name == "prepare_raw_data"
    assert plan.scene_mode == "out"


def test_workflow_plan_rejects_missing_scene_mode():
    with pytest.raises(ValueError):
        WorkflowPlan(
            date="20270605",
            scene_mode=None,
            processing_profile="parameterized_navigation_v1",
            steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
        )

    with pytest.raises(ValueError):
        WorkflowPlan(
            date="20270605",
            processing_profile="parameterized_navigation_v1",
            steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
        )
