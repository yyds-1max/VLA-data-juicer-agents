from __future__ import annotations

import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import vla_data_juicer_agents.annotation.store as annotation_store_module
import vla_data_juicer_agents.navigation.writer_lock as writer_lock_module
from vla_data_juicer_agents.annotation.catalog import (
    CalibrationCatalog,
    CalibrationProfile,
    _require_regular_file,
)
from vla_data_juicer_agents.annotation.application import (
    AnnotationApplicationService,
    _rollback_unaccepted_job_directory,
)
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationValidationError,
    CreateAnnotationJobRequest,
    DraftRequest,
    ExpectedJobRevisionRequest,
    SubmitRequest,
)
from vla_data_juicer_agents.annotation.migrations import (
    LATEST_ANNOTATION_SCHEMA_VERSION,
    UnsupportedAnnotationSchemaVersionError,
    apply_annotation_migrations,
    prepare_annotation_migration_ledger,
)
from vla_data_juicer_agents.annotation.runtime import (
    CapacityEstimate,
    PREPARATION_COMMAND_STEPS,
    PreparedJob,
    PreparedSegment,
    RuntimeCapabilities,
    RuntimeCapabilityReason,
    RuntimeExecutionError,
    RuntimeStepEvent,
    TrackingInputValidation,
    TrackingCheckpoint,
    TrackingResult,
    _sha256_file as runtime_sha256_file,
    _tree_sha256 as runtime_tree_sha256,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.annotation.worker import AnnotationWorker
from vla_data_juicer_agents.annotation.worker import _safe_runtime_failure
from vla_data_juicer_agents.navigation.golden.comparison import (
    compare_roots_from_annotation_store,
)
from vla_data_juicer_agents.navigation.golden.models import (
    GoldenCaseBundle,
    RuntimeRunAttestation,
)
from vla_data_juicer_agents.navigation.golden.snapshot import GoldenError
from vla_data_juicer_agents.navigation.writer_lock import (
    NavigationWriterQuarantinedError,
    ensure_navigation_writer_quarantine,
    navigation_writer_lock,
    navigation_writer_quarantine_present,
)
from vla_data_juicer_agents.web.app import create_app


PROFILE_SHA = "a" * 64


class FakeAgentScopeRuntime:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")


class FakeCalibrationCatalog:
    profile = CalibrationProfile(
        profile_ref="20260529_go2w",
        label="20260529_go2w",
        content_sha256=PROFILE_SHA,
        files=(),
    )

    def list_profiles(self) -> list[dict[str, str]]:
        return [self.profile.public_projection()]

    def get(self, profile_ref: str, expected_sha256: str) -> CalibrationProfile:
        assert profile_ref == self.profile.profile_ref
        assert expected_sha256 == PROFILE_SHA
        return self.profile

    def snapshot(
        self,
        _profile: CalibrationProfile,
        destination: Path,
    ) -> tuple[list[dict], str]:
        destination.mkdir(parents=True)
        return [], PROFILE_SHA


class FakeAnnotationRuntime:
    def __init__(self, work_root: Path) -> None:
        self.work_root = work_root
        self.prepare_calls = 0
        self.track_calls = 0
        self.cancelled: list[str] = []
        self.capacity_active_reservations: list[int] = []
        self.tracking_capacity_requests: list[tuple[int, int]] = []

    @staticmethod
    def capabilities() -> RuntimeCapabilities:
        return RuntimeCapabilities(available=True)

    def prepare(self, request) -> PreparedJob:
        self.prepare_calls += 1
        if request.step_observer is not None:
            for step in PREPARATION_COMMAND_STEPS:
                request.step_observer(
                    RuntimeStepEvent(
                        safe_step_code=step,
                        status="started",
                    )
                )
                request.step_observer(
                    RuntimeStepEvent(
                        safe_step_code=step,
                        status="succeeded",
                        return_code=0,
                    )
                )
        staging = (
            self.work_root
            / "jobs"
            / request.job_ref
            / "attempts"
            / request.run_ref
            / f"{request.dataset_date}_temp"
        )
        segment_root = staging / "samples" / "private-sequence"
        image_dir = segment_root / "fisheye_front"
        image_dir.mkdir(parents=True)
        frame = image_dir / "000001.jpg"
        # Minimal JPEG header containing a baseline SOF marker.
        frame.write_bytes(
            b"\xff\xd8"
            + b"\xff\xc0"
            + b"\x00\x07"
            + b"\x08"
            + (1536).to_bytes(2, "big")
            + (1920).to_bytes(2, "big")
            + b"\xff\xd9"
        )
        digest = hashlib.sha256(frame.read_bytes()).hexdigest()
        return PreparedJob(
            job_ref=request.job_ref,
            staging_root=staging,
            staging_ref="staging_test",
            segments=(
                PreparedSegment(
                    source_clip=request.source_clips[0],
                    private_segment_key="private-sequence",
                    segment_root=segment_root,
                    first_frame_path=frame,
                    width=1920,
                    height=1536,
                    sha256=digest,
                    etag=digest,
                ),
            ),
            runtime_manifest_sha256="b" * 64,
            input_tree_sha256="c" * 64,
            calibration_snapshot_sha256=PROFILE_SHA,
            prepared_artifact_tree_sha256=runtime_tree_sha256(staging),
        )

    def preflight_capacity(
        self,
        _dataset_date,
        _source_clips,
        *,
        active_reserved_bytes=0,
    ) -> CapacityEstimate:
        self.capacity_active_reservations.append(active_reserved_bytes)
        return CapacityEstimate(
            estimated_input_bytes=100,
            required_bytes=300 + active_reserved_bytes,
            free_bytes=10_000,
            available=True,
        )

    def track(self, request) -> TrackingResult:
        self.track_calls += 1
        assert request.attestation_targets
        assert set(request.targets).issubset(
            set(request.attestation_targets),
        )
        for attestation_target in request.attestation_targets:
            assert (
                hashlib.sha256(
                    attestation_target.yaml_path.read_bytes(),
                ).hexdigest()
                == attestation_target.expected_yaml_sha256
            )
        self.tracking_capacity_requests.append(
            (
                request.estimated_input_bytes,
                request.active_reserved_bytes,
            )
        )
        checkpoints = []
        for target in request.targets:
            assert target.yaml_path.is_file()
            output = target.segment_root / f"tracking_img_{target.identity}"
            output.mkdir()
            points = target.segment_root / f"img_{target.identity}.txt"
            points.write_text("1 2 3\n", encoding="utf-8")
            artifact_sha = hashlib.sha256(
                (
                    runtime_tree_sha256(output)
                    + ":"
                    + runtime_sha256_file(points)
                ).encode("ascii")
            ).hexdigest()
            checkpoints.append(
                TrackingCheckpoint(
                    segment_root=target.segment_root,
                    identity=target.identity,
                    output_dir=output,
                    points_path=points,
                    artifact_sha256=artifact_sha,
                )
            )
        return TrackingResult(
            checkpoints=tuple(checkpoints),
            runtime_manifest_sha256=(
                request.expected_runtime_manifest_sha256
            ),
        )

    @staticmethod
    def validate_tracking_inputs(request) -> TrackingInputValidation:
        return TrackingInputValidation(
            runtime_manifest_sha256=(
                request.expected_runtime_manifest_sha256
            ),
            prepared_artifact_tree_sha256=(
                request.expected_prepared_artifact_tree_sha256
            ),
        )

    def cancel(self, job_ref: str) -> None:
        self.cancelled.append(job_ref)

    @staticmethod
    def verify_checkpoint(request) -> bool:
        output = request.segment_root / f"tracking_img_{request.identity}"
        points = request.segment_root / f"img_{request.identity}.txt"
        if not output.is_dir() or not points.is_file():
            return False
        actual = hashlib.sha256(
            (
                runtime_tree_sha256(output)
                + ":"
                + runtime_sha256_file(points)
            ).encode("ascii")
        ).hexdigest()
        return actual == request.artifact_sha256


def _make_client(tmp_path: Path) -> tuple[TestClient, FakeAnnotationRuntime]:
    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    runtime = FakeAnnotationRuntime(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    return TestClient(app), runtime


def _create_job(client: TestClient, *, key: str = "create-1") -> dict:
    response = client.post(
        "/api/annotation/jobs",
        headers={"Idempotency-Key": key},
        json={
            "dataset_date": "20270605",
            "source_clips": ["20270605_160904"],
            "calibration_profile_ref": "20260529_go2w",
            "calibration_content_sha256": PROFILE_SHA,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for_status(client: TestClient, job_ref: str, status: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/annotation/jobs/{job_ref}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] == status:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_ref} did not reach {status}")


def _complete_target() -> dict:
    return {
        "target_ref": "target_" + "1" * 32,
        "bbox": [10, 20, 30, 40],
        "point": [15, 25],
        "colors": {"upper": "green", "lower": "gray", "shoes": "white"},
    }


def _second_complete_target() -> dict:
    return {
        "target_ref": "target_" + "2" * 32,
        "bbox": [100, 120, 30, 40],
        "point": [105, 125],
        "colors": {"upper": "blue", "lower": "black", "shoes": "gray"},
    }


def _start_single_target_tracking(
    client: TestClient,
    job: dict,
    *,
    key_prefix: str,
) -> dict:
    segment = job["segments"][0]
    base = (
        f"/api/annotation/jobs/{job['job_ref']}/segments/"
        f"{segment['segment_ref']}"
    )
    draft = client.put(
        f"{base}/draft",
        headers={"Idempotency-Key": f"{key_prefix}-draft"},
        json={
            "expected_segment_revision": segment["state_revision"],
            "expected_draft_revision": None,
            "targets": [_complete_target()],
        },
    ).json()
    submitted = client.post(
        f"{base}/submit",
        headers={"Idempotency-Key": f"{key_prefix}-submit"},
        json={
            "expected_segment_revision": draft["state_revision"],
            "expected_draft_revision": draft["draft_revision"],
        },
    )
    assert submitted.status_code == 200
    ready = client.get(f"/api/annotation/jobs/{job['job_ref']}").json()
    response = client.post(
        f"/api/annotation/jobs/{job['job_ref']}/tracking",
        headers={"Idempotency-Key": f"{key_prefix}-tracking"},
        json={"expected_job_revision": ready["state_revision"]},
    )
    assert response.status_code == 200
    return response.json()


def test_web_annotation_full_manual_flow_and_public_projection(tmp_path: Path):
    client, runtime = _make_client(tmp_path)
    with client:
        created = _create_job(client)
        job = _wait_for_status(client, created["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]

        draft_response = client.put(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/draft"
            ),
            headers={"Idempotency-Key": "draft-1"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": [
                    {
                        "target_ref": "target_" + "1" * 32,
                        "bbox": [10, 20, 30, 40],
                        "point": None,
                        "colors": {"upper": "green", "lower": None, "shoes": None},
                    }
                ],
            },
        )
        assert draft_response.status_code == 200
        draft = draft_response.json()
        assert draft["draft_revision"] == 1
        assert draft["draft"]["targets"][0]["point"] is None

        incomplete_submit = client.post(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/submit"
            ),
            headers={"Idempotency-Key": "submit-incomplete"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": 1,
            },
        )
        assert incomplete_submit.status_code == 400
        assert incomplete_submit.json()["detail"]["code"] == "annotation_incomplete"

        saved = client.put(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/draft"
            ),
            headers={"Idempotency-Key": "draft-2"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": 1,
                "targets": [_complete_target()],
            },
        ).json()
        submitted_response = client.post(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/submit"
            ),
            headers={"Idempotency-Key": "submit-complete"},
            json={
                "expected_segment_revision": saved["state_revision"],
                "expected_draft_revision": 2,
            },
        )
        assert submitted_response.status_code == 200
        assert submitted_response.json()["status"] == "submitted"

        ready = client.get(f"/api/annotation/jobs/{job['job_ref']}").json()
        assert ready["ready_for_tracking"] is True
        tracking_response = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/tracking",
            headers={"Idempotency-Key": "tracking-1"},
            json={"expected_job_revision": ready["state_revision"]},
        )
        assert tracking_response.status_code == 200
        tracked = _wait_for_status(client, job["job_ref"], "tracked")

        assert runtime.prepare_calls == 1
        assert runtime.track_calls == 1
        assert runtime.tracking_capacity_requests == [(100, 0)]
        assert tracked["counts"]["tracked"] == 1
        with sqlite3.connect(
            client.app.state.annotation_store.db_path
        ) as connection:
            assert connection.execute(
                "SELECT reserved_bytes FROM annotation_jobs WHERE job_ref = ?",
                (job["job_ref"],),
            ).fetchone()[0] == 0
        with sqlite3.connect(
            client.app.state.annotation_store.db_path
        ) as connection:
            step_rows = connection.execute(
                """
                SELECT r.kind, s.safe_step_code, s.status, s.return_code,
                       s.diagnostic_ref
                FROM runtime_run_steps s
                JOIN runtime_runs r ON r.id = s.run_id
                JOIN annotation_jobs j ON j.id = r.job_id
                WHERE j.job_ref = ?
                ORDER BY r.id, s.ordinal
                """,
                (job["job_ref"],),
            ).fetchall()
            prepare_steps = [
                row for row in step_rows if row[0] == "prepare"
            ]
            tracking_steps = [
                row for row in step_rows if row[0] == "tracking"
            ]
            assert [row[1] for row in prepare_steps] == list(
                PREPARATION_COMMAND_STEPS,
            )
            assert [row[1] for row in tracking_steps] == [
                "initial_annotation",
                "tracking",
                "tracking_target_completed",
            ]
            semantic_steps = [
                row
                for row in step_rows
                if row[1] != "tracking_target_completed"
            ]
            assert all(row[2] == "succeeded" for row in step_rows)
            assert all(row[3] == 0 for row in semantic_steps)
            assert all(row[4] is None for row in step_rows)
            tracking_run_ref = connection.execute(
                """
                SELECT r.run_ref
                FROM runtime_runs r
                JOIN annotation_jobs j ON j.id = r.job_id
                WHERE j.job_ref = ? AND r.kind = 'tracking'
                  AND r.status = 'succeeded'
                """,
                (job["job_ref"],),
            ).fetchone()[0]
        attestation = RuntimeRunAttestation.model_validate(
            client.app.state.annotation_store.runtime_run_attestation(
                tracking_run_ref
            )
        )
        assert attestation.committed is True
        assert attestation.runtime_manifest_sha256 == "b" * 64
        assert attestation.command_steps[-2:] == [
            "initial_annotation",
            "tracking",
        ]
        with sqlite3.connect(
            client.app.state.annotation_store.db_path
        ) as connection:
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET return_code = 9
                WHERE run_id = (
                    SELECT id FROM runtime_runs WHERE run_ref = ?
                ) AND safe_step_code = 'initial_annotation'
                """,
                (tracking_run_ref,),
            )
        with pytest.raises(
            RuntimeError,
            match="successful return code",
        ):
            client.app.state.annotation_store.runtime_run_attestation(
                tracking_run_ref,
            )
        with sqlite3.connect(
            client.app.state.annotation_store.db_path
        ) as connection:
            connection.execute(
                """
                UPDATE runtime_run_steps
                SET return_code = 0
                WHERE run_id = (
                    SELECT id FROM runtime_runs WHERE run_ref = ?
                ) AND safe_step_code = 'initial_annotation'
                """,
                (tracking_run_ref,),
            )
        baseline_root = tmp_path / "golden-baseline"
        (baseline_root / "legacy-segment").mkdir(parents=True)
        (baseline_root / "legacy-segment" / "artifact.bin").write_bytes(
            b"same",
        )
        bundle = GoldenCaseBundle.model_validate(
            {
                "schema_version": 2,
                "runtime_id": "navigation_odom_v1",
                "cases": [
                    {
                        "id": "store_attested_candidate",
                        "role_scopes": {
                            "legacy": {
                                "artifact_scope": "legacy-segment",
                                "dataset_date": "20260605",
                                "source_clip": "20260605_160904",
                                "internal_segment": "legacy-segment",
                                "provenance": "historical_unattested",
                            },
                            "candidate": {
                                "artifact_scope": (
                                    "samples/20270605/private-sequence"
                                ),
                                "dataset_date": "20270605",
                                "source_clip": "20270605_160904",
                                "internal_segment": "private-sequence",
                                "provenance": "runtime_attested",
                            },
                        },
                        "expected_command_steps": attestation.command_steps,
                        "candidate_attestation_required": True,
                    }
                ],
            }
        )
        comparison = compare_roots_from_annotation_store(
            annotation_store=client.app.state.annotation_store,
            candidate_run_ref=tracking_run_ref,
            baseline_root=baseline_root,
            bundle=bundle,
            case=bundle.cases[0],
        )
        assert comparison.verdict == "DIFFERENT"
        assert comparison.candidate_run_ref == tracking_run_ref
        with sqlite3.connect(
            client.app.state.annotation_store.db_path
        ) as connection:
            tracking_manifest_row = connection.execute(
                """
                SELECT a.id, a.manifest_json
                FROM artifact_manifests a
                JOIN runtime_runs r ON r.id = a.run_id
                WHERE r.run_ref = ? AND a.stage = 'tracking'
                """,
                (tracking_run_ref,),
            ).fetchone()
            tracking_manifest = json.loads(tracking_manifest_row[1])
            assert tracking_manifest["runtime_manifest_sha256"] == "b" * 64
            assert (
                len(tracking_manifest["prepared_artifact_tree_sha256"])
                == 64
            )
            tracking_manifest["runtime_manifest_sha256"] = "e" * 64
            # Simulate storage corruption after proving the normal immutable
            # trigger is installed; the attestation projection must still
            # refuse inconsistent prepare/Tracking provenance.
            connection.execute(
                "DROP TRIGGER artifact_manifests_no_update",
            )
            connection.execute(
                """
                UPDATE artifact_manifests SET manifest_json = ? WHERE id = ?
                """,
                (
                    json.dumps(
                        tracking_manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    tracking_manifest_row[0],
                ),
            )
        with pytest.raises(
            GoldenError,
            match="rejected",
        ):
            compare_roots_from_annotation_store(
                annotation_store=client.app.state.annotation_store,
                candidate_run_ref=tracking_run_ref,
                baseline_root=baseline_root,
                bundle=bundle,
                case=bundle.cases[0],
            )
        encoded = json.dumps(tracked)
        assert str(tmp_path) not in encoded
        assert "private-sequence" not in encoded
        assert "LegacyYamlAdapter" not in encoded


def test_create_job_idempotency_and_scope_conflict(tmp_path: Path):
    client, runtime = _make_client(tmp_path)
    with client:
        first = _create_job(client, key="same-create")
        replay = _create_job(client, key="same-create")
        assert replay == first
        reused = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "same-create"},
            json={
                "dataset_date": "20270605",
                "source_clips": ["another-clip"],
                "calibration_profile_ref": "20260529_go2w",
                "calibration_content_sha256": PROFILE_SHA,
            },
        )
        assert reused.status_code == 409
        assert reused.json()["detail"]["code"] == "idempotency_key_reused"

        second_clip = (
            tmp_path
            / "datasets"
            / "clip_data"
            / "20270605"
            / "20270605_160905"
            / "sync_data"
        )
        second_clip.mkdir(parents=True)
        conflict = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "different-create"},
            json={
                "dataset_date": "20270605",
                "source_clips": [
                    "20270605_160905",
                    "20270605_160904",
                ],
                "calibration_profile_ref": "20260529_go2w",
                "calibration_content_sha256": PROFILE_SHA,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "annotation_scope_conflict"
        assert runtime.prepare_calls <= 1
        with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM annotation_jobs"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM annotation_source_leases"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM calibration_snapshots"
            ).fetchone()[0] == 1
        job_directories = list(
            (tmp_path / "annotation-work" / "jobs").iterdir()
        )
        assert [path.name for path in job_directories] == [first["job_ref"]]


@pytest.mark.asyncio
async def test_worker_cleanup_survives_heartbeat_task_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeAnnotationRuntime(tmp_path / "annotation-work")
    worker = AnnotationWorker(SimpleNamespace(), runtime)
    run = {
        "run_ref": "run_" + "1" * 32,
        "job_ref": "job_" + "1" * 32,
    }

    async def completed_run(_run):
        await asyncio.sleep(0)

    async def failed_heartbeat(_run):
        raise RuntimeError("simulated heartbeat database failure")

    monkeypatch.setattr(worker, "_execute_run", completed_run)
    monkeypatch.setattr(worker, "_heartbeat", failed_heartbeat)
    worker._cancel_reasons[run["job_ref"]] = "heartbeat_failed"

    await worker._execute(run)

    assert worker._active_run is None
    assert run["job_ref"] not in worker._cancel_reasons


@pytest.mark.asyncio
async def test_heartbeat_renewal_race_observes_committed_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vla_data_juicer_agents.annotation.worker as worker_module

    renewal_entered = threading.Event()
    completion_committed = threading.Event()

    class CompletionRaceStore:
        @staticmethod
        def runtime_control_state(*, run_id, worker_id):
            del run_id, worker_id
            return (
                "finished"
                if completion_committed.is_set()
                else "continue"
            )

        @staticmethod
        def renew_run_lease(*, run_id, worker_id):
            del run_id, worker_id
            renewal_entered.set()
            assert completion_committed.wait(timeout=2)
            return False

    monotonic_values = iter((0.0, 31.0))
    monkeypatch.setattr(
        worker_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values, 31.0)),
    )
    real_sleep = asyncio.sleep

    async def immediate_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(worker_module.asyncio, "sleep", immediate_sleep)
    runtime = FakeAnnotationRuntime(tmp_path / "annotation-work")
    worker = AnnotationWorker(CompletionRaceStore(), runtime)
    run = {
        "run_id": 1,
        "run_ref": "run_" + "2" * 32,
        "job_ref": "job_" + "2" * 32,
    }

    heartbeat = asyncio.create_task(worker._heartbeat(run))
    assert await asyncio.to_thread(renewal_entered.wait, 2)
    completion_committed.set()
    await heartbeat

    assert runtime.cancelled == []
    assert run["job_ref"] not in worker._cancel_reasons


@pytest.mark.asyncio
async def test_worker_loop_failure_marks_capability_unhealthy(
    tmp_path: Path,
) -> None:
    class CrashingStore:
        @staticmethod
        def recover_interrupted_runs():
            raise RuntimeError(f"private database failure at {tmp_path}")

    worker = AnnotationWorker(
        CrashingStore(),
        FakeAnnotationRuntime(tmp_path / "annotation-work"),
    )

    await worker.run_forever()

    capability = worker.capabilities()
    assert capability["available"] is False
    assert capability["reason"]["code"] == "annotation_worker_unhealthy"
    assert capability["reason"]["error_ref"].startswith(
        "annotation_worker_error_",
    )
    assert str(tmp_path) not in json.dumps(capability)


def test_create_job_receipt_replays_before_runtime_capability_checks(tmp_path: Path):
    client, runtime = _make_client(tmp_path)
    payload = {
        "dataset_date": "20270605",
        "source_clips": ["20270605_160904"],
        "calibration_profile_ref": "20260529_go2w",
        "calibration_content_sha256": PROFILE_SHA,
    }
    with client:
        first = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "lost-create-response"},
            json=payload,
        )
        assert first.status_code == 201

        runtime.capabilities = lambda: RuntimeCapabilities(
            available=False,
            reason={"code": "test_unavailable", "message": "unavailable"},
        )
        client.app.state.annotation_worker.invalidate_capabilities()

        replay = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "lost-create-response"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json() == first.json()

        conflicting_reuse = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "lost-create-response"},
            json={**payload, "source_clips": ["different-clip"]},
        )
        assert conflicting_reuse.status_code == 409
        assert (
            conflicting_reuse.json()["detail"]["code"]
            == "idempotency_key_reused"
        )


def test_capability_projection_hides_runtime_tool_details(tmp_path: Path):
    client, runtime = _make_client(tmp_path)
    runtime.capabilities = lambda: RuntimeCapabilities(
        available=False,
        reason=RuntimeCapabilityReason(
            code="xvfb_version_mismatch",
            message="The installed Xvfb package does not match bubblewrap.",
        ),
    )
    client.app.state.annotation_worker.invalidate_capabilities()

    with client:
        response = client.get("/api/annotation/capabilities")

    assert response.status_code == 200
    assert response.json()["reason"] == {
        "code": "processing_runtime_preflight_failed",
        "message": (
            "The processing runtime has not passed its deployment preflight."
        ),
    }
    assert "xvfb" not in response.text.lower()
    assert "bubblewrap" not in response.text.lower()


def test_annotation_sqlite_and_wal_sidecars_are_private(tmp_path: Path):
    database = tmp_path / "annotation.sqlite"
    database.touch(mode=0o644)
    database.chmod(0o644)
    store = AnnotationStore(database)

    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for path in (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        ):
            assert path.exists()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        connection.rollback()
        connection.close()


def test_draft_cas_requires_null_then_exact_revision(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        url = (
            f"/api/annotation/jobs/{job['job_ref']}/segments/"
            f"{segment['segment_ref']}/draft"
        )
        payload = {
            "expected_segment_revision": 0,
            "expected_draft_revision": 1,
            "targets": [],
        }
        wrong_first = client.put(
            url,
            headers={"Idempotency-Key": "wrong-first"},
            json=payload,
        )
        assert wrong_first.status_code == 409
        assert wrong_first.json()["detail"]["code"] == "draft_revision_conflict"

        first = client.put(
            url,
            headers={"Idempotency-Key": "first"},
            json={**payload, "expected_draft_revision": None},
        ).json()
        stale = client.put(
            url,
            headers={"Idempotency-Key": "stale"},
            json={
                "expected_segment_revision": first["state_revision"],
                "expected_draft_revision": None,
                "targets": [],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "draft_revision_conflict"


def test_skip_unskip_restores_submitted_revision(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        base = f"/api/annotation/jobs/{job['job_ref']}/segments/{segment['segment_ref']}"
        draft = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "draft"},
            json={
                "expected_segment_revision": 0,
                "expected_draft_revision": None,
                "targets": [_complete_target()],
            },
        ).json()
        submitted = client.post(
            f"{base}/submit",
            headers={"Idempotency-Key": "submit"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": draft["draft_revision"],
            },
        ).json()
        skipped = client.post(
            f"{base}/skip",
            headers={"Idempotency-Key": "skip"},
            json={
                "expected_segment_revision": submitted["state_revision"],
                "reason_code": "no_valid_target",
            },
        ).json()
        assert skipped["submitted_revision"] is None
        unskipped = client.post(
            f"{base}/unskip",
            headers={"Idempotency-Key": "unskip"},
            json={"expected_segment_revision": skipped["state_revision"]},
        ).json()
        assert unskipped["status"] == "submitted"
        assert unskipped["submitted_revision"] == 1


def test_unskip_restores_latest_draft_after_reopen_and_edit(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )
        segment = job["segments"][0]
        base = (
            f"/api/annotation/jobs/{job['job_ref']}/segments/"
            f"{segment['segment_ref']}"
        )
        draft = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "draft-original"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": [_complete_target()],
            },
        ).json()
        submitted = client.post(
            f"{base}/submit",
            headers={"Idempotency-Key": "submit-original"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": draft["draft_revision"],
            },
        ).json()
        reopened = client.post(
            f"{base}/reopen",
            headers={"Idempotency-Key": "reopen-original"},
            json={"expected_segment_revision": submitted["state_revision"]},
        ).json()
        assert reopened["status"] == "draft"
        edited = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "draft-edited"},
            json={
                "expected_segment_revision": reopened["state_revision"],
                "expected_draft_revision": reopened["draft_revision"],
                "targets": [_second_complete_target()],
            },
        ).json()
        skipped = client.post(
            f"{base}/skip",
            headers={"Idempotency-Key": "skip-edited"},
            json={
                "expected_segment_revision": edited["state_revision"],
                "reason_code": "other",
                "note": "operator chose another segment",
            },
        ).json()
        restored = client.post(
            f"{base}/unskip",
            headers={"Idempotency-Key": "unskip-edited"},
            json={"expected_segment_revision": skipped["state_revision"]},
        ).json()

        assert restored["status"] == "draft"
        assert restored["submitted_revision"] is None
        assert restored["draft_revision"] == edited["draft_revision"]
        assert (
            restored["draft"]["targets"][0]["target_ref"]
            == _second_complete_target()["target_ref"]
        )


def test_all_skipped_completes_without_tracking(tmp_path: Path):
    client, runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        skipped = client.post(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/skip"
            ),
            headers={"Idempotency-Key": "skip-all"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "reason_code": "unusable_first_frame",
            },
        )
        assert skipped.status_code == 200
        ready = client.get(f"/api/annotation/jobs/{job['job_ref']}").json()
        assert ready["ready_for_no_processable_targets"] is True
        completed = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/complete-no-processable-targets",
            headers={"Idempotency-Key": "complete-empty"},
            json={"expected_job_revision": ready["state_revision"]},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "cancelled"
        assert completed.json()["completion_outcome"] == "no_processable_targets"
        assert runtime.track_calls == 0


def test_first_frame_hash_change_fails_closed_without_path_leak(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        frame_url = job["segments"][0]["first_frame"]["url"]
        first = client.get(frame_url)
        assert first.status_code == 200
        assert first.headers["etag"]

        private = client.app.state.annotation_store.first_frame_private(
            job["job_ref"], job["segments"][0]["segment_ref"]
        )
        Path(private["path"]).write_bytes(b"changed")
        changed = client.get(frame_url)
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "first_frame_changed"
        assert str(tmp_path) not in changed.text


def test_first_frame_rejects_ancestor_symlink_even_when_it_points_inside_root(
    tmp_path: Path,
):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        private = client.app.state.annotation_store.first_frame_private(
            job["job_ref"], segment["segment_ref"]
        )
        original = Path(private["path"])
        alias = Path(private["staging_root"]) / "frame-alias"
        alias.symlink_to(original.parent, target_is_directory=True)
        with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
            connection.execute(
                """
                UPDATE annotation_segments
                SET private_first_frame_path = ?
                WHERE segment_ref = ?
                """,
                (str(alias / original.name), segment["segment_ref"]),
            )

        response = client.get(segment["first_frame"]["url"])
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "unsafe_first_frame"


def test_first_frame_rechecks_stored_dimensions(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
            connection.execute(
                """
                UPDATE annotation_segments
                SET first_frame_width = 1
                WHERE segment_ref = ?
                """,
                (segment["segment_ref"],),
            )

        response = client.get(segment["first_frame"]["url"])
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "first_frame_changed"


def test_worker_leaves_queued_work_unclaimed_when_runtime_is_unavailable(tmp_path: Path):
    class RecordingStore:
        def __init__(self) -> None:
            self.claims = 0

        def claim_next_run(self, **_kwargs):
            self.claims += 1
            return None

    class Unavailable:
        @staticmethod
        def capabilities():
            return {"available": False, "runtime_id": "navigation_odom_v1"}

    store = RecordingStore()
    worker = AnnotationWorker(store, Unavailable())  # type: ignore[arg-type]

    assert asyncio.run(worker.run_once()) is False
    assert store.claims == 0


def test_capability_exception_is_safely_projected_and_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    class BrokenRuntime:
        @staticmethod
        def capabilities():
            raise RuntimeError(f"credential at {tmp_path}/private/token")

    worker = AnnotationWorker(object(), BrokenRuntime())  # type: ignore[arg-type]
    capabilities = worker.capabilities()

    assert capabilities["available"] is False
    assert capabilities["reason"]["error_ref"].startswith(
        "annotation_error_"
    )
    assert str(tmp_path) not in json.dumps(capabilities)
    assert str(tmp_path) not in caplog.text
    assert "credential" not in caplog.text


@pytest.mark.parametrize(
    "code",
    [
        "runtime_input_changed",
        "runtime_manifest_changed",
        "prepared_staging_changed",
        "unsafe_runtime_input",
        "unsupported_runtime_variant",
        "missing_runtime_input",
        "calibration_snapshot_mismatch",
    ],
)
def test_runtime_input_integrity_failures_are_not_web_retryable(code: str):
    public_code, _message, retryable = _safe_runtime_failure(
        RuntimeExecutionError(code, "/private/source/token")
    )

    assert public_code == code
    assert retryable is False


def test_runtime_switch_while_waiting_fails_before_yaml_or_tracking(
    tmp_path: Path,
) -> None:
    client, runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )

        def reject_switched_runtime(_request):
            raise RuntimeExecutionError(
                "runtime_manifest_changed",
                "private Runtime B differs from prepared Runtime A",
            )

        runtime.validate_tracking_inputs = reject_switched_runtime
        _start_single_target_tracking(
            client,
            job,
            key_prefix="runtime-switch",
        )
        failed = _wait_for_status(client, job["job_ref"], "failed")

    assert failed["failure"]["code"] == "runtime_manifest_changed"
    assert failed["failure"]["retryable"] is False
    assert runtime.track_calls == 0
    assert not list(
        (tmp_path / "annotation-work" / "jobs").rglob("*.yaml"),
    )


def test_two_store_workers_can_claim_a_queued_run_only_once(tmp_path: Path):
    database = tmp_path / "annotation.sqlite"
    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    first_store = AnnotationStore(database)
    first_worker = AnnotationWorker(
        first_store,
        FakeAnnotationRuntime(work_root / "first"),
    )
    service = AnnotationApplicationService(
        store=first_store,
        worker=first_worker,
        catalog=FakeCalibrationCatalog(),
        work_root=work_root,
        clip_data_root=clip_data,
    )
    service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_160904"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="create-claim",
    )
    second_store = AnnotationStore(database)
    second_worker = AnnotationWorker(
        second_store,
        FakeAnnotationRuntime(work_root / "second"),
    )
    barrier = threading.Barrier(2)
    writer_lock_path = tmp_path / "writer.lock"

    def claim(worker: AnnotationWorker):
        barrier.wait(timeout=2)
        return worker.store.claim_next_run(
            worker_id=worker.worker_id,
            owner_epoch=worker.owner_epoch,
            writer_lock_path=writer_lock_path,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(claim, (first_worker, second_worker))
        )

    assert sum(result is not None for result in results) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_runs WHERE status = 'running'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_leases"
        ).fetchone()[0] == 1


def test_new_process_epoch_quarantines_running_run_and_all_queued_claims(
    tmp_path: Path,
) -> None:
    database = tmp_path / "annotation.sqlite"
    clip_data = tmp_path / "datasets" / "clip_data"
    for clip in ("20270605_160904", "20270605_152930"):
        (clip_data / "20270605" / clip / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    writer_lock_path = tmp_path / "writer.lock"
    store = AnnotationStore(database)
    service = AnnotationApplicationService(
        store=store,
        worker=AnnotationWorker(store, FakeAnnotationRuntime(work_root)),
        catalog=FakeCalibrationCatalog(),
        work_root=work_root,
        clip_data_root=clip_data,
    )
    first = service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_160904"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="create-process-a",
    )
    second = service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_152930"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="create-process-b",
    )
    claimed = store.claim_next_run(
        worker_id="worker-a",
        owner_epoch="process-a",
        writer_lock_path=writer_lock_path,
    )
    assert claimed is not None
    assert claimed["job_ref"] == first["job_ref"]

    assert (
        store.claim_next_run(
            worker_id="worker-b",
            owner_epoch="process-b",
            writer_lock_path=writer_lock_path,
        )
        is None
    )
    assert store.get_job(first["job_ref"])["failure"]["code"] == (
        "recovery_required"
    )
    assert store.get_job(second["job_ref"])["status"] == "preparing"
    assert store.has_recovery_quarantine()
    assert navigation_writer_quarantine_present(writer_lock_path)
    assert (
        store.claim_next_run(
            worker_id="worker-b",
            owner_epoch="process-b",
            writer_lock_path=writer_lock_path,
        )
        is None
    )


def test_external_navigation_quarantine_keeps_annotation_run_queued(
    tmp_path: Path,
) -> None:
    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True,
    )
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    work_root = tmp_path / "annotation-work"
    service = AnnotationApplicationService(
        store=store,
        worker=AnnotationWorker(store, FakeAnnotationRuntime(work_root)),
        catalog=FakeCalibrationCatalog(),
        work_root=work_root,
        clip_data_root=clip_data,
    )
    service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_160904"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="create-before-navigation-quarantine",
    )
    writer_lock_path = tmp_path / "writer.lock"
    ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="navigation_runtime_unknown",
    )

    assert (
        store.claim_next_run(
            worker_id="annotation-worker",
            owner_epoch="annotation-process",
            writer_lock_path=writer_lock_path,
        )
        is None
    )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT status FROM runtime_runs"
        ).fetchone()[0] == "queued"


def test_healthy_active_writer_allows_job_creation_but_keeps_it_queued(
    tmp_path: Path,
) -> None:
    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True,
    )
    writer_lock_path = tmp_path / "writer.lock"
    runtime = FakeAnnotationRuntime(tmp_path / "annotation-work")
    runtime.config = SimpleNamespace(writer_lock_path=writer_lock_path)
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    worker = AnnotationWorker(store, runtime)
    service = AnnotationApplicationService(
        store=store,
        worker=worker,
        catalog=FakeCalibrationCatalog(),
        work_root=tmp_path / "annotation-work",
        clip_data_root=clip_data,
    )
    entered = threading.Event()
    release = threading.Event()

    def active_writer() -> None:
        with navigation_writer_lock(lock_path=writer_lock_path):
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=active_writer)
    thread.start()
    assert entered.wait(timeout=2)
    assert worker.capabilities().available is True
    created = service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_160904"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="create-while-writer-busy",
    )
    assert created["status"] == "preparing"
    assert (
        store.claim_next_run(
            worker_id=worker.worker_id,
            owner_epoch=worker.owner_epoch,
            writer_lock_path=writer_lock_path,
        )
        is None
    )
    release.set()
    thread.join(timeout=2)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT status FROM runtime_runs"
        ).fetchone()[0] == "queued"


def test_calibration_file_rejects_ancestor_and_root_symlinks(tmp_path: Path):
    root = tmp_path / "runtime"
    real_profile = root / "real-profile"
    real_profile.mkdir(parents=True)
    sensor = real_profile / "sensor.json"
    sensor.write_text("{}", encoding="utf-8")
    alias = root / "profile-alias"
    alias.symlink_to(real_profile, target_is_directory=True)

    with pytest.raises(AnnotationValidationError, match="symlink"):
        _require_regular_file(alias / "sensor.json", root=root)

    root_alias = tmp_path / "runtime-alias"
    root_alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(AnnotationValidationError, match="real directory"):
        _require_regular_file(
            root_alias / "real-profile" / "sensor.json",
            root=root_alias,
        )


def test_annotation_immutable_tables_reject_update_and_delete(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        base = f"/api/annotation/jobs/{job['job_ref']}/segments/{segment['segment_ref']}"
        draft = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "draft"},
            json={
                "expected_segment_revision": 0,
                "expected_draft_revision": None,
                "targets": [_complete_target()],
            },
        ).json()
        client.post(
            f"{base}/submit",
            headers={"Idempotency-Key": "submit"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": draft["draft_revision"],
            },
        )

    database = tmp_path / "annotation.sqlite"
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE initial_annotation_revisions SET content_sha256 = 'x'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM calibration_snapshots")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE artifact_manifests SET content_sha256 = 'x'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE annotation_segment_actions SET action = 'changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM annotation_mutation_receipts")


def test_annotation_schema_rejects_future_and_gapped_ledgers(tmp_path: Path):
    current = tmp_path / "current.sqlite"
    AnnotationStore(current)
    with sqlite3.connect(current) as connection:
        assert [
            row[0]
            for row in connection.execute(
                """
                SELECT version FROM annotation_schema_migrations
                ORDER BY version
                """
            )
        ] == list(range(1, LATEST_ANNOTATION_SCHEMA_VERSION + 1))
        step_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(runtime_run_steps)"
            )
        }
    assert {"return_code", "diagnostic_ref"} <= step_columns

    future = tmp_path / "future.sqlite"
    with sqlite3.connect(future) as connection:
        connection.execute(
            """
            CREATE TABLE annotation_schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO annotation_schema_migrations VALUES (?, 'future', 'now')",
            (LATEST_ANNOTATION_SCHEMA_VERSION + 1,),
        )
    with pytest.raises(UnsupportedAnnotationSchemaVersionError):
        AnnotationStore(future)

    gapped = tmp_path / "gapped.sqlite"
    with sqlite3.connect(gapped) as connection:
        connection.execute(
            """
            CREATE TABLE annotation_schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO annotation_schema_migrations VALUES (0, 'gap', 'now')"
        )
    with pytest.raises(RuntimeError, match="non-contiguous"):
        AnnotationStore(gapped)


def test_annotation_migration_rolls_back_schema_when_ledger_insert_fails(
    tmp_path: Path,
):
    database = tmp_path / "atomic.sqlite"
    with sqlite3.connect(database) as connection:
        prepare_annotation_migration_ledger(connection)
        connection.commit()
        connection.execute(
            """
            CREATE TRIGGER reject_migration_ledger
            BEFORE INSERT ON annotation_schema_migrations
            BEGIN
                SELECT RAISE(ABORT, 'simulated ledger failure');
            END
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="ledger failure"):
            apply_annotation_migrations(connection, applied_at="now")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "annotation_schema_migrations" in tables
    assert "annotation_jobs" not in tables


def test_annotation_mutations_record_manual_web_deployment_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VLA_DEPLOYMENT_INSTANCE", "annotation-test-instance")
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )
        segment = job["segments"][0]
        response = client.put(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/draft"
            ),
            headers={"Idempotency-Key": "audited-draft"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": [],
            },
        )
        assert response.status_code == 200

    database = tmp_path / "annotation.sqlite"
    assert database.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database) as connection:
        receipts = connection.execute(
            """
            SELECT actor_kind, deployment_instance, created_at
            FROM annotation_mutation_receipts
            """
        ).fetchall()
        actions = connection.execute(
            """
            SELECT actor_kind, deployment_instance, created_at
            FROM annotation_segment_actions
            """
        ).fetchall()
    assert receipts and actions
    assert all(row[0] == "manual_web" for row in [*receipts, *actions])
    assert all(
        row[1] == "annotation-test-instance" for row in [*receipts, *actions]
    )
    assert all("T" in row[2] for row in [*receipts, *actions])


def test_failed_snapshot_cleanup_refuses_symlinked_work_root_ancestor(
    tmp_path: Path,
):
    real_parent = tmp_path / "real"
    job_ref = "job_" + "a" * 32
    job_directory = real_parent / "work" / "jobs" / job_ref
    (job_directory / "calibration").mkdir(parents=True)
    (job_directory / "calibration" / "sensor.json").write_text(
        "{}",
        encoding="utf-8",
    )
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    _rollback_unaccepted_job_directory(
        work_root=alias / "work",
        job_ref=job_ref,
    )

    assert job_directory.is_dir()


def test_recovery_requires_global_and_job_specific_operator_audit(
    tmp_path: Path,
):
    database = tmp_path / "annotation.sqlite"
    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    store = AnnotationStore(database)
    worker = AnnotationWorker(store, FakeAnnotationRuntime(work_root))
    service = AnnotationApplicationService(
        store=store,
        worker=worker,
        catalog=FakeCalibrationCatalog(),
        work_root=work_root,
        clip_data_root=clip_data,
    )
    job = service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_160904"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="create-recovery",
    )
    claimed = store.claim_next_run(worker_id="crashed-worker")
    assert claimed is not None
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM runtime_leases")

    writer_lock_path = tmp_path / "writer.lock"
    assert store.recover_interrupted_runs(
        writer_lock_path=writer_lock_path,
    ) == 1
    failed = store.get_job(job["job_ref"])
    assert failed["status"] == "failed"
    assert failed["failure"]["code"] == "recovery_required"
    assert failed["failure"]["retryable"] is False
    store.fail_run(
        run_id=claimed["run_id"],
        code="annotation_runtime_failed",
        message="A late worker exception must not replace recovery.",
        retryable=True,
    )
    still_failed = store.get_job(job["job_ref"])
    assert still_failed["failure"] == failed["failure"]
    with pytest.raises(AnnotationConflictError):
        store.retry_job(
            job_ref=job["job_ref"],
            expected_job_revision=failed["state_revision"],
            idempotency_key="ordinary-retry-denied",
        )
    with pytest.raises(
        AnnotationConflictError,
        match="old process group is absent",
    ) as cancel_error:
        store.cancel_job(
            job_ref=job["job_ref"],
            expected_job_revision=failed["state_revision"],
            idempotency_key="ordinary-cancel-denied",
        )
    assert cancel_error.value.code == "recovery_confirmation_required"
    with pytest.raises(AnnotationConflictError) as scope_error:
        service.create_job(
            CreateAnnotationJobRequest(
                dataset_date="20270605",
                source_clips=["20270605_160904"],
                calibration_profile_ref="20260529_go2w",
                calibration_content_sha256=PROFILE_SHA,
            ),
            idempotency_key="replacement-before-recovery-confirmation",
        )
    assert scope_error.value.code == "annotation_scope_conflict"
    with pytest.raises(AnnotationValidationError) as missing_global:
        store.operator_confirm_recovery(
            job_ref=job["job_ref"],
            expected_job_revision=failed["state_revision"],
            confirmation="old_process_group_absent",
            operator_reference="OPS-20260723-001",
            idempotency_key="operator-recovery-without-global",
            global_quarantine_action_ref="writer_quarantine_action_missing",
            writer_lock_path=writer_lock_path,
        )
    assert missing_global.value.code == "global_quarantine_confirmation_required"
    global_clear = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-20260723-GLOBAL-001",
        idempotency_key="operator-global-recovery",
        writer_lock_path=writer_lock_path,
    )
    # A later, unrelated Navigation crash may establish a new global marker.
    # Resolving this Annotation Job must never clear that newer marker.
    ensure_navigation_writer_quarantine(writer_lock_path)
    assert navigation_writer_quarantine_present(writer_lock_path)
    with pytest.raises(AnnotationConflictError) as newer_global:
        store.operator_confirm_recovery(
            job_ref=job["job_ref"],
            expected_job_revision=failed["state_revision"],
            confirmation="old_process_group_absent",
            operator_reference="OPS-20260723-001",
            idempotency_key="operator-recovery",
            global_quarantine_action_ref=global_clear["action_ref"],
            writer_lock_path=writer_lock_path,
        )
    assert newer_global.value.code == "global_writer_quarantine_active"
    assert navigation_writer_quarantine_present(writer_lock_path)
    latest_global_clear = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-20260723-GLOBAL-002",
        idempotency_key="operator-global-recovery-new-marker",
        writer_lock_path=writer_lock_path,
    )
    recovered = store.operator_confirm_recovery(
        job_ref=job["job_ref"],
        expected_job_revision=failed["state_revision"],
        confirmation="old_process_group_absent",
        operator_reference="OPS-20260723-001",
        idempotency_key="operator-recovery",
        global_quarantine_action_ref=latest_global_clear["action_ref"],
        writer_lock_path=writer_lock_path,
    )
    assert recovered["status"] == "preparing"
    assert not navigation_writer_quarantine_present(writer_lock_path)
    assert (
        store.operator_confirm_recovery(
            job_ref=job["job_ref"],
            expected_job_revision=failed["state_revision"],
            confirmation="old_process_group_absent",
            operator_reference="OPS-20260723-001",
            idempotency_key="operator-recovery",
            global_quarantine_action_ref=latest_global_clear["action_ref"],
            writer_lock_path=writer_lock_path,
        )
        == recovered
    )
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            """
            SELECT action, confirmation, operator_reference,
                   deployment_instance, created_at,
                   global_quarantine_action_ref
            FROM annotation_operator_actions
            """
        ).fetchone()
        global_audit = connection.execute(
            """
            SELECT action, confirmation, operator_reference, marker_was_present
            FROM writer_quarantine_actions a
            JOIN writer_quarantine_action_completions c
              ON c.action_ref = a.action_ref
            ORDER BY a.created_at
            LIMIT 1
            """
        ).fetchone()
    assert audit[:3] == (
        "confirm_recovery",
        "old_process_group_absent",
        "OPS-20260723-001",
    )
    assert audit[3]
    assert "T" in audit[4]
    assert audit[5] == latest_global_clear["action_ref"]
    assert global_audit == (
        "clear_global_quarantine",
        "all_navigation_annotation_writer_process_groups_absent",
        "OPS-20260723-GLOBAL-001",
        1,
    )


def test_global_quarantine_completion_failure_keeps_marker_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    writer_lock_path = tmp_path / "writer.lock"
    ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="navigation_failure_ref",
    )
    real_write = store._write
    write_calls = 0

    @contextmanager
    def fail_completion_write():
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            with real_write() as connection:
                yield connection
                raise sqlite3.OperationalError(
                    "completion commit unavailable",
                )
            return
        with real_write() as connection:
            yield connection

    monkeypatch.setattr(store, "_write", fail_completion_write)
    with pytest.raises(sqlite3.OperationalError, match="completion"):
        store.operator_clear_global_writer_quarantine(
            confirmation=(
                "all_navigation_annotation_writer_process_groups_absent"
            ),
            operator_reference="OPS-COMPLETION-FAILURE",
            idempotency_key="global-clear-completion-failure",
            writer_lock_path=writer_lock_path,
        )
    assert navigation_writer_quarantine_present(writer_lock_path)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM writer_quarantine_actions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM writer_quarantine_action_completions"
        ).fetchone()[0] == 0

    monkeypatch.setattr(store, "_write", real_write)
    completed = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-COMPLETION-FAILURE",
        idempotency_key="global-clear-completion-failure",
        writer_lock_path=writer_lock_path,
    )
    assert completed["status"] == "global_quarantine_clear_confirmed"
    assert not navigation_writer_quarantine_present(writer_lock_path)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM writer_quarantine_action_completions"
        ).fetchone()[0] == 1


def test_completed_global_clearance_replays_exact_partial_unlink_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    writer_lock_path = tmp_path / "writer.lock"
    first = ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="a_partial_unlink",
    )
    second = ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="b_partial_unlink",
    )
    real_remove = writer_lock_module._remove_quarantine_marker
    remove_calls = 0

    def fail_second_unlink(*args, **kwargs) -> None:
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 2:
            raise OSError("injected partial marker unlink")
        real_remove(*args, **kwargs)

    monkeypatch.setattr(
        writer_lock_module,
        "_remove_quarantine_marker",
        fail_second_unlink,
    )
    with pytest.raises(OSError, match="partial marker unlink"):
        store.operator_clear_global_writer_quarantine(
            confirmation=(
                "all_navigation_annotation_writer_process_groups_absent"
            ),
            operator_reference="OPS-PARTIAL-UNLINK",
            idempotency_key="global-clear-partial-unlink",
            writer_lock_path=writer_lock_path,
        )

    assert not first.exists()
    assert second.is_file()
    assert navigation_writer_quarantine_present(writer_lock_path)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM writer_quarantine_action_completions"
        ).fetchone()[0] == 1

    monkeypatch.setattr(
        writer_lock_module,
        "_remove_quarantine_marker",
        real_remove,
    )
    replayed = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-PARTIAL-UNLINK",
        idempotency_key="global-clear-partial-unlink",
        writer_lock_path=writer_lock_path,
    )
    assert replayed["status"] == "global_quarantine_clear_confirmed"
    assert not navigation_writer_quarantine_present(writer_lock_path)


def test_completed_global_clearance_never_deletes_a_newer_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    writer_lock_path = tmp_path / "writer.lock"
    first = ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="a_old_recovery",
    )
    second = ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="b_old_recovery",
    )
    real_remove = writer_lock_module._remove_quarantine_marker
    remove_calls = 0

    def fail_second_unlink(*args, **kwargs) -> None:
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 2:
            raise OSError("injected partial marker unlink")
        real_remove(*args, **kwargs)

    monkeypatch.setattr(
        writer_lock_module,
        "_remove_quarantine_marker",
        fail_second_unlink,
    )
    with pytest.raises(OSError, match="partial marker unlink"):
        store.operator_clear_global_writer_quarantine(
            confirmation=(
                "all_navigation_annotation_writer_process_groups_absent"
            ),
            operator_reference="OPS-OLD-PARTIAL-UNLINK",
            idempotency_key="global-clear-before-new-marker",
            writer_lock_path=writer_lock_path,
        )
    assert not first.exists()
    assert second.is_file()

    monkeypatch.setattr(
        writer_lock_module,
        "_remove_quarantine_marker",
        real_remove,
    )
    newer = ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="z_new_recovery",
    )
    with pytest.raises(AnnotationConflictError) as replayed:
        store.operator_clear_global_writer_quarantine(
            confirmation="all_navigation_annotation_writer_process_groups_absent",
            operator_reference="OPS-OLD-PARTIAL-UNLINK",
            idempotency_key="global-clear-before-new-marker",
            writer_lock_path=writer_lock_path,
        )
    assert replayed.value.code == "global_writer_quarantine_active"
    assert second.is_file()
    assert newer.is_file()

    refreshed = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-NEW-GLOBAL-CLEAR",
        idempotency_key="global-clear-after-new-marker",
        writer_lock_path=writer_lock_path,
    )
    assert refreshed["status"] == "global_quarantine_clear_confirmed"
    assert not navigation_writer_quarantine_present(writer_lock_path)


def test_new_recovery_after_global_intent_invalidates_old_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    writer_lock_path = tmp_path / "writer.lock"
    first = ensure_navigation_writer_quarantine(
        writer_lock_path,
        recovery_ref="first_recovery",
    )
    real_clearance = (
        annotation_store_module.navigation_writer_quarantine_clearance
    )
    second: Path | None = None

    @contextmanager
    def inject_new_recovery(*args, **kwargs):
        nonlocal second
        second = ensure_navigation_writer_quarantine(
            writer_lock_path,
            recovery_ref="second_recovery",
        )
        with real_clearance(*args, **kwargs) as state:
            yield state

    monkeypatch.setattr(
        annotation_store_module,
        "navigation_writer_quarantine_clearance",
        inject_new_recovery,
    )
    with pytest.raises(NavigationWriterQuarantinedError, match="changed"):
        store.operator_clear_global_writer_quarantine(
            confirmation=(
                "all_navigation_annotation_writer_process_groups_absent"
            ),
            operator_reference="OPS-INTENT-RACE",
            idempotency_key="global-intent-before-new-recovery",
            writer_lock_path=writer_lock_path,
        )
    assert first.is_file()
    assert second is not None and second.is_file()
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM writer_quarantine_actions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM writer_quarantine_action_completions"
        ).fetchone()[0] == 0

    monkeypatch.setattr(
        annotation_store_module,
        "navigation_writer_quarantine_clearance",
        real_clearance,
    )
    completed = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-INTENT-RACE-REFRESHED",
        idempotency_key="global-after-new-recovery",
        writer_lock_path=writer_lock_path,
    )
    assert completed["marker_was_present"] is True
    assert not navigation_writer_quarantine_present(writer_lock_path)


def test_operator_may_abandon_quarantined_recovery_and_release_scope(
    tmp_path: Path,
):
    database = tmp_path / "annotation.sqlite"
    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    store = AnnotationStore(database)
    service = AnnotationApplicationService(
        store=store,
        worker=AnnotationWorker(store, FakeAnnotationRuntime(work_root)),
        catalog=FakeCalibrationCatalog(),
        work_root=work_root,
        clip_data_root=clip_data,
    )
    request = CreateAnnotationJobRequest(
        dataset_date="20270605",
        source_clips=["20270605_160904"],
        calibration_profile_ref="20260529_go2w",
        calibration_content_sha256=PROFILE_SHA,
    )
    job = service.create_job(request, idempotency_key="create-abandon-recovery")
    claimed = store.claim_next_run(worker_id="crashed-worker")
    assert claimed is not None
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM runtime_leases")
    writer_lock_path = tmp_path / "writer.lock"
    assert store.recover_interrupted_runs(
        writer_lock_path=writer_lock_path,
    ) == 1
    failed = store.get_job(job["job_ref"])

    global_clear = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference="OPS-20260723-GLOBAL-ABANDON",
        idempotency_key="operator-global-abandon",
        writer_lock_path=writer_lock_path,
    )
    abandoned = store.operator_confirm_recovery(
        job_ref=job["job_ref"],
        expected_job_revision=failed["state_revision"],
        confirmation="old_process_group_absent",
        operator_reference="OPS-20260723-ABANDON",
        idempotency_key="operator-abandon-recovery",
        global_quarantine_action_ref=global_clear["action_ref"],
        writer_lock_path=writer_lock_path,
        disposition="abandon",
    )
    assert abandoned["status"] == "cancelled"
    assert abandoned["completion_outcome"] == (
        "abandoned_after_recovery_confirmation"
    )
    assert (
        store.operator_confirm_recovery(
            job_ref=job["job_ref"],
            expected_job_revision=failed["state_revision"],
            confirmation="old_process_group_absent",
            operator_reference="OPS-20260723-ABANDON",
            idempotency_key="operator-abandon-recovery",
            global_quarantine_action_ref=global_clear["action_ref"],
            writer_lock_path=writer_lock_path,
            disposition="abandon",
        )
        == abandoned
    )
    replacement = service.create_job(
        request,
        idempotency_key="replacement-after-recovery-confirmation",
    )
    assert replacement["job_ref"] != job["job_ref"]
    with sqlite3.connect(database) as connection:
        action = connection.execute(
            """
            SELECT action, confirmation, operator_reference
            FROM annotation_operator_actions
            """
        ).fetchone()
    assert action == (
        "abandon_recovery",
        "old_process_group_absent",
        "OPS-20260723-ABANDON",
    )


def test_annotation_request_models_forbid_unknown_fields(tmp_path: Path):
    client, _runtime = _make_client(tmp_path)
    with client:
        response = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "unknown"},
            json={
                "dataset_date": "20270605",
                "source_clips": ["20270605_160904"],
                "calibration_profile_ref": "20260529_go2w",
                "calibration_content_sha256": PROFILE_SHA,
                "server_path": "/private/path",
            },
        )
        assert response.status_code == 422
        assert "/private/path" not in response.text
        assert "server_path" not in response.text


@pytest.mark.parametrize("invalid_value", [True, "3", 3.0])
def test_annotation_request_rejects_non_integer_coordinates_without_echo(
    tmp_path: Path,
    invalid_value,
):
    client, _runtime = _make_client(tmp_path)
    with client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )
        segment = job["segments"][0]
        payload = _complete_target()
        payload["bbox"][0] = invalid_value
        payload["point"][0] = invalid_value
        response = client.put(
            (
                f"/api/annotation/jobs/{job['job_ref']}/segments/"
                f"{segment['segment_ref']}/draft"
            ),
            headers={"Idempotency-Key": f"strict-{type(invalid_value).__name__}"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": [payload],
            },
        )
        assert response.status_code == 422
        assert response.json() == {
            "detail": {
                "code": "invalid_annotation_request",
                "message": "The annotation request is invalid.",
            }
        }


@pytest.mark.parametrize("invalid_revision", [True, "3", 3.0])
def test_annotation_request_models_reject_non_integer_revisions(
    invalid_revision,
):
    with pytest.raises(ValidationError):
        ExpectedJobRevisionRequest(expected_job_revision=invalid_revision)
    with pytest.raises(ValidationError):
        DraftRequest(
            expected_segment_revision=invalid_revision,
            expected_draft_revision=None,
            targets=[],
        )
    with pytest.raises(ValidationError):
        DraftRequest(
            expected_segment_revision=0,
            expected_draft_revision=invalid_revision,
            targets=[],
        )
    with pytest.raises(ValidationError):
        SubmitRequest(
            expected_segment_revision=0,
            expected_draft_revision=invalid_revision,
        )


def test_manifest_catalog_exposes_processing_profiles_only():
    profiles = CalibrationCatalog.default().list_profiles()

    assert [profile["profile_ref"] for profile in profiles] == [
        "20260320",
        "20260529_go2w",
    ]
    assert all(len(profile["content_sha256"]) == 64 for profile in profiles)
    assert "20260409_U" not in {profile["profile_ref"] for profile in profiles}


def test_calibration_change_during_snapshot_returns_conflict_without_job(
    tmp_path: Path,
):
    class ReplacedCalibrationCatalog(FakeCalibrationCatalog):
        def snapshot(self, _profile, destination: Path):
            destination.mkdir(parents=True)
            (destination / "sensors.json").write_text(
                "replaced",
                encoding="utf-8",
            )
            raise AnnotationConflictError(
                "calibration_profile_changed",
                "The selected calibration changed during snapshotting.",
                current=self.profile.public_projection(),
            )

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=FakeAnnotationRuntime(work_root),
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=ReplacedCalibrationCatalog(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/annotation/jobs",
            headers={"Idempotency-Key": "changed-profile"},
            json={
                "dataset_date": "20270605",
                "source_clips": ["20270605_160904"],
                "calibration_profile_ref": "20260529_go2w",
                "calibration_content_sha256": PROFILE_SHA,
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "calibration_profile_changed"

    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM annotation_jobs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM calibration_snapshots"
        ).fetchone()[0] == 0
    jobs_root = work_root / "jobs"
    assert not jobs_root.exists() or list(jobs_root.iterdir()) == []


def test_runtime_failure_keeps_private_detail_out_of_public_api(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    class FailingRuntime(FakeAnnotationRuntime):
        def prepare(self, request):
            request.step_observer(
                RuntimeStepEvent(
                    safe_step_code="processing_calibration_snapshot",
                    status="started",
                )
            )
            raise RuntimeExecutionError(
                "preparation_failed",
                "A frozen command failed.",
                return_code=23,
                diagnostic_kind="nonzero_exit",
                private_detail=(
                    f"oracle mismatch at {tmp_path}/private/candidate.json"
                ),
            )

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=FailingRuntime(work_root),
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        created = _create_job(client)
        failed = _wait_for_status(client, created["job_ref"], "failed")
        assert failed["failure"]["error_ref"].startswith("annotation_error_")
        assert str(tmp_path) not in json.dumps(failed)
        assert "oracle mismatch" not in json.dumps(failed)

    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        detail = connection.execute(
            "SELECT private_failure_detail FROM annotation_jobs"
        ).fetchone()[0]
        failed_step = connection.execute(
            """
            SELECT status, return_code, diagnostic_ref
            FROM runtime_run_steps
            """
        ).fetchone()
    assert "oracle mismatch" in detail
    assert str(tmp_path) in detail
    assert failed_step[0] == "failed"
    assert failed_step[1] == 23
    assert failed_step[2].startswith("runtime_step_nonzero_exit_")
    assert str(tmp_path) not in failed_step[2]
    assert "oracle mismatch" not in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize("tamper_checkpoint", [False, True])
def test_tracking_retry_verifies_committed_target_checkpoint(
    tmp_path: Path,
    tamper_checkpoint: bool,
):
    class FailsSecondInvocation(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.invocations = 0
            self.identities: list[str] = []
            self.attestation_sizes: list[int] = []

        def track(self, request):
            self.invocations += 1
            self.identities.append(request.targets[0].identity)
            self.attestation_sizes.append(
                len(request.attestation_targets),
            )
            if self.invocations == 2:
                raise RuntimeExecutionError("tracking_failed", "private failure")
            return super().track(request)

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    runtime = FailsSecondInvocation(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        base = f"/api/annotation/jobs/{job['job_ref']}/segments/{segment['segment_ref']}"
        draft = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "checkpoint-draft"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": [_complete_target(), _second_complete_target()],
            },
        ).json()
        client.post(
            f"{base}/submit",
            headers={"Idempotency-Key": "checkpoint-submit"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": draft["draft_revision"],
            },
        )
        ready = client.get(f"/api/annotation/jobs/{job['job_ref']}").json()
        client.post(
            f"/api/annotation/jobs/{job['job_ref']}/tracking",
            headers={"Idempotency-Key": "checkpoint-track"},
            json={"expected_job_revision": ready["state_revision"]},
        )
        failed = _wait_for_status(client, job["job_ref"], "failed")
        assert failed["failure"]["retryable"] is True
        if tamper_checkpoint:
            with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
                output_dir = Path(
                    connection.execute(
                        "SELECT private_output_dir FROM tracking_checkpoints"
                    ).fetchone()[0]
                )
            (output_dir / "tampered.jpg").write_bytes(b"changed")
        retry = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/retry",
            headers={"Idempotency-Key": "checkpoint-retry"},
            json={"expected_job_revision": failed["state_revision"]},
        )
        assert retry.status_code == 200
        terminal = _wait_for_status(
            client,
            job["job_ref"],
            "failed" if tamper_checkpoint else "tracked",
        )

    assert runtime.invocations == (2 if tamper_checkpoint else 3)
    assert runtime.attestation_sizes == [2] * runtime.invocations
    assert runtime.identities[0].startswith("master_")
    assert runtime.identities.count(runtime.identities[0]) == 1
    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM tracking_checkpoints"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE tracking_checkpoints SET artifact_sha256 = 'x'"
            )
    if tamper_checkpoint:
        assert checkpoint_count == 1
        assert terminal["failure"]["code"] == "recovery_required"
        assert terminal["failure"]["retryable"] is False
    else:
        assert checkpoint_count == 2


@pytest.mark.parametrize(
    ("failure_point", "operator_disposition", "expected_checkpoint_count"),
    (
        ("record_checkpoint", "abandon", 0),
        ("complete_tracking", "retry", 1),
    ),
)
def test_tracking_ledger_closure_failure_requires_operator_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    operator_disposition: str,
    expected_checkpoint_count: int,
) -> None:
    client, _runtime = _make_client(tmp_path)
    store = client.app.state.annotation_store
    with client:
        job = _wait_for_status(
            client,
            _create_job(
                client,
                key=f"{failure_point}-create",
            )["job_ref"],
            "waiting_initial_annotation",
        )
        if failure_point == "record_checkpoint":
            monkeypatch.setattr(
                store,
                "record_tracking_checkpoint",
                lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("simulated checkpoint ledger failure"),
                ),
            )
        else:
            monkeypatch.setattr(
                store,
                "complete_tracking",
                lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("simulated terminal ledger failure"),
                ),
            )

        _start_single_target_tracking(
            client,
            job,
            key_prefix=failure_point,
        )
        failed = _wait_for_status(client, job["job_ref"], "failed")
        assert failed["failure"]["code"] == "recovery_required"
        assert failed["failure"]["retryable"] is False

        ordinary_retry = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/retry",
            headers={"Idempotency-Key": f"{failure_point}-ordinary-retry"},
            json={"expected_job_revision": failed["state_revision"]},
        )
        assert ordinary_retry.status_code == 409
        ordinary_cancel = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/cancel",
            headers={"Idempotency-Key": f"{failure_point}-ordinary-cancel"},
            json={"expected_job_revision": failed["state_revision"]},
        )
        assert ordinary_cancel.status_code == 409
        assert (
            ordinary_cancel.json()["detail"]["code"]
            == "recovery_confirmation_required"
        )

    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tracking_checkpoints",
        ).fetchone()[0] == expected_checkpoint_count

    writer_lock_path = tmp_path / f"{failure_point}.writer.lock"
    global_clear = store.operator_clear_global_writer_quarantine(
        confirmation="all_navigation_annotation_writer_process_groups_absent",
        operator_reference=f"OPS-{failure_point}",
        idempotency_key=f"{failure_point}-global-clear",
        writer_lock_path=writer_lock_path,
    )
    recovered = store.operator_confirm_recovery(
        job_ref=job["job_ref"],
        expected_job_revision=failed["state_revision"],
        confirmation="old_process_group_absent",
        operator_reference=f"OPS-{failure_point}",
        idempotency_key=f"{failure_point}-operator-recovery",
        global_quarantine_action_ref=global_clear["action_ref"],
        writer_lock_path=writer_lock_path,
        disposition=operator_disposition,
    )
    assert recovered["status"] == (
        "cancelled" if operator_disposition == "abandon" else "tracking"
    )
    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        lease_count = connection.execute(
            "SELECT COUNT(*) FROM annotation_source_leases",
        ).fetchone()[0]
        queued_count = connection.execute(
            "SELECT COUNT(*) FROM runtime_runs WHERE status = 'queued'",
        ).fetchone()[0]
    if operator_disposition == "abandon":
        assert lease_count == 0
        assert queued_count == 0
    else:
        assert lease_count == 1
        assert queued_count == 1


def test_worker_tracks_legacy_yaml_filename_order_with_other10_before_other2(
    tmp_path: Path,
):
    class RecordingRuntime(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.identities: list[str] = []

        def track(self, request):
            self.identities.append(request.targets[0].identity)
            return super().track(request)

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    runtime = RecordingRuntime(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )
        segment = job["segments"][0]
        targets = []
        for index in range(11):
            targets.append(
                {
                    "target_ref": f"target_{index + 1:032x}",
                    "bbox": [10 + index, 20 + index, 30, 40],
                    "point": [15 + index, 25 + index],
                    "colors": {
                        "upper": "green",
                        "lower": "gray",
                        "shoes": "white",
                    },
                }
            )
        base = (
            f"/api/annotation/jobs/{job['job_ref']}/segments/"
            f"{segment['segment_ref']}"
        )
        draft = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "order-draft"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": targets,
            },
        ).json()
        client.post(
            f"{base}/submit",
            headers={"Idempotency-Key": "order-submit"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": draft["draft_revision"],
            },
        )
        ready = client.get(f"/api/annotation/jobs/{job['job_ref']}").json()
        client.post(
            f"/api/annotation/jobs/{job['job_ref']}/tracking",
            headers={"Idempotency-Key": "order-tracking"},
            json={"expected_job_revision": ready["state_revision"]},
        )
        _wait_for_status(client, job["job_ref"], "tracked")

    identities = [
        f"{identity}_green_gray_white"
        for identity in ["master"] + [f"other{index}" for index in range(1, 11)]
    ]
    expected = sorted(
        identities,
        key=lambda identity: f"{identity}.yaml",
    )
    assert runtime.identities == expected
    assert runtime.identities.index(
        "other10_green_gray_white"
    ) < runtime.identities.index("other2_green_gray_white")


def test_prepare_retry_uses_a_distinct_runtime_attempt(tmp_path: Path):
    class FailsFirstPrepare(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.run_refs: list[str] = []

        def prepare(self, request):
            self.run_refs.append(request.run_ref)
            if len(self.run_refs) == 1:
                raise RuntimeExecutionError("preparation_failed", "private failure")
            return super().prepare(request)

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    runtime = FailsFirstPrepare(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        created = _create_job(client)
        failed = _wait_for_status(client, created["job_ref"], "failed")
        retry = client.post(
            f"/api/annotation/jobs/{created['job_ref']}/retry",
            headers={"Idempotency-Key": "prepare-retry"},
            json={"expected_job_revision": failed["state_revision"]},
        )
        assert retry.status_code == 200
        _wait_for_status(
            client,
            created["job_ref"],
            "waiting_initial_annotation",
        )

    assert len(runtime.run_refs) == 2
    assert runtime.run_refs[0] != runtime.run_refs[1]


def test_running_tracking_cancel_reaches_runtime_and_closes_run(tmp_path: Path):
    class BlockingTrackingRuntime(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.started = threading.Event()
            self.cancel_event = threading.Event()
            self.allow_exit = threading.Event()

        def track(self, _request):
            self.started.set()
            self.cancel_event.wait(timeout=3)
            self.allow_exit.wait(timeout=3)
            raise RuntimeExecutionError(
                "runtime_cancelled",
                "cancelled",
                diagnostic_kind="cancelled",
            )

        def cancel(self, job_ref: str) -> None:
            self.cancelled.append(job_ref)
            self.cancel_event.set()

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    runtime = BlockingTrackingRuntime(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _wait_for_status(client, _create_job(client)["job_ref"], "waiting_initial_annotation")
        segment = job["segments"][0]
        base = f"/api/annotation/jobs/{job['job_ref']}/segments/{segment['segment_ref']}"
        draft = client.put(
            f"{base}/draft",
            headers={"Idempotency-Key": "cancel-draft"},
            json={
                "expected_segment_revision": segment["state_revision"],
                "expected_draft_revision": None,
                "targets": [_complete_target()],
            },
        ).json()
        client.post(
            f"{base}/submit",
            headers={"Idempotency-Key": "cancel-submit"},
            json={
                "expected_segment_revision": draft["state_revision"],
                "expected_draft_revision": draft["draft_revision"],
            },
        )
        ready = client.get(f"/api/annotation/jobs/{job['job_ref']}").json()
        tracking = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/tracking",
            headers={"Idempotency-Key": "cancel-tracking"},
            json={"expected_job_revision": ready["state_revision"]},
        ).json()
        assert runtime.started.wait(timeout=2)
        # Simulate a second Web process: its Worker does not own the Runtime
        # call, so cancellation must cross the durable DB boundary and be
        # observed by the executing Worker's control poll.
        second_store = AnnotationStore(tmp_path / "annotation.sqlite")
        second_worker = AnnotationWorker(
            second_store,
            FakeAnnotationRuntime(tmp_path / "other-worker"),
        )
        second_service = AnnotationApplicationService(
            store=second_store,
            worker=second_worker,
            catalog=FakeCalibrationCatalog(),
            work_root=tmp_path / "other-worker",
            clip_data_root=clip_data,
        )
        cancelled = second_service.job_action(
            "cancel",
            job["job_ref"],
            ExpectedJobRevisionRequest(
                expected_job_revision=tracking["state_revision"]
            ),
            idempotency_key="cancel-running",
        )
        assert cancelled["status"] == "tracking"
        assert cancelled["cancel_requested"] is True
        assert second_worker.owns_active_run(job["job_ref"]) is False
        assert runtime.cancel_event.wait(timeout=2.5)
        assert runtime.cancelled == [job["job_ref"]]
        with pytest.raises(AnnotationConflictError) as conflict:
            second_service.create_job(
                CreateAnnotationJobRequest(
                    dataset_date="20270605",
                    source_clips=["20270605_160904"],
                    calibration_profile_ref="20260529_go2w",
                    calibration_content_sha256=PROFILE_SHA,
                ),
                idempotency_key="replacement-before-runtime-exit",
            )
        assert conflict.value.code == "annotation_scope_conflict"
        runtime.allow_exit.set()

        deadline = time.monotonic() + 2
        runtime_status = None
        while time.monotonic() < deadline:
            with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
                runtime_status = connection.execute(
                    """
                    SELECT status FROM runtime_runs
                    WHERE kind = 'tracking' ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()[0]
            if runtime_status == "cancelled":
                break
            time.sleep(0.01)
        assert runtime_status == "cancelled"
        with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
            failed_tracking_step = connection.execute(
                """
                SELECT return_code, diagnostic_ref
                FROM runtime_run_steps
                WHERE safe_step_code = 'tracking'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        assert failed_tracking_step[0] is None
        assert failed_tracking_step[1].startswith(
            "runtime_step_cancelled_",
        )
        replacement = second_service.create_job(
            CreateAnnotationJobRequest(
                dataset_date="20270605",
                source_clips=["20270605_160904"],
                calibration_profile_ref="20260529_go2w",
                calibration_content_sha256=PROFILE_SHA,
            ),
            idempotency_key="replacement-after-runtime-exit",
        )
        assert replacement["job_ref"] != job["job_ref"]


def test_fail_run_rechecks_durable_cancel_in_same_transaction(
    tmp_path: Path,
) -> None:
    clip_data = tmp_path / "datasets" / "clip_data"
    for clip in ("20270605_160904", "20270605_152930"):
        (clip_data / "20270605" / clip / "sync_data").mkdir(parents=True)
    work_root = tmp_path / "annotation-work"
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    service = AnnotationApplicationService(
        store=store,
        worker=AnnotationWorker(store, FakeAnnotationRuntime(work_root)),
        catalog=FakeCalibrationCatalog(),
        work_root=work_root,
        clip_data_root=clip_data,
    )
    first = service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_160904"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="cancel-fail-race-first",
    )
    first_run = store.claim_next_run(worker_id="race-worker")
    assert first_run is not None
    # This is the Worker's stale pre-check. The authoritative fail path must
    # read cancel_requested again inside its own BEGIN IMMEDIATE transaction.
    assert store.cancellation_requested_for_run(
        run_id=first_run["run_id"],
    ) is False
    pending_cancel = store.cancel_job(
        job_ref=first["job_ref"],
        expected_job_revision=first["state_revision"],
        idempotency_key="cancel-wins-before-fail",
    )
    assert pending_cancel["cancel_requested"] is True
    store.fail_run(
        run_id=first_run["run_id"],
        code="annotation_runtime_failed",
        message="late runtime failure",
        retryable=True,
    )
    cancelled = store.get_job(first["job_ref"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is False
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT status FROM runtime_runs WHERE id = ?",
            (first_run["run_id"],),
        ).fetchone()[0] == "cancelled"
        assert connection.execute(
            "SELECT COUNT(*) FROM annotation_source_leases WHERE job_id = "
            "(SELECT id FROM annotation_jobs WHERE job_ref = ?)",
            (first["job_ref"],),
        ).fetchone()[0] == 0

    second = service.create_job(
        CreateAnnotationJobRequest(
            dataset_date="20270605",
            source_clips=["20270605_152930"],
            calibration_profile_ref="20260529_go2w",
            calibration_content_sha256=PROFILE_SHA,
        ),
        idempotency_key="cancel-fail-race-second",
    )
    second_run = store.claim_next_run(worker_id="race-worker")
    assert second_run is not None
    store.fail_run(
        run_id=second_run["run_id"],
        code="annotation_runtime_failed",
        message="failure wins before cancel",
        retryable=True,
    )
    with pytest.raises(AnnotationConflictError):
        store.cancel_job(
            job_ref=second["job_ref"],
            expected_job_revision=second["state_revision"],
            idempotency_key="late-cancel-loses-cas",
        )
    assert store.get_job(second["job_ref"])["status"] == "failed"


def test_prepare_success_racing_with_cancel_does_not_publish(
    tmp_path: Path,
) -> None:
    class SuccessfulPrepareAfterCancel(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.started = threading.Event()
            self.cancel_seen = threading.Event()
            self.allow_return = threading.Event()

        def prepare(self, request):
            result = super().prepare(request)
            self.started.set()
            self.cancel_seen.wait(timeout=3)
            self.allow_return.wait(timeout=3)
            return result

        def cancel(self, job_ref: str) -> None:
            self.cancelled.append(job_ref)
            self.cancel_seen.set()

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True,
    )
    work_root = tmp_path / "annotation-work"
    runtime = SuccessfulPrepareAfterCancel(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _create_job(client, key="prepare-cancel-race-create")
        assert runtime.started.wait(timeout=2)
        current = client.get(
            f"/api/annotation/jobs/{job['job_ref']}",
        ).json()
        cancelled = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/cancel",
            headers={"Idempotency-Key": "prepare-cancel-race"},
            json={"expected_job_revision": current["state_revision"]},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        assert runtime.cancel_seen.wait(timeout=2)
        runtime.allow_return.set()
        terminal = _wait_for_status(
            client,
            job["job_ref"],
            "cancelled",
        )
        assert terminal["completion_outcome"] == "cancelled_by_user"

    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM annotation_segments",
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_manifests",
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM annotation_source_leases",
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM runtime_runs",
        ).fetchone()[0] == "cancelled"


def test_tracking_success_racing_with_cancel_requires_recovery_after_publish(
    tmp_path: Path,
) -> None:
    class SuccessfulTrackingAfterCancel(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.started = threading.Event()
            self.cancel_seen = threading.Event()
            self.allow_return = threading.Event()

        def track(self, request):
            self.started.set()
            self.cancel_seen.wait(timeout=3)
            self.allow_return.wait(timeout=3)
            return super().track(request)

        def cancel(self, job_ref: str) -> None:
            self.cancelled.append(job_ref)
            self.cancel_seen.set()

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True,
    )
    work_root = tmp_path / "annotation-work"
    runtime = SuccessfulTrackingAfterCancel(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _wait_for_status(
            client,
            _create_job(
                client,
                key="tracking-cancel-race-create",
            )["job_ref"],
            "waiting_initial_annotation",
        )
        tracking = _start_single_target_tracking(
            client,
            job,
            key_prefix="tracking-cancel-race",
        )
        assert runtime.started.wait(timeout=2)
        cancelled = client.post(
            f"/api/annotation/jobs/{job['job_ref']}/cancel",
            headers={"Idempotency-Key": "tracking-cancel-race-cancel"},
            json={
                "expected_job_revision": tracking["state_revision"],
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        assert runtime.cancel_seen.wait(timeout=2)
        runtime.allow_return.set()
        terminal = _wait_for_status(
            client,
            job["job_ref"],
            "failed",
        )
        assert terminal["failure"]["code"] == "recovery_required"
        assert terminal["failure"]["retryable"] is False

    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tracking_checkpoints",
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT COUNT(*) FROM artifact_manifests
            WHERE stage = 'tracking'
            """
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM annotation_source_leases",
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT status FROM runtime_runs
            WHERE kind = 'tracking'
            """
        ).fetchone()[0] == "failed"


def test_cancelled_running_job_crash_keeps_scope_quarantined(
    tmp_path: Path,
) -> None:
    class OrphanableTrackingRuntime(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.started = threading.Event()
            self.cancel_event = threading.Event()
            self.allow_exit = threading.Event()

        def track(self, _request):
            self.started.set()
            self.cancel_event.wait(timeout=4)
            self.allow_exit.wait(timeout=4)
            raise RuntimeExecutionError(
                "runtime_cancelled",
                "cancelled",
                diagnostic_kind="cancelled",
            )

        def cancel(self, job_ref: str) -> None:
            self.cancelled.append(job_ref)
            self.cancel_event.set()

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True,
    )
    work_root = tmp_path / "annotation-work"
    runtime = OrphanableTrackingRuntime(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )
        tracking = _start_single_target_tracking(
            client,
            job,
            key_prefix="cancel-crash",
        )
        assert runtime.started.wait(timeout=2)
        restarted_store = AnnotationStore(tmp_path / "annotation.sqlite")
        pending = restarted_store.cancel_job(
            job_ref=job["job_ref"],
            expected_job_revision=tracking["state_revision"],
            idempotency_key="cancel-before-crash",
        )
        assert pending["status"] == "tracking"
        assert pending["cancel_requested"] is True
        with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
            assert connection.execute(
                """
                SELECT COUNT(*) FROM annotation_source_leases
                WHERE job_id = (
                    SELECT id FROM annotation_jobs WHERE job_ref = ?
                )
                """,
                (job["job_ref"],),
            ).fetchone()[0] == 1
            connection.execute(
                "UPDATE runtime_leases SET expires_at = ?",
                ("2000-01-01T00:00:00.000+00:00",),
            )
        assert restarted_store.recover_interrupted_runs(
            writer_lock_path=tmp_path / "writer.lock",
        ) == 1
        quarantined = restarted_store.get_job(job["job_ref"])
        assert quarantined["status"] == "failed"
        assert quarantined["failure"]["code"] == "recovery_required"
        assert quarantined["cancel_requested"] is True
        replacement_worker = AnnotationWorker(
            restarted_store,
            FakeAnnotationRuntime(tmp_path / "replacement-worker"),
        )
        replacement_service = AnnotationApplicationService(
            store=restarted_store,
            worker=replacement_worker,
            catalog=FakeCalibrationCatalog(),
            work_root=tmp_path / "replacement-worker",
            clip_data_root=clip_data,
        )
        with pytest.raises(AnnotationConflictError) as conflict:
            replacement_service.create_job(
                CreateAnnotationJobRequest(
                    dataset_date="20270605",
                    source_clips=["20270605_160904"],
                    calibration_profile_ref="20260529_go2w",
                    calibration_content_sha256=PROFILE_SHA,
                ),
                idempotency_key="replacement-while-orphan-unknown",
            )
        assert conflict.value.code == "annotation_scope_conflict"
        runtime.cancel_event.set()
        runtime.allow_exit.set()


def test_valid_lease_is_not_recovered_but_expired_lease_fails_closed(
    tmp_path: Path,
):
    class BlockingTrackingRuntime(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.started = threading.Event()
            self.cancel_event = threading.Event()

        def track(self, _request):
            self.started.set()
            self.cancel_event.wait(timeout=4)
            raise RuntimeExecutionError("runtime_cancelled", "cancelled")

        def cancel(self, job_ref: str) -> None:
            self.cancelled.append(job_ref)
            self.cancel_event.set()

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    runtime = BlockingTrackingRuntime(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        job = _wait_for_status(
            client,
            _create_job(client)["job_ref"],
            "waiting_initial_annotation",
        )
        _start_single_target_tracking(client, job, key_prefix="lease")
        assert runtime.started.wait(timeout=2)
        second_store = AnnotationStore(tmp_path / "annotation.sqlite")
        assert second_store.recover_interrupted_runs(
            writer_lock_path=tmp_path / "writer.lock",
        ) == 0
        with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
            connection.execute(
                "UPDATE runtime_leases SET expires_at = ?",
                ("2000-01-01T00:00:00.000+00:00",),
            )
        assert second_store.recover_interrupted_runs(
            writer_lock_path=tmp_path / "writer.lock",
        ) == 1
        failed = client.get(
            f"/api/annotation/jobs/{job['job_ref']}"
        ).json()
        assert failed["status"] == "failed"
        assert failed["failure"]["code"] == "recovery_required"
        assert failed["failure"]["retryable"] is False
        assert runtime.cancel_event.wait(timeout=2.5)

    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        final_run = connection.execute(
            """
            SELECT status, failure_code
            FROM runtime_runs
            WHERE kind = 'tracking' ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert final_run == ("failed", "recovery_required")


def test_web_shutdown_waits_for_bound_runtime_cancellation(tmp_path: Path):
    class BlockingPrepareRuntime(FakeAnnotationRuntime):
        def __init__(self, work_root: Path) -> None:
            super().__init__(work_root)
            self.started = threading.Event()
            self.cancel_event = threading.Event()
            self.exited = threading.Event()

        def prepare(self, _request):
            self.started.set()
            self.cancel_event.wait(timeout=4)
            self.exited.set()
            raise RuntimeExecutionError("runtime_cancelled", "cancelled")

        def cancel(self, job_ref: str) -> None:
            self.cancelled.append(job_ref)
            self.cancel_event.set()

    clip_data = tmp_path / "datasets" / "clip_data"
    (clip_data / "20270605" / "20270605_160904" / "sync_data").mkdir(
        parents=True
    )
    work_root = tmp_path / "annotation-work"
    runtime = BlockingPrepareRuntime(work_root)
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
        annotation_db_path=tmp_path / "annotation.sqlite",
        annotation_runtime=runtime,
        annotation_work_root=work_root,
        annotation_clip_data_root=clip_data,
        annotation_catalog=FakeCalibrationCatalog(),
    )
    with TestClient(app) as client:
        created = _create_job(client)
        assert runtime.started.wait(timeout=2)

    assert runtime.exited.is_set()
    assert runtime.cancelled == [created["job_ref"]]
    with sqlite3.connect(tmp_path / "annotation.sqlite") as connection:
        run = connection.execute(
            """
            SELECT status, failure_code
            FROM runtime_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert run == ("failed", "runtime_interrupted")
