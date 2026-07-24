"""Exclusive lifecycle lock shared by the Annotation Web service and CLI."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import threading
from typing import Final


_LOCK_NAME: Final = ".annotation-service-maintenance.lock"
_SYSTEM_ROOT: Final = Path("/")
_PROCESS_LOCK = threading.Lock()
_ACTIVE_PATHS: set[Path] = set()


class AnnotationMaintenanceError(RuntimeError):
    """The Annotation lifecycle lock could not be used safely."""


class AnnotationServiceOnlineError(AnnotationMaintenanceError):
    """Another Web/maintenance owner already holds the lifecycle lock."""


def _canonical_private_parent(
    database_path: Path,
    *,
    create_parent: bool,
) -> Path:
    candidate = database_path
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    parent = candidate.parent
    try:
        metadata = parent.lstat()
    except FileNotFoundError:
        if not create_parent:
            raise AnnotationMaintenanceError(
                "annotation maintenance parent is unavailable",
            )
        grandparent = parent.parent
        try:
            grandparent_metadata = grandparent.lstat()
            canonical_grandparent = grandparent.resolve(strict=True)
        except OSError as exc:
            raise AnnotationMaintenanceError(
                "annotation maintenance parent is unavailable",
            ) from exc
        if (
            stat.S_ISLNK(grandparent_metadata.st_mode)
            or not stat.S_ISDIR(grandparent_metadata.st_mode)
            or canonical_grandparent != grandparent
        ):
            raise AnnotationMaintenanceError(
                "annotation maintenance parent is unsafe",
            )
        try:
            parent.mkdir(mode=0o700)
            metadata = parent.lstat()
        except OSError as exc:
            raise AnnotationMaintenanceError(
                "annotation maintenance parent is unavailable",
            ) from exc
    except OSError as exc:
        raise AnnotationMaintenanceError(
            "annotation maintenance parent is unavailable",
        ) from exc
    try:
        canonical_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise AnnotationMaintenanceError(
            "annotation maintenance parent is unavailable",
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or canonical_parent != parent
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise AnnotationMaintenanceError(
            "annotation maintenance parent is unsafe",
        )
    return canonical_parent


def annotation_maintenance_lock_path(
    database_path: Path | str,
    *,
    create_parent: bool = False,
) -> Path:
    database = Path(database_path)
    parent = _canonical_private_parent(
        database,
        create_parent=create_parent,
    )
    return parent / _LOCK_NAME


def _open_system_root() -> int:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(_SYSTEM_ROOT, directory_flags)
        metadata = os.fstat(descriptor)
        current = _SYSTEM_ROOT.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise AnnotationMaintenanceError(
                "annotation maintenance system root is unsafe",
            )
        return descriptor
    except AnnotationMaintenanceError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise AnnotationMaintenanceError(
            "annotation maintenance system root is unavailable",
        ) from exc


def _open_parent(path: Path) -> int:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path.parent, directory_flags)
        metadata = os.fstat(descriptor)
        current = path.parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise AnnotationMaintenanceError(
                "annotation maintenance parent is unsafe",
            )
        return descriptor
    except AnnotationMaintenanceError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise AnnotationMaintenanceError(
            "annotation maintenance parent is unavailable",
        ) from exc


def _open_lock(
    path: Path,
    *,
    parent_descriptor: int,
    create_lock_file: bool,
) -> int:
    flags = os.O_RDWR
    if create_lock_file:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise AnnotationMaintenanceError(
                "annotation maintenance lock is unsafe",
            )
        if create_lock_file:
            os.fchmod(descriptor, 0o600)
        return descriptor
    except AnnotationMaintenanceError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise AnnotationMaintenanceError(
            "annotation maintenance lock is unavailable",
        ) from exc


def _assert_lock_identity(
    *,
    path: Path,
    parent_descriptor: int,
    descriptor: int,
) -> tuple[int, int]:
    try:
        metadata = os.fstat(descriptor)
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise AnnotationMaintenanceError(
            "annotation maintenance lock is unavailable",
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or current.st_dev != metadata.st_dev
        or current.st_ino != metadata.st_ino
    ):
        raise AnnotationMaintenanceError(
            "annotation maintenance lock is unsafe",
        )
    return metadata.st_dev, metadata.st_ino


class AnnotationMaintenanceLease:
    def __init__(
        self,
        *,
        path: Path,
        system_root_descriptor: int,
        parent_descriptor: int,
        descriptor: int,
        lock_identity: tuple[int, int],
    ) -> None:
        self.path = path
        self._system_root_descriptor = system_root_descriptor
        self._parent_descriptor = parent_descriptor
        self._descriptor = descriptor
        self._lock_identity = lock_identity
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            descriptor = self._descriptor
            parent_descriptor = self._parent_descriptor
            system_root_descriptor = self._system_root_descriptor
            self._descriptor = -1
            self._parent_descriptor = -1
            self._system_root_descriptor = -1
            self._closed = True
            try:
                os.close(descriptor)
            finally:
                try:
                    os.close(parent_descriptor)
                finally:
                    try:
                        os.close(system_root_descriptor)
                    finally:
                        with _PROCESS_LOCK:
                            _ACTIVE_PATHS.discard(self.path)

    def __enter__(self) -> "AnnotationMaintenanceLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Process exit still releases flock; finalization must not surface
            # filesystem errors during interpreter or cyclic-GC teardown.
            pass


def acquire_annotation_maintenance(
    database_path: Path | str,
    *,
    create_parent: bool = False,
    create_lock_file: bool = False,
) -> AnnotationMaintenanceLease:
    """Acquire the service/maintenance lock without waiting."""

    path = annotation_maintenance_lock_path(
        database_path,
        create_parent=create_parent,
    )
    with _PROCESS_LOCK:
        # The system-root lock intentionally makes the Annotation lifecycle
        # capacity one per host. It is acquired before any caller-controlled
        # directory so replacing the entire working directory cannot move a
        # contender onto a different parent inode.
        if _ACTIVE_PATHS:
            raise AnnotationServiceOnlineError(
                "the Annotation service or maintenance CLI is already active",
            )
        system_root_descriptor = _open_system_root()
        try:
            fcntl.flock(
                system_root_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            os.close(system_root_descriptor)
            raise AnnotationServiceOnlineError(
                "the Annotation service or maintenance CLI is already active",
            ) from exc
        except OSError as exc:
            os.close(system_root_descriptor)
            raise AnnotationMaintenanceError(
                "annotation maintenance lock is unavailable",
            ) from exc
        try:
            parent_descriptor = _open_parent(path)
        except BaseException:
            os.close(system_root_descriptor)
            raise
        try:
            fcntl.flock(
                parent_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            os.close(parent_descriptor)
            os.close(system_root_descriptor)
            raise AnnotationServiceOnlineError(
                "the Annotation service or maintenance CLI is already active",
            ) from exc
        except OSError as exc:
            os.close(parent_descriptor)
            os.close(system_root_descriptor)
            raise AnnotationMaintenanceError(
                "annotation maintenance lock is unavailable",
            ) from exc
        descriptor: int | None = None
        try:
            descriptor = _open_lock(
                path,
                parent_descriptor=parent_descriptor,
                create_lock_file=create_lock_file,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AnnotationServiceOnlineError(
                    "the Annotation service or maintenance CLI is already active",
                ) from exc
            except OSError as exc:
                raise AnnotationMaintenanceError(
                    "annotation maintenance lock is unavailable",
                ) from exc
            lock_identity = _assert_lock_identity(
                path=path,
                parent_descriptor=parent_descriptor,
                descriptor=descriptor,
            )
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)
            os.close(system_root_descriptor)
            raise
        _ACTIVE_PATHS.add(path)
    return AnnotationMaintenanceLease(
        path=path,
        system_root_descriptor=system_root_descriptor,
        parent_descriptor=parent_descriptor,
        descriptor=descriptor,
        lock_identity=lock_identity,
    )
