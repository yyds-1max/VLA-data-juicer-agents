from __future__ import annotations

import json
from pathlib import Path
import struct

import pytest

from vla_data_juicer_agents.navigation.golden.comparison import compare_roots
from vla_data_juicer_agents.navigation.golden.models import GoldenCaseBundle
from vla_data_juicer_agents.navigation.golden.snapshot import (
    GoldenError,
    capture_snapshot,
    load_case_bundle,
)


def _bundle(**case_overrides) -> GoldenCaseBundle:
    case = {
        "id": "odom_golden",
        "patterns": {
            "images": ["**/*.png"],
            "gridmaps": ["**/grid_map/*.json"],
            "trajectories": ["**/*trajectory*.json"],
        },
    }
    case.update(case_overrides)
    return GoldenCaseBundle.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "navigation_odom_v1",
            "cases": [case],
        },
    )


def _case(bundle: GoldenCaseBundle):
    return bundle.cases[0]


def _png(width: int, height: int, suffix: bytes = b"") -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + suffix
    )


def test_capture_is_deterministic_and_contains_only_safe_derived_data(tmp_path: Path) -> None:
    root = tmp_path / "private-input"
    (root / "scene" / "grid_map").mkdir(parents=True)
    (root / "scene" / "frame.png").write_bytes(_png(1920, 1536))
    (root / "scene" / "grid_map" / "map.json").write_text(
        json.dumps(
            {
                "x_range": [-10.0, 10.0],
                "y_range": [-4.0, 4.0],
                "resolution": 0.1,
                "data": [17.25, 18.5, 19.75, 20.0],
                "private_note": "/media/company/raw/secret",
            },
        ),
        encoding="utf-8",
    )
    bundle = _bundle()

    first = capture_snapshot(
        root=root,
        bundle=bundle,
        case=_case(bundle),
        role="legacy",
    )
    second = capture_snapshot(
        root=root,
        bundle=bundle,
        case=_case(bundle),
        role="legacy",
    )

    assert first.model_dump_json() == second.model_dump_json()
    serialized = first.model_dump_json()
    assert str(root) not in serialized
    assert "/media/company/raw/secret" not in serialized
    assert "17.25" not in serialized
    image = next(entry for entry in first.entries if entry.semantic_type == "image")
    assert image.image is not None
    assert (image.image.width, image.image.height) == (1920, 1536)
    gridmap = next(entry for entry in first.entries if entry.semantic_type == "gridmap")
    assert gridmap.numeric is not None
    assert gridmap.numeric.count == 9


def test_capture_rejects_symlinks_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / "input"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    bundle = _bundle()

    with pytest.raises(GoldenError, match="symlinks"):
        capture_snapshot(
            root=root,
            bundle=bundle,
            case=_case(bundle),
            role="legacy",
        )


def test_capture_rejects_symlink_in_artifact_scope(tmp_path: Path) -> None:
    root = tmp_path / "input"
    root.mkdir()
    target = root / "real"
    target.mkdir()
    (root / "scope").symlink_to(target, target_is_directory=True)
    bundle = _bundle(artifact_scope="scope")

    with pytest.raises(GoldenError, match="scope cannot contain a symlink"):
        capture_snapshot(
            root=root,
            bundle=bundle,
            case=_case(bundle),
            role="legacy",
        )


def test_capture_binds_registered_sample_name_and_root_expectations(
    tmp_path: Path,
) -> None:
    segment_name = "20260714_104651_zhigu_wuhan_0"
    root = tmp_path / segment_name
    (root / "fisheye_front").mkdir(parents=True)
    for index in range(2):
        (root / "fisheye_front" / f"{index}.jpg").write_bytes(
            b"\xff\xd8\xff\xd9",
        )
    bundle = GoldenCaseBundle.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "navigation_odom_v1",
            "samples": [
                {
                    "id": "nav_sample",
                    "dataset_date": "20260714",
                    "source_clip": "20260714_104651",
                    "internal_segment": segment_name,
                    "sample_kind": "synchronized_input_missing_gridmap",
                    "contamination_risk": "synchronized_input_read_only",
                },
            ],
            "cases": [
                {
                    "id": "sample_case",
                    "sample_id": "nav_sample",
                    "patterns": {"images": []},
                    "root_expectations": [
                        {
                            "modality": "fisheye_front",
                            "relative_pattern": "fisheye_front/*",
                            "present": True,
                            "file_count": 2,
                        },
                        {
                            "modality": "grid_map",
                            "relative_pattern": "grid_map/*",
                            "present": False,
                        },
                    ],
                },
            ],
        },
    )

    snapshot = capture_snapshot(
        root=root,
        bundle=bundle,
        case=bundle.cases[0],
        role="legacy",
    )
    assert snapshot.case_id == "sample_case"

    (root / "fisheye_front" / "1.jpg").unlink()
    with pytest.raises(GoldenError, match="file count mismatch"):
        capture_snapshot(
            root=root,
            bundle=bundle,
            case=bundle.cases[0],
            role="legacy",
        )

    wrong_root = tmp_path / "different_segment"
    wrong_root.mkdir()
    with pytest.raises(GoldenError, match="root name"):
        capture_snapshot(
            root=wrong_root,
            bundle=bundle,
            case=bundle.cases[0],
            role="legacy",
        )


def test_file_expectation_rejects_a_same_named_directory(tmp_path: Path) -> None:
    root = tmp_path / "input"
    (root / "times.json").mkdir(parents=True)
    bundle = _bundle(
        root_expectations=[
            {
                "modality": "times_json",
                "relative_pattern": "times.json",
                "present": True,
            },
        ],
    )

    with pytest.raises(GoldenError, match="expectation mismatch"):
        capture_snapshot(
            root=root,
            bundle=bundle,
            case=_case(bundle),
            role="legacy",
        )


def test_capture_validates_missing_gridmap_source_separately_from_output(
    tmp_path: Path,
) -> None:
    segment_name = "20260714_104651_zhigu_wuhan_0"
    source_root = tmp_path / "source" / segment_name
    output_root = tmp_path / "output" / segment_name
    (source_root / "fisheye_front").mkdir(parents=True)
    (source_root / "fisheye_front" / "frame.jpg").write_bytes(b"source")
    (output_root / "grid_map").mkdir(parents=True)
    (output_root / "grid_map" / "map.json").write_text(
        '{"data":[1]}',
        encoding="utf-8",
    )
    bundle = GoldenCaseBundle.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "navigation_odom_v1",
            "samples": [
                {
                    "id": "missing_gridmap_sample",
                    "dataset_date": "20260714",
                    "source_clip": "20260714_104651",
                    "internal_segment": segment_name,
                    "sample_kind": "synchronized_input_missing_gridmap",
                    "contamination_risk": "synchronized_input_read_only",
                    "source_expectations": [
                        {
                            "modality": "fisheye_front",
                            "relative_pattern": "fisheye_front/*",
                            "present": True,
                            "file_count": 1,
                        },
                        {
                            "modality": "grid_map",
                            "relative_pattern": "grid_map/*",
                            "present": False,
                        },
                    ],
                },
            ],
            "cases": [
                {
                    "id": "gridmap_output",
                    "sample_id": "missing_gridmap_sample",
                    "root_expectations": [
                        {
                            "modality": "grid_map",
                            "relative_pattern": "grid_map/*",
                            "present": True,
                        },
                    ],
                },
            ],
        },
    )

    with pytest.raises(GoldenError, match="requires a source root"):
        capture_snapshot(
            root=output_root,
            bundle=bundle,
            case=bundle.cases[0],
            role="legacy",
        )

    snapshot = capture_snapshot(
        root=output_root,
        source_root=source_root,
        bundle=bundle,
        case=bundle.cases[0],
        role="legacy",
    )
    assert snapshot.case_id == "gridmap_output"

    (source_root / "grid_map").mkdir()
    (source_root / "grid_map" / "unexpected.json").write_text(
        "{}",
        encoding="utf-8",
    )
    with pytest.raises(GoldenError, match="expectation mismatch"):
        capture_snapshot(
            root=output_root,
            source_root=source_root,
            bundle=bundle,
            case=bundle.cases[0],
            role="legacy",
        )


def test_source_expectation_rejects_an_intermediate_symlink(
    tmp_path: Path,
) -> None:
    segment_name = "20260714_104651_zhigu_wuhan_0"
    source_root = tmp_path / "source" / segment_name
    actual_images = source_root / "actual_images"
    actual_images.mkdir(parents=True)
    (actual_images / "frame.jpg").write_bytes(b"source")
    (source_root / "fisheye_front").symlink_to(
        actual_images,
        target_is_directory=True,
    )
    output_root = tmp_path / "output" / segment_name
    output_root.mkdir(parents=True)
    bundle = GoldenCaseBundle.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "navigation_odom_v1",
            "samples": [
                {
                    "id": "nav_sample",
                    "dataset_date": "20260714",
                    "source_clip": "20260714_104651",
                    "internal_segment": segment_name,
                    "sample_kind": "synchronized_input_missing_gridmap",
                    "contamination_risk": "synchronized_input_read_only",
                    "source_expectations": [
                        {
                            "modality": "fisheye_front",
                            "relative_pattern": "fisheye_front/*",
                            "present": True,
                        },
                    ],
                },
            ],
            "cases": [{"id": "case", "sample_id": "nav_sample"}],
        },
    )

    with pytest.raises(GoldenError, match="matched a symlink"):
        capture_snapshot(
            root=output_root,
            source_root=source_root,
            bundle=bundle,
            case=bundle.cases[0],
            role="legacy",
        )


@pytest.mark.parametrize(
    "filename,content,error",
    [
        ("duplicate.json", '{"key": 1, "key": 2}', "duplicate"),
        ("duplicate.yaml", "key: 1\nkey: 2\n", "duplicate"),
        ("nan.json", '{"key": NaN}', "non-finite"),
    ],
)
def test_capture_rejects_ambiguous_or_non_finite_documents(
    tmp_path: Path,
    filename: str,
    content: str,
    error: str,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    (root / filename).write_text(content, encoding="utf-8")
    bundle = _bundle()

    with pytest.raises(GoldenError, match=error):
        capture_snapshot(
            root=root,
            bundle=bundle,
            case=_case(bundle),
            role="legacy",
        )


def test_json_representation_drift_is_business_equivalent(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "metadata.json").write_text('{"b":2,"a":1}\n', encoding="utf-8")
    (candidate / "metadata.json").write_text(
        '{\n  "a": 1,\n  "b": 2\n}\n',
        encoding="utf-8",
    )
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=_case(bundle),
    )

    assert result.business_equivalence is True
    assert result.byte_identity is False
    assert [warning.code for warning in result.warnings] == ["representation_drift"]


def test_compare_detects_file_tree_image_and_binary_differences(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "frame.png").write_bytes(_png(10, 20, b"baseline"))
    (candidate / "frame.png").write_bytes(_png(11, 20, b"candidate"))
    (baseline / "payload.bin").write_bytes(b"a")
    (candidate / "payload.bin").write_bytes(b"b")
    (candidate / "extra.bin").write_bytes(b"extra")
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=_case(bundle),
    )

    codes = {difference.code for difference in result.differences}
    assert result.business_equivalence is False
    assert {
        "extra_artifact",
        "image_dimensions_mismatch",
        "image_content_hash_mismatch",
        "content_hash_mismatch",
    }.issubset(codes)


def test_compare_ignores_only_explicitly_declared_non_business_artifacts(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "result.json").write_text('{"value":1}', encoding="utf-8")
    (candidate / "result.json").write_text('{"value":1}', encoding="utf-8")
    (baseline / "diagnostics.log").write_text("legacy", encoding="utf-8")
    (candidate / "diagnostics.log").write_text("candidate", encoding="utf-8")

    strict_bundle = _bundle()
    ignored_bundle = _bundle(
        ignored_artifact_patterns=["diagnostics.log"],
    )
    strict = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=strict_bundle,
        case=_case(strict_bundle),
    )
    ignored = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=ignored_bundle,
        case=_case(ignored_bundle),
    )

    assert strict.business_equivalence is False
    assert ignored.business_equivalence is True


def test_compare_detects_gridmap_and_trajectory_numeric_differences_at_zero_tolerance(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "segment" / "grid_map").mkdir(parents=True)
    (baseline / "segment" / "grid_map" / "map.json").write_text(
        json.dumps({"resolution": 0.1, "data": [1.0, 2.0]}),
        encoding="utf-8",
    )
    (candidate / "segment" / "grid_map" / "map.json").write_text(
        json.dumps({"resolution": 0.1, "data": [1.0, 2.000000001]}),
        encoding="utf-8",
    )
    (baseline / "segment" / "person_trajectory.json").write_text(
        json.dumps({"traj": [[1.0, 2.0]], "speed": [0.5]}),
        encoding="utf-8",
    )
    (candidate / "segment" / "person_trajectory.json").write_text(
        json.dumps({"traj": [[1.0, 2.0]], "speed": [0.500000001]}),
        encoding="utf-8",
    )
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=_case(bundle),
    )

    by_code = {difference.code: difference for difference in result.differences}
    assert "gridmap_numeric_mismatch" in by_code
    assert "trajectory_numeric_mismatch" in by_code
    assert by_code["gridmap_numeric_mismatch"].detail["mismatch_count"] == 1
    assert (
        by_code["trajectory_numeric_mismatch"].detail["first_selector"]
        == '$["speed"][0]'
    )


def test_structural_differences_are_aggregated_without_unbounded_report_growth(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "person_trajectory.json").write_text(
        json.dumps({"data": [0] * 1000}),
        encoding="utf-8",
    )
    (candidate / "person_trajectory.json").write_text(
        json.dumps({"data": [0.0] * 1000}),
        encoding="utf-8",
    )
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=_case(bundle),
    )

    structural = [
        difference
        for difference in result.differences
        if difference.code == "trajectory_structure_mismatch"
    ]
    assert len(structural) == 1
    assert structural[0].detail["mismatch_count"] == 1000
    assert result.difference_count == 1


def test_non_zero_tolerance_requires_an_explicit_case_policy(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "person_trajectory.json").write_text(
        json.dumps({"traj": [1.0]}),
        encoding="utf-8",
    )
    (candidate / "person_trajectory.json").write_text(
        json.dumps({"traj": [1.000001]}),
        encoding="utf-8",
    )
    exact_bundle = _bundle()
    tolerant_bundle = _bundle(
        trajectory_tolerance={"abs_tol": 0.00001, "rel_tol": 0.0},
    )

    exact = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=exact_bundle,
        case=_case(exact_bundle),
    )
    tolerant = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=tolerant_bundle,
        case=_case(tolerant_bundle),
    )

    assert exact.business_equivalence is False
    assert tolerant.business_equivalence is True
    assert tolerant.warning_count == 1


def test_tolerance_policy_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        _bundle(trajectory_tolerance={"abs_tol": float("inf"), "rel_tol": 0.0})


def test_bundle_validates_commit_safe_sample_registry() -> None:
    bundle = GoldenCaseBundle.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "navigation_odom_v1",
            "samples": [
                {
                    "id": "nav_sample",
                    "dataset_date": "20260714",
                    "source_clip": "20260714_104651",
                    "internal_segment": "20260714_104651_zhigu_wuhan_0",
                    "sample_kind": "synchronized_input_missing_gridmap",
                    "contamination_risk": "synchronized_input_read_only",
                    "source_expectations": [
                        {
                            "modality": "fisheye_front",
                            "relative_pattern": "fisheye_front/*",
                            "present": True,
                            "file_count": 71,
                        },
                        {
                            "modality": "grid_map",
                            "relative_pattern": "grid_map/*",
                            "present": False,
                        },
                    ],
                },
            ],
            "cases": [
                {
                    "id": "missing_gridmap",
                    "sample_id": "nav_sample",
                    "applicable_stages": ["gridmap_prepare"],
                    "excluded_stages": ["fix"],
                    "root_expectations": [
                        {
                            "modality": "fisheye_front",
                            "relative_pattern": "fisheye_front/*",
                            "present": True,
                            "file_count": 71,
                        },
                        {
                            "modality": "grid_map",
                            "relative_pattern": "grid_map/*",
                            "present": False,
                        },
                    ],
                },
            ],
        },
    )

    assert bundle.cases[0].sample_id == "nav_sample"
    assert bundle.samples[0].source_expectations[0].file_count == 71


def test_checked_in_golden_registry_freezes_three_private_sample_identities() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    bundle = load_case_bundle(
        repository_root / "runtime/navigation_odom_v1/golden-cases.yaml",
    )

    assert {sample.internal_segment for sample in bundle.samples} == {
        "20260605_160904_zhigu_wuhan_0",
        "20260623_145550_zhigu_wuhan_0",
        "20260714_104651_zhigu_wuhan_0",
    }
    missing_gridmap_sample = next(
        sample
        for sample in bundle.samples
        if sample.id == "nav_20260714_104651_zhigu_wuhan_0"
    )
    expectations = {
        item.modality: (item.present, item.file_count)
        for item in missing_gridmap_sample.source_expectations
    }
    assert expectations["grid_map"] == (False, None)
    assert expectations["fisheye_front"] == (True, 71)
    missing_gridmap_case = next(
        case
        for case in bundle.cases
        if case.id == "missing_gridmap_20260714_104651"
    )
    assert missing_gridmap_case.root_expectations[0].present is True
    assert all(case.gridmap_tolerance.abs_tol == 0 for case in bundle.cases)
    assert all(case.trajectory_tolerance.abs_tol == 0 for case in bundle.cases)


def test_bundle_rejects_unknown_sample_reference() -> None:
    with pytest.raises(ValueError, match="unknown sample"):
        GoldenCaseBundle.model_validate(
            {
                "schema_version": 1,
                "runtime_id": "navigation_odom_v1",
                "cases": [{"id": "missing", "sample_id": "not_registered"}],
            },
        )


def test_expected_command_sequence_is_compared_by_digest(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    bundle = _bundle(expected_command_steps=["projection", "trajectory_0525", "publish"])

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=_case(bundle),
        baseline_command_steps=["projection", "publish", "trajectory_0525"],
        candidate_command_steps=["projection", "trajectory_0525", "publish"],
    )

    assert result.business_equivalence is False
    assert [difference.detail["role"] for difference in result.differences] == ["legacy"]
