from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionBase(StrictModel):
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1)


class SensorBindingDecision(DecisionBase):
    bindings: dict[
        Literal["fisheye_front", "lidar", "odom", "ins", "localization", "gridmap"],
        str,
    ] = Field(
        description=(
            "Model-selected sensor role to full observed ROS topic name. Bind localization "
            "to the selected native Ins topic, or to the exact odom topic when Ins is absent."
        )
    )


class TopicSelectionDecision(DecisionBase):
    topic_whitelist: list[str] = Field(
        min_length=1,
        description=(
            "Full observed ROS topic names to extract. Select the bound front fisheye, "
            "lidar, and localization topic, plus a selected gridmap topic when applicable."
        ),
    )
    topic_map: dict[str, str] = Field(
        min_length=1,
        description=(
            "Mapping from extracted tmp_dir child names to canonical sync_data child names, "
            "for example rs32_lidar_points -> r32_rslidar_points. Use observed topic routes."
        ),
    )
    query_dir: str = Field(
        min_length=1,
        description=(
            "One relative tmp_dir child name used as the timestamp reference, for example "
            "rs32_lidar_points. Never provide a ROS topic or filesystem path."
        ),
    )


class TimeSyncDecision(DecisionBase):
    reference_sensor: str = Field(
        description=(
            "A bound sensor role whose observed extracted_dir equals topic_selection.query_dir; "
            "normally lidar, or gridmap when an extracted gridmap stream is selected."
        )
    )
    method: Literal["nearest_timestamp"]


class LocalizationDecision(DecisionBase):
    source: Literal["odom", "ins"] = Field(
        description="Observed localization representation selected for finish processing."
    )
    conversion: Literal["odom_to_ins", "none"] = Field(
        description="Use odom_to_ins for odom input and none for native Ins input."
    )


class GridmapDecision(DecisionBase):
    source: Literal["existing_gridmap", "generated_from_pcd", "projection_ready"]


class CalibrationDecision(DecisionBase):
    mode: Literal[
        "hardcoded_with_user_confirmation",
        "selected_profile",
        "annotation_snapshot",
    ]
    selected_sensor_source: str | None
    requires_user_confirmation: bool

    @model_validator(mode="after")
    def validate_source_contract(self) -> "CalibrationDecision":
        if self.mode == "annotation_snapshot":
            if self.selected_sensor_source is not None:
                raise ValueError(
                    "annotation_snapshot calibration is resolved server-side"
                )
            if self.requires_user_confirmation:
                raise ValueError(
                    "annotation_snapshot calibration does not request confirmation"
                )
        elif not isinstance(self.selected_sensor_source, str) or not (
            self.selected_sensor_source.strip()
        ):
            raise ValueError("selected calibration source must be non-empty")
        return self


class EmptyArguments(StrictModel):
    pass


class ExtractSyncArguments(StrictModel):
    processes_num: int = Field(ge=1, le=64)


class StepBase(StrictModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    depends_on: list[str]
    failure_policy: Literal["stop"]
    decision_refs: list[str]


class PrepareRawStep(StepBase):
    action: Literal["prepare_raw_data"]
    variant: Literal["default"]
    arguments: EmptyArguments


class ExtractSyncStep(StepBase):
    action: Literal["extract_and_sync_navigation_data"]
    variant: Literal["explicit_topic_params"]
    arguments: ExtractSyncArguments


class ConfirmCalibrationStep(StepBase):
    action: Literal["confirm_navigation_calibration_params"]
    variant: Literal["default"]
    arguments: EmptyArguments


class AssembleFinishTempStep(StepBase):
    action: Literal["assemble_finish_temp"]
    variant: Literal["default"]
    arguments: EmptyArguments


class NoobscenePreprocessingStep(StepBase):
    action: Literal["run_noobscene_preprocessing"]
    variant: Literal["default"]
    arguments: EmptyArguments


class InitialAnnotationStep(StepBase):
    action: Literal["run_initial_annotation_gui"]
    variant: Literal["human_gui"]
    arguments: EmptyArguments


class TrackingStep(StepBase):
    action: Literal["run_tracking"]
    variant: Literal["default"]
    arguments: EmptyArguments


class AnnotationTrackingWorkflowStep(StepBase):
    action: Literal["run_annotation_tracking_workflow"]
    variant: Literal["durable_web_handoff"]
    arguments: EmptyArguments


class PrepareGridmapStep(StepBase):
    action: Literal["prepare_gridmap_for_projection"]
    variant: Literal["copy_existing_gridmap", "generate_from_pcd", "skip_if_projection_ready"]
    arguments: EmptyArguments


class ProjectionStep(StepBase):
    action: Literal["run_projection_and_trajectory"]
    variant: Literal["cjl_with_gridmap", "cjl_0525_with_gridmap"]
    arguments: EmptyArguments


class AnnotationPostprocessingWorkflowStep(StepBase):
    action: Literal["run_annotation_postprocessing_workflow"]
    variant: Literal["plan_bound_runtime"]
    arguments: EmptyArguments


class ValidateOutputsStep(StepBase):
    action: Literal["validate_navigation_outputs"]
    variant: Literal["expect_gridmap"]
    arguments: EmptyArguments


class OpenTrajectoryFixWorkbenchStep(StepBase):
    action: Literal["open_trajectory_fix_workbench"]
    variant: Literal["durable_human_handoff"]
    arguments: EmptyArguments


class ValidateTrajectoryReviewOutcomeStep(StepBase):
    action: Literal["validate_trajectory_review_outcome"]
    variant: Literal["approved_or_terminal"]
    arguments: EmptyArguments


ExtractSyncStepInput = Annotated[
    PrepareRawStep | ExtractSyncStep,
    Field(discriminator="action"),
]
FinishProcessingStepInput = Annotated[
    ConfirmCalibrationStep
    | AssembleFinishTempStep
    | NoobscenePreprocessingStep
    | InitialAnnotationStep
    | TrackingStep
    | AnnotationTrackingWorkflowStep
    | PrepareGridmapStep
    | ProjectionStep
    | AnnotationPostprocessingWorkflowStep
    | ValidateOutputsStep,
    Field(discriminator="action"),
]
TrajectoryReviewStepInput = Annotated[
    OpenTrajectoryFixWorkbenchStep | ValidateTrajectoryReviewOutcomeStep,
    Field(discriminator="action"),
]


class ExtractSyncDecisions(StrictModel):
    sensor_bindings: SensorBindingDecision
    topic_selection: TopicSelectionDecision
    time_sync: TimeSyncDecision


class FinishProcessingDecisions(StrictModel):
    localization: LocalizationDecision
    gridmap: GridmapDecision
    calibration: CalibrationDecision


class TrajectoryReviewDecision(DecisionBase):
    mode: Literal["human_fix"]


class TrajectoryReviewDecisions(StrictModel):
    review: TrajectoryReviewDecision


class ExtractSyncPlanInput(StrictModel):
    decisions: ExtractSyncDecisions
    steps: list[ExtractSyncStepInput] = Field(min_length=1)


class FinishProcessingPlanInput(StrictModel):
    decisions: FinishProcessingDecisions
    steps: list[FinishProcessingStepInput] = Field(min_length=1)


class TrajectoryReviewPlanInput(StrictModel):
    decisions: TrajectoryReviewDecisions
    steps: list[TrajectoryReviewStepInput] = Field(min_length=1)


class PlanValidationIssue(StrictModel):
    path: str
    code: str
    message: str
    allowed_values: list[str] = Field(default_factory=list)


class PlanValidationReport(StrictModel):
    ok: bool
    errors: list[PlanValidationIssue] = Field(default_factory=list)
    warnings: list[PlanValidationIssue] = Field(default_factory=list)


class NavigationPlanRecord(StrictModel):
    plan_id: str
    task_id: str
    phase: Literal["extract_sync", "finish_processing", "trajectory_review"]
    plan_revision: int
    contract_version: str
    observation_revision: int
    status: Literal["active", "superseded", "completed", "invalidated"]
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput | TrajectoryReviewPlanInput
    created_at: str


class PlanSubmissionAttempt(StrictModel):
    attempt_id: str
    task_id: str
    phase: Literal["extract_sync", "finish_processing", "trajectory_review"]
    planning_context_revision: str
    candidate: dict[str, Any]
    validation: PlanValidationReport
    created_at: str


class ExecutionStepRecord(StrictModel):
    id: str
    plan_id: str
    plan_revision: int
    sequence: int
    step_id: str
    action: str
    status: Literal["pending", "running", "waiting_user", "completed", "failed", "needs_replan"]
    result_summary: dict[str, Any] | None = None
    result_ref: str | None = None
    retry_count: int = 0


class PlanExecutionOverview(StrictModel):
    plan_id: str
    plan_revision: int
    status: str
    total_steps: int
    completed_steps: int
    current_step_id: str | None
    steps: list[ExecutionStepRecord]
