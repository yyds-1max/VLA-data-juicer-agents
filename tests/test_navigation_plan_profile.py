from pydantic import ValidationError

from vla_data_juicer_agents.navigation.models import (
    NavigationCalibrationPolicy,
    NavigationDataProfile,
    NavigationLocalizationPolicy,
    NavigationRequest,
    NavigationProcessingProfile,
    NavigationTopicParams,
    PlanIssue,
    StageVariantDecision,
    WorkflowStep,
)
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState, build_plan_from_draft
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template


def _complete_stage_variants(gridmap_variant: str, gridmap_reason: str, gridmap_evidence: list[str]):
    return {
        "extract_and_sync_navigation_data": StageVariantDecision(
            variant="go2w_like",
            reason="dataset classified as go2w_like",
            evidence=["classify_navigation_dataset_tool"],
        ),
        "prepare_gridmap_for_projection": StageVariantDecision(
            variant=gridmap_variant,
            reason=gridmap_reason,
            evidence=gridmap_evidence,
        ),
        "run_projection_and_trajectory": StageVariantDecision(
            variant="cjl_0525_with_gridmap",
            reason="go2w_like uses the 0525 projection script",
            evidence=["inspect_runtime_assets_tool"],
        ),
    }


def _go2w_topic_params() -> NavigationTopicParams:
    return NavigationTopicParams(
        profile_hint="go2w_like",
        confidence=1.0,
        topic_whitelist=[
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        topic_map={
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        query_dir="rs32_lidar_points",
        evidence=["infer_navigation_topic_params_tool"],
    )


def _processing_profile(
    *,
    topic_params: NavigationTopicParams | None = None,
    platform_hint: str = "unknown",
    gridmap_source: str = "generated_from_pcd",
    stage_variants: dict[str, StageVariantDecision] | None = None,
    blocking_issues: list[PlanIssue] | None = None,
) -> NavigationProcessingProfile:
    return NavigationProcessingProfile(
        id="parameterized_navigation_v1",
        platform_hint=platform_hint,
        topic_params=topic_params or _go2w_topic_params(),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        gridmap_policy={"source": gridmap_source},
        stage_variants=stage_variants or {},
        blocking_issues=blocking_issues or [],
    )


def test_lightweight_navigation_data_profile_keeps_only_variant_decision_facts():
    stage_variants = {
        "prepare_gridmap_for_projection": StageVariantDecision(
            variant="generate_from_pcd",
            reason="no existing grid_map artifact but PCD generator is available",
            evidence=["inspect_gridmap_artifacts_tool"],
        )
    }
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(stage_variants=stage_variants),
        platform_hint="unknown",
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        gridmap_source="generated_from_pcd",
        pcd_gridmap_tool_available=True,
        stage_variants=stage_variants,
        evidence={"processing_profile": ["infer_navigation_processing_profile_tool"]},
    )

    assert profile.segments is None
    assert profile.projection_input_ready is False
    assert profile.stage_variants["prepare_gridmap_for_projection"].variant == "generate_from_pcd"
    assert "raw_topics" not in NavigationDataProfile.model_fields
    assert "calibration" not in NavigationDataProfile.model_fields


def test_navigation_data_profile_records_blocking_issues_without_active_details():
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(gridmap_source="unknown"),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        gridmap_source="unknown",
        pcd_gridmap_tool_available=False,
        blocking_issues=[
            PlanIssue(
                type="missing_gridmap_source_or_generator",
                message="grid_map is required but no source or generator is available",
                evidence=["inspect_gridmap_artifacts_tool", "list_navigation_tool_capabilities_tool"],
            )
        ],
    )

    assert profile.blocking_issues[0].type == "missing_gridmap_source_or_generator"
    assert profile.stage_variants == {}


def test_navigation_data_profile_rejects_unknown_variant_inputs():
    with pytest_raises_validation_error():
        NavigationDataProfile(
            date="20270605",
            scene_mode="warehouse",
        )


def test_workflow_step_accepts_optional_variant_metadata():
    step = WorkflowStep(
        step_id="prepare_gridmap_for_projection",
        tool_name="prepare_gridmap_for_projection",
        arguments={"date": "20270605"},
        variant="generate_from_pcd",
        effects="execute",
        decision_ref="data_profile.stage_variants.prepare_gridmap_for_projection",
        evidence=["inspect_gridmap_artifacts_tool"],
    )

    assert step.variant == "generate_from_pcd"
    assert step.effects == "execute"
    assert step.decision_ref == "data_profile.stage_variants.prepare_gridmap_for_projection"
    assert step.evidence == ["inspect_gridmap_artifacts_tool"]


def test_plan_from_lightweight_profile_skips_gridmap_when_projection_input_ready():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(
            gridmap_source="projection_ready",
            stage_variants=_complete_stage_variants(
                "skip_if_projection_ready",
                "finish temp already contains projection grid_map inputs",
                ["inspect_gridmap_artifacts_tool"],
            ),
        ),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        gridmap_source="projection_ready",
        projection_input_ready=True,
        stage_variants=_complete_stage_variants(
            "skip_if_projection_ready",
            "finish temp already contains projection grid_map inputs",
            ["inspect_gridmap_artifacts_tool"],
        ),
    )

    state.update(data_profile=profile.model_dump(mode="json"))
    plan = build_plan_from_draft(state)

    step_names = [step.tool_name for step in plan.steps]
    assert "prepare_gridmap_for_projection" not in step_names
    projection = next(step for step in plan.steps if step.tool_name == "run_projection_and_trajectory")
    assert projection.preconditions == ["run_tracking"]


def test_workflow_plan_draft_snapshot_exposes_react_profile_state_panel():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    snapshot = state.schema_snapshot()

    assert snapshot["navigation_data_profile_schema"]["title"] == "NavigationDataProfile"
    assert "processing_profile" in snapshot["navigation_data_profile_schema"]["properties"]
    assert "dataset_profile" not in snapshot["navigation_data_profile_schema"]["properties"]
    assert "topic_params" in snapshot["navigation_data_profile_schema"]["properties"]
    assert snapshot["data_profile_draft"] == {
        "date": "20270605",
        "scene_mode": "out",
        "segments": None,
        "platform_hint": "unknown",
    }
    assert "date" in snapshot["filled_fields"]
    assert "scene_mode" in snapshot["filled_fields"]
    assert "processing_profile" in snapshot["missing_fields"]
    assert "topic_params" in snapshot["missing_fields"]
    assert "stage_variants.prepare_gridmap_for_projection" in snapshot["missing_fields"]
    assert snapshot["ready_to_finish"] is False
    assert "infer_navigation_topic_params_tool" in snapshot["next_tool_candidates"]


def test_workflow_plan_draft_requires_processing_profile_not_dataset_profile():
    state = WorkflowPlanDraftState(date="20270605", scene_mode="out")

    snapshot = state.snapshot()

    assert "processing_profile" in snapshot["navigation_data_profile_schema"]["properties"]
    assert "dataset_profile" not in snapshot["navigation_data_profile_schema"]["properties"]
    assert "processing_profile" in snapshot["missing_fields"]


def test_workflow_plan_draft_update_accepts_processing_profile_dict_argument():
    state = WorkflowPlanDraftState(date="20270605", scene_mode="out")

    result = state.update(
        processing_profile={
            "id": "parameterized_navigation_v1",
            "platform_hint": "custom_robot",
            "topic_params": _go2w_topic_params().model_dump(mode="json"),
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
        }
    )

    draft = result["draft"]["data_profile_draft"]
    assert state.processing_profile == "parameterized_navigation_v1"
    assert state.platform_hint == "custom_robot"
    assert draft["processing_profile"]["id"] == "parameterized_navigation_v1"
    assert draft["processing_profile"]["platform_hint"] == "custom_robot"
    assert result["draft"]["platform_hint"] == "custom_robot"
    assert "processing_profile" not in result["draft"]["missing_fields"]


def test_workflow_plan_draft_reports_blocking_issues_with_remediation_candidate():
    state = WorkflowPlanDraftState(date="20270605", scene_mode="out")

    result = state.update(
        data_profile_patch={
            "processing_profile": _processing_profile().model_dump(mode="json"),
            "topic_params": _go2w_topic_params().model_dump(mode="json"),
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "stage_variants": _complete_stage_variants(
                "generate_from_pcd",
                "no existing grid_map artifact but generator is available",
                ["inspect_gridmap_artifacts_tool"],
            ),
            "blocking_issues": [
                {
                    "type": "missing_gridmap_source_or_generator",
                    "message": "grid_map source is unresolved",
                }
            ],
        }
    )

    assert "blocking_issues" in result["draft"]["missing_fields"]
    assert "processing_profile.blocking_issues" not in result["draft"]["missing_fields"]
    assert "infer_navigation_processing_profile_tool" in result["draft"]["next_tool_candidates"]
    assert state.ready_to_finish() is False


def test_workflow_plan_draft_uses_nested_platform_hint_when_top_level_is_unknown():
    state = WorkflowPlanDraftState(date="20270605", scene_mode="out")
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(platform_hint="go2w"),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        stage_variants=_complete_stage_variants(
            "generate_from_pcd",
            "no existing grid_map artifact but generator is available",
            ["inspect_gridmap_artifacts_tool"],
        ),
    )

    result = state.update(data_profile=profile)

    assert state.platform_hint == "go2w"
    assert result["draft"]["data_profile_draft"]["platform_hint"] == "go2w"
    assert state.data_profile is not None
    assert state.data_profile.platform_hint == "go2w"


def test_plan_from_draft_accepts_complete_processing_profile():
    state = WorkflowPlanDraftState(date="20270605", scene_mode="out")

    state.update(
        data_profile_patch={
            "processing_profile": {
                "id": "parameterized_navigation_v1",
                "platform_hint": "unknown",
                "topic_params": {
                    "profile_hint": "mixed",
                    "confidence": 1.0,
                    "topic_whitelist": [
                        "/cam_video5/csi_cam/image_raw/compressed",
                        "/lidar_points",
                        "/sport_odom",
                    ],
                    "topic_map": {
                        "cam_video5": "fisheye_front",
                        "lidar_points": "r32_rslidar_points",
                        "sport_odom": "odom",
                    },
                    "query_dir": "lidar_points",
                },
                "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
                "gridmap_policy": {"source": "generated_from_pcd"},
                "calibration_policy": {
                    "mode": "hardcoded_with_user_confirmation",
                    "requires_user_confirmation": True,
                },
            },
            "platform_hint": "unknown",
            "topic_params": {
                "topic_whitelist": [
                    "/cam_video5/csi_cam/image_raw/compressed",
                    "/lidar_points",
                    "/sport_odom",
                ],
                "topic_map": {
                    "cam_video5": "fisheye_front",
                    "lidar_points": "r32_rslidar_points",
                    "sport_odom": "odom",
                },
                "query_dir": "lidar_points",
            },
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "stage_variants": {
                "extract_and_sync_navigation_data": {
                    "variant": "parameterized_ros2_bag",
                    "reason": "topic parameters inferred from metadata",
                    "evidence": ["infer_navigation_processing_profile_tool"],
                },
                "prepare_gridmap_for_projection": {
                    "variant": "generate_from_pcd",
                    "reason": "gridmap is generated from lidar",
                    "evidence": ["infer_navigation_processing_profile_tool"],
                },
                "run_projection_and_trajectory": {
                    "variant": "cjl_with_gridmap",
                    "reason": "projection uses generated gridmap",
                    "evidence": ["infer_navigation_processing_profile_tool"],
                },
            },
        }
    )

    plan = build_plan_from_draft(state)
    assert plan.processing_profile == "parameterized_navigation_v1"
    assert plan.platform_hint == "unknown"


def test_plan_from_processing_profile_inserts_calibration_confirmation_before_assemble():
    data_profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        platform_hint="unknown",
        processing_profile=NavigationProcessingProfile(
            id="parameterized_navigation_v1",
            platform_hint="unknown",
            topic_params=NavigationTopicParams(
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
            ),
            localization_policy=NavigationLocalizationPolicy(
                source="odom",
                conversion="odom_to_ins",
            ),
            calibration_policy=NavigationCalibrationPolicy(
                mode="hardcoded_with_user_confirmation",
                requires_user_confirmation=True,
            ),
        ),
        topic_params=NavigationTopicParams(
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
        ),
        localization_policy=NavigationLocalizationPolicy(
            source="odom",
            conversion="odom_to_ins",
        ),
    )

    plan = build_deterministic_plan_template(
        "20270605",
        None,
        None,
        scene_mode="out",
        data_profile=data_profile,
    )
    step_ids = [step.step_id for step in plan.steps]

    assert plan.processing_profile == "parameterized_navigation_v1"
    assert plan.platform_hint == "unknown"
    assert step_ids.index("confirm_navigation_calibration_params") < step_ids.index("assemble_finish_temp")
    confirm_step = next(step for step in plan.steps if step.step_id == "confirm_navigation_calibration_params")
    assert confirm_step.human_blocking is True
    preprocessing = next(step for step in plan.steps if step.step_id == "run_noobscene_preprocessing")
    assert preprocessing.arguments["localization_source"] == "odom"
    assert preprocessing.arguments["localization_conversion"] == "odom_to_ins"


def test_workflow_plan_draft_merges_data_profile_patches_across_react_rounds():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    first = state.update(
        data_profile_patch={
            "processing_profile": _processing_profile().model_dump(mode="json"),
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "topic_params": _go2w_topic_params().model_dump(mode="json"),
            "stage_variants": {
                "extract_and_sync_navigation_data": {
                    "variant": "go2w_like",
                    "reason": "classified as go2w_like",
                    "evidence": ["classify_navigation_dataset_tool"],
                },
            },
        },
        observation_id="dataset_classification",
        used_tool="classify_navigation_dataset_tool",
    )
    second = state.update(
        data_profile_patch={
            "gridmap_source": "existing_gridmap",
            "pcd_gridmap_tool_available": True,
            "stage_variants": {
                "prepare_gridmap_for_projection": {
                    "variant": "copy_existing_gridmap",
                    "reason": "grid_map artifacts already exist",
                    "evidence": ["inspect_gridmap_artifacts_tool"],
                },
                "run_projection_and_trajectory": {
                    "variant": "cjl_0525_with_gridmap",
                    "reason": "go2w_like uses 0525 projection",
                    "evidence": ["inspect_runtime_assets_tool"],
                },
            },
        },
        observation_id="gridmap_artifacts",
        used_tool="inspect_gridmap_artifacts_tool",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    draft = second["draft"]["data_profile_draft"]
    assert draft["processing_profile"]["id"] == "parameterized_navigation_v1"
    assert draft["topic_params"]["query_dir"] == "rs32_lidar_points"
    assert draft["gridmap_source"] == "existing_gridmap"
    assert draft["stage_variants"]["extract_and_sync_navigation_data"]["variant"] == "go2w_like"
    assert draft["stage_variants"]["prepare_gridmap_for_projection"]["variant"] == "copy_existing_gridmap"
    assert state.data_profile is not None
    assert state.data_profile.processing_profile is not None
    assert state.data_profile.processing_profile.id == "parameterized_navigation_v1"
    assert second["draft"]["ready_to_finish"] is True
    assert second["draft"]["completed_observations"] == [
        {"observation_id": "dataset_classification", "used_tool": "classify_navigation_dataset_tool"},
        {"observation_id": "gridmap_artifacts", "used_tool": "inspect_gridmap_artifacts_tool"},
    ]


def test_workflow_plan_draft_keeps_incomplete_patch_as_draft_only():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    result = state.update(
        data_profile_patch={"gridmap_source": "existing_gridmap"},
        observation_id="gridmap_artifacts",
        used_tool="inspect_gridmap_artifacts_tool",
    )

    assert result["ok"] is True
    assert result["draft"]["data_profile_draft"]["gridmap_source"] == "existing_gridmap"
    assert "processing_profile" in result["draft"]["missing_fields"]
    assert state.data_profile is None


def test_plan_from_draft_requires_complete_navigation_data_profile():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    state.update(data_profile_patch={"processing_profile": {"id": "parameterized_navigation_v1"}})

    with pytest_raises_value_error("NavigationDataProfile draft is incomplete"):
        build_plan_from_draft(state)


def test_plan_from_draft_requires_topic_params_before_finalizing():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    state.update(
        data_profile_patch={
            "processing_profile": {
                "id": "parameterized_navigation_v1",
                "platform_hint": "unknown",
                "topic_params": _go2w_topic_params().model_dump(mode="json"),
                "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            },
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_source": "existing_gridmap",
            "pcd_gridmap_tool_available": True,
            "stage_variants": _complete_stage_variants(
                "copy_existing_gridmap",
                "grid_map artifacts already exist",
                ["inspect_gridmap_artifacts_tool"],
            ),
        }
    )

    assert "topic_params" in state.missing_fields()
    assert state.ready_to_finish() is False
    with pytest_raises_value_error("topic_params"):
        build_plan_from_draft(state)


def test_plan_from_lightweight_profile_keeps_gridmap_generation_variant():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(
            stage_variants=_complete_stage_variants(
                "generate_from_pcd",
                "no existing grid_map artifact but generator is available",
                ["inspect_gridmap_artifacts_tool", "inspect_runtime_assets_tool"],
            ),
        ),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        gridmap_source="generated_from_pcd",
        stage_variants=_complete_stage_variants(
            "generate_from_pcd",
            "no existing grid_map artifact but generator is available",
            ["inspect_gridmap_artifacts_tool", "inspect_runtime_assets_tool"],
        ),
    )

    state.update(data_profile=profile.model_dump(mode="json"))
    plan = build_plan_from_draft(state)
    gridmap = next(step for step in plan.steps if step.tool_name == "prepare_gridmap_for_projection")

    assert gridmap.variant == "generate_from_pcd"
    assert gridmap.effects == "execute"
    assert gridmap.decision_ref == "data_profile.stage_variants.prepare_gridmap_for_projection"
    assert gridmap.evidence == ["inspect_gridmap_artifacts_tool", "inspect_runtime_assets_tool"]


def test_plan_from_lightweight_profile_writes_variant_metadata_for_profile_driven_steps():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(
            gridmap_source="existing_gridmap",
            stage_variants={
                "extract_and_sync_navigation_data": StageVariantDecision(
                    variant="go2w_like",
                    reason="dataset classified as go2w_like",
                    evidence=["classify_navigation_dataset_tool"],
                ),
                "prepare_gridmap_for_projection": StageVariantDecision(
                    variant="copy_existing_gridmap",
                    reason="sync data already contains grid_map",
                    evidence=["inspect_gridmap_artifacts_tool"],
                ),
                "run_projection_and_trajectory": StageVariantDecision(
                    variant="cjl_0525_with_gridmap",
                    reason="go2w_like uses the 0525 projection script",
                    evidence=["inspect_runtime_assets_tool"],
                ),
            },
        ),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        gridmap_source="existing_gridmap",
        stage_variants={
            "extract_and_sync_navigation_data": StageVariantDecision(
                variant="go2w_like",
                reason="dataset classified as go2w_like",
                evidence=["classify_navigation_dataset_tool"],
            ),
            "prepare_gridmap_for_projection": StageVariantDecision(
                variant="copy_existing_gridmap",
                reason="sync data already contains grid_map",
                evidence=["inspect_gridmap_artifacts_tool"],
            ),
            "run_projection_and_trajectory": StageVariantDecision(
                variant="cjl_0525_with_gridmap",
                reason="go2w_like uses the 0525 projection script",
                evidence=["inspect_runtime_assets_tool"],
            ),
        },
    )

    state.update(data_profile=profile.model_dump(mode="json"))
    plan = build_plan_from_draft(state)
    steps = {step.tool_name: step for step in plan.steps}

    assert steps["extract_and_sync_navigation_data"].variant == "go2w_like"
    assert steps["extract_and_sync_navigation_data"].arguments["topic_whitelist"] == [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    ]
    assert steps["extract_and_sync_navigation_data"].arguments["topic_map"] == {
        "cam_video4": "fisheye_front",
        "rs32_lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }
    assert steps["extract_and_sync_navigation_data"].arguments["query_dir"] == "rs32_lidar_points"
    assert steps["extract_and_sync_navigation_data"].decision_ref == (
        "data_profile.stage_variants.extract_and_sync_navigation_data"
    )
    assert steps["run_projection_and_trajectory"].variant == "cjl_0525_with_gridmap"
    assert steps["run_projection_and_trajectory"].decision_ref == (
        "data_profile.stage_variants.run_projection_and_trajectory"
    )


def test_plan_from_lightweight_profile_rejects_blocking_issues():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        processing_profile=_processing_profile(
            gridmap_source="unknown",
            stage_variants=_complete_stage_variants(
                "generate_from_pcd",
                "no existing grid_map artifact but generator is available",
                ["inspect_gridmap_artifacts_tool"],
            ),
        ),
        localization_policy={"source": "odom", "conversion": "odom_to_ins"},
        topic_params=_go2w_topic_params(),
        gridmap_source="unknown",
        pcd_gridmap_tool_available=False,
        stage_variants=_complete_stage_variants(
            "generate_from_pcd",
            "no existing grid_map artifact but generator is available",
            ["inspect_gridmap_artifacts_tool"],
        ),
        blocking_issues=[
            PlanIssue(type="missing_gridmap_source_or_generator", message="grid_map required")
        ],
    )

    state.update(data_profile=profile.model_dump(mode="json"))

    with pytest_raises_value_error("blocking_issues"):
        build_plan_from_draft(state)


class pytest_raises_validation_error:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        assert exc_type is ValidationError
        return True


class pytest_raises_value_error:
    def __init__(self, expected: str):
        self.expected = expected

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        assert exc_type is ValueError
        assert self.expected in str(exc)
        return True
