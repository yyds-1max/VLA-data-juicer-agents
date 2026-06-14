import subprocess
from pathlib import Path

from vla_data_juicer_agents.navigation.models import CommandRecord


OUTPUT_LIMIT = 8000


def _tail(value: str) -> str:
    return value[-OUTPUT_LIMIT:]


def run_command(
    command: list[str],
    cwd: Path | None = None,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> CommandRecord:
    if dry_run:
        return CommandRecord(command=command, cwd=cwd, dry_run=True)

    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandRecord(
        command=command,
        cwd=cwd,
        dry_run=False,
        return_code=completed.returncode,
        stdout=_tail(completed.stdout),
        stderr=_tail(completed.stderr),
    )
