import pytest

from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore


def test_workflow_run_store_writes_request_and_plan(tmp_path):
    store = WorkflowRunStore(root=tmp_path)
    request = NavigationRequest(date="20270605")
    plan = WorkflowPlan(
        date="20270605",
        dataset_profile="go2w_like",
        steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
    )

    run_dir = store.create_run("20270605")
    store.write_json(run_dir, "request.json", request.model_dump(mode="json"))
    store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))

    assert (run_dir / "request.json").exists()
    assert (run_dir / "plan.json").exists()


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
