from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from vla_data_juicer_agents.annotation.runtime import _tree_sha256
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.golden.comparison import (
    compare_roots_from_annotation_store,
)
from vla_data_juicer_agents.navigation.golden.models import GoldenCaseBundle


POSTPROCESSING_RUN_REF = "run_" + "1" * 32
FIX_RUN_REF = "run_" + "2" * 32
RUNTIME_SHA = "3" * 64
PROCESSING_CALIBRATION_SHA = "4" * 64
FIX_CALIBRATION_SHA = "5" * 64
DATE = "20270623"
CLIP = "clip_0"
SEGMENT = "segment_0"
TIMESTAMP = "2026-07-28T00:00:00.000+00:00"
POSTPROCESSING_STEPS = [
    "postprocess_input_snapshot",
    "postprocess_metadata",
    "postprocess_gridmap",
    "postprocess_projection",
    "postprocess_world_coordinates",
    "postprocess_speed_direction",
    "postprocess_gridmap_transform",
    "postprocess_trajectory",
    "postprocess_final_candidate",
    "postprocess_validate_outputs",
]


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _insert_manifest(
    connection: sqlite3.Connection,
    *,
    manifest_ref: str,
    job_id: int,
    run_id: int,
    stage: str,
    manifest: dict,
) -> None:
    encoded = _canonical(manifest)
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
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            encoded,
            TIMESTAMP,
        ),
    )


def _insert_steps(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    steps: list[str],
) -> None:
    for ordinal, step in enumerate(steps, start=1):
        connection.execute(
            """
            INSERT INTO runtime_run_steps (
                run_id, ordinal, safe_step_code, status, return_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'succeeded', 0, ?, ?)
            """,
            (run_id, ordinal, step, TIMESTAMP, TIMESTAMP),
        )


def _build_committed_postprocessing(
    tmp_path: Path,
) -> tuple[AnnotationStore, Path, Path]:
    database = tmp_path / "annotation.sqlite"
    store = AnnotationStore(database)
    candidate_date_root = (
        tmp_path / "private" / "finish_data" / DATE
    )
    segment_root = candidate_date_root / CLIP / SEGMENT
    segment_root.mkdir(parents=True)
    trajectory_path = segment_root / f"{SEGMENT}_trajectory.json"
    trajectory_path.write_text(
        _canonical({"frames": [{"frame": 0, "position": [1.0, 2.0]}]}),
        encoding="utf-8",
    )
    (segment_root / f"{SEGMENT}_speed_direction.json").write_text(
        _canonical({"speed": [0.5], "direction": [1.2]}),
        encoding="utf-8",
    )
    compatibility_segment = (
        tmp_path / "published" / "finish_data" / DATE / CLIP / SEGMENT
    )
    shutil.copytree(segment_root, compatibility_segment)
    artifact_sha = _tree_sha256(
        segment_root,
        unsafe_code="test_artifact_changed",
    )
    candidate_tree_sha = _tree_sha256(
        candidate_date_root,
        unsafe_code="test_artifact_changed",
    )
    annotation_targets = [
        {
            "target_ref": "target_" + "a" * 32,
            "bbox": [1, 2, 3, 4],
            "point": [2, 3],
            "colors": {
                "upper": "black",
                "lower": "black",
                "shoes": "black",
            },
        }
    ]
    annotation_sha = _sha(annotation_targets)
    trajectory_state = {
        "frames": [{"frame_index": 0, "targets": {}}],
    }
    trajectory_state_sha = _sha(trajectory_state)
    spec = {
        "localization_kind": "odom",
        "gridmap_decision": "copy_existing_gridmap",
        "trajectory_variant": "cjl_0525_with_gridmap",
        "plan_sha256": "6" * 64,
        "observations_sha256": "7" * 64,
    }
    spec_sha = _sha(spec)
    revision_set = [
        {
            "segment_ref": "segment_" + "8" * 32,
            "annotation_revision_sha256": annotation_sha,
            "trajectory_sha256": hashlib.sha256(
                trajectory_path.read_bytes()
            ).hexdigest(),
            "artifact_sha256": artifact_sha,
        }
    ]
    manifest_ref = "artifact_manifest_" + "9" * 32
    manifest = {
        "runtime_manifest_sha256": RUNTIME_SHA,
        "postprocessing_spec_sha256": spec_sha,
        "plan_sha256": spec["plan_sha256"],
        "observations_sha256": spec["observations_sha256"],
        "input_tree_sha256": "a" * 64,
        "candidate_tree_sha256": candidate_tree_sha,
        "command_steps": POSTPROCESSING_STEPS,
        "revision_set": revision_set,
        "publication": {
            "source_clips": [CLIP],
            "journal_sha256": "b" * 64,
        },
    }
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        job_id = int(
            connection.execute(
                """
                INSERT INTO annotation_jobs (
                    job_ref, dataset_date, status, state_revision,
                    calibration_snapshot_id, staging_root, created_at, updated_at
                ) VALUES (?, ?, 'annotated', 5, NULL, ?, ?, ?)
                """,
                (
                    "job_" + "d" * 32,
                    DATE,
                    str(tmp_path / "tracked"),
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        calibration_id = int(
            connection.execute(
                """
                INSERT INTO calibration_snapshots (
                    snapshot_ref, job_id, profile_ref, label, content_sha256,
                    private_snapshot_dir, files_json, created_at
                ) VALUES (?, ?, 'processing_profile', 'processing_profile',
                          ?, ?, '[]', ?)
                """,
                (
                    "snapshot_" + "c" * 32,
                    job_id,
                    PROCESSING_CALIBRATION_SHA,
                    str(tmp_path / "calibration"),
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        connection.execute(
            "UPDATE annotation_jobs SET calibration_snapshot_id = ? WHERE id = ?",
            (calibration_id, job_id),
        )
        connection.execute(
            """
            INSERT INTO annotation_job_source_clips (
                job_id, ordinal, source_clip
            ) VALUES (?, 1, ?)
            """,
            (job_id, CLIP),
        )
        segment_id = int(
            connection.execute(
                """
                INSERT INTO annotation_segments (
                    segment_ref, job_id, ordinal, source_clip, status,
                    state_revision, draft_revision, submitted_revision,
                    private_segment_key, private_segment_root,
                    private_first_frame_path, first_frame_width,
                    first_frame_height, first_frame_sha256, first_frame_etag,
                    created_at, updated_at
                ) VALUES (?, ?, 1, ?, 'annotated', 5, 1, 1, ?, ?, ?,
                          1, 1, ?, ?, ?, ?)
                """,
                (
                    revision_set[0]["segment_ref"],
                    job_id,
                    CLIP,
                    SEGMENT,
                    str(tmp_path / "tracked" / SEGMENT),
                    str(tmp_path / "tracked" / SEGMENT / "frame.jpg"),
                    "e" * 64,
                    "e" * 64,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO initial_annotation_revisions (
                revision_ref, segment_id, revision_number, targets_json,
                content_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                "initial_revision_" + "f" * 32,
                segment_id,
                _canonical(annotation_targets),
                annotation_sha,
                TIMESTAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO postprocessing_specs (
                spec_ref, job_id, localization_kind, gridmap_decision,
                trajectory_variant, plan_sha256, observations_sha256,
                content_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "postprocessing_spec_" + "0" * 32,
                job_id,
                spec["localization_kind"],
                spec["gridmap_decision"],
                spec["trajectory_variant"],
                spec["plan_sha256"],
                spec["observations_sha256"],
                spec_sha,
                TIMESTAMP,
            ),
        )
        run_id = int(
            connection.execute(
                """
                INSERT INTO runtime_runs (
                    run_ref, job_id, kind, status, attempt, started_at,
                    finished_at, created_at, updated_at
                ) VALUES (?, ?, 'postprocessing', 'succeeded', 1, ?, ?, ?, ?)
                """,
                (
                    POSTPROCESSING_RUN_REF,
                    job_id,
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        _insert_steps(
            connection,
            run_id=run_id,
            steps=POSTPROCESSING_STEPS,
        )
        _insert_manifest(
            connection,
            manifest_ref=manifest_ref,
            job_id=job_id,
            run_id=run_id,
            stage="postprocessing",
            manifest=manifest,
        )
        trajectory_id = int(
            connection.execute(
                """
                INSERT INTO trajectory_revisions (
                    revision_ref, job_id, segment_id, revision_number,
                    content_sha256, private_artifact_path,
                    private_compatibility_path, artifact_sha256,
                    private_state_json, artifact_manifest_ref, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "trajectory_revision_" + "1" * 32,
                    job_id,
                    segment_id,
                    trajectory_state_sha,
                    str(segment_root),
                    str(compatibility_segment),
                    artifact_sha,
                    _canonical(trajectory_state),
                    manifest_ref,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO trajectory_review_tasks (
                review_ref, trajectory_revision_id, status, state_revision,
                created_at, updated_at
            ) VALUES (?, ?, 'pending', 0, ?, ?)
            """,
            ("review_" + "2" * 32, trajectory_id, TIMESTAMP, TIMESTAMP),
        )
    return store, database, segment_root


def _add_committed_fix(
    database: Path,
    postprocessing_segment_root: Path,
) -> Path:
    published_segment = (
        database.parent / "published" / "finish_data" / DATE / CLIP / SEGMENT
    )
    candidate_segment = (
        database.parent
        / "private"
        / "reviews"
        / ("review_" + "2" * 32)
        / "fix"
        / FIX_RUN_REF
        / "segment"
    )
    shutil.copytree(postprocessing_segment_root, candidate_segment)
    candidate_fix_path = (
        candidate_segment / f"{SEGMENT}_trajectory_fix_five.json"
    )
    fix_path = published_segment / f"{SEGMENT}_trajectory_fix_five.json"
    commands = [{"kind": "set_speed", "frame_index": 0, "value": 0.4}]
    fix_state = {
        "schema_version": 1,
        "trajectory_revision_state_sha256": "7" * 64,
        "calibration_snapshot_ref": "fix_calibration_" + "3" * 32,
        "calibration_profile_ref": "fix_profile",
        "commands": commands,
    }
    candidate_fix_path.write_text(_canonical(fix_state), encoding="utf-8")
    fix_path.write_bytes(candidate_fix_path.read_bytes())
    publication_sha = hashlib.sha256(fix_path.read_bytes()).hexdigest()
    draft_sha = _sha(fix_state)
    candidate_tree_sha = _tree_sha256(
        candidate_segment,
        unsafe_code="test_artifact_changed",
    )
    fix_manifest_ref = "artifact_manifest_" + "6" * 32
    fix_revision_ref = "fix_revision_" + "4" * 32
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        job_id = int(
            connection.execute(
                "SELECT id FROM annotation_jobs",
            ).fetchone()[0]
        )
        review = connection.execute(
            """
            SELECT r.id, r.trajectory_revision_id, r.review_ref,
                   t.revision_ref, t.artifact_sha256, t.private_state_json
            FROM trajectory_review_tasks r
            JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
            """,
        ).fetchone()
        review_id = int(review[0])
        calibration_id = int(
            connection.execute(
                """
                INSERT INTO fix_calibration_snapshots (
                    snapshot_ref, review_id, profile_ref, label,
                    content_sha256, private_snapshot_dir, files_json,
                    differs_from_processing, difference_reason, created_at
                ) VALUES (?, ?, 'fix_profile', 'fix_profile', ?, ?, '[]',
                          1, 'validated alternate calibration', ?)
                """,
                (
                    "fix_calibration_" + "3" * 32,
                    review_id,
                    FIX_CALIBRATION_SHA,
                    str(database.parent / "fix-calibration"),
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        draft_id = int(
            connection.execute(
                """
                INSERT INTO fix_drafts (
                    draft_ref, review_id,
                    calibration_snapshot_id, base_trajectory_revision_id,
                    draft_revision, original_state_json, state_json,
                    content_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    "fix_draft_" + "4" * 32,
                    review_id,
                    calibration_id,
                    int(review[1]),
                    review[5],
                    _canonical(fix_state),
                    draft_sha,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        run_id = int(
            connection.execute(
                """
                INSERT INTO runtime_runs (
                    run_ref, job_id, kind, status, attempt, started_at,
                    finished_at, created_at, updated_at
                ) VALUES (?, ?, 'fix', 'succeeded', 1, ?, ?, ?, ?)
                """,
                (
                    FIX_RUN_REF,
                    job_id,
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO runtime_run_review_links (
                run_id, review_id, fix_draft_id, source_draft_revision,
                planned_revision_ref, planned_revision_number, created_at
            ) VALUES (?, ?, ?, 1, ?, 1, ?)
            """,
            (run_id, review_id, draft_id, fix_revision_ref, TIMESTAMP),
        )
        _insert_steps(
            connection,
            run_id=run_id,
            steps=["fix_candidate"],
        )
        _insert_manifest(
            connection,
            manifest_ref=fix_manifest_ref,
            job_id=job_id,
            run_id=run_id,
            stage="fix",
            manifest={
                "runtime_manifest_sha256": RUNTIME_SHA,
                "trajectory_revision_ref": review[3],
                "base_tree_sha256": review[4],
                "calibration_snapshot_sha256": FIX_CALIBRATION_SHA,
                "draft_sha256": draft_sha,
                "command_log_sha256": _sha(commands),
                "adapter_sha256": "8" * 64,
                "candidate_tree_sha256": candidate_tree_sha,
                "fix_trajectory_sha256": publication_sha,
                "command_steps": ["fix_candidate"],
                "revision_set": [
                    {
                        "review_ref": review[2],
                        "segment_ref": "segment_" + "8" * 32,
                        "planned_revision_ref": fix_revision_ref,
                        "source_draft_revision": 1,
                    }
                ],
            },
        )
        fix_revision_id = int(
            connection.execute(
                """
                INSERT INTO fix_revisions (
                    revision_ref, review_id, revision_number,
                    calibration_snapshot_id, base_trajectory_revision_id,
                    source_draft_revision, state_json, content_sha256,
                    private_artifact_path, artifact_sha256,
                    artifact_manifest_ref, runtime_run_id, created_at
                ) VALUES (?, ?, 1, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fix_revision_ref,
                    review_id,
                    calibration_id,
                    int(review[1]),
                    _canonical(fix_state),
                    publication_sha,
                    str(candidate_segment),
                    candidate_tree_sha,
                    fix_manifest_ref,
                    run_id,
                    TIMESTAMP,
                ),
            ).lastrowid
        )
        connection.execute(
            """
            UPDATE trajectory_review_tasks
            SET status = 'approved', active_fix_draft_id = ?,
                approved_fix_revision_id = ?, state_revision = 2,
                updated_at = ?
            WHERE id = ?
            """,
            (draft_id, fix_revision_id, TIMESTAMP, review_id),
        )
        connection.execute(
            """
            INSERT INTO compatibility_publications (
                publication_ref, review_id, fix_revision_id, attempt,
                status, content_sha256, private_artifact_path,
                artifact_manifest_ref, created_at
            ) VALUES (?, ?, ?, 1, 'succeeded', ?, ?, ?, ?)
            """,
            (
                "publication_" + "5" * 32,
                review_id,
                fix_revision_id,
                publication_sha,
                str(fix_path),
                fix_manifest_ref,
                TIMESTAMP,
            ),
        )
    return published_segment


def _bundle(scope_kind: str) -> GoldenCaseBundle:
    expectation = (
        "*_trajectory.json"
        if scope_kind == "postprocessing_segment"
        else "*_trajectory_fix_five.json"
    )
    steps = (
        POSTPROCESSING_STEPS
        if scope_kind == "postprocessing_segment"
        else ["fix_candidate"]
    )
    return GoldenCaseBundle.model_validate(
        {
            "schema_version": 2,
            "runtime_id": "navigation_odom_v1",
            "cases": [
                {
                    "id": f"m2_{scope_kind}",
                    "artifact_root_kind": "finish_date",
                    "role_scopes": {
                        "legacy": {
                            "scope_kind": scope_kind,
                            "artifact_scope": SEGMENT,
                            "dataset_date": "20260623",
                            "source_clip": CLIP,
                            "internal_segment": SEGMENT,
                            "provenance": "historical_unattested",
                        },
                        "candidate": {
                            "scope_kind": scope_kind,
                            "artifact_scope": SEGMENT,
                            "dataset_date": DATE,
                            "source_clip": CLIP,
                            "internal_segment": SEGMENT,
                            "provenance": "runtime_attested",
                        },
                    },
                    "root_expectations": [
                        {
                            "modality": "trajectory",
                            "relative_pattern": expectation,
                            "present": True,
                        }
                    ],
                    "expected_command_steps": steps,
                    "candidate_attestation_required": True,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("scope_kind", "run_ref"),
    [
        ("postprocessing_segment", POSTPROCESSING_RUN_REF),
        ("fix_segment", FIX_RUN_REF),
    ],
)
def test_m2_golden_binding_uses_only_committed_store_evidence(
    tmp_path: Path,
    scope_kind: str,
    run_ref: str,
) -> None:
    store, database, postprocessing_segment = _build_committed_postprocessing(
        tmp_path
    )
    candidate_segment = postprocessing_segment
    if scope_kind == "fix_segment":
        candidate_segment = _add_committed_fix(
            database,
            postprocessing_segment,
        )
    baseline = tmp_path / f"baseline-{scope_kind}"
    shutil.copytree(candidate_segment, baseline / SEGMENT)
    bundle = _bundle(scope_kind)

    binding = store.golden_candidate_binding(
        run_ref=run_ref,
        dataset_date=DATE,
        source_clip=CLIP,
        internal_segment=SEGMENT,
        scope_kind=scope_kind,
    )

    assert Path(binding["staging_root"]) == candidate_segment.parent
    assert binding["artifact_scope"] == SEGMENT
    assert binding["scope_kind"] == scope_kind
    assert binding["attestation"] == store.runtime_run_attestation(run_ref)
    comparison = compare_roots_from_annotation_store(
        annotation_store=store,
        candidate_run_ref=run_ref,
        baseline_root=baseline,
        bundle=bundle,
        case=bundle.cases[0],
    )
    assert comparison.verdict == "EQUIVALENT"


def test_postprocessing_binding_rejects_revision_artifact_tampering(
    tmp_path: Path,
) -> None:
    store, _database, segment_root = _build_committed_postprocessing(tmp_path)
    (segment_root / f"{SEGMENT}_trajectory.json").write_text(
        _canonical({"frames": [{"frame": 0, "position": [9.0, 9.0]}]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="changed after commit"):
        store.golden_candidate_binding(
            run_ref=POSTPROCESSING_RUN_REF,
            dataset_date=DATE,
            source_clip=CLIP,
            internal_segment=SEGMENT,
            scope_kind="postprocessing_segment",
        )


def test_postprocessing_binding_rejects_publication_tampering(
    tmp_path: Path,
) -> None:
    store, _database, _segment_root = _build_committed_postprocessing(tmp_path)
    published_trajectory = (
        tmp_path
        / "published"
        / "finish_data"
        / DATE
        / CLIP
        / SEGMENT
        / f"{SEGMENT}_trajectory.json"
    )
    published_trajectory.write_text(
        _canonical({"frames": [{"frame": 0, "position": [9.0, 9.0]}]}),
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match="publication differs from committed trajectory",
    ):
        store.golden_candidate_binding(
            run_ref=POSTPROCESSING_RUN_REF,
            dataset_date=DATE,
            source_clip=CLIP,
            internal_segment=SEGMENT,
            scope_kind="postprocessing_segment",
        )


def test_fix_binding_rejects_publication_tampering(tmp_path: Path) -> None:
    store, database, postprocessing_segment = _build_committed_postprocessing(
        tmp_path
    )
    published_segment = _add_committed_fix(
        database,
        postprocessing_segment,
    )
    (published_segment / f"{SEGMENT}_trajectory_fix_five.json").write_text(
        _canonical({"commands": [{"kind": "delete_target"}]}),
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match="manifest, revision, and publication hashes differ",
    ):
        store.golden_candidate_binding(
            run_ref=FIX_RUN_REF,
            dataset_date=DATE,
            source_clip=CLIP,
            internal_segment=SEGMENT,
            scope_kind="fix_segment",
        )


def test_fix_binding_rejects_private_revision_tampering(tmp_path: Path) -> None:
    store, database, postprocessing_segment = _build_committed_postprocessing(
        tmp_path
    )
    _add_committed_fix(database, postprocessing_segment)
    candidate_fix = (
        tmp_path
        / "private"
        / "reviews"
        / ("review_" + "2" * 32)
        / "fix"
        / FIX_RUN_REF
        / "segment"
        / f"{SEGMENT}_trajectory_fix_five.json"
    )
    candidate_fix.write_text(
        _canonical({"commands": [{"kind": "delete_target"}]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="candidate changed after commit"):
        store.golden_candidate_binding(
            run_ref=FIX_RUN_REF,
            dataset_date=DATE,
            source_clip=CLIP,
            internal_segment=SEGMENT,
            scope_kind="fix_segment",
        )


def test_fix_binding_uses_frozen_revision_after_draft_advances(
    tmp_path: Path,
) -> None:
    store, database, postprocessing_segment = _build_committed_postprocessing(
        tmp_path
    )
    published_segment = _add_committed_fix(
        database,
        postprocessing_segment,
    )
    advanced_state = {
        "schema_version": 1,
        "commands": [{"kind": "delete_target", "frame_index": 1}],
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE fix_drafts
            SET draft_revision = 2, state_json = ?, content_sha256 = ?
            """,
            (_canonical(advanced_state), _sha(advanced_state)),
        )

    binding = store.golden_candidate_binding(
        run_ref=FIX_RUN_REF,
        dataset_date=DATE,
        source_clip=CLIP,
        internal_segment=SEGMENT,
        scope_kind="fix_segment",
    )

    assert Path(binding["staging_root"]) == published_segment.parent
