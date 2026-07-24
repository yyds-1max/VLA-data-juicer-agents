#!/usr/bin/env python3
"""Safe PID-file operations and lifecycle serialization for run_web.sh."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
from typing import Final


UNSAFE_EXIT: Final = 2
MISSING_EXIT: Final = 3
CHANGED_EXIT: Final = 4
STALE_EXIT: Final = 5
_MAX_PID_FILE_BYTES: Final = 32
_MAX_IDENTITY_FILE_BYTES: Final = 160
_PID_PATTERN: Final = re.compile(rb"(?:[2-9]|[1-9][0-9]+)\n?")
_IDENTITY_PATTERN: Final = re.compile(
    rb"v2 (?:[2-9]|[1-9][0-9]+) "
    rb"(?:linux:[0-9a-f-]{36}:[0-9]+|darwin:[0-9]+:[0-9]+) "
    rb"[0-9a-f]{64}\n",
)
_LOCK_SUFFIX: Final = ".control.lock"
_IDENTITY_SUFFIX: Final = ".instance"
_ANCHOR_PATH: Final = Path("/usr")
_LOCKED_PID_PATH_ENV: Final = "VLA_RUN_WEB_LOCKED_PID_PATH"
_LOCKED_PARENT_IDENTITY_ENV: Final = "VLA_RUN_WEB_LOCKED_PARENT_IDENTITY"
_ANCHOR_FD_ENV: Final = "VLA_RUN_WEB_ANCHOR_FD"
_PARENT_FD_ENV: Final = "VLA_RUN_WEB_PARENT_FD"
_CONTROL_FD_ENV: Final = "VLA_RUN_WEB_CONTROL_FD"
_LINUX_BOOT_ID_PATTERN: Final = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    rb"[0-9a-f]{4}-[0-9a-f]{12}",
)
_LINUX_PIDFD_SYSCALLS: Final = {
    # Linux assigns these pidfd calls the same syscall numbers on the two
    # production architectures we support.  Keep the allowlist explicit so an
    # unknown ABI fails closed instead of invoking an unrelated syscall.
    "aarch64": (434, 424),
    "arm64": (434, 424),
    "amd64": (434, 424),
    "x86_64": (434, 424),
}
_PROC_PIDTBSDINFO: Final = 3


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class ControlPathError(RuntimeError):
    """A control path cannot be used without following or trusting unsafe state."""


def _linux_pidfd_syscall_numbers() -> tuple[int, int]:
    try:
        machine = os.uname().machine.lower()
    except (AttributeError, OSError) as exc:
        raise ControlPathError(
            "Linux pidfd signalling is unavailable",
        ) from exc
    numbers = _LINUX_PIDFD_SYSCALLS.get(machine)
    if numbers is None:
        raise ControlPathError(
            "Linux pidfd signalling is unavailable",
        )
    return numbers


def _raw_linux_syscall(number: int, *arguments: object) -> int:
    try:
        syscall = ctypes.CDLL(None, use_errno=True).syscall
    except (AttributeError, OSError) as exc:
        raise ControlPathError(
            "Linux pidfd signalling is unavailable",
        ) from exc
    syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = syscall(ctypes.c_long(number), *arguments)
    if result == -1:
        error_number = ctypes.get_errno() or errno.ENOSYS
        raise OSError(error_number, os.strerror(error_number))
    return int(result)


def _open_linux_pidfd(pid: int) -> int:
    pidfd_open = getattr(os, "pidfd_open", None)
    if callable(pidfd_open):
        return int(pidfd_open(pid))
    open_number, _send_number = _linux_pidfd_syscall_numbers()
    return _raw_linux_syscall(
        open_number,
        ctypes.c_int(pid),
        ctypes.c_uint(0),
    )


def _send_linux_pidfd_signal(
    pid_descriptor: int,
    signal_number: int,
) -> None:
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(pidfd_send_signal):
        pidfd_send_signal(
            pid_descriptor,
            signal_number,
            None,
            0,
        )
        return
    _open_number, send_number = _linux_pidfd_syscall_numbers()
    _raw_linux_syscall(
        send_number,
        ctypes.c_int(pid_descriptor),
        ctypes.c_int(signal_number),
        ctypes.c_void_p(),
        ctypes.c_uint(0),
    )


def _read_linux_process_birth_identity(pid: int) -> str | None:
    stat_path = f"/proc/{pid}/stat"
    try:
        descriptor = os.open(stat_path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControlPathError(
            "Web process birth identity is unavailable",
        ) from exc
    try:
        payload = os.read(descriptor, 8193)
    except OSError as exc:
        raise ControlPathError(
            "Web process birth identity is unavailable",
        ) from exc
    finally:
        os.close(descriptor)
    if len(payload) > 8192:
        raise ControlPathError("Web process birth identity is invalid")
    closing_parenthesis = payload.rfind(b") ")
    opening_parenthesis = payload.find(b" (")
    if opening_parenthesis <= 0 or closing_parenthesis <= opening_parenthesis:
        raise ControlPathError("Web process birth identity is invalid")
    try:
        stat_pid = int(payload[:opening_parenthesis])
    except ValueError as exc:
        raise ControlPathError("Web process birth identity is invalid") from exc
    fields_after_comm = payload[closing_parenthesis + 2 :].split()
    if stat_pid != pid or len(fields_after_comm) <= 19:
        raise ControlPathError("Web process birth identity is invalid")
    start_ticks = fields_after_comm[19]
    if not start_ticks.isdigit():
        raise ControlPathError("Web process birth identity is invalid")
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_bytes().strip()
    except OSError as exc:
        raise ControlPathError(
            "Linux boot identity is unavailable",
        ) from exc
    if not _LINUX_BOOT_ID_PATTERN.fullmatch(boot_id):
        raise ControlPathError("Linux boot identity is invalid")
    return (
        f"linux:{boot_id.decode('ascii')}:{int(start_ticks)}"
    )


def _read_darwin_process_birth_identity(pid: int) -> str | None:
    try:
        library = ctypes.CDLL(
            "/usr/lib/libproc.dylib",
            use_errno=True,
        )
    except OSError as exc:
        raise ControlPathError(
            "Web process birth identity is unavailable",
        ) from exc
    proc_pidinfo = library.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _DarwinProcBsdInfo()
    ctypes.set_errno(0)
    result = proc_pidinfo(
        pid,
        _PROC_PIDTBSDINFO,
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if result == 0:
        error_number = ctypes.get_errno()
        if error_number in {0, errno.ESRCH}:
            return None
        raise ControlPathError("Web process birth identity is unavailable")
    if result != ctypes.sizeof(info) or int(info.pbi_pid) != pid:
        raise ControlPathError("Web process birth identity is invalid")
    return (
        f"darwin:{int(info.pbi_start_tvsec)}:"
        f"{int(info.pbi_start_tvusec)}"
    )


def _process_birth_identity(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        return _read_linux_process_birth_identity(pid)
    if sys.platform == "darwin":
        return _read_darwin_process_birth_identity(pid)
    raise ControlPathError(
        "Stable process birth identity is unsupported on this platform",
    )


def _open_anchor() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(_ANCHOR_PATH, flags)
        metadata = os.fstat(descriptor)
        current = _ANCHOR_PATH.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or _ANCHOR_PATH.resolve(strict=True) != _ANCHOR_PATH
        ):
            raise ControlPathError("Web control anchor is unsafe")
        return descriptor
    except ControlPathError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ControlPathError("Web control anchor is unavailable") from exc


def _absolute_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise ControlPathError("PID parent directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or canonical != path
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ControlPathError(
            "PID parent directory must be a real owner-controlled directory",
        )


def _ensure_pid_parent(path: Path) -> None:
    parent = path.parent
    try:
        parent.lstat()
    except FileNotFoundError:
        grandparent = parent.parent
        try:
            grandparent_metadata = grandparent.lstat()
            canonical_grandparent = grandparent.resolve(strict=True)
        except OSError as exc:
            raise ControlPathError("PID parent directory is unavailable") from exc
        if (
            stat.S_ISLNK(grandparent_metadata.st_mode)
            or not stat.S_ISDIR(grandparent_metadata.st_mode)
            or canonical_grandparent != grandparent
        ):
            raise ControlPathError("PID parent directory is unsafe")
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ControlPathError("PID parent directory is unavailable") from exc
    except OSError as exc:
        raise ControlPathError("PID parent directory is unavailable") from exc
    _validate_directory(parent)


def _open_parent(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path.parent, flags)
        metadata = os.fstat(descriptor)
        current = path.parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise ControlPathError("PID parent directory is unsafe")
        return descriptor
    except ControlPathError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ControlPathError("PID parent directory is unavailable") from exc


def _assert_regular_owner_file(
    *,
    parent_descriptor: int,
    name: str,
    descriptor: int,
    label: str,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ControlPathError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_nlink != 1
        or current.st_dev != metadata.st_dev
        or current.st_ino != metadata.st_ino
    ):
        raise ControlPathError(
            f"{label} must be a real owner-controlled regular file",
        )
    return metadata


def _open_control_lock(path: Path, parent_descriptor: int) -> int:
    name = f"{path.name}{_LOCK_SUFFIX}"
    flags = os.O_RDWR
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        _assert_regular_owner_file(
            parent_descriptor=parent_descriptor,
            name=name,
            descriptor=descriptor,
            label="Web control lock",
        )
        return descriptor
    except ControlPathError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ControlPathError("Web control lock is unavailable") from exc


def _open_pid_file(
    path: Path,
    parent_descriptor: int,
) -> tuple[int, os.stat_result] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControlPathError("PID file is unavailable") from exc
    try:
        metadata = _assert_regular_owner_file(
            parent_descriptor=parent_descriptor,
            name=path.name,
            descriptor=descriptor,
            label="PID file",
        )
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _open_identity_file(
    path: Path,
    parent_descriptor: int,
) -> tuple[int, os.stat_result] | None:
    name = f"{path.name}{_IDENTITY_SUFFIX}"
    flags = os.O_RDWR
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControlPathError("Web instance identity is unavailable") from exc
    try:
        metadata = _assert_regular_owner_file(
            parent_descriptor=parent_descriptor,
            name=name,
            descriptor=descriptor,
            label="Web instance identity",
        )
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _read_identity_descriptor(
    descriptor: int,
    metadata: os.stat_result,
) -> tuple[int, str, str]:
    try:
        if metadata.st_size > _MAX_IDENTITY_FILE_BYTES:
            raise ControlPathError("Web instance identity is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, _MAX_IDENTITY_FILE_BYTES + 1)
    except OSError as exc:
        raise ControlPathError("Web instance identity is unavailable") from exc
    if not _IDENTITY_PATTERN.fullmatch(payload):
        raise ControlPathError("Web instance identity is invalid")
    _version, raw_pid, raw_birth, raw_token = payload.rstrip(b"\n").split(b" ")
    return (
        int(raw_pid),
        raw_birth.decode("ascii"),
        raw_token.decode("ascii"),
    )


def _identity_lock_is_held(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        raise ControlPathError("Web instance identity is unavailable") from exc
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    return False


def _active_pid(
    path: Path,
    parent_descriptor: int,
    *,
    expected_pid: int | None = None,
) -> tuple[int, int | None]:
    current = _read_pid(path, parent_descriptor)
    if current is None:
        return MISSING_EXIT, None
    pid, _identity = current
    if expected_pid is not None and pid != expected_pid:
        return CHANGED_EXIT, None
    opened_identity = _open_identity_file(path, parent_descriptor)
    if opened_identity is None:
        return STALE_EXIT, None
    descriptor, metadata = opened_identity
    try:
        identity_pid, recorded_birth, _token = _read_identity_descriptor(
            descriptor,
            metadata,
        )
        if identity_pid != pid:
            return STALE_EXIT, None
        if not _identity_lock_is_held(descriptor):
            return STALE_EXIT, None
        current_birth = _process_birth_identity(pid)
        if current_birth is None or current_birth != recorded_birth:
            return STALE_EXIT, None
    finally:
        os.close(descriptor)
    return 0, pid


def _read_pid(
    path: Path,
    parent_descriptor: int,
) -> tuple[int, tuple[int, int]] | None:
    opened = _open_pid_file(path, parent_descriptor)
    if opened is None:
        return None
    descriptor, metadata = opened
    try:
        if metadata.st_size > _MAX_PID_FILE_BYTES:
            raise ControlPathError("PID file content is invalid")
        payload = os.read(descriptor, _MAX_PID_FILE_BYTES + 1)
    except OSError as exc:
        raise ControlPathError("PID file is unavailable") from exc
    finally:
        os.close(descriptor)
    if not _PID_PATTERN.fullmatch(payload):
        raise ControlPathError(
            "PID file must contain one canonical decimal PID greater than 1",
        )
    pid = int(payload.rstrip(b"\n"))
    if pid <= 1:
        raise ControlPathError(
            "PID file must contain one canonical decimal PID greater than 1",
        )
    return pid, (metadata.st_dev, metadata.st_ino)


def _validated_pid(raw_pid: str) -> int:
    try:
        payload = raw_pid.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ControlPathError("PID must be a canonical decimal greater than 1") from exc
    if not _PID_PATTERN.fullmatch(payload) or b"\n" in payload:
        raise ControlPathError("PID must be a canonical decimal greater than 1")
    pid = int(raw_pid)
    if pid <= 1:
        raise ControlPathError("PID must be a canonical decimal greater than 1")
    return pid


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short PID-file write")
        offset += written


def _atomic_write_pid(path: Path, parent_descriptor: int, pid: int) -> None:
    # Refuse to replace an unsafe pre-existing object.
    existing = _open_pid_file(path, parent_descriptor)
    if existing is not None:
        os.close(existing[0])

    temporary_name = (
        f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        _write_all(descriptor, f"{pid}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise ControlPathError("PID file could not be written safely") from exc


def _create_locked_identity(
    path: Path,
    parent_descriptor: int,
    pid: int,
) -> int:
    birth_identity = _process_birth_identity(pid)
    if birth_identity is None:
        raise ControlPathError("Web process birth identity is unavailable")
    existing = _open_identity_file(path, parent_descriptor)
    if existing is not None:
        existing_descriptor, existing_metadata = existing
        try:
            _read_identity_descriptor(existing_descriptor, existing_metadata)
            if _identity_lock_is_held(existing_descriptor):
                raise ControlPathError(
                    "Another Web service instance identity is still active",
                )
        finally:
            os.close(existing_descriptor)

    token = secrets.token_hex(32)
    identity_name = f"{path.name}{_IDENTITY_SUFFIX}"
    temporary_name = (
        f".{identity_name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    )
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        _write_all(
            descriptor,
            f"v2 {pid} {birth_identity} {token}\n".encode("ascii"),
        )
        os.fsync(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.replace(
            temporary_name,
            identity_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        os.set_inheritable(descriptor, True)
        return descriptor
    except BlockingIOError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise ControlPathError(
            "Web instance identity could not be locked",
        ) from exc
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise ControlPathError(
            "Web instance identity could not be created safely",
        ) from exc


def _remove_expected_identity(
    path: Path,
    parent_descriptor: int,
    expected_pid: int,
) -> int:
    opened = _open_identity_file(path, parent_descriptor)
    if opened is None:
        return MISSING_EXIT
    descriptor, metadata = opened
    try:
        identity_pid, _birth, _token = _read_identity_descriptor(
            descriptor,
            metadata,
        )
        if identity_pid != expected_pid:
            return CHANGED_EXIT
        if _identity_lock_is_held(descriptor):
            return CHANGED_EXIT
        current = os.stat(
            f"{path.name}{_IDENTITY_SUFFIX}",
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            return CHANGED_EXIT
        os.unlink(
            f"{path.name}{_IDENTITY_SUFFIX}",
            dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except FileNotFoundError:
        return MISSING_EXIT
    except OSError as exc:
        raise ControlPathError(
            "Web instance identity could not be removed safely",
        ) from exc
    finally:
        os.close(descriptor)
    return 0


def _remove_expected_pid(
    path: Path,
    parent_descriptor: int,
    expected_pid: int,
) -> int:
    current = _read_pid(path, parent_descriptor)
    if current is None:
        return MISSING_EXIT
    current_pid, identity = current
    if current_pid != expected_pid:
        return CHANGED_EXIT
    try:
        metadata = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (metadata.st_dev, metadata.st_ino) != identity:
            return CHANGED_EXIT
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except FileNotFoundError:
        return MISSING_EXIT
    except OSError as exc:
        raise ControlPathError("PID file could not be removed safely") from exc
    return 0


def _remove_expected_service_record(
    path: Path,
    parent_descriptor: int,
    expected_pid: int,
) -> int:
    current = _read_pid(path, parent_descriptor)
    if current is None:
        return MISSING_EXIT
    if current[0] != expected_pid:
        return CHANGED_EXIT
    identity_status = _remove_expected_identity(
        path,
        parent_descriptor,
        expected_pid,
    )
    if identity_status not in {0, MISSING_EXIT}:
        return identity_status
    return _remove_expected_pid(path, parent_descriptor, expected_pid)


def _prepare_empty_service_record(
    path: Path,
    parent_descriptor: int,
) -> int:
    if _read_pid(path, parent_descriptor) is not None:
        return CHANGED_EXIT
    opened = _open_identity_file(path, parent_descriptor)
    if opened is None:
        return 0
    descriptor, metadata = opened
    try:
        identity_pid, _birth, _token = _read_identity_descriptor(
            descriptor,
            metadata,
        )
        if _identity_lock_is_held(descriptor):
            return CHANGED_EXIT
    finally:
        os.close(descriptor)
    if _read_pid(path, parent_descriptor) is not None:
        return CHANGED_EXIT
    remove_status = _remove_expected_identity(
        path,
        parent_descriptor,
        identity_pid,
    )
    if remove_status == MISSING_EXIT:
        return 0
    return remove_status


def _send_instance_signal(
    path: Path,
    parent_descriptor: int,
    *,
    expected_pid: int,
    signal_number: int,
) -> int:
    pid_descriptor: int | None = None
    if sys.platform.startswith("linux"):
        try:
            pid_descriptor = _open_linux_pidfd(expected_pid)
        except ProcessLookupError:
            return STALE_EXIT
        except (OSError, OverflowError) as exc:
            raise ControlPathError("Web service process identity is unavailable") from exc
    active_status, _active = _active_pid(
        path,
        parent_descriptor,
        expected_pid=expected_pid,
    )
    if active_status != 0:
        if pid_descriptor is not None:
            os.close(pid_descriptor)
        return active_status
    try:
        if pid_descriptor is not None:
            _send_linux_pidfd_signal(
                pid_descriptor,
                signal_number,
            )
        else:
            os.kill(expected_pid, signal_number)
    except ProcessLookupError:
        return STALE_EXIT
    except OSError as exc:
        raise ControlPathError("Web service signal could not be delivered") from exc
    finally:
        if pid_descriptor is not None:
            os.close(pid_descriptor)
    return 0


def _pid_path(raw_path: str) -> Path:
    path = _absolute_path(raw_path)
    if path.name in {"", ".", ".."}:
        raise ControlPathError("PID file path is invalid")
    _ensure_pid_parent(path)
    locked_path = os.environ.get(_LOCKED_PID_PATH_ENV)
    locked_parent_identity = os.environ.get(_LOCKED_PARENT_IDENTITY_ENV)
    if locked_path is not None or locked_parent_identity is not None:
        if locked_path != str(path) or locked_parent_identity is None:
            raise ControlPathError("PID control scope changed while locked")
        try:
            parent_metadata = path.parent.lstat()
        except OSError as exc:
            raise ControlPathError(
                "PID control scope changed while locked",
            ) from exc
        actual_identity = f"{parent_metadata.st_dev}:{parent_metadata.st_ino}"
        if actual_identity != locked_parent_identity:
            raise ControlPathError("PID control scope changed while locked")
    return path


def _descriptor_from_environment(name: str) -> int:
    raw_descriptor = os.environ.get(name)
    if (
        raw_descriptor is None
        or not raw_descriptor.isascii()
        or not raw_descriptor.isdigit()
    ):
        raise ControlPathError("Web control lock capability is unavailable")
    descriptor = int(raw_descriptor)
    if descriptor <= 2:
        raise ControlPathError("Web control lock capability is invalid")
    try:
        os.fstat(descriptor)
    except OSError as exc:
        raise ControlPathError(
            "Web control lock capability is unavailable",
        ) from exc
    return descriptor


def _verify_lock_command(args: argparse.Namespace) -> int:
    if os.environ.get("VLA_RUN_WEB_CONTROL_LOCKED") != "1":
        raise ControlPathError("Web control lock capability is unavailable")
    path = _absolute_path(args.pid_file)
    anchor_descriptor = _descriptor_from_environment(_ANCHOR_FD_ENV)
    parent_descriptor = _descriptor_from_environment(_PARENT_FD_ENV)
    control_descriptor = _descriptor_from_environment(_CONTROL_FD_ENV)
    if len({anchor_descriptor, parent_descriptor, control_descriptor}) != 3:
        raise ControlPathError("Web control lock capability is invalid")
    if os.environ.get(_LOCKED_PID_PATH_ENV) != str(path):
        raise ControlPathError("PID control scope changed while locked")

    anchor_metadata = os.fstat(anchor_descriptor)
    current_anchor = _ANCHOR_PATH.lstat()
    if (
        not stat.S_ISDIR(anchor_metadata.st_mode)
        or current_anchor.st_dev != anchor_metadata.st_dev
        or current_anchor.st_ino != anchor_metadata.st_ino
    ):
        raise ControlPathError("Web control anchor capability is invalid")
    try:
        fcntl.flock(
            anchor_descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError as exc:
        raise ControlPathError(
            "Web control anchor capability is not locked",
        ) from exc

    parent_metadata = os.fstat(parent_descriptor)
    try:
        current_parent = path.parent.lstat()
    except OSError as exc:
        raise ControlPathError("PID control scope changed while locked") from exc
    expected_parent_identity = os.environ.get(_LOCKED_PARENT_IDENTITY_ENV)
    actual_parent_identity = (
        f"{parent_metadata.st_dev}:{parent_metadata.st_ino}"
    )
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or expected_parent_identity != actual_parent_identity
        or current_parent.st_dev != parent_metadata.st_dev
        or current_parent.st_ino != parent_metadata.st_ino
    ):
        raise ControlPathError("PID control scope changed while locked")

    _assert_regular_owner_file(
        parent_descriptor=parent_descriptor,
        name=f"{path.name}{_LOCK_SUFFIX}",
        descriptor=control_descriptor,
        label="Web control lock",
    )
    try:
        fcntl.flock(
            control_descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError as exc:
        raise ControlPathError(
            "Web control lock capability is not locked",
        ) from exc
    return 0


def _run_with_lock(args: argparse.Namespace) -> int:
    if not args.command:
        raise ControlPathError("A locked lifecycle command is required")
    anchor_descriptor = _open_anchor()
    parent_descriptor: int | None = None
    lock_descriptor: int | None = None
    try:
        fcntl.flock(anchor_descriptor, fcntl.LOCK_EX)
        path = _pid_path(args.pid_file)
        parent_descriptor = _open_parent(path)
        lock_descriptor = _open_control_lock(path, parent_descriptor)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        _assert_regular_owner_file(
            parent_descriptor=parent_descriptor,
            name=f"{path.name}{_LOCK_SUFFIX}",
            descriptor=lock_descriptor,
            label="Web control lock",
        )
        environment = os.environ.copy()
        environment["VLA_RUN_WEB_CONTROL_LOCKED"] = "1"
        parent_metadata = os.fstat(parent_descriptor)
        environment[_LOCKED_PID_PATH_ENV] = str(path)
        environment[_LOCKED_PARENT_IDENTITY_ENV] = (
            f"{parent_metadata.st_dev}:{parent_metadata.st_ino}"
        )
        environment[_ANCHOR_FD_ENV] = str(anchor_descriptor)
        environment[_PARENT_FD_ENV] = str(parent_descriptor)
        environment[_CONTROL_FD_ENV] = str(lock_descriptor)
        completed = subprocess.run(
            args.command,
            env=environment,
            check=False,
            pass_fds=(
                anchor_descriptor,
                parent_descriptor,
                lock_descriptor,
            ),
        )
        return completed.returncode
    except OSError as exc:
        raise ControlPathError("Web control lock is unavailable") from exc
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        os.close(anchor_descriptor)


def _read_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    parent_descriptor = _open_parent(path)
    try:
        current = _read_pid(path, parent_descriptor)
    finally:
        os.close(parent_descriptor)
    if current is None:
        return MISSING_EXIT
    print(current[0])
    return 0


def _active_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    expected_pid = (
        _validated_pid(args.expected_pid)
        if args.expected_pid is not None
        else None
    )
    parent_descriptor = _open_parent(path)
    try:
        active_status, active_pid = _active_pid(
            path,
            parent_descriptor,
            expected_pid=expected_pid,
        )
    finally:
        os.close(parent_descriptor)
    if active_status != 0:
        return active_status
    assert active_pid is not None
    print(active_pid)
    return 0


def _write_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    pid = _validated_pid(args.pid)
    parent_descriptor = _open_parent(path)
    try:
        _atomic_write_pid(path, parent_descriptor, pid)
    finally:
        os.close(parent_descriptor)
    return 0


def _remove_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    expected_pid = _validated_pid(args.expected_pid)
    parent_descriptor = _open_parent(path)
    try:
        return _remove_expected_pid(path, parent_descriptor, expected_pid)
    finally:
        os.close(parent_descriptor)


def _remove_service_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    expected_pid = _validated_pid(args.expected_pid)
    parent_descriptor = _open_parent(path)
    try:
        return _remove_expected_service_record(
            path,
            parent_descriptor,
            expected_pid,
        )
    finally:
        os.close(parent_descriptor)


def _prepare_start_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    parent_descriptor = _open_parent(path)
    try:
        return _prepare_empty_service_record(path, parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _signal_command(args: argparse.Namespace) -> int:
    path = _pid_path(args.pid_file)
    expected_pid = _validated_pid(args.expected_pid)
    signal_number = {
        "TERM": signal.SIGTERM,
        "KILL": signal.SIGKILL,
    }[args.signal]
    parent_descriptor = _open_parent(path)
    try:
        return _send_instance_signal(
            path,
            parent_descriptor,
            expected_pid=expected_pid,
            signal_number=signal_number,
        )
    finally:
        os.close(parent_descriptor)


def _hold_instance_command(args: argparse.Namespace) -> int:
    if not args.command:
        raise ControlPathError("A Web service command is required")
    path = _pid_path(args.pid_file)
    pid = os.getpid()
    if pid <= 1:
        raise ControlPathError("Web service PID is invalid")
    parent_descriptor = _open_parent(path)
    identity_descriptor: int | None = None
    try:
        identity_descriptor = _create_locked_identity(
            path,
            parent_descriptor,
            pid,
        )
    finally:
        os.close(parent_descriptor)
    try:
        environment = os.environ.copy()
        environment.pop("VLA_RUN_WEB_CONTROL_LOCKED", None)
        environment.pop(_LOCKED_PID_PATH_ENV, None)
        environment.pop(_LOCKED_PARENT_IDENTITY_ENV, None)
        environment.pop(_ANCHOR_FD_ENV, None)
        environment.pop(_PARENT_FD_ENV, None)
        environment.pop(_CONTROL_FD_ENV, None)
        os.execvpe(args.command[0], args.command, environment)
    except OSError as exc:
        raise ControlPathError("Web service command could not be executed") from exc
    finally:
        if identity_descriptor is not None:
            os.close(identity_descriptor)
    return 127


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_web_control.py")
    subparsers = parser.add_subparsers(dest="action", required=True)

    locked = subparsers.add_parser("with-lock")
    locked.add_argument("--pid-file", required=True)
    locked.add_argument("command", nargs=argparse.REMAINDER)
    locked.set_defaults(handler=_run_with_lock)

    verify_lock = subparsers.add_parser("verify-lock")
    verify_lock.add_argument("--pid-file", required=True)
    verify_lock.set_defaults(handler=_verify_lock_command)

    read = subparsers.add_parser("read-pid")
    read.add_argument("--pid-file", required=True)
    read.set_defaults(handler=_read_command)

    active = subparsers.add_parser("active-pid")
    active.add_argument("--pid-file", required=True)
    active.add_argument("--expected-pid")
    active.set_defaults(handler=_active_command)

    write = subparsers.add_parser("write-pid")
    write.add_argument("--pid-file", required=True)
    write.add_argument("--pid", required=True)
    write.set_defaults(handler=_write_command)

    remove = subparsers.add_parser("remove-pid")
    remove.add_argument("--pid-file", required=True)
    remove.add_argument("--expected-pid", required=True)
    remove.set_defaults(handler=_remove_command)

    remove_service = subparsers.add_parser("remove-service")
    remove_service.add_argument("--pid-file", required=True)
    remove_service.add_argument("--expected-pid", required=True)
    remove_service.set_defaults(handler=_remove_service_command)

    prepare_start = subparsers.add_parser("prepare-start")
    prepare_start.add_argument("--pid-file", required=True)
    prepare_start.set_defaults(handler=_prepare_start_command)

    send_signal = subparsers.add_parser("signal-instance")
    send_signal.add_argument("--pid-file", required=True)
    send_signal.add_argument("--expected-pid", required=True)
    send_signal.add_argument("--signal", choices=("TERM", "KILL"), required=True)
    send_signal.set_defaults(handler=_signal_command)

    hold_instance = subparsers.add_parser("hold-instance")
    hold_instance.add_argument("--pid-file", required=True)
    hold_instance.add_argument("command", nargs=argparse.REMAINDER)
    hold_instance.set_defaults(handler=_hold_instance_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action in {"with-lock", "hold-instance"} and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        return int(args.handler(args))
    except ControlPathError as exc:
        print(f"run_web control error: {exc}", file=sys.stderr)
        return UNSAFE_EXIT
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
