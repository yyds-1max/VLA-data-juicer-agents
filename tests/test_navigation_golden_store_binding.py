from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct

import pytest
import yaml

from vla_data_juicer_agents.annotation.legacy_yaml import LegacyYamlAdapter
from vla_data_juicer_agents.annotation.runtime import (
    TrackingTarget,
    prepared_staging_artifact_sha256,
    tracking_checkpoint_artifact_sha256,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.golden import cli
from vla_data_juicer_agents.navigation.golden.comparison import (
    compare_roots_from_annotation_store,
)
from vla_data_juicer_agents.navigation.golden.models import GoldenCaseBundle
from vla_data_juicer_agents.navigation.golden.snapshot import GoldenError


RUN_REF = "run_" + "1" * 32
ORACLE_REF = "oracle_" + "2" * 32
RUNTIME_SHA = "3" * 64
CALIBRATION_SHA = "4" * 64
COMMAND_STEPS = [
    "processing_calibration_snapshot",
    "initial_annotation",
    "tracking",
]


def _jpeg(width: int = 1, height: int = 1) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + b"\x00\x07"
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\xff\xd9"
    )


def _png(suffix: bytes = b"same", width: int = 1, height: int = 1) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + suffix
    )


def _bundle(
    *,
    candidate_date: str = "20270605",
    candidate_clip: str = "clip_0",
    candidate_segment: str = "segment_0",
) -> GoldenCaseBundle:
    return GoldenCaseBundle.model_validate(
        {
            "schema_version": 2,
            "runtime_id": "navigation_odom_v1",
            "cases": [
                {
                    "id": "store_bound_tracking",
                    "role_scopes": {
                        "legacy": {
                            "artifact_scope": "oracle/legacy_segment",
                            "dataset_date": "20260605",
                            "source_clip": "clip_0",
                            "internal_segment": "legacy_segment",
                            "provenance": "historical_unattested",
                        },
                        "candidate": {
                            # The committed test tree intentionally uses a
                            # different hierarchy. Store binding, not this
                            # caller-facing string, selects the actual scope.
                            "artifact_scope": (
                                f"samples/{candidate_date}/{candidate_segment}"
                            ),
                            "dataset_date": candidate_date,
                            "source_clip": candidate_clip,
                            "internal_segment": candidate_segment,
                            "provenance": "runtime_attested",
                        },
                    },
                    "expected_command_steps": COMMAND_STEPS,
                    "document_normalizations": [
                        {
                            "path_pattern": "*.yaml",
                            "selector": '$["paths"]["img2video_mp4"]',
                            "strategy": "artifact_local_file",
                            "expected_relative_path": "dog.mp4",
                        }
                    ],
                    "dimensions_only_image_patterns": [
                        "tracking_img_*/*",
                    ],
                    "candidate_attestation_required": True,
                    "legacy_oracle_selection_required": True,
                },
            ],
        },
    )


def _global_bundle(scope_kind: str) -> GoldenCaseBundle:
    artifact_scope = {
        "prepare_maps": "maps",
        "prepare_metadata": "v1.0-trainval",
    }[scope_kind]
    expectation = (
        {
            "modality": "published_map",
            "relative_pattern": "map.png",
            "present": True,
            "file_count": 1,
        }
        if scope_kind == "prepare_maps"
        else {
            "modality": "generated_metadata",
            "relative_pattern": "**/*",
            "present": True,
        }
    )
    return GoldenCaseBundle.model_validate(
        {
            "schema_version": 2,
            "runtime_id": "navigation_odom_v1",
            "cases": [
                {
                    "id": f"store_bound_{scope_kind}",
                    "artifact_root_kind": "finish_temp_date",
                    "role_scopes": {
                        "legacy": {
                            "scope_kind": scope_kind,
                            "artifact_scope": artifact_scope,
                            "dataset_date": "20260605",
                            "source_clip": "clip_0",
                            "provenance": "historical_unattested",
                        },
                        "candidate": {
                            "scope_kind": scope_kind,
                            "artifact_scope": artifact_scope,
                            "dataset_date": "20270605",
                            "source_clip": "clip_0",
                            "provenance": "runtime_attested",
                        },
                    },
                    "root_expectations": [expectation],
                    "applicable_stages": [
                        (
                            "map_publish"
                            if scope_kind == "prepare_maps"
                            else "metadata_generate"
                        ),
                    ],
                    "artifact_stage_rules": [
                        {
                            "path_pattern": "**/*",
                            "stage": (
                                "map_publish"
                                if scope_kind == "prepare_maps"
                                else "metadata_generate"
                            ),
                        },
                    ],
                    "expected_command_steps": COMMAND_STEPS,
                    "candidate_attestation_required": True,
                    "legacy_oracle_selection_required": True,
                },
            ],
        },
    )


def _insert_committed_run(
    tmp_path: Path,
    *,
    map_bytes: bytes | None = None,
    metadata_bytes: bytes = b'{"samples":["same"]}\n',
    extra_map_artifact: bool = False,
    extra_top_level: str | None = None,
) -> tuple[AnnotationStore, Path, Path]:
    database = tmp_path / "annotation.sqlite"
    store = AnnotationStore(database)
    staging_root = tmp_path / "private-work" / "committed-staging"
    map_bytes = _png() if map_bytes is None else map_bytes
    (staging_root / ".runtime").mkdir(parents=True)
    (staging_root / "maps").mkdir()
    (staging_root / "maps" / "map.png").write_bytes(map_bytes)
    if extra_map_artifact:
        (staging_root / "maps" / "unexpected.bin").write_bytes(b"unexpected")
    (staging_root / "v1.0-trainval").mkdir()
    (staging_root / "v1.0-trainval" / "sample.json").write_bytes(
        metadata_bytes,
    )
    if extra_top_level is not None:
        (staging_root / extra_top_level).mkdir()
    segment_root = (
        staging_root / "samples" / "20270605" / "segment_0"
    )
    segment_root.mkdir(parents=True)
    artifact = segment_root / "artifact.bin"
    artifact.write_bytes(b"same")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    timestamp = "2026-07-23T00:00:00.000+00:00"
    target_ref = "target_" + "7" * 32
    segment_ref = "segment_" + "a" * 32
    targets = [
        {
            "target_ref": target_ref,
            "bbox": [0, 0, 1, 1],
            "point": [0, 0],
            "colors": {
                "upper": "green",
                "lower": "gray",
                "shoes": "white",
            },
        }
    ]
    revision_sha = hashlib.sha256(
        json.dumps(
            targets,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    rendered = LegacyYamlAdapter().render(
        segment_root,
        [
            {
                "target_ref": target_ref,
                "bbox": [0, 0, 1, 1],
                "point": [0, 0],
                "upper_color": "green",
                "lower_color": "gray",
                "shoes_color": "white",
            }
        ],
    )
    yaml_path = segment_root / rendered[0].filename
    yaml_path.write_text(rendered[0].content, encoding="utf-8")
    identity = yaml_path.stem
    output_dir = segment_root / f"tracking_img_{identity}"
    output_dir.mkdir()
    (output_dir / "000001.jpg").write_bytes(_jpeg())
    points_path = segment_root / f"img_{identity}.txt"
    points_path.write_text("1 2 3\n", encoding="utf-8")
    checkpoint_sha = tracking_checkpoint_artifact_sha256(
        output_dir,
        points_path,
    )
    prepared_sha = prepared_staging_artifact_sha256(
        staging_root,
        (
            TrackingTarget(
                segment_root=segment_root,
                yaml_path=yaml_path,
                identity=identity,
            ),
        ),
    )

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        job_id = connection.execute(
            """
            INSERT INTO annotation_jobs (
                job_ref, dataset_date, status, state_revision,
                calibration_snapshot_id, staging_root, created_at, updated_at
            ) VALUES (?, '20270605', 'tracked', 3, NULL, ?, ?, ?)
            """,
            ("job_" + "8" * 32, str(staging_root), timestamp, timestamp),
        ).lastrowid
        assert job_id is not None
        calibration_id = connection.execute(
            """
            INSERT INTO calibration_snapshots (
                snapshot_ref, job_id, profile_ref, label, content_sha256,
                private_snapshot_dir, files_json, created_at
            ) VALUES (?, ?, 'calibration_profile', 'calibration_profile', ?,
                      ?, '[]', ?)
            """,
            (
                "snapshot_" + "9" * 32,
                job_id,
                CALIBRATION_SHA,
                str(tmp_path / "private-calibration"),
                timestamp,
            ),
        ).lastrowid
        connection.execute(
            """
            UPDATE annotation_jobs
            SET calibration_snapshot_id = ?
            WHERE id = ?
            """,
            (calibration_id, job_id),
        )
        connection.execute(
            """
            INSERT INTO annotation_job_source_clips (
                job_id, ordinal, source_clip
            ) VALUES (?, 1, 'clip_0')
            """,
            (job_id,),
        )
        segment_id = connection.execute(
            """
            INSERT INTO annotation_segments (
                segment_ref, job_id, ordinal, source_clip, status,
                state_revision, draft_revision, submitted_revision,
                private_segment_key, private_segment_root,
                private_first_frame_path, first_frame_width,
                first_frame_height, first_frame_sha256, first_frame_etag,
                created_at, updated_at
            ) VALUES (?, ?, 1, 'clip_0', 'tracked', 3, 1, 1,
                      'segment_0', ?, ?, 1, 1, ?, ?, ?, ?)
            """,
            (
                segment_ref,
                job_id,
                str(segment_root),
                str(artifact),
                artifact_sha,
                artifact_sha,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        assert segment_id is not None
        connection.execute(
            """
            INSERT INTO initial_annotation_revisions (
                revision_ref, segment_id, revision_number, targets_json,
                content_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                "revision_" + "b" * 32,
                segment_id,
                json.dumps(targets),
                revision_sha,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO annotation_segment_actions (
                action_ref, segment_id, action, safe_payload_json,
                actor_kind, deployment_instance, created_at
            ) VALUES (?, ?, 'submitted', '{"revision":1}', 'manual_web',
                      'test', ?)
            """,
            ("action_" + "c" * 32, segment_id, timestamp),
        )
        prepare_run_id = connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt, created_at, updated_at
            ) VALUES (?, ?, 'prepare', 'succeeded', 1, ?, ?)
            """,
            ("run_" + "d" * 32, job_id, timestamp, timestamp),
        ).lastrowid
        tracking_run_id = connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt, created_at, updated_at
            ) VALUES (?, ?, 'tracking', 'succeeded', 1, ?, ?)
            """,
            (RUN_REF, job_id, timestamp, timestamp),
        ).lastrowid
        assert prepare_run_id is not None and tracking_run_id is not None
        prepare_manifest = {
            "runtime_manifest_sha256": RUNTIME_SHA,
            "prepared_artifact_tree_sha256": prepared_sha,
            "command_steps": COMMAND_STEPS[:1],
        }
        tracking_manifest = {
            "runtime_manifest_sha256": RUNTIME_SHA,
            "prepared_artifact_tree_sha256": prepared_sha,
            "command_steps": COMMAND_STEPS[1:],
            "revision_set": [
                {
                    "segment_ref": segment_ref,
                    "revision": 1,
                    "sha256": revision_sha,
                }
            ],
            "checkpoints": [
                {
                    "segment_ref": segment_ref,
                    "target_ref": target_ref,
                    "identity": identity,
                    "artifact_sha256": checkpoint_sha,
                }
            ],
        }
        for run_id, steps in (
            (prepare_run_id, COMMAND_STEPS[:1]),
            (tracking_run_id, COMMAND_STEPS[1:]),
        ):
            for ordinal, safe_step_code in enumerate(steps, start=1):
                connection.execute(
                    """
                    INSERT INTO runtime_run_steps (
                        run_id, ordinal, safe_step_code, status,
                        return_code, created_at, updated_at
                    ) VALUES (?, ?, ?, 'succeeded', 0, ?, ?)
                    """,
                    (
                        run_id,
                        ordinal,
                        safe_step_code,
                        timestamp,
                        timestamp,
                    ),
                )
        connection.execute(
            """
            INSERT INTO runtime_run_steps (
                run_id, ordinal, safe_step_code, status,
                artifact_sha256, created_at, updated_at
            ) VALUES (?, 3, 'tracking_target_completed', 'succeeded',
                      ?, ?, ?)
            """,
            (
                tracking_run_id,
                checkpoint_sha,
                timestamp,
                timestamp,
            ),
        )
        for manifest_ref, run_id, stage, manifest in (
            (
                "manifest_" + "e" * 32,
                prepare_run_id,
                "prepare",
                prepare_manifest,
            ),
            (
                "manifest_" + "f" * 32,
                tracking_run_id,
                "tracking",
                tracking_manifest,
            ),
        ):
            encoded_manifest = json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO artifact_manifests (
                    manifest_ref, job_id, run_id, stage, content_sha256,
                    manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest_ref,
                    job_id,
                    run_id,
                    stage,
                    hashlib.sha256(
                        encoded_manifest.encode("utf-8"),
                    ).hexdigest(),
                    encoded_manifest,
                    timestamp,
                ),
            )
        connection.execute(
            """
            INSERT INTO tracking_checkpoints (
                checkpoint_ref, job_id, run_id, segment_id, target_ref,
                revision_sha256, identity, private_output_dir,
                private_points_path, artifact_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?)
            """,
            (
                "checkpoint_" + "0" * 32,
                job_id,
                tracking_run_id,
                segment_id,
                target_ref,
                revision_sha,
                identity,
                str(output_dir),
                str(points_path),
                checkpoint_sha,
                timestamp,
            ),
        )
    return store, database, staging_root


def _baseline(tmp_path: Path) -> Path:
    root = tmp_path / "historical-oracle"
    scope = root / "oracle" / "legacy_segment"
    scope.mkdir(parents=True)
    (scope / "artifact.bin").write_bytes(b"same")
    rendered = LegacyYamlAdapter().render(
        scope,
        [
            {
                "target_ref": "target_" + "7" * 32,
                "bbox": [0, 0, 1, 1],
                "point": [0, 0],
                "upper_color": "green",
                "lower_color": "gray",
                "shoes_color": "white",
            }
        ],
    )[0]
    (scope / rendered.filename).write_text(
        rendered.content,
        encoding="utf-8",
    )
    tracking = scope / f"tracking_img_{Path(rendered.filename).stem}"
    tracking.mkdir()
    (tracking / "000001.jpg").write_bytes(_jpeg())
    (scope / f"img_{Path(rendered.filename).stem}.txt").write_text(
        "1 2 3\n",
        encoding="utf-8",
    )
    return root


def _global_baseline(
    tmp_path: Path,
    scope_kind: str,
    *,
    extra_map_artifact: bool = False,
    extra_map_name: str | None = None,
) -> Path:
    root = tmp_path / f"historical-{scope_kind}"
    if scope_kind == "prepare_maps":
        scope = root / "maps"
        scope.mkdir(parents=True)
        (scope / "map.png").write_bytes(_png())
        if extra_map_artifact:
            (scope / "legacy-only.bin").write_bytes(b"legacy")
        if extra_map_name is not None:
            (scope / extra_map_name).write_bytes(b"legacy")
    else:
        scope = root / "v1.0-trainval"
        scope.mkdir(parents=True)
        (scope / "sample.json").write_bytes(b'{"samples":["same"]}\n')
    return root


def _write_cases(path: Path, bundle: GoldenCaseBundle) -> None:
    path.write_text(
        yaml.safe_dump(
            bundle.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _cli_args(
    *,
    cases: Path,
    database: Path,
    baseline: Path,
    output: Path,
    case_id: str = "store_bound_tracking",
) -> list[str]:
    return [
        "compare-annotation-run",
        "--cases",
        str(cases),
        "--case",
        case_id,
        "--annotation-db",
        str(database),
        "--candidate-run-ref",
        RUN_REF,
        "--baseline-root",
        str(baseline),
        "--baseline-oracle-ref",
        ORACLE_REF,
        "--output-dir",
        str(output),
    ]


def test_store_bound_compare_uses_committed_staging_and_safe_provenance(
    tmp_path: Path,
) -> None:
    store, _database, staging_root = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    bundle = _bundle()

    binding = store.golden_candidate_binding(
        run_ref=RUN_REF,
        dataset_date="20270605",
        source_clip="clip_0",
        internal_segment="segment_0",
    )
    assert Path(binding["staging_root"]) == staging_root.resolve()
    assert binding["artifact_scope"] == (
        "samples/20270605/segment_0"
    )
    assert binding["segments"] == [
        {
            "source_clip": "clip_0",
            "internal_segment": "segment_0",
            "artifact_scope": "samples/20270605/segment_0",
        },
    ]

    comparison = compare_roots_from_annotation_store(
        annotation_store=store,
        candidate_run_ref=RUN_REF,
        baseline_root=baseline,
        bundle=bundle,
        case=bundle.cases[0],
        baseline_oracle_ref=ORACLE_REF,
    )
    assert comparison.verdict == "EQUIVALENT"
    assert comparison.candidate_run_ref == RUN_REF
    assert comparison.oracle_ref == ORACLE_REF
    assert comparison.runtime_manifest_sha256 == RUNTIME_SHA
    assert comparison.calibration_snapshot_sha256 == CALIBRATION_SHA
    assert comparison.annotation_revision_set_sha256 is not None
    serialized = comparison.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "segment_0" not in serialized
    assert "private_segment_root" not in serialized


@pytest.mark.parametrize(
    "scope_kind",
    ["prepare_maps", "prepare_metadata"],
)
def test_store_bound_compare_covers_prepare_global_artifacts(
    tmp_path: Path,
    scope_kind: str,
) -> None:
    store, _database, _staging = _insert_committed_run(tmp_path)
    bundle = _global_bundle(scope_kind)

    comparison = compare_roots_from_annotation_store(
        annotation_store=store,
        candidate_run_ref=RUN_REF,
        baseline_root=_global_baseline(tmp_path, scope_kind),
        bundle=bundle,
        case=bundle.cases[0],
        baseline_oracle_ref=ORACLE_REF,
    )

    assert comparison.verdict == "EQUIVALENT"


@pytest.mark.parametrize(
    ("scope_kind", "candidate_kwargs", "difference_code"),
    [
        (
            "prepare_maps",
            {"map_bytes": _png(b"changed")},
            "image_content_hash_mismatch",
        ),
        (
            "prepare_metadata",
            {"metadata_bytes": b'{"samples":["changed"]}\n'},
            "document_semantics_mismatch",
        ),
    ],
)
def test_store_bound_prepare_global_business_drift_is_different(
    tmp_path: Path,
    scope_kind: str,
    candidate_kwargs: dict[str, bytes],
    difference_code: str,
) -> None:
    store, _database, _staging = _insert_committed_run(
        tmp_path,
        **candidate_kwargs,
    )
    bundle = _global_bundle(scope_kind)

    comparison = compare_roots_from_annotation_store(
        annotation_store=store,
        candidate_run_ref=RUN_REF,
        baseline_root=_global_baseline(tmp_path, scope_kind),
        bundle=bundle,
        case=bundle.cases[0],
        baseline_oracle_ref=ORACLE_REF,
    )

    assert comparison.verdict == "DIFFERENT"
    assert any(
        difference.code == difference_code
        for difference in comparison.differences
    )


@pytest.mark.parametrize(
    ("candidate_extra", "baseline_extra", "difference_code", "relative_path"),
    [
        (True, False, "extra_artifact", "unexpected.bin"),
        (False, True, "missing_artifact", "legacy-only.bin"),
    ],
)
def test_store_bound_prepare_global_file_set_drift_is_precise(
    tmp_path: Path,
    candidate_extra: bool,
    baseline_extra: bool,
    difference_code: str,
    relative_path: str,
) -> None:
    store, _database, _staging = _insert_committed_run(
        tmp_path,
        extra_map_artifact=candidate_extra,
    )
    bundle = _global_bundle("prepare_maps")

    comparison = compare_roots_from_annotation_store(
        annotation_store=store,
        candidate_run_ref=RUN_REF,
        baseline_root=_global_baseline(
            tmp_path,
            "prepare_maps",
            extra_map_artifact=baseline_extra,
        ),
        bundle=bundle,
        case=bundle.cases[0],
        baseline_oracle_ref=ORACLE_REF,
    )

    assert comparison.verdict == "DIFFERENT"
    difference = next(
        item
        for item in comparison.differences
        if item.code == difference_code
    )
    assert difference.relative_path == relative_path
    assert difference.detail["stage"] == "map_publish"


@pytest.mark.parametrize("mutation", ["modified", "added", "missing"])
def test_store_bound_prepare_global_tampering_fails_attestation(
    tmp_path: Path,
    mutation: str,
) -> None:
    store, _database, staging = _insert_committed_run(tmp_path)
    map_path = staging / "maps" / "map.png"
    if mutation == "modified":
        map_path.write_bytes(_png(b"tampered"))
    elif mutation == "added":
        (staging / "maps" / "unexpected.bin").write_bytes(b"unexpected")
    else:
        map_path.unlink()
    bundle = _global_bundle("prepare_maps")

    with pytest.raises(GoldenError, match="rejected"):
        compare_roots_from_annotation_store(
            annotation_store=store,
            candidate_run_ref=RUN_REF,
            baseline_root=_global_baseline(tmp_path, "prepare_maps"),
            bundle=bundle,
            case=bundle.cases[0],
            baseline_oracle_ref=ORACLE_REF,
        )


def test_store_bound_prepare_global_rejects_unregistered_root_from_manifest(
    tmp_path: Path,
) -> None:
    store, _database, _staging = _insert_committed_run(
        tmp_path,
        extra_top_level="unexpected-business-root",
    )
    bundle = _global_bundle("prepare_metadata")

    with pytest.raises(GoldenError, match="rejected"):
        compare_roots_from_annotation_store(
            annotation_store=store,
            candidate_run_ref=RUN_REF,
            baseline_root=_global_baseline(tmp_path, "prepare_metadata"),
            bundle=bundle,
            case=bundle.cases[0],
            baseline_oracle_ref=ORACLE_REF,
        )


def test_existing_annotation_store_read_only_open_cannot_mutate(
    tmp_path: Path,
) -> None:
    _store, database, _staging = _insert_committed_run(tmp_path)
    read_only_store = AnnotationStore.open_existing_read_only(database)

    assert read_only_store.golden_candidate_binding(
        run_ref=RUN_REF,
        dataset_date="20270605",
        source_clip="clip_0",
        internal_segment="segment_0",
    )["run_ref"] == RUN_REF
    with pytest.raises(RuntimeError, match="read-only"):
        read_only_store.recover_interrupted_runs()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_date", "20270606"),
        ("candidate_clip", "clip_other"),
        ("candidate_segment", "segment_1"),
    ],
)
def test_store_bound_compare_rejects_case_identity_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store, _database, _staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    bundle = _bundle(**{field: value})

    with pytest.raises(GoldenError, match="rejected"):
        compare_roots_from_annotation_store(
            annotation_store=store,
            candidate_run_ref=RUN_REF,
            baseline_root=baseline,
            bundle=bundle,
            case=bundle.cases[0],
            baseline_oracle_ref=ORACLE_REF,
        )


def test_store_bound_compare_rejects_run_root_confusion(
    tmp_path: Path,
) -> None:
    store, database, _staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    outside = tmp_path / "other-run" / "segment_0"
    outside.mkdir(parents=True)
    (outside / "artifact.bin").write_bytes(b"same")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE annotation_segments
            SET private_segment_root = ?
            WHERE private_segment_key = 'segment_0'
            """,
            (str(outside),),
        )

    bundle = _bundle()
    with pytest.raises(GoldenError, match="rejected"):
        compare_roots_from_annotation_store(
            annotation_store=store,
            candidate_run_ref=RUN_REF,
            baseline_root=baseline,
            bundle=bundle,
            case=bundle.cases[0],
            baseline_oracle_ref=ORACLE_REF,
        )


def test_store_bound_compare_rejects_uncommitted_tracking_run(
    tmp_path: Path,
) -> None:
    store, database, _staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = 'queued'
            WHERE run_ref = ?
            """,
            (RUN_REF,),
        )

    bundle = _bundle()
    with pytest.raises(GoldenError, match="rejected"):
        compare_roots_from_annotation_store(
            annotation_store=store,
            candidate_run_ref=RUN_REF,
            baseline_root=baseline,
            bundle=bundle,
            case=bundle.cases[0],
            baseline_oracle_ref=ORACLE_REF,
        )


@pytest.mark.parametrize(
    "artifact_kind",
    ["prepared", "yaml", "tracking_image", "points"],
)
def test_store_bound_compare_rejects_candidate_artifact_tampering(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    store, _database, staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    segment = (
        staging
        / "samples"
        / "20270605"
        / "segment_0"
    )
    identity = "master_green_gray_white"
    paths = {
        "prepared": segment / "artifact.bin",
        "yaml": segment / f"{identity}.yaml",
        "tracking_image": (
            segment / f"tracking_img_{identity}" / "000001.jpg"
        ),
        "points": segment / f"img_{identity}.txt",
    }
    paths[artifact_kind].write_bytes(b"tampered")

    with pytest.raises(GoldenError, match="rejected"):
        compare_roots_from_annotation_store(
            annotation_store=store,
            candidate_run_ref=RUN_REF,
            baseline_root=baseline,
            bundle=_bundle(),
            case=_bundle().cases[0],
            baseline_oracle_ref=ORACLE_REF,
        )


def test_store_bound_retry_accepts_checkpoint_from_prior_tracking_attempt(
    tmp_path: Path,
) -> None:
    store, database, staging = _insert_committed_run(tmp_path)
    with sqlite3.connect(database) as connection:
        current_run_id = connection.execute(
            "SELECT id FROM runtime_runs WHERE run_ref = ?",
            (RUN_REF,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE runtime_runs SET attempt = 2 WHERE id = ?
            """,
            (current_run_id,),
        )
        prior_run_id = connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt,
                created_at, updated_at, finished_at
            )
            SELECT ?, job_id, 'tracking', 'failed', 1,
                   created_at, updated_at, updated_at
            FROM runtime_runs WHERE id = ?
            """,
            ("run_" + "9" * 32, current_run_id),
        ).lastrowid
        assert prior_run_id is not None
        connection.execute(
            "DROP TRIGGER tracking_checkpoints_no_update",
        )
        connection.execute(
            """
            UPDATE tracking_checkpoints SET run_id = ?
            WHERE run_id = ?
            """,
            (prior_run_id, current_run_id),
        )
        connection.execute(
            """
            UPDATE runtime_run_steps SET run_id = ?, ordinal = 1
            WHERE run_id = ?
              AND safe_step_code = 'tracking_target_completed'
            """,
            (prior_run_id, current_run_id),
        )

    attestation = store.runtime_run_attestation(RUN_REF)
    assert attestation["committed"] is True
    binding = store.golden_candidate_binding(
        run_ref=RUN_REF,
        dataset_date="20270605",
        source_clip="clip_0",
        internal_segment="segment_0",
    )
    assert binding["run_ref"] == RUN_REF

    (
        staging
        / "samples"
        / "20270605"
        / "segment_0"
        / "tracking_img_master_green_gray_white"
        / "000001.jpg"
    ).write_bytes(b"tampered-after-retry")
    with pytest.raises(
        RuntimeError,
        match="checkpoint artifacts changed",
    ):
        store.golden_candidate_binding(
            run_ref=RUN_REF,
            dataset_date="20270605",
            source_clip="clip_0",
            internal_segment="segment_0",
        )


@pytest.mark.parametrize(
    "scope_kind",
    ["prepare_maps", "prepare_metadata"],
)
def test_compare_annotation_run_cli_supports_prepare_global_equivalence(
    tmp_path: Path,
    capsys,
    scope_kind: str,
) -> None:
    _store, database, _staging = _insert_committed_run(tmp_path)
    bundle = _global_bundle(scope_kind)
    cases = tmp_path / f"{scope_kind}-cases.yaml"
    _write_cases(cases, bundle)
    baseline = _global_baseline(tmp_path, scope_kind)
    output = tmp_path / f"{scope_kind}-report"

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=baseline,
            output=output,
            case_id=bundle.cases[0].id,
        ),
    ) == 0

    report_json = (output / "comparison.json").read_text(encoding="utf-8")
    report_markdown = (output / "comparison.md").read_text(encoding="utf-8")
    report = json.loads(report_json)
    assert report["verdict"] == "EQUIVALENT"
    assert report["candidate_run_ref"] == RUN_REF
    for public_report in (report_json, report_markdown):
        assert str(tmp_path) not in public_report
        assert "segment_0" not in public_report
        assert "private_segment_root" not in public_report
    console = capsys.readouterr()
    assert str(tmp_path) not in console.out
    assert str(tmp_path) not in console.err


@pytest.mark.parametrize(
    ("scope_kind", "candidate_kwargs", "difference_code"),
    [
        (
            "prepare_maps",
            {"map_bytes": _png(b"changed")},
            "image_content_hash_mismatch",
        ),
        (
            "prepare_metadata",
            {"metadata_bytes": b'{"samples":["changed"]}\n'},
            "document_semantics_mismatch",
        ),
    ],
)
def test_compare_annotation_run_cli_reports_prepare_global_difference(
    tmp_path: Path,
    scope_kind: str,
    candidate_kwargs: dict[str, bytes],
    difference_code: str,
) -> None:
    _store, database, _staging = _insert_committed_run(
        tmp_path,
        **candidate_kwargs,
    )
    bundle = _global_bundle(scope_kind)
    cases = tmp_path / f"{scope_kind}-cases.yaml"
    _write_cases(cases, bundle)
    output = tmp_path / f"{scope_kind}-different"

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=_global_baseline(tmp_path, scope_kind),
            output=output,
            case_id=bundle.cases[0].id,
        ),
    ) == 1

    report = json.loads(
        (output / "comparison.json").read_text(encoding="utf-8"),
    )
    assert report["verdict"] == "DIFFERENT"
    assert any(
        difference["code"] == difference_code
        for difference in report["differences"]
    )


def test_compare_annotation_run_cli_rejects_prepare_global_output_inside_input(
    tmp_path: Path,
    capsys,
) -> None:
    _store, database, staging = _insert_committed_run(tmp_path)
    bundle = _global_bundle("prepare_maps")
    cases = tmp_path / "prepare-maps-cases.yaml"
    _write_cases(cases, bundle)
    output = staging / "unsafe-report"

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=_global_baseline(tmp_path, "prepare_maps"),
            output=output,
            case_id=bundle.cases[0].id,
        ),
    ) == 2
    assert not (output / "comparison.json").exists()
    assert not (output / "comparison.md").exists()
    error = capsys.readouterr().err
    assert str(tmp_path) not in error
    assert "refusing to write" in error


def test_compare_annotation_run_cli_safety_scans_prepare_global_reports(
    tmp_path: Path,
    capsys,
) -> None:
    _store, database, _staging = _insert_committed_run(tmp_path)
    bundle = _global_bundle("prepare_maps")
    cases = tmp_path / "prepare-maps-cases.yaml"
    _write_cases(cases, bundle)
    secret = "sk-abcdefghijklmnop"
    output = tmp_path / "unsafe-global-report"

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=_global_baseline(
                tmp_path,
                "prepare_maps",
                extra_map_name=secret,
            ),
            output=output,
            case_id=bundle.cases[0].id,
        ),
    ) == 2
    assert not (output / "comparison.json").exists()
    assert not (output / "comparison.md").exists()
    error = capsys.readouterr().err
    assert secret not in error
    assert str(tmp_path) not in error


def test_compare_annotation_run_cli_writes_only_safe_public_reports(
    tmp_path: Path,
    capsys,
) -> None:
    _store, database, _staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    baseline_before = (baseline / "oracle" / "legacy_segment" / "artifact.bin").read_bytes()
    cases = tmp_path / "cases.yaml"
    _write_cases(cases, _bundle())
    output = tmp_path / "public-report"

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=baseline,
            output=output,
        ),
    ) == 0
    assert (baseline / "oracle" / "legacy_segment" / "artifact.bin").read_bytes() == (
        baseline_before
    )
    report_json = (output / "comparison.json").read_text(encoding="utf-8")
    report_markdown = (output / "comparison.md").read_text(encoding="utf-8")
    report = json.loads(report_json)
    assert report["candidate_run_ref"] == RUN_REF
    assert report["oracle_ref"] == ORACLE_REF
    assert report["runtime_manifest_sha256"] == RUNTIME_SHA
    assert report["calibration_snapshot_sha256"] == CALIBRATION_SHA
    for public_report in (report_json, report_markdown):
        assert str(tmp_path) not in public_report
        assert "segment_0" not in public_report
        assert "private_segment_root" not in public_report
        assert "sk-abcdefghijklmnop" not in public_report
    console = capsys.readouterr()
    assert str(tmp_path) not in console.out
    assert str(tmp_path) not in console.err


def test_compare_annotation_run_cli_returns_one_for_difference(
    tmp_path: Path,
) -> None:
    _store, database, _staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    (
        baseline
        / "oracle"
        / "legacy_segment"
        / "artifact.bin"
    ).write_bytes(b"different")
    cases = tmp_path / "cases.yaml"
    _write_cases(cases, _bundle())

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=baseline,
            output=tmp_path / "different-report",
        ),
    ) == 1


def test_compare_annotation_run_cli_refuses_credential_like_report_content(
    tmp_path: Path,
    capsys,
) -> None:
    _store, database, staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    secret = "sk-abcdefghijklmnop"
    (
        staging
        / "samples"
        / "20270605"
        / "segment_0"
        / secret
    ).write_bytes(b"unsafe-name")
    cases = tmp_path / "cases.yaml"
    _write_cases(cases, _bundle())
    output = tmp_path / "unsafe-report"

    assert cli.main(
        _cli_args(
            cases=cases,
            database=database,
            baseline=baseline,
            output=output,
        ),
    ) == 2
    assert not (output / "comparison.json").exists()
    assert not (output / "comparison.md").exists()
    error = capsys.readouterr().err
    assert secret not in error
    assert str(tmp_path) not in error


def test_compare_annotation_run_cli_requires_existing_regular_database(
    tmp_path: Path,
    capsys,
) -> None:
    _store, database, _staging = _insert_committed_run(tmp_path)
    baseline = _baseline(tmp_path)
    cases = tmp_path / "cases.yaml"
    _write_cases(cases, _bundle())

    missing = tmp_path / "missing.sqlite"
    assert cli.main(
        _cli_args(
            cases=cases,
            database=missing,
            baseline=baseline,
            output=tmp_path / "missing-report",
        ),
    ) == 2
    assert not missing.exists()

    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(database)
    assert cli.main(
        _cli_args(
            cases=cases,
            database=linked,
            baseline=baseline,
            output=tmp_path / "linked-report",
        ),
    ) == 2

    fifo = tmp_path / "annotation.fifo"
    os.mkfifo(fifo)
    assert cli.main(
        _cli_args(
            cases=cases,
            database=fifo,
            baseline=baseline,
            output=tmp_path / "fifo-report",
        ),
    ) == 2
    error = capsys.readouterr().err
    assert str(tmp_path) not in error
