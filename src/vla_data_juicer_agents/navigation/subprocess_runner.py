import os
import signal
import subprocess
import time
from pathlib import Path

from vla_data_juicer_agents.core.cancellation import TurnCancelled, current_cancellation
from vla_data_juicer_agents.navigation.models import CommandRecord
from vla_data_juicer_agents.navigation.writer_lock import (
    active_writer_lock_fds,
    quarantine_active_writer,
)


OUTPUT_LIMIT = 8000


def _tail(value: str) -> str:
    return value[-OUTPUT_LIMIT:]


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(pgid):
            return
        time.sleep(0.05)

    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and _process_group_exists(pgid):
        process.poll()
        time.sleep(0.05)
    if _process_group_exists(pgid):
        raise RuntimeError("writer process group termination could not be verified")


def run_command(
    command: list[str],
    cwd: Path | None = None,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> CommandRecord:
    if dry_run:
        return CommandRecord(command=command, cwd=cwd, dry_run=True)

    cancellation = current_cancellation()
    if cancellation is not None:
        cancellation.raise_if_cancelled()

    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=active_writer_lock_fds(),
        )
        started = time.monotonic()
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancellation is not None and cancellation.cancelled:
                    _terminate_process_group(process)
                    process.communicate()
                    raise TurnCancelled("The current turn was interrupted.")
                if (
                    timeout_seconds is not None
                    and time.monotonic() - started >= timeout_seconds
                ):
                    _terminate_process_group(process)
                    stdout, stderr = process.communicate()
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout_seconds,
                        output=_tail(stdout),
                        stderr=_tail(stderr),
                    )
    except BaseException:
        if process is not None and (
            process.poll() is None or _process_group_exists(process.pid)
        ):
            try:
                _terminate_process_group(process)
                process.communicate()
            except BaseException:
                quarantine_active_writer()
        raise

    return CommandRecord(
        command=command,
        cwd=cwd,
        dry_run=False,
        return_code=process.returncode,
        stdout=_tail(stdout),
        stderr=_tail(stderr),
    )
