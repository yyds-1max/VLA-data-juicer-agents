from vla_data_juicer_agents.navigation.models import (
    NavigationDataProfile,
    PlanIssue,
    WorkflowPlan,
    WorkflowStep,
)
from vla_data_juicer_agents.navigation.plan_validation import validate_workflow_plan
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template


def test_validate_workflow_plan_accepts_default_template():
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
    )

    result = validate_workflow_plan(plan)

    assert result["ok"] is True
    assert result["errors"] == []


def test_validate_workflow_plan_rejects_unknown_tool():
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        dataset_profile="go2w_like",
        steps=[WorkflowStep(step_id="bad", tool_name="invented_tool")],
    )

    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "unknown_tool"


def test_validate_workflow_plan_rejects_unknown_variant():
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
    )
    gridmap = next(step for step in plan.steps if step.tool_name == "prepare_gridmap_for_projection")
    gridmap.variant = "made_up_variant"

    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "unknown_or_unavailable_variant"


def test_validate_workflow_plan_rejects_gridmap_before_tracking():
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
    )
    gridmap_index = next(index for index, step in enumerate(plan.steps) if step.tool_name == "prepare_gridmap_for_projection")
    tracking_index = next(index for index, step in enumerate(plan.steps) if step.tool_name == "run_tracking")
    plan.steps[gridmap_index], plan.steps[tracking_index] = plan.steps[tracking_index], plan.steps[gridmap_index]

    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "invalid_gridmap_stage_order"


def test_validate_workflow_plan_rejects_active_plan_with_blocking_profile():
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
    )
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        dataset_profile="go2w_like",
        blocking_issues=[PlanIssue(type="missing_gridmap_source_or_generator")],
    )

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "blocking_profile_has_active_plan"


def test_validate_workflow_plan_rejects_variant_selector_mismatch():
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        dataset_profile="go2w_like",
        gridmap_source="existing_gridmap",
    )
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
        data_profile=profile,
    )
    gridmap = next(step for step in plan.steps if step.tool_name == "prepare_gridmap_for_projection")
    gridmap.variant = "generate_from_pcd"

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "variant_selector_mismatch"


def test_validate_workflow_plan_blocks_unknown_gridmap_without_issue():
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        dataset_profile="go2w_like",
        gridmap_source="unknown",
        pcd_gridmap_tool_available=False,
    )
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
        data_profile=profile,
    )

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "missing_gridmap_source_or_generator"


def test_validate_workflow_plan_rejects_unknown_dataset_profile_from_json_path():
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
    )
    plan.dataset_profile = "invented_profile"

    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "unknown_dataset_profile"
