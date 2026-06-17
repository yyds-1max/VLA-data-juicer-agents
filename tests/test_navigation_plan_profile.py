from pydantic import ValidationError

from vla_data_juicer_agents.navigation.models import (
    NavigationDataProfile,
    NavigationRequest,
    PlanIssue,
    StageVariantDecision,
    WorkflowStep,
)
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState, build_plan_from_draft


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


def test_lightweight_navigation_data_profile_keeps_only_variant_decision_facts():
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        dataset_profile="go2w_like",
        gridmap_source="generated_from_pcd",
        pcd_gridmap_tool_available=True,
        stage_variants={
            "prepare_gridmap_for_projection": StageVariantDecision(
                variant="generate_from_pcd",
                reason="no existing grid_map artifact but PCD generator is available",
                evidence=["inspect_gridmap_artifacts_tool"],
            )
        },
        evidence={"dataset_profile": ["classify_navigation_dataset_tool"]},
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
        dataset_profile="go2w_like",
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
            dataset_profile="go2w_like",
        )

    with pytest_raises_validation_error():
        NavigationDataProfile(
            date="20270605",
            scene_mode="out",
            dataset_profile="unknown_profile",
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
        dataset_profile="go2w_like",
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
    assert snapshot["navigation_data_profile_schema"]["properties"]["dataset_profile"]["enum"] == [
        "u_legacy_like",
        "go2w_like",
    ]
    assert snapshot["data_profile_draft"] == {
        "date": "20270605",
        "scene_mode": "out",
        "segments": None,
    }
    assert "date" in snapshot["filled_fields"]
    assert "scene_mode" in snapshot["filled_fields"]
    assert "dataset_profile" in snapshot["missing_fields"]
    assert "stage_variants.prepare_gridmap_for_projection" in snapshot["missing_fields"]
    assert snapshot["ready_to_finish"] is False
    assert "classify_navigation_dataset_tool" in snapshot["next_tool_candidates"]


def test_workflow_plan_draft_merges_data_profile_patches_across_react_rounds():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    first = state.update(
        data_profile_patch={
            "dataset_profile": "go2w_like",
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
    assert draft["dataset_profile"] == "go2w_like"
    assert draft["gridmap_source"] == "existing_gridmap"
    assert draft["stage_variants"]["extract_and_sync_navigation_data"]["variant"] == "go2w_like"
    assert draft["stage_variants"]["prepare_gridmap_for_projection"]["variant"] == "copy_existing_gridmap"
    assert state.data_profile is not None
    assert state.data_profile.dataset_profile == "go2w_like"
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
    assert "dataset_profile" in result["draft"]["missing_fields"]
    assert state.data_profile is None


def test_plan_from_draft_requires_complete_navigation_data_profile():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )

    state.update(data_profile_patch={"dataset_profile": "go2w_like"})

    with pytest_raises_value_error("NavigationDataProfile draft is incomplete"):
        build_plan_from_draft(state)


def test_plan_from_lightweight_profile_keeps_gridmap_generation_variant():
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    profile = NavigationDataProfile(
        date="20270605",
        scene_mode="out",
        dataset_profile="go2w_like",
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
        dataset_profile="go2w_like",
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
        dataset_profile="go2w_like",
        gridmap_source="unknown",
        pcd_gridmap_tool_available=False,
        blocking_issues=[
            PlanIssue(type="missing_gridmap_source_or_generator", message="grid_map required")
        ],
    )

    state.update(data_profile=profile.model_dump(mode="json"))

    with pytest_raises_value_error("blocking issues"):
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
