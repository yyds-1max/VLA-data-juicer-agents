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
    AnnotationConflictError,
    AnnotationValidationError,
    CreateAnnotationJobRequest,
    DraftRequest,
    ExpectedJobRevisionRequest,
    SegmentRevisionRequest,
    SkipRequest,
    SubmitRequest,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.navigation.config import NavigationSettings


_PUBLIC_CAPABILITY_ERROR_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$",
)


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
    error_ref = reason.get("error_ref")
    if (
        isinstance(error_ref, str)
        and _PUBLIC_CAPABILITY_ERROR_REF_RE.fullmatch(error_ref)
    ):
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

    def list_calibration_profiles(self) -> dict[str, Any]:
        return {"profiles": self.catalog.list_profiles()}

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

    def get_job(self, job_ref: str) -> dict[str, Any]:
        return self.store.get_job(job_ref)

    def get_segment(self, job_ref: str, segment_ref: str) -> dict[str, Any]:
        return self.store.get_segment(job_ref, segment_ref)

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
