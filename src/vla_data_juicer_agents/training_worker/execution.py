from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence

from .client import CenterClientError, WorkerCenterClient
from .datasets import DATASET_MARKER_CONTRACT, DATASET_MARKER_NAME
from .identity import WorkerIdentity
from .ledger import WorkerLedger, process_identity_for_pid
from .resources import ResourceCollector


_SAFE_REF = re.compile(r"[A-Za-z0-9_.:-]{1,255}\Z")
_SAFE_ENVIRONMENT = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
VERSION_MARKER = ".datapilot-training-version.json"
SUPERVISOR_CONTRACT = "datapilot_training_supervisor_v1"
MAX_LOG_LINE_BYTES = 16 * 1024
MAX_LOG_BATCH_LINES = 200
MAX_LOG_BATCH_BYTES = 256 * 1024


class TrainingExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StartRequest:
    run_ref: str
    stage_ref: str
    stage_number: int
    owner_epoch: int
    version_label: str
    family_ref: str
    working_directory: Path
    entrypoint: Path
    output_root: Path
    output_directory: Path
    argv: tuple[str, ...]
    gpu_uuids: tuple[str, ...]
    runtime_environment: dict[str, object]
    monitoring_format: str
    dataset_manifest_path: Path | None
    dataset_manifest: dict[str, object] | None
    dataset_replicas: tuple[dict[str, object], ...]
    redactions: tuple[str, ...]


class TrainingExecutionManager:
    """Launch and supervise the fixed real-training action protocol."""

    def __init__(
        self,
        *,
        identity: WorkerIdentity,
        ledger: WorkerLedger,
        center_client: WorkerCenterClient,
        resource_collector: ResourceCollector,
        state_dir: Path,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_timeout_seconds: float = 30.0,
    ) -> None:
        self.identity = identity
        self.ledger = ledger
        self.center_client = center_client
        self.resource_collector = resource_collector
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._popen = popen
        self._clock = monotonic_clock
        self._sleep = sleep
        self.stop_timeout_seconds = stop_timeout_seconds
        self._last_heartbeat: dict[str, float] = {}

    def handle_action(self, action: Mapping[str, object]) -> dict[str, object]:
        action_ref = _ref(action.get("action_ref"), "action_ref")
        claim_token = action.get("claim_token")
        kind = action.get("kind")
        if (
            not isinstance(claim_token, str)
            or not 32 <= len(claim_token) <= 512
            or any(character in claim_token for character in ("\x00", "\r", "\n"))
        ):
            raise TrainingExecutionError(
                "training_action_invalid", "Training action claim token is invalid."
            )
        try:
            if kind == "start_training_stage":
                request = self._parse_start(action)
                self._start(request)
            elif kind == "stop_training_run":
                owner_epoch = action.get("owner_epoch")
                if (
                    not isinstance(owner_epoch, int)
                    or isinstance(owner_epoch, bool)
                    or owner_epoch < 1
                ):
                    raise TrainingExecutionError(
                        "training_action_invalid", "Owner epoch is invalid."
                    )
                self._stop(
                    _ref(action.get("run_ref"), "run_ref"), owner_epoch
                )
            else:
                raise TrainingExecutionError(
                    "training_action_unsupported", "Training action kind is unsupported."
                )
        except TrainingExecutionError as exc:
            return {
                "action_ref": action_ref,
                "claim_token": claim_token,
                "status": "failed",
                "error": {"code": exc.code, "message": exc.message},
            }
        return {
            "action_ref": action_ref,
            "claim_token": claim_token,
            "status": "succeeded",
        }

    def tick(self) -> None:
        for run in self.ledger.list_active_runs():
            self._collect_log(run)
            self._observe_supervisor(run)
            if run["state"] in {"running", "stopping"}:
                now = self._clock()
                run_ref = str(run["run_ref"])
                if now - self._last_heartbeat.get(run_ref, -10_000.0) >= 5.0:
                    self._queue(run, {"kind": "heartbeat", "stage_ref": run.get("stage_ref")})
                    gpu_samples = self._gpu_samples(run.get("gpu_uuids", []))
                    if gpu_samples:
                        self._queue(
                            run,
                            {
                                "kind": "metric",
                                "stage_ref": run.get("stage_ref"),
                                "gpus": gpu_samples,
                            },
                        )
                    self._last_heartbeat[run_ref] = now
        for run_ref in self.ledger.pending_run_refs():
            self._flush(run_ref)

    def reconciliation_updates(self) -> list[dict[str, object]]:
        updates: list[dict[str, object]] = []
        for row in self.ledger.list_active_runs():
            self._collect_log(row)
            self._observe_supervisor(row)
        for result in self.ledger.reconcile_active_runs():
            row = self.ledger.get_run(result.run_ref)
            if row is None:
                continue
            status = "running" if result.status == "matched" else "unresolved"
            if result.status == "missing":
                child_exists = False
                raw_state_path = row.get("supervisor_state_path")
                if isinstance(raw_state_path, str):
                    try:
                        supervisor_state = _read_json(Path(raw_state_path), 16_384)
                    except TrainingExecutionError:
                        supervisor_state = {}
                    child_pid = supervisor_state.get("child_pid")
                    child_exists = isinstance(child_pid, int) and _process_exists(child_pid)
                if not child_exists:
                    status = "lost"
            payload = {
                "kind": "reconciliation",
                "stage_ref": row.get("stage_ref"),
                "status": status,
                "reason": result.status,
            }
            self._queue(row, payload)
            updates.append(payload)
        return updates

    def _parse_start(self, action: Mapping[str, object]) -> StartRequest:
        payload = action.get("payload")
        if not isinstance(payload, dict):
            raise TrainingExecutionError(
                "training_action_invalid", "Training action payload is invalid."
            )
        run_ref = _ref(action.get("run_ref"), "run_ref")
        stage_ref = _ref(payload.get("stage_ref"), "stage_ref")
        owner_epoch = action.get("owner_epoch")
        stage_number = payload.get("stage_number")
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise TrainingExecutionError("training_action_invalid", "Owner epoch is invalid.")
        if not isinstance(stage_number, int) or isinstance(stage_number, bool) or not 1 <= stage_number <= 10:
            raise TrainingExecutionError("training_action_invalid", "Stage number is invalid.")
        version_label = _safe_label(payload.get("version_label"), "version_label")
        family_ref = _ref(payload.get("family_ref"), "family_ref")
        working_directory = _existing_directory(payload.get("working_directory"), "working_directory")
        raw_entrypoint = payload.get("entrypoint")
        if not isinstance(raw_entrypoint, str) or not _safe_relative_path(raw_entrypoint):
            raise TrainingExecutionError(
                "training_entrypoint_invalid", "Training entrypoint must be relative to the working directory."
            )
        try:
            entrypoint = (working_directory / raw_entrypoint).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise TrainingExecutionError(
                "training_entrypoint_missing", "Training entrypoint is unavailable."
            ) from exc
        if not _is_within(entrypoint, working_directory) or not entrypoint.is_file() or not os.access(entrypoint, os.R_OK):
            raise TrainingExecutionError(
                "training_entrypoint_unsafe", "Training entrypoint is outside the working directory or unreadable."
            )
        output_root = _absolute_path(payload.get("output_root"), "output_root")
        output_directory = _absolute_path(payload.get("output_directory"), "output_directory")
        if not _is_within(output_directory, output_root):
            raise TrainingExecutionError(
                "training_output_unsafe", "Stage output directory is outside the registered output root."
            )
        expected_version_root = output_root / family_ref / version_label
        expected_stage = expected_version_root / f"stage-{stage_number:02d}"
        if output_directory != expected_stage:
            raise TrainingExecutionError(
                "training_output_unsafe", "Stage output directory does not match the run identity."
            )
        raw_argv = payload.get("argv")
        if (
            not isinstance(raw_argv, list)
            or not 1 <= len(raw_argv) <= 4096
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 16_384
                or any(character in value for character in ("\x00", "\r", "\n"))
                for value in raw_argv
            )
        ):
            raise TrainingExecutionError("training_argv_invalid", "Training argv is invalid.")
        if raw_entrypoint not in raw_argv:
            raise TrainingExecutionError(
                "training_entrypoint_invalid", "Training argv does not contain the registered entrypoint."
            )
        gpu_uuids = payload.get("gpu_uuids")
        if (
            not isinstance(gpu_uuids, list)
            or not 1 <= len(gpu_uuids) <= 8
            or len(gpu_uuids) != len(set(gpu_uuids))
            or any(not isinstance(value, str) or not _SAFE_REF.fullmatch(value) for value in gpu_uuids)
        ):
            raise TrainingExecutionError("training_gpu_invalid", "Training GPU selection is invalid.")
        runtime = payload.get("runtime_environment", {"kind": "system"})
        if not isinstance(runtime, dict):
            raise TrainingExecutionError("training_runtime_invalid", "Training runtime is invalid.")
        monitoring = payload.get("monitoring", {"format": "plain"})
        monitoring_format = monitoring.get("format", "plain") if isinstance(monitoring, dict) else None
        if monitoring_format not in {"plain", "transformers", "jsonl"}:
            raise TrainingExecutionError("training_monitoring_invalid", "Training monitoring format is invalid.")
        manifest_path_raw = payload.get("dataset_manifest_path")
        manifest = payload.get("dataset_manifest")
        replicas = payload.get("dataset_replicas", [])
        redactions = payload.get("redactions", [])
        manifest_path = None
        if manifest_path_raw is not None:
            manifest_path = _absolute_path(manifest_path_raw, "dataset_manifest_path")
            if manifest_path != expected_version_root / "dataset-manifest.json" or not isinstance(manifest, dict):
                raise TrainingExecutionError("training_manifest_invalid", "Dataset manifest path is invalid.")
        elif manifest is not None:
            raise TrainingExecutionError("training_manifest_invalid", "Dataset manifest path is missing.")
        if not isinstance(replicas, list) or any(not isinstance(value, dict) for value in replicas):
            raise TrainingExecutionError("training_manifest_invalid", "Dataset replicas are invalid.")
        if (
            not isinstance(redactions, list)
            or len(redactions) > 64
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 4096
                for value in redactions
            )
        ):
            raise TrainingExecutionError("training_action_invalid", "Training log redactions are invalid.")
        return StartRequest(
            run_ref=run_ref,
            stage_ref=stage_ref,
            stage_number=stage_number,
            owner_epoch=owner_epoch,
            version_label=version_label,
            family_ref=family_ref,
            working_directory=working_directory,
            entrypoint=entrypoint,
            output_root=output_root,
            output_directory=output_directory,
            argv=tuple(raw_argv),
            gpu_uuids=tuple(gpu_uuids),
            runtime_environment=dict(runtime),
            monitoring_format=str(monitoring_format),
            dataset_manifest_path=manifest_path,
            dataset_manifest=dict(manifest) if isinstance(manifest, dict) else None,
            dataset_replicas=tuple(dict(value) for value in replicas),
            redactions=tuple(str(value) for value in redactions),
        )

    def _start(self, request: StartRequest) -> None:
        existing = self.ledger.get_run(request.run_ref)
        if existing is not None:
            if (
                existing.get("stage_ref") == request.stage_ref
                and existing.get("owner_epoch") == request.owner_epoch
                and existing.get("state") == "running"
            ):
                return
            if (
                existing.get("stage_ref") == request.stage_ref
                and existing.get("owner_epoch") == request.owner_epoch
                and existing.get("state") == "accepted"
                and self._resume_launch_intent(request, existing)
            ):
                return
            if existing.get("state") in {"running", "stopping"}:
                raise TrainingExecutionError(
                    "training_run_already_active", "This run already has an active process."
                )
        gpu_indexes = self._gpu_indexes(request.gpu_uuids)
        resolved_output_root = _prepare_output_root(request.output_root)
        version_root = request.output_directory.parent
        self._prepare_version_root(request, version_root)
        request.output_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        _assert_no_symlink_components(request.output_directory)
        resolved_output_directory = request.output_directory.resolve(strict=True)
        if (
            request.output_directory.is_symlink()
            or not resolved_output_directory.is_dir()
            or not _is_within(resolved_output_directory, resolved_output_root)
        ):
            raise TrainingExecutionError("training_output_unsafe", "Stage output directory is unsafe.")
        argv = self._runtime_argv(
            request.runtime_environment,
            request.argv,
            request.working_directory,
        )
        environment = {
            "CUDA_VISIBLE_DEVICES": ",".join(str(index) for index in gpu_indexes),
            "DATAPILOT_RUN_REF": request.run_ref,
            "DATAPILOT_STAGE_REF": request.stage_ref,
            "DATAPILOT_VERSION_LABEL": request.version_label,
            "PYTHONUNBUFFERED": "1",
        }
        private_root = version_root / ".datapilot"
        private_root.mkdir(mode=0o700, exist_ok=True)
        log_path = request.output_directory / ".datapilot-training.log"
        supervisor_state = private_root / f"stage-{request.stage_number:02d}-supervisor.json"
        spec_path = private_root / f"stage-{request.stage_number:02d}-supervisor-spec.json"
        commit_path = private_root / f"stage-{request.stage_number:02d}-launch-commit.json"
        lock_path = private_root / f"stage-{request.stage_number:02d}-launch.lock"
        launch_token = (
            str(existing["launch_token"])
            if existing is not None
            and existing.get("state") == "accepted"
            and isinstance(existing.get("launch_token"), str)
            else secrets.token_urlsafe(32)
        )
        self.ledger.record_process_observation(
            run_ref=request.run_ref,
            state="accepted",
            gpu_uuids=request.gpu_uuids,
            working_directory=str(request.working_directory),
            stdout_path=str(log_path),
            stderr_path=str(log_path),
            stage_ref=request.stage_ref,
            stage_number=request.stage_number,
            owner_epoch=request.owner_epoch,
            version_label=request.version_label,
            output_directory=str(request.output_directory),
            supervisor_state_path=str(supervisor_state),
            monitoring_format=request.monitoring_format,
            redactions=request.redactions,
            launch_token=launch_token,
        )
        _atomic_json(
            spec_path,
            {
                "argv": argv,
                "working_directory": str(request.working_directory),
                "log_path": str(log_path),
                "state_path": str(supervisor_state),
                "commit_path": str(commit_path),
                "lock_path": str(lock_path),
                "launch_token": launch_token,
                "environment": environment,
            },
        )
        supervisor_command = _supervisor_command(spec_path)
        try:
            supervisor = self._popen(
                supervisor_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise TrainingExecutionError(
                "training_process_start_failed", "Worker could not start the training supervisor."
            ) from exc
        state = _wait_for_supervisor(supervisor_state, supervisor, self._clock, self._sleep)
        if (
            state.get("contract") != SUPERVISOR_CONTRACT
            or state.get("launch_token") != launch_token
            or state.get("supervisor_pid") != supervisor.pid
            or state.get("status") not in {
                "waiting",
            "running",
            "exited",
            }
        ):
            _terminate_failed_supervisor(supervisor, state)
            raise TrainingExecutionError(
                "training_process_start_failed", "Training process did not start successfully."
            )
        observation = process_identity_for_pid(supervisor.pid)
        try:
            self.ledger.attach_launch_supervisor(
                request.run_ref,
                pid=supervisor.pid,
                process_start_marker=(
                    observation.process_start_marker if observation else None
                ),
                argv_digest=(
                    observation.argv_digest
                    if observation
                    else _argv_digest(supervisor_command)
                ),
            )
        except Exception as exc:
            _terminate_failed_supervisor(supervisor, state)
            raise TrainingExecutionError(
                "training_ledger_write_failed", "Worker could not persist the training process identity."
            ) from exc
        _atomic_json(
            commit_path, {"launch_token": launch_token, "decision": "commit"}
        )
        state = _wait_for_training_start(
            supervisor_state, supervisor, launch_token, self._clock, self._sleep
        )
        if state.get("status") not in {"running", "exited"}:
            _terminate_failed_supervisor(supervisor, state)
            raise TrainingExecutionError(
                "training_process_start_failed", "Training process did not start successfully."
            )
        self.ledger.update_run_state(request.run_ref, "running")
        row = self.ledger.get_run(request.run_ref)
        assert row is not None
        self._queue_launch_started(row, request)
        self._flush(request.run_ref)

    def _resume_launch_intent(
        self, request: StartRequest, existing: Mapping[str, object]
    ) -> bool:
        if (
            existing.get("working_directory") != str(request.working_directory)
            or existing.get("output_directory") != str(request.output_directory)
            or existing.get("version_label") != request.version_label
            or tuple(existing.get("gpu_uuids") or ()) != tuple(sorted(request.gpu_uuids))
        ):
            raise TrainingExecutionError(
                "training_launch_intent_mismatch",
                "The retried training action does not match the durable launch intent.",
            )
        launch_token = existing.get("launch_token")
        raw_state_path = existing.get("supervisor_state_path")
        if not isinstance(launch_token, str) or not isinstance(raw_state_path, str):
            raise TrainingExecutionError(
                "training_launch_unresolved",
                "Worker cannot safely resolve the existing training launch intent.",
            )
        state_path = Path(raw_state_path)
        if not state_path.exists():
            pid = existing.get("pid")
            if isinstance(pid, int) and _process_exists(pid):
                raise TrainingExecutionError(
                    "training_launch_unresolved",
                    "Training supervisor exists but its durable state is not yet available.",
                )
            return False
        state = _read_json(state_path, 16_384)
        if (
            state.get("contract") != SUPERVISOR_CONTRACT
            or state.get("launch_token") != launch_token
        ):
            raise TrainingExecutionError(
                "training_launch_unresolved",
                "Training supervisor state does not match the durable launch intent.",
            )
        status = state.get("status")
        supervisor_pid = state.get("supervisor_pid")
        child_pid = state.get("child_pid")
        if status == "exited":
            self.ledger.update_run_state(request.run_ref, "running")
            row = self.ledger.get_run(request.run_ref)
            assert row is not None
            self._queue_launch_started(row, request)
            self._observe_supervisor(row)
            self._flush(request.run_ref)
            return True
        if not isinstance(supervisor_pid, int) or not _process_exists(supervisor_pid):
            if isinstance(child_pid, int) and _process_exists(child_pid):
                raise TrainingExecutionError(
                    "training_launch_unresolved",
                    "Training child process exists without a verifiable supervisor.",
                )
            if status == "waiting":
                state_path.unlink(missing_ok=True)
                return False
            raise TrainingExecutionError(
                "training_launch_unresolved",
                "Worker cannot confirm whether the previous training launch is still active.",
            )
        observation = process_identity_for_pid(supervisor_pid)
        self.ledger.attach_launch_supervisor(
            request.run_ref,
            pid=supervisor_pid,
            process_start_marker=(observation.process_start_marker if observation else None),
            argv_digest=(observation.argv_digest if observation else None),
        )
        commit_path = state_path.with_name(
            f"stage-{request.stage_number:02d}-launch-commit.json"
        )
        _atomic_json(
            commit_path, {"launch_token": launch_token, "decision": "commit"}
        )
        state = _wait_for_training_start(
            state_path, None, launch_token, self._clock, self._sleep
        )
        if state.get("status") not in {"running", "exited"}:
            raise TrainingExecutionError(
                "training_launch_unresolved",
                "Training supervisor did not confirm the recovered launch.",
            )
        self.ledger.update_run_state(request.run_ref, "running")
        row = self.ledger.get_run(request.run_ref)
        assert row is not None
        self._queue_launch_started(row, request)
        self._observe_supervisor(row)
        self._flush(request.run_ref)
        return True

    def _queue_launch_started(
        self, row: Mapping[str, object], request: StartRequest
    ) -> None:
        self._queue(row, {"kind": "accepted", "stage_ref": request.stage_ref})
        self._queue(
            row,
            {
                "kind": "started",
                "stage_ref": request.stage_ref,
                "stage_number": request.stage_number,
            },
        )

    def _prepare_version_root(self, request: StartRequest, version_root: Path) -> None:
        _assert_no_symlink_components(version_root)
        marker_path = version_root / VERSION_MARKER
        if version_root.exists():
            if version_root.is_symlink() or not version_root.is_dir():
                raise TrainingExecutionError("training_output_unsafe", "Version output path is unsafe.")
            if marker_path.exists():
                marker = _read_json(marker_path, 16_384)
                if (
                    marker.get("contract") != "datapilot_training_version_v1"
                    or marker.get("run_ref") != request.run_ref
                    or marker.get("version_label") != request.version_label
                ):
                    raise TrainingExecutionError(
                        "training_output_marker_mismatch", "Version output belongs to another run."
                    )
            elif any(version_root.iterdir()):
                raise TrainingExecutionError(
                    "training_output_not_managed", "Version output directory is not managed by DataPilot."
                )
        else:
            version_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            version_root.mkdir(mode=0o700)
        _assert_no_symlink_components(version_root)
        if not marker_path.exists():
            _atomic_json(
                marker_path,
                {
                    "contract": "datapilot_training_version_v1",
                    "run_ref": request.run_ref,
                    "family_ref": request.family_ref,
                    "version_label": request.version_label,
                },
            )
        if request.dataset_manifest is not None and request.dataset_manifest_path is not None:
            if (
                request.dataset_manifest.get("contract") != "datapilot_dataset_manifest_v1"
                or request.dataset_manifest.get("run_ref") != request.run_ref
                or request.dataset_manifest.get("family_ref") != request.family_ref
            ):
                raise TrainingExecutionError(
                    "training_manifest_invalid", "Dataset manifest identity does not match this run."
                )
            self._verify_replicas(request.dataset_replicas, request.dataset_manifest)
            if request.dataset_manifest_path.exists():
                current = _read_json(request.dataset_manifest_path, 16 * 1024 * 1024)
                if current != request.dataset_manifest:
                    raise TrainingExecutionError(
                        "training_manifest_changed", "Existing dataset manifest does not match this run."
                    )
            else:
                _atomic_json(request.dataset_manifest_path, request.dataset_manifest)

    def _verify_replicas(
        self,
        replicas: Sequence[Mapping[str, object]],
        manifest: Mapping[str, object],
    ) -> None:
        expected: dict[str, tuple[str, str, str]] = {}
        splits = manifest.get("splits")
        if not isinstance(splits, dict):
            raise TrainingExecutionError("training_manifest_invalid", "Dataset manifest splits are invalid.")
        for split in ("train", "test"):
            values = splits.get(split, [])
            if not isinstance(values, list):
                raise TrainingExecutionError("training_manifest_invalid", "Dataset manifest split is invalid.")
            for value in values:
                if not isinstance(value, dict):
                    raise TrainingExecutionError("training_manifest_invalid", "Dataset manifest item is invalid.")
                root = value.get("local_root")
                release = value.get("release_ref")
                date = value.get("dataset_date")
                digest = value.get("inventory_sha256")
                if not all(isinstance(item, str) for item in (root, release, date, digest)):
                    raise TrainingExecutionError("training_manifest_invalid", "Dataset manifest item is incomplete.")
                expected[str(root)] = (str(release), str(date), str(digest))
        supplied = {
            str(value.get("local_root")): (
                str(value.get("release_ref")),
                str(value.get("dataset_date")),
                str(value.get("inventory_sha256")),
            )
            for value in replicas
        }
        if supplied != expected:
            raise TrainingExecutionError("training_manifest_invalid", "Dataset replicas do not match the manifest.")
        for raw_root, (release_ref, dataset_date, digest) in expected.items():
            root = _existing_directory(raw_root, "dataset replica")
            marker = _read_json(root / DATASET_MARKER_NAME, 16_384)
            if (
                marker.get("contract") != DATASET_MARKER_CONTRACT
                or marker.get("release_ref") != release_ref
                or marker.get("dataset_date") != dataset_date
                or marker.get("inventory_sha256") != digest
                or not _SHA256.fullmatch(digest)
            ):
                raise TrainingExecutionError(
                    "training_dataset_replica_invalid", "A managed dataset marker is missing or changed."
                )

    def _gpu_indexes(self, requested: Sequence[str]) -> list[int]:
        resources = self.resource_collector.collect()
        values = resources.get("gpus")
        by_uuid = {
            str(value.get("uuid")): value.get("index")
            for value in values if isinstance(value, dict)
        } if isinstance(values, list) else {}
        missing = [value for value in requested if value not in by_uuid]
        indexes = [by_uuid[value] for value in requested if value in by_uuid]
        if missing or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in indexes):
            raise TrainingExecutionError(
                "training_gpu_not_found", "One or more selected GPU UUIDs are unavailable on this node."
            )
        return [int(value) for value in indexes]

    def _gpu_samples(self, requested: object) -> list[dict[str, object]]:
        if not isinstance(requested, (list, tuple)):
            return []
        requested_set = {
            value for value in requested if isinstance(value, str)
        }
        resources = self.resource_collector.collect()
        values = resources.get("gpus")
        samples: list[dict[str, object]] = []
        if not isinstance(values, list):
            return samples
        for value in values:
            if not isinstance(value, dict) or value.get("uuid") not in requested_set:
                continue
            utilization = value.get("utilization_percent")
            memory_used = value.get("memory_used_bytes")
            temperature = value.get("temperature_celsius")
            sample: dict[str, object] = {
                "uuid": value["uuid"],
                "index": value.get("index"),
            }
            if _finite_number(utilization):
                sample["utilization_percent"] = float(utilization)
            if (
                isinstance(memory_used, int)
                and not isinstance(memory_used, bool)
                and memory_used >= 0
            ):
                sample["gpu_memory_mib"] = memory_used / (1024 * 1024)
            if _finite_number(temperature):
                sample["temperature_celsius"] = float(temperature)
            samples.append(sample)
        return samples

    def _runtime_argv(
        self,
        runtime: Mapping[str, object],
        argv: Sequence[str],
        working_directory: Path,
    ) -> list[str]:
        kind = runtime.get("kind", "system")
        if kind == "system":
            raw_executable = argv[0]
            if "/" not in raw_executable:
                executable = shutil.which(raw_executable)
            else:
                candidate = Path(raw_executable)
                relative_candidate = not candidate.is_absolute()
                if relative_candidate:
                    if ".." in PurePosixPath(raw_executable).parts:
                        raise TrainingExecutionError(
                            "training_executable_unsafe",
                            "Relative training executable must remain inside the working directory.",
                        )
                    candidate = working_directory / candidate
                try:
                    resolved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    resolved = candidate
                if relative_candidate and not _is_within(
                    resolved, working_directory.resolve()
                ):
                    raise TrainingExecutionError(
                        "training_executable_unsafe",
                        "Relative training executable must remain inside the working directory.",
                    )
                executable = str(resolved)
            if (
                not executable
                or not Path(executable).is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise TrainingExecutionError("training_executable_missing", "Training executable is unavailable.")
            return [executable, *argv[1:]]
        if kind != "conda":
            raise TrainingExecutionError("training_runtime_invalid", "Training runtime kind is unsupported.")
        environment = runtime.get("conda_environment")
        if not isinstance(environment, str) or not _SAFE_ENVIRONMENT.fullmatch(environment):
            raise TrainingExecutionError("training_runtime_invalid", "Conda environment name is invalid.")
        conda = os.environ.get("DATAPILOT_CONDA_EXECUTABLE") or shutil.which("conda")
        if not conda or not os.path.isabs(conda) or not os.access(conda, os.X_OK):
            raise TrainingExecutionError("training_conda_missing", "Configured Conda executable is unavailable.")
        return [conda, "run", "--no-capture-output", "-n", environment, *argv]

    def _stop(self, run_ref: str, owner_epoch: int) -> None:
        row = self.ledger.get_run(run_ref)
        if row is not None and row.get("state") == "cancelled":
            return
        if row is None or row.get("state") not in {"accepted", "running", "stopping"}:
            raise TrainingExecutionError(
                "training_stop_unresolved",
                "Worker cannot confirm an active process for this training run.",
            )
        if int(row.get("owner_epoch") or 0) != owner_epoch:
            raise TrainingExecutionError(
                "training_stop_owner_epoch_mismatch",
                "Worker no longer owns this training run epoch.",
            )
        was_launching = row.get("state") == "accepted"
        self.ledger.update_run_state(run_ref, "stopping")
        state_path = row.get("supervisor_state_path")
        state: dict[str, object] = {}
        if state_path and Path(str(state_path)).exists():
            state = _read_json(Path(str(state_path)), 16_384)
        if was_launching:
            launch_token = row.get("launch_token")
            if not isinstance(launch_token, str) or not state_path:
                self.ledger.update_run_state(run_ref, "unknown")
                raise TrainingExecutionError(
                    "training_stop_unresolved",
                    "Worker cannot safely cancel the pending training launch.",
                )
            commit_path = Path(str(state_path)).with_name(
                f"stage-{int(row.get('stage_number') or 0):02d}-launch-commit.json"
            )
            _atomic_json(
                commit_path,
                {"launch_token": launch_token, "decision": "cancel"},
            )
        child_pid = state.get("child_pid")
        if isinstance(child_pid, int) and child_pid > 0:
            try:
                os.killpg(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = self._clock() + self.stop_timeout_seconds
            while self._clock() < deadline and _process_exists(child_pid):
                self._sleep(0.1)
            if _process_exists(child_pid):
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.ledger.update_run_state(run_ref, "cancelled")
        latest = self.ledger.get_run(run_ref)
        assert latest is not None
        self._collect_log(latest)
        self._queue(
            latest,
            {
                "kind": "exited",
                "stage_ref": latest.get("stage_ref"),
                "status": "cancelled",
                "exit_code": None,
            },
        )
        self._flush(run_ref)

    def _observe_supervisor(self, run: Mapping[str, object]) -> None:
        raw_path = run.get("supervisor_state_path")
        if not isinstance(raw_path, str):
            return
        try:
            state = _read_json(Path(raw_path), 16_384)
        except TrainingExecutionError:
            return
        if state.get("contract") != SUPERVISOR_CONTRACT or state.get("status") != "exited":
            return
        exit_code = state.get("exit_code")
        if not isinstance(exit_code, int):
            return
        run_ref = str(run["run_ref"])
        status = "succeeded" if exit_code == 0 else "failed"
        self.ledger.update_run_state(run_ref, status)
        latest = self.ledger.get_run(run_ref)
        assert latest is not None
        self._queue(
            latest,
            {
                "kind": "exited",
                "stage_ref": latest.get("stage_ref"),
                "status": status,
                "exit_code": exit_code,
                **(
                    {}
                    if exit_code == 0
                    else {
                        "failure_code": "training_process_exit",
                        "failure_message": f"Training process exited with status {exit_code}.",
                    }
                ),
            },
        )

    def _collect_log(self, run: Mapping[str, object]) -> None:
        raw_path = run.get("stdout_path")
        if not isinstance(raw_path, str):
            return
        path = Path(raw_path)
        if not path.is_file() or path.is_symlink():
            return
        offset = int(run.get("log_offset") or 0)
        try:
            size = path.stat().st_size
            if offset > size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                raw_lines: list[bytes] = []
                batch_bytes = 0
                while (
                    len(raw_lines) < MAX_LOG_BATCH_LINES
                    and batch_bytes < MAX_LOG_BATCH_BYTES
                ):
                    raw_line = stream.readline(
                        min(
                            MAX_LOG_LINE_BYTES + 1,
                            MAX_LOG_BATCH_BYTES - batch_bytes,
                        )
                    )
                    if not raw_line:
                        break
                    raw_lines.append(raw_line)
                    batch_bytes += len(raw_line)
                end_offset = stream.tell()
        except OSError:
            return
        if not raw_lines:
            return
        lines: list[str] = []
        metrics: list[dict[str, object]] = []
        for raw_line in raw_lines:
            line = raw_line.rstrip(b"\r\n")[:MAX_LOG_LINE_BYTES].decode(
                "utf-8", errors="replace"
            )
            for secret in run.get("redactions", []):
                if isinstance(secret, str) and secret:
                    line = line.replace(secret, "********")
            lines.append(line)
            event = _parse_event(line, str(run.get("monitoring_format") or "plain"))
            if event is not None:
                if event.get("kind") != "checkpoint" or _checkpoint_exists(event, run):
                    metrics.append(event)
        if not lines:
            return
        self.ledger.update_log_offset(str(run["run_ref"]), end_offset)
        message = "\n".join(lines)
        while message:
            chunk = message[:MAX_LOG_LINE_BYTES]
            message = message[len(chunk) :]
            self._queue(
                run,
                {
                    "kind": "log",
                    "stage_ref": run.get("stage_ref"),
                    "level": "info",
                    "message": chunk,
                },
            )
        for event in metrics:
            self._queue(run, {**event, "stage_ref": run.get("stage_ref")})

    def _queue(self, run: Mapping[str, object], payload: dict[str, object]) -> None:
        self.ledger.enqueue_update(
            str(run["run_ref"]), int(run.get("owner_epoch") or 0), payload
        )

    def _flush(self, run_ref: str) -> None:
        for update in self.ledger.pending_updates(run_ref, limit=100):
            owner_epoch = int(update.pop("owner_epoch"))
            worker_seq = int(update.pop("worker_seq"))
            try:
                self.center_client.publish_run_updates(
                    self.identity,
                    run_ref,
                    {
                        "owner_epoch": owner_epoch,
                        "worker_seq": worker_seq,
                        "updates": [update],
                    },
                )
            except CenterClientError:
                return
            self.ledger.acknowledge_updates(run_ref, owner_epoch, worker_seq)


def _parse_event(line: str, monitoring_format: str) -> dict[str, object] | None:
    if monitoring_format == "plain":
        return None
    candidate = line.strip()
    if candidate.startswith("DATAPILOT_EVENT "):
        candidate = candidate[len("DATAPILOT_EVENT ") :]
    parsed: object | None = None
    candidates = [candidate]
    if monitoring_format == "transformers":
        # Trainer normally prints a Python dict, and tqdm may prefix that dict
        # with a carriage-return progress bar.  Keep JSONL strict, but accept
        # flat embedded Trainer dictionaries such as
        # ``100%|...| {'loss': 0.4, 'learning_rate': 1e-5}``.
        candidates.extend(
            match.group(0)
            for match in re.finditer(r"\{[^{}\r\n]*\}", candidate)
            if match.group(0) != candidate
        )
    for item in candidates:
        try:
            parsed = json.loads(item)
        except json.JSONDecodeError:
            if monitoring_format != "transformers":
                return None
            try:
                parsed = ast.literal_eval(item)
            except (ValueError, SyntaxError):
                continue
        if isinstance(parsed, dict):
            break
        parsed = None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("contract") == "datapilot_training_event_v1":
        event_type = parsed.get("type")
        if event_type == "checkpoint":
            relative_path = parsed.get("relative_path")
            if not isinstance(relative_path, str) or not _safe_relative_path(relative_path):
                return None
            checkpoint: dict[str, object] = {
                "kind": "checkpoint",
                "relative_path": relative_path,
            }
            step = parsed.get("step")
            if isinstance(step, int) and not isinstance(step, bool) and step >= 0:
                checkpoint["step"] = step
            return checkpoint
        if event_type != "metric":
            return None
    metric: dict[str, object] = {}
    for key in ("step", "total_steps"):
        value = parsed.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            metric[key] = value
    for key in ("epoch", "loss", "learning_rate", "grad_norm"):
        value = parsed.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            metric[key] = value
    return {"kind": "metric", **metric} if metric else None


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _supervisor_command(spec_path: Path) -> list[str]:
    executable = Path(sys.argv[0])
    if executable.suffix == ".pyz" and executable.is_file():
        return [sys.executable, str(executable), "--supervise-spec", str(spec_path)]
    return [
        sys.executable,
        "-m",
        "vla_data_juicer_agents.training_worker.supervisor",
        str(spec_path),
    ]


def _wait_for_supervisor(
    state_path: Path,
    process: subprocess.Popen[bytes],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, object]:
    deadline = clock() + 10.0
    while clock() < deadline:
        if state_path.is_file():
            return _read_json(state_path, 16_384)
        if process.poll() is not None:
            return {"status": "exited", "exit_code": process.returncode}
        sleep(0.05)
    return {"status": "timeout"}


def _wait_for_training_start(
    state_path: Path,
    process: subprocess.Popen[bytes] | None,
    launch_token: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, object]:
    deadline = clock() + 10.0
    while clock() < deadline:
        if state_path.is_file():
            state = _read_json(state_path, 16_384)
            if (
                state.get("contract") == SUPERVISOR_CONTRACT
                and state.get("launch_token") == launch_token
                and state.get("status") in {"running", "exited"}
            ):
                return state
        if process is not None and process.poll() is not None:
            return {"status": "exited", "exit_code": process.returncode}
        sleep(0.05)
    return {"status": "timeout"}


def _terminate_failed_supervisor(
    process: subprocess.Popen[bytes], state: Mapping[str, object]
) -> None:
    child_pid = state.get("child_pid")
    if isinstance(child_pid, int) and child_pid > 0:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.terminate()
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _existing_directory(value: object, label: str) -> Path:
    path = _absolute_path(value, label)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TrainingExecutionError("training_path_missing", f"{label} does not exist.") from exc
    if not resolved.is_dir() or path.is_symlink():
        raise TrainingExecutionError("training_path_unsafe", f"{label} is unsafe.")
    return resolved


def _prepare_output_root(path: Path) -> Path:
    _assert_no_symlink_components(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise TrainingExecutionError(
            "training_output_write_failed", "Worker cannot create the registered output root."
        ) from exc
    _assert_no_symlink_components(path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TrainingExecutionError(
            "training_output_unsafe", "Registered output root is unsafe."
        ) from exc
    if path.is_symlink() or not resolved.is_dir() or not os.access(resolved, os.W_OK | os.X_OK):
        raise TrainingExecutionError(
            "training_output_unsafe", "Registered output root is unsafe or not writable."
        )
    return resolved


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                raise TrainingExecutionError(
                    "training_output_unsafe", "Training output path contains a symbolic link."
                )
        except OSError as exc:
            raise TrainingExecutionError(
                "training_output_unsafe", "Training output path cannot be inspected safely."
            ) from exc


def _absolute_path(value: object, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise TrainingExecutionError("training_path_invalid", f"{label} must be an absolute path.")
    path = Path(value)
    if any(part in {".", ".."} for part in PurePosixPath(value).parts):
        raise TrainingExecutionError("training_path_invalid", f"{label} is invalid.")
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _read_json(path: Path, maximum_bytes: int) -> dict[str, object]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
            raise OSError
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrainingExecutionError("training_marker_invalid", "Managed training metadata is missing or invalid.") from exc
    if not isinstance(payload, dict):
        raise TrainingExecutionError("training_marker_invalid", "Managed training metadata is invalid.")
    return payload


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise TrainingExecutionError("training_output_write_failed", "Worker cannot write managed training metadata.") from exc


def _ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise TrainingExecutionError("training_action_invalid", f"{label} is invalid.")
    return value


def _safe_label(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise TrainingExecutionError("training_action_invalid", f"{label} is invalid.")
    return value


def _argv_digest(argv: Sequence[str]) -> str:
    return hashlib.sha256(b"\0".join(value.encode("utf-8") for value in argv)).hexdigest()


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _checkpoint_exists(
    event: Mapping[str, object], run: Mapping[str, object]
) -> bool:
    relative = event.get("relative_path")
    output = run.get("output_directory")
    if not isinstance(relative, str) or not isinstance(output, str):
        return False
    output_root = Path(output).resolve()
    try:
        checkpoint = (output_root / relative).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return _is_within(checkpoint, output_root) and not checkpoint.is_symlink()
