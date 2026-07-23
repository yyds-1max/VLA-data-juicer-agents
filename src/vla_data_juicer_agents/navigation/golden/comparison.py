from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from .models import (
    GoldenCase,
    GoldenCaseBundle,
    GoldenComparison,
    GoldenDifference,
    GoldenEntry,
    NumericTolerance,
)
from .snapshot import (
    GoldenError,
    capture_snapshot,
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

    def add(self, selector: str, reason: str) -> None:
        self.count += 1
        if self.first_selector is None:
            self.first_selector = selector
            self.first_reason = reason


@dataclass
class _NumericDifferences:
    count: int = 0
    first_selector: str | None = None
    max_abs_diff: float = 0.0
    max_rel_diff: float = 0.0

    def add(self, selector: str, baseline: int | float, candidate: int | float) -> None:
        absolute = abs(baseline - candidate)
        denominator = max(abs(baseline), abs(candidate), 1e-300)
        relative = absolute / denominator
        self.count += 1
        self.first_selector = self.first_selector or selector
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
            structural.add(selector, f"type:{left_type}->{right_type}")
            return
        if left_type == "object":
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys):
                structural.add(_selector(selector, key), "missing_key")
            for key in sorted(right_keys - left_keys):
                structural.add(_selector(selector, key), "extra_key")
            for key in sorted(left_keys & right_keys):
                visit(left[key], right[key], _selector(selector, key))
            return
        if left_type == "array":
            if len(left) != len(right):
                structural.add(
                    selector,
                    f"length:{len(left)}->{len(right)}",
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
            structural.add(selector, "scalar_mismatch")

    visit(baseline, candidate, "$")
    return structural, numeric


def _entry_map(entries: list[GoldenEntry]) -> dict[str, GoldenEntry]:
    return {entry.relative_path: entry for entry in entries}


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


def _artifact_path(root: Path, case: GoldenCase, relative_path: str) -> Path:
    root = root.resolve(strict=True)
    return root / case.artifact_scope / relative_path


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
) -> GoldenComparison:
    baseline = capture_snapshot(
        root=baseline_root,
        bundle=bundle,
        case=case,
        role="legacy",
        runtime_manifest_sha256=runtime_manifest_sha256,
        command_steps=baseline_command_steps,
        source_root=source_root,
    )
    candidate = capture_snapshot(
        root=candidate_root,
        bundle=bundle,
        case=case,
        role="candidate",
        runtime_manifest_sha256=runtime_manifest_sha256,
        command_steps=candidate_command_steps,
        source_root=source_root,
    )
    differences: list[GoldenDifference] = []
    warnings: list[GoldenDifference] = []

    expected_steps = case.expected_command_steps
    if expected_steps:
        from .snapshot import command_sequence_sha256

        expected_hash = command_sequence_sha256(expected_steps)
        for role, actual_hash in (
            ("legacy", baseline.command_sequence_sha256),
            ("candidate", candidate.command_sequence_sha256),
        ):
            if actual_hash != expected_hash:
                _append(
                    differences,
                    code="command_sequence_mismatch",
                    detail={"role": role},
                )

    left_entries = _entry_map(baseline.entries)
    right_entries = _entry_map(candidate.entries)
    for relative_path in sorted(left_entries.keys() - right_entries.keys()):
        _append(differences, code="missing_artifact", relative_path=relative_path)
    for relative_path in sorted(right_entries.keys() - left_entries.keys()):
        _append(differences, code="extra_artifact", relative_path=relative_path)

    for relative_path in sorted(left_entries.keys() & right_entries.keys()):
        left = left_entries[relative_path]
        right = right_entries[relative_path]
        if left.kind != right.kind or left.semantic_type != right.semantic_type:
            _append(differences, code="artifact_type_mismatch", relative_path=relative_path)
            continue
        if left.kind == "directory":
            continue

        kind = left.semantic_type
        if kind == "binary":
            if left.sha256 != right.sha256:
                _append(differences, code="content_hash_mismatch", relative_path=relative_path)
            continue
        if kind == "image":
            if left.image != right.image:
                _append(differences, code="image_dimensions_mismatch", relative_path=relative_path)
            if left.sha256 != right.sha256:
                _append(differences, code="image_content_hash_mismatch", relative_path=relative_path)
            continue

        if kind in {"json", "yaml"}:
            if left.document != right.document:
                _append(differences, code="document_semantics_mismatch", relative_path=relative_path)
            elif left.sha256 != right.sha256:
                _append(warnings, code="representation_drift", relative_path=relative_path)
            continue

        tolerance = (
            case.gridmap_tolerance
            if kind == "gridmap"
            else case.trajectory_tolerance
        )
        left_value = load_document_for_type(
            _artifact_path(baseline_root, case, relative_path),
            kind,
        )
        right_value = load_document_for_type(
            _artifact_path(candidate_root, case, relative_path),
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
                    "max_abs_diff": numeric.max_abs_diff,
                    "max_rel_diff": numeric.max_rel_diff,
                },
            )
        if not structural.count and not numeric.count and left.sha256 != right.sha256:
            _append(warnings, code="representation_drift", relative_path=relative_path)

    business_equivalence = not differences
    byte_identity = (
        business_equivalence
        and not warnings
        and baseline.tree_sha256 == candidate.tree_sha256
    )
    return GoldenComparison(
        case_id=case.id,
        runtime_id=bundle.runtime_id,
        verdict="EQUIVALENT" if business_equivalence else "DIFFERENT",
        byte_identity=byte_identity,
        business_equivalence=business_equivalence,
        baseline_tree_sha256=baseline.tree_sha256,
        candidate_tree_sha256=candidate.tree_sha256,
        difference_count=len(differences),
        warning_count=len(warnings),
        differences=differences[:MAX_REPORTED_ITEMS],
        warnings=warnings[:MAX_REPORTED_ITEMS],
    )
