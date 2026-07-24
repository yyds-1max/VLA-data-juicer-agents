from __future__ import annotations

import errno
import importlib.util
from pathlib import Path
import signal
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROL_HELPER = ROOT / "scripts" / "run_web_control.py"


def _load_control_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_web_control_under_test",
        CONTROL_HELPER,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argument_value(argument: object) -> object:
    return getattr(argument, "value", argument)


def test_linux_pidfd_fallback_uses_allowlisted_raw_syscalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _load_control_helper()
    calls: list[tuple[int, tuple[object, ...]]] = []

    monkeypatch.delattr(control.os, "pidfd_open", raising=False)
    monkeypatch.delattr(control.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        control.os,
        "uname",
        lambda: SimpleNamespace(machine="x86_64"),
    )

    def fake_syscall(number: int, *arguments: object) -> int:
        calls.append(
            (number, tuple(_argument_value(value) for value in arguments)),
        )
        return 91 if number == 434 else 0

    monkeypatch.setattr(control, "_raw_linux_syscall", fake_syscall)

    descriptor = control._open_linux_pidfd(321)
    control._send_linux_pidfd_signal(descriptor, signal.SIGTERM)

    assert descriptor == 91
    assert calls == [
        (434, (321, 0)),
        (424, (91, signal.SIGTERM, None, 0)),
    ]


def test_linux_pidfd_prefers_python_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _load_control_helper()
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        control.os,
        "pidfd_open",
        lambda pid: calls.append(("open", pid)) or 73,
        raising=False,
    )
    monkeypatch.setattr(
        control.signal,
        "pidfd_send_signal",
        lambda descriptor, number, info, flags: calls.append(
            ("send", descriptor, number, info, flags),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        control,
        "_raw_linux_syscall",
        lambda *_args: pytest.fail("raw syscall fallback was used"),
    )

    descriptor = control._open_linux_pidfd(456)
    control._send_linux_pidfd_signal(descriptor, signal.SIGKILL)

    assert descriptor == 73
    assert calls == [
        ("open", 456),
        ("send", 73, signal.SIGKILL, None, 0),
    ]


def test_linux_pidfd_fallback_rejects_unknown_architecture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _load_control_helper()
    monkeypatch.delattr(control.os, "pidfd_open", raising=False)
    monkeypatch.setattr(
        control.os,
        "uname",
        lambda: SimpleNamespace(machine="unknown-linux-abi"),
    )

    with pytest.raises(
        control.ControlPathError,
        match="Linux pidfd signalling is unavailable",
    ):
        control._open_linux_pidfd(789)


def test_raw_linux_syscall_preserves_process_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _load_control_helper()

    class FakeSyscall:
        restype: object = None

        def __call__(self, *_arguments: object) -> int:
            return -1

    fake_syscall = FakeSyscall()
    monkeypatch.setattr(
        control.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(syscall=fake_syscall),
    )
    monkeypatch.setattr(control.ctypes, "get_errno", lambda: errno.ESRCH)

    with pytest.raises(ProcessLookupError):
        control._raw_linux_syscall(434, 123, 0)

