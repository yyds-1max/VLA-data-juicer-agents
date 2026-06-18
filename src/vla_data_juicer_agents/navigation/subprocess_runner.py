import os
import signal
import subprocess
import time
from pathlib import Path

from vla_data_juicer_agents.core.cancellation import TurnCancelled, current_cancellation
from vla_data_juicer_agents.navigation.models import CommandRecord


OUTPUT_LIMIT = 8000


def _tail(value: str) -> str:
    return value[-OUTPUT_LIMIT:]


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=1.0)
    except ProcessLookupError:
        return


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

    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
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
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                _terminate_process_group(process)
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=_tail(stdout),
                    stderr=_tail(stderr),
                )

    return CommandRecord(
        command=command,
        cwd=cwd,
        dry_run=False,
        return_code=process.returncode,
        stdout=_tail(stdout),
        stderr=_tail(stderr),
    )
