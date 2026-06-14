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
