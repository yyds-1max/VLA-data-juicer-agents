from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base class for versioned evaluation inputs and outputs."""

    model_config = ConfigDict(extra="forbid")


class ConversationTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class CaseLimits(StrictModel):
    max_model_calls: int = Field(default=4, ge=1)
    max_tool_calls: int = Field(default=4, ge=0)
    timeout_seconds: int = Field(default=180, ge=1, le=1800)


class ExpectedHandoff(StrictModel):
    # Contract-v1 historical handoff fields. They remain readable so the
    # frozen router-smoke artifacts can still be inspected and compared.
    request: str | None = None
    target: str | None = None
    date: str | None = Field(default=None, pattern=r"^[0-9]{8}$")
    clips: list[str] = Field(default_factory=list)
    response_language: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    allowed_confidence: list[Literal["medium", "high"]] = Field(
        default_factory=lambda: ["medium", "high"],
        min_length=1,
    )
    # DataPilot session contract v1 operation fields.
    operation: Literal[
        "start",
        "continue",
        "stop",
        "cancel",
        "submit_plan",
    ] | None = None
    scope_source: Literal["request_context", "interpreted_user_text"] | None = None
    dataset_date: str | None = Field(default=None, pattern=r"^[0-9]{8}$")
    selection: dict[str, Any] | None = None
    status: str | None = None
    requested_outcome: Literal[
        "auto",
        "extract_sync",
        "postprocessing",
        "postprocessing_and_fix",
        "trajectory_fix",
    ] | None = None
    phase: Literal[
        "extract_sync",
        "finish_processing",
        "trajectory_review",
    ] | None = None
    decision_modes: dict[str, str] | None = None
    step_actions: list[str] | None = None
    step_variants: dict[str, str] | None = None
    linked_fix: bool | None = None
    forbidden_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract_shape(self) -> "ExpectedHandoff":
        if self.operation is None:
            if None in (self.request, self.target, self.date, self.response_language):
                raise ValueError("historical handoff expectations require legacy fields")
            return self
        if self.operation == "start":
            if self.scope_source is None or self.dataset_date is None or self.selection is None:
                raise ValueError(
                    "start operation expectations require scope_source, dataset_date, and selection",
                )
            kind = self.selection.get("kind")
            clips = self.selection.get("clips")
            if kind == "all_clips" and set(self.selection) == {"kind"}:
                return self
            if (
                kind == "selected_clips"
                and set(self.selection) == {"kind", "clips"}
                and isinstance(clips, list)
                and clips
                and all(isinstance(item, str) and item for item in clips)
                and len(set(clips)) == len(clips)
            ):
                return self
            raise ValueError("start selection must be all_clips or non-empty selected_clips")
        if self.operation == "submit_plan":
            if (
                self.phase is None
                or self.decision_modes is None
                or self.step_actions is None
                or self.step_variants is None
            ):
                raise ValueError(
                    "submit_plan expectations require phase, decision_modes, "
                    "step_actions, and step_variants",
                )
        return self


class FocusedTaskSetup(StrictModel):
    task_ref: str = "DP-EVAL-FOCUSED"
    status: Literal[
        "active",
        "waiting_user",
        "paused",
        "needs_replan",
        "completed",
    ]
    dataset_date: str = Field(default="20260718", pattern=r"^[0-9]{8}$")
    selection: dict[str, Any] = Field(default_factory=lambda: {"kind": "all_clips"})
    wait_cause: str | None = None
    requested_outcome: Literal[
        "auto",
        "extract_sync",
        "postprocessing",
        "postprocessing_and_fix",
        "trajectory_fix",
    ] = "auto"
    completion_outcome: Literal[
        "extract_sync_completed",
        "postprocessing_completed_fix_pending",
        "trajectory_review_completed",
        "processing_completed_no_fix",
    ] | None = None


class NavigationTaskSetup(StrictModel):
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    selection: dict[str, Any]
    scene_mode: Literal["in", "out"] | None = None
    requested_outcome: Literal[
        "postprocessing",
        "postprocessing_and_fix",
        "trajectory_fix",
    ]
    tool_results: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_selection_and_tools(self) -> "NavigationTaskSetup":
        kind = self.selection.get("kind")
        clips = self.selection.get("clips")
        valid_selection = (
            kind == "all_clips"
            and set(self.selection) == {"kind"}
        ) or (
            kind == "selected_clips"
            and set(self.selection) == {"kind", "clips"}
            and isinstance(clips, list)
            and bool(clips)
            and all(isinstance(item, str) and item for item in clips)
        )
        if not valid_selection:
            raise ValueError("navigation evaluation selection is invalid")
        allowed_tools = {
            "inspect_navigation_raw_metadata_tool",
            "inspect_navigation_sensor_candidates_tool",
            "inspect_navigation_topic_candidates_tool",
            "inspect_navigation_runtime_assets_tool",
            "inspect_navigation_calibration_inventory_tool",
            "inspect_navigation_localization_sources_tool",
            "inspect_navigation_annotation_job_facts_tool",
            "inspect_navigation_artifact_state_tool",
            "inspect_navigation_gridmap_artifacts_tool",
        }
        unexpected = sorted(set(self.tool_results) - allowed_tools)
        if unexpected:
            raise ValueError(
                "navigation evaluation tool_results contains unsupported tools: "
                + ", ".join(unexpected),
            )
        return self


class TrustedRequestContextSetup(StrictModel):
    kind: Literal["navigation_dataset_selection_v1"]
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    selection: dict[str, Any]

    @model_validator(mode="after")
    def validate_selection(self) -> "TrustedRequestContextSetup":
        kind = self.selection.get("kind")
        clips = self.selection.get("clips")
        if kind == "all_clips" and set(self.selection) == {"kind"}:
            return self
        if (
            kind == "selected_clips"
            and set(self.selection) == {"kind", "clips"}
            and isinstance(clips, list)
            and clips
            and all(isinstance(item, str) and item for item in clips)
            and len(set(clips)) == len(clips)
        ):
            return self
        raise ValueError(
            "trusted request context selection must be all_clips or non-empty selected_clips",
        )


class EvaluationRuntimeSetup(StrictModel):
    focused_task: FocusedTaskSetup | None = None
    request_context: TrustedRequestContextSetup | None = None
    navigation_task: NavigationTaskSetup | None = None

    @model_validator(mode="after")
    def validate_exclusive_setup(self) -> "EvaluationRuntimeSetup":
        configured = sum(
            item is not None
            for item in (
                self.focused_task,
                self.request_context,
                self.navigation_task,
            )
        )
        if configured > 1:
            raise ValueError(
                "focused_task, request_context, and navigation_task are exclusive",
            )
        return self


class ToolExpectations(StrictModel):
    allowed_calls: list[str] = Field(default_factory=list)
    required_counts: dict[str, int] = Field(default_factory=dict)
    handoff_count: int = Field(default=0, ge=0)
    handoff: ExpectedHandoff | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> "ToolExpectations":
        if any(count < 0 for count in self.required_counts.values()):
            raise ValueError("required tool counts cannot be negative")
        if self.handoff is not None and self.handoff_count < 1:
            raise ValueError("handoff expectations require handoff_count >= 1")
        return self


class ResponseExpectations(StrictModel):
    language: Literal["Chinese", "English"] | None = None
    allow_empty: bool = False
    required_any_groups: list[list[str]] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    require_question: bool = False
    max_chars: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_groups(self) -> "ResponseExpectations":
        if any(not group or any(not term for term in group) for group in self.required_any_groups):
            raise ValueError("required_any_groups must contain non-empty terms")
        return self


class CaseExpectations(StrictModel):
    tools: ToolExpectations
    response: ResponseExpectations


class EvaluationCase(StrictModel):
    schema_version: Literal[1, 2]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    suite: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    entrypoint: Literal["router", "navigation"]
    tags: list[str] = Field(default_factory=list)
    conversation: list[ConversationTurn] = Field(min_length=1)
    runtime_setup: EvaluationRuntimeSetup | None = None
    limits: CaseLimits = Field(default_factory=CaseLimits)
    expectations: CaseExpectations

    @model_validator(mode="after")
    def validate_conversation(self) -> "EvaluationCase":
        if self.schema_version == 1 and (
            len(self.conversation) != 1 or self.conversation[0].role != "user"
        ):
            raise ValueError(
                "evaluation case schema v1 supports exactly one user conversation turn",
            )
        if self.schema_version == 1 and self.runtime_setup is not None:
            raise ValueError("evaluation case schema v1 does not support runtime_setup")
        if self.schema_version == 2 and any(
            turn.role != "user" for turn in self.conversation
        ):
            raise ValueError(
                "evaluation case schema v2 conversations contain user turns only; "
                "assistant turns are produced by the host",
            )
        if self.entrypoint == "navigation":
            if (
                self.runtime_setup is None
                or self.runtime_setup.navigation_task is None
            ):
                raise ValueError(
                    "navigation evaluation cases require runtime_setup.navigation_task",
                )
        elif (
            self.runtime_setup is not None
            and self.runtime_setup.navigation_task is not None
        ):
            raise ValueError(
                "router evaluation cases cannot seed a navigation_task",
            )
        return self


class ToolCallObservation(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    blocked: bool = False


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class CaseRunObservation(StrictModel):
    final_response: str = ""
    tool_calls: list[ToolCallObservation] = Field(default_factory=list)
    forbidden_calls: list[str] = Field(default_factory=list)
    handoffs: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    visible_tool_sets: list[list[str]] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_calls: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class GradingCheck(StrictModel):
    name: str
    passed: bool
    message: str


class CaseResult(StrictModel):
    case_id: str
    suite: str
    repeat_index: int = Field(default=1, ge=1)
    status: EvaluationStatus
    checks: list[GradingCheck] = Field(default_factory=list)
    observation: CaseRunObservation | None = None
    error_type: str | None = None
    error_message: str | None = None
    metrics: dict[str, int | float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def timeout(
        cls,
        case: EvaluationCase,
        *,
        repeat_index: int = 1,
        message: str = "case exceeded its hard timeout",
        observation: CaseRunObservation | None = None,
    ) -> "CaseResult":
        return cls(
            case_id=case.id,
            suite=case.suite,
            repeat_index=repeat_index,
            status=EvaluationStatus.TIMEOUT,
            observation=observation,
            error_type="TimeoutError",
            error_message=message,
        )

    @classmethod
    def error(
        cls,
        case: EvaluationCase,
        *,
        error: BaseException | str,
        repeat_index: int = 1,
        observation: CaseRunObservation | None = None,
    ) -> "CaseResult":
        return cls(
            case_id=case.id,
            suite=case.suite,
            repeat_index=repeat_index,
            status=EvaluationStatus.ERROR,
            observation=observation,
            error_type=type(error).__name__ if isinstance(error, BaseException) else "EvaluationError",
            error_message=str(error),
        )
