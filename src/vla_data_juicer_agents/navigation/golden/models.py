from __future__ import annotations

import math
from pathlib import Path
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


class GoldenRoleScope(StrictModel):
    """Role-specific identity and artifact scope for paired 2026/2027 runs."""

    scope_kind: Literal[
        "segment",
        "prepare_maps",
        "prepare_metadata",
    ] = "segment"
    artifact_scope: str = "."
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    source_clip: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    internal_segment: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    provenance: Literal[
        "historical_unattested",
        "runtime_attested",
        "synthetic",
    ]

    @field_validator("artifact_scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("role artifact scope must stay relative to the supplied root")
        return normalized or "."

    @model_validator(mode="after")
    def validate_scope_identity(self) -> "GoldenRoleScope":
        if self.scope_kind == "segment":
            if self.internal_segment is None:
                raise ValueError("segment role scope requires internal_segment")
            return self
        expected_scope = {
            "prepare_maps": "maps",
            "prepare_metadata": "v1.0-trainval",
        }[self.scope_kind]
        if self.internal_segment is not None:
            raise ValueError("prepare-global role scope cannot name a segment")
        if self.artifact_scope != expected_scope:
            raise ValueError(
                f"{self.scope_kind} role scope must be exactly {expected_scope!r}",
            )
        return self


class GoldenRoleScopes(StrictModel):
    legacy: GoldenRoleScope
    candidate: GoldenRoleScope


class DocumentNormalization(StrictModel):
    """One narrow, reviewed non-business document normalization."""

    path_pattern: str
    selector: Literal['$["paths"]["img2video_mp4"]']
    strategy: Literal["artifact_local_file"]
    expected_relative_path: Literal["dog.mp4"]

    @field_validator("path_pattern")
    @classmethod
    def validate_path_pattern(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
        ):
            raise ValueError(
                "document normalization path patterns must stay relative",
            )
        return normalized


class ArtifactStageRule(StrictModel):
    path_pattern: str
    stage: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")

    @field_validator("path_pattern")
    @classmethod
    def validate_path_pattern(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
        ):
            raise ValueError("artifact stage patterns must stay relative")
        return normalized


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
    role_scopes: GoldenRoleScopes | None = None
    patterns: ArtifactPatterns = Field(default_factory=ArtifactPatterns)
    gridmap_tolerance: NumericTolerance = Field(default_factory=NumericTolerance)
    trajectory_tolerance: NumericTolerance = Field(default_factory=NumericTolerance)
    root_expectations: list[InputExpectation] = Field(default_factory=list)
    ignored_artifact_patterns: list[str] = Field(default_factory=list)
    applicable_stages: list[str] = Field(default_factory=list)
    excluded_stages: list[str] = Field(default_factory=list)
    expected_command_steps: list[str] = Field(default_factory=list)
    document_normalizations: list[DocumentNormalization] = Field(
        default_factory=list,
    )
    dimensions_only_image_patterns: list[str] = Field(default_factory=list)
    artifact_stage_rules: list[ArtifactStageRule] = Field(default_factory=list)
    candidate_attestation_required: bool = False
    legacy_oracle_selection_required: bool = False

    @field_validator("artifact_scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("artifact_scope must stay relative to the supplied root")
        return normalized or "."

    @field_validator(
        "ignored_artifact_patterns",
        "dimensions_only_image_patterns",
    )
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
        normalization_keys = [
            (item.path_pattern, item.selector)
            for item in self.document_normalizations
        ]
        if len(set(normalization_keys)) != len(normalization_keys):
            raise ValueError("document normalizations must be unique")
        stage_patterns = [item.path_pattern for item in self.artifact_stage_rules]
        if len(set(stage_patterns)) != len(stage_patterns):
            raise ValueError("artifact stage patterns must be unique")
        return self


class GoldenCaseBundle(StrictModel):
    schema_version: Literal[1, 2]
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
        if self.schema_version == 1:
            v2_cases = [
                case.id
                for case in self.cases
                if case.role_scopes is not None
                or case.document_normalizations
                or case.dimensions_only_image_patterns
                or case.artifact_stage_rules
                or case.candidate_attestation_required
                or case.legacy_oracle_selection_required
            ]
            if v2_cases:
                raise ValueError(
                    "Golden schema v1 cannot declare v2 comparison policies",
                )
        for case in self.cases:
            if case.role_scopes is not None:
                legacy_scope = case.role_scopes.legacy
                candidate_scope = case.role_scopes.candidate
                if legacy_scope.scope_kind != candidate_scope.scope_kind:
                    raise ValueError(
                        "Golden role scope kinds must match",
                    )
                if (
                    legacy_scope.scope_kind != "segment"
                    and case.artifact_root_kind != "finish_temp_date"
                ):
                    raise ValueError(
                        "prepare-global scopes require finish_temp_date roots",
                    )
            if case.candidate_attestation_required and case.role_scopes is None:
                raise ValueError(
                    "candidate attestation requires role-specific scopes",
                )
            if (
                case.role_scopes is not None
                and case.role_scopes.candidate.provenance != "runtime_attested"
                and case.candidate_attestation_required
            ):
                raise ValueError(
                    "candidate attestation requires runtime_attested provenance",
                )
            if self.schema_version == 2:
                if case.ignored_artifact_patterns:
                    raise ValueError(
                        "Golden schema v2 forbids ignored artifact patterns",
                    )
                if (
                    case.gridmap_tolerance.abs_tol != 0
                    or case.gridmap_tolerance.rel_tol != 0
                    or case.trajectory_tolerance.abs_tol != 0
                    or case.trajectory_tolerance.rel_tol != 0
                ):
                    raise ValueError(
                        "Golden schema v2 requires exact numeric comparison",
                    )
                if any(
                    pattern != "tracking_img_*/*"
                    for pattern in case.dimensions_only_image_patterns
                ):
                    raise ValueError(
                        "Golden schema v2 only permits the registered "
                        "Tracking dynamic-image policy",
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
    normalized_representation_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_kind_payload(self) -> "GoldenEntry":
        if self.kind == "directory":
            if self.semantic_type != "directory" or any(
                value is not None
                for value in (
                    self.size,
                    self.sha256,
                    self.image,
                    self.document,
                    self.numeric,
                    self.normalized_representation_sha256,
                )
            ):
                raise ValueError("directory entries cannot contain file fingerprints")
            return self
        if self.size is None or self.sha256 is None or self.semantic_type == "directory":
            raise ValueError("file entries require size, hash, and a file semantic type")
        return self


class GoldenSnapshot(StrictModel):
    schema_version: Literal[1, 2] = 1
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
    calibration_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    annotation_revision_set_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runtime_run_ref: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$",
    )
    provenance: Literal[
        "historical_unattested",
        "runtime_attested",
        "synthetic",
    ] | None = None
    oracle_ref: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$",
    )
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: list[GoldenEntry]


class GoldenDifference(StrictModel):
    code: str
    relative_path: str | None = None
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class GoldenComparison(StrictModel):
    schema_version: Literal[1, 2] = 1
    case_id: str
    runtime_id: str
    verdict: Literal["EQUIVALENT", "DIFFERENT"]
    byte_identity: bool
    business_equivalence: bool
    baseline_tree_sha256: str
    candidate_tree_sha256: str
    candidate_run_ref: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$",
    )
    oracle_ref: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$",
    )
    runtime_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    calibration_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    annotation_revision_set_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    difference_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    differences: list[GoldenDifference]
    warnings: list[GoldenDifference]


class RuntimeRunAttestation(StrictModel):
    """Safe projection of a committed RuntimeRun execution ledger."""

    source: Literal["runtime_run"]
    run_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
    committed: Literal[True]
    runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibration_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    annotation_revision_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_steps: list[str] = Field(min_length=1)

    @field_validator("command_steps")
    @classmethod
    def validate_command_steps(cls, values: list[str]) -> list[str]:
        import re

        if len(set(values)) != len(values):
            raise ValueError("attested command steps must be unique")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_-]*", value)
            for value in values
        ):
            raise ValueError("attested command steps must be normalized IDs")
        return values


class StoreBoundSegment(StrictModel):
    source_clip: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    internal_segment: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    artifact_scope: str

    @field_validator("artifact_scope")
    @classmethod
    def validate_artifact_scope(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
        ):
            raise ValueError("bound segment scope must stay relative to staging")
        return normalized


class StoreBoundCandidate(StrictModel):
    """Private, process-local projection binding a case to Store-owned artifacts.

    ``staging_root`` and ``artifact_scope`` are deliberately not copied into
    ``GoldenComparison``.  They exist only long enough for the comparator to
    open the committed candidate tree.
    """

    source: Literal["annotation_store"]
    run_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    source_clips: list[str] = Field(min_length=1)
    source_clip: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    scope_kind: Literal[
        "segment",
        "prepare_maps",
        "prepare_metadata",
    ] = "segment"
    internal_segment: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    staging_root: Path
    artifact_scope: str
    segments: list[StoreBoundSegment] = Field(min_length=1)
    attestation: RuntimeRunAttestation

    @field_validator("source_clips")
    @classmethod
    def validate_source_clips(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("bound source clips must be unique")
        if any(
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            for value in values
        ):
            raise ValueError("bound source clips must be safe components")
        return values

    @field_validator("artifact_scope")
    @classmethod
    def validate_artifact_scope(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
        ):
            raise ValueError("bound artifact scope must stay relative to staging")
        return normalized

    @field_validator("staging_root")
    @classmethod
    def validate_staging_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("bound staging root must be absolute")
        return value

    @model_validator(mode="after")
    def validate_binding_identity(self) -> "StoreBoundCandidate":
        if self.attestation.run_ref != self.run_ref:
            raise ValueError("bound RuntimeRun identity mismatch")
        if self.source_clip not in self.source_clips:
            raise ValueError("bound source clip is not selected by the job")
        segment_keys = [
            segment.internal_segment for segment in self.segments
        ]
        segment_scopes = [
            segment.artifact_scope for segment in self.segments
        ]
        if (
            len(segment_keys) != len(set(segment_keys))
            or len(segment_scopes) != len(set(segment_scopes))
            or any(
                segment.source_clip not in self.source_clips
                for segment in self.segments
            )
        ):
            raise ValueError("bound segment mapping is inconsistent")
        if self.scope_kind == "segment":
            if self.internal_segment is None:
                raise ValueError("bound segment scope requires an internal segment")
            if self.artifact_scope.replace("\\", "/").split("/")[-1] != (
                self.internal_segment
            ):
                raise ValueError("bound artifact scope does not name the segment")
            selected = [
                segment
                for segment in self.segments
                if segment.internal_segment == self.internal_segment
            ]
            if (
                len(selected) != 1
                or selected[0].source_clip != self.source_clip
                or selected[0].artifact_scope != self.artifact_scope
            ):
                raise ValueError("selected segment is not in the Store segment mapping")
            return self
        expected_scope = {
            "prepare_maps": "maps",
            "prepare_metadata": "v1.0-trainval",
        }[self.scope_kind]
        if self.internal_segment is not None:
            raise ValueError("bound prepare-global scope cannot name a segment")
        if self.artifact_scope != expected_scope:
            raise ValueError(
                "bound prepare-global scope does not match its registered kind",
            )
        return self
