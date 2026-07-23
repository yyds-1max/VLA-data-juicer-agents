from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NumericTolerance(StrictModel):
    """Numeric comparison policy.

    Production case files intentionally default to exact comparison.  A
    non-zero tolerance is supported by the comparator, but must be an explicit
    reviewed change to the case file.
    """

    abs_tol: float = Field(default=0.0, ge=0.0)
    rel_tol: float = Field(default=0.0, ge=0.0)

    @field_validator("abs_tol", "rel_tol")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("numeric tolerances must be finite")
        return value


class ArtifactPatterns(StrictModel):
    images: list[str] = Field(
        default_factory=lambda: ["**/*.jpg", "**/*.jpeg", "**/*.png"],
    )
    gridmaps: list[str] = Field(default_factory=lambda: ["**/grid_map/*.json"])
    trajectories: list[str] = Field(
        default_factory=lambda: ["**/*trajectory*.json"],
    )

    @field_validator("images", "gridmaps", "trajectories")
    @classmethod
    def validate_globs(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or value.startswith(("/", "\\")):
                raise ValueError("artifact globs must be non-empty relative patterns")
            parts = value.replace("\\", "/").split("/")
            if ".." in parts:
                raise ValueError("artifact globs cannot contain '..'")
        return values


class InputExpectation(StrictModel):
    modality: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    relative_pattern: str
    present: bool
    expected_kind: Literal["file", "directory", "any"] = "file"
    file_count: int | None = Field(default=None, ge=0)

    @field_validator("relative_pattern")
    @classmethod
    def validate_relative_pattern(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
        ):
            raise ValueError(
                "input expectation patterns must stay relative to the artifact scope",
            )
        return normalized

    @model_validator(mode="after")
    def validate_file_count(self) -> "InputExpectation":
        if not self.present and self.file_count is not None:
            raise ValueError("an absent input cannot declare a file count")
        if self.file_count is not None and self.expected_kind != "file":
            raise ValueError("file_count requires expected_kind=file")
        return self


class GoldenSample(StrictModel):
    """Commit-safe identity and applicability of one private Golden sample."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    source_clip: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    internal_segment: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    sample_kind: Literal[
        "historical_complete",
        "synchronized_input_missing_gridmap",
        "synthetic_fixture",
    ]
    contamination_risk: Literal[
        "historical_production_read_only",
        "synchronized_input_read_only",
        "synthetic",
    ]
    source_expectations: list[InputExpectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_expectations(self) -> "GoldenSample":
        modalities = [
            expectation.modality for expectation in self.source_expectations
        ]
        if len(set(modalities)) != len(modalities):
            raise ValueError("source expectation modalities must be unique")
        return self


class GoldenCase(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    sample_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    artifact_root_kind: Literal[
        "synthetic",
        "finish_temp_date",
        "finish_date",
        "staged_sync_segment",
    ] = "synthetic"
    artifact_scope: str = "."
    patterns: ArtifactPatterns = Field(default_factory=ArtifactPatterns)
    gridmap_tolerance: NumericTolerance = Field(default_factory=NumericTolerance)
    trajectory_tolerance: NumericTolerance = Field(default_factory=NumericTolerance)
    root_expectations: list[InputExpectation] = Field(default_factory=list)
    ignored_artifact_patterns: list[str] = Field(default_factory=list)
    applicable_stages: list[str] = Field(default_factory=list)
    excluded_stages: list[str] = Field(default_factory=list)
    expected_command_steps: list[str] = Field(default_factory=list)

    @field_validator("artifact_scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("artifact_scope must stay relative to the supplied root")
        return normalized or "."

    @field_validator("ignored_artifact_patterns")
    @classmethod
    def validate_ignored_artifact_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            normalized = value.replace("\\", "/")
            if (
                not normalized
                or normalized.startswith("/")
                or ".." in normalized.split("/")
            ):
                raise ValueError(
                    "ignored artifact patterns must stay relative to the supplied root",
                )
        if len(set(values)) != len(values):
            raise ValueError("ignored artifact patterns must be unique")
        return values

    @field_validator(
        "applicable_stages",
        "excluded_stages",
        "expected_command_steps",
    )
    @classmethod
    def validate_stage_ids(cls, values: list[str]) -> list[str]:
        import re

        for value in values:
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
                raise ValueError("stage IDs must be normalized opaque identifiers")
        if len(set(values)) != len(values):
            raise ValueError("stage IDs must be unique")
        return values

    @model_validator(mode="after")
    def validate_stage_scope(self) -> "GoldenCase":
        overlap = set(self.applicable_stages) & set(self.excluded_stages)
        if overlap:
            raise ValueError("applicable and excluded stages must not overlap")
        modalities = [
            expectation.modality for expectation in self.root_expectations
        ]
        if len(set(modalities)) != len(modalities):
            raise ValueError("root expectation modalities must be unique")
        return self


class GoldenCaseBundle(StrictModel):
    schema_version: Literal[1]
    runtime_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    samples: list[GoldenSample] = Field(default_factory=list)
    cases: list[GoldenCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> "GoldenCaseBundle":
        sample_ids = [sample.id for sample in self.samples]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("Golden sample IDs must be unique")
        identifiers = [case.id for case in self.cases]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("golden case IDs must be unique")
        known_samples = set(sample_ids)
        unknown_samples = sorted(
            {
                case.sample_id
                for case in self.cases
                if case.sample_id is not None
                and case.sample_id not in known_samples
            },
        )
        if unknown_samples:
            raise ValueError(
                "Golden cases reference unknown sample IDs: "
                + ", ".join(unknown_samples),
            )
        return self


class ImageFingerprint(StrictModel):
    format: Literal["jpeg", "png"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class DocumentFingerprint(StrictModel):
    root_type: str
    shape_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    numeric_leaf_count: int = Field(ge=0)


class NumericFingerprint(StrictModel):
    count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GoldenEntry(StrictModel):
    relative_path: str
    kind: Literal["directory", "file"]
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    semantic_type: Literal[
        "directory",
        "binary",
        "image",
        "json",
        "yaml",
        "gridmap",
        "trajectory",
    ]
    image: ImageFingerprint | None = None
    document: DocumentFingerprint | None = None
    numeric: NumericFingerprint | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self) -> "GoldenEntry":
        if self.kind == "directory":
            if self.semantic_type != "directory" or any(
                value is not None
                for value in (self.size, self.sha256, self.image, self.document, self.numeric)
            ):
                raise ValueError("directory entries cannot contain file fingerprints")
            return self
        if self.size is None or self.sha256 is None or self.semantic_type == "directory":
            raise ValueError("file entries require size, hash, and a file semantic type")
        return self


class GoldenSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    case_id: str
    role: Literal["legacy", "candidate"]
    runtime_id: str
    runtime_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    command_sequence_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: list[GoldenEntry]


class GoldenDifference(StrictModel):
    code: str
    relative_path: str | None = None
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class GoldenComparison(StrictModel):
    schema_version: Literal[1] = 1
    case_id: str
    runtime_id: str
    verdict: Literal["EQUIVALENT", "DIFFERENT"]
    byte_identity: bool
    business_equivalence: bool
    baseline_tree_sha256: str
    candidate_tree_sha256: str
    difference_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    differences: list[GoldenDifference]
    warnings: list[GoldenDifference]
