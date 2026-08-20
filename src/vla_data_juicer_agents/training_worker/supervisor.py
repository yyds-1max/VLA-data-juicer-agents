from __future__ import annotations

"""Small, node-local training process supervisor.

The Worker launches this module in a separate session.  It keeps the training
process alive across Worker restarts and leaves an atomic exit record that a
new Worker instance can reconcile without guessing from a recycled PID.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Mapping

import fcntl


def run_supervisor(spec_path: Path) -> int:
    spec = _read_spec(spec_path)
    argv = spec["argv"]
    working_directory = spec["working_directory"]
    log_path = Path(spec["log_path"])
    state_path = Path(spec["state_path"])
    commit_path = Path(spec["commit_path"])
    lock_path = Path(spec["lock_path"])
    launch_token = str(spec["launch_token"])
    environment = spec["environment"]
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return 73

    stopping = False

    def wait_signal(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, wait_signal)
    signal.signal(signal.SIGINT, wait_signal)
    _write_state(
        state_path,
        {
            "contract": "datapilot_training_supervisor_v1",
            "status": "waiting",
            "launch_token": launch_token,
            "supervisor_pid": os.getpid(),
            "child_pid": None,
            "exit_code": None,
        },
    )
    decision: str | None = None
    while not stopping and decision is None:
        decision = _launch_decision(commit_path, launch_token)
        if decision is not None:
            break
        time.sleep(0.05)
    if stopping or decision == "cancel":
        os.close(lock_fd)
        return 143 if stopping else 0

    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log_stream:
        process = subprocess.Popen(
            argv,
            cwd=working_directory,
            env={**os.environ, **environment},
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
        _write_state(
            state_path,
            {
                "contract": "datapilot_training_supervisor_v1",
                "status": "running",
                "launch_token": launch_token,
                "supervisor_pid": os.getpid(),
                "child_pid": process.pid,
                "exit_code": None,
            },
        )

        def forward(signum: int, _frame: object) -> None:
            nonlocal stopping
            if stopping:
                return
            stopping = True
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        return_code = process.wait()
    _write_state(
        state_path,
        {
            "contract": "datapilot_training_supervisor_v1",
            "status": "exited",
            "launch_token": launch_token,
            "supervisor_pid": os.getpid(),
            "child_pid": process.pid,
            "exit_code": return_code,
        },
    )
    os.close(lock_fd)
    return 0


def _read_spec(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError("invalid supervisor specification")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("invalid supervisor specification")
    argv = raw.get("argv")
    working_directory = raw.get("working_directory")
    log_path = raw.get("log_path")
    state_path = raw.get("state_path")
    commit_path = raw.get("commit_path")
    lock_path = raw.get("lock_path")
    launch_token = raw.get("launch_token")
    environment = raw.get("environment")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or not isinstance(working_directory, str)
        or not isinstance(log_path, str)
        or not isinstance(state_path, str)
        or not isinstance(commit_path, str)
        or not isinstance(lock_path, str)
        or not isinstance(launch_token, str)
        or not launch_token
        or not isinstance(environment, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items())
    ):
        raise RuntimeError("invalid supervisor specification")
    return {
        "argv": argv,
        "working_directory": working_directory,
        "log_path": log_path,
        "state_path": state_path,
        "commit_path": commit_path,
        "lock_path": lock_path,
        "launch_token": launch_token,
        "environment": environment,
    }


def _launch_decision(path: Path, launch_token: str) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("launch_token") != launch_token:
        return None
    return "cancel" if payload.get("decision") == "cancel" else "commit"


def _write_state(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 1:
        raise SystemExit("usage: training-supervisor SPEC_PATH")
    return run_supervisor(Path(values[0]))


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
