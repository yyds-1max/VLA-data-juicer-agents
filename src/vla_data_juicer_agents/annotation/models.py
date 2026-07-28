from __future__ import annotations

from math import isfinite
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


AnnotationJobStatus = Literal[
    "preparing",
    "waiting_initial_annotation",
    "tracking",
    "tracked",
    "postprocessing",
    "annotated",
    "failed",
    "cancelled",
]
AnnotationSegmentStatus = Literal[
    "pending_initial_annotation",
    "draft",
    "submitted",
    "skipped",
    "tracking",
    "tracked",
    "postprocessing",
    "annotated",
    "postprocessing_failed",
]
RuntimeRunKind = Literal[
    "prepare",
    "tracking",
    "postprocessing",
    "fix",
    "compatibility_publish",
]
GridmapDecision = Literal[
    "copy_existing_gridmap",
    "generate_from_pcd",
    "skip_if_projection_ready",
]
TrajectoryVariant = Literal["cjl_with_gridmap", "cjl_0525_with_gridmap"]
TrajectoryReviewStatus = Literal[
    "pending",
    "in_progress",
    "returned",
    "approved",
    "discarded",
]

COLOR_VALUES = (
    "black",
    "white",
    "gray",
    "red",
    "yellow",
    "blue",
    "green",
    "pink",
    "purple",
    "brown",
    "orange",
    "camouflage",
    "beige",
    "khaki",
)
ColorValue = Literal[
    "black",
    "white",
    "gray",
    "red",
    "yellow",
    "blue",
    "green",
    "pink",
    "purple",
    "brown",
    "orange",
    "camouflage",
    "beige",
    "khaki",
]
StrictRevision = Annotated[int, Field(strict=True, ge=0)]


class AnnotationConflictError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        current: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current = current


class AnnotationNotFoundError(KeyError):
    pass


class AnnotationValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalibrationSelection(StrictModel):
    profile_ref: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DraftAnnotationTarget(StrictModel):
    target_ref: str = Field(pattern=r"^target_[0-9a-f]{32}$")
    bbox: tuple[StrictInt, StrictInt, StrictInt, StrictInt] | None = None
    point: tuple[StrictInt, StrictInt] | None = None
    colors: dict[Literal["upper", "lower", "shoes"], ColorValue | None] = Field(
        default_factory=lambda: {"upper": None, "lower": None, "shoes": None}
    )

    @field_validator("colors")
    @classmethod
    def require_all_colors(
        cls,
        value: dict[Literal["upper", "lower", "shoes"], ColorValue | None],
    ) -> dict[Literal["upper", "lower", "shoes"], ColorValue | None]:
        if set(value) != {"upper", "lower", "shoes"}:
            raise ValueError("upper, lower, and shoes colors are required")
        return value


class CreateAnnotationJobRequest(StrictModel):
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    source_clips: list[str] = Field(min_length=1, max_length=200)
    calibration_profile_ref: str = Field(min_length=1, max_length=100)
    calibration_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_clips")
    @classmethod
    def validate_source_clips(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(
            not item
            or item in {".", ".."}
            or "/" in item
            or "\\" in item
            or "\r" in item
            or "\n" in item
            or len(item) > 200
            for item in normalized
        ):
            raise ValueError("source clips must be safe path components")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source clips must be unique")
        return normalized


class ExpectedJobRevisionRequest(StrictModel):
    expected_job_revision: StrictRevision


class PostprocessingSpecInput(StrictModel):
    localization_kind: Literal["odom", "ins"]
    gridmap_decision: GridmapDecision
    trajectory_variant: TrajectoryVariant
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_variant_for_localization(self) -> "PostprocessingSpecInput":
        expected = {
            "odom": "cjl_0525_with_gridmap",
            "ins": "cjl_with_gridmap",
        }[self.localization_kind]
        if self.trajectory_variant != expected:
            raise ValueError(
                "trajectory_variant is incompatible with localization_kind"
            )
        return self


class DraftRequest(StrictModel):
    expected_segment_revision: StrictRevision
    expected_draft_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    targets: list[DraftAnnotationTarget] = Field(max_length=100)

    @field_validator("targets")
    @classmethod
    def target_refs_unique(
        cls,
        value: list[DraftAnnotationTarget],
    ) -> list[DraftAnnotationTarget]:
        refs = [target.target_ref for target in value]
        if len(refs) != len(set(refs)):
            raise ValueError("target_ref values must be unique")
        return value


class SegmentRevisionRequest(StrictModel):
    expected_segment_revision: StrictRevision


class SubmitRequest(SegmentRevisionRequest):
    expected_draft_revision: StrictRevision


class SkipRequest(SegmentRevisionRequest):
    reason_code: Literal["no_valid_target", "unusable_first_frame", "other"]
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_other_note(self) -> "SkipRequest":
        normalized = self.note.strip() if self.note else None
        if self.reason_code == "other" and not normalized:
            raise ValueError("note is required when reason_code is other")
        self.note = normalized
        return self


class PreparedSegment(BaseModel):
    source_clip: str
    private_segment_key: str
    segment_root: str
    first_frame_path: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    etag: str = Field(min_length=1)


class RuntimeCapabilities(BaseModel):
    available: bool
    runtime_id: str = "navigation_odom_v1"
    reason: dict[str, str] | None = None


FiniteFloat = Annotated[float, Field(strict=True)]
TrajectoryTargetRef = Annotated[
    str,
    Field(pattern=r"^target_[0-9a-f]{32}$"),
]


def _require_finite(value: float) -> float:
    if not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


class SetPositionCommand(StrictModel):
    kind: Literal["set_position"]
    frame_index: Annotated[int, Field(strict=True, ge=0)]
    target_ref: TrajectoryTargetRef
    x: FiniteFloat
    y: FiniteFloat

    _finite_x = field_validator("x")(_require_finite)
    _finite_y = field_validator("y")(_require_finite)


class SetDirectionCommand(StrictModel):
    kind: Literal["set_direction"]
    frame_index: Annotated[int, Field(strict=True, ge=0)]
    target_ref: TrajectoryTargetRef
    direction: FiniteFloat

    _finite_direction = field_validator("direction")(_require_finite)


class SetSpeedCommand(StrictModel):
    kind: Literal["set_speed"]
    frame_index: Annotated[int, Field(strict=True, ge=0)]
    target_ref: TrajectoryTargetRef
    speed: Annotated[float, Field(strict=True, ge=0)]

    _finite_speed = field_validator("speed")(_require_finite)


class DeleteTargetCommand(StrictModel):
    kind: Literal["delete_target"]
    frame_index: Annotated[int, Field(strict=True, ge=0)]
    target_ref: TrajectoryTargetRef


class AddMissingTargetCommand(StrictModel):
    kind: Literal["add_missing_target"]
    frame_index: Annotated[int, Field(strict=True, ge=1)]
    target_ref: TrajectoryTargetRef


class RestoreFrameCommand(StrictModel):
    kind: Literal["restore_frame"]
    frame_index: Annotated[int, Field(strict=True, ge=0)]


class TogglePassCommand(StrictModel):
    kind: Literal["toggle_pass"]
    frame_index: Annotated[int, Field(strict=True, ge=0)]
    value: bool


FixCommand = Annotated[
    SetPositionCommand
    | SetDirectionCommand
    | SetSpeedCommand
    | DeleteTargetCommand
    | AddMissingTargetCommand
    | RestoreFrameCommand
    | TogglePassCommand,
    Field(discriminator="kind"),
]


class CreateFixSessionRequest(StrictModel):
    expected_review_revision: StrictRevision
    calibration_profile_ref: str = Field(min_length=1, max_length=100)
    calibration_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibration_difference_reason: str | None = Field(
        default=None,
        max_length=1000,
    )

    @field_validator("calibration_difference_reason")
    @classmethod
    def normalize_difference_reason(cls, value: str | None) -> str | None:
        normalized = value.strip() if value else None
        return normalized or None


class ApplyFixCommandRequest(StrictModel):
    expected_review_revision: StrictRevision
    expected_draft_revision: Annotated[int, Field(strict=True, ge=1)]
    command: FixCommand


class CreateFixRevisionRequest(StrictModel):
    expected_review_revision: StrictRevision
    expected_draft_revision: Annotated[int, Field(strict=True, ge=1)]


class ApproveReviewRequest(StrictModel):
    expected_review_revision: StrictRevision
    fix_revision_ref: str = Field(pattern=r"^fix_revision_[0-9a-f]{32}$")


class ReturnReviewRequest(StrictModel):
    expected_review_revision: StrictRevision
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason cannot be blank")
        return normalized


class DiscardReviewRequest(ReturnReviewRequest):
    pass


class RetryPublicationRequest(StrictModel):
    expected_review_revision: StrictRevision


class FixRuntimeState(StrictModel):
    """Opaque deterministic state returned by the frozen Fix adapter.

    The application layer persists the state and its hash, but never interprets
    trajectory numbers.  The production adapter remains responsible for the
    legacy numerical semantics.
    """

    state: dict[str, Any]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FixRuntimeAdapter(Protocol):
    def initialize(
        self,
        trajectory_state: dict[str, Any],
        *,
        calibration_snapshot: dict[str, Any],
    ) -> FixRuntimeState: ...

    def apply(
        self,
        current_state: dict[str, Any],
        command: FixCommand,
    ) -> FixRuntimeState: ...


class CompatibilityPublicationResult(StrictModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_artifact_path: str = Field(min_length=1)


class CompatibilityPublisher(Protocol):
    def publish(
        self,
        state: dict[str, Any],
        *,
        review_ref: str,
        fix_revision_ref: str,
    ) -> CompatibilityPublicationResult: ...
