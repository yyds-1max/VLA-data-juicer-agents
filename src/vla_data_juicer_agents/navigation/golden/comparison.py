from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .models import (
    GoldenCase,
    GoldenCaseBundle,
    GoldenComparison,
    GoldenDifference,
    GoldenEntry,
    NumericTolerance,
    RuntimeRunAttestation,
    StoreBoundCandidate,
)
from .snapshot import (
    GoldenError,
    _normalized_document,
    _safe_scope,
    _capture_snapshot,
    load_document_for_type,
)


MAX_REPORTED_ITEMS = 100


def _node_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _selector(parent: str, child: str | int) -> str:
    if isinstance(child, int):
        return f"{parent}[{child}]"
    escaped = child.replace("\\", "\\\\").replace('"', '\\"')
    return f'{parent}["{escaped}"]'


@dataclass
class _StructuralDifferences:
    count: int = 0
    first_selector: str | None = None
    first_reason: str | None = None
    first_baseline: str | int | float | bool | None = None
    first_candidate: str | int | float | bool | None = None

    def add(
        self,
        selector: str,
        reason: str,
        baseline: Any = None,
        candidate: Any = None,
    ) -> None:
        self.count += 1
        if self.first_selector is None:
            self.first_selector = selector
            self.first_reason = reason
            self.first_baseline = _safe_scalar_projection(baseline)
            self.first_candidate = _safe_scalar_projection(candidate)


@dataclass
class _NumericDifferences:
    count: int = 0
    first_selector: str | None = None
    max_abs_diff: float = 0.0
    max_rel_diff: float = 0.0
    first_baseline: int | float | None = None
    first_candidate: int | float | None = None

    def add(self, selector: str, baseline: int | float, candidate: int | float) -> None:
        absolute = abs(baseline - candidate)
        denominator = max(abs(baseline), abs(candidate), 1e-300)
        relative = absolute / denominator
        self.count += 1
        self.first_selector = self.first_selector or selector
        if self.first_baseline is None:
            self.first_baseline = baseline
            self.first_candidate = candidate
        self.max_abs_diff = max(self.max_abs_diff, float(absolute))
        self.max_rel_diff = max(self.max_rel_diff, float(relative))


def _compare_document(
    baseline: Any,
    candidate: Any,
    *,
    tolerance: NumericTolerance,
) -> tuple[_StructuralDifferences, _NumericDifferences]:
    structural = _StructuralDifferences()
    numeric = _NumericDifferences()

    def visit(left: Any, right: Any, selector: str) -> None:
        left_type = _node_type(left)
        right_type = _node_type(right)
        if left_type != right_type:
            structural.add(
                selector,
                f"type:{left_type}->{right_type}",
                left_type,
                right_type,
            )
            return
        if left_type == "object":
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys):
                structural.add(
                    _selector(selector, key),
                    "missing_key",
                    left[key],
                    None,
                )
            for key in sorted(right_keys - left_keys):
                structural.add(
                    _selector(selector, key),
                    "extra_key",
                    None,
                    right[key],
                )
            for key in sorted(left_keys & right_keys):
                visit(left[key], right[key], _selector(selector, key))
            return
        if left_type == "array":
            if len(left) != len(right):
                structural.add(
                    selector,
                    f"length:{len(left)}->{len(right)}",
                    len(left),
                    len(right),
                )
                return
            for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
                visit(left_item, right_item, _selector(selector, index))
            return
        if left_type in {"int", "float"}:
            if not math.isclose(
                left,
                right,
                rel_tol=tolerance.rel_tol,
                abs_tol=tolerance.abs_tol,
            ):
                numeric.add(selector, left, right)
            return
        if left != right:
            structural.add(selector, "scalar_mismatch", left, right)

    visit(baseline, candidate, "$")
    return structural, numeric


def _entry_map(entries: list[GoldenEntry]) -> dict[str, GoldenEntry]:
    return {entry.relative_path: entry for entry in entries}


def _safe_scalar_projection(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and re_full_safe_scalar(value):
        return value
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        serialized = type(value).__name__
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def re_full_safe_scalar(value: str) -> bool:
    return (
        0 < len(value) <= 128
        and "/" not in value
        and "\\" not in value
        and all(character.isprintable() for character in value)
    )


def _append(
    destination: list[GoldenDifference],
    *,
    code: str,
    relative_path: str | None = None,
    detail: dict[str, str | int | float | bool | None] | None = None,
) -> None:
    destination.append(
        GoldenDifference(
            code=code,
            relative_path=relative_path,
            detail=detail or {},
        ),
    )


def _artifact_path(
    root: Path,
    case: GoldenCase,
    relative_path: str,
    *,
    role: str,
    bound_artifact_scope: str | None = None,
) -> Path:
    root = root.resolve(strict=True)
    if bound_artifact_scope is not None:
        if role != "candidate":
            raise GoldenError("only candidate artifacts can use a bound scope")
        scope = bound_artifact_scope
    else:
        scope = (
            case.artifact_scope
            if case.role_scopes is None
            else (
                case.role_scopes.legacy.artifact_scope
                if role == "legacy"
                else case.role_scopes.candidate.artifact_scope
            )
        )
    return root / scope / relative_path


def _matches(relative_path: str, patterns: list[str]) -> bool:
    candidate = PurePosixPath(relative_path)
    return any(
        candidate.match(pattern)
        or (pattern.startswith("**/") and candidate.match(pattern[3:]))
        for pattern in patterns
    )


def _stage_for(
    case: GoldenCase,
    relative_path: str | None,
    code: str,
) -> str:
    if code.startswith("command_") or relative_path is None:
        return "orchestration"
    for rule in case.artifact_stage_rules:
        if _matches(relative_path, [rule.path_pattern]):
            return rule.stage
    if relative_path.endswith((".yaml", ".yml")):
        return "initial_annotation"
    if (
        "tracking_img_" in relative_path
        or PurePosixPath(relative_path).name.startswith("img_")
    ):
        return "tracking"
    if "grid_map/" in relative_path:
        return "gridmap_prepare"
    if "trajectory" in relative_path:
        return "trajectory"
    return case.applicable_stages[0] if case.applicable_stages else "unknown"


def _suspected_cause(code: str) -> str:
    if code in {"missing_artifact", "extra_artifact"}:
        return "pipeline_output_set_or_scope_drift"
    if code == "image_dimensions_mismatch":
        return "resize_policy_or_input_dimension_drift"
    if code == "image_content_hash_mismatch":
        return "visual_business_output_drift"
    if code == "content_hash_mismatch":
        return "business_output_byte_drift"
    if "numeric_mismatch" in code:
        return "algorithm_calibration_or_runtime_numeric_drift"
    if "structure_mismatch" in code or "semantics_mismatch" in code:
        return "document_schema_field_or_value_drift"
    if code == "document_representation_mismatch":
        return "non_whitelisted_document_representation_drift"
    if code == "command_sequence_mismatch":
        return "runtime_execution_order_or_ledger_drift"
    return "runtime_input_or_representation_drift"


def _enrich_differences(
    values: list[GoldenDifference],
    *,
    case: GoldenCase,
) -> None:
    for value in values:
        value.detail.setdefault(
            "stage",
            _stage_for(case, value.relative_path, value.code),
        )
        value.detail.setdefault(
            "suspected_cause",
            _suspected_cause(value.code),
        )


def _compare_roots(
    *,
    baseline_root: Path,
    candidate_root: Path,
    bundle: GoldenCaseBundle,
    case: GoldenCase,
    baseline_command_steps: list[str] | None = None,
    candidate_command_steps: list[str] | None = None,
    runtime_manifest_sha256: str | None = None,
    source_root: Path | None = None,
    baseline_source_root: Path | None = None,
    candidate_source_root: Path | None = None,
    candidate_attestation: RuntimeRunAttestation | None = None,
    baseline_oracle_ref: str | None = None,
    candidate_bound_artifact_scope: str | None = None,
) -> GoldenComparison:
    baseline = _capture_snapshot(
        root=baseline_root,
        bundle=bundle,
        case=case,
        role="legacy",
        runtime_manifest_sha256=runtime_manifest_sha256,
        command_steps=baseline_command_steps,
        source_root=baseline_source_root or source_root,
        oracle_ref=baseline_oracle_ref,
    )
    candidate = _capture_snapshot(
        root=candidate_root,
        bundle=bundle,
        case=case,
        role="candidate",
        runtime_manifest_sha256=runtime_manifest_sha256,
        command_steps=candidate_command_steps,
        source_root=candidate_source_root or source_root,
        attestation=candidate_attestation,
        bound_artifact_scope=candidate_bound_artifact_scope,
    )
    differences: list[GoldenDifference] = []
    warnings: list[GoldenDifference] = []

    expected_steps = case.expected_command_steps
    if expected_steps:
        from .snapshot import command_sequence_sha256

        expected_hash = command_sequence_sha256(expected_steps)
        role_hashes = [("candidate", candidate.command_sequence_sha256)]
        if not (
            case.role_scopes is not None
            and case.role_scopes.legacy.provenance == "historical_unattested"
        ):
            role_hashes.insert(
                0,
                ("legacy", baseline.command_sequence_sha256),
            )
        for role, actual_hash in role_hashes:
            if actual_hash != expected_hash:
                _append(
                    differences,
                    code="command_sequence_mismatch",
                    detail={"role": role},
                )

    left_entries = _entry_map(baseline.entries)
    right_entries = _entry_map(candidate.entries)
    for relative_path in sorted(left_entries.keys() - right_entries.keys()):
        left = left_entries[relative_path]
        _append(
            differences,
            code="missing_artifact",
            relative_path=relative_path,
            detail={
                "semantic_type": left.semantic_type,
                "baseline_sha256": left.sha256,
            },
        )
    for relative_path in sorted(right_entries.keys() - left_entries.keys()):
        right = right_entries[relative_path]
        _append(
            differences,
            code="extra_artifact",
            relative_path=relative_path,
            detail={
                "semantic_type": right.semantic_type,
                "candidate_sha256": right.sha256,
            },
        )

    for relative_path in sorted(left_entries.keys() & right_entries.keys()):
        left = left_entries[relative_path]
        right = right_entries[relative_path]
        if left.kind != right.kind or left.semantic_type != right.semantic_type:
            _append(
                differences,
                code="artifact_type_mismatch",
                relative_path=relative_path,
                detail={
                    "baseline_type": left.semantic_type,
                    "candidate_type": right.semantic_type,
                },
            )
            continue
        if left.kind == "directory":
            continue

        kind = left.semantic_type
        if kind == "binary":
            if left.sha256 != right.sha256:
                _append(
                    differences,
                    code="content_hash_mismatch",
                    relative_path=relative_path,
                    detail={
                        "semantic_type": kind,
                        "baseline_sha256": left.sha256,
                        "candidate_sha256": right.sha256,
                    },
                )
            continue
        if kind == "image":
            if left.image != right.image:
                _append(
                    differences,
                    code="image_dimensions_mismatch",
                    relative_path=relative_path,
                    detail={
                        "baseline_width": left.image.width if left.image else None,
                        "baseline_height": left.image.height if left.image else None,
                        "candidate_width": right.image.width if right.image else None,
                        "candidate_height": right.image.height if right.image else None,
                    },
                )
            dimensions_only = _matches(
                relative_path,
                case.dimensions_only_image_patterns,
            )
            if left.sha256 != right.sha256 and not dimensions_only:
                _append(
                    differences,
                    code="image_content_hash_mismatch",
                    relative_path=relative_path,
                    detail={
                        "baseline_sha256": left.sha256,
                        "candidate_sha256": right.sha256,
                    },
                )
            continue

        if kind in {"json", "yaml"}:
            left_path = _artifact_path(
                baseline_root,
                case,
                relative_path,
                role="legacy",
            )
            right_path = _artifact_path(
                candidate_root,
                case,
                relative_path,
                role="candidate",
                bound_artifact_scope=candidate_bound_artifact_scope,
            )
            left_value = _normalized_document(
                value=load_document_for_type(left_path, kind),
                relative_path=relative_path,
                scope=_safe_scope(baseline_root, case, role="legacy"),
                case=case,
            )
            right_value = _normalized_document(
                value=load_document_for_type(right_path, kind),
                relative_path=relative_path,
                scope=_safe_scope(
                    candidate_root,
                    case,
                    role="candidate",
                    bound_artifact_scope=candidate_bound_artifact_scope,
                ),
                case=case,
            )
            structural, numeric = _compare_document(
                left_value,
                right_value,
                tolerance=NumericTolerance(),
            )
            if structural.count:
                _append(
                    differences,
                    code="document_semantics_mismatch",
                    relative_path=relative_path,
                    detail={
                        "mismatch_count": structural.count,
                        "first_selector": structural.first_selector,
                        "first_reason": structural.first_reason,
                        "baseline_value": structural.first_baseline,
                        "candidate_value": structural.first_candidate,
                    },
                )
            if numeric.count:
                _append(
                    differences,
                    code="document_numeric_mismatch",
                    relative_path=relative_path,
                    detail={
                        "mismatch_count": numeric.count,
                        "first_selector": numeric.first_selector,
                        "baseline_value": numeric.first_baseline,
                        "candidate_value": numeric.first_candidate,
                        "max_abs_diff": numeric.max_abs_diff,
                        "max_rel_diff": numeric.max_rel_diff,
                    },
                )
            if not structural.count and not numeric.count:
                if bundle.schema_version == 2:
                    if (
                        left.normalized_representation_sha256
                        != right.normalized_representation_sha256
                    ):
                        _append(
                            differences,
                            code="document_representation_mismatch",
                            relative_path=relative_path,
                            detail={
                                "baseline_normalized_representation_sha256": (
                                    left.normalized_representation_sha256
                                ),
                                "candidate_normalized_representation_sha256": (
                                    right.normalized_representation_sha256
                                ),
                            },
                        )
                elif left.sha256 != right.sha256:
                    _append(
                        warnings,
                        code="representation_drift",
                        relative_path=relative_path,
                    )
            continue

        tolerance = (
            case.gridmap_tolerance
            if kind == "gridmap"
            else case.trajectory_tolerance
        )
        left_value = load_document_for_type(
            _artifact_path(
                baseline_root,
                case,
                relative_path,
                role="legacy",
            ),
            kind,
        )
        right_value = load_document_for_type(
            _artifact_path(
                candidate_root,
                case,
                relative_path,
                role="candidate",
                bound_artifact_scope=candidate_bound_artifact_scope,
            ),
            kind,
        )
        structural, numeric = _compare_document(
            left_value,
            right_value,
            tolerance=tolerance,
        )
        if structural.count:
            _append(
                differences,
                code=f"{kind}_structure_mismatch",
                relative_path=relative_path,
                detail={
                    "mismatch_count": structural.count,
                    "first_selector": structural.first_selector,
                    "first_reason": structural.first_reason,
                    "baseline_value": structural.first_baseline,
                    "candidate_value": structural.first_candidate,
                },
            )
        if numeric.count:
            _append(
                differences,
                code=f"{kind}_numeric_mismatch",
                relative_path=relative_path,
                detail={
                    "mismatch_count": numeric.count,
                    "first_selector": numeric.first_selector,
                    "baseline_value": numeric.first_baseline,
                    "candidate_value": numeric.first_candidate,
                    "max_abs_diff": numeric.max_abs_diff,
                    "max_rel_diff": numeric.max_rel_diff,
                },
            )
        if not structural.count and not numeric.count and left.sha256 != right.sha256:
            _append(warnings, code="representation_drift", relative_path=relative_path)

    _enrich_differences(differences, case=case)
    _enrich_differences(warnings, case=case)
    business_equivalence = not differences
    byte_identity = (
        business_equivalence
        and not warnings
        and baseline.tree_sha256 == candidate.tree_sha256
    )
    return GoldenComparison(
        schema_version=bundle.schema_version,
        case_id=case.id,
        runtime_id=bundle.runtime_id,
        verdict="EQUIVALENT" if business_equivalence else "DIFFERENT",
        byte_identity=byte_identity,
        business_equivalence=business_equivalence,
        baseline_tree_sha256=baseline.tree_sha256,
        candidate_tree_sha256=candidate.tree_sha256,
        candidate_run_ref=candidate.runtime_run_ref,
        oracle_ref=baseline.oracle_ref,
        runtime_manifest_sha256=candidate.runtime_manifest_sha256,
        calibration_snapshot_sha256=candidate.calibration_snapshot_sha256,
        annotation_revision_set_sha256=(
            candidate.annotation_revision_set_sha256
        ),
        difference_count=len(differences),
        warning_count=len(warnings),
        differences=differences[:MAX_REPORTED_ITEMS],
        warnings=warnings[:MAX_REPORTED_ITEMS],
    )


def compare_roots(
    *,
    baseline_root: Path,
    candidate_root: Path,
    bundle: GoldenCaseBundle,
    case: GoldenCase,
    baseline_command_steps: list[str] | None = None,
    candidate_command_steps: list[str] | None = None,
    runtime_manifest_sha256: str | None = None,
    source_root: Path | None = None,
    baseline_source_root: Path | None = None,
    candidate_source_root: Path | None = None,
    candidate_attestation: RuntimeRunAttestation | None = None,
    baseline_oracle_ref: str | None = None,
) -> GoldenComparison:
    """Compare caller-selected roots for non-production and legacy workflows.

    Store-derived scope overrides are intentionally absent from this public
    interface. Production M1 callers must use
    :func:`compare_roots_from_annotation_store`.
    """

    return _compare_roots(
        baseline_root=baseline_root,
        candidate_root=candidate_root,
        bundle=bundle,
        case=case,
        baseline_command_steps=baseline_command_steps,
        candidate_command_steps=candidate_command_steps,
        runtime_manifest_sha256=runtime_manifest_sha256,
        source_root=source_root,
        baseline_source_root=baseline_source_root,
        candidate_source_root=candidate_source_root,
        candidate_attestation=candidate_attestation,
        baseline_oracle_ref=baseline_oracle_ref,
    )


def compare_roots_from_annotation_store(
    *,
    annotation_store: Any,
    candidate_run_ref: str,
    baseline_root: Path,
    bundle: GoldenCaseBundle,
    case: GoldenCase,
    baseline_source_root: Path | None = None,
    baseline_oracle_ref: str | None = None,
) -> GoldenComparison:
    """Compare an M1 candidate using only a Store-derived artifact binding.

    Neither the candidate root nor its effective artifact scope is accepted
    from the caller.  Both are resolved from the committed Tracking run and
    its job/segment ledger.
    """

    if (
        case.role_scopes is None
        or not case.candidate_attestation_required
        or case.role_scopes.candidate.provenance != "runtime_attested"
    ):
        raise GoldenError(
            "Store-bound comparison requires an exact attested candidate scope",
        )
    expected = case.role_scopes.candidate
    provider = getattr(annotation_store, "golden_candidate_binding", None)
    if not callable(provider):
        raise GoldenError(
            "AnnotationStore does not provide a Golden candidate binding",
        )
    try:
        binding = StoreBoundCandidate.model_validate(
            provider(
                run_ref=candidate_run_ref,
                dataset_date=expected.dataset_date,
                source_clip=expected.source_clip,
                internal_segment=expected.internal_segment,
                scope_kind=expected.scope_kind,
            ),
        )
    except Exception as exc:
        raise GoldenError(
            "AnnotationStore rejected the Golden candidate provenance binding",
        ) from exc
    if (
        binding.run_ref != candidate_run_ref
        or binding.dataset_date != expected.dataset_date
        or binding.source_clip != expected.source_clip
        or binding.internal_segment != expected.internal_segment
        or binding.scope_kind != expected.scope_kind
    ):
        raise GoldenError(
            "AnnotationStore Golden candidate binding identity mismatch",
        )
    candidate_source_root = binding.staging_root / binding.artifact_scope
    return _compare_roots(
        baseline_root=baseline_root,
        candidate_root=binding.staging_root,
        bundle=bundle,
        case=case,
        baseline_source_root=baseline_source_root,
        candidate_source_root=candidate_source_root,
        candidate_attestation=binding.attestation,
        baseline_oracle_ref=baseline_oracle_ref,
        candidate_bound_artifact_scope=binding.artifact_scope,
    )
