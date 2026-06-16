import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import (
    NavigationRequest,
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


def test_workflow_plan_keeps_ordered_steps():
    plan = WorkflowPlan(
        date="20270605",
        segments=["20260605_152856"],
        scene_mode="out",
        dataset_profile="go2w_like",
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
            dataset_profile="go2w_like",
            steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
        )

    with pytest.raises(ValueError):
        WorkflowPlan(
            date="20270605",
            dataset_profile="go2w_like",
            steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
        )
