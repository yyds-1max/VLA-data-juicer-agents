"""Frozen navigation annotation runtime adapters for M1.

The adapter performs orchestration, staging isolation, and exact invocation of
the frozen business payload.  It does not reimplement any preprocessing or
Tracking algorithm.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import stat
import subprocess
import threading
from typing import Callable, Iterable, Iterator, Sequence

from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    TurnCancelled,
    bind_cancellation,
    current_cancellation,
)
from vla_data_juicer_agents.navigation.golden.image_headers import (
    ImageHeaderError,
    image_dimensions,
)
from vla_data_juicer_agents.navigation.runtime_manifest import (
    load_manifest,
    verify_root,
)
from vla_data_juicer_agents.navigation.subprocess_runner import run_command
from vla_data_juicer_agents.navigation.writer_lock import (
    NavigationWriterLockError,
    navigation_writer_coordination_status,
    navigation_writer_lock,
    validate_writer_lock_path,
)


RUNTIME_ID = "navigation_odom_v1"
EXPECTED_XVFB_VERSION = "2:1.20.13-1ubuntu1~20.04.20"
PREPARATION_COMMAND_STEPS = (
    "processing_calibration_snapshot",
    "assemble_finish_temp",
    "preprocess_create_box",
    "preprocess_odom_convert",
    "preprocess_resize",
    "metadata_generate",
    "map_publish",
    "video_prepare",
)
TRACKING_COMMAND_STEP = "tracking"

_DATE_RE = re.compile(r"^[0-9]{8}$")
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
_CLIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_IDENTITY_RE = re.compile(
    r"^(?:master|other[0-9]+)_[a-z]+_[a-z]+_[a-z]+$",
)
_REQUIRED_SENSOR_FILES = ("fisheye_front.json", "r32_rslidar_points.json")
_FIRST_FRAME_SUFFIXES = frozenset({".jpg", ".png"})


class RuntimeUnavailableError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        return_code: int | None = None,
        diagnostic_kind: str = "error",
        private_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.return_code = return_code
        self.diagnostic_kind = diagnostic_kind
        self.private_detail = private_detail


@dataclass(frozen=True)
class RuntimeStepEvent:
    safe_step_code: str
    status: str
    return_code: int | None = None
    diagnostic_kind: str | None = None


RuntimeStepObserver = Callable[[RuntimeStepEvent], None]


def _notify_runtime_step(
    observer: RuntimeStepObserver | None,
    safe_step_code: str,
    status: str,
    *,
    return_code: int | None = None,
    diagnostic_kind: str | None = None,
) -> None:
    if observer is None:
        return
    observer(
        RuntimeStepEvent(
            safe_step_code=safe_step_code,
            status=status,
            return_code=return_code,
            diagnostic_kind=diagnostic_kind,
        )
    )


@contextmanager
def _observed_runtime_step(
    observer: RuntimeStepObserver | None,
    safe_step_code: str,
) -> Iterator[None]:
    if observer is None:
        yield
        return
    _notify_runtime_step(
        observer,
        safe_step_code,
        "started",
    )
    try:
        yield
    except BaseException as exc:
        return_code = getattr(exc, "return_code", None)
        diagnostic_kind = getattr(exc, "diagnostic_kind", None)
        if isinstance(exc, TurnCancelled):
            diagnostic_kind = "cancelled"
        elif isinstance(exc, subprocess.TimeoutExpired):
            diagnostic_kind = "timeout"
        if diagnostic_kind not in {
            "nonzero_exit",
            "timeout",
            "cancelled",
            "error",
        }:
            diagnostic_kind = "error"
        _notify_runtime_step(
            observer,
            safe_step_code,
            "failed",
            return_code=(
                return_code
                if isinstance(return_code, int)
                and not isinstance(return_code, bool)
                else None
            ),
            diagnostic_kind=diagnostic_kind,
        )
        raise
    else:
        _notify_runtime_step(
            observer,
            safe_step_code,
            "succeeded",
            return_code=0,
        )


def _private_command_failure_detail(
    *,
    diagnostic_kind: str,
    return_code: int | None,
    stdout: object,
    stderr: object,
) -> str:
    """Build bounded private evidence without persisting the command or cwd."""

    def tail(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            rendered = value.decode("utf-8", errors="replace")
        else:
            rendered = str(value)
        return rendered[-8000:]

    return "\n".join(
        (
            f"diagnostic_kind={diagnostic_kind}",
            f"return_code={return_code if return_code is not None else 'unknown'}",
            "stdout_tail:",
            tail(stdout),
            "stderr_tail:",
            tail(stderr),
        )
    )


@dataclass(frozen=True)
class RuntimeCapabilityReason:
    code: str
    message: str


@dataclass(frozen=True)
class RuntimeCapabilities:
    available: bool
    runtime_id: str = RUNTIME_ID
    reason: RuntimeCapabilityReason | None = None


@dataclass(frozen=True)
class NavigationAnnotationRuntimeConfig:
    runtime_source_root: Path | None
    work_root: Path | None
    clip_data_root: Path | None
    data_python: Path | None
    data_env_setup: Path | None
    manifest_path: Path
    bwrap_path: Path = Path("/usr/bin/bwrap")
    xvfb_run_path: Path = Path("/usr/bin/xvfb-run")
    xvfb_path: Path = Path("/usr/bin/Xvfb")
    xvfb_deb_path: Path | None = None
    runtime_dependency_summary_path: Path | None = None
    dpkg_query_path: Path = Path("/usr/bin/dpkg-query")
    expected_xvfb_version: str = EXPECTED_XVFB_VERSION
    legacy_tracking_data_root: Path = Path(
        "/mnt/data1/gh/tracking_1/Data",
    )
    legacy_clip_data_root: Path = Path(
        "/media/heying/hy_data1/VLADatasets/clip_data",
    )
    writer_lock_path: Path | None = None
    minimum_free_bytes: int = 10 * 1024**3
    timeout_seconds: int | None = None
    version_probe: Callable[[str], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    package_probe: Callable[[tuple[str, ...]], dict[str, str]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    gpu_probe: Callable[[], bool] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_env(cls) -> "NavigationAnnotationRuntimeConfig":
        repository_root = Path(__file__).resolve().parents[3]

        def optional_path(name: str) -> Path | None:
            value = os.getenv(name)
            return Path(value) if value else None

        vla_root = optional_path("VLA_VLADATASETS_ROOT")
        free_bytes = os.getenv("VLA_ANNOTATION_MINIMUM_FREE_BYTES")
        timeout_value = os.getenv(
            "VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS",
        )
        timeout_seconds = (
            int(timeout_value)
            if timeout_value is not None
            and timeout_value.isascii()
            and timeout_value.isdigit()
            and int(timeout_value) > 0
            else None
        )
        return cls(
            runtime_source_root=optional_path(
                "VLA_NAVIGATION_ODOM_V1_SOURCE",
            ),
            work_root=optional_path("VLA_ANNOTATION_WORK_ROOT"),
            clip_data_root=(vla_root / "clip_data") if vla_root else None,
            data_python=optional_path("AGENT_DATA_PYTHON"),
            data_env_setup=optional_path("AGENT_DATA_ENV_SETUP"),
            manifest_path=Path(
                os.getenv(
                    "VLA_NAVIGATION_ODOM_V1_MANIFEST",
                    str(
                        repository_root
                        / "runtime"
                        / RUNTIME_ID
                        / "manifest.json"
                    ),
                ),
            ),
            bwrap_path=Path(os.getenv("VLA_BWRAP", "/usr/bin/bwrap")),
            xvfb_run_path=Path(
                os.getenv("VLA_XVFB_RUN", "/usr/bin/xvfb-run"),
            ),
            xvfb_path=Path(os.getenv("VLA_XVFB", "/usr/bin/Xvfb")),
            xvfb_deb_path=optional_path("VLA_XVFB_DEB"),
            runtime_dependency_summary_path=optional_path(
                "VLA_RUNTIME_DEPENDENCY_SUMMARY",
            ),
            dpkg_query_path=Path(
                os.getenv("VLA_DPKG_QUERY", "/usr/bin/dpkg-query"),
            ),
            legacy_tracking_data_root=Path(
                os.getenv(
                    "VLA_TRACKING_LEGACY_DATA_ROOT",
                    "/mnt/data1/gh/tracking_1/Data",
                ),
            ),
            legacy_clip_data_root=Path(
                os.getenv(
                    "VLA_LEGACY_CLIP_DATA_ROOT",
                    "/media/heying/hy_data1/VLADatasets/clip_data",
                ),
            ),
            writer_lock_path=optional_path(
                "VLA_NAVIGATION_WRITER_LOCK_PATH",
            ),
            minimum_free_bytes=(
                int(free_bytes) if free_bytes is not None else 10 * 1024**3
            ),
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class CalibrationSnapshotFile:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PreparationRequest:
    job_ref: str
    run_ref: str
    attempt: int
    dataset_date: str
    source_clips: tuple[str, ...]
    calibration_snapshot_dir: Path
    calibration_snapshot_files: tuple[CalibrationSnapshotFile, ...]
    calibration_snapshot_sha256: str
    active_reserved_bytes: int = 0
    step_observer: RuntimeStepObserver | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class PreparedSegment:
    source_clip: str
    private_segment_key: str
    segment_root: Path
    first_frame_path: Path
    width: int
    height: int
    sha256: str
    etag: str


@dataclass(frozen=True)
class PreparedJob:
    job_ref: str
    staging_root: Path
    staging_ref: str
    segments: tuple[PreparedSegment, ...]
    runtime_manifest_sha256: str
    input_tree_sha256: str
    calibration_snapshot_sha256: str
    prepared_artifact_tree_sha256: str
    command_steps: tuple[str, ...] = PREPARATION_COMMAND_STEPS


@dataclass(frozen=True)
class TrackingTarget:
    segment_root: Path
    yaml_path: Path
    identity: str


@dataclass(frozen=True)
class TrackingInputValidationRequest:
    job_ref: str
    staging_root: Path
    targets: tuple[TrackingTarget, ...]
    expected_runtime_manifest_sha256: str
    expected_prepared_artifact_tree_sha256: str


@dataclass(frozen=True)
class TrackingInputValidation:
    runtime_manifest_sha256: str
    prepared_artifact_tree_sha256: str


@dataclass(frozen=True)
class TrackingRequest:
    job_ref: str
    run_ref: str
    attempt: int
    staging_root: Path
    targets: tuple[TrackingTarget, ...]
    expected_runtime_manifest_sha256: str
    expected_prepared_artifact_tree_sha256: str
    estimated_input_bytes: int = 0
    active_reserved_bytes: int = 0


@dataclass(frozen=True)
class TrackingCheckpoint:
    segment_root: Path
    identity: str
    output_dir: Path
    points_path: Path
    artifact_sha256: str


@dataclass(frozen=True)
class TrackingResult:
    checkpoints: tuple[TrackingCheckpoint, ...]
    runtime_manifest_sha256: str
    command_steps: tuple[str, ...] = (TRACKING_COMMAND_STEP,)


@dataclass(frozen=True)
class CheckpointVerificationRequest:
    job_ref: str
    staging_root: Path
    segment_root: Path
    identity: str
    artifact_sha256: str


@dataclass(frozen=True)
class CapacityEstimate:
    estimated_input_bytes: int
    required_bytes: int
    free_bytes: int
    available: bool


def tracking_target_sort_key(target: TrackingTarget) -> tuple[str, str]:
    """Frozen run_odom order: segment shell-glob, then sorted YAML name."""

    return (target.segment_root.as_posix(), target.yaml_path.name)


def prepared_staging_artifact_sha256(
    staging_root: Path,
    targets: Sequence[TrackingTarget],
) -> str:
    """Fingerprint prepare-owned artifacts while excluding exact Tracking outputs."""

    try:
        staging_metadata = staging_root.lstat()
        resolved_staging = staging_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeExecutionError(
            "runtime_path_unavailable",
            "The prepared staging root is unavailable.",
        ) from exc
    if (
        stat.S_ISLNK(staging_metadata.st_mode)
        or not stat.S_ISDIR(staging_metadata.st_mode)
    ):
        raise RuntimeExecutionError(
            "unsafe_runtime_path",
            "The prepared staging root is unsafe.",
        )
    if not targets:
        raise RuntimeExecutionError(
            "no_tracking_targets",
            "At least one submitted target is required.",
        )

    excluded_files: set[str] = set()
    excluded_prefixes: set[str] = {".runtime/runs"}
    identities: set[tuple[str, str]] = set()
    for target in targets:
        try:
            segment_metadata = target.segment_root.lstat()
            segment_root = target.segment_root.resolve(strict=True)
            segment_relative = segment_root.relative_to(
                resolved_staging,
            ).as_posix()
            yaml_parent = target.yaml_path.parent.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise RuntimeExecutionError(
                "unsafe_runtime_path",
                "A Tracking target escapes its prepared staging root.",
            ) from exc
        if (
            stat.S_ISLNK(segment_metadata.st_mode)
            or not stat.S_ISDIR(segment_metadata.st_mode)
            or yaml_parent != segment_root
        ):
            raise RuntimeExecutionError(
                "unsafe_runtime_path",
                "A Tracking target path is unsafe.",
            )
        if (
            not _IDENTITY_RE.fullmatch(target.identity)
            or target.yaml_path.name != f"{target.identity}.yaml"
        ):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "A Tracking target identity is invalid.",
            )
        identity_key = (segment_relative, target.identity)
        if identity_key in identities:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "A Tracking target identity is duplicated.",
            )
        identities.add(identity_key)
        excluded_files.add(
            f"{segment_relative}/{target.yaml_path.name}",
        )
        excluded_files.add(
            f"{segment_relative}/img_{target.identity}.txt",
        )
        excluded_prefixes.add(
            f"{segment_relative}/tracking_img_{target.identity}",
        )

    return _tree_sha256(
        resolved_staging,
        unsafe_code="prepared_staging_changed",
        excluded_relative_paths=frozenset(excluded_files),
        excluded_relative_prefixes=frozenset(excluded_prefixes),
    )


def tracking_checkpoint_artifact_sha256(
    output_dir: Path,
    points_path: Path,
) -> str:
    """Recompute the exact hash committed for one Tracking target."""

    if not _regular_directory(output_dir) or not _regular_file(points_path):
        raise RuntimeExecutionError(
            "unsafe_tracking_output",
            "A Tracking checkpoint artifact is unavailable or unsafe.",
        )
    return hashlib.sha256(
        (
            _tree_sha256(output_dir)
            + ":"
            + _sha256_file(points_path)
        ).encode("ascii"),
    ).hexdigest()


def regular_artifact_sha256(path: Path) -> str:
    """Hash a stable regular file without following a final symlink."""

    if not _regular_file(path):
        raise RuntimeExecutionError(
            "unsafe_tracking_output",
            "A committed Runtime artifact is unavailable or unsafe.",
        )
    return _sha256_file(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("hash target is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        finished = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if not _same_entry(opened, finished) or not _same_entry(
            opened,
            current,
        ):
            raise OSError("hash target changed while reading")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _tree_sha256(
    root: Path,
    *,
    unsafe_code: str = "unsafe_tracking_output",
    excluded_relative_paths: frozenset[str] = frozenset(),
    excluded_relative_prefixes: frozenset[str] = frozenset(),
) -> str:
    descriptor: int | None = None
    try:
        descriptor = _open_directory_descriptor(root)
        entries = _fingerprint_directory_entries(
            descriptor,
            prefix="",
            unsafe_code=unsafe_code,
            excluded_relative_paths=excluded_relative_paths,
            excluded_relative_prefixes=excluded_relative_prefixes,
        )
    except RuntimeExecutionError:
        raise
    except OSError as exc:
        raise RuntimeExecutionError(
            unsafe_code,
            "An artifact tree could not be fingerprinted safely.",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return hashlib.sha256(
        json.dumps(
            entries,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()


def _regular_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _regular_file(path: Path, *, executable: bool = False) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return False
    return not executable or bool(metadata.st_mode & 0o111)


def _safe_configured_writer_lock_path(path: Path) -> bool:
    try:
        validate_writer_lock_path(path)
    except NavigationWriterLockError:
        return False
    return True


def _safe_descendant_directory(
    root: Path,
    *components: str,
) -> Path:
    if not _regular_directory(root):
        raise RuntimeExecutionError(
            "unsafe_runtime_input",
            "The synchronized data root is not a real directory.",
        )
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeExecutionError(
            "unsafe_runtime_input",
            "The synchronized data root cannot be resolved.",
        ) from exc
    current = root
    for component in components:
        if (
            not component
            or component in {".", ".."}
            or "/" in component
            or "\\" in component
        ):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input path component is unsafe.",
            )
        current = current / component
        if not current.exists() and not current.is_symlink():
            raise RuntimeExecutionError(
                "missing_sync_data",
                "A selected clip does not contain synchronized data.",
            )
        if not _regular_directory(current):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input path contains a symlink or non-directory.",
            )
        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input path cannot be resolved.",
            ) from exc
        if not resolved.is_relative_to(resolved_root):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input path escapes clip_data.",
            )
    return current


def _safe_component(value: str, *, label: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value):
        raise RuntimeExecutionError(
            "invalid_runtime_request",
            f"{label} is not a normalized identifier.",
        )
    return value


def _open_directory_descriptor(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise OSError("not a directory")
    return descriptor


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _ensure_private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeExecutionError(
            "unsafe_runtime_path",
            "A private Runtime directory is unavailable.",
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise RuntimeExecutionError(
            "unsafe_runtime_path",
            "A private Runtime path is not a real directory.",
        )
    path.chmod(0o700, follow_symlinks=False)


def _ensure_private_directory_chain(
    root: Path,
    components: Sequence[str],
) -> Path:
    _ensure_private_directory(root)
    current = root
    for component in components:
        if (
            not component
            or component in {".", ".."}
            or "/" in component
            or "\\" in component
        ):
            raise RuntimeExecutionError(
                "unsafe_runtime_path",
                "A private Runtime path component is unsafe.",
            )
        current = current / component
        _ensure_private_directory(current, create=True)
    return current


def _harden_private_tree(root: Path) -> None:
    _ensure_private_directory(root)
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeExecutionError(
                "unsafe_runtime_output",
                "A private Runtime tree contains a symlink.",
            )
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o700, follow_symlinks=False)
        elif stat.S_ISREG(metadata.st_mode):
            path.chmod(0o600, follow_symlinks=False)
        else:
            raise RuntimeExecutionError(
                "unsafe_runtime_output",
                "A private Runtime tree contains a special filesystem entry.",
            )


def _fingerprint_path_is_excluded(
    relative_path: str,
    *,
    excluded_relative_paths: frozenset[str],
    excluded_relative_prefixes: frozenset[str],
) -> bool:
    return (
        relative_path in excluded_relative_paths
        or any(
            relative_path == prefix
            or relative_path.startswith(f"{prefix}/")
            for prefix in excluded_relative_prefixes
        )
    )


def _fingerprint_directory_entries(
    directory: int,
    *,
    prefix: str,
    unsafe_code: str,
    excluded_relative_paths: frozenset[str] = frozenset(),
    excluded_relative_prefixes: frozenset[str] = frozenset(),
) -> list[dict[str, str | int]]:
    initial_names = sorted(
        name
        for name in os.listdir(directory)
        if not name.startswith("._")
    )
    entries: list[dict[str, str | int]] = []
    for name in initial_names:
        relative_path = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(
            name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeExecutionError(
                unsafe_code,
                "An artifact tree contains a symlink.",
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if stat.S_ISDIR(metadata.st_mode):
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            child = os.open(name, flags, dir_fd=directory)
            try:
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode) or not _same_entry(
                    metadata,
                    opened,
                ):
                    raise RuntimeExecutionError(
                        "runtime_input_changed",
                        "An artifact directory changed while fingerprinting.",
                    )
                if not _fingerprint_path_is_excluded(
                    relative_path,
                    excluded_relative_paths=excluded_relative_paths,
                    excluded_relative_prefixes=excluded_relative_prefixes,
                ):
                    entries.append(
                        {
                            "relative_path": relative_path,
                            "kind": "directory",
                        },
                    )
                entries.extend(
                    _fingerprint_directory_entries(
                        child,
                        prefix=relative_path,
                        unsafe_code=unsafe_code,
                        excluded_relative_paths=excluded_relative_paths,
                        excluded_relative_prefixes=excluded_relative_prefixes,
                    ),
                )
                current = os.stat(
                    name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
                if not _same_entry(opened, current):
                    raise RuntimeExecutionError(
                        "runtime_input_changed",
                        "An artifact directory changed while fingerprinting.",
                    )
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            descriptor = os.open(name, flags, dir_fd=directory)
            try:
                opened = os.fstat(descriptor)
                if not _same_entry(metadata, opened):
                    raise RuntimeExecutionError(
                        "runtime_input_changed",
                        "An artifact changed while fingerprinting.",
                    )
                digest = hashlib.sha256()
                with os.fdopen(
                    descriptor,
                    "rb",
                    closefd=False,
                ) as stream:
                    for chunk in iter(
                        lambda: stream.read(1024 * 1024),
                        b"",
                    ):
                        digest.update(chunk)
                finished = os.fstat(descriptor)
                current = os.stat(
                    name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
                if not _same_entry(opened, finished) or not _same_entry(
                    opened,
                    current,
                ):
                    raise RuntimeExecutionError(
                        "runtime_input_changed",
                        "An artifact changed while fingerprinting.",
                    )
                if not _fingerprint_path_is_excluded(
                    relative_path,
                    excluded_relative_paths=excluded_relative_paths,
                    excluded_relative_prefixes=excluded_relative_prefixes,
                ):
                    entries.append(
                        {
                            "relative_path": relative_path,
                            "kind": "file",
                            "size": opened.st_size,
                            "sha256": digest.hexdigest(),
                        },
                    )
            finally:
                os.close(descriptor)
        else:
            raise RuntimeExecutionError(
                unsafe_code,
                "An artifact tree contains a special filesystem entry.",
            )
    final_names = sorted(
        name
        for name in os.listdir(directory)
        if not name.startswith("._")
    )
    if final_names != initial_names:
        raise RuntimeExecutionError(
            "runtime_input_changed",
            "An artifact directory changed while fingerprinting.",
        )
    return entries


def _copy_regular_entry(
    *,
    source_directory: int,
    destination_directory: int,
    name: str,
    expected: os.stat_result,
    destination_name: str | None = None,
) -> None:
    target_name = destination_name or name
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        write_flags |= os.O_NOFOLLOW
    source_descriptor = os.open(
        name,
        read_flags,
        dir_fd=source_directory,
    )
    destination_descriptor: int | None = None
    created = False
    try:
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_entry(
            expected,
            opened,
        ):
            raise RuntimeExecutionError(
                "runtime_input_changed",
                "A runtime input changed while it was being copied.",
            )
        destination_descriptor = os.open(
            target_name,
            write_flags,
            0o600,
            dir_fd=destination_directory,
        )
        created = True
        with os.fdopen(
            source_descriptor,
            "rb",
            closefd=False,
        ) as source_stream, os.fdopen(
            destination_descriptor,
            "wb",
            closefd=False,
        ) as destination_stream:
            while chunk := source_stream.read(1024 * 1024):
                cancellation = current_cancellation()
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        finished = os.fstat(source_descriptor)
        current = os.stat(
            name,
            dir_fd=source_directory,
            follow_symlinks=False,
        )
        if not _same_entry(opened, finished) or not _same_entry(
            opened,
            current,
        ):
            raise RuntimeExecutionError(
                "runtime_input_changed",
                "A runtime input changed while it was being copied.",
            )
        os.fchmod(destination_descriptor, 0o600)
        os.utime(
            target_name,
            ns=(opened.st_atime_ns, opened.st_mtime_ns),
            dir_fd=destination_directory,
            follow_symlinks=False,
        )
    except Exception:
        if created:
            try:
                os.unlink(target_name, dir_fd=destination_directory)
            except OSError:
                pass
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _copy_directory_entries(
    *,
    source_directory: int,
    destination_directory: int,
) -> None:
    initial_names = sorted(
        name
        for name in os.listdir(source_directory)
        if not name.startswith("._")
    )
    for name in initial_names:
        cancellation = current_cancellation()
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        metadata = os.stat(
            name,
            dir_fd=source_directory,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input contains a symlink.",
            )
        if stat.S_ISDIR(metadata.st_mode):
            read_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                read_flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            child_source = os.open(
                name,
                read_flags,
                dir_fd=source_directory,
            )
            child_destination: int | None = None
            try:
                opened = os.fstat(child_source)
                if not stat.S_ISDIR(opened.st_mode) or not _same_entry(
                    metadata,
                    opened,
                ):
                    raise RuntimeExecutionError(
                        "runtime_input_changed",
                        "A runtime input directory changed while copying.",
                    )
                os.mkdir(
                    name,
                    0o700,
                    dir_fd=destination_directory,
                )
                child_destination = os.open(
                    name,
                    read_flags,
                    dir_fd=destination_directory,
                )
                _copy_directory_entries(
                    source_directory=child_source,
                    destination_directory=child_destination,
                )
                os.fchmod(child_destination, 0o700)
                current = os.stat(
                    name,
                    dir_fd=source_directory,
                    follow_symlinks=False,
                )
                if not _same_entry(opened, current):
                    raise RuntimeExecutionError(
                        "runtime_input_changed",
                        "A runtime input directory changed while copying.",
                    )
            finally:
                if child_destination is not None:
                    os.close(child_destination)
                os.close(child_source)
        elif stat.S_ISREG(metadata.st_mode):
            _copy_regular_entry(
                source_directory=source_directory,
                destination_directory=destination_directory,
                name=name,
                expected=metadata,
            )
        else:
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input contains a special filesystem entry.",
            )
    final_names = sorted(
        name
        for name in os.listdir(source_directory)
        if not name.startswith("._")
    )
    if final_names != initial_names:
        raise RuntimeExecutionError(
            "runtime_input_changed",
            "A runtime input directory changed while copying.",
        )


def _copy_tree_bytes(source: Path, destination: Path) -> None:
    if not _regular_directory(source):
        raise RuntimeExecutionError(
            "missing_runtime_input",
            "A required synchronized input directory is unavailable.",
        )
    if not _regular_directory(destination.parent):
        raise RuntimeExecutionError(
            "unsafe_runtime_path",
            "A Runtime copy destination parent is unavailable.",
        )
    destination.mkdir(mode=0o700, exist_ok=False)
    destination.chmod(0o700, follow_symlinks=False)
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    try:
        source_descriptor = _open_directory_descriptor(source)
        destination_descriptor = _open_directory_descriptor(destination)
        _copy_directory_entries(
            source_directory=source_descriptor,
            destination_directory=destination_descriptor,
        )
    except RuntimeExecutionError:
        raise
    except OSError as exc:
        raise RuntimeExecutionError(
            "unsafe_runtime_input",
            "A runtime input could not be copied safely.",
        ) from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
    source_hash = _tree_sha256(
        source,
        unsafe_code="unsafe_runtime_input",
    )
    destination_hash = _tree_sha256(
        destination,
        unsafe_code="unsafe_runtime_output",
    )
    if source_hash != destination_hash:
        raise RuntimeExecutionError(
            "runtime_input_changed",
            "A runtime input tree changed during byte-copy.",
        )


def _tree_size_regular(root: Path) -> int:
    if not _regular_directory(root):
        raise RuntimeExecutionError(
            "missing_runtime_input",
            "A required synchronized input directory is unavailable.",
        )
    total = 0
    for path in root.rglob("*"):
        cancellation = current_cancellation()
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if path.name.startswith("._"):
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input contains a symlink.",
            )
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
        elif not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A synchronized input contains a special filesystem entry.",
            )
    return total


def _copy_file_bytes(source: Path, destination: Path) -> None:
    if not _regular_file(source):
        raise RuntimeExecutionError(
            "missing_runtime_input",
            "A required runtime input file is unavailable.",
        )
    if not _regular_directory(destination.parent):
        raise RuntimeExecutionError(
            "unsafe_runtime_path",
            "A Runtime copy destination parent is unavailable.",
        )
    source_parent: int | None = None
    destination_parent: int | None = None
    try:
        source_parent = _open_directory_descriptor(source.parent)
        destination_parent = _open_directory_descriptor(destination.parent)
        metadata = os.stat(
            source.name,
            dir_fd=source_parent,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeExecutionError(
                "unsafe_runtime_input",
                "A required runtime input is not a regular file.",
            )
        _copy_regular_entry(
            source_directory=source_parent,
            destination_directory=destination_parent,
            name=source.name,
            expected=metadata,
            destination_name=destination.name,
        )
    except RuntimeExecutionError:
        raise
    except OSError as exc:
        raise RuntimeExecutionError(
            "unsafe_runtime_input",
            "A runtime input could not be copied safely.",
        ) from exc
    finally:
        if destination_parent is not None:
            os.close(destination_parent)
        if source_parent is not None:
            os.close(source_parent)


def _read_calibration_snapshot(
    *,
    config: NavigationAnnotationRuntimeConfig,
    request: PreparationRequest,
) -> dict[str, bytes]:
    assert config.work_root is not None
    if not re.fullmatch(
        r"^[0-9a-f]{64}$",
        request.calibration_snapshot_sha256,
    ):
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The calibration snapshot ledger hash is invalid.",
        )
    expected_names = tuple(sorted(_REQUIRED_SENSOR_FILES))
    files = request.calibration_snapshot_files
    if (
        tuple(item.relative_path for item in files) != expected_names
        or len({item.relative_path for item in files}) != len(files)
    ):
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The calibration snapshot ledger does not contain the exact "
            "required sensor files.",
        )
    for item in files:
        if (
            Path(item.relative_path).name != item.relative_path
            or not isinstance(item.size, int)
            or isinstance(item.size, bool)
            or item.size < 0
            or not re.fullmatch(r"^[0-9a-f]{64}$", item.sha256)
        ):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "The calibration snapshot ledger contains an invalid file.",
            )
    try:
        expected_root = _safe_descendant_directory(
            config.work_root,
            "jobs",
            request.job_ref,
            "calibration",
        )
        if (
            request.calibration_snapshot_dir.absolute()
            != expected_root.absolute()
            or request.calibration_snapshot_dir.resolve(strict=True)
            != expected_root.resolve(strict=True)
        ):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "The calibration snapshot is not owned by this job.",
            )
    except RuntimeExecutionError as exc:
        if exc.code == "calibration_snapshot_mismatch":
            raise
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The calibration snapshot path is unsafe.",
        ) from exc
    except OSError as exc:
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The calibration snapshot is unavailable.",
        ) from exc

    directory_descriptor: int | None = None
    try:
        directory_descriptor = _open_directory_descriptor(expected_root)
        actual_names = sorted(os.listdir(directory_descriptor))
        if actual_names != list(expected_names):
            raise RuntimeExecutionError(
                "calibration_snapshot_mismatch",
                "The calibration snapshot directory differs from its ledger.",
            )
        contents: dict[str, bytes] = {}
        for item in files:
            metadata = os.stat(
                item.relative_path,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(
                item.relative_path,
                flags,
                dir_fd=directory_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not _same_entry(metadata, opened)
                    or opened.st_size != item.size
                ):
                    raise RuntimeExecutionError(
                        "calibration_snapshot_mismatch",
                        "A calibration snapshot file differs from its ledger.",
                    )
                with os.fdopen(
                    descriptor,
                    "rb",
                    closefd=False,
                ) as stream:
                    content = stream.read()
                finished = os.fstat(descriptor)
                current = os.stat(
                    item.relative_path,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not _same_entry(opened, finished)
                    or not _same_entry(opened, current)
                    or hashlib.sha256(content).hexdigest() != item.sha256
                ):
                    raise RuntimeExecutionError(
                        "calibration_snapshot_mismatch",
                        "A calibration snapshot file differs from its ledger.",
                    )
                contents[item.relative_path] = content
            finally:
                os.close(descriptor)
    except RuntimeExecutionError:
        raise
    except OSError as exc:
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The calibration snapshot could not be verified safely.",
        ) from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    ledger = [
        {
            "relative_path": item.relative_path,
            "sha256": item.sha256,
            "size": item.size,
        }
        for item in files
    ]
    aggregate = hashlib.sha256(
        json.dumps(
            ledger,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    if aggregate != request.calibration_snapshot_sha256:
        raise RuntimeExecutionError(
            "calibration_snapshot_mismatch",
            "The calibration snapshot aggregate differs from its ledger.",
        )
    return contents


def _write_bytes_exclusive(destination: Path, content: bytes) -> None:
    if not _regular_directory(destination.parent):
        raise RuntimeExecutionError(
            "unsafe_runtime_path",
            "A private Runtime output parent is unavailable.",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _discover_supported_sequences(
    *,
    config: NavigationAnnotationRuntimeConfig,
    dataset_date: str,
    source_clips: Sequence[str],
) -> tuple[list[tuple[str, Path]], dict[str, str], int]:
    assert config.clip_data_root is not None
    planned_sequences: list[tuple[str, Path]] = []
    source_to_segments: dict[str, str] = {}
    seen_segments: set[str] = set()
    estimated_input_bytes = 0
    for source_clip in source_clips:
        sync_root = _safe_descendant_directory(
            config.clip_data_root,
            dataset_date,
            source_clip,
            "sync_data",
        )
        for sequence in sorted(sync_root.iterdir(), key=lambda path: path.name):
            if sequence.name.startswith("._"):
                continue
            if not _regular_directory(sequence):
                raise RuntimeExecutionError(
                    "unsafe_runtime_input",
                    "sync_data contains an unexpected non-directory entry.",
                )
            if not sequence.name.startswith(("2025", "2026")):
                raise RuntimeExecutionError(
                    "unsupported_runtime_variant",
                    "The frozen M1 Runtime only supports legacy 2025/2026 "
                    "internal segment naming.",
                )
            if sequence.name in seen_segments:
                raise RuntimeExecutionError(
                    "duplicate_internal_segment",
                    "Selected source clips contain the same internal segment identity.",
                )
            seen_segments.add(sequence.name)
            for modality in (
                "fisheye_front",
                "r32_rslidar_points",
                "odom",
            ):
                modality_root = sequence / modality
                if not _regular_directory(modality_root) or not any(
                    child.is_file()
                    for child in modality_root.iterdir()
                    if not child.name.startswith("._")
                ):
                    code = (
                        "unsupported_runtime_variant"
                        if modality == "odom"
                        else "missing_sync_modality"
                    )
                    raise RuntimeExecutionError(
                        code,
                        "M1 requires non-empty odom, image, and lidar synchronized inputs.",
                    )
            planned_sequences.append((source_clip, sequence))
            source_to_segments[sequence.name] = source_clip
            estimated_input_bytes += sum(
                _tree_size_regular(sequence / modality)
                for modality in (
                    "fisheye_front",
                    "r32_rslidar_points",
                    "odom",
                )
            )
    if not source_to_segments:
        raise RuntimeExecutionError(
            "missing_sync_segments",
            "No supported internal segments were found.",
        )
    return planned_sequences, source_to_segments, estimated_input_bytes


class _RuntimeBase:
    def __init__(
        self,
        config: NavigationAnnotationRuntimeConfig | None = None,
    ) -> None:
        self.config = config or NavigationAnnotationRuntimeConfig.from_env()

    def _version(self, package: str) -> str:
        if self.config.version_probe is not None:
            return self.config.version_probe(package).strip()
        result = subprocess.run(
            [
                str(self.config.dpkg_query_path),
                "-W",
                "-f=${Version}",
                package,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _python_package_versions(
        self,
        distributions: tuple[str, ...],
    ) -> dict[str, str]:
        if self.config.package_probe is not None:
            return self.config.package_probe(distributions)
        assert self.config.data_env_setup is not None
        assert self.config.data_python is not None
        code = (
            "import importlib.metadata as m,json,sys;"
            "print(json.dumps({name:m.version(name) for name in sys.argv[1:]},"
            "sort_keys=True))"
        )
        shell = (
            f"source {shlex.quote(str(self.config.data_env_setup))}"
            " && exec "
            f"{shlex.quote(str(self.config.data_python))} -c "
            f"{shlex.quote(code)} "
            + " ".join(shlex.quote(name) for name in distributions)
        )
        result = subprocess.run(
            ["/bin/bash", "-lc", shell],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("data Python dependency probe failed")
        payload = json.loads(result.stdout)
        if (
            not isinstance(payload, dict)
            or set(payload) != set(distributions)
            or any(not isinstance(value, str) for value in payload.values())
        ):
            raise RuntimeError("data Python dependency probe was malformed")
        return payload

    def _gpu_available(self) -> bool:
        if self.config.gpu_probe is not None:
            return bool(self.config.gpu_probe())
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return False
        try:
            result = subprocess.run(
                [executable, "-L"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    @staticmethod
    def _external_file_matches(path: Path, entry: dict[str, object]) -> bool:
        if not _regular_file(
            path,
            executable=bool(entry.get("executable")),
        ):
            return False
        metadata = path.stat(follow_symlinks=False)
        return (
            metadata.st_size == entry.get("size")
            and _sha256_file(path) == entry.get("sha256")
            and bool(metadata.st_mode & 0o111)
            == bool(entry.get("executable"))
        )

    def capabilities(self) -> RuntimeCapabilities:
        config = self.config

        def unavailable(code: str, message: str) -> RuntimeCapabilities:
            return RuntimeCapabilities(
                available=False,
                reason=RuntimeCapabilityReason(code=code, message=message),
            )

        required_config = {
            "runtime source": config.runtime_source_root,
            "annotation work root": config.work_root,
            "clip data root": config.clip_data_root,
            "data Python": config.data_python,
            "data environment": config.data_env_setup,
        }
        if any(path is None for path in required_config.values()):
            return unavailable(
                "runtime_not_configured",
                "The navigation annotation runtime is not fully configured.",
            )
        assert config.runtime_source_root is not None
        assert config.work_root is not None
        assert config.clip_data_root is not None
        assert config.data_python is not None
        assert config.data_env_setup is not None

        if (
            config.timeout_seconds is None
            or isinstance(config.timeout_seconds, bool)
            or not isinstance(config.timeout_seconds, int)
            or config.timeout_seconds <= 0
        ):
            return unavailable(
                "runtime_timeout_not_configured",
                "A positive annotation Runtime command timeout is required.",
            )
        if config.writer_lock_path is None:
            return unavailable(
                "writer_lock_not_configured",
                "A dedicated navigation writer lock path is required.",
            )
        if not _safe_configured_writer_lock_path(config.writer_lock_path):
            return unavailable(
                "writer_lock_path_unsafe",
                "The dedicated navigation writer lock path is unsafe.",
            )
        try:
            if (
                navigation_writer_coordination_status(config.writer_lock_path)
                == "quarantined"
            ):
                return unavailable(
                    "runtime_coordination_unavailable",
                    "Navigation writer coordination requires an operator safety check.",
                )
        except NavigationWriterLockError:
            return unavailable(
                "runtime_coordination_unavailable",
                "Navigation writer coordination requires an operator safety check.",
            )
        if not _regular_directory(config.work_root) or not os.access(
            config.work_root,
            os.W_OK | os.X_OK,
        ):
            return unavailable(
                "work_root_unavailable",
                "The dedicated annotation work root is unavailable.",
            )
        if not _regular_directory(config.clip_data_root):
            return unavailable(
                "clip_data_unavailable",
                "The synchronized data root is unavailable.",
            )
        if not _regular_file(config.data_python, executable=True):
            return unavailable(
                "data_python_unavailable",
                "The frozen data Python interpreter is unavailable.",
            )
        if not _regular_file(config.data_env_setup, executable=True):
            return unavailable(
                "data_environment_unavailable",
                "The frozen data environment setup is unavailable.",
            )
        try:
            manifest = load_manifest(config.manifest_path)
            if manifest["runtime_id"] != RUNTIME_ID:
                return unavailable(
                    "runtime_manifest_mismatch",
                    "The configured manifest has the wrong runtime identity.",
                )
            mismatches, runtime_errors = verify_root(
                manifest,
                root_alias="NAVIGATION_ODOM_V1_SOURCE",
                root=config.runtime_source_root,
            )
        except Exception:
            return unavailable(
                "runtime_manifest_unavailable",
                "The frozen Runtime manifest cannot be verified.",
            )
        if mismatches or runtime_errors:
            return unavailable(
                "runtime_payload_mismatch",
                "The deployed Runtime payload does not match its manifest.",
            )
        external_by_role = {
            str(entry["role"]): entry
            for entry in manifest["entries"]
            if entry["kind"] == "external_runtime"
        }
        installation_roles = {
            "xvfb_deb_package": config.xvfb_deb_path,
            "xvfb_server_binary": config.xvfb_path,
            "xvfb_launcher": config.xvfb_run_path,
            "sandbox_binary": config.bwrap_path,
            "runtime_dependency_summary": (
                config.runtime_dependency_summary_path
            ),
        }
        if any(path is None for path in installation_roles.values()):
            return unavailable(
                "runtime_installation_attestation_not_configured",
                "The frozen Runtime installation evidence is not configured.",
            )
        if any(
            role not in external_by_role
            for role in installation_roles
        ):
            return unavailable(
                "runtime_installation_manifest_incomplete",
                "The Runtime manifest lacks required installation evidence.",
            )
        if any(
            not self._external_file_matches(
                path,
                external_by_role[role],
            )
            for role, path in installation_roles.items()
            if path is not None
        ):
            return unavailable(
                "runtime_installation_mismatch",
                "The installed Xvfb or sandbox Runtime differs from its "
                "manifest.",
            )
        xvfb_deb_entry = external_by_role["xvfb_deb_package"]
        if xvfb_deb_entry.get("version") != config.expected_xvfb_version:
            return unavailable(
                "runtime_installation_manifest_incomplete",
                "The Runtime manifest does not attest the frozen Xvfb version.",
            )
        try:
            xvfb_version = self._version("xvfb")
        except (OSError, subprocess.SubprocessError):
            xvfb_version = ""
        if xvfb_version != config.expected_xvfb_version:
            return unavailable(
                "xvfb_version_mismatch",
                "The installed Xvfb package does not match the frozen version.",
            )
        setup_entry = external_by_role.get("active_environment_setup")
        python_entry = external_by_role.get("active_python_interpreter")
        if (
            setup_entry is None
            or python_entry is None
            or not self._external_file_matches(
                config.data_env_setup,
                setup_entry,
            )
            or not self._external_file_matches(
                config.data_python,
                python_entry,
            )
        ):
            return unavailable(
                "data_runtime_mismatch",
                "The data environment setup or Python interpreter differs from the manifest.",
            )
        package_entries = {
            str(entry["relative_path"]).rsplit("/", 1)[-1]: str(
                entry["version"],
            )
            for entry in manifest["entries"]
            if entry["kind"] == "external_runtime"
            and entry["role"] in {
                "python_package",
                "python_package_direct_dependency",
            }
            and entry["stage"] in {"all", "metadata", "tracking"}
        }
        try:
            actual_packages = self._python_package_versions(
                tuple(sorted(package_entries)),
            )
        except Exception:
            return unavailable(
                "data_dependency_probe_failed",
                "The frozen data Python dependency set cannot be verified.",
            )
        if actual_packages != {
            key: package_entries[key]
            for key in sorted(package_entries)
        }:
            return unavailable(
                "data_dependency_mismatch",
                "The data Python dependencies differ from the manifest.",
            )
        if not self._gpu_available():
            return unavailable(
                "gpu_runtime_unavailable",
                "The Tracking GPU runtime is unavailable.",
            )
        for required_directory in (
            config.runtime_source_root / "NoobScenes" / "samples",
            config.runtime_source_root / "NoobScenes" / "v1.0-develop",
            config.runtime_source_root / "Data",
            config.legacy_tracking_data_root,
            config.legacy_clip_data_root,
        ):
            if not _regular_directory(required_directory):
                return unavailable(
                    "sandbox_target_unavailable",
                    "A required sandbox overlay target is unavailable.",
                )
        return RuntimeCapabilities(available=True)

    def _require_available(self) -> None:
        capability = self.capabilities()
        if not capability.available:
            assert capability.reason is not None
            raise RuntimeUnavailableError(
                capability.reason.code,
                capability.reason.message,
            )

    def _assert_under(self, path: Path, root: Path, *, label: str) -> Path:
        try:
            resolved_root = root.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeExecutionError(
                "runtime_path_unavailable",
                f"{label} is unavailable: {type(exc).__name__}.",
            ) from exc
        if not resolved.is_relative_to(resolved_root):
            raise RuntimeExecutionError(
                "unsafe_runtime_path",
                f"{label} escapes its private root.",
            )
        return resolved

    def _sandbox_command(
        self,
        *,
        staging_root: Path,
        argv: Sequence[str | Path],
        cwd: Path,
        writable_bindings: Sequence[tuple[Path, Path]] = (),
        readonly_bindings: Sequence[tuple[Path, Path]] = (),
    ) -> list[str]:
        config = self.config
        assert config.runtime_source_root is not None
        assert config.data_env_setup is not None
        staging = self._assert_under(
            staging_root,
            config.work_root or staging_root,
            label="job staging",
        )
        command = [
            str(config.xvfb_run_path),
            "--auto-servernum",
            "--server-args=-screen 0 1920x1536x24 -nolisten tcp -noreset",
            str(config.bwrap_path),
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(staging),
            str(staging),
        ]
        for source, target in writable_bindings:
            source_resolved = self._assert_under(
                source,
                staging,
                label="sandbox writable binding",
            )
            if not target.is_absolute():
                raise RuntimeExecutionError(
                    "unsafe_runtime_path",
                    "Sandbox target must be absolute.",
                )
            command.extend(
                ["--bind", str(source_resolved), str(target)],
            )
        for source, target in readonly_bindings:
            try:
                source_resolved = source.resolve(strict=True)
            except OSError as exc:
                raise RuntimeExecutionError(
                    "runtime_path_unavailable",
                    "Sandbox read-only binding is unavailable.",
                ) from exc
            if source.is_symlink() or not target.is_absolute():
                raise RuntimeExecutionError(
                    "unsafe_runtime_path",
                    "Sandbox read-only binding is unsafe.",
                )
            command.extend(
                ["--ro-bind", str(source_resolved), str(target)],
            )
        command.extend(["--chdir", str(cwd), *[str(item) for item in argv]])
        quoted = " ".join(shlex.quote(item) for item in command)
        return [
            "/bin/bash",
            "-lc",
            (
                "umask 077"
                f" && source {shlex.quote(str(config.data_env_setup))}"
                f" && exec {quoted}"
            ),
        ]

    def _run_checked(
        self,
        *,
        staging_root: Path,
        argv: Sequence[str | Path],
        cwd: Path,
        writable_bindings: Sequence[tuple[Path, Path]] = (),
        readonly_bindings: Sequence[tuple[Path, Path]] = (),
        error_code: str,
    ) -> None:
        try:
            record = run_command(
                self._sandbox_command(
                    staging_root=staging_root,
                    argv=argv,
                    cwd=cwd,
                    writable_bindings=writable_bindings,
                    readonly_bindings=readonly_bindings,
                ),
                timeout_seconds=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeExecutionError(
                "runtime_command_timeout",
                "A frozen navigation Runtime command timed out.",
                diagnostic_kind="timeout",
                private_detail=_private_command_failure_detail(
                    diagnostic_kind="timeout",
                    return_code=None,
                    stdout=exc.output,
                    stderr=exc.stderr,
                ),
            ) from exc
        if record.return_code != 0:
            raise RuntimeExecutionError(
                error_code,
                "A frozen navigation Runtime command failed.",
                return_code=record.return_code,
                diagnostic_kind="nonzero_exit",
                private_detail=_private_command_failure_detail(
                    diagnostic_kind="nonzero_exit",
                    return_code=record.return_code,
                    stdout=record.stdout,
                    stderr=record.stderr,
                ),
            )


class NavigationAnnotationRuntimeAdapter(_RuntimeBase):
    def preflight_capacity(
        self,
        dataset_date: str,
        source_clips: Sequence[str],
        *,
        active_reserved_bytes: int = 0,
    ) -> CapacityEstimate:
        self._require_available()
        config = self.config
        assert config.work_root is not None
        _safe_component(dataset_date, label="dataset_date", pattern=_DATE_RE)
        if (
            not source_clips
            or len(set(source_clips)) != len(source_clips)
            or active_reserved_bytes < 0
        ):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Capacity preflight requires unique clips and a non-negative reservation.",
            )
        for clip in source_clips:
            _safe_component(clip, label="source clip", pattern=_CLIP_RE)
        _planned, _mapping, estimated_input_bytes = (
            _discover_supported_sequences(
                config=config,
                dataset_date=dataset_date,
                source_clips=source_clips,
            )
        )
        required_bytes = (
            estimated_input_bytes * 3
            + config.minimum_free_bytes
            + active_reserved_bytes
        )
        free_bytes = shutil.disk_usage(config.work_root).free
        return CapacityEstimate(
            estimated_input_bytes=estimated_input_bytes,
            required_bytes=required_bytes,
            free_bytes=free_bytes,
            available=free_bytes >= required_bytes,
        )

    def prepare(self, request: PreparationRequest) -> PreparedJob:
        self._require_available()
        config = self.config
        assert config.runtime_source_root is not None
        assert config.work_root is not None
        assert config.clip_data_root is not None
        assert config.data_python is not None
        _safe_component(request.job_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(request.run_ref, label="run_ref", pattern=_OPAQUE_REF_RE)
        if request.attempt < 1:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Runtime attempt must be positive.",
            )
        _safe_component(
            request.dataset_date,
            label="dataset_date",
            pattern=_DATE_RE,
        )
        if not request.source_clips:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "At least one source clip is required.",
            )
        if len(set(request.source_clips)) != len(request.source_clips):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Source clips must be unique.",
            )
        for clip in request.source_clips:
            _safe_component(clip, label="source clip", pattern=_CLIP_RE)
        if request.active_reserved_bytes < 0:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Active capacity reservation cannot be negative.",
            )

        staging_root = (
            config.work_root
            / "jobs"
            / request.job_ref
            / "attempts"
            / request.run_ref
            / f"{request.dataset_date}_temp"
        )
        if staging_root.exists() or staging_root.is_symlink():
            raise RuntimeExecutionError(
                "recovery_required",
                "This Runtime attempt already has staging; inspect it before retry.",
            )
        # The Store ledger, not directory existence, is the authority for the
        # immutable calibration snapshot. Verify it before creating staging or
        # launching any process.
        with _observed_runtime_step(
            request.step_observer,
            "processing_calibration_snapshot",
        ):
            calibration_contents = _read_calibration_snapshot(
                config=config,
                request=request,
            )
        calibration_bytes = sum(
            len(content)
            for content in calibration_contents.values()
        )
        planned_sequences, source_to_segments, estimated_input_bytes = (
            _discover_supported_sequences(
                config=config,
                dataset_date=request.dataset_date,
                source_clips=request.source_clips,
            )
        )
        free_bytes = shutil.disk_usage(config.work_root).free
        # Input copy, resized working set/video, and Tracking scratch can
        # coexist.  Reserve three input-sized working sets plus the configured
        # fixed safety margin before writing the first byte.
        required_bytes = (
            (estimated_input_bytes + calibration_bytes) * 3
            + config.minimum_free_bytes
            + request.active_reserved_bytes
        )
        if free_bytes < required_bytes:
            raise RuntimeExecutionError(
                "insufficient_work_space",
                "The dedicated annotation work root has insufficient free space.",
            )

        _notify_runtime_step(
            request.step_observer,
            "assemble_finish_temp",
            "started",
        )
        job_root = _ensure_private_directory_chain(
            config.work_root,
            ("jobs", request.job_ref),
        )
        attempts_root = _ensure_private_directory_chain(
            job_root,
            ("attempts",),
        )
        run_root = attempts_root / request.run_ref
        if run_root.exists() or run_root.is_symlink():
            raise RuntimeExecutionError(
                "recovery_required",
                "This Runtime attempt root already exists; inspect it before retry.",
            )
        _ensure_private_directory(run_root, create=True)
        staging_root.mkdir(mode=0o700, exist_ok=False)
        staging_root.chmod(0o700, follow_symlinks=False)
        samples_date_root = _ensure_private_directory_chain(
            staging_root,
            ("samples", request.dataset_date),
        )
        private_clip_root = (
            staging_root / ".runtime" / "clip_data"
        )
        input_fingerprints: list[dict[str, str | int]] = []
        clip_ordinals = {
            clip: ordinal
            for ordinal, clip in enumerate(request.source_clips, start=1)
        }
        for segment_ordinal, (source_clip, sequence) in enumerate(
            planned_sequences,
            start=1,
        ):
            destination = samples_date_root / sequence.name
            _ensure_private_directory(destination, create=True)
            calibration_target = destination / "sensors"
            _ensure_private_directory(
                calibration_target,
                create=True,
            )
            for filename in _REQUIRED_SENSOR_FILES:
                _write_bytes_exclusive(
                    calibration_target / filename,
                    calibration_contents[filename],
                )
            for modality in ("fisheye_front", "r32_rslidar_points"):
                _copy_tree_bytes(
                    sequence / modality,
                    destination / modality,
                )
                input_fingerprints.append(
                    {
                        "clip_ordinal": clip_ordinals[source_clip],
                        "segment_ordinal": segment_ordinal,
                        "modality": modality,
                        "sha256": _tree_sha256(destination / modality),
                    },
                )
            private_odom = (
                private_clip_root
                / request.dataset_date
                / source_clip
                / "sync_data"
                / sequence.name
                / "odom"
            )
            _ensure_private_directory_chain(
                staging_root,
                (
                    ".runtime",
                    "clip_data",
                    request.dataset_date,
                    source_clip,
                    "sync_data",
                    sequence.name,
                ),
            )
            _copy_tree_bytes(sequence / "odom", private_odom)
            input_fingerprints.append(
                {
                    "clip_ordinal": clip_ordinals[source_clip],
                    "segment_ordinal": segment_ordinal,
                    "modality": "odom",
                    "sha256": _tree_sha256(private_odom),
                },
            )
        input_tree_sha256 = hashlib.sha256(
            json.dumps(
                input_fingerprints,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        _notify_runtime_step(
            request.step_observer,
            "assemble_finish_temp",
            "succeeded",
            return_code=0,
        )

        noobscene_root = config.runtime_source_root / "NoobScenes"
        overlay_root = _ensure_private_directory_chain(
            staging_root,
            (".runtime", "NoobScenes"),
        )
        overlay_samples = staging_root / "samples"
        overlay_metadata = overlay_root / "v1.0-develop"
        _ensure_private_directory(overlay_metadata, create=True)

        preprocessing_commands = (
            (
                [
                    config.data_python,
                    noobscene_root / "include" / "0_creat_box.py",
                    "--dataset_root",
                    staging_root,
                ],
                noobscene_root,
                (),
                "preprocess_create_box",
                "preprocess_create_box_failed",
            ),
            (
                [
                    config.data_python,
                    noobscene_root / "include" / "1_odom_convert.py",
                    "--temp_path",
                    staging_root,
                ],
                noobscene_root,
                (),
                "preprocess_odom_convert",
                "preprocess_odom_convert_failed",
            ),
            (
                [
                    config.data_python,
                    noobscene_root / "include" / "2_resize.py",
                    "--temp_path",
                    staging_root,
                ],
                noobscene_root,
                (),
                "preprocess_resize",
                "preprocess_resize_failed",
            ),
            (
                [config.data_python, noobscene_root / "main_smart_odom.py"],
                noobscene_root,
                (
                    (overlay_samples, noobscene_root / "samples"),
                    (
                        overlay_metadata,
                        noobscene_root / "v1.0-develop",
                    ),
                ),
                "metadata_generate",
                "metadata_generate_failed",
            ),
        )
        with navigation_writer_lock(
            lock_path=self.config.writer_lock_path,
        ):
            for index, (
                argv,
                cwd,
                bindings,
                safe_step_code,
                error_code,
            ) in enumerate(
                preprocessing_commands,
            ):
                _notify_runtime_step(
                    request.step_observer,
                    safe_step_code,
                    "started",
                )
                self._run_checked(
                    staging_root=staging_root,
                    argv=argv,
                    cwd=cwd,
                    writable_bindings=bindings,
                    readonly_bindings=(
                        (
                            (
                                private_clip_root,
                                config.legacy_clip_data_root,
                            ),
                        )
                        if index == 1
                        else ()
                    ),
                    error_code=error_code,
                )
                if safe_step_code != "metadata_generate":
                    _notify_runtime_step(
                        request.step_observer,
                        safe_step_code,
                        "succeeded",
                        return_code=0,
                    )
            trainval_path = _ensure_private_directory_chain(
                staging_root,
                ("v1.0-trainval",),
            )
            for child in sorted(
                overlay_metadata.iterdir(),
                key=lambda path: path.name,
            ):
                if child.is_file() and not child.is_symlink():
                    _copy_file_bytes(child, trainval_path / child.name)
                elif child.is_dir() and not child.is_symlink():
                    _copy_tree_bytes(child, trainval_path / child.name)
                else:
                    raise RuntimeExecutionError(
                        "unsafe_runtime_output",
                        "Metadata output contains an unsupported filesystem entry.",
                    )
            _notify_runtime_step(
                request.step_observer,
                "metadata_generate",
                "succeeded",
                return_code=0,
            )
            _notify_runtime_step(
                request.step_observer,
                "map_publish",
                "started",
            )
            maps_root = _ensure_private_directory_chain(
                staging_root,
                ("maps",),
            )
            _copy_file_bytes(
                noobscene_root / "maps" / "map.png",
                maps_root / "map.png",
            )
            _notify_runtime_step(
                request.step_observer,
                "map_publish",
                "succeeded",
                return_code=0,
            )
            _notify_runtime_step(
                request.step_observer,
                "video_prepare",
                "started",
            )
            self._run_checked(
                staging_root=staging_root,
                argv=[
                    config.data_python,
                    config.runtime_source_root
                    / "0_1th_box"
                    / "img2video.py",
                    "--dataset_root",
                    staging_root,
                ],
                cwd=config.runtime_source_root / "0_1th_box",
                error_code="video_prepare_failed",
            )

        _harden_private_tree(staging_root)
        prepared_segments: list[PreparedSegment] = []
        for segment_key in sorted(source_to_segments):
            segment_root = samples_date_root / segment_key
            image_root = segment_root / "fisheye_front"
            candidates = sorted(
                path
                for path in image_root.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in _FIRST_FRAME_SUFFIXES
            )
            if not candidates:
                raise RuntimeExecutionError(
                    "missing_first_frame",
                    "A prepared segment does not contain a supported first frame.",
                )
            first_frame = candidates[0]
            try:
                _image_format, width, height = image_dimensions(first_frame)
            except (OSError, ImageHeaderError) as exc:
                raise RuntimeExecutionError(
                    "invalid_first_frame",
                    "A prepared first frame cannot be decoded.",
                ) from exc
            digest = _sha256_file(first_frame)
            prepared_segments.append(
                PreparedSegment(
                    source_clip=source_to_segments[segment_key],
                    private_segment_key=segment_key,
                    segment_root=segment_root,
                    first_frame_path=first_frame,
                    width=width,
                    height=height,
                    sha256=digest,
                    etag=f'"sha256:{digest}"',
                ),
            )

        _notify_runtime_step(
            request.step_observer,
            "video_prepare",
            "succeeded",
            return_code=0,
        )
        return PreparedJob(
            job_ref=request.job_ref,
            staging_root=staging_root,
            staging_ref=PurePosixPath(
                "jobs",
                request.job_ref,
                "attempts",
                request.run_ref,
                f"{request.dataset_date}_temp",
            ).as_posix(),
            segments=tuple(prepared_segments),
            runtime_manifest_sha256=_sha256_file(config.manifest_path),
            input_tree_sha256=input_tree_sha256,
            calibration_snapshot_sha256=(
                request.calibration_snapshot_sha256
            ),
            prepared_artifact_tree_sha256=_tree_sha256(staging_root),
        )


class NavigationTrackingRuntime(_RuntimeBase):
    def _current_runtime_manifest_sha256(self, expected: str) -> str:
        if not re.fullmatch(r"^[0-9a-f]{64}$", expected):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "The expected Runtime manifest hash is invalid.",
            )
        try:
            actual = _sha256_file(self.config.manifest_path)
        except OSError as exc:
            raise RuntimeExecutionError(
                "runtime_manifest_changed",
                "The frozen Runtime manifest cannot be safely re-read.",
            ) from exc
        if actual != expected:
            raise RuntimeExecutionError(
                "runtime_manifest_changed",
                "The frozen Runtime changed after job preparation.",
            )
        return actual

    def validate_tracking_inputs(
        self,
        request: TrackingInputValidationRequest,
    ) -> TrackingInputValidation:
        """Re-attest prepare provenance before publishing Web-generated YAML."""

        self._require_available()
        _safe_component(request.job_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        if not re.fullmatch(
            r"^[0-9a-f]{64}$",
            request.expected_prepared_artifact_tree_sha256,
        ):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "The expected prepared artifact hash is invalid.",
            )
        assert self.config.work_root is not None
        staging_root = self._assert_under(
            request.staging_root,
            self.config.work_root,
            label="job staging",
        )
        runtime_manifest_sha256 = self._current_runtime_manifest_sha256(
            request.expected_runtime_manifest_sha256,
        )

        prepared_artifact_tree_sha256 = prepared_staging_artifact_sha256(
            staging_root,
            request.targets,
        )
        if (
            prepared_artifact_tree_sha256
            != request.expected_prepared_artifact_tree_sha256
        ):
            raise RuntimeExecutionError(
                "prepared_staging_changed",
                "Prepared Runtime artifacts changed while awaiting annotation.",
            )
        return TrackingInputValidation(
            runtime_manifest_sha256=runtime_manifest_sha256,
            prepared_artifact_tree_sha256=prepared_artifact_tree_sha256,
        )

    def track(self, request: TrackingRequest) -> TrackingResult:
        self._require_available()
        config = self.config
        assert config.runtime_source_root is not None
        _safe_component(request.job_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        _safe_component(request.run_ref, label="run_ref", pattern=_OPAQUE_REF_RE)
        if request.attempt < 1:
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Runtime attempt must be positive.",
            )
        if not re.fullmatch(
            r"^[0-9a-f]{64}$",
            request.expected_prepared_artifact_tree_sha256,
        ):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "The expected prepared artifact hash is invalid.",
            )
        runtime_manifest_sha256 = self._current_runtime_manifest_sha256(
            request.expected_runtime_manifest_sha256,
        )
        assert config.work_root is not None
        staging_root = self._assert_under(
            request.staging_root,
            config.work_root,
            label="job staging",
        )
        if (
            request.estimated_input_bytes < 0
            or request.active_reserved_bytes < 0
        ):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Tracking capacity inputs cannot be negative.",
            )
        if not request.targets:
            raise RuntimeExecutionError(
                "no_tracking_targets",
                "At least one submitted target is required.",
            )
        # The job may wait for Web annotation for hours or days, so the
        # successful creation-time reservation is not proof that Tracking can
        # still start.  Recompute immediately before creating scratch.  The
        # larger of the original synchronized input and current staged working
        # set is used to reserve two additional concurrent working sets for the
        # Tracking scratch/output peak, alongside other active job reservations
        # and the fixed safety margin.
        staged_bytes = _tree_size_regular(staging_root)
        estimated_working_set = max(
            staged_bytes,
            request.estimated_input_bytes,
        )
        required_bytes = (
            estimated_working_set * 2
            + request.active_reserved_bytes
            + config.minimum_free_bytes
        )
        if shutil.disk_usage(config.work_root).free < required_bytes:
            raise RuntimeExecutionError(
                "insufficient_work_space",
                "The dedicated annotation work root has insufficient free "
                "space to start Tracking.",
            )

        private_data = (
            staging_root / ".runtime" / "runs" / request.run_ref / "Data"
        )
        param_dir = private_data / "3_param"
        output_root = private_data / "1_img_output"
        _ensure_private_directory_chain(
            staging_root,
            (
                ".runtime",
                "runs",
                request.run_ref,
                "Data",
                "3_param",
            ),
        )
        _ensure_private_directory(output_root, create=True)
        for filename in ("ost.yaml", "camera_extrinsics.yaml"):
            _copy_file_bytes(
                config.runtime_source_root / "Data" / "3_param" / filename,
                param_dir / filename,
            )

        checkpoints: list[TrackingCheckpoint] = []
        tracking_root = config.runtime_source_root / "1_onnx_tam"
        with navigation_writer_lock(
            lock_path=self.config.writer_lock_path,
        ):
            for target in sorted(
                request.targets,
                key=tracking_target_sort_key,
            ):
                runtime_manifest_sha256 = (
                    self._current_runtime_manifest_sha256(
                        request.expected_runtime_manifest_sha256,
                    )
                )
                segment_root = self._assert_under(
                    target.segment_root,
                    staging_root,
                    label="tracking segment",
                )
                yaml_path = self._assert_under(
                    target.yaml_path,
                    segment_root,
                    label="tracking YAML",
                )
                if not _regular_file(yaml_path):
                    raise RuntimeExecutionError(
                        "missing_tracking_yaml",
                        "A submitted Tracking YAML is unavailable.",
                    )
                if not _IDENTITY_RE.fullmatch(target.identity):
                    raise RuntimeExecutionError(
                        "invalid_runtime_request",
                        "Tracking identity is invalid.",
                    )
                dog_yaml = param_dir / "dog.yaml"
                if dog_yaml.exists() or dog_yaml.is_symlink():
                    try:
                        metadata = dog_yaml.lstat()
                    except OSError as exc:
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Tracking compatibility YAML cannot be inspected.",
                        ) from exc
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISREG(metadata.st_mode)
                    ):
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Tracking compatibility YAML has an unexpected type.",
                        )
                    dog_yaml.unlink()
                _copy_file_bytes(yaml_path, dog_yaml)
                tracking_img = output_root / "tracking_img"
                img_points = output_root / "img_points.txt"
                if tracking_img.exists():
                    if tracking_img.is_symlink() or not tracking_img.is_dir():
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Tracking scratch exists with an unexpected type.",
                        )
                    shutil.rmtree(tracking_img)
                if img_points.exists():
                    if img_points.is_symlink() or not img_points.is_file():
                        raise RuntimeExecutionError(
                            "recovery_required",
                            "Tracking scratch exists with an unexpected type.",
                        )
                    img_points.unlink()
                tracking_img.mkdir(mode=0o700)
                tracking_img.chmod(0o700, follow_symlinks=False)

                self._run_checked(
                    staging_root=staging_root,
                    argv=["./bin/main"],
                    cwd=tracking_root,
                    writable_bindings=(
                        (
                            private_data,
                            config.runtime_source_root / "Data",
                        ),
                        (
                            private_data,
                            config.legacy_tracking_data_root,
                        ),
                    ),
                    error_code="tracking_failed",
                )
                if not _regular_directory(tracking_img) or not _regular_file(
                    img_points,
                ):
                    raise RuntimeExecutionError(
                        "tracking_outputs_missing",
                        "Tracking did not produce its required outputs.",
                    )
                output_dir = segment_root / f"tracking_img_{target.identity}"
                points_path = segment_root / f"img_{target.identity}.txt"
                if output_dir.exists() or points_path.exists():
                    raise RuntimeExecutionError(
                        "recovery_required",
                        "Tracking output already exists; verify its checkpoint before retry.",
                    )
                try:
                    shutil.move(str(tracking_img), str(output_dir))
                    shutil.move(str(img_points), str(points_path))
                except OSError as exc:
                    raise RuntimeExecutionError(
                        "recovery_required",
                        "Tracking output publication was interrupted; inspect the partial output.",
                    ) from exc
                _harden_private_tree(output_dir)
                points_path.chmod(0o600, follow_symlinks=False)
                artifact_hash = tracking_checkpoint_artifact_sha256(
                    output_dir,
                    points_path,
                )
                checkpoints.append(
                    TrackingCheckpoint(
                        segment_root=segment_root,
                        identity=target.identity,
                        output_dir=output_dir,
                        points_path=points_path,
                        artifact_sha256=artifact_hash,
                    ),
                )
        return TrackingResult(
            checkpoints=tuple(checkpoints),
            runtime_manifest_sha256=runtime_manifest_sha256,
        )

    def verify_checkpoint(
        self,
        request: CheckpointVerificationRequest,
    ) -> bool:
        _safe_component(request.job_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        if not re.fullmatch(r"^[0-9a-f]{64}$", request.artifact_sha256):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Checkpoint hash is invalid.",
            )
        assert self.config.work_root is not None
        staging_root = self._assert_under(
            request.staging_root,
            self.config.work_root,
            label="job staging",
        )
        segment_root = self._assert_under(
            request.segment_root,
            staging_root,
            label="tracking segment",
        )
        if not _IDENTITY_RE.fullmatch(request.identity):
            raise RuntimeExecutionError(
                "invalid_runtime_request",
                "Tracking identity is invalid.",
            )
        output_dir = segment_root / f"tracking_img_{request.identity}"
        points_path = segment_root / f"img_{request.identity}.txt"
        if not _regular_directory(output_dir) or not _regular_file(points_path):
            return False
        actual = tracking_checkpoint_artifact_sha256(
            output_dir,
            points_path,
        )
        return actual == request.artifact_sha256

    def cancel(self, run_ref: str) -> None:
        """Cancellation is delivered through the bound CancellationContext.

        A run reference is accepted to make the application boundary explicit,
        but this adapter never guesses a PID from it.
        """

        _safe_component(run_ref, label="run_ref", pattern=_OPAQUE_REF_RE)
        raise RuntimeExecutionError(
            "cancellation_context_required",
            "Cancel the bound worker CancellationContext; run_ref is not a PID.",
        )


class NavigationAnnotationRuntimeDriver:
    """Single application-facing facade over preparation and Tracking."""

    def __init__(
        self,
        config: NavigationAnnotationRuntimeConfig | None = None,
    ) -> None:
        resolved = config or NavigationAnnotationRuntimeConfig.from_env()
        self._preparation = NavigationAnnotationRuntimeAdapter(resolved)
        self._tracking = NavigationTrackingRuntime(resolved)
        self._cancellation_lock = threading.RLock()
        self._active_cancellations: dict[str, CancellationContext] = {}
        self._pending_cancellations: set[str] = set()

    def capabilities(self) -> RuntimeCapabilities:
        return self._preparation.capabilities()

    def prepare(self, request: PreparationRequest) -> PreparedJob:
        return self._run_bound(
            request.job_ref,
            self._preparation.prepare,
            request,
        )

    def preflight_capacity(
        self,
        dataset_date: str,
        source_clips: Sequence[str],
        *,
        active_reserved_bytes: int = 0,
    ) -> CapacityEstimate:
        return self._preparation.preflight_capacity(
            dataset_date,
            source_clips,
            active_reserved_bytes=active_reserved_bytes,
        )

    def track(self, request: TrackingRequest) -> TrackingResult:
        return self._run_bound(
            request.job_ref,
            self._tracking.track,
            request,
        )

    def validate_tracking_inputs(
        self,
        request: TrackingInputValidationRequest,
    ) -> TrackingInputValidation:
        return self._tracking.validate_tracking_inputs(request)

    def verify_checkpoint(
        self,
        request: CheckpointVerificationRequest,
    ) -> bool:
        return self._tracking.verify_checkpoint(request)

    def cancel(self, run_ref: str) -> None:
        _safe_component(run_ref, label="job_ref", pattern=_OPAQUE_REF_RE)
        with self._cancellation_lock:
            cancellation = self._active_cancellations.get(run_ref)
            if cancellation is None:
                # Covers the race between durable cancellation and the worker
                # entering the runtime facade.
                self._pending_cancellations.add(run_ref)
                return
        cancellation.cancel()

    def _run_bound(
        self,
        job_ref: str,
        callable_: Callable[[object], object],
        request: object,
    ):
        cancellation = CancellationContext()
        with self._cancellation_lock:
            if job_ref in self._active_cancellations:
                raise RuntimeExecutionError(
                    "runtime_job_already_active",
                    "This annotation job already has an active Runtime call.",
                )
            if job_ref in self._pending_cancellations:
                self._pending_cancellations.remove(job_ref)
                cancellation.cancel()
            self._active_cancellations[job_ref] = cancellation
        try:
            with bind_cancellation(cancellation):
                with cancellation.track_background_operation():
                    cancellation.raise_if_cancelled()
                    return callable_(request)
        except TurnCancelled as exc:
            raise RuntimeExecutionError(
                "runtime_cancelled",
                "The annotation Runtime call was cancelled.",
                diagnostic_kind="cancelled",
                private_detail="diagnostic_kind=cancelled",
            ) from exc
        finally:
            with self._cancellation_lock:
                if self._active_cancellations.get(job_ref) is cancellation:
                    self._active_cancellations.pop(job_ref, None)


def build_default_runtime_driver() -> NavigationAnnotationRuntimeDriver:
    return NavigationAnnotationRuntimeDriver(
        NavigationAnnotationRuntimeConfig.from_env(),
    )
