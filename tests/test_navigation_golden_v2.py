from __future__ import annotations

import json
from pathlib import Path
import struct

import pytest

from vla_data_juicer_agents.navigation.golden.comparison import (
    compare_roots,
    compare_roots_from_annotation_store,
)
from vla_data_juicer_agents.navigation.golden.models import (
    GoldenCaseBundle,
    RuntimeRunAttestation,
)
from vla_data_juicer_agents.navigation.golden.snapshot import (
    GoldenError,
    capture_snapshot,
)


def _png(width: int, height: int, suffix: bytes) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + suffix
    )


def _bundle() -> GoldenCaseBundle:
    return GoldenCaseBundle.model_validate(
        {
            "schema_version": 2,
            "runtime_id": "navigation_odom_v1",
            "cases": [
                {
                    "id": "paired_tracking",
                    "role_scopes": {
                        "legacy": {
                            "artifact_scope": "samples/20260605/segment_0",
                            "dataset_date": "20260605",
                            "source_clip": "20260605_160904",
                            "internal_segment": "segment_0",
                            "provenance": "historical_unattested",
                        },
                        "candidate": {
                            "artifact_scope": "samples/20270605/segment_0",
                            "dataset_date": "20270605",
                            "source_clip": "20260605_160904",
                            "internal_segment": "segment_0",
                            "provenance": "runtime_attested",
                        },
                    },
                    "patterns": {
                        "images": ["**/*.png"],
                        "gridmaps": [],
                        "trajectories": [],
                    },
                    "expected_command_steps": [
                        "preprocess",
                        "initial_annotation",
                        "tracking",
                    ],
                    "document_normalizations": [
                        {
                            "path_pattern": "*.yaml",
                            "selector": '$["paths"]["img2video_mp4"]',
                            "strategy": "artifact_local_file",
                            "expected_relative_path": "dog.mp4",
                        },
                    ],
                    "dimensions_only_image_patterns": [
                        "tracking_img_*/*",
                    ],
                    "artifact_stage_rules": [
                        {
                            "path_pattern": "*.yaml",
                            "stage": "initial_annotation",
                        },
                        {
                            "path_pattern": "tracking_img_*/*",
                            "stage": "tracking",
                        },
                    ],
                    "candidate_attestation_required": True,
                },
            ],
        },
    )


def _attestation() -> RuntimeRunAttestation:
    return RuntimeRunAttestation(
        source="runtime_run",
        run_ref="run_1234567890abcdef1234567890abcdef",
        committed=True,
        runtime_manifest_sha256="1" * 64,
        calibration_snapshot_sha256="2" * 64,
        annotation_revision_set_sha256="3" * 64,
        command_steps=["preprocess", "initial_annotation", "tracking"],
    )


def _write_role(
    root: Path,
    *,
    date: str,
    box_x: int = 10,
    tracking_suffix: bytes,
    extra_image_suffix: bytes = b"same",
    video_path_override: str | None = None,
) -> Path:
    scope = root / "samples" / date / "segment_0"
    (scope / "tracking_img_master_green_gray_white").mkdir(parents=True)
    (scope / "dog.mp4").write_bytes(b"same-video")
    video_path = video_path_override or str(scope / "dog.mp4")
    (scope / "master_green_gray_white.yaml").write_text(
        "\n".join(
            [
                "paths:",
                "  intri: /mnt/data1/gh/tracking_1/Data/3_param/ost.yaml",
                "  extri: /mnt/data1/gh/tracking_1/Data/3_param/camera_extrinsics.yaml",
                f"  img2video_mp4: {video_path}",
                f"box: [[{box_x}, 20, 30, 40]]",
                "point: [[25, 35]]",
                "",
            ],
        ),
        encoding="utf-8",
    )
    (
        scope
        / "tracking_img_master_green_gray_white"
        / "000001.png"
    ).write_bytes(_png(1920, 1536, tracking_suffix))
    (scope / "preview.png").write_bytes(
        _png(1920, 1536, extra_image_suffix),
    )
    return scope


def test_v2_requires_committed_candidate_runtime_attestation(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    _write_role(
        candidate,
        date="20270605",
        tracking_suffix=b"candidate",
    )
    bundle = _bundle()
    with pytest.raises(GoldenError, match="RuntimeRun attestation"):
        capture_snapshot(
            root=candidate,
            bundle=bundle,
            case=bundle.cases[0],
            role="candidate",
        )


def test_v2_allows_only_registered_path_and_tracking_image_normalizations(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    _write_role(candidate, date="20270605", tracking_suffix=b"candidate")
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=bundle.cases[0],
        candidate_attestation=_attestation(),
    )

    assert result.business_equivalence is True
    assert result.byte_identity is False
    assert result.difference_count == 0
    assert result.warning_count == 0


def test_v2_yaml_representation_drift_is_a_stop_line_difference(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    candidate_scope = _write_role(
        candidate,
        date="20270605",
        tracking_suffix=b"candidate",
    )
    yaml_path = candidate_scope / "master_green_gray_white.yaml"
    content = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        content.replace("box: [[10, 20, 30, 40]]", "box:  [[10, 20, 30, 40]]"),
        encoding="utf-8",
    )
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=bundle.cases[0],
        candidate_attestation=_attestation(),
    )

    assert result.verdict == "DIFFERENT"
    difference = next(
        item
        for item in result.differences
        if item.code == "document_representation_mismatch"
    )
    assert difference.relative_path == "master_green_gray_white.yaml"
    assert (
        difference.detail["suspected_cause"]
        == "non_whitelisted_document_representation_drift"
    )
    assert difference.detail["stage"] == "initial_annotation"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ignored_artifact_patterns", ["**/*"], "forbids ignored"),
        (
            "dimensions_only_image_patterns",
            ["**/*"],
            "only permits the registered Tracking",
        ),
        (
            "gridmap_tolerance",
            {"abs_tol": 0.1, "rel_tol": 0.0},
            "requires exact numeric",
        ),
    ],
)
def test_v2_rejects_broad_comparison_escape_hatches(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _bundle().model_dump(mode="json")
    payload["cases"][0][field] = value
    with pytest.raises(ValueError, match=message):
        GoldenCaseBundle.model_validate(payload)


def test_v2_non_whitelisted_field_drift_stops_line_with_auditable_detail(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    _write_role(
        candidate,
        date="20270605",
        box_x=11,
        tracking_suffix=b"candidate",
    )
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=bundle.cases[0],
        candidate_attestation=_attestation(),
    )

    assert result.business_equivalence is False
    difference = next(
        item
        for item in result.differences
        if item.code == "document_numeric_mismatch"
    )
    assert difference.relative_path == "master_green_gray_white.yaml"
    assert difference.detail["first_selector"] == '$["box"][0][0]'
    assert difference.detail["baseline_value"] == 10
    assert difference.detail["candidate_value"] == 11
    assert difference.detail["stage"] == "initial_annotation"
    assert (
        difference.detail["suspected_cause"]
        == "algorithm_calibration_or_runtime_numeric_drift"
    )


def test_v2_non_tracking_image_content_drift_is_not_ignored(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    _write_role(
        candidate,
        date="20270605",
        tracking_suffix=b"candidate",
        extra_image_suffix=b"changed",
    )
    bundle = _bundle()

    result = compare_roots(
        baseline_root=baseline,
        candidate_root=candidate,
        bundle=bundle,
        case=bundle.cases[0],
        candidate_attestation=_attestation(),
    )

    difference = next(
        item
        for item in result.differences
        if item.code == "image_content_hash_mismatch"
    )
    assert difference.relative_path == "preview.png"
    assert difference.detail["stage"] == "unknown"
    assert difference.detail["baseline_sha256"] != difference.detail["candidate_sha256"]


def test_v2_registered_yaml_path_must_still_resolve_to_local_dog_mp4(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    _write_role(
        candidate,
        date="20270605",
        tracking_suffix=b"candidate",
        video_path_override=str(tmp_path / "outside" / "dog.mp4"),
    )
    bundle = _bundle()

    with pytest.raises(GoldenError, match="outside its artifact scope"):
        capture_snapshot(
            root=candidate,
            bundle=bundle,
            case=bundle.cases[0],
            role="candidate",
            attestation=_attestation(),
        )


def test_v2_rejects_caller_declared_candidate_runtime_facts(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    _write_role(candidate, date="20270605", tracking_suffix=b"candidate")
    bundle = _bundle()

    with pytest.raises(GoldenError, match="caller-declared"):
        capture_snapshot(
            root=candidate,
            bundle=bundle,
            case=bundle.cases[0],
            role="candidate",
            command_steps=["preprocess", "initial_annotation", "tracking"],
            attestation=_attestation(),
        )


@pytest.mark.parametrize(
    "scope_kind",
    ["postprocessing_segment", "fix_segment"],
)
def test_v2_m2_scope_requires_internal_segment(scope_kind: str) -> None:
    payload = _bundle().model_dump(mode="json")
    candidate = payload["cases"][0]["role_scopes"]["candidate"]
    candidate["scope_kind"] = scope_kind
    candidate["internal_segment"] = None

    with pytest.raises(ValueError, match="requires internal_segment"):
        GoldenCaseBundle.model_validate(payload)


@pytest.mark.parametrize(
    "scope_kind",
    ["postprocessing_segment", "fix_segment"],
)
def test_v2_m2_candidate_cannot_use_a_caller_supplied_attestation(
    tmp_path: Path,
    scope_kind: str,
) -> None:
    candidate = tmp_path / "candidate"
    _write_role(candidate, date="20270605", tracking_suffix=b"candidate")
    payload = _bundle().model_dump(mode="json")
    payload["cases"][0]["role_scopes"]["legacy"]["scope_kind"] = scope_kind
    payload["cases"][0]["role_scopes"]["candidate"]["scope_kind"] = scope_kind
    bundle = GoldenCaseBundle.model_validate(payload)

    with pytest.raises(
        GoldenError,
        match="must be resolved from AnnotationStore",
    ):
        capture_snapshot(
            root=candidate,
            bundle=bundle,
            case=bundle.cases[0],
            role="candidate",
            attestation=_attestation(),
        )


@pytest.mark.parametrize(
    "scope_kind",
    ["postprocessing_segment", "fix_segment"],
)
def test_v2_m2_candidate_accepts_only_store_bound_scope(
    tmp_path: Path,
    scope_kind: str,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    _write_role(candidate, date="20270605", tracking_suffix=b"candidate")
    payload = _bundle().model_dump(mode="json")
    payload["cases"][0]["role_scopes"]["legacy"]["scope_kind"] = scope_kind
    payload["cases"][0]["role_scopes"]["candidate"]["scope_kind"] = scope_kind
    bundle = GoldenCaseBundle.model_validate(payload)
    attestation = _attestation()

    class Store:
        def golden_candidate_binding(self, **request):
            return {
                "source": "annotation_store",
                "run_ref": request["run_ref"],
                "dataset_date": request["dataset_date"],
                "source_clips": [request["source_clip"]],
                "source_clip": request["source_clip"],
                "scope_kind": request["scope_kind"],
                "internal_segment": request["internal_segment"],
                "staging_root": candidate,
                "artifact_scope": "samples/20270605/segment_0",
                "segments": [
                    {
                        "source_clip": request["source_clip"],
                        "internal_segment": request["internal_segment"],
                        "artifact_scope": "samples/20270605/segment_0",
                    },
                ],
                "attestation": attestation.model_dump(mode="json"),
            }

    result = compare_roots_from_annotation_store(
        annotation_store=Store(),
        candidate_run_ref=attestation.run_ref,
        baseline_root=baseline,
        bundle=bundle,
        case=bundle.cases[0],
    )

    assert result.business_equivalence is True
    assert result.candidate_run_ref == attestation.run_ref


def test_v2_production_entry_reads_attestation_from_annotation_store(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    _write_role(candidate, date="20270605", tracking_suffix=b"candidate")
    bundle = _bundle()
    attestation = _attestation()

    class Store:
        def __init__(self) -> None:
            self.requested: list[dict[str, str]] = []

        def golden_candidate_binding(self, **request):
            self.requested.append(request)
            return {
                "source": "annotation_store",
                "run_ref": request["run_ref"],
                "dataset_date": request["dataset_date"],
                "source_clips": [request["source_clip"]],
                "source_clip": request["source_clip"],
                "scope_kind": request["scope_kind"],
                "internal_segment": request["internal_segment"],
                "staging_root": candidate,
                "artifact_scope": "samples/20270605/segment_0",
                "segments": [
                    {
                        "source_clip": request["source_clip"],
                        "internal_segment": request["internal_segment"],
                        "artifact_scope": "samples/20270605/segment_0",
                    },
                ],
                "attestation": attestation.model_dump(mode="json"),
            }

    store = Store()
    result = compare_roots_from_annotation_store(
        annotation_store=store,
        candidate_run_ref=attestation.run_ref,
        baseline_root=baseline,
        bundle=bundle,
        case=bundle.cases[0],
    )
    assert result.business_equivalence is True
    assert result.candidate_run_ref == attestation.run_ref
    assert result.runtime_manifest_sha256 == "1" * 64
    assert store.requested == [
        {
            "run_ref": attestation.run_ref,
            "dataset_date": "20270605",
            "source_clip": "20260605_160904",
            "internal_segment": "segment_0",
            "scope_kind": "segment",
        },
    ]


def test_v2_store_bound_prepare_global_scope_is_exact_and_attested(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    (baseline / "maps").mkdir(parents=True)
    (candidate / "maps").mkdir(parents=True)
    (baseline / "maps" / "map.png").write_bytes(_png(1, 1, b"same"))
    (candidate / "maps" / "map.png").write_bytes(_png(1, 1, b"same"))
    payload = _bundle().model_dump(mode="json")
    role_scopes = payload["cases"][0]["role_scopes"]
    for role in ("legacy", "candidate"):
        role_scopes[role]["scope_kind"] = "prepare_maps"
        role_scopes[role]["artifact_scope"] = "maps"
        role_scopes[role]["internal_segment"] = None
    payload["cases"][0]["document_normalizations"] = []
    payload["cases"][0]["dimensions_only_image_patterns"] = []
    payload["cases"][0]["artifact_root_kind"] = "finish_temp_date"
    bundle = GoldenCaseBundle.model_validate(payload)
    attestation = _attestation()

    class Store:
        def golden_candidate_binding(self, **request):
            assert request["scope_kind"] == "prepare_maps"
            assert request["internal_segment"] is None
            return {
                "source": "annotation_store",
                "run_ref": request["run_ref"],
                "dataset_date": request["dataset_date"],
                "source_clips": [request["source_clip"]],
                "source_clip": request["source_clip"],
                "scope_kind": "prepare_maps",
                "internal_segment": None,
                "staging_root": candidate,
                "artifact_scope": "maps",
                "segments": [
                    {
                        "source_clip": request["source_clip"],
                        "internal_segment": "segment_0",
                        "artifact_scope": "samples/20270605/segment_0",
                    },
                ],
                "attestation": attestation.model_dump(mode="json"),
            }

    result = compare_roots_from_annotation_store(
        annotation_store=Store(),
        candidate_run_ref=attestation.run_ref,
        baseline_root=baseline,
        bundle=bundle,
        case=bundle.cases[0],
    )

    assert result.verdict == "EQUIVALENT"


@pytest.mark.parametrize(
    ("scope_kind", "artifact_scope"),
    [
        ("prepare_maps", "v1.0-trainval"),
        ("prepare_metadata", "maps"),
    ],
)
def test_v2_rejects_prepare_global_scope_aliasing(
    scope_kind: str,
    artifact_scope: str,
) -> None:
    payload = _bundle().model_dump(mode="json")
    candidate = payload["cases"][0]["role_scopes"]["candidate"]
    candidate["scope_kind"] = scope_kind
    candidate["artifact_scope"] = artifact_scope
    candidate["internal_segment"] = None
    with pytest.raises(ValueError, match="must be exactly"):
        GoldenCaseBundle.model_validate(payload)


def test_v2_ambiguous_historical_root_requires_explicit_oracle_reference(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    _write_role(baseline, date="20260605", tracking_suffix=b"legacy")
    payload = _bundle().model_dump(mode="json")
    payload["cases"][0]["legacy_oracle_selection_required"] = True
    bundle = GoldenCaseBundle.model_validate(payload)

    with pytest.raises(GoldenError, match="explicit legacy oracle"):
        capture_snapshot(
            root=baseline,
            bundle=bundle,
            case=bundle.cases[0],
            role="legacy",
        )
    snapshot = capture_snapshot(
        root=baseline,
        bundle=bundle,
        case=bundle.cases[0],
        role="legacy",
        oracle_ref="oracle_1234567890abcdef1234567890abcdef",
    )
    assert snapshot.oracle_ref.startswith("oracle_")
