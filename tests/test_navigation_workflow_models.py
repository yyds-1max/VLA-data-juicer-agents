import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep


def test_navigation_request_defaults_to_all_segments():
    request = NavigationRequest(date="20270605")

    assert request.date == "20270605"
    assert request.segments is None
    assert request.dry_run is False


def test_navigation_request_rejects_bad_date():
    with pytest.raises(ValueError):
        NavigationRequest(date="2026-06-05")


def test_navigation_settings_derives_data_roots(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    assert settings.raw_data_root == tmp_path / "VLADatasets" / "raw_data"
    assert settings.clip_data_root == tmp_path / "VLADatasets" / "clip_data"
    assert settings.finish_data_root == tmp_path / "VLADatasets" / "finish_data"


def test_workflow_plan_keeps_ordered_steps():
    plan = WorkflowPlan(
        date="20270605",
        segments=["20260605_152856"],
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
