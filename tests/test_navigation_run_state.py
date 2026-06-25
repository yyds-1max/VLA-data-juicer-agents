import asyncio

import pytest

from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.tools.vla.run_workflow import RunVLAWorkflowInput, run_vla_workflow


def test_workflow_run_store_writes_request_and_plan(tmp_path):
    store = WorkflowRunStore(root=tmp_path)
    request = NavigationRequest(date="20270605", scene_mode="out")
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        processing_profile="parameterized_navigation_v1",
        platform_hint="go2w",
        steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
    )

    run_dir = store.create_run("20270605")
    store.write_json(run_dir, "request.json", request.model_dump(mode="json"))
    store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))

    assert (run_dir / "request.json").exists()
    assert (run_dir / "plan.json").exists()


def test_run_vla_workflow_requires_scene_mode_before_creating_run(tmp_path, monkeypatch):
    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))

    result = asyncio.run(
        run_vla_workflow(
            ctx=None,
            raw_args=RunVLAWorkflowInput(date="20270605", dry_run=True, approve=False),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "needs_user_input"
    assert result["error_type"] == "missing_scene_mode"
    assert "in" in result["message"]
    assert "out" in result["message"]
    assert not (tmp_path / "runs").exists()


def test_workflow_run_store_rejects_paths_outside_run_dir(tmp_path):
    store = WorkflowRunStore(root=tmp_path)
    run_dir = store.create_run("20270605")

    nested_path = store.write_json(run_dir, "steps/prepare.json", {})
    assert nested_path.exists()

    outside_path = run_dir.parent / "outside.json"
    with pytest.raises(ValueError):
        store.write_json(run_dir, "../outside.json", {})
    assert not outside_path.exists()

    absolute_outside_path = tmp_path / "absolute-outside.json"
    with pytest.raises(ValueError):
        store.write_json(run_dir, str(absolute_outside_path), {})
    assert not absolute_outside_path.exists()
