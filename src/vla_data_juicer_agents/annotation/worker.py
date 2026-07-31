from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.writer_lock import (
    NavigationWriterLockError,
    configured_writer_lock_path,
    navigation_writer_coordination_status,
    navigation_writer_lock,
)


logger = logging.getLogger(__name__)
_WORKER_PROCESS_EPOCH = uuid4().hex


class UnavailableRuntimeDriver:
    def __init__(self, *, error_ref: str | None = None) -> None:
        self.error_ref = error_ref or f"annotation_error_{uuid4().hex}"

    def capabilities(self) -> dict[str, Any]:
        return {
            "available": False,
            "runtime_id": "navigation_odom_v1",
            "reason": {
                "code": "runtime_not_configured",
                "message": "The annotation runtime is not configured.",
                "error_ref": self.error_ref,
            },
        }

    def prepare(self, _run: dict[str, Any]) -> Any:
        raise RuntimeError("annotation runtime is unavailable")

    def track(self, _run: dict[str, Any], _inputs: dict[str, Any]) -> Any:
        raise RuntimeError("annotation runtime is unavailable")

    def validate_tracking_inputs(self, _request: Any) -> Any:
        raise RuntimeError("annotation runtime is unavailable")

    def cancel(self, _job_ref: str) -> None:
        return None


class AnnotationWorker:
    def __init__(
        self,
        store: AnnotationStore,
        runtime: Any | None = None,
        *,
        postprocessing_runtime: Any | None = None,
        postprocessing_publisher: Any | None = None,
        fix_runtime: Any | None = None,
        fix_publisher: Any | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        self.store = store
        if runtime is None:
            try:
                from vla_data_juicer_agents.annotation.runtime import (
                    build_default_runtime_driver,
                )

                runtime = build_default_runtime_driver()
            except Exception:
                logger.error(
                    "Annotation Runtime configuration failed; Runtime is unavailable"
                )
                runtime = UnavailableRuntimeDriver(
                    error_ref=f"annotation_error_{uuid4().hex}"
                )
        self.runtime = runtime
        self.postprocessing_runtime = postprocessing_runtime
        self.postprocessing_publisher = postprocessing_publisher
        self.fix_runtime = fix_runtime
        self.fix_publisher = fix_publisher
        if (
            self.postprocessing_runtime is None
            and getattr(runtime, "config", None) is not None
        ):
            try:
                from vla_data_juicer_agents.annotation.postprocessing_runtime import (
                    CompatibilityPublisher,
                    NavigationPostprocessingRuntime,
                )
                from vla_data_juicer_agents.navigation.config import (
                    NavigationSettings,
                )

                self.postprocessing_runtime = NavigationPostprocessingRuntime(
                    runtime.config,
                )
                self.postprocessing_publisher = (
                    self.postprocessing_publisher
                    or CompatibilityPublisher(
                        NavigationSettings().finish_data_root,
                    )
                )
            except Exception:
                logger.error(
                    "M2 postprocessing Runtime configuration failed; "
                    "postprocessing remains unavailable"
                )
                self.postprocessing_runtime = None
                self.postprocessing_publisher = None
        if getattr(runtime, "config", None) is not None:
            if self.fix_runtime is None:
                try:
                    from vla_data_juicer_agents.annotation.fix_runtime import (
                        NavigationFixRuntime,
                    )

                    self.fix_runtime = NavigationFixRuntime(runtime.config)
                except Exception:
                    logger.error(
                        "M2 Fix Runtime configuration failed; "
                        "Fix remains unavailable"
                    )
                    self.fix_runtime = None
            if self.fix_publisher is None:
                try:
                    from vla_data_juicer_agents.annotation.fix_runtime import (
                        FixCompatibilityPublisher,
                    )

                    self.fix_publisher = FixCompatibilityPublisher()
                except Exception:
                    logger.error(
                        "M2 Fix compatibility publisher configuration failed; "
                        "publication remains unavailable"
                    )
                    self.fix_publisher = None
        self.poll_interval = poll_interval
        self.worker_id = f"annotation-worker-{uuid4().hex}"
        self.owner_epoch = _WORKER_PROCESS_EPOCH
        self._stop = asyncio.Event()
        self._cached_available = False
        self._cached_capabilities: Any | None = None
        self._capabilities_checked_at = 0.0
        self._capabilities_ttl = 30.0
        self._recovery_checked_at = 0.0
        self._active_run: dict[str, Any] | None = None
        self._cancel_reasons: dict[str, str] = {}
        self._unhealthy_reason: dict[str, str] | None = None
        self._state_lock = threading.RLock()

    def capabilities(self) -> Any:
        now = time.monotonic()
        with self._state_lock:
            if self._unhealthy_reason is not None:
                return {
                    "available": False,
                    "runtime_id": "navigation_odom_v1",
                    "reason": dict(self._unhealthy_reason),
                }
            coordination_reason = self._writer_coordination_reason()
            if coordination_reason is not None:
                return {
                    "available": False,
                    "runtime_id": "navigation_odom_v1",
                    "reason": coordination_reason,
                }
            if (
                self._cached_capabilities is not None
                and now - self._capabilities_checked_at < self._capabilities_ttl
            ):
                return self._cached_capabilities
        try:
            value = self.runtime.capabilities()
        except Exception:
            logger.error(
                "Annotation Runtime capability check failed; Runtime is unavailable"
            )
            value = UnavailableRuntimeDriver(
                error_ref=f"annotation_error_{uuid4().hex}"
            ).capabilities()
        with self._state_lock:
            self._cached_capabilities = value
            self._cached_available = _capabilities_available(value)
            self._capabilities_checked_at = now
        return value

    def _writer_coordination_reason(self) -> dict[str, str] | None:
        config = getattr(self.runtime, "config", None)
        writer_lock_path = getattr(config, "writer_lock_path", None)
        if writer_lock_path is None:
            return None
        try:
            quarantined = (
                navigation_writer_coordination_status(Path(writer_lock_path))
                == "quarantined"
            )
        except NavigationWriterLockError:
            quarantined = True
        if not quarantined:
            return None
        return {
            "code": "runtime_coordination_unavailable",
            "message": (
                "Navigation writer coordination requires an operator safety check."
            ),
        }

    def invalidate_capabilities(self) -> None:
        with self._state_lock:
            self._cached_capabilities = None
            self._capabilities_checked_at = 0.0

    def owns_active_run(self, job_ref: str) -> bool:
        with self._state_lock:
            return (
                self._active_run is not None
                and self._active_run["job_ref"] == job_ref
            )

    def preflight_capacity(
        self,
        dataset_date: str,
        source_clips: list[str],
        *,
        active_reserved_bytes: int,
    ) -> dict[str, Any]:
        preflight = getattr(self.runtime, "preflight_capacity", None)
        if not callable(preflight):
            # Injectable fake runtimes used by local contract tests do not own
            # real datasets. Production runtime drivers must implement this.
            return {
                "estimated_input_bytes": 0,
                "required_bytes": active_reserved_bytes,
                "free_bytes": active_reserved_bytes,
                "available": True,
            }
        value = preflight(
            dataset_date,
            tuple(source_clips),
            active_reserved_bytes=active_reserved_bytes,
        )
        return _plain(value)

    def preflight_runtime_stage(
        self,
        stage: str,
        *,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fresh, fail-closed M2 payload proof before durable state is created."""

        if stage not in {"postprocessing", "fix"}:
            raise ValueError("unsupported annotation runtime stage")
        self.invalidate_capabilities()
        base = _plain(self.capabilities())
        if not _capabilities_available(base):
            return base
        runtime = (
            self.postprocessing_runtime
            if stage == "postprocessing"
            else self.fix_runtime
        )
        if runtime is None or (
            stage == "postprocessing"
            and self.postprocessing_publisher is None
        ):
            return {
                "available": False,
                "runtime_id": "navigation_odom_v1",
                "reason": {
                    "code": f"{stage}_runtime_not_configured",
                    "message": (
                        f"The M2 {stage} runtime has not completed deployment."
                    ),
                },
            }
        preflight = getattr(runtime, "preflight", None)
        if not callable(preflight):
            return {
                "available": False,
                "runtime_id": "navigation_odom_v1",
                "reason": {
                    "code": f"{stage}_preflight_not_configured",
                    "message": (
                        f"The M2 {stage} runtime cannot prove its frozen payload."
                    ),
                },
            }
        try:
            if stage == "postprocessing":
                if decision is None:
                    manifest_sha256 = preflight()
                else:
                    if not isinstance(decision, dict):
                        raise ValueError(
                            "postprocessing preflight requires a normalized decision"
                        )
                    manifest_sha256 = preflight(
                        localization_kind=decision.get("localization_kind"),
                        gridmap_decision=decision.get("gridmap_decision"),
                        trajectory_variant=decision.get("trajectory_variant"),
                    )
                publisher_preflight = getattr(
                    self.postprocessing_publisher,
                    "preflight",
                    None,
                )
                if not callable(publisher_preflight):
                    raise ValueError(
                        "postprocessing publisher has no safety preflight"
                    )
                publisher_preflight()
            else:
                manifest_sha256 = preflight()
            if (
                not isinstance(manifest_sha256, str)
                or len(manifest_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in manifest_sha256
                )
            ):
                raise ValueError("runtime preflight returned an invalid proof")
        except Exception as exc:
            error_ref = f"annotation_error_{uuid4().hex}"
            logger.error(
                "M2 Runtime preflight failed: stage=%s error_ref=%s type=%s",
                stage,
                error_ref,
                type(exc).__name__,
            )
            return {
                "available": False,
                "runtime_id": "navigation_odom_v1",
                "reason": {
                    "code": f"{stage}_runtime_preflight_failed",
                    "message": (
                        f"The M2 {stage} frozen payload failed preflight."
                    ),
                    "error_ref": error_ref,
                },
            }
        return {
            "available": True,
            "runtime_id": str(base.get("runtime_id", "navigation_odom_v1")),
            "runtime_manifest_sha256": manifest_sha256,
        }

    async def run_forever(self) -> None:
        try:
            await self._run_forever_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_ref = f"annotation_worker_error_{uuid4().hex}"
            with self._state_lock:
                self._unhealthy_reason = {
                    "code": "annotation_worker_unhealthy",
                    "message": (
                        "The annotation worker stopped unexpectedly; "
                        "writer jobs are disabled until the service restarts."
                    ),
                    "error_ref": error_ref,
                }
                self._cached_available = False
                self._cached_capabilities = None
            logger.error(
                "Annotation worker stopped unexpectedly: error_ref=%s type=%s",
                error_ref,
                type(exc).__name__,
            )

    async def _run_forever_loop(self) -> None:
        await self._recover_interrupted_runs()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._recovery_checked_at >= 5:
                await self._recover_interrupted_runs()
                self._recovery_checked_at = now
            if now - self._capabilities_checked_at >= self._capabilities_ttl:
                value = await asyncio.to_thread(self.capabilities)
                self._cached_available = _capabilities_available(value)
            if not self._cached_available:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(self.poll_interval, 5),
                    )
                except TimeoutError:
                    continue
                continue
            claimed = await asyncio.to_thread(
                self.store.claim_next_run,
                worker_id=self.worker_id,
                owner_epoch=self.owner_epoch,
                writer_lock_path=self._writer_lock_path(),
            )
            if claimed is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    continue
                continue
            await self._execute(claimed)

    async def run_once(self) -> bool:
        await self._recover_interrupted_runs()
        capabilities = await asyncio.to_thread(self.capabilities)
        if not _capabilities_available(capabilities):
            return False
        claimed = await asyncio.to_thread(
            self.store.claim_next_run,
            worker_id=self.worker_id,
            owner_epoch=self.owner_epoch,
            writer_lock_path=self._writer_lock_path(),
        )
        if claimed is None:
            return False
        await self._execute(claimed)
        return True

    async def _recover_interrupted_runs(self) -> int:
        recover = getattr(self.store, "recover_interrupted_runs", None)
        if not callable(recover):
            return 0
        writer_lock_path = self._writer_lock_path()
        recovered = await asyncio.to_thread(
            recover,
            current_owner_epoch=self.owner_epoch,
            writer_lock_path=writer_lock_path,
        )
        return recovered

    def _writer_lock_path(self) -> Path | None:
        config = getattr(self.runtime, "config", None)
        writer_lock_path = getattr(config, "writer_lock_path", None)
        if writer_lock_path is not None:
            return Path(writer_lock_path)
        try:
            return configured_writer_lock_path()
        except NavigationWriterLockError:
            # Test doubles may not own a real writer process. If recovery is
            # actually detected, Store will still require a configured path
            # before changing durable state.
            return None

    async def stop(self) -> None:
        self._stop.set()
        with self._state_lock:
            active = self._active_run
            if active is not None:
                self._cancel_reasons.setdefault(active["job_ref"], "shutdown")
        if active is not None:
            self._cancel_runtime(active["job_ref"])

    def request_cancel(self, job_ref: str, *, reason: str = "user") -> None:
        with self._state_lock:
            self._cancel_reasons[job_ref] = reason
        self._cancel_runtime(job_ref)

    def _cancel_runtime(self, job_ref: str) -> None:
        cancellable_runtimes = (
            self.runtime,
            self.postprocessing_runtime,
            self.fix_runtime,
        )
        for runtime in cancellable_runtimes:
            cancel = getattr(runtime, "cancel", None)
            if not callable(cancel):
                continue
            try:
                cancel(job_ref)
            except Exception:
                # The durable state transition already prevents new work.
                # Bound CancellationContext delivery is handled by the runtime;
                # never turn a successful cancellation request into an HTTP 500.
                logger.info(
                    "Annotation Runtime cancellation could not be delivered "
                    "through this process"
                )

    async def _execute(self, run: dict[str, Any]) -> None:
        with self._state_lock:
            self._active_run = run
        heartbeat = asyncio.create_task(
            self._heartbeat(run),
            name=f"annotation-lease:{run['run_ref']}",
        )
        try:
            await self._execute_run(run)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.error(
                    "Annotation runtime heartbeat ended unexpectedly: run_ref=%s",
                    run["run_ref"],
                )
            finally:
                with self._state_lock:
                    if self._active_run is run:
                        self._active_run = None
                    self._cancel_reasons.pop(run["job_ref"], None)

    async def _execute_run(self, run: dict[str, Any]) -> None:
        # True only while a Tracking subprocess has returned published output
        # that is not yet represented by a durable checkpoint, or while the
        # completed checkpoint set is being finalized.  Any ordinary failure
        # in this window has unknown durable side effects and must not become a
        # Web-retryable/cancellable failure.
        tracking_ledger_closure_pending = False
        postprocessing_ledger_closure_pending = False
        publication_ledger_closure_pending = False
        try:
            capacity = await asyncio.to_thread(
                self.preflight_capacity,
                run["dataset_date"],
                run["source_clips"],
                active_reserved_bytes=run["active_reserved_bytes"],
            )
            if not capacity["available"]:
                raise CapacityPreflightError(
                    "The annotation work root no longer has enough free space."
                )
            if run["kind"] == "prepare":
                from vla_data_juicer_agents.annotation.runtime import (
                    CalibrationSnapshotFile,
                    PreparationRequest,
                    RuntimeExecutionError,
                    RuntimeStepEvent,
                )

                def observe_prepare_step(event: RuntimeStepEvent) -> None:
                    try:
                        if event.status == "started":
                            self.store.start_runtime_step(
                                run_id=run["run_id"],
                                safe_step_code=event.safe_step_code,
                            )
                        else:
                            self.store.finish_runtime_step(
                                run_id=run["run_id"],
                                safe_step_code=event.safe_step_code,
                                status=event.status,
                                return_code=event.return_code,
                                diagnostic_kind=event.diagnostic_kind,
                            )
                    except Exception as exc:
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Runtime step evidence could not be committed.",
                        ) from exc

                request = PreparationRequest(
                    job_ref=run["job_ref"],
                    run_ref=run["run_ref"],
                    attempt=run["attempt"],
                    dataset_date=run["dataset_date"],
                    source_clips=tuple(run["source_clips"]),
                    calibration_snapshot_dir=Path(run["calibration_snapshot_dir"]),
                    calibration_snapshot_files=tuple(
                        CalibrationSnapshotFile(
                            relative_path=str(item["relative_path"]),
                            size=int(item["size"]),
                            sha256=str(item["sha256"]),
                        )
                        for item in run["calibration_snapshot_files"]
                    ),
                    calibration_snapshot_sha256=str(
                        run["calibration_snapshot_sha256"]
                    ),
                    active_reserved_bytes=run["active_reserved_bytes"],
                    step_observer=observe_prepare_step,
                )
                result = await _maybe_await_in_thread(self.runtime.prepare, request)
                prepared = _plain(result)
                segments = []
                for raw_segment in prepared["segments"]:
                    segment = _plain(raw_segment)
                    segment["segment_root"] = str(segment["segment_root"])
                    segment["first_frame_path"] = str(segment["first_frame_path"])
                    segments.append(segment)
                for field in (
                    "runtime_manifest_sha256",
                    "input_tree_sha256",
                    "calibration_snapshot_sha256",
                    "prepared_artifact_tree_sha256",
                ):
                    value = prepared.get(field)
                    if (
                        not isinstance(value, str)
                        or len(value) != 64
                        or any(character not in "0123456789abcdef" for character in value)
                    ):
                        raise RuntimeError(
                            f"runtime preparation attestation lacks {field}"
                        )
                if (
                    prepared["calibration_snapshot_sha256"]
                    != run["calibration_snapshot_sha256"]
                ):
                    raise RuntimeError(
                        "runtime preparation calibration attestation mismatch"
                    )
                manifest = {
                    "runtime_manifest_sha256": prepared["runtime_manifest_sha256"],
                    "input_tree_sha256": prepared["input_tree_sha256"],
                    "calibration_snapshot_sha256": prepared[
                        "calibration_snapshot_sha256"
                    ],
                    "prepared_artifact_tree_sha256": prepared[
                        "prepared_artifact_tree_sha256"
                    ],
                    "command_steps": prepared.get("command_steps", []),
                    "segment_count": len(segments),
                }
                await asyncio.to_thread(
                    self.store.complete_prepare,
                    run_id=run["run_id"],
                    staging_root=str(prepared["staging_root"]),
                    segments=segments,
                    manifest=manifest,
                )
            elif run["kind"] == "tracking":
                from vla_data_juicer_agents.annotation.legacy_yaml import LegacyYamlAdapter
                from vla_data_juicer_agents.annotation.runtime import (
                    CheckpointVerificationRequest,
                    TrackingInputValidationRequest,
                    TrackingRequest,
                    TrackingTarget,
                    tracking_target_sort_key,
                )

                inputs = await asyncio.to_thread(
                    self.store.tracking_inputs,
                    run["job_id"],
                )
                yaml_adapter = LegacyYamlAdapter()
                await asyncio.to_thread(
                    self.store.start_runtime_step,
                    run_id=run["run_id"],
                    safe_step_code="initial_annotation",
                )
                runtime_targets: list[
                    tuple[dict[str, Any], dict[str, Any], TrackingTarget]
                ] = []
                rendered_segments: list[
                    tuple[dict[str, Any], list[dict[str, Any]], tuple[Path, ...]]
                ] = []
                for segment in inputs["segments"]:
                    target_payloads = [
                        {
                            "target_ref": target["target_ref"],
                            "bbox": target["bbox"],
                            "point": target["point"],
                            "upper_color": target["colors"]["upper"],
                            "lower_color": target["colors"]["lower"],
                            "shoes_color": target["colors"]["shoes"],
                        }
                        for target in segment["targets"]
                    ]
                    segment_root = Path(segment["segment_root"])
                    rendered = yaml_adapter.render(
                        segment_root,
                        target_payloads,
                    )
                    yaml_paths = tuple(
                        segment_root / item.filename
                        for item in rendered
                    )
                    rendered_segments.append(
                        (segment, target_payloads, yaml_paths),
                    )
                    runtime_targets.extend(
                        (
                            segment,
                            target,
                            TrackingTarget(
                                segment_root=segment_root,
                                yaml_path=(
                                    segment_root / rendered_target.filename
                                ),
                                identity=Path(
                                    rendered_target.filename,
                                ).stem,
                                expected_yaml_sha256=(
                                    rendered_target.sha256
                                ),
                            ),
                        )
                        for target, rendered_target in zip(
                            segment["targets"],
                            rendered,
                            strict=True,
                        )
                    )
                runtime_targets.sort(
                    key=lambda item: tracking_target_sort_key(item[2])
                )
                attestation_targets = tuple(
                    item[2] for item in runtime_targets
                )
                validation = _plain(
                    await _maybe_await_in_thread(
                        self.runtime.validate_tracking_inputs,
                        TrackingInputValidationRequest(
                            job_ref=inputs["job_ref"],
                            staging_root=Path(inputs["staging_root"]),
                            targets=attestation_targets,
                            expected_runtime_manifest_sha256=inputs[
                                "expected_runtime_manifest_sha256"
                            ],
                            expected_prepared_artifact_tree_sha256=inputs[
                                "expected_prepared_artifact_tree_sha256"
                            ],
                        ),
                    ),
                )
                if (
                    validation.get("runtime_manifest_sha256")
                    != inputs["expected_runtime_manifest_sha256"]
                    or validation.get("prepared_artifact_tree_sha256")
                    != inputs["expected_prepared_artifact_tree_sha256"]
                ):
                    raise RuntimeError(
                        "runtime tracking-input attestation mismatch",
                    )
                for segment, target_payloads, expected_paths in rendered_segments:
                    yaml_paths = yaml_adapter.write(
                        Path(segment["segment_root"]),
                        target_payloads,
                    )
                    if yaml_paths != expected_paths:
                        raise RuntimeError(
                            "legacy YAML publication identity mismatch",
                        )
                await asyncio.to_thread(
                    self.store.finish_runtime_step,
                    run_id=run["run_id"],
                    safe_step_code="initial_annotation",
                    status="succeeded",
                    return_code=0,
                )
                safe_checkpoints: list[dict[str, Any]] = []
                # This semantic step is committed only after every selected
                # revision has been rendered successfully by the legacy
                # adapter. Attestation later reads it from this manifest; it
                # never invents the step at projection time.
                command_steps: list[str] = ["initial_annotation"]
                await asyncio.to_thread(
                    self.store.start_runtime_step,
                    run_id=run["run_id"],
                    safe_step_code="tracking",
                )
                for segment, target, runtime_target in runtime_targets:
                    committed = target.get("checkpoint")
                    if committed is not None:
                        verified = await _maybe_await_in_thread(
                            self.runtime.verify_checkpoint,
                            CheckpointVerificationRequest(
                                job_ref=inputs["job_ref"],
                                staging_root=Path(inputs["staging_root"]),
                                segment_root=runtime_target.segment_root,
                                identity=committed["identity"],
                                artifact_sha256=committed["artifact_sha256"],
                            ),
                        )
                        if not verified:
                            raise CheckpointMismatchError(
                                "A committed Tracking checkpoint no longer matches its artifacts."
                            )
                        safe_checkpoints.append(
                            {
                                "segment_ref": segment["segment_ref"],
                                "target_ref": target["target_ref"],
                                "identity": committed["identity"],
                                "artifact_sha256": committed["artifact_sha256"],
                            }
                        )
                        if "tracking" not in command_steps:
                            command_steps.append("tracking")
                        continue
                    request = TrackingRequest(
                        job_ref=inputs["job_ref"],
                        run_ref=run["run_ref"],
                        attempt=run["attempt"],
                        staging_root=Path(inputs["staging_root"]),
                        targets=(runtime_target,),
                        attestation_targets=attestation_targets,
                        expected_runtime_manifest_sha256=inputs[
                            "expected_runtime_manifest_sha256"
                        ],
                        expected_prepared_artifact_tree_sha256=inputs[
                            "expected_prepared_artifact_tree_sha256"
                        ],
                        estimated_input_bytes=int(
                            capacity["estimated_input_bytes"]
                        ),
                        active_reserved_bytes=run["active_reserved_bytes"],
                    )
                    result = await _maybe_await_in_thread(self.runtime.track, request)
                    tracking_ledger_closure_pending = True
                    tracked_target = _plain(result)
                    if (
                        tracked_target.get("runtime_manifest_sha256")
                        != validation["runtime_manifest_sha256"]
                    ):
                        raise RuntimeError(
                            "Tracking Runtime manifest attestation mismatch",
                        )
                    checkpoints = tracked_target.get("checkpoints", [])
                    if len(checkpoints) != 1:
                        raise RuntimeError(
                            "runtime must return exactly one checkpoint per target"
                        )
                    checkpoint = _plain(checkpoints[0])
                    if checkpoint.get("identity") != runtime_target.identity:
                        raise RuntimeError("runtime checkpoint identity mismatch")
                    safe_checkpoint = await asyncio.to_thread(
                        self.store.record_tracking_checkpoint,
                        run_id=run["run_id"],
                        segment_ref=segment["segment_ref"],
                        target_ref=target["target_ref"],
                        revision_sha256=segment["revision_sha256"],
                        identity=checkpoint["identity"],
                        output_dir=str(checkpoint["output_dir"]),
                        points_path=str(checkpoint["points_path"]),
                        artifact_sha256=checkpoint["artifact_sha256"],
                    )
                    safe_checkpoints.append(safe_checkpoint)
                    for step in tracked_target.get("command_steps", []):
                        if step not in command_steps:
                            command_steps.append(step)
                    tracking_ledger_closure_pending = False
                # Every output has a committed checkpoint, but the semantic
                # step, terminal manifest, and Job state still need to commit
                # as one recoverable ledger closure.
                tracking_ledger_closure_pending = True
                await asyncio.to_thread(
                    self.store.finish_runtime_step,
                    run_id=run["run_id"],
                    safe_step_code="tracking",
                    status="succeeded",
                    return_code=0,
                )
                manifest = {
                    "runtime_manifest_sha256": validation[
                        "runtime_manifest_sha256"
                    ],
                    "prepared_artifact_tree_sha256": validation[
                        "prepared_artifact_tree_sha256"
                    ],
                    "command_steps": command_steps,
                    "checkpoints": safe_checkpoints,
                    "revision_set": [
                        {
                            "segment_ref": segment["segment_ref"],
                            "revision": segment["revision_number"],
                            "sha256": segment["revision_sha256"],
                        }
                        for segment in inputs["segments"]
                    ],
                }
                await asyncio.to_thread(
                    self.store.complete_tracking,
                    run_id=run["run_id"],
                    manifest=manifest,
                )
                tracking_ledger_closure_pending = False
            elif run["kind"] == "postprocessing":
                from vla_data_juicer_agents.annotation.postprocessing_runtime import (
                    PostprocessingRequest,
                    PostprocessingSegmentInput,
                    PublicationItem,
                )
                from vla_data_juicer_agents.annotation.runtime import (
                    RuntimeExecutionError,
                    RuntimeStepEvent,
                    _sha256_file,
                    _tree_sha256,
                )
                from vla_data_juicer_agents.annotation.trajectory_evidence import (
                    build_trajectory_revision_state,
                )

                if (
                    self.postprocessing_runtime is None
                    or self.postprocessing_publisher is None
                ):
                    raise RuntimeExecutionError(
                        "annotation_runtime_unavailable",
                        "The M2 postprocessing Runtime is unavailable.",
                    )
                inputs = await asyncio.to_thread(
                    self.store.postprocessing_inputs,
                    run["job_id"],
                )
                publisher_preflight = getattr(
                    self.postprocessing_publisher,
                    "preflight",
                    None,
                )
                if not callable(publisher_preflight):
                    raise RuntimeExecutionError(
                        "annotation_runtime_unavailable",
                        "The M2 publication Runtime is unavailable.",
                    )
                await _maybe_await_in_thread(publisher_preflight)

                def observe_postprocessing_step(event: RuntimeStepEvent) -> None:
                    try:
                        if event.status == "started":
                            self.store.start_runtime_step(
                                run_id=run["run_id"],
                                safe_step_code=event.safe_step_code,
                            )
                        else:
                            self.store.finish_runtime_step(
                                run_id=run["run_id"],
                                safe_step_code=event.safe_step_code,
                                status=event.status,
                                return_code=event.return_code,
                                diagnostic_kind=event.diagnostic_kind,
                            )
                    except Exception as exc:
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Runtime step evidence could not be committed.",
                        ) from exc

                segment_inputs: list[PostprocessingSegmentInput] = []
                private_by_ref: dict[str, dict[str, Any]] = {}
                for segment in inputs["segments"]:
                    segment_root = Path(segment["private_segment_root"])
                    tree_sha256 = await asyncio.to_thread(
                        _tree_sha256,
                        segment_root,
                        unsafe_code="tracked_staging_changed",
                    )
                    segment_inputs.append(
                        PostprocessingSegmentInput(
                            segment_ref=segment["segment_ref"],
                            source_clip=segment["source_clip"],
                            private_segment_key=segment[
                                "private_segment_key"
                            ],
                            tracked_segment_root=segment_root,
                            expected_tree_sha256=tree_sha256,
                            tracking_identities=tuple(
                                segment["tracking_identities"]
                            ),
                        )
                    )
                    private_by_ref[segment["segment_ref"]] = segment
                spec = inputs["spec"]
                request = PostprocessingRequest(
                    job_ref=inputs["job_ref"],
                    run_ref=run["run_ref"],
                    attempt=run["attempt"],
                    dataset_date=inputs["dataset_date"],
                    tracked_staging_root=Path(
                        inputs["tracked_staging_root"],
                    ),
                    segments=tuple(segment_inputs),
                    gridmap_decision=spec["gridmap_decision"],
                    localization_kind=spec["localization_kind"],
                    trajectory_variant=spec["trajectory_variant"],
                    expected_runtime_manifest_sha256=inputs[
                        "runtime_manifest_sha256"
                    ],
                    expected_prepared_artifact_tree_sha256=inputs[
                        "prepared_artifact_tree_sha256"
                    ],
                    step_observer=observe_postprocessing_step,
                )
                runtime_result = _plain(
                    await _maybe_await_in_thread(
                        self.postprocessing_runtime.run,
                        request,
                    )
                )
                if runtime_result.get("runtime_manifest_sha256") != inputs[
                    "runtime_manifest_sha256"
                ]:
                    raise RuntimeError(
                        "postprocessing Runtime manifest attestation mismatch"
                    )
                candidates = [
                    _plain(item)
                    for item in runtime_result.get("trajectories", ())
                ]
                expected_refs = [
                    segment["segment_ref"] for segment in inputs["segments"]
                ]
                if [item.get("segment_ref") for item in candidates] != expected_refs:
                    raise RuntimeError(
                        "postprocessing Runtime result scope mismatch"
                    )
                publication_items: list[PublicationItem] = []
                for source_clip in dict.fromkeys(
                    segment["source_clip"] for segment in inputs["segments"]
                ):
                    candidate_root = (
                        Path(runtime_result["final_candidate_root"])
                        / source_clip
                    )
                    publication_items.append(
                        PublicationItem(
                            source_clip=source_clip,
                            candidate_root=candidate_root,
                            expected_tree_sha256=await asyncio.to_thread(
                                _tree_sha256,
                                candidate_root,
                                unsafe_code="unsafe_runtime_output",
                            ),
                        )
                    )
                attempt_root = Path(runtime_result["attempt_root"])
                journal_parent = attempt_root.parent / "publication-journals"
                journal_parent.mkdir(mode=0o700, exist_ok=True)
                journal_root = journal_parent / run["run_ref"]
                journal_root.mkdir(mode=0o700)

                def publish_candidates() -> Any:
                    with navigation_writer_lock(
                        lock_path=self._writer_lock_path(),
                    ):
                        if not self.store.begin_postprocessing_publication(
                            run_id=run["run_id"],
                        ):
                            raise RuntimeExecutionError(
                                "runtime_cancelled",
                                "The annotation runtime was cancelled.",
                            )
                        publication_result = (
                            self.postprocessing_publisher.publish(
                                job_ref=inputs["job_ref"],
                                run_ref=run["run_ref"],
                                dataset_date=inputs["dataset_date"],
                                items=tuple(publication_items),
                                journal_root=journal_root,
                            )
                        )
                        self.store.finish_runtime_step(
                            run_id=run["run_id"],
                            safe_step_code="compatibility_publish",
                            status="succeeded",
                            return_code=0,
                        )
                        return publication_result

                publication = _plain(
                    await asyncio.to_thread(publish_candidates),
                )
                postprocessing_ledger_closure_pending = True
                trajectory_results: list[dict[str, Any]] = []
                revision_set: list[dict[str, str]] = []
                for candidate in candidates:
                    segment_ref = str(candidate["segment_ref"])
                    private = private_by_ref[segment_ref]
                    candidate_root = Path(candidate["candidate_segment_root"])
                    artifact_sha256 = await asyncio.to_thread(
                        _tree_sha256,
                        candidate_root,
                        unsafe_code="unsafe_runtime_output",
                    )
                    compatibility_root = (
                        Path(self.postprocessing_publisher.finish_data_root)
                        / inputs["dataset_date"]
                        / private["source_clip"]
                        / private["private_segment_key"]
                    )
                    compatibility_sha256 = await asyncio.to_thread(
                        _tree_sha256,
                        compatibility_root,
                        unsafe_code="publication_target_unsafe",
                    )
                    if compatibility_sha256 != artifact_sha256:
                        raise RuntimeError(
                            "published postprocessing segment differs from candidate"
                        )
                    state = await asyncio.to_thread(
                        build_trajectory_revision_state,
                        candidate_root,
                        target_bindings=private["target_bindings"],
                    )
                    state_sha256 = hashlib.sha256(
                        json.dumps(
                            state,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    trajectory_results.append(
                        {
                            "segment_ref": segment_ref,
                            "state": state,
                            "content_sha256": state_sha256,
                            "private_artifact_path": str(candidate_root),
                            "private_compatibility_path": str(
                                compatibility_root,
                            ),
                            "artifact_sha256": artifact_sha256,
                            "artifact_manifest_ref": None,
                        }
                    )
                    revision_set.append(
                        {
                            "segment_ref": segment_ref,
                            "annotation_revision_sha256": private[
                                "annotation_revision_sha256"
                            ],
                            "trajectory_sha256": str(
                                candidate["trajectory_sha256"]
                            ),
                            "artifact_sha256": artifact_sha256,
                        }
                    )
                command_steps = [
                    *runtime_result.get("command_steps", ()),
                    "compatibility_publish",
                ]
                manifest = {
                    "runtime_manifest_sha256": runtime_result[
                        "runtime_manifest_sha256"
                    ],
                    "postprocessing_spec_sha256": spec["content_sha256"],
                    "plan_sha256": spec["plan_sha256"],
                    "observations_sha256": spec["observations_sha256"],
                    "input_tree_sha256": runtime_result["input_tree_sha256"],
                    "candidate_tree_sha256": runtime_result[
                        "candidate_tree_sha256"
                    ],
                    "command_steps": command_steps,
                    "revision_set": revision_set,
                    "publication": {
                        "source_clips": list(
                            publication.get(
                                "committed_source_clips",
                                (),
                            )
                        ),
                        "journal_sha256": _sha256_file(
                            Path(publication["journal_path"])
                        ),
                    },
                }
                await asyncio.to_thread(
                    self.store.complete_postprocessing_run,
                    run_id=run["run_id"],
                    trajectories=trajectory_results,
                    manifest=manifest,
                )
                postprocessing_ledger_closure_pending = False
            elif run["kind"] == "fix":
                from vla_data_juicer_agents.annotation.fix_runtime import (
                    FixRequest,
                )
                from vla_data_juicer_agents.annotation.runtime import (
                    CalibrationSnapshotFile,
                    RuntimeExecutionError,
                    RuntimeStepEvent,
                    _sha256_file,
                    _tree_sha256,
                )

                if self.fix_runtime is None:
                    raise RuntimeExecutionError(
                        "annotation_runtime_unavailable",
                        "The M2 Fix Runtime is unavailable.",
                    )
                inputs = await asyncio.to_thread(
                    self.store.fix_run_inputs,
                    run["run_id"],
                )

                def observe_fix_step(event: RuntimeStepEvent) -> None:
                    try:
                        if event.status == "started":
                            self.store.start_runtime_step(
                                run_id=run["run_id"],
                                safe_step_code=event.safe_step_code,
                            )
                        else:
                            self.store.finish_runtime_step(
                                run_id=run["run_id"],
                                safe_step_code=event.safe_step_code,
                                status=event.status,
                                return_code=event.return_code,
                                diagnostic_kind=event.diagnostic_kind,
                            )
                    except Exception as exc:
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Runtime step evidence could not be committed.",
                        ) from exc

                request = FixRequest(
                    review_ref=inputs["review_ref"],
                    run_ref=run["run_ref"],
                    attempt=run["attempt"],
                    base_segment_root=Path(
                        inputs["base_artifact_path"],
                    ),
                    expected_base_tree_sha256=inputs[
                        "base_artifact_sha256"
                    ],
                    calibration_snapshot_ref=inputs[
                        "calibration_snapshot_ref"
                    ],
                    calibration_snapshot_dir=Path(
                        inputs["calibration_snapshot_dir"],
                    ),
                    calibration_snapshot_files=tuple(
                        CalibrationSnapshotFile(
                            relative_path=str(item["relative_path"]),
                            size=int(item["size"]),
                            sha256=str(item["sha256"]),
                        )
                        for item in inputs["calibration_snapshot_files"]
                    ),
                    calibration_snapshot_sha256=inputs[
                        "calibration_snapshot_sha256"
                    ],
                    target_bindings={
                        str(key): str(value)
                        for key, value in inputs["target_bindings"].items()
                    },
                    commands=tuple(
                        dict(command) for command in inputs["commands"]
                    ),
                    expected_runtime_manifest_sha256=inputs[
                        "runtime_manifest_sha256"
                    ],
                    step_observer=observe_fix_step,
                )
                runtime_result = _plain(
                    await _maybe_await_in_thread(
                        self.fix_runtime.run,
                        request,
                    )
                )
                if runtime_result.get("runtime_manifest_sha256") != inputs[
                    "runtime_manifest_sha256"
                ]:
                    raise RuntimeError(
                        "Fix Runtime manifest attestation mismatch"
                    )
                candidate_root = Path(
                    runtime_result["candidate_segment_root"],
                )
                candidate_tree_sha256 = await asyncio.to_thread(
                    _tree_sha256,
                    candidate_root,
                    unsafe_code="unsafe_runtime_output",
                )
                output_path = Path(runtime_result["fix_trajectory_path"])
                fix_trajectory_sha256 = await asyncio.to_thread(
                    _sha256_file,
                    output_path,
                )
                if (
                    fix_trajectory_sha256
                    != runtime_result["fix_trajectory_sha256"]
                    or output_path.parent != candidate_root
                ):
                    raise RuntimeError("Fix Runtime output attestation mismatch")
                command_steps = list(
                    runtime_result.get("command_steps", ()),
                )
                manifest = {
                    "runtime_manifest_sha256": runtime_result[
                        "runtime_manifest_sha256"
                    ],
                    "trajectory_revision_ref": inputs[
                        "trajectory_revision_ref"
                    ],
                    "base_tree_sha256": runtime_result[
                        "base_tree_sha256"
                    ],
                    "calibration_snapshot_sha256": runtime_result[
                        "calibration_snapshot_sha256"
                    ],
                    "draft_sha256": inputs["draft_sha256"],
                    "command_log_sha256": runtime_result[
                        "command_log_sha256"
                    ],
                    "adapter_sha256": runtime_result["adapter_sha256"],
                    "candidate_tree_sha256": candidate_tree_sha256,
                    "fix_trajectory_sha256": fix_trajectory_sha256,
                    "command_steps": command_steps,
                    "revision_set": [
                        {
                            "review_ref": inputs["review_ref"],
                            "segment_ref": inputs["segment_ref"],
                            "planned_revision_ref": inputs[
                                "planned_revision_ref"
                            ],
                            "source_draft_revision": inputs[
                                "source_draft_revision"
                            ],
                        }
                    ],
                }
                await asyncio.to_thread(
                    self.store.complete_fix_run,
                    run_id=run["run_id"],
                    candidate_segment_root=str(candidate_root),
                    candidate_tree_sha256=candidate_tree_sha256,
                    fix_trajectory_sha256=fix_trajectory_sha256,
                    manifest=manifest,
                )
            elif run["kind"] == "compatibility_publish":
                from vla_data_juicer_agents.annotation.models import (
                    CompatibilityPublicationResult,
                )
                from vla_data_juicer_agents.annotation.runtime import (
                    RuntimeExecutionError,
                    _ensure_private_directory_chain,
                    _sha256_file,
                    _tree_sha256,
                )

                if self.fix_publisher is None:
                    raise RuntimeExecutionError(
                        "annotation_runtime_unavailable",
                        "The Fix compatibility publisher is unavailable.",
                    )
                publish_bound = getattr(
                    self.fix_publisher,
                    "publish_bound_revision",
                    None,
                )
                if not callable(publish_bound):
                    raise RuntimeExecutionError(
                        "annotation_runtime_unavailable",
                        "The Fix compatibility publisher is not store-bound.",
                    )
                inputs = await asyncio.to_thread(
                    self.store.compatibility_publication_inputs,
                    run["run_id"],
                )
                writer_lock_path = self._writer_lock_path()
                if writer_lock_path is None:
                    raise RuntimeExecutionError(
                        "annotation_runtime_unavailable",
                        "The Fix compatibility writer lock is unavailable.",
                    )
                candidate_root = Path(
                    inputs["candidate_segment_root"],
                )
                journal_root = _ensure_private_directory_chain(
                    candidate_root.parent,
                    (
                        "publication-journals",
                        inputs["publication_ref"],
                    ),
                )
                await asyncio.to_thread(
                    self.store.start_runtime_step,
                    run_id=run["run_id"],
                    safe_step_code="fix_compatibility_publish",
                )
                raw_publication = await _maybe_await_in_thread(
                    publish_bound,
                    review_ref=inputs["review_ref"],
                    revision_ref=inputs["fix_revision_ref"],
                    candidate_segment_root=candidate_root,
                    expected_candidate_tree_sha256=inputs[
                        "candidate_tree_sha256"
                    ],
                    expected_fix_sha256=inputs["fix_content_sha256"],
                    target_segment_root=Path(
                        inputs["target_segment_root"],
                    ),
                    journal_root=journal_root,
                    writer_lock_path=writer_lock_path,
                )
                publication = CompatibilityPublicationResult.model_validate(
                    raw_publication,
                )
                published_path = Path(publication.private_artifact_path)
                if (
                    published_path.parent
                    != Path(inputs["target_segment_root"])
                    or await asyncio.to_thread(
                        _sha256_file,
                        published_path,
                    )
                    != publication.content_sha256
                    or publication.content_sha256
                    != inputs["fix_content_sha256"]
                ):
                    raise RuntimeError(
                        "Fix compatibility publication attestation mismatch"
                    )
                await asyncio.to_thread(
                    self.store.finish_runtime_step,
                    run_id=run["run_id"],
                    safe_step_code="fix_compatibility_publish",
                    status="succeeded",
                    return_code=0,
                )
                manifest = {
                    "fix_revision_ref": inputs["fix_revision_ref"],
                    "candidate_tree_sha256": inputs[
                        "candidate_tree_sha256"
                    ],
                    "content_sha256": publication.content_sha256,
                    "journal_tree_sha256": await asyncio.to_thread(
                        _tree_sha256,
                        journal_root,
                        unsafe_code="publication_journal_unsafe",
                    ),
                    "command_steps": ["fix_compatibility_publish"],
                }
                publication_ledger_closure_pending = True
                await asyncio.to_thread(
                    self.store.complete_compatibility_publication,
                    run_id=run["run_id"],
                    content_sha256=publication.content_sha256,
                    private_artifact_path=publication.private_artifact_path,
                    manifest=manifest,
                )
                publication_ledger_closure_pending = False
            else:
                raise RuntimeError("unsupported annotation runtime run kind")
        except Exception as exc:
            if (
                tracking_ledger_closure_pending
                and not _is_runtime_cancellation(exc)
            ):
                from vla_data_juicer_agents.annotation.runtime import (
                    RuntimeExecutionError,
                )

                recovery_error = RuntimeExecutionError(
                    "recovery_required",
                    "Tracking output ledger closure requires operator recovery.",
                    private_detail=(
                        "A Tracking output was published before its durable "
                        f"ledger closure failed ({type(exc).__name__})."
                    ),
                )
                recovery_error.__cause__ = exc
                exc = recovery_error
            if (
                publication_ledger_closure_pending
                and not _is_runtime_cancellation(exc)
            ):
                from vla_data_juicer_agents.annotation.runtime import (
                    RuntimeExecutionError,
                )

                recovery_error = RuntimeExecutionError(
                    "recovery_required",
                    "Published Fix output requires durable ledger recovery.",
                    private_detail=(
                        "A Fix compatibility file was published before its "
                        f"durable ledger closure failed ({type(exc).__name__})."
                    ),
                )
                recovery_error.__cause__ = exc
                exc = recovery_error
            if (
                postprocessing_ledger_closure_pending
                and not _is_runtime_cancellation(exc)
            ):
                from vla_data_juicer_agents.annotation.runtime import (
                    RuntimeExecutionError,
                )

                recovery_error = RuntimeExecutionError(
                    "recovery_required",
                    "Published postprocessing output requires operator recovery.",
                    private_detail=(
                        "Compatibility output was published before its durable "
                        f"ledger closure failed ({type(exc).__name__})."
                    ),
                )
                recovery_error.__cause__ = exc
                exc = recovery_error
            try:
                return_code = getattr(exc, "return_code", None)
                diagnostic_kind = getattr(exc, "diagnostic_kind", "error")
                await asyncio.to_thread(
                    self.store.fail_active_runtime_step,
                    run_id=run["run_id"],
                    return_code=(
                        return_code
                        if isinstance(return_code, int)
                        and not isinstance(return_code, bool)
                        else None
                    ),
                    diagnostic_kind=(
                        diagnostic_kind
                        if diagnostic_kind
                        in {"nonzero_exit", "timeout", "cancelled", "error"}
                        else "error"
                    ),
                )
            except Exception as ledger_exc:
                from vla_data_juicer_agents.annotation.runtime import (
                    RuntimeExecutionError,
                )

                exc = RuntimeExecutionError(
                    "recovery_required",
                    "Runtime step evidence requires operator recovery.",
                )
                exc.__cause__ = ledger_exc
            cancel_requested = await asyncio.to_thread(
                self.store.cancellation_requested_for_run,
                run["run_id"],
            )
            if cancel_requested and not _requires_operator_recovery(exc):
                await asyncio.to_thread(
                    self.store.complete_cancelled_run,
                    run_id=run["run_id"],
                )
                return
            code, public_message, retryable = _safe_runtime_failure(exc)
            logger.error(
                "Annotation Runtime run failed: run_ref=%s code=%s",
                run["run_ref"],
                code,
            )
            if code == "annotation_runtime_unavailable":
                self.invalidate_capabilities()
            if code == "cancelled":
                cancel_requested = await asyncio.to_thread(
                    self.store.cancellation_requested_for_run,
                    run["run_id"],
                )
                with self._state_lock:
                    reason = self._cancel_reasons.pop(run["job_ref"], None)
                if cancel_requested:
                    await asyncio.to_thread(
                        self.store.complete_cancelled_run,
                        run_id=run["run_id"],
                    )
                else:
                    interrupted_code = (
                        "runtime_interrupted"
                        if reason == "shutdown"
                        else "recovery_required"
                    )
                    await asyncio.to_thread(
                        self.store.fail_run,
                        run_id=run["run_id"],
                        code=interrupted_code,
                        message=(
                            "The runtime was safely interrupted during shutdown."
                            if reason == "shutdown"
                            else "Runtime recovery requires an operator safety check."
                        ),
                        retryable=reason == "shutdown",
                        private_detail=(
                            f"Runtime cancellation reason: {reason or 'unknown'}"
                        ),
                    )
                return
            await asyncio.to_thread(
                self.store.fail_run,
                run_id=run["run_id"],
                code=code,
                message=public_message,
                retryable=retryable,
                private_detail=(
                    getattr(exc, "private_detail", None)
                    or f"{type(exc).__name__}: {exc}"
                ),
            )

    async def _heartbeat(self, run: dict[str, Any]) -> None:
        try:
            next_renewal = time.monotonic() + 30
            while True:
                await asyncio.sleep(1)
                control = await asyncio.to_thread(
                    self.store.runtime_control_state,
                    run_id=run["run_id"],
                    worker_id=self.worker_id,
                )
                if control == "cancel_requested":
                    self.request_cancel(run["job_ref"], reason="user")
                    return
                if control == "finished":
                    return
                if control != "continue":
                    logger.error(
                        "Annotation runtime lease was lost: %s",
                        run["run_ref"],
                    )
                    self.request_cancel(run["job_ref"], reason="lease_lost")
                    return
                if time.monotonic() < next_renewal:
                    continue
                renewed = await asyncio.to_thread(
                    self.store.renew_run_lease,
                    run_id=run["run_id"],
                    worker_id=self.worker_id,
                )
                if not renewed:
                    # Completion deletes the lease in the same transaction
                    # that commits the run. Re-read durable state so that a
                    # successful completion racing this renewal is not
                    # misclassified as lease loss and carried into the next
                    # phase as a pending cancellation.
                    after_renewal = await asyncio.to_thread(
                        self.store.runtime_control_state,
                        run_id=run["run_id"],
                        worker_id=self.worker_id,
                    )
                    if after_renewal == "finished":
                        return
                    if after_renewal == "cancel_requested":
                        self.request_cancel(run["job_ref"], reason="user")
                        return
                    if after_renewal == "continue":
                        next_renewal = time.monotonic()
                        continue
                    logger.error(
                        "Annotation runtime lease was lost: %s",
                        run["run_ref"],
                    )
                    self.request_cancel(
                        run["job_ref"],
                        reason=(
                            "recovery_required"
                            if after_renewal == "recovery_required"
                            else "lease_lost"
                        ),
                    )
                    return
                next_renewal = time.monotonic() + 30
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Annotation runtime heartbeat failed: run_ref=%s",
                run["run_ref"],
            )
            self.request_cancel(run["job_ref"], reason="heartbeat_failed")


async def _maybe_await_in_thread(
    callable_: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    if inspect.iscoroutinefunction(callable_):
        return await callable_(*args, **kwargs)
    result = await asyncio.to_thread(callable_, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _plain(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError("runtime result must be a mapping or dataclass")


class CheckpointMismatchError(RuntimeError):
    pass


class CapacityPreflightError(RuntimeError):
    pass


def _is_runtime_cancellation(exc: BaseException) -> bool:
    return (
        type(exc).__name__ == "RuntimeExecutionError"
        and getattr(exc, "code", "") == "runtime_cancelled"
    )


def _requires_operator_recovery(exc: BaseException) -> bool:
    return (
        type(exc).__name__ == "RuntimeExecutionError"
        and getattr(exc, "code", "") == "recovery_required"
    )


def _safe_runtime_failure(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, CheckpointMismatchError):
        return (
            "recovery_required",
            "A committed runtime checkpoint changed; processing stopped.",
            False,
        )
    if isinstance(exc, CapacityPreflightError):
        return (
            "insufficient_annotation_capacity",
            "The annotation work root has insufficient free space.",
            True,
        )
    class_name = type(exc).__name__
    if class_name == "RuntimeUnavailableError":
        return (
            "annotation_runtime_unavailable",
            "The annotation runtime is unavailable.",
            True,
        )
    if class_name == "RuntimeExecutionError":
        error_code = getattr(exc, "code", "")
        if error_code == "annotation_runtime_unavailable":
            return (
                "annotation_runtime_unavailable",
                "The annotation runtime is unavailable.",
                True,
            )
        if error_code in {
            "runtime_input_changed",
            "runtime_manifest_changed",
            "prepared_staging_changed",
            "tracking_yaml_changed",
            "unsafe_runtime_input",
            "unsupported_runtime_variant",
            "missing_runtime_input",
        }:
            return (
                error_code,
                "The synchronized Runtime input failed safety validation.",
                False,
            )
        if error_code == "calibration_snapshot_mismatch":
            return (
                "calibration_snapshot_mismatch",
                "The frozen calibration snapshot failed integrity verification.",
                False,
            )
        if error_code in {
            "unsafe_existing_tracking_output",
            "unsafe_tracking_output",
            "tracking_output_conflict",
            "recovery_required",
            "runtime_cancelled",
        }:
            return (
                "recovery_required" if error_code != "runtime_cancelled" else "cancelled",
                (
                    "Runtime recovery requires an operator safety check."
                    if error_code != "runtime_cancelled"
                    else "The annotation runtime was cancelled."
                ),
                False,
            )
        return (
            "annotation_runtime_failed",
            "The annotation runtime failed. Use the error reference for investigation.",
            True,
        )
    return (
        "annotation_runtime_failed",
        "The annotation runtime failed. Use the error reference for investigation.",
        False,
    )


def _capabilities_available(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("available"))
    return bool(getattr(value, "available", False))
