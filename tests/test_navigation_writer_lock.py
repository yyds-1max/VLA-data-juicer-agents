from __future__ import annotations

from pathlib import Path
import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from vla_data_juicer_agents.navigation.writer_lock import (
    NavigationWriterLockError,
    NavigationWriterQuarantinedError,
    active_writer_lock_fds,
    clear_navigation_writer_quarantine,
    configured_writer_lock_path,
    ensure_navigation_writer_quarantine,
    navigation_writer_marker_state,
    navigation_writer_quarantine_clearance,
    navigation_writer_lock,
    quarantine_active_writer,
    writer_active_path,
    writer_quarantine_path,
)


def _hold_process_lock(
    lock_path: str,
    acquired,
    release,
) -> None:
    with navigation_writer_lock(lock_path=Path(lock_path)):
        acquired.set()
        release.wait(timeout=5)


def _report_process_lock(lock_path: str, acquired_queue) -> None:
    with navigation_writer_lock(lock_path=Path(lock_path)):
        acquired_queue.put("second")


def _crash_while_holding_process_lock(lock_path: str, acquired) -> None:
    with navigation_writer_lock(lock_path=Path(lock_path)):
        acquired.set()
        os._exit(17)


def _crash_parent_with_inherited_lock_child(
    lock_path: str,
    child_ready_path: str,
    child_release_path: str,
    acquired,
) -> None:
    child_code = (
        "import pathlib,sys,time;"
        "ready=pathlib.Path(sys.argv[1]);"
        "release=pathlib.Path(sys.argv[2]);"
        "ready.write_text('ready');"
        "\nwhile not release.exists(): time.sleep(0.02)"
    )
    with navigation_writer_lock(lock_path=Path(lock_path)):
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                child_ready_path,
                child_release_path,
            ],
            pass_fds=active_writer_lock_fds(),
            start_new_session=True,
        )
        deadline = time.monotonic() + 2
        while (
            not Path(child_ready_path).exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not Path(child_ready_path).exists():
            os._exit(18)
        acquired.set()
        os._exit(17)


def _report_quarantined_or_entered(lock_path: str, report_queue) -> None:
    try:
        with navigation_writer_lock(lock_path=Path(lock_path)):
            report_queue.put("entered")
    except NavigationWriterQuarantinedError:
        report_queue.put("quarantined")


def test_navigation_writer_lock_serializes_threads(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    entered: list[str] = []
    first_holds = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with navigation_writer_lock(lock_path=lock_path):
            entered.append("first")
            first_holds.set()
            release_first.wait(timeout=2)

    def second() -> None:
        first_holds.wait(timeout=2)
        with navigation_writer_lock(lock_path=lock_path):
            entered.append("second")

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    assert first_holds.wait(timeout=2)
    time.sleep(0.05)
    assert entered == ["first"]
    release_first.set()
    one.join(timeout=2)
    two.join(timeout=2)
    assert entered == ["first", "second"]


def test_navigation_writer_lock_rejects_relative_and_symlink_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(NavigationWriterLockError, match="absolute"):
        with navigation_writer_lock(lock_path=Path("relative.lock")):
            pass

    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(NavigationWriterLockError, match="symlink"):
        with navigation_writer_lock(lock_path=link):
            pass

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(NavigationWriterLockError, match="parent"):
        with navigation_writer_lock(
            lock_path=linked_parent / "writer.lock",
        ):
            pass


def test_navigation_writer_lock_requires_explicit_private_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        raising=False,
    )
    with pytest.raises(NavigationWriterLockError, match="configured"):
        configured_writer_lock_path()
    with pytest.raises(NavigationWriterLockError, match="configured"):
        with navigation_writer_lock():
            pass

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        str(shared / "writer.lock"),
    )
    with pytest.raises(NavigationWriterLockError, match="private"):
        configured_writer_lock_path()

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    configured = private / "writer.lock"
    monkeypatch.setenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        str(configured),
    )
    assert configured_writer_lock_path() == configured
    with navigation_writer_lock():
        pass
    assert configured.is_file()


def test_disabled_navigation_writer_lock_does_not_create_file(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "writer.lock"
    with navigation_writer_lock(enabled=False, lock_path=lock_path):
        pass
    assert not lock_path.exists()


def test_navigation_writer_lock_preserves_business_oserror(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    with pytest.raises(OSError, match="business failure"):
        with navigation_writer_lock(lock_path=lock_path):
            raise OSError("business failure")
    assert not writer_quarantine_path(lock_path).exists()


def test_navigation_writer_lock_serializes_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    reports = context.Queue()
    lock_path = tmp_path / "writer.lock"
    first = context.Process(
        target=_hold_process_lock,
        args=(str(lock_path), acquired, release),
    )
    second = context.Process(
        target=_report_process_lock,
        args=(str(lock_path), reports),
    )
    first.start()
    assert acquired.wait(timeout=2)
    second.start()
    with pytest.raises(queue.Empty):
        reports.get(timeout=0.1)
    release.set()
    assert reports.get(timeout=2) == "second"
    first.join(timeout=2)
    second.join(timeout=2)
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_writer_child_inherits_flock_descriptor(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    with navigation_writer_lock(lock_path=lock_path):
        descriptors = active_writer_lock_fds()
        assert len(descriptors) == 1
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; os.fstat(int(sys.argv[1]))",
                str(descriptors[0]),
            ],
            check=True,
            pass_fds=descriptors,
        )
        assert writer_active_path(lock_path).is_file()
    assert not writer_active_path(lock_path).exists()


def test_crashed_writer_leaves_durable_quarantine_until_operator_clear(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    lock_path = tmp_path / "writer.lock"
    crashed = context.Process(
        target=_crash_while_holding_process_lock,
        args=(str(lock_path), acquired),
    )
    crashed.start()
    assert acquired.wait(timeout=2)
    crashed.join(timeout=2)
    assert crashed.exitcode == 17
    marker_path = writer_active_path(lock_path)
    assert marker_path.is_file()

    with pytest.raises(NavigationWriterQuarantinedError):
        with navigation_writer_lock(lock_path=lock_path):
            pass
    with pytest.raises(NavigationWriterLockError, match="confirmation"):
        clear_navigation_writer_quarantine(
            lock_path,
            all_writer_process_groups_absent=False,
        )
    assert clear_navigation_writer_quarantine(
        lock_path,
        all_writer_process_groups_absent=True,
    )
    assert not marker_path.exists()
    with navigation_writer_lock(lock_path=lock_path):
        pass


def test_uncertain_child_cleanup_retains_quarantine(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    with navigation_writer_lock(lock_path=lock_path):
        quarantine_active_writer()
    assert writer_active_path(lock_path).is_file()
    assert clear_navigation_writer_quarantine(
        lock_path,
        all_writer_process_groups_absent=True,
    )


def test_parent_crash_child_holds_flock_then_marker_still_blocks(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    reports = context.Queue()
    lock_path = tmp_path / "writer.lock"
    child_ready = tmp_path / "child.ready"
    child_release = tmp_path / "child.release"
    holder = context.Process(
        target=_crash_parent_with_inherited_lock_child,
        args=(
            str(lock_path),
            str(child_ready),
            str(child_release),
            acquired,
        ),
    )
    holder.start()
    assert acquired.wait(timeout=2)
    holder.join(timeout=2)
    assert holder.exitcode == 17
    assert writer_active_path(lock_path).is_file()

    with pytest.raises(NavigationWriterLockError, match="still active"):
        clear_navigation_writer_quarantine(
            lock_path,
            all_writer_process_groups_absent=True,
        )
    contender = context.Process(
        target=_report_quarantined_or_entered,
        args=(str(lock_path), reports),
    )
    contender.start()
    with pytest.raises(queue.Empty):
        reports.get(timeout=0.1)

    child_release.write_text("release", encoding="utf-8")
    assert reports.get(timeout=3) == "quarantined"
    contender.join(timeout=2)
    assert contender.exitcode == 0
    assert clear_navigation_writer_quarantine(
        lock_path,
        all_writer_process_groups_absent=True,
    )
    with navigation_writer_lock(lock_path=lock_path):
        pass


def test_recovery_promotion_survives_active_writer_normal_exit(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "writer.lock"
    active = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with navigation_writer_lock(lock_path=lock_path):
            active.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=writer)
    thread.start()
    assert active.wait(timeout=2)
    assert writer_active_path(lock_path).is_file()
    recovery_marker = ensure_navigation_writer_quarantine(lock_path)
    assert recovery_marker.is_file()
    release.set()
    thread.join(timeout=2)

    assert not writer_active_path(lock_path).exists()
    assert recovery_marker.is_file()
    with pytest.raises(NavigationWriterQuarantinedError):
        with navigation_writer_lock(lock_path=lock_path):
            pass
    assert clear_navigation_writer_quarantine(
        lock_path,
        all_writer_process_groups_absent=True,
    )


def test_new_recovery_event_cannot_be_cleared_by_older_action_intent(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "writer.lock"
    first = ensure_navigation_writer_quarantine(
        lock_path,
        recovery_ref="first_recovery",
    )
    old_intent = navigation_writer_marker_state(lock_path)
    second = ensure_navigation_writer_quarantine(
        lock_path,
        recovery_ref="second_recovery",
    )

    with pytest.raises(
        NavigationWriterQuarantinedError,
        match="changed",
    ):
        with navigation_writer_quarantine_clearance(
            lock_path,
            expected_marker_state_sha256=old_intent.sha256,
            all_writer_process_groups_absent=True,
        ):
            pass
    assert first.is_file()
    assert second.is_file()
    assert clear_navigation_writer_quarantine(
        lock_path,
        all_writer_process_groups_absent=True,
    )
