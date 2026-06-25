from vla_data_juicer_agents.navigation.models import (
    NavigationDataProfile,
    NavigationProcessingProfile,
    NavigationTopicParams,
    PlanIssue,
    WorkflowPlan,
    WorkflowStep,
)
from vla_data_juicer_agents.navigation.plan_validation import validate_workflow_plan
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template


def _topic_params(profile_hint: str = "mixed") -> NavigationTopicParams:
    return NavigationTopicParams(
        profile_hint=profile_hint,
        confidence=1.0,
        topic_whitelist=["/cam", "/lidar_points", "/sport_odom"],
        topic_map={"cam": "fisheye_front", "lidar_points": "r32_rslidar_points", "sport_odom": "odom"},
        query_dir="lidar_points",
    )


def _processing_profile(
    *,
    profile_hint: str = "mixed",
    platform_hint: str = "unknown",
) -> NavigationProcessingProfile:
    return NavigationProcessingProfile(
        id="parameterized_navigation_v1",
        platform_hint=platform_hint,
        topic_params=_topic_params(profile_hint),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
    )


def _go2w_data_profile() -> NavigationDataProfile:
    return NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(profile_hint="go2w_like"),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_topic_params("go2w_like"),
    )


def test_validate_workflow_plan_accepts_default_template():
    profile = _go2w_data_profile()
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
        data_profile=profile,
    )

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is True
    assert result["errors"] == []


def test_validate_workflow_plan_rejects_unknown_tool():
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        processing_profile="parameterized_navigation_v1",
        platform_hint="unknown",
        steps=[WorkflowStep(step_id="bad", tool_name="invented_tool")],
    )

    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "unknown_tool"


def test_validate_workflow_plan_rejects_unknown_variant():
    profile = _go2w_data_profile()
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
        data_profile=profile,
    )
    gridmap = next(step for step in plan.steps if step.tool_name == "prepare_gridmap_for_projection")
    gridmap.variant = "made_up_variant"

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "unknown_or_unavailable_variant"


def test_validate_workflow_plan_checks_legacy_dataset_profile_selectors_from_processing_facts():
    profile = _go2w_data_profile()
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        processing_profile="parameterized_navigation_v1",
        platform_hint="unknown",
        steps=[
            WorkflowStep(
                step_id="extract_and_sync_navigation_data",
                tool_name="extract_and_sync_navigation_data",
                variant="u_legacy_like",
            )
        ],
    )

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "variant_selector_mismatch"
    assert result["errors"][0]["details"]["selector"] == "dataset_profile"
    assert result["errors"][0]["details"]["actual"] == "go2w_like"


def test_validate_workflow_plan_derives_legacy_dataset_profile_from_plan_only_facts():
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        processing_profile="go2w_like",
        platform_hint="unknown",
        steps=[
            WorkflowStep(
                step_id="extract_and_sync_navigation_data",
                tool_name="extract_and_sync_navigation_data",
                variant="go2w_like",
            )
        ],
    )

    result = validate_workflow_plan(plan)

    assert result["ok"] is True
    assert result["errors"] == []

    plan.processing_profile = "parameterized_navigation_v1"
    plan.platform_hint = "u"
    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "variant_selector_mismatch"
    assert result["errors"][0]["details"]["selector"] == "dataset_profile"
    assert result["errors"][0]["details"]["actual"] == "u_legacy_like"


def test_validate_workflow_plan_rejects_gridmap_before_tracking():
    profile = _go2w_data_profile()
    plan = build_deterministic_plan_template(
        "20270605",
        "go2w_like",
        None,
        scene_mode="out",
        data_profile=profile,
    )
    gridmap_index = next(index for index, step in enumerate(plan.steps) if step.tool_name == "prepare_gridmap_for_projection")
    tracking_index = next(index for index, step in enumerate(plan.steps) if step.tool_name == "run_tracking")
    plan.steps[gridmap_index], plan.steps[tracking_index] = plan.steps[tracking_index], plan.steps[gridmap_index]

    result = validate_workflow_plan(plan, data_profile=profile)

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
        processing_profile=_processing_profile(),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        blocking_issues=[PlanIssue(type="missing_gridmap_source_or_generator")],
    )

    result = validate_workflow_plan(plan, data_profile=profile)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "blocking_profile_has_active_plan"


def test_validate_workflow_plan_rejects_variant_selector_mismatch():
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
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
        processing_profile=_processing_profile(),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
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


def test_validate_workflow_plan_rejects_empty_processing_profile_from_json_path():
    plan = WorkflowPlan(
        date="20270605",
        scene_mode="out",
        processing_profile="",
        platform_hint="unknown",
        steps=[],
    )

    result = validate_workflow_plan(plan)

    assert result["ok"] is False
    assert result["errors"][0]["type"] == "missing_processing_profile"
