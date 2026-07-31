from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import struct
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.annotation.catalog import CalibrationCatalog
from vla_data_juicer_agents.annotation.models import (
    ApplyFixCommandRequest,
    AnnotationConflictError,
    AnnotationValidationError,
    ApproveReviewRequest,
    CreateFixRevisionRequest,
    CreateFixSessionRequest,
    CreateAnnotationJobRequest,
    DiscardReviewRequest,
    DraftRequest,
    ExpectedJobRevisionRequest,
    FixRuntimeState,
    PostprocessingSpecInput,
    public_annotation_error_ref,
    RetryPublicationRequest,
    ReturnReviewRequest,
    SegmentRevisionRequest,
    SkipRequest,
    SubmitRequest,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.config import NavigationSettings


def _public_capability_reason(reason: Any) -> dict[str, str]:
    if hasattr(reason, "__dict__"):
        reason = dict(vars(reason))
    if not isinstance(reason, dict):
        reason = {}
    internal_code = str(reason.get("code", ""))
    if internal_code == "annotation_worker_unhealthy":
        code = "processing_worker_unavailable"
        message = (
            "The processing service stopped unexpectedly and must be "
            "restarted by an operator."
        )
    elif internal_code in {
        "runtime_not_configured",
        "runtime_timeout_not_configured",
        "writer_lock_not_configured",
        "postprocessing_runtime_not_configured",
        "fix_runtime_not_configured",
    }:
        code = "processing_runtime_not_configured"
        message = (
            "The processing runtime has not completed its deployment "
            "configuration."
        )
    else:
        code = "processing_runtime_preflight_failed"
        message = (
            "The processing runtime has not passed its deployment preflight."
        )
    projected = {"code": code, "message": message}
    error_ref = public_annotation_error_ref(reason.get("error_ref"))
    if error_ref is not None:
        projected["error_ref"] = error_ref
    return projected


class AnnotationApplicationService:
    def __init__(
        self,
        *,
        store: AnnotationStore,
        worker: Any,
        catalog: CalibrationCatalog | Any | None = None,
        work_root: Path | str | None = None,
        clip_data_root: Path | str | None = None,
        fix_runtime: Any | None = None,
    ) -> None:
        self.store = store
        self.worker = worker
        self.catalog = catalog or CalibrationCatalog.default()
        self.work_root = Path(
            work_root
            if work_root is not None
            else os.environ.get(
                "VLA_ANNOTATION_WORK_ROOT",
                str(store.db_path.parent / "annotation_work"),
            )
        )
        self.clip_data_root = Path(
            clip_data_root
            if clip_data_root is not None
            else NavigationSettings().clip_data_root
        )
        self.fix_runtime = fix_runtime

    def capabilities(self) -> dict[str, Any]:
        value = self.worker.capabilities()
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if hasattr(value, "__dict__") and not isinstance(value, dict):
            value = vars(value)
        available = bool(value.get("available", False))
        reason = value.get("reason")
        return {
            "available": available,
            "runtime_id": str(value.get("runtime_id", "navigation_odom_v1")),
            "reason": None if available else _public_capability_reason(reason),
        }

    def _require_runtime_stage(
        self,
        stage: str,
        *,
        decision: dict[str, Any] | None = None,
    ) -> None:
        checker = getattr(self.worker, "preflight_runtime_stage", None)
        try:
            value = (
                checker(stage, decision=decision)
                if callable(checker)
                else {
                    "available": False,
                    "runtime_id": "navigation_odom_v1",
                    "reason": {
                        "code": f"{stage}_preflight_not_configured",
                    },
                }
            )
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            if hasattr(value, "__dict__") and not isinstance(value, dict):
                value = vars(value)
            if not isinstance(value, dict):
                raise TypeError("runtime stage preflight returned no projection")
        except Exception:
            value = {
                "available": False,
                "runtime_id": "navigation_odom_v1",
                "reason": {"code": f"{stage}_runtime_preflight_failed"},
            }
        if bool(value.get("available", False)):
            return
        capabilities = {
            "available": False,
            "runtime_id": str(value.get("runtime_id", "navigation_odom_v1")),
            "reason": _public_capability_reason(value.get("reason")),
        }
        raise AnnotationConflictError(
            "annotation_runtime_unavailable",
            "The annotation runtime is unavailable.",
            current={"capabilities": capabilities},
        )

    def list_calibration_profiles(
        self,
        *,
        purpose: str = "processing",
    ) -> dict[str, Any]:
        if purpose == "processing":
            return {"profiles": self.catalog.list_profiles()}
        return {"profiles": self.catalog.list_profiles(purpose=purpose)}

    def create_job(
        self,
        request: CreateAnnotationJobRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        public_payload = {
            "dataset_date": request.dataset_date,
            "source_clips": request.source_clips,
            "calibration_profile_ref": request.calibration_profile_ref,
            "calibration_content_sha256": request.calibration_content_sha256,
        }
        replay = self.store.replay_receipt(
            idempotency_key=idempotency_key,
            operation="create_job",
            request_payload=public_payload,
        )
        if replay is not None:
            return replay
        capabilities = self.capabilities()
        if not capabilities["available"]:
            raise AnnotationConflictError(
                "annotation_runtime_unavailable",
                "The annotation runtime is unavailable.",
                current={"capabilities": capabilities},
            )
        self._require_synced_inputs(request.dataset_date, request.source_clips)
        active_reserved_bytes = self.store.active_reserved_bytes()
        try:
            capacity = self.worker.preflight_capacity(
                request.dataset_date,
                request.source_clips,
                active_reserved_bytes=active_reserved_bytes,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "annotation_runtime_unavailable"))
            if code in {
                "unsupported_runtime_variant",
                "unsafe_runtime_input",
                "missing_runtime_input",
            }:
                raise AnnotationValidationError(
                    code,
                    "The selected synchronized data is not supported by the M1 runtime.",
                ) from exc
            raise AnnotationConflictError(
                "annotation_runtime_unavailable",
                "The annotation runtime preflight is unavailable.",
            ) from exc
        if not capacity["available"]:
            raise AnnotationConflictError(
                "insufficient_annotation_capacity",
                "The annotation work root has insufficient free space.",
            )
        reserved_bytes = int(capacity["estimated_input_bytes"]) * 3
        profile = self.catalog.get(
            request.calibration_profile_ref,
            request.calibration_content_sha256,
        )
        job_ref = f"job_{uuid4().hex}"
        snapshot_dir = self.work_root / "jobs" / job_ref / "calibration"
        accepted = False
        try:
            try:
                snapshot_files, snapshot_sha = self.catalog.snapshot(
                    profile,
                    snapshot_dir,
                )
            except (
                AnnotationConflictError,
                AnnotationValidationError,
            ):
                raise
            except OSError as exc:
                raise AnnotationValidationError(
                    "calibration_snapshot_unavailable",
                    "The processing calibration could not be frozen safely.",
                ) from exc
            if snapshot_sha != request.calibration_content_sha256:
                raise AnnotationConflictError(
                    "calibration_profile_changed",
                    "The calibration changed while the job was being created.",
                    current=profile.public_projection(),
                )
            result = self.store.create_job(
                job_ref=job_ref,
                dataset_date=request.dataset_date,
                source_clips=request.source_clips,
                calibration=profile.public_projection(),
                snapshot_dir=snapshot_dir,
                snapshot_files=snapshot_files,
                reserved_bytes=reserved_bytes,
                idempotency_key=idempotency_key,
            )
            accepted = result.get("job_ref") == job_ref
            return result
        finally:
            if not accepted and not self.store.has_job(job_ref):
                _rollback_unaccepted_job_directory(
                    work_root=self.work_root,
                    job_ref=job_ref,
                )

    def list_jobs(self) -> dict[str, Any]:
        return {"jobs": self.store.list_jobs()}

    def public_event_cursor(self) -> dict[str, int]:
        return {"cursor": self.store.public_event_cursor()}

    def list_public_events_after(
        self,
        *,
        after_seq: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.store.list_public_events_after(
            after_seq=after_seq,
            limit=limit,
        )

    def get_job(self, job_ref: str) -> dict[str, Any]:
        return self.store.get_job(job_ref)

    def get_segment(self, job_ref: str, segment_ref: str) -> dict[str, Any]:
        return self.store.get_segment(job_ref, segment_ref)

    def begin_postprocessing(
        self,
        job_ref: str,
        expected_job_revision: int,
        spec: PostprocessingSpecInput,
        *,
        idempotency_key: str,
        processing_navigation_task_ref: str | None = None,
    ) -> dict[str, Any]:
        spec_payload = spec.model_dump(mode="json")
        request_payload = {
            "job_ref": job_ref,
            "expected_job_revision": expected_job_revision,
            "spec": spec_payload,
        }
        if processing_navigation_task_ref is not None:
            request_payload["processing_navigation_task_ref"] = (
                processing_navigation_task_ref
            )
        replay = self.store.replay_receipt(
            idempotency_key=idempotency_key,
            operation="begin_postprocessing",
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        self._require_runtime_stage(
            "postprocessing",
            decision=spec_payload,
        )
        return self.store.begin_postprocessing(
            job_ref=job_ref,
            expected_job_revision=expected_job_revision,
            spec=spec_payload,
            idempotency_key=idempotency_key,
            processing_navigation_task_ref=processing_navigation_task_ref,
        )

    def complete_postprocessing(
        self,
        job_ref: str,
        expected_job_revision: int,
        trajectories: list[dict[str, Any]],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.complete_postprocessing(
            job_ref=job_ref,
            expected_job_revision=expected_job_revision,
            trajectories=trajectories,
            idempotency_key=idempotency_key,
        )

    def list_reviews(
        self,
        *,
        status: str | None = None,
        dataset_date: str | None = None,
        source_clip: str | None = None,
    ) -> dict[str, Any]:
        return {
            "reviews": self.store.list_reviews(
                status=status,
                dataset_date=dataset_date,
                source_clip=source_clip,
            )
        }

    def get_review(self, review_ref: str) -> dict[str, Any]:
        return self.store.get_review(review_ref)

    def get_review_trajectory_evidence(
        self,
        review_ref: str,
    ) -> dict[str, Any]:
        from vla_data_juicer_agents.annotation.runtime import _tree_sha256
        from vla_data_juicer_agents.annotation.trajectory_evidence import (
            gridmap_metadata,
            resolve_evidence_file,
        )

        private = self.store.review_evidence_private(review_ref)
        artifact_root = Path(private["private_artifact_path"])
        try:
            actual_artifact_sha256 = _tree_sha256(
                artifact_root,
                unsafe_code="trajectory_revision_changed",
            )
        except Exception as exc:
            raise AnnotationConflictError(
                "trajectory_revision_changed",
                "The trajectory evidence is no longer available.",
            ) from exc
        if actual_artifact_sha256 != private["artifact_sha256"]:
            raise AnnotationConflictError(
                "trajectory_revision_changed",
                "The trajectory evidence changed after it was frozen.",
            )
        state = private["trajectory_state"]
        frames = state.get("frames")
        if not isinstance(frames, list):
            raise AnnotationConflictError(
                "trajectory_evidence_unavailable",
                "The trajectory evidence index is unavailable.",
            )
        camera_size: tuple[int, int] | None = None
        projection_size: tuple[int, int] | None = None
        fallback_gridmap_metadata: dict[str, Any] | None = None
        available_paths: dict[tuple[int, str], Path] = {}
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue
            for kind in ("camera", "projection", "gridmap"):
                try:
                    path = resolve_evidence_file(
                        artifact_root,
                        state,
                        frame_index=index,
                        kind=kind,
                    )
                    available_paths[(index, kind)] = path
                    if kind == "camera" and camera_size is None:
                        content = _read_regular_descendant(
                            path,
                            root=artifact_root,
                        )
                        image_format, width, height = (
                            _image_dimensions_from_bytes(content)
                        )
                        if image_format not in {"jpeg", "png"}:
                            raise ValueError("unsupported camera image")
                        camera_size = (width, height)
                    elif kind == "projection" and projection_size is None:
                        content = _read_regular_descendant(
                            path,
                            root=artifact_root,
                        )
                        image_format, width, height = (
                            _image_dimensions_from_bytes(content)
                        )
                        if image_format != "png":
                            raise ValueError("unsupported projection image")
                        projection_size = (width, height)
                    elif (
                        kind == "gridmap"
                        and fallback_gridmap_metadata is None
                    ):
                        fallback_gridmap_metadata = gridmap_metadata(
                            _read_regular_descendant(
                                path,
                                root=artifact_root,
                            )
                        )
                except (
                    AnnotationConflictError,
                    AnnotationValidationError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ):
                    available_paths.pop((index, kind), None)
        public_frames: list[dict[str, Any]] = []
        for frame in frames:
            if not isinstance(frame, dict):
                raise AnnotationConflictError(
                    "trajectory_evidence_unavailable",
                    "The trajectory evidence index is invalid.",
                )
            frame_index = frame.get("frame_index")
            if not isinstance(frame_index, int) or isinstance(frame_index, bool):
                raise AnnotationConflictError(
                    "trajectory_evidence_unavailable",
                    "The trajectory evidence frame identity is invalid.",
                )
            raw_targets = frame.get("targets")
            if not isinstance(raw_targets, dict):
                raise AnnotationConflictError(
                    "trajectory_evidence_unavailable",
                    "The trajectory evidence target set is invalid.",
                )
            targets = [
                {
                    "target_ref": target_ref,
                    "label": target.get("label"),
                    "position": target.get("position"),
                    "direction": target.get("direction"),
                    "speed": target.get("speed"),
                    "color": target.get("color"),
                    "image_box": target.get("image_box"),
                    "trajectory_points": target.get("trajectory_points"),
                }
                for target_ref, target in raw_targets.items()
                if isinstance(target_ref, str) and isinstance(target, dict)
            ]
            targets.sort(
                key=lambda target: (
                    0 if target["label"] == "Master" else 1,
                    str(target["label"]),
                )
            )
            camera = None
            if (frame_index, "camera") in available_paths:
                camera = {
                    "url": (
                        f"/api/annotation/reviews/{review_ref}/evidence/"
                        f"frames/{frame_index}/camera"
                    ),
                    "width": camera_size[0] if camera_size else None,
                    "height": camera_size[1] if camera_size else None,
                }
            projection = None
            if (frame_index, "projection") in available_paths:
                projection = {
                    "url": (
                        f"/api/annotation/reviews/{review_ref}/evidence/"
                        f"frames/{frame_index}/projection"
                    ),
                    "width": (
                        projection_size[0] if projection_size else None
                    ),
                    "height": (
                        projection_size[1] if projection_size else None
                    ),
                }
            gridmap = None
            if (frame_index, "gridmap") in available_paths:
                gridmap_metadata_value = {
                    "width": frame.get("gridmap_width"),
                    "height": frame.get("gridmap_height"),
                    "resolution": frame.get("gridmap_resolution"),
                    "x_range": frame.get("gridmap_x_range"),
                    "y_range": frame.get("gridmap_y_range"),
                }
                if any(
                    value is None
                    for value in gridmap_metadata_value.values()
                ):
                    gridmap_metadata_value = (
                        fallback_gridmap_metadata or {}
                    )
                gridmap_width = gridmap_metadata_value.get("width")
                gridmap_height = gridmap_metadata_value.get("height")
                gridmap_resolution = gridmap_metadata_value.get("resolution")
                gridmap_x_range = gridmap_metadata_value.get("x_range")
                gridmap_y_range = gridmap_metadata_value.get("y_range")
                if (
                    not isinstance(gridmap_width, int)
                    or isinstance(gridmap_width, bool)
                    or gridmap_width < 1
                    or not isinstance(gridmap_height, int)
                    or isinstance(gridmap_height, bool)
                    or gridmap_height < 1
                    or not isinstance(gridmap_resolution, (int, float))
                    or isinstance(gridmap_resolution, bool)
                    or not isinstance(gridmap_x_range, list)
                    or len(gridmap_x_range) != 2
                    or not isinstance(gridmap_y_range, list)
                    or len(gridmap_y_range) != 2
                ):
                    raise AnnotationConflictError(
                        "trajectory_evidence_unavailable",
                        "The gridmap evidence metadata is invalid.",
                    )
                gridmap = {
                    "url": (
                        f"/api/annotation/reviews/{review_ref}/evidence/"
                        f"frames/{frame_index}/gridmap"
                    ),
                    "width": gridmap_width,
                    "height": gridmap_height,
                    "resolution": gridmap_resolution,
                    "x_range": gridmap_x_range,
                    "y_range": gridmap_y_range,
                }
            public_frames.append(
                {
                    "frame_index": frame_index,
                    "pass": bool(frame.get("pass", False)),
                    "camera": camera,
                    "projection": projection,
                    "gridmap": gridmap,
                    "targets": targets,
                }
            )
        draft_state = private.get("draft_state")
        draft_commands = (
            draft_state.get("commands")
            if isinstance(draft_state, dict)
            and isinstance(draft_state.get("commands"), list)
            else []
        )
        return {
            "availability": "available",
            "review_ref": review_ref,
            "trajectory_revision_ref": private[
                "trajectory_revision_ref"
            ],
            "review_state_revision": private["state_revision"],
            "draft_revision": private.get("draft_revision"),
            "frame_count": len(public_frames),
            "frames": public_frames,
            "draft_commands": draft_commands,
        }

    def resolve_review_evidence_file(
        self,
        review_ref: str,
        *,
        frame_index: int,
        kind: str,
        verify_tree: bool = True,
    ) -> tuple[bytes, str, str]:
        from vla_data_juicer_agents.annotation.runtime import _tree_sha256
        from vla_data_juicer_agents.annotation.trajectory_evidence import (
            render_gridmap_png,
            resolve_evidence_file,
        )

        private = self.store.review_evidence_private(review_ref)
        artifact_root = Path(private["private_artifact_path"])
        if verify_tree:
            try:
                actual_sha256 = _tree_sha256(
                    artifact_root,
                    unsafe_code="trajectory_revision_changed",
                )
            except Exception as exc:
                raise AnnotationConflictError(
                    "trajectory_revision_changed",
                    "The trajectory evidence is no longer available.",
                ) from exc
            if actual_sha256 != private["artifact_sha256"]:
                raise AnnotationConflictError(
                    "trajectory_revision_changed",
                    "The trajectory evidence changed after it was frozen.",
                )
        try:
            path = resolve_evidence_file(
                artifact_root,
                private["trajectory_state"],
                frame_index=frame_index,
                kind=kind,
            )
            content = _read_regular_descendant(path, root=artifact_root)
            if kind == "gridmap":
                content, _width, _height = render_gridmap_png(content)
            elif kind == "projection":
                image_format, _width, _height = (
                    _image_dimensions_from_bytes(content)
                )
                if image_format != "png":
                    raise ValueError(
                        "projection evidence must be a PNG image"
                    )
        except AnnotationValidationError as exc:
            raise AnnotationValidationError(
                "trajectory_evidence_unavailable",
                "The requested trajectory evidence is unavailable.",
            ) from exc
        except Exception as exc:
            raise AnnotationValidationError(
                "trajectory_evidence_unavailable",
                "The requested trajectory evidence is unavailable.",
            ) from exc
        suffix = path.suffix.lower()
        media_type = (
            "image/png"
            if kind == "gridmap"
            else {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
            }.get(suffix)
        )
        if media_type is None:
            raise AnnotationValidationError(
                "trajectory_evidence_unavailable",
                "The requested trajectory evidence format is unsupported.",
            )
        return content, hashlib.sha256(content).hexdigest(), media_type

    def get_processing_facts(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, Any]:
        return self.store.get_processing_facts(
            dataset_date=dataset_date,
            source_clips=source_clips,
        )

    def resolve_scope_binding(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, Any]:
        """Private gateway binding; callers must never expose the refs to an LLM."""

        return self.store.resolve_scope_binding(
            dataset_date=dataset_date,
            source_clips=source_clips,
        )

    def resolve_navigation_task_binding(
        self,
        *,
        navigation_task_ref: str,
        link_kind: str,
    ) -> dict[str, Any]:
        """Private task lineage lookup for the in-process Navigation gateway."""

        return self.store.resolve_navigation_task_binding(
            navigation_task_ref=navigation_task_ref,
            link_kind=link_kind,
        )

    def resolve_navigation_review_outcome(
        self,
        *,
        navigation_task_ref: str,
    ) -> dict[str, Any]:
        """Private ref-free review aggregate for the Navigation gateway."""

        return self.store.resolve_navigation_review_outcome(
            navigation_task_ref=navigation_task_ref,
        )

    def link_navigation_task(
        self,
        *,
        job_ref: str,
        review_ref: str | None,
        navigation_task_ref: str,
        parent_navigation_task_ref: str | None,
        link_kind: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.link_navigation_task(
            job_ref=job_ref,
            review_ref=review_ref,
            navigation_task_ref=navigation_task_ref,
            parent_navigation_task_ref=parent_navigation_task_ref,
            link_kind=link_kind,
            idempotency_key=idempotency_key,
        )

    def create_workflow_handoff(
        self,
        *,
        job_ref: str,
        review_ref: str | None,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.create_workflow_handoff(
            job_ref=job_ref,
            review_ref=review_ref,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def create_fix_session(
        self,
        review_ref: str,
        request: CreateFixSessionRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        public_payload = {
            "review_ref": review_ref,
            "expected_review_revision": request.expected_review_revision,
            "calibration_profile_ref": request.calibration_profile_ref,
            "calibration_content_sha256": request.calibration_content_sha256,
            "difference_reason": request.calibration_difference_reason,
        }
        replay = self.store.replay_receipt(
            idempotency_key=idempotency_key,
            operation="create_fix_session",
            request_payload=public_payload,
        )
        if replay is not None:
            return replay
        private = self.store.fix_runtime_input(review_ref)
        if private["state_revision"] != request.expected_review_revision:
            raise AnnotationConflictError(
                "review_revision_conflict",
                "The trajectory review changed; refresh before retrying.",
                current=self.store.get_review(review_ref),
            )
        self._require_runtime_stage("fix")
        if private["status"] == "returned" and private["draft"] is not None:
            draft_calibration = private["draft"]["calibration"]
            if (
                draft_calibration["profile_ref"] != request.calibration_profile_ref
                or draft_calibration["content_sha256"]
                != request.calibration_content_sha256
                or draft_calibration["difference_reason"]
                != request.calibration_difference_reason
            ):
                raise AnnotationConflictError(
                    "fix_calibration_frozen",
                    "The returned Fix draft must resume with its frozen calibration.",
                    current=self.store.get_review(review_ref),
                )
            return self.store.resume_returned_review(
                review_ref=review_ref,
                expected_review_revision=request.expected_review_revision,
                calibration_profile_ref=request.calibration_profile_ref,
                calibration_content_sha256=request.calibration_content_sha256,
                difference_reason=request.calibration_difference_reason,
                idempotency_key=idempotency_key,
            )
        if self.fix_runtime is None:
            raise AnnotationConflictError(
                "fix_runtime_unavailable",
                "The Fix runtime is unavailable.",
            )
        profile = self.catalog.get(
            request.calibration_profile_ref,
            request.calibration_content_sha256,
            purpose="fix",
        )
        snapshot_ref = f"fix_calibration_{uuid4().hex}"
        snapshot_dir = (
            self.work_root
            / "reviews"
            / review_ref
            / "calibration"
            / snapshot_ref
        )
        accepted = False
        try:
            snapshot_files, snapshot_sha = self.catalog.snapshot(
                profile,
                snapshot_dir,
            )
            if snapshot_sha != request.calibration_content_sha256:
                raise AnnotationConflictError(
                    "calibration_profile_changed",
                    "The Fix calibration changed while the session was created.",
                    current=profile.public_projection(),
                )
            try:
                runtime_state = FixRuntimeState.model_validate(
                    self.fix_runtime.initialize(
                        private["trajectory_state"],
                    calibration_snapshot={
                        "snapshot_ref": snapshot_ref,
                        "profile_ref": profile.profile_ref,
                        "content_sha256": snapshot_sha,
                        "private_snapshot_dir": str(snapshot_dir),
                        "files": snapshot_files,
                    },
                    )
                )
            except Exception as exc:
                raise AnnotationConflictError(
                    "fix_runtime_failed",
                    "The Fix runtime could not initialize a draft.",
                ) from exc
            result = self.store.create_fix_session(
                review_ref=review_ref,
                expected_review_revision=request.expected_review_revision,
                calibration=profile.public_projection(),
                snapshot_ref=snapshot_ref,
                snapshot_dir=snapshot_dir,
                snapshot_files=snapshot_files,
                difference_reason=request.calibration_difference_reason,
                initial_state=runtime_state.state,
                initial_state_sha256=runtime_state.content_sha256,
                idempotency_key=idempotency_key,
            )
            accepted = True
            return result
        finally:
            if not accepted:
                _rollback_unaccepted_fix_snapshot(
                    work_root=self.work_root,
                    review_ref=review_ref,
                    snapshot_ref=snapshot_ref,
                )

    def apply_fix_command(
        self,
        review_ref: str,
        request: ApplyFixCommandRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": request.expected_review_revision,
            "expected_draft_revision": request.expected_draft_revision,
            "command": request.command.model_dump(mode="json"),
        }
        replay = self.store.replay_receipt(
            idempotency_key=idempotency_key,
            operation="apply_fix_command",
            request_payload=payload,
        )
        if replay is not None:
            return replay
        if self.fix_runtime is None:
            raise AnnotationConflictError(
                "fix_runtime_unavailable",
                "The Fix runtime is unavailable.",
            )
        private = self.store.fix_runtime_input(review_ref)
        draft = private["draft"]
        if draft is None:
            raise AnnotationConflictError(
                "fix_session_required",
                "Start a Fix session before applying changes.",
                current=self.store.get_review(review_ref),
            )
        if (
            private["state_revision"] != request.expected_review_revision
            or draft["revision"] != request.expected_draft_revision
        ):
            raise AnnotationConflictError(
                "fix_draft_revision_conflict",
                "The Fix draft changed; refresh before retrying.",
                current=self.store.get_review(review_ref),
            )
        try:
            runtime_state = FixRuntimeState.model_validate(
                self.fix_runtime.apply(
                    draft["state"],
                    request.command,
                )
            )
        except Exception as exc:
            raise AnnotationConflictError(
                "fix_runtime_failed",
                "The Fix runtime could not apply the requested change.",
            ) from exc
        return self.store.apply_fix_command_result(
            review_ref=review_ref,
            expected_review_revision=request.expected_review_revision,
            expected_draft_revision=request.expected_draft_revision,
            command=request.command.model_dump(mode="json"),
            result_state=runtime_state.state,
            result_sha256=runtime_state.content_sha256,
            idempotency_key=idempotency_key,
        )

    def create_fix_revision(
        self,
        review_ref: str,
        request: CreateFixRevisionRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "review_ref": review_ref,
            "expected_review_revision": request.expected_review_revision,
            "expected_draft_revision": request.expected_draft_revision,
        }
        replay = self.store.replay_receipt(
            idempotency_key=idempotency_key,
            operation="create_fix_revision",
            request_payload=payload,
        )
        if replay is not None:
            return replay
        self._require_runtime_stage("fix")
        return self.store.create_fix_revision(
            review_ref=review_ref,
            expected_review_revision=request.expected_review_revision,
            expected_draft_revision=request.expected_draft_revision,
            idempotency_key=idempotency_key,
        )

    def return_review(
        self,
        review_ref: str,
        request: ReturnReviewRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.decide_review(
            operation="return",
            review_ref=review_ref,
            expected_review_revision=request.expected_review_revision,
            reason=request.reason,
            idempotency_key=idempotency_key,
        )

    def discard_review(
        self,
        review_ref: str,
        request: DiscardReviewRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.decide_review(
            operation="discard",
            review_ref=review_ref,
            expected_review_revision=request.expected_review_revision,
            reason=request.reason,
            idempotency_key=idempotency_key,
        )

    def approve_review(
        self,
        review_ref: str,
        request: ApproveReviewRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.approve_fix_revision(
            review_ref=review_ref,
            expected_review_revision=request.expected_review_revision,
            fix_revision_ref=request.fix_revision_ref,
            idempotency_key=idempotency_key,
        )

    def retry_publication(
        self,
        review_ref: str,
        request: RetryPublicationRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.retry_compatibility_publication(
            review_ref=review_ref,
            expected_review_revision=request.expected_review_revision,
            idempotency_key=idempotency_key,
        )

    def save_draft(
        self,
        job_ref: str,
        segment_ref: str,
        request: DraftRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.save_draft(
            job_ref=job_ref,
            segment_ref=segment_ref,
            expected_segment_revision=request.expected_segment_revision,
            expected_draft_revision=request.expected_draft_revision,
            targets=[target.model_dump(mode="json") for target in request.targets],
            idempotency_key=idempotency_key,
        )

    def submit(
        self,
        job_ref: str,
        segment_ref: str,
        request: SubmitRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.submit_segment(
            job_ref=job_ref,
            segment_ref=segment_ref,
            expected_segment_revision=request.expected_segment_revision,
            expected_draft_revision=request.expected_draft_revision,
            idempotency_key=idempotency_key,
        )

    def segment_action(
        self,
        operation: str,
        job_ref: str,
        segment_ref: str,
        request: SegmentRevisionRequest | SkipRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.store.segment_action(
            operation=operation,
            job_ref=job_ref,
            segment_ref=segment_ref,
            expected_segment_revision=request.expected_segment_revision,
            idempotency_key=idempotency_key,
            reason_code=request.reason_code if isinstance(request, SkipRequest) else None,
            note=request.note if isinstance(request, SkipRequest) else None,
        )

    def job_action(
        self,
        operation: str,
        job_ref: str,
        request: ExpectedJobRevisionRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        arguments = {
            "job_ref": job_ref,
            "expected_job_revision": request.expected_job_revision,
            "idempotency_key": idempotency_key,
        }
        if operation == "tracking":
            return self.store.start_tracking(**arguments)
        if operation == "complete_no_processable_targets":
            return self.store.complete_no_processable_targets(**arguments)
        if operation == "cancel":
            result = self.store.cancel_job(**arguments)
            if self.worker.owns_active_run(job_ref):
                self.worker.request_cancel(job_ref)
            return result
        if operation == "retry":
            payload = {
                "job_ref": job_ref,
                "expected_job_revision": request.expected_job_revision,
            }
            replay = self.store.replay_receipt(
                idempotency_key=idempotency_key,
                operation="retry_job",
                request_payload=payload,
            )
            if replay is not None:
                return replay
            job = self.store.get_job(job_ref)
            if int(
                job.get("counts", {}).get("postprocessing_failed", 0)
            ) > 0:
                # The decision was already accepted and frozen by the first
                # attempt; retry only needs a fresh payload/publication proof.
                self._require_runtime_stage("postprocessing")
            return self.store.retry_job(**arguments)
        raise RuntimeError(f"unsupported annotation job action: {operation}")

    def resolve_first_frame(
        self,
        job_ref: str,
        segment_ref: str,
    ) -> tuple[bytes, str, str]:
        private = self.store.first_frame_private(job_ref, segment_ref)
        if not private["staging_root"]:
            raise AnnotationValidationError(
                "first_frame_unavailable",
                "The first frame is not available.",
            )
        root = Path(private["staging_root"])
        path = Path(private["path"])
        _require_safe_regular_descendant(path, root=root)
        if path.suffix.lower() not in {".jpg", ".png"}:
            raise AnnotationValidationError(
                "unsafe_first_frame",
                "The first frame has an unsupported image format.",
            )
        content = _read_regular_descendant(path, root=root)
        actual_sha = hashlib.sha256(content).hexdigest()
        if actual_sha != private["sha256"]:
            raise AnnotationConflictError(
                "first_frame_changed",
                "The first frame changed after preparation.",
            )
        try:
            image_format, width, height = _image_dimensions_from_bytes(content)
        except ValueError as exc:
            raise AnnotationConflictError(
                "first_frame_changed",
                "The first frame is no longer a valid prepared image.",
            ) from exc
        expected_format = "png" if path.suffix.lower() == ".png" else "jpeg"
        if (
            image_format != expected_format
            or width != private["width"]
            or height != private["height"]
        ):
            raise AnnotationConflictError(
                "first_frame_changed",
                "The first frame dimensions changed after preparation.",
            )
        media_type = "image/png" if image_format == "png" else "image/jpeg"
        return content, str(private["etag"]), media_type

    def _require_synced_inputs(self, dataset_date: str, clips: list[str]) -> None:
        root_lexical = self.clip_data_root.absolute()
        _require_real_directory(root_lexical, code="unsafe_clip_scope")
        root = root_lexical.resolve(strict=True)
        for clip in clips:
            cursor = root_lexical
            for component in (dataset_date, clip, "sync_data"):
                cursor = cursor / component
                _require_real_directory(cursor, code="unsafe_clip_scope")
            sync_dir = cursor
            try:
                resolved = sync_dir.resolve(strict=True)
            except OSError as exc:
                raise AnnotationValidationError(
                    "clip_not_synced",
                    "Every selected clip must have synchronized data.",
                ) from exc
            if root not in resolved.parents or not resolved.is_dir():
                raise AnnotationValidationError(
                    "unsafe_clip_scope",
                    "A selected clip resolved outside the configured dataset root.",
                )


def _require_safe_regular_descendant(path: Path, *, root: Path) -> None:
    root_lexical = root.absolute()
    path_lexical = path.absolute()
    try:
        relative_lexical = path_lexical.relative_to(root_lexical)
    except ValueError as exc:
        raise AnnotationValidationError(
            "unsafe_first_frame",
            "The first frame escaped the annotation staging root.",
        ) from exc
    try:
        root_metadata = root_lexical.lstat()
    except OSError as exc:
        raise AnnotationValidationError(
            "first_frame_unavailable",
            "The first frame is not available.",
        ) from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise AnnotationValidationError(
            "unsafe_first_frame",
            "The annotation staging root must be a real directory.",
        )
    cursor = root_lexical
    for component in relative_lexical.parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise AnnotationValidationError(
                "first_frame_unavailable",
                "The first frame is not available.",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AnnotationValidationError(
                "unsafe_first_frame",
                "The first frame path cannot contain symlinks.",
            )
    try:
        root_resolved = root_lexical.resolve(strict=True)
        path_resolved = path_lexical.resolve(strict=True)
    except OSError as exc:
        raise AnnotationValidationError(
            "first_frame_unavailable",
            "The first frame is not available.",
        ) from exc
    if path_resolved == root_resolved or root_resolved not in path_resolved.parents:
        raise AnnotationValidationError(
            "unsafe_first_frame",
            "The first frame escaped the annotation staging root.",
        )
    metadata = path_lexical.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise AnnotationValidationError(
            "unsafe_first_frame",
            "The first frame must be a regular file.",
        )


def _read_regular_descendant(path: Path, *, root: Path) -> bytes:
    root_lexical = root.absolute()
    path_lexical = path.absolute()
    try:
        relative = path_lexical.relative_to(root_lexical)
    except ValueError as exc:
        raise AnnotationValidationError(
            "unsafe_first_frame",
            "The first frame escaped the annotation staging root.",
        ) from exc
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        directory_fd = os.open(root_lexical, directory_flags)
        descriptors.append(directory_fd)
        for component in relative.parts[:-1]:
            directory_fd = os.open(
                component,
                directory_flags,
                dir_fd=directory_fd,
            )
            descriptors.append(directory_fd)
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(file_fd)
            raise AnnotationValidationError(
                "unsafe_first_frame",
                "The first frame must be a regular file.",
            )
        with os.fdopen(file_fd, "rb") as handle:
            return handle.read()
    except AnnotationValidationError:
        raise
    except OSError as exc:
        raise AnnotationValidationError(
            "first_frame_unavailable",
            "The first frame is not available.",
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _image_dimensions_from_bytes(content: bytes) -> tuple[str, int, int]:
    stream = BytesIO(content)
    prefix = stream.read(24)
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(prefix) < 24 or prefix[12:16] != b"IHDR":
            raise ValueError("invalid PNG IHDR")
        width, height = struct.unpack(">II", prefix[16:24])
        if width <= 0 or height <= 0:
            raise ValueError("invalid PNG dimensions")
        return "png", width, height
    if not prefix.startswith(b"\xff\xd8"):
        raise ValueError("image is neither PNG nor JPEG")
    stream.seek(2)
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while True:
        marker_prefix = stream.read(1)
        if not marker_prefix:
            raise ValueError("JPEG has no start-of-frame marker")
        if marker_prefix != b"\xff":
            continue
        marker_byte = stream.read(1)
        while marker_byte == b"\xff":
            marker_byte = stream.read(1)
        if not marker_byte:
            raise ValueError("truncated JPEG marker")
        marker = marker_byte[0]
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        length_bytes = stream.read(2)
        if len(length_bytes) != 2:
            raise ValueError("truncated JPEG segment")
        segment_length = struct.unpack(">H", length_bytes)[0]
        if segment_length < 2:
            raise ValueError("invalid JPEG segment length")
        if marker in sof_markers:
            payload = stream.read(5)
            if len(payload) != 5:
                raise ValueError("truncated JPEG start-of-frame")
            height, width = struct.unpack(">HH", payload[1:5])
            if width <= 0 or height <= 0:
                raise ValueError("invalid JPEG dimensions")
            return "jpeg", width, height
        stream.seek(segment_length - 2, 1)


def _require_real_directory(path: Path, *, code: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AnnotationValidationError(
            "clip_not_synced",
            "Every selected clip must have synchronized data.",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AnnotationValidationError(
            code,
            "Synchronized input paths must be real directories.",
        )


def _rollback_unaccepted_job_directory(*, work_root: Path, job_ref: str) -> None:
    work_root_lexical = work_root.absolute()
    jobs_root = work_root_lexical / "jobs"
    job_directory = jobs_root / job_ref
    if not job_directory.exists() and not job_directory.is_symlink():
        return
    if not re.fullmatch(r"job_[0-9a-f]{32}", job_ref):
        return
    # Cleanup is best-effort and must fail closed. A symlink in any ancestor
    # of the configured work root makes lexical containment insufficient, so
    # preserve the private orphan for an operator rather than deleting it.
    if not _all_directory_ancestors_are_real(work_root_lexical):
        return
    try:
        work_metadata = work_root_lexical.lstat()
        jobs_metadata = jobs_root.lstat()
        job_metadata = job_directory.lstat()
        children = list(job_directory.iterdir())
    except OSError:
        return
    if any(
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
        for metadata in (work_metadata, jobs_metadata, job_metadata)
    ):
        return
    try:
        jobs_resolved = jobs_root.resolve(strict=True)
        job_resolved = job_directory.resolve(strict=True)
    except OSError:
        return
    if job_resolved.parent != jobs_resolved:
        return
    if any(child.name != "calibration" for child in children):
        return
    for path in job_directory.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            return
    shutil.rmtree(job_directory)


def _rollback_unaccepted_fix_snapshot(
    *,
    work_root: Path,
    review_ref: str,
    snapshot_ref: str,
) -> None:
    if not re.fullmatch(r"review_[0-9a-f]{32}", review_ref):
        return
    if not re.fullmatch(r"fix_calibration_[0-9a-f]{32}", snapshot_ref):
        return
    work_root_lexical = work_root.absolute()
    reviews_root = work_root_lexical / "reviews"
    review_root = reviews_root / review_ref
    calibration_root = review_root / "calibration"
    snapshot_dir = calibration_root / snapshot_ref
    if not snapshot_dir.exists() and not snapshot_dir.is_symlink():
        return
    if not _all_directory_ancestors_are_real(work_root_lexical):
        return
    try:
        metadata_chain = [
            work_root_lexical.lstat(),
            reviews_root.lstat(),
            review_root.lstat(),
            calibration_root.lstat(),
            snapshot_dir.lstat(),
        ]
    except OSError:
        return
    if any(
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
        for metadata in metadata_chain
    ):
        return
    try:
        if (
            snapshot_dir.resolve(strict=True).parent
            != calibration_root.resolve(strict=True)
        ):
            return
    except OSError:
        return
    for path in snapshot_dir.rglob("*"):
        try:
            metadata = path.lstat()
        except OSError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            return
    shutil.rmtree(snapshot_dir)
    try:
        calibration_root.rmdir()
        review_root.rmdir()
        reviews_root.rmdir()
    except OSError:
        pass


def _all_directory_ancestors_are_real(path: Path) -> bool:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    try:
        root_metadata = cursor.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        return False
    for component in absolute.parts[1:]:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False
    return True
