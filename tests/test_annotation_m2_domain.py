from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import vla_data_juicer_agents.annotation.store as annotation_store_module
from vla_data_juicer_agents.annotation.api import create_annotation_router
from vla_data_juicer_agents.annotation.application import (
    AnnotationApplicationService,
)
from vla_data_juicer_agents.annotation.catalog import CalibrationProfile
from vla_data_juicer_agents.annotation.catalog import CalibrationCatalog
from vla_data_juicer_agents.annotation.migrations import (
    AnnotationOfflineMigrationRequiredError,
    LATEST_ANNOTATION_SCHEMA_VERSION,
    _migration_001_annotation_m1,
    _migration_002_runtime_step_evidence,
    _migration_003_global_writer_quarantine_audit,
    _migration_004_annotation_m2_domain,
    _migration_005_processing_owner_and_safety_marker,
    prepare_annotation_migration_ledger,
)
from vla_data_juicer_agents.annotation.models import (
    ApplyFixCommandRequest,
    AnnotationConflictError,
    ApproveReviewRequest,
    CreateFixRevisionRequest,
    CreateFixSessionRequest,
    FixRuntimeState,
    PostprocessingSpecInput,
    ReturnReviewRequest,
)
from vla_data_juicer_agents.annotation.postprocessing_runtime import (
    POSTPROCESSING_COMMAND_STEPS,
    PostprocessingResult,
    PublicationResult,
    TrajectoryCandidate,
)
from vla_data_juicer_agents.annotation.fix_runtime import (
    CommandLogFixDraftAdapter,
    FixCompatibilityPublisher,
    FixResult,
)
from vla_data_juicer_agents.annotation.runtime import (
    RuntimeStepEvent,
    _sha256_file,
    _tree_sha256,
)
from vla_data_juicer_agents.annotation.trajectory_evidence import (
    render_gridmap_png,
)
from vla_data_juicer_agents.annotation.store import (
    AnnotationStore,
    migrate_annotation_store_offline,
)
from vla_data_juicer_agents.annotation.worker import AnnotationWorker


PROCESSING_SHA = "a" * 64
FIX_SHA = "b" * 64
TARGET_REF = "target_" + "1" * 32


def _canonical_state_sha(state: dict) -> str:
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FakeWorker:
    def __init__(self) -> None:
        self.stage_available = {
            "postprocessing": True,
            "fix": True,
        }
        self.stage_requests: list[tuple[str, dict | None]] = []

    @staticmethod
    def capabilities():
        return {"available": True, "runtime_id": "navigation_odom_v1"}

    def preflight_runtime_stage(self, stage, *, decision=None):
        self.stage_requests.append((stage, decision))
        if not self.stage_available[stage]:
            return {
                "available": False,
                "runtime_id": "navigation_odom_v1",
                "reason": {
                    "code": f"{stage}_runtime_preflight_failed",
                    "message": "The frozen test payload is unavailable.",
                },
            }
        return {
            "available": True,
            "runtime_id": "navigation_odom_v1",
            "runtime_manifest_sha256": "1" * 64,
        }


class FakeM2Catalog:
    processing = CalibrationProfile(
        profile_ref="20260529_go2w",
        label="20260529_go2w",
        content_sha256=PROCESSING_SHA,
        files=(),
    )
    fix = CalibrationProfile(
        profile_ref="20260409_U",
        label="20260409_U",
        content_sha256=FIX_SHA,
        files=(),
    )

    def list_profiles(self, *, purpose: str = "processing"):
        profile = self.processing if purpose == "processing" else self.fix
        return [profile.public_projection()]

    def get(self, profile_ref, expected_sha256, *, purpose="processing"):
        profile = self.processing if purpose == "processing" else self.fix
        assert profile_ref == profile.profile_ref
        assert expected_sha256 == profile.content_sha256
        return profile

    @staticmethod
    def snapshot(profile, destination: Path):
        destination.mkdir(parents=True)
        return [], profile.content_sha256


class DeterministicFakeFixRuntime:
    @staticmethod
    def initialize(trajectory_state, *, calibration_snapshot):
        state = {
            "trajectory": trajectory_state,
            "calibration_profile_ref": calibration_snapshot["profile_ref"],
            "commands": [],
        }
        return FixRuntimeState(
            state=state,
            content_sha256=_canonical_state_sha(state),
        )

    @staticmethod
    def apply(current_state, command):
        state = json.loads(json.dumps(current_state))
        state["commands"].append(command.model_dump(mode="json"))
        return FixRuntimeState(
            state=state,
            content_sha256=_canonical_state_sha(state),
        )


class BoundTestPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def publish_bound_revision(
        self,
        *,
        review_ref,
        revision_ref,
        candidate_segment_root,
        expected_candidate_tree_sha256,
        expected_fix_sha256,
        target_segment_root,
        journal_root,
        writer_lock_path,
    ):
        del review_ref, revision_ref, writer_lock_path
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated compatibility publication failure")
        assert _tree_sha256(candidate_segment_root) == (
            expected_candidate_tree_sha256
        )
        source = next(
            candidate_segment_root.glob("*_trajectory_fix_five.json")
        )
        assert _sha256_file(source) == expected_fix_sha256
        journal_root.mkdir(parents=True, exist_ok=True)
        (journal_root / "publication.json").write_text(
            '{"state":"committed"}\n',
            encoding="utf-8",
        )
        target = target_segment_root / source.name
        shutil.copyfile(source, target)
        return {
            "content_sha256": expected_fix_sha256,
            "private_artifact_path": str(target),
        }


class FakeM1Runtime:
    def __init__(self, writer_lock_path: Path) -> None:
        self.config = SimpleNamespace(writer_lock_path=writer_lock_path)

    @staticmethod
    def capabilities():
        return {"available": True, "runtime_id": "navigation_odom_v1"}


class FakePostprocessingRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def preflight(**_decision):
        return "1" * 64

    def run(self, request):
        for step in POSTPROCESSING_COMMAND_STEPS:
            request.step_observer(RuntimeStepEvent(step, "started"))
            request.step_observer(
                RuntimeStepEvent(step, "succeeded", return_code=0)
            )
        attempt = self.root / request.run_ref
        final = attempt / "final" / request.dataset_date
        candidates = []
        for segment in request.segments:
            candidate = (
                final / segment.source_clip / segment.private_segment_key
            )
            (candidate / "fisheye_front").mkdir(parents=True)
            (candidate / "grid_map").mkdir()
            (candidate / "rout_plot_v2").mkdir()
            frame_key = "1.000000"
            trajectory = candidate / (
                f"{segment.private_segment_key}_trajectory.json"
            )
            speed = candidate / (
                f"{segment.private_segment_key}_speed_direction.json"
            )
            trajectory.write_text(
                json.dumps(
                    {
                        frame_key: {
                            "master": {
                                "color": ["black", "black", "black"],
                                "img": [1, 2, 3, 4],
                                "traj": [[1.0, 2.0, 0.0]],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            speed.write_text(
                json.dumps(
                    {
                        frame_key: {
                            "speed_dog": [0.8, 0.0, 0.8],
                            "master": {
                                "speed_object": [1.0, 0.0, 1.0],
                                "direction_object": 0.5,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            (candidate / "master_black_black_black.txt").write_text(
                "1 2 0\n",
                encoding="utf-8",
            )
            (candidate / "fisheye_front" / f"{frame_key}.jpg").write_bytes(
                b"\xff\xd8"
                + b"\xff\xc0"
                + b"\x00\x07"
                + b"\x08"
                + (6).to_bytes(2, "big")
                + (8).to_bytes(2, "big")
                + b"\xff\xd9"
            )
            gridmap_content = json.dumps(
                {
                    "data": [-1, 0, 1, 2],
                    "grid_size": 2,
                    "resolution": 1,
                    "x_range": [0, 2],
                    "y_range": [0, 2],
                }
            ).encode()
            (candidate / "grid_map" / f"{frame_key}.json").write_bytes(
                gridmap_content
            )
            projection_png, _width, _height = render_gridmap_png(
                gridmap_content
            )
            (
                candidate / "rout_plot_v2" / f"{frame_key}.png"
            ).write_bytes(
                projection_png
            )
            candidates.append(
                TrajectoryCandidate(
                    segment_ref=segment.segment_ref,
                    source_clip=segment.source_clip,
                    private_segment_key=segment.private_segment_key,
                    candidate_segment_root=candidate,
                    trajectory_path=trajectory,
                    trajectory_sha256=_sha256_file(trajectory),
                    speed_direction_path=speed,
                    speed_direction_sha256=_sha256_file(speed),
                )
            )
        return PostprocessingResult(
            attempt_root=attempt,
            finish_temp_root=attempt / "finish_temp",
            final_candidate_root=final,
            trajectories=tuple(candidates),
            runtime_manifest_sha256=request.expected_runtime_manifest_sha256,
            input_tree_sha256="a" * 64,
            candidate_tree_sha256=_tree_sha256(final),
        )


class FakePostprocessingPublisher:
    def __init__(self, finish_data_root: Path) -> None:
        self.finish_data_root = finish_data_root
        self.finish_data_root.mkdir(parents=True)

    def preflight(self):
        return None

    def publish(self, *, dataset_date, items, journal_root, **_kwargs):
        for item in items:
            target = (
                self.finish_data_root
                / dataset_date
                / item.source_clip
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(item.candidate_root, target)
        journal = journal_root / "publication.json"
        journal.write_text('{"state":"committed"}\n', encoding="utf-8")
        return PublicationResult(
            committed_source_clips=tuple(
                item.source_clip for item in items
            ),
            journal_path=journal,
        )


class FakeBatchFixRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def preflight():
        return "1" * 64

    def run(self, request):
        request.step_observer(RuntimeStepEvent("fix_candidate", "started"))
        candidate = self.root / request.run_ref / "segment"
        shutil.copytree(request.base_segment_root, candidate)
        output = candidate / "sample_trajectory_fix_five.json"
        output.write_text('{"frame":{"pass":false}}\n', encoding="utf-8")
        request.step_observer(
            RuntimeStepEvent(
                "fix_candidate",
                "succeeded",
                return_code=0,
            )
        )
        command_log = json.dumps(
            list(request.commands),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return FixResult(
            attempt_root=candidate.parent,
            candidate_segment_root=candidate,
            fix_trajectory_path=output,
            fix_trajectory_sha256=_sha256_file(output),
            base_tree_sha256=request.expected_base_tree_sha256,
            calibration_snapshot_sha256=request.calibration_snapshot_sha256,
            command_log_sha256=hashlib.sha256(command_log).hexdigest(),
            adapter_sha256="c" * 64,
            runtime_manifest_sha256=request.expected_runtime_manifest_sha256,
        )


def _seed_tracked_job(store: AnnotationStore, tmp_path: Path) -> dict:
    staging_root = tmp_path / "work" / "jobs" / ("job_" + "2" * 32) / "tracked"
    segment_root = (
        staging_root
        / "samples"
        / "20270623"
        / "private-sequence"
    )
    tracking_output = segment_root / "tracking_img_master_black_black_black"
    tracking_output.mkdir(parents=True)
    (tracking_output / "000001.jpg").write_bytes(b"tracking")
    points_path = segment_root / "img_master_black_black_black.txt"
    points_path.write_text("1 2\n", encoding="utf-8")
    (segment_root / "master_black_black_black.txt").write_text(
        "1 2 0\n",
        encoding="utf-8",
    )
    targets = [
        {
            "target_ref": TARGET_REF,
            "bbox": [1, 2, 3, 4],
            "point": [2, 3],
            "colors": {
                "upper": "black",
                "lower": "black",
                "shoes": "black",
            },
        }
    ]
    targets_json = json.dumps(
        targets,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    targets_sha = hashlib.sha256(targets_json.encode("utf-8")).hexdigest()
    created = store.create_job(
        job_ref="job_" + "2" * 32,
        dataset_date="20270623",
        source_clips=["20260623_145550"],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": PROCESSING_SHA,
        },
        snapshot_dir=tmp_path / "processing-calibration",
        snapshot_files=[],
        reserved_bytes=100,
        idempotency_key="seed-job",
    )
    with store._write() as connection:
        job_id = connection.execute(
            "SELECT id FROM annotation_jobs WHERE job_ref = ?",
            (created["job_ref"],),
        ).fetchone()["id"]
        connection.execute(
            """
            UPDATE annotation_jobs
            SET status = 'tracked', state_revision = 1, staging_root = ?
            WHERE id = ?
            """,
            (str(staging_root), job_id),
        )
        connection.execute(
            """
            INSERT INTO annotation_segments (
                segment_ref, job_id, ordinal, source_clip, status,
                state_revision, draft_revision, submitted_revision,
                private_segment_key, private_segment_root,
                private_first_frame_path, first_frame_width,
                first_frame_height, first_frame_sha256, first_frame_etag,
                created_at, updated_at
            ) VALUES (
                ?, ?, 1, ?, 'tracked', 1, 1, 1, ?, ?, ?,
                1920, 1536, ?, ?, ?, ?
            )
            """,
            (
                "segment_" + "3" * 32,
                job_id,
                "20260623_145550",
                "private-sequence",
                str(segment_root),
                str(segment_root / "000001.jpg"),
                "c" * 64,
                "c" * 64,
                "2026-07-28T00:00:00+00:00",
                "2026-07-28T00:00:00+00:00",
            ),
        )
        segment_id = int(connection.execute(
            "SELECT id FROM annotation_segments WHERE job_id = ?",
            (job_id,),
        ).fetchone()["id"])
        connection.execute(
            """
            INSERT INTO initial_annotation_revisions (
                revision_ref, segment_id, revision_number, targets_json,
                content_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                "annotation_revision_" + "5" * 32,
                segment_id,
                targets_json,
                targets_sha,
                "2026-07-28T00:00:00+00:00",
            ),
        )
        tracking_run = connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt,
                started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, 'tracking', 'succeeded', 1, ?, ?, ?, ?)
            """,
            (
                "run_" + "6" * 32,
                job_id,
                "2026-07-28T00:00:00+00:00",
                "2026-07-28T00:00:00+00:00",
                "2026-07-28T00:00:00+00:00",
                "2026-07-28T00:00:00+00:00",
            ),
        )
        manifest = json.dumps(
            {
                "runtime_manifest_sha256": "7" * 64,
                "prepared_artifact_tree_sha256": "8" * 64,
                "command_steps": ["initial_annotation", "tracking"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO artifact_manifests (
                manifest_ref, job_id, run_id, stage, content_sha256,
                manifest_json, created_at
            ) VALUES (?, ?, ?, 'tracking', ?, ?, ?)
            """,
            (
                "artifact_manifest_" + "6" * 32,
                job_id,
                int(tracking_run.lastrowid),
                hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
                manifest,
                "2026-07-28T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO tracking_checkpoints (
                checkpoint_ref, job_id, run_id, segment_id, target_ref,
                revision_sha256, identity, private_output_dir,
                private_points_path, artifact_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tracking_checkpoint_" + "7" * 32,
                job_id,
                int(tracking_run.lastrowid),
                segment_id,
                TARGET_REF,
                targets_sha,
                "master_black_black_black",
                str(tracking_output),
                str(points_path),
                "9" * 64,
                "2026-07-28T00:00:00+00:00",
            ),
        )
    return store.get_job(created["job_ref"])


def _seed_scope_job(
    store: AnnotationStore,
    tmp_path: Path,
    *,
    job_ref: str,
    dataset_date: str,
    source_clips: list[str],
    idempotency_key: str,
) -> dict:
    return store.create_job(
        job_ref=job_ref,
        dataset_date=dataset_date,
        source_clips=source_clips,
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": PROCESSING_SHA,
        },
        snapshot_dir=tmp_path / f"{idempotency_key}-calibration",
        snapshot_files=[],
        reserved_bytes=100,
        idempotency_key=idempotency_key,
    )


def _complete_fake_postprocessing(
    service: AnnotationApplicationService,
    job: dict,
) -> dict:
    postprocessing = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="begin-postprocessing",
    )
    state = {
        "target_bindings": {TARGET_REF: "master"},
        "frames": [
            {
                "frame_index": 0,
                "targets": {
                    TARGET_REF: {
                        "position": [1.0, 2.0],
                        "direction": 0.0,
                        "speed": 1.0,
                        "pass": False,
                    }
                },
            }
        ]
    }
    compatibility_root = (
        service.work_root
        / "compatibility"
        / postprocessing["job_ref"]
        / postprocessing["segments"][0]["segment_ref"]
    )
    compatibility_root.mkdir(parents=True)
    return service.complete_postprocessing(
        job["job_ref"],
        postprocessing["state_revision"],
        [
            {
                "segment_ref": postprocessing["segments"][0]["segment_ref"],
                "state": state,
                "content_sha256": _canonical_state_sha(state),
                "private_artifact_path": "/private/trajectory.json",
                "private_compatibility_path": str(compatibility_root),
                "artifact_sha256": "f" * 64,
                "artifact_manifest_ref": "artifact_manifest_" + "4" * 32,
            }
        ],
        idempotency_key="complete-postprocessing",
    )


def _make_m2_client(tmp_path: Path):
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
        catalog=FakeM2Catalog(),
        work_root=tmp_path / "work",
        clip_data_root=tmp_path / "clip_data",
        fix_runtime=DeterministicFakeFixRuntime(),
    )
    job = _seed_tracked_job(store, tmp_path)
    _complete_fake_postprocessing(service, job)
    app = FastAPI()
    app.include_router(create_annotation_router(service))
    return TestClient(app), store


def test_public_domain_events_are_durable_ordered_and_redacted(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    initial = store.list_public_events_after(after_seq=0)

    assert initial
    assert [event["seq"] for event in initial] == sorted(
        event["seq"] for event in initial
    )
    assert store.public_event_cursor() == initial[-1]["seq"]
    assert {
        event["event_kind"] for event in initial
    } >= {
        "annotation.job.changed",
        "annotation.segment.changed",
    }
    assert all(event["job_ref"] == job["job_ref"] for event in initial)
    serialized = json.dumps(initial, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "private-sequence" not in serialized
    assert "database_id" not in serialized

    service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
        catalog=FakeM2Catalog(),
        work_root=tmp_path / "work",
        clip_data_root=tmp_path / "clip_data",
        fix_runtime=DeterministicFakeFixRuntime(),
    )
    _complete_fake_postprocessing(service, job)
    later = store.list_public_events_after(after_seq=initial[-1]["seq"])
    review_events = [
        event
        for event in later
        if event["event_kind"] == "annotation.review.changed"
    ]
    assert len(review_events) == 1
    assert review_events[0]["status"] == "pending"
    assert review_events[0]["job_ref"] == job["job_ref"]
    assert review_events[0]["segment_ref"] == job["segments"][0]["segment_ref"]
    assert review_events[0]["review_ref"].startswith("review_")
    assert store.list_public_events_after(after_seq=later[-1]["seq"]) == []

    with store._write() as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="annotation public events are immutable",
    ):
        connection.execute(
            "UPDATE annotation_public_events SET status = 'tampered' WHERE seq = 1"
        )


def test_public_event_cursor_api_and_resume_validation(tmp_path: Path) -> None:
    client, store = _make_m2_client(tmp_path)
    response = client.get("/api/annotation/events/cursor")
    assert response.status_code == 200
    assert response.json() == {"cursor": store.public_event_cursor()}

    invalid = client.get(
        "/api/annotation/events",
        headers={"Last-Event-ID": "not-a-cursor"},
    )
    assert invalid.status_code == 400
    assert client.get("/api/annotation/events?after_seq=-1").status_code == 422


def _freeze_test_fix_revision(
    client: TestClient,
    store: AnnotationStore,
    tmp_path: Path,
    *,
    key_prefix: str,
) -> tuple[dict, dict]:
    review = client.get("/api/annotation/reviews").json()["reviews"][0]
    session = client.post(
        f"/api/annotation/reviews/{review['review_ref']}/fix-sessions",
        headers={"Idempotency-Key": f"{key_prefix}-session"},
        json={
            "expected_review_revision": review["state_revision"],
            "calibration_profile_ref": "20260409_U",
            "calibration_content_sha256": FIX_SHA,
            "calibration_difference_reason": "test alternate calibration",
        },
    ).json()
    queued = client.post(
        f"/api/annotation/reviews/{review['review_ref']}/fix-revisions",
        headers={"Idempotency-Key": f"{key_prefix}-revision"},
        json={
            "expected_review_revision": session["state_revision"],
            "expected_draft_revision": session["fix_draft"]["revision"],
        },
    )
    assert queued.status_code == 201
    claimed = store.claim_next_run(
        worker_id=f"{key_prefix}-fix-worker",
        writer_lock_path=tmp_path / "writer.lock",
    )
    assert claimed is not None and claimed["kind"] == "fix"
    store.start_runtime_step(
        run_id=claimed["run_id"],
        safe_step_code="fix_candidate",
    )
    store.finish_runtime_step(
        run_id=claimed["run_id"],
        safe_step_code="fix_candidate",
        status="succeeded",
        return_code=0,
    )
    candidate = tmp_path / f"{key_prefix}-candidate"
    candidate.mkdir()
    fix_output = candidate / f"{key_prefix}_trajectory_fix_five.json"
    fix_output.write_text('{"frame":{}}\n', encoding="utf-8")
    completed = store.complete_fix_run(
        run_id=claimed["run_id"],
        candidate_segment_root=str(candidate),
        candidate_tree_sha256=_tree_sha256(candidate),
        fix_trajectory_sha256=_sha256_file(fix_output),
        manifest={
            "runtime_manifest_sha256": "1" * 64,
            "command_steps": ["fix_candidate"],
        },
    )
    return review, completed


def test_m2_migration_preserves_m1_rows(tmp_path: Path):
    database = tmp_path / "annotation.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    prepare_annotation_migration_ledger(connection)
    for version, name, migration in (
        (1, "annotation_m1", _migration_001_annotation_m1),
        (2, "runtime_step_evidence", _migration_002_runtime_step_evidence),
        (3, "global_writer_quarantine_audit", _migration_003_global_writer_quarantine_audit),
    ):
        migration(connection)
        connection.execute(
            """
            INSERT INTO annotation_schema_migrations(version, name, applied_at)
            VALUES (?, ?, '2026-07-28T00:00:00+00:00')
            """,
            (version, name),
        )
        connection.commit()
    connection.execute(
        """
        INSERT INTO annotation_jobs (
            job_ref, dataset_date, status, created_at, updated_at
        ) VALUES (?, '20270623', 'tracked', ?, ?)
        """,
        (
            "job_" + "9" * 32,
            "2026-07-28T00:00:00+00:00",
            "2026-07-28T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AnnotationOfflineMigrationRequiredError):
        AnnotationStore(database)
    migration = migrate_annotation_store_offline(
        database,
        backup_root=tmp_path / "migration-backup",
    )
    AnnotationStore(database)

    with sqlite3.connect(database) as migrated:
        assert migrated.execute(
            "SELECT status FROM annotation_jobs WHERE job_ref = ?",
            ("job_" + "9" * 32,),
        ).fetchone() == ("tracked",)
        versions = [
            row[0]
            for row in migrated.execute(
                "SELECT version FROM annotation_schema_migrations ORDER BY version"
            )
        ]
        assert versions == list(range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1))
        migrated.execute(
            "UPDATE annotation_jobs SET status = 'postprocessing'"
        )
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.execute(
            """
            SELECT schema_version, status
            FROM annotation_migration_safety
            WHERE singleton = 1
            """
        ).fetchone() == (LATEST_ANNOTATION_SCHEMA_VERSION, "verified")
        assert migrated.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table'
              AND name = 'annotation_processing_authorities'
            """
        ).fetchone() == (1,)
        assert migrated.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_annotation_task_links_processing_job'
            """
        ).fetchone() is None
        assert migrated.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'annotation_processing_authorities_guard_insert'
            """
        ).fetchone() == (1,)
    assert migration["from_version"] == 3
    assert migration["to_version"] == LATEST_ANNOTATION_SCHEMA_VERSION
    backup_manifest = json.loads(
        (tmp_path / "migration-backup" / "backup-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert backup_manifest["source_schema_versions"] == [1, 2, 3]
    assert {item["name"] for item in backup_manifest["files"]} >= {
        "annotation.sqlite"
    }


def test_failed_migration_integrity_check_stays_fail_closed_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "annotation.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    prepare_annotation_migration_ledger(connection)
    for version, name, migration in (
        (1, "annotation_m1", _migration_001_annotation_m1),
        (2, "runtime_step_evidence", _migration_002_runtime_step_evidence),
        (3, "global_writer_quarantine_audit", _migration_003_global_writer_quarantine_audit),
        (4, "annotation_m2_domain", _migration_004_annotation_m2_domain),
    ):
        migration(connection)
        connection.execute(
            """
            INSERT INTO annotation_schema_migrations(version, name, applied_at)
            VALUES (?, ?, '2026-07-28T00:00:00+00:00')
            """,
            (version, name),
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
    job_id = connection.execute(
        """
        INSERT INTO annotation_jobs (
            job_ref, dataset_date, status, created_at, updated_at
        ) VALUES (?, '20270703', 'preparing', ?, ?)
        """,
        (
            "job_" + "c" * 32,
            "2026-07-28T00:00:00+00:00",
            "2026-07-28T00:00:00+00:00",
        ),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO annotation_task_links (
            link_ref, job_id, review_id, navigation_task_ref,
            parent_navigation_task_ref, link_kind, created_at
        ) VALUES (?, ?, NULL, 'existing-m2-owner', NULL, 'processing', ?)
        """,
        (
            "annotation_task_link_" + "c" * 32,
            job_id,
            "2026-07-28T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    database.chmod(0o600)

    original_integrity_check = (
        annotation_store_module._annotation_database_integrity_results
    )
    monkeypatch.setattr(
        annotation_store_module,
        "_annotation_database_integrity_results",
        lambda _connection: ([], ["injected integrity failure"]),
    )
    with pytest.raises(
        RuntimeError,
        match="failed post-migration integrity checks",
    ):
        migrate_annotation_store_offline(
            database,
            backup_root=tmp_path / "migration-backup",
        )

    with sqlite3.connect(database) as failed:
        assert failed.execute(
            """
            SELECT version FROM annotation_schema_migrations
            ORDER BY version
            """
        ).fetchall() == [
            (version,)
            for version in range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1)
        ]
        assert failed.execute(
            """
            SELECT status FROM annotation_migration_safety
            WHERE singleton = 1
            """
        ).fetchone() == ("pending_integrity_check",)
        assert failed.execute(
            """
            SELECT navigation_task_ref
            FROM annotation_task_links
            WHERE link_kind = 'processing'
            """
        ).fetchone() == ("existing-m2-owner",)

    monkeypatch.setattr(
        annotation_store_module,
        "_annotation_database_integrity_results",
        original_integrity_check,
    )
    with pytest.raises(
        RuntimeError,
        match="migration safety verification is incomplete",
    ):
        AnnotationStore(database)


def test_v5_to_v6_migration_preserves_processing_lineage_and_pins_handoff(
    tmp_path: Path,
) -> None:
    database = tmp_path / "annotation.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    prepare_annotation_migration_ledger(connection)
    for version, name, migration in (
        (1, "annotation_m1", _migration_001_annotation_m1),
        (2, "runtime_step_evidence", _migration_002_runtime_step_evidence),
        (3, "global_writer_quarantine_audit", _migration_003_global_writer_quarantine_audit),
        (4, "annotation_m2_domain", _migration_004_annotation_m2_domain),
        (5, "processing_owner_and_safety_marker", _migration_005_processing_owner_and_safety_marker),
    ):
        migration(connection)
        connection.execute(
            """
            INSERT INTO annotation_schema_migrations(version, name, applied_at)
            VALUES (?, ?, '2026-07-28T00:00:00+00:00')
            """,
            (version, name),
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
    job_id = int(
        connection.execute(
            """
            INSERT INTO annotation_jobs (
                job_ref, dataset_date, status, created_at, updated_at
            ) VALUES (?, '20270704', 'tracked', ?, ?)
            """,
            (
                "job_" + "d" * 32,
                "2026-07-28T00:00:00+00:00",
                "2026-07-28T00:00:00+00:00",
            ),
        ).lastrowid
    )
    link_id = int(
        connection.execute(
            """
            INSERT INTO annotation_task_links (
                link_ref, job_id, review_id, navigation_task_ref,
                parent_navigation_task_ref, link_kind, created_at
            ) VALUES (?, ?, NULL, ?, NULL, 'processing', ?)
            """,
            (
                "annotation_task_link_" + "d" * 32,
                job_id,
                "historical-navigation-attempt",
                "2026-07-28T00:00:00+00:00",
            ),
        ).lastrowid
    )
    handoff_id = int(
        connection.execute(
            """
            INSERT INTO workflow_handoffs (
                handoff_ref, job_id, review_id, kind, payload_json,
                content_sha256, created_at
            ) VALUES (?, ?, NULL, 'tracking_completed', '{}', ?, ?)
            """,
            (
                "handoff_" + "d" * 32,
                job_id,
                hashlib.sha256(b"{}").hexdigest(),
                "2026-07-28T00:00:00+00:00",
            ),
        ).lastrowid
    )
    connection.execute(
        """
        UPDATE annotation_migration_safety
        SET status = 'verified', verified_at = ?
        WHERE singleton = 1
        """,
        ("2026-07-28T00:00:00+00:00",),
    )
    connection.commit()
    connection.close()
    database.chmod(0o600)

    result = migrate_annotation_store_offline(
        database,
        backup_root=tmp_path / "v6-migration-backup",
    )
    assert result["from_version"] == 5
    assert result["to_version"] == LATEST_ANNOTATION_SCHEMA_VERSION
    with sqlite3.connect(database) as migrated:
        assert migrated.execute(
            """
            SELECT job_id, link_id
            FROM annotation_processing_authorities
            """
        ).fetchone() == (job_id, link_id)
        assert migrated.execute(
            """
            SELECT handoff_id, link_id
            FROM workflow_handoff_processing_links
            """
        ).fetchone() == (handoff_id, link_id)


def test_processing_scope_requires_exact_clips_and_uses_job_order(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_scope_job(
        store,
        tmp_path,
        job_ref="job_" + "a" * 32,
        dataset_date="20270701",
        source_clips=["clip-b", "clip-a"],
        idempotency_key="exact-scope-job",
    )

    reordered_facts = store.get_processing_facts(
        dataset_date="20270701",
        source_clips=["clip-a", "clip-b"],
    )
    assert reordered_facts["exists"] is True
    assert reordered_facts["source_clips"] == ["clip-b", "clip-a"]
    assert store.resolve_scope_binding(
        dataset_date="20270701",
        source_clips=["clip-a", "clip-b"],
    )["job_ref"] == job["job_ref"]

    for mismatched_scope in (
        ["clip-a"],
        ["clip-a", "clip-b", "clip-c"],
        ["clip-a", "clip-a"],
    ):
        with pytest.raises(AnnotationConflictError) as facts_error:
            store.get_processing_facts(
                dataset_date="20270701",
                source_clips=mismatched_scope,
            )
        assert facts_error.value.code == "annotation_scope_mismatch"
        with pytest.raises(AnnotationConflictError) as binding_error:
            store.resolve_scope_binding(
                dataset_date="20270701",
                source_clips=mismatched_scope,
            )
        assert binding_error.value.code == "annotation_scope_mismatch"


def test_workflow_handoffs_do_not_overtake_an_earlier_running_delivery(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_scope_job(
        store,
        tmp_path,
        job_ref="job_" + "c" * 32,
        dataset_date="20270703",
        source_clips=["clip-ordered"],
        idempotency_key="ordered-handoff-job",
    )
    store.link_navigation_task(
        job_ref=job["job_ref"],
        review_ref=None,
        navigation_task_ref="navigation-ordered",
        parent_navigation_task_ref=None,
        link_kind="processing",
        idempotency_key="ordered-handoff-link",
    )
    initial = store.create_workflow_handoff(
        job_ref=job["job_ref"],
        review_ref=None,
        kind="initial_annotation_submitted",
        payload={"status": "submitted"},
        idempotency_key="ordered-initial-handoff",
    )
    tracking = store.create_workflow_handoff(
        job_ref=job["job_ref"],
        review_ref=None,
        kind="tracking_completed",
        payload={"status": "tracked"},
        idempotency_key="ordered-tracking-handoff",
    )

    claimed_initial = store.claim_workflow_handoff_delivery(
        worker_id="ordered-worker-a",
    )
    assert claimed_initial is not None
    assert claimed_initial["handoff_ref"] == initial["handoff_ref"]
    assert (
        store.claim_workflow_handoff_delivery(worker_id="ordered-worker-b")
        is None
    )

    store.complete_workflow_handoff_delivery(
        handoff_id=int(claimed_initial["handoff_id"]),
        worker_id="ordered-worker-a",
        success=True,
    )
    claimed_tracking = store.claim_workflow_handoff_delivery(
        worker_id="ordered-worker-b",
    )
    assert claimed_tracking is not None
    assert claimed_tracking["handoff_ref"] == tracking["handoff_ref"]


def test_processing_link_blocks_active_run_then_preserves_failed_attempt_lineage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "annotation.sqlite"
    store = AnnotationStore(database)
    job = _seed_scope_job(
        store,
        tmp_path,
        job_ref="job_" + "b" * 32,
        dataset_date="20270702",
        source_clips=["clip-owner"],
        idempotency_key="owner-scope-job",
    )
    session_a = AnnotationStore.open_existing_mutable(database)
    session_b = AnnotationStore.open_existing_mutable(database)
    barrier = threading.Barrier(2)

    def link(session: AnnotationStore, owner: str) -> tuple[str, str]:
        barrier.wait()
        try:
            session.link_navigation_task(
                job_ref=job["job_ref"],
                review_ref=None,
                navigation_task_ref=owner,
                parent_navigation_task_ref=None,
                link_kind="processing",
                idempotency_key=f"processing-owner:{owner}",
            )
        except AnnotationConflictError as exc:
            return owner, exc.code
        return owner, "linked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda arguments: link(*arguments),
                (
                    (session_a, "navigation-owner-a"),
                    (session_b, "navigation-owner-b"),
                ),
            )
        )

    assert sorted(status for _owner, status in results) == [
        "annotation_processing_active_attempt_conflict",
        "linked",
    ]
    winner = next(owner for owner, status in results if status == "linked")
    retry = session_b.link_navigation_task(
        job_ref=job["job_ref"],
        review_ref=None,
        navigation_task_ref=winner,
        parent_navigation_task_ref=None,
        link_kind="processing",
        idempotency_key="processing-owner-idempotent-retry",
    )
    assert retry == {"linked": True, "link_kind": "processing"}
    pinned_handoff = store.create_workflow_handoff(
        job_ref=job["job_ref"],
        review_ref=None,
        kind="initial_annotation_submitted",
        payload={"status": "submitted"},
        idempotency_key="pinned-first-owner-handoff",
    )

    with sqlite3.connect(database) as connection:
        job_id = connection.execute(
            "SELECT id FROM annotation_jobs WHERE job_ref = ?",
            (job["job_ref"],),
        ).fetchone()[0]
        assert connection.execute(
            """
            SELECT navigation_task_ref
            FROM annotation_task_links
            WHERE job_id = ? AND link_kind = 'processing'
            """,
            (job_id,),
        ).fetchall() == [(winner,)]
    active = store.claim_next_run(worker_id="failed-attempt-worker")
    assert active is not None
    store.fail_run(
        run_id=int(active["run_id"]),
        code="injected_prepare_failure",
        message="Injected prepare failure.",
        retryable=True,
    )
    successor = "navigation-owner-successor"
    assert session_a.link_navigation_task(
        job_ref=job["job_ref"],
        review_ref=None,
        navigation_task_ref=successor,
        parent_navigation_task_ref=None,
        link_kind="processing",
        idempotency_key="processing-owner-successor",
    ) == {"linked": True, "link_kind": "processing"}
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT navigation_task_ref
            FROM annotation_task_links
            WHERE job_id = ? AND link_kind = 'processing'
            ORDER BY id
            """,
            (job_id,),
        ).fetchall() == [(winner,), (successor,)]
        assert connection.execute(
            """
            SELECT l.navigation_task_ref
            FROM annotation_processing_authorities a
            JOIN annotation_task_links l ON l.id = a.link_id
            WHERE a.job_id = ?
            """,
            (job_id,),
        ).fetchone() == (successor,)

    first_claim = store.claim_workflow_handoff_delivery(
        worker_id="first-handoff-worker",
    )
    assert first_claim is not None
    assert first_claim["handoff_ref"] == pinned_handoff["handoff_ref"]
    assert first_claim["navigation_task_ref"] == winner
    store.complete_workflow_handoff_delivery(
        handoff_id=int(first_claim["handoff_id"]),
        worker_id="first-handoff-worker",
        success=True,
    )

    handoff = store.create_workflow_handoff(
        job_ref=job["job_ref"],
        review_ref=None,
        kind="tracking_completed",
        payload={"status": "tracked"},
        idempotency_key="single-owner-handoff",
    )
    claimed = store.claim_workflow_handoff_delivery(worker_id="handoff-worker")
    assert claimed is not None
    assert claimed["handoff_ref"] == handoff["handoff_ref"]
    assert claimed["navigation_task_ref"] == successor


def test_superseded_processing_attempt_cannot_retake_authority_at_begin(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    with store._write() as connection:
        connection.execute(
            """
            UPDATE runtime_runs
            SET status = 'succeeded',
                started_at = COALESCE(started_at, created_at),
                finished_at = COALESCE(finished_at, created_at)
            WHERE kind = 'prepare' AND status = 'queued'
            """
        )
    historical_attempt = "navigation-historical-attempt"
    current_attempt = "navigation-current-attempt"
    for attempt in (historical_attempt, current_attempt):
        assert store.link_navigation_task(
            job_ref=job["job_ref"],
            review_ref=None,
            navigation_task_ref=attempt,
            parent_navigation_task_ref=None,
            link_kind="processing",
            idempotency_key=f"link:{attempt}",
        ) == {"linked": True, "link_kind": "processing"}

    spec = {
        "localization_kind": "odom",
        "gridmap_decision": "copy_existing_gridmap",
        "trajectory_variant": "cjl_0525_with_gridmap",
        "plan_sha256": "d" * 64,
        "observations_sha256": "e" * 64,
    }
    with pytest.raises(AnnotationConflictError) as raised:
        store.begin_postprocessing(
            job_ref=job["job_ref"],
            expected_job_revision=job["state_revision"],
            spec=spec,
            idempotency_key="superseded-begin",
            processing_navigation_task_ref=historical_attempt,
        )
    assert raised.value.code == "annotation_processing_attempt_superseded"
    assert store.get_job(job["job_ref"])["status"] == "tracked"

    started = store.begin_postprocessing(
        job_ref=job["job_ref"],
        expected_job_revision=job["state_revision"],
        spec=spec,
        idempotency_key="authoritative-begin",
        processing_navigation_task_ref=current_attempt,
    )
    assert started["status"] == "postprocessing"
    with store._connect() as connection:
        assert connection.execute(
            """
            SELECT l.navigation_task_ref
            FROM annotation_processing_authorities a
            JOIN annotation_task_links l ON l.id = a.link_id
            """
        ).fetchone()[0] == current_attempt


def test_worker_executes_store_bound_postprocessing_and_creates_review(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
    )
    started = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="worker-postprocessing",
    )
    writer_lock = tmp_path / "writer.lock"
    worker = AnnotationWorker(
        store,
        FakeM1Runtime(writer_lock),
        postprocessing_runtime=FakePostprocessingRuntime(
            tmp_path / "post-runtime",
        ),
        postprocessing_publisher=FakePostprocessingPublisher(
            tmp_path / "finish_data",
        ),
    )

    assert asyncio.run(worker.run_once()) is True

    completed = store.get_job(started["job_ref"])
    assert completed["status"] == "annotated"
    reviews = store.list_reviews()
    assert len(reviews) == 1
    assert reviews[0]["status"] == "pending"
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            """
            SELECT status FROM runtime_runs
            WHERE kind = 'postprocessing'
            """
        ).fetchone()
        trajectory = connection.execute(
            """
            SELECT private_artifact_path, artifact_sha256,
                   artifact_manifest_ref
            FROM trajectory_revisions
            """
        ).fetchone()
    assert run["status"] == "succeeded"
    assert Path(trajectory["private_artifact_path"]).is_dir()
    assert len(trajectory["artifact_sha256"]) == 64
    assert trajectory["artifact_manifest_ref"].startswith(
        "artifact_manifest_"
    )
    app = FastAPI()
    app.include_router(create_annotation_router(service))
    with TestClient(app) as client:
        review_ref = reviews[0]["review_ref"]
        evidence_response = client.get(
            f"/api/annotation/reviews/{review_ref}/evidence/trajectory"
        )
        assert evidence_response.status_code == 200
        evidence = evidence_response.json()
        assert set(evidence) == {
            "availability",
            "review_ref",
            "evidence_kind",
            "fix_revision_ref",
            "fix_revision_source_draft_revision",
            "trajectory_revision_ref",
            "review_state_revision",
            "draft_revision",
            "frame_count",
            "frames",
            "draft_commands",
        }
        assert evidence["frames"][0]["camera"] == {
            "url": (
                f"/api/annotation/reviews/{review_ref}/evidence/"
                "frames/0/camera"
            ),
            "width": 8,
            "height": 6,
        }
        assert evidence["frames"][0]["gridmap"] == {
            "url": (
                f"/api/annotation/reviews/{review_ref}/evidence/"
                "frames/0/gridmap"
            ),
            "width": 2,
            "height": 2,
            "resolution": 1.0,
            "x_range": [0.0, 2.0],
            "y_range": [0.0, 2.0],
        }
        assert evidence["frames"][0]["projection"] == {
            "url": (
                f"/api/annotation/reviews/{review_ref}/evidence/"
                "frames/0/projection"
            ),
            "width": 2,
            "height": 2,
        }
        projection = client.get(
            evidence["frames"][0]["projection"]["url"]
        )
        assert projection.status_code == 200
        assert projection.headers["content-type"] == "image/png"
        gridmap = client.get(evidence["frames"][0]["gridmap"]["url"])
        assert gridmap.status_code == 200
        assert gridmap.headers["content-type"] == "image/png"
        assert gridmap.headers["x-content-type-options"] == "nosniff"
        assert gridmap.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert int.from_bytes(gridmap.content[16:20], "big") == 2
        assert int.from_bytes(gridmap.content[20:24], "big") == 2
        assert "/media/" not in evidence_response.text
        assert str(tmp_path) not in evidence_response.text



def test_postprocessing_cancel_before_publish_does_not_write_finish_data(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    service = AnnotationApplicationService(store=store, worker=FakeWorker())
    started = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="worker-postprocessing-cancel-fence",
    )

    class CancellingRuntime(FakePostprocessingRuntime):
        def run(self, request):
            result = super().run(request)
            current = store.get_job(request.job_ref)
            store.cancel_job(
                job_ref=request.job_ref,
                expected_job_revision=current["state_revision"],
                idempotency_key="cancel-before-postprocessing-publication",
            )
            return result

    class RecordingPublisher(FakePostprocessingPublisher):
        calls = 0

        def publish(self, **kwargs):
            self.calls += 1
            return super().publish(**kwargs)

    finish_root = tmp_path / "finish_data"
    publisher = RecordingPublisher(finish_root)
    worker = AnnotationWorker(
        store,
        FakeM1Runtime(tmp_path / "writer.lock"),
        postprocessing_runtime=CancellingRuntime(tmp_path / "post-runtime"),
        postprocessing_publisher=publisher,
    )

    assert asyncio.run(worker.run_once()) is True

    cancelled = store.get_job(started["job_ref"])
    assert cancelled["status"] == "cancelled"
    assert publisher.calls == 0
    assert list(finish_root.iterdir()) == []
    assert store.list_reviews() == []


def test_postprocessing_publication_fence_rejects_late_cancel(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    service = AnnotationApplicationService(store=store, worker=FakeWorker())
    started = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="worker-postprocessing-publication-fence",
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingPublisher(FakePostprocessingPublisher):
        def publish(self, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test publication release timed out")
            return super().publish(**kwargs)

    finish_root = tmp_path / "finish_data"
    worker = AnnotationWorker(
        store,
        FakeM1Runtime(tmp_path / "writer.lock"),
        postprocessing_runtime=FakePostprocessingRuntime(
            tmp_path / "post-runtime",
        ),
        postprocessing_publisher=BlockingPublisher(finish_root),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, worker.run_once())
        assert entered.wait(timeout=5)
        current = store.get_job(started["job_ref"])
        try:
            with pytest.raises(AnnotationConflictError) as failure:
                store.cancel_job(
                    job_ref=started["job_ref"],
                    expected_job_revision=current["state_revision"],
                    idempotency_key="late-cancel-after-publication-fence",
                )
            assert (
                failure.value.code
                == "postprocessing_publication_in_progress"
            )
        finally:
            release.set()
        assert future.result(timeout=5) is True

    completed = store.get_job(started["job_ref"])
    assert completed["status"] == "annotated"
    assert len(store.list_reviews()) == 1
    assert any(finish_root.rglob("*_trajectory.json"))


def test_worker_generates_store_bound_fix_revision_from_command_log(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
    )
    service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="worker-fix-postprocessing",
    )
    writer_lock = tmp_path / "writer.lock"
    post_worker = AnnotationWorker(
        store,
        FakeM1Runtime(writer_lock),
        postprocessing_runtime=FakePostprocessingRuntime(
            tmp_path / "post-runtime",
        ),
        postprocessing_publisher=FakePostprocessingPublisher(
            tmp_path / "finish_data",
        ),
    )
    assert asyncio.run(post_worker.run_once()) is True

    review = store.list_reviews()[0]
    fix_service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
        catalog=FakeM2Catalog(),
        work_root=tmp_path / "work",
        fix_runtime=CommandLogFixDraftAdapter(),
    )
    started = fix_service.create_fix_session(
        review["review_ref"],
        CreateFixSessionRequest(
            expected_review_revision=review["state_revision"],
            calibration_profile_ref="20260409_U",
            calibration_content_sha256=FIX_SHA,
            calibration_difference_reason="verified alternate calibration",
        ),
        idempotency_key="worker-fix-session",
    )
    changed = fix_service.apply_fix_command(
        review["review_ref"],
        ApplyFixCommandRequest.model_validate(
            {
                "expected_review_revision": started["state_revision"],
                "expected_draft_revision": started["fix_draft"]["revision"],
                "command": {
                    "kind": "set_speed",
                    "frame_index": 0,
                    "target_ref": TARGET_REF,
                    "speed": 2.5,
                },
            }
        ),
        idempotency_key="worker-fix-command",
    )
    queued = fix_service.create_fix_revision(
        review["review_ref"],
        CreateFixRevisionRequest(
            expected_review_revision=changed["state_revision"],
            expected_draft_revision=changed["fix_draft"]["revision"],
        ),
        idempotency_key="worker-fix-revision",
    )
    assert queued["active_fix_run"]["status"] == "queued"
    assert "run_ref" not in queued["active_fix_run"]
    assert "draft_ref" not in queued["fix_draft"]
    assert "submitted_fix_run_ref" not in queued
    with pytest.raises(
        AnnotationConflictError,
        match="frozen while a Fix revision",
    ):
        store.decide_review(
            operation="discard",
            review_ref=review["review_ref"],
            expected_review_revision=queued["state_revision"],
            reason="must not race the frozen Runtime",
            idempotency_key="discard-active-fix",
        )

    fix_worker = AnnotationWorker(
        store,
        FakeM1Runtime(writer_lock),
        fix_runtime=FakeBatchFixRuntime(tmp_path / "fix-runtime"),
    )
    assert asyncio.run(fix_worker.run_once()) is True

    completed = store.get_review(review["review_ref"])
    assert completed["active_fix_run"] is None
    assert len(completed["fix_revisions"]) == 1
    assert completed["fix_revisions"][0]["content_sha256"] == _sha256_file(
        next((tmp_path / "fix-runtime").glob(
            "*/segment/*_trajectory_fix_five.json"
        ))
    )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            """
            SELECT status FROM runtime_runs
            WHERE kind = 'fix'
            """
        ).fetchone() == ("succeeded",)


def test_failed_fix_run_is_safe_to_retry_without_reopening_the_job(
    tmp_path: Path,
) -> None:
    client, store = _make_m2_client(tmp_path)
    with client:
        review = client.get("/api/annotation/reviews").json()["reviews"][0]
        started = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-sessions",
            headers={"Idempotency-Key": "failed-fix-session"},
            json={
                "expected_review_revision": review["state_revision"],
                "calibration_profile_ref": "20260409_U",
                "calibration_content_sha256": FIX_SHA,
                "calibration_difference_reason": "verified alternate calibration",
            },
        ).json()
        queued = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-revisions",
            headers={"Idempotency-Key": "failed-fix-submit"},
            json={
                "expected_review_revision": started["state_revision"],
                "expected_draft_revision": started["fix_draft"]["revision"],
            },
        ).json()

        claimed = store.claim_next_run(
            worker_id="failed-fix-worker",
            writer_lock_path=tmp_path / "writer.lock",
        )
        assert claimed is not None and claimed["kind"] == "fix"
        store.fail_run(
            run_id=claimed["run_id"],
            code="fix_runtime_failed",
            message="The frozen Fix Runtime failed.",
            retryable=True,
        )
        failed = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        ).json()
        assert failed["status"] == "in_progress"
        assert failed["active_fix_run"]["status"] == "failed"
        assert failed["fix_failure"] == {
            "code": "fix_runtime_failed",
            "message": "The frozen Fix Runtime failed.",
            "error_ref": failed["fix_failure"]["error_ref"],
            "retryable": True,
        }
        assert store.get_job(failed["job_ref"])["status"] == "annotated"

        retried = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-revisions",
            headers={"Idempotency-Key": "failed-fix-retry"},
            json={
                "expected_review_revision": failed["state_revision"],
                "expected_draft_revision": failed["fix_draft"]["revision"],
            },
        )
        assert retried.status_code == 201
        retry_state = retried.json()
        assert retry_state["active_fix_run"]["status"] == "queued"
        assert retry_state["fix_failure"] is None
        assert retry_state["fix_revisions"] == []
        blocked = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/return",
            headers={"Idempotency-Key": "return-active-retry"},
            json={
                "expected_review_revision": retry_state["state_revision"],
                "reason": "must not race the retry",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "fix_runtime_already_active"


def test_gridmap_renderer_preserves_legacy_transposed_display_dimensions():
    content = json.dumps(
        {
            "data": [-1, 0, 1, 2, 3, 4],
            "grid_size": [3, 2],
            "resolution": 1,
            "x_range": [0, 2],
            "y_range": [0, 3],
        }
    ).encode()

    png, width, height = render_gridmap_png(content)

    assert (width, height) == (3, 2)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(png[16:20], "big") == width
    assert int.from_bytes(png[20:24], "big") == height


def test_gridmap_renderer_accepts_production_scalar_grid_size():
    content = json.dumps(
        {
            "data": [-1] * 40_000,
            "grid_size": 200,
            "resolution": 0.12,
            "x_range": [-12, 12],
            "y_range": [-12, 12],
        }
    ).encode()

    png, width, height = render_gridmap_png(content)

    assert (width, height) == (200, 200)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_legacy_frozen_evidence_state_is_enhanced_without_rewrite(
    tmp_path: Path,
):
    artifact_root = tmp_path / "segment"
    (artifact_root / "grid_map").mkdir(parents=True)
    (artifact_root / "rout_plot_v2").mkdir()
    frame_key = "1.000000"
    gridmap_content = json.dumps(
        {
            "data": [-1, 0, 1, 2],
            "grid_size": 2,
            "resolution": 1,
            "x_range": [0, 2],
            "y_range": [0, 2],
        }
    ).encode()
    (artifact_root / "grid_map" / f"{frame_key}.json").write_bytes(
        gridmap_content
    )
    projection, _width, _height = render_gridmap_png(gridmap_content)
    (
        artifact_root / "rout_plot_v2" / f"{frame_key}.png"
    ).write_bytes(projection)
    frozen_state = {
        "schema_version": 1,
        "target_bindings": {TARGET_REF: "master"},
        "frame_count": 1,
        "frames": [{
            "frame_index": 0,
            "private_frame_key": frame_key,
            "camera_available": False,
            "gridmap_available": False,
            "pass": False,
            "targets": {
                TARGET_REF: {
                    "label": "Master",
                    "position": [1.0, 1.0],
                    "direction": 0.0,
                    "speed": 1.0,
                    "color": [],
                    "image_box": None,
                    "trajectory_points": [[1.0, 1.0]],
                }
            },
        }],
    }

    class LegacyEvidenceStore:
        db_path = tmp_path / "annotation.sqlite"

        def review_evidence_private(self, review_ref):
            assert review_ref == "review_" + "2" * 32
            return {
                "review_ref": review_ref,
                "status": "in_progress",
                "state_revision": 2,
                "trajectory_revision_ref": (
                    "trajectory_revision_" + "3" * 32
                ),
                "trajectory_state": frozen_state,
                "private_artifact_path": str(artifact_root),
                "artifact_sha256": _tree_sha256(artifact_root),
                "draft_state": None,
                "draft_revision": None,
            }

    service = AnnotationApplicationService(
        store=LegacyEvidenceStore(),
        worker=FakeWorker(),
    )

    evidence = service.get_review_trajectory_evidence(
        "review_" + "2" * 32
    )

    assert evidence["frames"][0]["gridmap"] is not None
    assert evidence["frames"][0]["projection"] is not None
    assert frozen_state["frames"][0]["gridmap_available"] is False
    assert "gridmap_width" not in frozen_state["frames"][0]


def test_latest_fix_revision_exposes_frozen_candidate_evidence(
    tmp_path: Path,
):
    base_root = tmp_path / "base-segment"
    candidate_root = tmp_path / "fix-segment"
    (base_root / "grid_map").mkdir(parents=True)
    frame_key = "1.000000"
    gridmap_content = json.dumps(
        {
            "data": [-1, 0, 1, 2],
            "grid_size": 2,
            "resolution": 1,
            "x_range": [0, 2],
            "y_range": [0, 2],
        }
    ).encode()
    (base_root / "grid_map" / f"{frame_key}.json").write_bytes(
        gridmap_content
    )
    shutil.copytree(base_root, candidate_root)
    frozen_state = {
        "schema_version": 1,
        "target_bindings": {TARGET_REF: "master"},
        "frame_count": 1,
        "frames": [{
            "frame_index": 0,
            "private_frame_key": frame_key,
            "camera_available": False,
            "gridmap_available": True,
            "pass": False,
            "targets": {
                TARGET_REF: {
                    "label": "Master",
                    "position": [1.0, 1.0],
                    "direction": 0.0,
                    "speed": 1.0,
                    "color": [],
                    "image_box": None,
                    "trajectory_points": [[1.0, 1.0]],
                }
            },
        }],
    }
    preview = {
        "schema_version": 1,
        "source": "fix_revision",
        "target_bindings": {TARGET_REF: "master"},
        "frame_count": 1,
        "frames": [{
            "frame_index": 0,
            "private_frame_key": frame_key,
            "camera_available": False,
            "gridmap_available": True,
            "pass": True,
            "targets": {
                TARGET_REF: {
                    "label": "Master",
                    "position": [1.5, 0.5],
                    "direction": 0.25,
                    "speed": 2.0,
                    "color": [],
                    "image_box": None,
                    "trajectory_points": [[1.5, 0.5], [1.75, 0.75]],
                    "camera_position": [100.0, 200.0],
                    "camera_trajectory_points": [
                        [100.0, 200.0],
                        [110.0, 210.0],
                    ],
                }
            },
        }],
    }
    (
        candidate_root / ".system_fix_preview.json"
    ).write_text(json.dumps(preview), encoding="utf-8")
    fix_revision_ref = "fix_revision_" + "4" * 32

    class CandidateEvidenceStore:
        db_path = tmp_path / "annotation.sqlite"

        def review_evidence_private(self, review_ref):
            return {
                "review_ref": review_ref,
                "status": "in_progress",
                "state_revision": 4,
                "trajectory_revision_ref": (
                    "trajectory_revision_" + "3" * 32
                ),
                "trajectory_state": frozen_state,
                "private_artifact_path": str(base_root),
                "artifact_sha256": _tree_sha256(base_root),
                "draft_state": {
                    "commands": [{
                        "kind": "set_position",
                        "frame_index": 0,
                        "target_ref": TARGET_REF,
                        "x": 1.5,
                        "y": 0.5,
                    }],
                },
                "draft_revision": 2,
                "fix_revision": {
                    "revision_ref": fix_revision_ref,
                    "source_draft_revision": 2,
                    "private_artifact_path": str(candidate_root),
                    "artifact_sha256": _tree_sha256(candidate_root),
                },
            }

    service = AnnotationApplicationService(
        store=CandidateEvidenceStore(),
        worker=FakeWorker(),
    )
    evidence = service.get_review_trajectory_evidence(
        "review_" + "2" * 32
    )

    assert evidence["evidence_kind"] == "fix_revision"
    assert evidence["fix_revision_ref"] == fix_revision_ref
    assert evidence["fix_revision_source_draft_revision"] == 2
    assert evidence["draft_commands"] == []
    assert evidence["frames"][0]["projection"] is None
    target = evidence["frames"][0]["targets"][0]
    assert target["position"] == [1.5, 0.5]
    assert target["base_position"] == [1.0, 1.0]
    assert target["base_trajectory_points"] == [[1.0, 1.0]]
    assert target["camera_position"] == [100.0, 200.0]
    assert target["camera_trajectory_points"] == [
        [100.0, 200.0],
        [110.0, 210.0],
    ]


@pytest.mark.parametrize(
    "override",
    [
        {"x_range": [2, 0]},
        {"x_range": [0, 2.4]},
        {"grid_size": 3},
        {"grid_size": True},
    ],
)
def test_gridmap_renderer_rejects_inconsistent_production_metadata(override):
    payload = {
        "data": [-1, 0, 1, 2],
        "grid_size": 2,
        "resolution": 1,
        "x_range": [0, 2],
        "y_range": [0, 2],
    }
    payload.update(override)

    with pytest.raises(RuntimeError):
        render_gridmap_png(json.dumps(payload).encode())


def test_postprocessing_spec_and_trajectory_records_are_immutable(tmp_path: Path):
    client, store = _make_m2_client(tmp_path)
    with client:
        reviews = client.get("/api/annotation/reviews").json()["reviews"]

    assert len(reviews) == 1
    assert reviews[0]["status"] == "pending"
    assert reviews[0]["trajectory_revision"]["revision_ref"].startswith(
        "trajectory_revision_"
    )
    facts = store.get_processing_facts(
        dataset_date="20270623",
        source_clips=["20260623_145550"],
    )
    assert facts == {
        "dataset_date": "20270623",
        "source_clips": ["20260623_145550"],
        "exists": True,
        "job_status": "annotated",
        "segment_counts": {
            "total": 1,
            "pending_initial_annotation": 0,
            "draft": 0,
            "submitted": 0,
            "skipped": 0,
            "tracking": 0,
            "tracked": 0,
            "postprocessing": 0,
            "annotated": 1,
            "postprocessing_failed": 0,
        },
        "ready_for_postprocessing": False,
        "review_counts": {
            "pending": 1,
            "in_progress": 0,
            "returned": 0,
            "approved": 0,
            "discarded": 0,
        },
    }
    assert "job_ref" not in facts
    assert "review_ref" not in json.dumps(facts)
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE postprocessing_specs SET localization_kind = 'ins'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM trajectory_revisions"
            )


def test_review_fix_cas_idempotency_and_approval_api(tmp_path: Path):
    client, store = _make_m2_client(tmp_path)
    with client:
        assert client.get(
            "/api/annotation/calibration-profiles",
            params={"domain": "navigation", "purpose": "fix"},
        ).json()["profiles"][0]["profile_ref"] == "20260409_U"
        review = client.get("/api/annotation/reviews").json()["reviews"][0]
        missing_reason = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-sessions",
            headers={"Idempotency-Key": "missing-reason"},
            json={
                "expected_review_revision": review["state_revision"],
                "calibration_profile_ref": "20260409_U",
                "calibration_content_sha256": FIX_SHA,
            },
        )
        assert missing_reason.status_code == 400
        started = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-sessions",
            headers={"Idempotency-Key": "start-fix"},
            json={
                "expected_review_revision": review["state_revision"],
                "calibration_profile_ref": "20260409_U",
                "calibration_content_sha256": FIX_SHA,
                "calibration_difference_reason": "temporary projection correction",
            },
        )
        assert started.status_code == 201
        current = started.json()
        command_request = {
            "expected_review_revision": current["state_revision"],
            "expected_draft_revision": current["fix_draft"]["revision"],
            "command": {
                "kind": "set_speed",
                "frame_index": 0,
                "target_ref": TARGET_REF,
                "speed": 2.5,
            },
        }
        changed = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-commands",
            headers={"Idempotency-Key": "speed-command"},
            json=command_request,
        )
        assert changed.status_code == 200
        assert changed.json()["fix_draft"]["revision"] == 2
        assert client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-commands",
            headers={"Idempotency-Key": "speed-command"},
            json=command_request,
        ).json() == changed.json()
        stale = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-commands",
            headers={"Idempotency-Key": "stale-command"},
            json=command_request,
        )
        assert stale.status_code == 409
        submitted = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-revisions",
            headers={"Idempotency-Key": "submit-fix"},
            json={
                "expected_review_revision": changed.json()["state_revision"],
                "expected_draft_revision": 2,
            },
        )
        assert submitted.status_code == 201
        claimed = store.claim_next_run(
            worker_id="fix-test-worker",
            writer_lock_path=tmp_path / "writer.lock",
        )
        assert claimed is not None and claimed["kind"] == "fix"
        store.start_runtime_step(
            run_id=claimed["run_id"],
            safe_step_code="fix_candidate",
        )
        store.finish_runtime_step(
            run_id=claimed["run_id"],
            safe_step_code="fix_candidate",
            status="succeeded",
            return_code=0,
        )
        candidate = tmp_path / "fix-candidate"
        candidate.mkdir()
        fix_output = candidate / "sample_trajectory_fix_five.json"
        fix_output.write_text('{"frame":{}}\n', encoding="utf-8")
        completed = store.complete_fix_run(
            run_id=claimed["run_id"],
            candidate_segment_root=str(candidate),
            candidate_tree_sha256=_tree_sha256(candidate),
            fix_trajectory_sha256=_sha256_file(fix_output),
            manifest={
                "runtime_manifest_sha256": "1" * 64,
                "command_steps": ["fix_candidate"],
            },
        )
        assert completed["fix_revisions"]
        approved = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/approve",
            headers={"Idempotency-Key": "approve-fix"},
            json={
                "expected_review_revision": completed["state_revision"],
                "fix_revision_ref": completed["submitted_fix_revision_ref"],
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert (
            approved.json()["latest_publication"]["status"]
            == "publishing"
        )
        assert client.post(
            f"/api/annotation/reviews/{review['review_ref']}/approve",
            headers={"Idempotency-Key": "approve-fix"},
            json={
                "expected_review_revision": completed["state_revision"],
                "fix_revision_ref": completed["submitted_fix_revision_ref"],
            },
        ).json() == approved.json()
        publication_worker = AnnotationWorker(
            store,
            FakeM1Runtime(tmp_path / "writer.lock"),
            fix_runtime=SimpleNamespace(),
            fix_publisher=FixCompatibilityPublisher(),
        )
        assert asyncio.run(publication_worker.run_once()) is True
        published = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        )

    assert published.status_code == 200
    assert published.json()["status"] == "approved"
    assert published.json()["latest_publication"]["status"] == "published"
    assert "publication_ref" not in published.json()["latest_publication"]
    assert "/private/" not in approved.text
    assert "/private/" not in published.text
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE decision = 'approved'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM compatibility_publications"
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM runtime_runs
            WHERE kind = 'compatibility_publish'
            """
        ).fetchone()[0] == 1


def test_approval_rejects_fix_revision_older_than_current_draft(
    tmp_path: Path,
) -> None:
    client, store = _make_m2_client(tmp_path)
    with client:
        review, completed = _freeze_test_fix_revision(
            client,
            store,
            tmp_path,
            key_prefix="stale-preview",
        )
        changed = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-commands",
            headers={"Idempotency-Key": "stale-preview-command"},
            json={
                "expected_review_revision": completed["state_revision"],
                "expected_draft_revision": completed["fix_draft"]["revision"],
                "command": {
                    "kind": "set_speed",
                    "frame_index": 0,
                    "target_ref": TARGET_REF,
                    "speed": 3.0,
                },
            },
        )
        assert changed.status_code == 200
        rejected = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/approve",
            headers={"Idempotency-Key": "stale-preview-approve"},
            json={
                "expected_review_revision": changed.json()["state_revision"],
                "fix_revision_ref": completed[
                    "submitted_fix_revision_ref"
                ],
            },
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "fix_revision_stale"


def test_failed_publication_keeps_approval_terminal_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    client, store = _make_m2_client(tmp_path)
    with client:
        review, completed = _freeze_test_fix_revision(
            client,
            store,
            tmp_path,
            key_prefix="failed-publication",
        )
        store.link_navigation_task(
            job_ref=completed["job_ref"],
            review_ref=review["review_ref"],
            navigation_task_ref="publication-outcome-task",
            parent_navigation_task_ref="processing-parent",
            link_kind="trajectory_fix",
            idempotency_key="link-publication-outcome",
        )
        approved = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/approve",
            headers={"Idempotency-Key": "failed-publication-approve"},
            json={
                "expected_review_revision": completed["state_revision"],
                "fix_revision_ref": completed[
                    "submitted_fix_revision_ref"
                ],
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert (
            approved.json()["latest_publication"]["status"]
            == "publishing"
        )
        assert store.resolve_navigation_review_outcome(
            navigation_task_ref="publication-outcome-task",
        ) == {
            "status": "in_progress",
            "review_count": 1,
            "counts": {
                "pending": 0,
                "in_progress": 0,
                "returned": 0,
                "approved": 1,
                "discarded": 0,
            },
            "all_terminal": False,
        }

        failing = BoundTestPublisher(fail=True)
        worker = AnnotationWorker(
            store,
            FakeM1Runtime(tmp_path / "writer.lock"),
            fix_runtime=SimpleNamespace(),
            fix_publisher=failing,
        )
        assert asyncio.run(worker.run_once()) is True
        failed = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        ).json()
        assert failed["status"] == "approved"
        assert failed["latest_publication"]["status"] == "failed"
        assert store.resolve_navigation_review_outcome(
            navigation_task_ref="publication-outcome-task",
        )["all_terminal"] is False
        assert "publication_ref" not in failed["latest_publication"]
        assert "private_artifact_path" not in failed["latest_publication"]

        for action in ("return", "discard"):
            blocked = client.post(
                f"/api/annotation/reviews/{review['review_ref']}/{action}",
                headers={
                    "Idempotency-Key": f"blocked-after-approval-{action}"
                },
                json={
                    "expected_review_revision": failed["state_revision"],
                    "reason": "must remain terminal",
                },
            )
            assert blocked.status_code == 409

        retry_payload = {
            "expected_review_revision": failed["state_revision"],
        }
        retried = client.post(
            (
                f"/api/annotation/reviews/{review['review_ref']}"
                "/retry-publication"
            ),
            headers={"Idempotency-Key": "retry-failed-publication"},
            json=retry_payload,
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "approved"
        assert (
            retried.json()["latest_publication"]["status"]
            == "publishing"
        )
        assert client.post(
            (
                f"/api/annotation/reviews/{review['review_ref']}"
                "/retry-publication"
            ),
            headers={"Idempotency-Key": "retry-failed-publication"},
            json=retry_payload,
        ).json() == retried.json()

        succeeding = BoundTestPublisher()
        retry_worker = AnnotationWorker(
            store,
            FakeM1Runtime(tmp_path / "writer.lock"),
            fix_runtime=SimpleNamespace(),
            fix_publisher=succeeding,
        )
        assert asyncio.run(retry_worker.run_once()) is True
        published = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        ).json()
        assert published["status"] == "approved"
        assert published["latest_publication"]["status"] == "published"
        assert store.resolve_navigation_review_outcome(
            navigation_task_ref="publication-outcome-task",
        ) == {
            "status": "completed",
            "review_count": 1,
            "counts": {
                "pending": 0,
                "in_progress": 0,
                "returned": 0,
                "approved": 1,
                "discarded": 0,
            },
            "all_terminal": True,
        }

    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE decision = 'approved'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM compatibility_publications"
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT COUNT(*) FROM runtime_runs
            WHERE kind = 'compatibility_publish'
            """
        ).fetchone()[0] == 2


def test_publication_ledger_failure_never_unapproves_published_file(
    tmp_path: Path,
) -> None:
    client, store = _make_m2_client(tmp_path)
    with client:
        review, completed = _freeze_test_fix_revision(
            client,
            store,
            tmp_path,
            key_prefix="ledger-failure",
        )
        approved = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/approve",
            headers={"Idempotency-Key": "ledger-failure-approve"},
            json={
                "expected_review_revision": completed["state_revision"],
                "fix_revision_ref": completed[
                    "submitted_fix_revision_ref"
                ],
            },
        ).json()
        original_complete = store.complete_compatibility_publication

        def fail_ledger_closure(**_kwargs):
            raise RuntimeError("simulated durable ledger failure")

        store.complete_compatibility_publication = fail_ledger_closure  # type: ignore[method-assign]
        publisher = BoundTestPublisher()
        worker = AnnotationWorker(
            store,
            FakeM1Runtime(tmp_path / "writer.lock"),
            fix_runtime=SimpleNamespace(),
            fix_publisher=publisher,
        )
        try:
            assert asyncio.run(worker.run_once()) is True
        finally:
            store.complete_compatibility_publication = original_complete  # type: ignore[method-assign]

        current = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        ).json()
        assert approved["status"] == "approved"
        assert current["status"] == "approved"
        assert current["latest_publication"]["status"] == "failed"
        compatibility_root = (
            tmp_path
            / "work"
            / "compatibility"
            / current["job_ref"]
            / current["segment_ref"]
        )
        assert list(
            compatibility_root.glob("*_trajectory_fix_five.json")
        )

    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT status FROM trajectory_review_tasks"
        ).fetchone()[0] == "approved"
        assert connection.execute(
            """
            SELECT status, failure_code
            FROM compatibility_publications
            """
        ).fetchone() == ("failed", "recovery_required")


def test_interrupted_running_publication_recovers_failed_but_approved(
    tmp_path: Path,
) -> None:
    client, store = _make_m2_client(tmp_path)
    with client:
        review, completed = _freeze_test_fix_revision(
            client,
            store,
            tmp_path,
            key_prefix="interrupted-publication",
        )
        store.link_navigation_task(
            job_ref=completed["job_ref"],
            review_ref=review["review_ref"],
            navigation_task_ref="running-publication-outcome",
            parent_navigation_task_ref="processing-parent",
            link_kind="trajectory_fix",
            idempotency_key="link-running-publication-outcome",
        )
        client.post(
            f"/api/annotation/reviews/{review['review_ref']}/approve",
            headers={"Idempotency-Key": "interrupted-publication-approve"},
            json={
                "expected_review_revision": completed["state_revision"],
                "fix_revision_ref": completed[
                    "submitted_fix_revision_ref"
                ],
            },
        ).raise_for_status()
        claimed = store.claim_next_run(
            worker_id="old-publication-worker",
            owner_epoch="old-process",
            writer_lock_path=tmp_path / "writer.lock",
        )
        assert claimed is not None
        assert claimed["kind"] == "compatibility_publish"
        running = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        ).json()
        assert running["status"] == "approved"
        assert running["latest_publication"]["status"] == "publishing"
        assert store.resolve_navigation_review_outcome(
            navigation_task_ref="running-publication-outcome",
        )["all_terminal"] is False
        assert store.recover_interrupted_runs(
            current_owner_epoch="replacement-process",
            writer_lock_path=tmp_path / "writer.lock",
        ) == 1
        recovered = client.get(
            f"/api/annotation/reviews/{review['review_ref']}"
        ).json()
        assert recovered["status"] == "approved"
        assert recovered["latest_publication"]["status"] == "failed"
        clearance = store.operator_clear_global_writer_quarantine(
            confirmation=(
                "all_navigation_annotation_writer_process_groups_absent"
            ),
            operator_reference="test recovery audit",
            idempotency_key="clear-interrupted-publication-quarantine",
            writer_lock_path=tmp_path / "writer.lock",
        )
        assert clearance["status"] == "global_quarantine_clear_confirmed"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM writer_quarantine_action_recoveries ar
            JOIN compatibility_publications p
              ON p.failure_ref = ar.failure_ref
            WHERE p.failure_code = 'recovery_required'
            """
        ).fetchone()[0] == 1


def test_approve_and_return_race_has_one_durable_winner(
    tmp_path: Path,
) -> None:
    client, store = _make_m2_client(tmp_path)
    with client:
        review, completed = _freeze_test_fix_revision(
            client,
            store,
            tmp_path,
            key_prefix="decision-race",
        )
    service = AnnotationApplicationService(store=store, worker=FakeWorker())
    barrier = threading.Barrier(2)

    def approve():
        barrier.wait()
        try:
            return service.approve_review(
                review["review_ref"],
                ApproveReviewRequest(
                    expected_review_revision=completed["state_revision"],
                    fix_revision_ref=completed[
                        "submitted_fix_revision_ref"
                    ],
                ),
                idempotency_key="decision-race-approve",
            )["status"]
        except AnnotationConflictError:
            return "conflict"

    def return_review():
        barrier.wait()
        try:
            return service.return_review(
                review["review_ref"],
                ReturnReviewRequest(
                    expected_review_revision=completed["state_revision"],
                    reason="race return",
                ),
                idempotency_key="decision-race-return",
            )["status"]
        except AnnotationConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(approve)
        return_future = executor.submit(return_review)
        results = {approve_future.result(), return_future.result()}
    assert "conflict" in results
    current = store.get_review(review["review_ref"])
    assert current["status"] in {"approved", "returned"}
    with sqlite3.connect(store.db_path) as connection:
        approval_count = connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE decision = 'approved'"
        ).fetchone()[0]
        return_count = connection.execute(
            "SELECT COUNT(*) FROM review_decisions WHERE decision = 'returned'"
        ).fetchone()[0]
        publication_count = connection.execute(
            "SELECT COUNT(*) FROM compatibility_publications"
        ).fetchone()[0]
        published_file_count = connection.execute(
            """
            SELECT COUNT(*) FROM compatibility_publications
            WHERE private_artifact_path IS NOT NULL
            """
        ).fetchone()[0]
    assert approval_count + return_count == 1
    assert publication_count == approval_count
    assert published_file_count == 0


def test_review_return_resume_and_discard_transitions(tmp_path: Path):
    client, _store = _make_m2_client(tmp_path / "returned")
    with client:
        review = client.get("/api/annotation/reviews").json()["reviews"][0]
        request = {
            "expected_review_revision": review["state_revision"],
            "calibration_profile_ref": "20260409_U",
            "calibration_content_sha256": FIX_SHA,
            "calibration_difference_reason": "temporary projection correction",
        }
        started = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-sessions",
            headers={"Idempotency-Key": "return-start"},
            json=request,
        ).json()
        returned = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/return",
            headers={"Idempotency-Key": "return-review"},
            json={
                "expected_review_revision": started["state_revision"],
                "reason": "needs another pass",
            },
        ).json()
        assert returned["status"] == "returned"
        request["expected_review_revision"] = returned["state_revision"]
        resumed_response = client.post(
            f"/api/annotation/reviews/{review['review_ref']}/fix-sessions",
            headers={"Idempotency-Key": "resume-review"},
            json=request,
        )
        resumed = resumed_response.json()
        assert resumed_response.status_code == 201
        assert resumed["status"] == "in_progress"
        assert resumed["fix_draft"]["revision"] == started["fix_draft"]["revision"]
        assert (
            resumed["fix_draft"]["content_sha256"]
            == started["fix_draft"]["content_sha256"]
        )

    discard_client, _discard_store = _make_m2_client(tmp_path / "discarded")
    with discard_client:
        review = discard_client.get("/api/annotation/reviews").json()["reviews"][0]
        discarded = discard_client.post(
            f"/api/annotation/reviews/{review['review_ref']}/discard",
            headers={"Idempotency-Key": "discard-review"},
            json={
                "expected_review_revision": review["state_revision"],
                "reason": "not suitable for training",
            },
        )
    assert discarded.status_code == 200
    assert discarded.json()["status"] == "discarded"


def test_fix_command_models_are_strict_and_discriminated():
    with pytest.raises(ValidationError):
        ApplyFixCommandRequest.model_validate(
            {
                "expected_review_revision": 1,
                "expected_draft_revision": 1,
                "command": {
                    "kind": "set_position",
                    "frame_index": 0,
                    "target_ref": TARGET_REF,
                    "x": float("nan"),
                    "y": 1.0,
                },
            }
        )
    with pytest.raises(ValidationError):
        ApplyFixCommandRequest.model_validate(
            {
                "expected_review_revision": 1,
                "expected_draft_revision": 1,
                "command": {
                    "kind": "unknown",
                    "frame_index": 0,
                },
            }
        )


def test_manifest_catalog_exposes_all_explicit_fix_choices_without_recommendation():
    profiles = CalibrationCatalog.default().list_profiles(purpose="fix")

    assert [profile["profile_ref"] for profile in profiles] == [
        "20260320",
        "20260409_U",
        "20260529_go2w",
    ]
    assert all(
        set(profile) == {"profile_ref", "label", "content_sha256"}
        for profile in profiles
    )


def test_postprocessing_preflight_fails_before_durable_run(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    worker = FakeWorker()
    worker.stage_available["postprocessing"] = False
    service = AnnotationApplicationService(store=store, worker=worker)
    spec = PostprocessingSpecInput(
        localization_kind="odom",
        gridmap_decision="copy_existing_gridmap",
        trajectory_variant="cjl_0525_with_gridmap",
        plan_sha256="d" * 64,
        observations_sha256="e" * 64,
    )

    with pytest.raises(AnnotationConflictError) as raised:
        service.begin_postprocessing(
            job["job_ref"],
            job["state_revision"],
            spec,
            idempotency_key="preflight-denied-postprocessing",
        )

    assert raised.value.code == "annotation_runtime_unavailable"
    assert store.get_job(job["job_ref"])["status"] == "tracked"
    assert worker.stage_requests == [
        ("postprocessing", spec.model_dump(mode="json"))
    ]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM postprocessing_specs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_runs WHERE kind = 'postprocessing'"
        ).fetchone()[0] == 0


def test_missing_postprocessing_runtime_has_specific_public_capability_reason(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    worker = FakeWorker()

    def _not_configured(stage, *, decision=None):
        assert stage == "postprocessing"
        assert decision is not None
        return {
            "available": False,
            "runtime_id": "navigation_odom_v1",
            "reason": {
                "code": "postprocessing_runtime_not_configured",
                "message": "/private/runtime/config is missing",
                "error_ref": "sk-not-an-annotation-error-reference",
            },
        }

    worker.preflight_runtime_stage = _not_configured
    service = AnnotationApplicationService(store=store, worker=worker)
    spec = PostprocessingSpecInput(
        localization_kind="odom",
        gridmap_decision="copy_existing_gridmap",
        trajectory_variant="cjl_0525_with_gridmap",
        plan_sha256="d" * 64,
        observations_sha256="e" * 64,
    )

    with pytest.raises(AnnotationConflictError) as raised:
        service.begin_postprocessing(
            job["job_ref"],
            job["state_revision"],
            spec,
            idempotency_key="postprocessing-runtime-not-configured",
        )

    assert raised.value.code == "annotation_runtime_unavailable"
    assert raised.value.current == {
        "capabilities": {
            "available": False,
            "runtime_id": "navigation_odom_v1",
            "reason": {
                "code": "processing_runtime_not_configured",
                "message": (
                    "The processing runtime has not completed its deployment "
                    "configuration."
                ),
            },
        }
    }
    assert "/private/" not in json.dumps(raised.value.current)
    assert store.get_job(job["job_ref"])["status"] == "tracked"


def test_fix_preflight_fails_before_snapshot_or_draft_is_created(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    setup_service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
    )
    completed = _complete_fake_postprocessing(setup_service, job)
    review = store.list_reviews()[0]
    worker = FakeWorker()
    worker.stage_available["fix"] = False
    work_root = tmp_path / "fix-work"
    service = AnnotationApplicationService(
        store=store,
        worker=worker,
        catalog=FakeM2Catalog(),
        work_root=work_root,
        fix_runtime=CommandLogFixDraftAdapter(),
    )

    with pytest.raises(AnnotationConflictError) as raised:
        service.create_fix_session(
            review["review_ref"],
            CreateFixSessionRequest(
                expected_review_revision=review["state_revision"],
                calibration_profile_ref="20260409_U",
                calibration_content_sha256=FIX_SHA,
                calibration_difference_reason="verified alternate calibration",
            ),
            idempotency_key="preflight-denied-fix-session",
        )

    assert completed["status"] == "annotated"
    assert raised.value.code == "annotation_runtime_unavailable"
    assert store.get_review(review["review_ref"])["status"] == "pending"
    assert not work_root.exists()
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM fix_calibration_snapshots"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM fix_drafts"
        ).fetchone()[0] == 0


def test_fix_revision_rechecks_preflight_before_durable_run(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    setup_service = AnnotationApplicationService(
        store=store,
        worker=FakeWorker(),
    )
    _complete_fake_postprocessing(setup_service, job)
    review = store.list_reviews()[0]
    worker = FakeWorker()
    service = AnnotationApplicationService(
        store=store,
        worker=worker,
        catalog=FakeM2Catalog(),
        work_root=tmp_path / "fix-work",
        fix_runtime=CommandLogFixDraftAdapter(),
    )
    session = service.create_fix_session(
        review["review_ref"],
        CreateFixSessionRequest(
            expected_review_revision=review["state_revision"],
            calibration_profile_ref="20260409_U",
            calibration_content_sha256=FIX_SHA,
            calibration_difference_reason="verified alternate calibration",
        ),
        idempotency_key="fix-session-before-preflight-drift",
    )
    worker.stage_available["fix"] = False

    with pytest.raises(AnnotationConflictError) as raised:
        service.create_fix_revision(
            review["review_ref"],
            CreateFixRevisionRequest(
                expected_review_revision=session["state_revision"],
                expected_draft_revision=session["fix_draft"]["revision"],
            ),
            idempotency_key="preflight-denied-fix-revision",
        )

    assert raised.value.code == "annotation_runtime_unavailable"
    current = store.get_review(review["review_ref"])
    assert current["status"] == "in_progress"
    assert current["fix_draft"]["revision"] == session["fix_draft"]["revision"]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_runs WHERE kind = 'fix'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_run_review_links"
        ).fetchone()[0] == 0


def test_successful_postprocessing_receipt_replays_without_new_preflight(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    worker = FakeWorker()
    service = AnnotationApplicationService(store=store, worker=worker)
    spec = PostprocessingSpecInput(
        localization_kind="odom",
        gridmap_decision="copy_existing_gridmap",
        trajectory_variant="cjl_0525_with_gridmap",
        plan_sha256="d" * 64,
        observations_sha256="e" * 64,
    )
    first = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        spec,
        idempotency_key="postprocessing-replay-before-preflight",
    )
    worker.stage_available["postprocessing"] = False

    replay = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        spec,
        idempotency_key="postprocessing-replay-before-preflight",
    )

    assert replay == first
    assert worker.stage_requests == [
        ("postprocessing", spec.model_dump(mode="json"))
    ]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_runs WHERE kind = 'postprocessing'"
        ).fetchone()[0] == 1


def test_postprocessing_failure_creates_pinned_navigation_handoff(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    service = AnnotationApplicationService(store=store, worker=FakeWorker())
    service.link_navigation_task(
        job_ref=job["job_ref"],
        review_ref=None,
        navigation_task_ref="navigation-postprocessing-owner",
        parent_navigation_task_ref=None,
        link_kind="processing",
        idempotency_key="link-postprocessing-owner",
    )
    service.begin_postprocessing(
        job["job_ref"],
        store.get_job(job["job_ref"])["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="begin-postprocessing-for-failure-handoff",
    )
    claimed = store.claim_next_run(
        worker_id="failed-postprocessing-worker",
        writer_lock_path=tmp_path / "writer.lock",
    )
    assert claimed is not None and claimed["kind"] == "postprocessing"

    store.fail_run(
        run_id=claimed["run_id"],
        code="annotation_runtime_failed",
        message="The postprocessing Runtime failed.",
        retryable=True,
    )

    handoff = store.claim_workflow_handoff_delivery(
        worker_id="failure-handoff-worker",
    )
    assert handoff is not None
    assert handoff["kind"] == "postprocessing_failed"
    assert handoff["navigation_task_ref"] == "navigation-postprocessing-owner"
    assert handoff["payload"]["failure_code"] == "annotation_runtime_failed"
    assert handoff["payload"]["retryable"] is True
    assert handoff["payload"]["error_ref"].startswith("annotation_error_")


def test_postprocessing_retry_preflight_fails_before_new_run(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_tracked_job(store, tmp_path)
    worker = FakeWorker()
    service = AnnotationApplicationService(store=store, worker=worker)
    started = service.begin_postprocessing(
        job["job_ref"],
        job["state_revision"],
        PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        ),
        idempotency_key="postprocessing-before-retry",
    )
    claimed = store.claim_next_run(
        worker_id="failed-postprocessing-worker",
        writer_lock_path=tmp_path / "writer.lock",
    )
    assert claimed is not None and claimed["kind"] == "postprocessing"
    store.fail_run(
        run_id=claimed["run_id"],
        code="annotation_runtime_failed",
        message="The postprocessing Runtime failed.",
        retryable=True,
    )
    failed = store.get_job(job["job_ref"])
    worker.stage_available["postprocessing"] = False

    with pytest.raises(AnnotationConflictError) as raised:
        service.job_action(
            "retry",
            job["job_ref"],
            SimpleNamespace(
                expected_job_revision=failed["state_revision"],
            ),
            idempotency_key="preflight-denied-postprocessing-retry",
        )

    assert started["status"] == "postprocessing"
    assert raised.value.code == "annotation_runtime_unavailable"
    assert store.get_job(job["job_ref"]) == failed
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_runs WHERE kind = 'postprocessing'"
        ).fetchone()[0] == 1
