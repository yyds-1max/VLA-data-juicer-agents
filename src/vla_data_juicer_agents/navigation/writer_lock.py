"""Cross-thread and cross-process serialization for navigation writers.

The legacy navigation runtime contains mutable compatibility paths.  A thread
semaphore alone is therefore insufficient: two web-service processes could
otherwise execute writers concurrently.  This module deliberately combines a
process-local capacity of one with an advisory ``flock`` on a system-owned
lock file.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, Token
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Iterator


_THREAD_CAPACITY = threading.BoundedSemaphore(1)
_LOCK_ENV = "VLA_NAVIGATION_WRITER_LOCK_PATH"


class NavigationWriterLockError(RuntimeError):
    """The global writer lock could not be acquired safely."""


class NavigationWriterQuarantinedError(NavigationWriterLockError):
    """A previous writer ended without proving that its side effects stopped."""


@dataclass
class _ActiveWriterLease:
    descriptor: int
    marker_path: Path
    marker_token: str
    marker_device: int
    marker_inode: int
    safe_to_clear: bool = True


@dataclass(frozen=True)
class WriterMarkerState:
    sha256: str
    active_present: bool
    quarantine_present: bool
    marker_entry_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class _MarkerIdentity:
    path: Path
    token: str
    device: int
    inode: int


_ACTIVE_WRITER_LEASES: ContextVar[tuple[_ActiveWriterLease, ...]] = ContextVar(
    "vla_active_navigation_writer_leases",
    default=(),
)


def configured_writer_lock_path() -> Path:
    configured = os.getenv(_LOCK_ENV)
    if not configured:
        raise NavigationWriterLockError(
            f"{_LOCK_ENV} must be configured explicitly",
        )
    path = Path(configured)
    validate_writer_lock_path(path)
    return path


def validate_writer_lock_path(path: Path) -> None:
    """Validate the shared production lock without creating filesystem state."""

    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise NavigationWriterLockError(
            f"{_LOCK_ENV} must be an absolute file path",
        )
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
        if parent.resolve(strict=True) != parent:
            raise NavigationWriterLockError(
                "writer lock parent must not traverse symlinks",
            )
    except NavigationWriterLockError:
        raise
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot inspect writer lock parent: {type(exc).__name__}",
        ) from exc
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(parent, os.W_OK | os.X_OK)
    ):
        raise NavigationWriterLockError(
            "writer lock parent is not private to the service",
        )
    try:
        lock_metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot inspect writer lock: {type(exc).__name__}",
        ) from exc
    if stat.S_ISLNK(lock_metadata.st_mode):
        raise NavigationWriterLockError(
            "writer lock path cannot be a symlink",
        )
    if (
        not stat.S_ISREG(lock_metadata.st_mode)
        or lock_metadata.st_uid != os.geteuid()
        or lock_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise NavigationWriterLockError(
            "writer lock must be a private service-owned regular file",
        )


def writer_quarantine_path(
    lock_path: Path,
    recovery_ref: str | None = None,
) -> Path:
    """Return the durable crash marker colocated with the shared lock."""

    suffix = (
        ".quarantine"
        if recovery_ref is None
        else f".quarantine.{recovery_ref}"
    )
    return lock_path.with_name(f"{lock_path.name}{suffix}")


def writer_active_path(lock_path: Path) -> Path:
    """Return the per-writer ownership marker colocated with the lock."""

    return lock_path.with_name(f"{lock_path.name}.active")


def _validate_quarantine_marker(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot inspect writer quarantine marker: {type(exc).__name__}",
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise NavigationWriterLockError(
            "writer quarantine marker must be a private service-owned regular file",
        )
    return metadata


def _open_parent_descriptor(path: Path) -> int:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        return os.open(path.parent, directory_flags)
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot open writer lock parent: {type(exc).__name__}",
        ) from exc


def _create_marker(
    marker_path: Path,
) -> tuple[Path, str, int, int]:
    token = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_descriptor = _open_parent_descriptor(marker_path)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                marker_path.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError as exc:
            _validate_quarantine_marker(marker_path)
            raise NavigationWriterQuarantinedError(
                "navigation writers are quarantined pending an operator safety check",
            ) from exc
        except OSError as exc:
            raise NavigationWriterLockError(
                f"cannot create writer quarantine marker: {type(exc).__name__}",
            ) from exc
        payload = f"v1:{token}\n".encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        os.fsync(parent_descriptor)
        return marker_path, token, metadata.st_dev, metadata.st_ino
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _read_marker_token(descriptor: int) -> str:
    chunks: list[bytes] = []
    total = 0
    while total <= 256:
        chunk = os.read(descriptor, 256 - total + 1)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > 256:
        raise NavigationWriterLockError("writer quarantine marker is malformed")
    try:
        payload = b"".join(chunks).decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise NavigationWriterLockError(
            "writer quarantine marker is malformed",
        ) from exc
    if not payload.startswith("v1:") or len(payload) != 67:
        raise NavigationWriterLockError("writer quarantine marker is malformed")
    return payload[3:]


def _remove_quarantine_marker(
    marker_path: Path,
    *,
    expected_token: str | None = None,
    expected_device: int | None = None,
    expected_inode: int | None = None,
) -> bool:
    try:
        _validate_quarantine_marker(marker_path)
    except NavigationWriterLockError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return False
        raise
    parent_descriptor = _open_parent_descriptor(marker_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                marker_path.name,
                flags,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise NavigationWriterLockError(
                f"cannot open writer quarantine marker: {type(exc).__name__}",
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise NavigationWriterLockError(
                "writer quarantine marker changed while it was active",
            )
        if (
            expected_device is not None
            and expected_inode is not None
            and (
                metadata.st_dev != expected_device
                or metadata.st_ino != expected_inode
            )
        ):
            raise NavigationWriterLockError(
                "writer quarantine marker changed while it was active",
            )
        token = _read_marker_token(descriptor)
        if expected_token is not None and token != expected_token:
            raise NavigationWriterLockError(
                "writer quarantine marker changed while it was active",
            )
        current = os.stat(
            marker_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
            raise NavigationWriterLockError(
                "writer quarantine marker changed while it was active",
            )
        os.unlink(marker_path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _marker_identity(path: Path) -> _MarkerIdentity | None:
    try:
        _validate_quarantine_marker(path)
    except NavigationWriterLockError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_descriptor = _open_parent_descriptor(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        token = _read_marker_token(descriptor)
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise NavigationWriterLockError(
                "writer quarantine marker changed while it was inspected",
            )
        return _MarkerIdentity(
            path=path,
            token=token,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot inspect writer quarantine marker: {type(exc).__name__}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _capture_marker_state(
    lock_path: Path,
) -> tuple[WriterMarkerState, tuple[_MarkerIdentity, ...]]:
    active = _marker_identity(writer_active_path(lock_path))
    quarantine_prefix = f"{lock_path.name}.quarantine"
    quarantine_paths: list[Path] = []
    try:
        with os.scandir(lock_path.parent) as entries:
            for entry in entries:
                if (
                    entry.name == quarantine_prefix
                    or entry.name.startswith(f"{quarantine_prefix}.")
                ):
                    quarantine_paths.append(lock_path.parent / entry.name)
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot enumerate writer quarantine markers: {type(exc).__name__}",
        ) from exc
    quarantines = tuple(
        identity
        for identity in (
            _marker_identity(path)
            for path in sorted(quarantine_paths, key=lambda item: item.name)
        )
        if identity is not None
    )
    lines = [f"active={active.token if active is not None else ''}"]
    lines.extend(f"{item.path.name}={item.token}" for item in quarantines)
    payload = ("\n".join(lines) + "\n").encode("ascii")
    marker_lines = (
        ([f"{active.path.name}={active.token}"] if active is not None else [])
        + [f"{item.path.name}={item.token}" for item in quarantines]
    )
    return (
        WriterMarkerState(
            sha256=hashlib.sha256(payload).hexdigest(),
            active_present=active is not None,
            quarantine_present=bool(quarantines),
            marker_entry_sha256s=tuple(
                hashlib.sha256(line.encode("ascii")).hexdigest()
                for line in marker_lines
            ),
        ),
        tuple(item for item in (active, *quarantines) if item is not None),
    )


def active_writer_lock_fds() -> tuple[int, ...]:
    """Descriptors a child writer must inherit to retain the global flock."""

    return tuple(lease.descriptor for lease in _ACTIVE_WRITER_LEASES.get())


def quarantine_active_writer() -> None:
    """Prevent automatic marker removal when child-process state is unknown."""

    for lease in _ACTIVE_WRITER_LEASES.get():
        lease.safe_to_clear = False


def ensure_navigation_writer_quarantine(
    lock_path: Path,
    *,
    recovery_ref: str | None = None,
) -> Path:
    """Persist a fail-closed marker when Store recovery detects unknown effects."""

    validate_writer_lock_path(lock_path)
    marker_ref = recovery_ref or f"recovery_{secrets.token_hex(16)}"
    if (
        not marker_ref
        or len(marker_ref) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in marker_ref
        )
    ):
        raise NavigationWriterLockError("writer recovery reference is invalid")
    marker_path = writer_quarantine_path(lock_path, marker_ref)
    existing = _marker_identity(marker_path)
    if existing is not None:
        return marker_path
    _create_marker(marker_path)
    return marker_path


def navigation_writer_marker_state(lock_path: Path) -> WriterMarkerState:
    """Capture an exact private marker-state fingerprint for an operator action."""

    validate_writer_lock_path(lock_path)
    state, _identities = _capture_marker_state(lock_path)
    return state


def navigation_writer_quarantine_present(lock_path: Path) -> bool:
    """Return whether either active or durable recovery state blocks writers."""

    state = navigation_writer_marker_state(lock_path)
    return state.active_present or state.quarantine_present


def navigation_writer_coordination_status(lock_path: Path) -> str:
    """Classify writer coordination as available, busy, or quarantined."""

    state = navigation_writer_marker_state(lock_path)
    if state.quarantine_present:
        return "quarantined"
    if not state.active_present:
        return "available"
    acquired_thread_capacity = _THREAD_CAPACITY.acquire(blocking=False)
    if not acquired_thread_capacity:
        refreshed = navigation_writer_marker_state(lock_path)
        return "quarantined" if refreshed.quarantine_present else "busy"
    descriptor: int | None = None
    try:
        descriptor = _open_lock_file(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            refreshed = navigation_writer_marker_state(lock_path)
            return "quarantined" if refreshed.quarantine_present else "busy"
        except OSError as exc:
            raise NavigationWriterLockError(
                f"writer lock operation failed: {type(exc).__name__}",
            ) from exc
        refreshed = navigation_writer_marker_state(lock_path)
        if refreshed.quarantine_present or refreshed.active_present:
            # An active marker without a lock owner survived a crash.
            return "quarantined"
        return "available"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _THREAD_CAPACITY.release()


@contextmanager
def navigation_writer_quarantine_clearance(
    lock_path: Path,
    *,
    expected_marker_state_sha256: str,
    all_writer_process_groups_absent: bool,
) -> Iterator[WriterMarkerState]:
    """Hold flock while a durable audit is committed, then clear exact markers."""

    if not all_writer_process_groups_absent:
        raise NavigationWriterLockError(
            "writer quarantine requires confirmation that all writer process "
            "groups are absent",
        )
    validate_writer_lock_path(lock_path)
    acquired_thread_capacity = _THREAD_CAPACITY.acquire(blocking=False)
    if not acquired_thread_capacity:
        raise NavigationWriterLockError("a navigation writer is still active")
    descriptor: int | None = None
    identities: tuple[_MarkerIdentity, ...] = ()
    body_completed = False
    try:
        descriptor = _open_lock_file(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NavigationWriterLockError(
                "a navigation writer process group is still active",
            ) from exc
        except OSError as exc:
            raise NavigationWriterLockError(
                f"writer lock operation failed: {type(exc).__name__}",
            ) from exc
        state, identities = _capture_marker_state(lock_path)
        if state.sha256 != expected_marker_state_sha256:
            raise NavigationWriterQuarantinedError(
                "writer quarantine changed after the operator confirmation",
            )
        yield state
        body_completed = True
        for identity in identities:
            _remove_quarantine_marker(
                identity.path,
                expected_token=identity.token,
                expected_device=identity.device,
                expected_inode=identity.inode,
            )
    finally:
        # On a DB/body failure, body_completed remains false and every marker
        # is preserved. An unlink failure also leaves at least one marker and
        # propagates, so later writers still fail closed.
        _ = body_completed
        if descriptor is not None:
            os.close(descriptor)
        _THREAD_CAPACITY.release()


def clear_navigation_writer_quarantine(
    lock_path: Path,
    *,
    all_writer_process_groups_absent: bool,
) -> bool:
    """Clear the crash marker only after an explicit, verifiable operator check."""

    expected = navigation_writer_marker_state(lock_path)
    with navigation_writer_quarantine_clearance(
        lock_path,
        expected_marker_state_sha256=expected.sha256,
        all_writer_process_groups_absent=all_writer_process_groups_absent,
    ) as state:
        return state.active_present or state.quarantine_present


def _open_lock_file(path: Path) -> int:
    validate_writer_lock_path(path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_descriptor: int | None = None
    try:
        parent_descriptor = os.open(path.parent, directory_flags)
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise NavigationWriterLockError(
            f"cannot open writer lock: {type(exc).__name__}",
        ) from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise NavigationWriterLockError(
                "writer lock must be a service-owned regular file",
            )
        os.fchmod(descriptor, 0o600)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def navigation_writer_lock(
    *,
    enabled: bool = True,
    lock_path: Path | None = None,
) -> Iterator[None]:
    """Hold the one-at-a-time navigation writer capacity.

    Dry runs pass ``enabled=False`` and intentionally avoid both locks.
    Advisory locking cannot serialize raw colleague scripts that do not use
    this contract; server operations must still enforce the operational
    no-concurrent-legacy-writer rule.
    """

    if not enabled:
        with nullcontext():
            yield
        return

    with _THREAD_CAPACITY:
        resolved_lock_path = lock_path or configured_writer_lock_path()
        descriptor = _open_lock_file(resolved_lock_path)
        active_lease: _ActiveWriterLease | None = None
        context_token: Token[tuple[_ActiveWriterLease, ...]] | None = None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise NavigationWriterLockError(
                    f"writer lock operation failed: {type(exc).__name__}",
                ) from exc
            if navigation_writer_quarantine_present(resolved_lock_path):
                raise NavigationWriterQuarantinedError(
                    "navigation writers are quarantined pending an operator "
                    "safety check",
                )
            (
                marker_path,
                marker_token,
                marker_device,
                marker_inode,
            ) = _create_marker(writer_active_path(resolved_lock_path))
            active_lease = _ActiveWriterLease(
                descriptor=descriptor,
                marker_path=marker_path,
                marker_token=marker_token,
                marker_device=marker_device,
                marker_inode=marker_inode,
            )
            context_token = _ACTIVE_WRITER_LEASES.set(
                (*_ACTIVE_WRITER_LEASES.get(), active_lease),
            )
            yield
        finally:
            if context_token is not None:
                _ACTIVE_WRITER_LEASES.reset(context_token)
            if active_lease is not None and active_lease.safe_to_clear:
                _remove_quarantine_marker(
                    active_lease.marker_path,
                    expected_token=active_lease.marker_token,
                    expected_device=active_lease.marker_device,
                    expected_inode=active_lease.marker_inode,
                )
            # Do not explicitly LOCK_UN: an inherited descriptor in a child
            # keeps the shared flock alive until that process also exits.
            os.close(descriptor)
