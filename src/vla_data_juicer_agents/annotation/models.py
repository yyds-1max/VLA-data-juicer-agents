from __future__ import annotations

from typing import Annotated, Any, Literal

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
