from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_web.sh"


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_run_web_script_help_documents_service_commands() -> None:
    result = run_script("--help")

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "start|stop|restart|status|logs|foreground" in result.stdout
    assert "VLA_VLADATASETS_ROOT" in result.stdout


def test_run_web_script_status_reports_stopped_when_pid_file_is_missing(tmp_path: Path) -> None:
    result = run_script("status", env={"STATE_DIR": str(tmp_path / ".djx")})

    assert result.returncode == 3
    assert "DataPilot web service is not running" in result.stdout
