from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_web.sh"
CONTROL_HELPER = ROOT / "scripts" / "run_web_control.py"
ECHO_COMMAND = shutil.which("echo")
TRUE_COMMAND = shutil.which("true")
assert ECHO_COMMAND is not None
assert TRUE_COMMAND is not None
SCRIPT_ENV_KEYS = (
    "STATE_DIR",
    "WORKING_DIR",
    "VLA_DATA_AGENT_WEB_WORKING_DIR",
    "PID_FILE",
    "LOG_DIR",
    "LOG_FILE",
    "SKIP_FRONTEND_BUILD",
    "VLA_FRONTEND_NODE_BIN_DIR",
    "WEB_CMD",
    "RUN_WEB_CONTROL_PYTHON",
    "VLA_RUN_WEB_CONTROL_LOCKED",
    "VLA_RUN_WEB_LOCKED_PID_PATH",
    "VLA_RUN_WEB_LOCKED_PARENT_IDENTITY",
    "VLA_RUN_WEB_ANCHOR_FD",
    "VLA_RUN_WEB_PARENT_FD",
    "VLA_RUN_WEB_CONTROL_FD",
)


def script_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    merged_env = os.environ.copy()
    for name in SCRIPT_ENV_KEYS:
        merged_env.pop(name, None)
    if env:
        merged_env.update(env)
    return merged_env


def run_script(
    *args: str,
    env: dict[str, str] | None = None,
    start_new_session: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=script_environment(env),
        text=True,
        capture_output=True,
        check=False,
        start_new_session=start_new_session,
    )


def write_fake_frontend_toolchain(
    bin_dir: Path,
    *,
    node_version: str = "24.18.0",
    npm_version: str = "11.16.0",
) -> None:
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'v{node_version}'\n",
        encoding="utf-8",
    )
    node.chmod(0o700)
    npm = bin_dir / "npm"
    npm.write_text(
        (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"--version\" ]; then\n"
            f"  printf '%s\\n' '{npm_version}'\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s|%s\\n' \"$PWD\" \"$*\" >> \"${FAKE_NPM_LOG}\"\n"
        ),
        encoding="utf-8",
    )
    npm.chmod(0o700)


def test_run_web_script_help_documents_service_commands() -> None:
    result = run_script("--help")

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "start|stop|restart|status|logs|foreground" in result.stdout
    assert "VLA_VLADATASETS_ROOT" in result.stdout
    assert "VLA_DATA_AGENT_WEB_WORKING_DIR" in result.stdout
    assert "VLA_FRONTEND_NODE_BIN_DIR" in result.stdout


def test_frontend_package_pins_supported_node_and_npm() -> None:
    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8"),
    )

    assert (ROOT / ".nvmrc").read_text(encoding="ascii") == "24.18.0\n"
    assert package["packageManager"] == "npm@11.16.0"
    assert package["engines"] == {
        "node": "24.18.0",
        "npm": "11.16.0",
    }
    assert (
        ROOT / "frontend" / ".npmrc"
    ).read_text(encoding="ascii") == "engine-strict=true\n"


def test_run_web_script_builds_with_configured_frontend_toolchain(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "node-bin"
    npm_log = tmp_path / "npm.log"
    write_fake_frontend_toolchain(bin_dir)

    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(tmp_path / "working"),
            "VLA_FRONTEND_NODE_BIN_DIR": str(bin_dir),
            "FAKE_NPM_LOG": str(npm_log),
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert npm_log.read_text(encoding="utf-8") == (
        f"{ROOT / 'frontend'}|run build\n"
    )


def test_run_web_script_rejects_frontend_toolchain_version_mismatch(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "node-bin"
    npm_log = tmp_path / "npm.log"
    write_fake_frontend_toolchain(
        bin_dir,
        node_version="20.20.2",
        npm_version="10.8.2",
    )

    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(tmp_path / "working"),
            "VLA_FRONTEND_NODE_BIN_DIR": str(bin_dir),
            "FAKE_NPM_LOG": str(npm_log),
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 2
    assert "Frontend build toolchain mismatch" in result.stderr
    assert "Required: Node.js 24.18.0, npm 11.16.0" in result.stderr
    assert "Detected: Node.js 20.20.2, npm 10.8.2" in result.stderr
    assert not npm_log.exists()


def test_run_web_script_skip_build_does_not_require_node(
    tmp_path: Path,
) -> None:
    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(tmp_path / "working"),
            "SKIP_FRONTEND_BUILD": "1",
            "VLA_FRONTEND_NODE_BIN_DIR": str(tmp_path / "missing-node-bin"),
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Skipping frontend build" in result.stdout


def test_run_web_script_requires_absolute_frontend_toolchain_directory(
    tmp_path: Path,
) -> None:
    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(tmp_path / "working"),
            "VLA_FRONTEND_NODE_BIN_DIR": "relative/node-bin",
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 2
    assert "must be an absolute directory" in result.stderr


def test_run_web_script_status_reports_stopped_when_pid_file_is_missing(tmp_path: Path) -> None:
    result = run_script("status", env={"STATE_DIR": str(tmp_path / ".djx")})

    assert result.returncode == 3
    assert "DataPilot web service is not running" in result.stdout


def test_run_web_script_uses_web_working_dir_environment_value(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "web-working"
    state_dir = tmp_path / "state"
    result = run_script(
        "foreground",
        env={
            "STATE_DIR": str(state_dir),
            "VLA_DATA_AGENT_WEB_WORKING_DIR": str(working_dir),
            "SKIP_FRONTEND_BUILD": "1",
            "WEB_CMD": ECHO_COMMAND,
        },
    )

    assert result.returncode == 0
    assert f"--working-dir {working_dir}" in result.stdout


def test_run_web_script_rejects_conflicting_working_directory_values_for_startup(
    tmp_path: Path,
) -> None:
    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(tmp_path / "script-working"),
            "VLA_DATA_AGENT_WEB_WORKING_DIR": str(tmp_path / "web-working"),
            "SKIP_FRONTEND_BUILD": "1",
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 2
    assert (
        "WORKING_DIR and VLA_DATA_AGENT_WEB_WORKING_DIR must match"
        in result.stderr
    )


def test_run_web_script_control_commands_ignore_stale_working_dir_conflict(
    tmp_path: Path,
) -> None:
    result = run_script(
        "status",
        env={
            "STATE_DIR": str(tmp_path / "state"),
            "WORKING_DIR": str(tmp_path / "script-working"),
            "VLA_DATA_AGENT_WEB_WORKING_DIR": str(tmp_path / "web-working"),
        },
    )

    assert result.returncode == 3
    assert "DataPilot web service is not running" in result.stdout
    assert "must match" not in result.stderr


def test_run_web_script_creates_private_directories_without_chmod_existing_ones(
    tmp_path: Path,
) -> None:
    new_working = tmp_path / "new-working"
    new_state = tmp_path / "new-state"
    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(new_working),
            "STATE_DIR": str(new_state),
            "SKIP_FRONTEND_BUILD": "1",
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 0
    for path in (new_working, new_state, new_state / "logs"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700

    existing_working = tmp_path / "existing-working"
    existing_state = tmp_path / "existing-state"
    existing_log = existing_state / "logs"
    existing_working.mkdir()
    existing_log.mkdir(parents=True)
    for path in (existing_working, existing_state, existing_log):
        path.chmod(0o775)

    result = run_script(
        "foreground",
        env={
            "WORKING_DIR": str(existing_working),
            "STATE_DIR": str(existing_state),
            "SKIP_FRONTEND_BUILD": "1",
            "WEB_CMD": TRUE_COMMAND,
        },
    )

    assert result.returncode == 0
    for path in (existing_working, existing_state, existing_log):
        assert stat.S_IMODE(path.stat().st_mode) == 0o775


@pytest.mark.parametrize(
    "payload",
    (
        "",
        "0\n",
        "1\n",
        "-1\n",
        "02\n",
        "2  \n",
        "2\n3\n",
        "not-a-pid\n",
    ),
)
def test_run_web_script_never_signals_noncanonical_pid_content(
    tmp_path: Path,
    payload: str,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    pid_file.write_text(payload, encoding="ascii")

    result = run_script(
        "stop",
        env={
            "STATE_DIR": str(state_dir),
            "PID_FILE": str(pid_file),
        },
        # If a regression executes ``kill 0``, contain it to this subprocess
        # process group instead of the test runner's group.
        start_new_session=True,
    )

    assert result.returncode == 2
    assert "canonical decimal PID greater than 1" in result.stderr
    assert pid_file.read_text(encoding="ascii") == payload


def test_run_web_script_treats_canonical_pid_without_instance_lock_as_stale(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")

    result = run_script(
        "status",
        env={
            "STATE_DIR": str(state_dir),
            "PID_FILE": str(pid_file),
        },
    )

    assert result.returncode == 1
    assert "Stale PID file" in result.stdout


def test_run_web_script_does_not_signal_reused_stale_pid(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        pid_file.write_text(f"{unrelated.pid}\n", encoding="ascii")

        result = run_script(
            "stop",
            env={
                "STATE_DIR": str(state_dir),
                "PID_FILE": str(pid_file),
            },
        )

        assert result.returncode == 0, (result.stdout, result.stderr)
        assert unrelated.poll() is None
        assert not pid_file.exists()
        assert "Removing stale PID record" in result.stdout
    finally:
        if unrelated.poll() is None:
            unrelated.terminate()
        unrelated.wait(timeout=5)


def test_inherited_old_instance_lock_cannot_authorize_reused_pid_or_new_start(
    tmp_path: Path,
) -> None:
    if sys.platform.startswith("linux"):
        stale_birth = (
            "linux:00000000-0000-0000-0000-000000000000:0"
        )
    elif sys.platform == "darwin":
        stale_birth = "darwin:0:0"
    else:
        pytest.skip("stable process birth identity is platform-specific")

    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    identity_file = state_dir / "web.pid.instance"
    identity_ready = tmp_path / "identity-ready"
    web_started = tmp_path / "web-started"
    fake_web = tmp_path / "fake-web"
    fake_web.write_text(
        (
            "#!/usr/bin/env bash\n"
            f"printf started > {str(web_started)!r}\n"
        ),
        encoding="utf-8",
    )
    fake_web.chmod(0o700)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    holder: subprocess.Popen[str] | None = None
    try:
        pid_file.write_text(f"{unrelated.pid}\n", encoding="ascii")
        identity_payload = (
            f"v2 {unrelated.pid} {stale_birth} {'a' * 64}\n"
        )
        identity_file.write_text(identity_payload, encoding="ascii")
        holder_code = (
            "import fcntl,pathlib,time;"
            f"identity=pathlib.Path({str(identity_file)!r});"
            f"ready=pathlib.Path({str(identity_ready)!r});"
            "handle=identity.open('r+');"
            "fcntl.flock(handle.fileno(),fcntl.LOCK_EX);"
            "ready.write_text('ready');"
            "time.sleep(60)"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code],
        )
        deadline = time.monotonic() + 5
        while not identity_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert identity_ready.exists()

        start_result = run_script(
            "start",
            env={
                "STATE_DIR": str(state_dir),
                "WORKING_DIR": str(tmp_path / "working"),
                "PID_FILE": str(pid_file),
                "SKIP_FRONTEND_BUILD": "1",
                "WEB_CMD": str(fake_web),
            },
        )

        assert start_result.returncode == 4
        assert "start is blocked" in start_result.stderr
        assert unrelated.poll() is None
        assert holder.poll() is None
        assert not web_started.exists()
        assert pid_file.read_text(encoding="ascii") == f"{unrelated.pid}\n"
        assert identity_file.read_text(encoding="ascii") == identity_payload

        stop_result = run_script(
            "stop",
            env={
                "STATE_DIR": str(state_dir),
                "PID_FILE": str(pid_file),
            },
        )

        assert stop_result.returncode == 1
        assert unrelated.poll() is None
        assert holder.poll() is None
        assert pid_file.read_text(encoding="ascii") == f"{unrelated.pid}\n"
        assert identity_file.read_text(encoding="ascii") == identity_payload
    finally:
        for process in (holder, unrelated):
            if process is not None and process.poll() is None:
                process.terminate()
            if process is not None:
                process.wait(timeout=5)


def test_locked_orphan_instance_record_blocks_start_before_spawn(
    tmp_path: Path,
) -> None:
    if sys.platform.startswith("linux"):
        recorded_birth = (
            "linux:00000000-0000-0000-0000-000000000000:0"
        )
    elif sys.platform == "darwin":
        recorded_birth = "darwin:0:0"
    else:
        pytest.skip("stable process birth identity is platform-specific")

    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    identity_file = state_dir / "web.pid.instance"
    identity_ready = tmp_path / "identity-ready"
    web_started = tmp_path / "web-started"
    fake_web = tmp_path / "fake-web"
    fake_web.write_text(
        (
            "#!/usr/bin/env bash\n"
            f"printf started > {str(web_started)!r}\n"
        ),
        encoding="utf-8",
    )
    fake_web.chmod(0o700)
    holder_code = (
        "import fcntl,os,pathlib,time;"
        f"identity=pathlib.Path({str(identity_file)!r});"
        f"ready=pathlib.Path({str(identity_ready)!r});"
        f"birth={recorded_birth!r};"
        "identity.write_text("
        "f\"v2 {os.getpid()} {birth} {'b' * 64}\\n\",encoding='ascii');"
        "handle=identity.open('r+');"
        "fcntl.flock(handle.fileno(),fcntl.LOCK_EX);"
        "ready.write_text('ready');"
        "time.sleep(60)"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_code])
    try:
        deadline = time.monotonic() + 5
        while not identity_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert identity_ready.exists()
        identity_payload = identity_file.read_text(encoding="ascii")

        result = run_script(
            "start",
            env={
                "STATE_DIR": str(state_dir),
                "WORKING_DIR": str(tmp_path / "working"),
                "PID_FILE": str(pid_file),
                "SKIP_FRONTEND_BUILD": "1",
                "WEB_CMD": str(fake_web),
            },
        )

        assert result.returncode == 4
        assert "start is blocked" in result.stderr
        assert holder.poll() is None
        assert not web_started.exists()
        assert not pid_file.exists()
        assert identity_file.read_text(encoding="ascii") == identity_payload
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.wait(timeout=5)


def test_run_web_script_rejects_unsafe_pid_parent_and_file(
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "unsafe-state"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    unsafe_parent_pid = unsafe_parent / "web.pid"
    unsafe_parent_pid.write_text(f"{os.getpid()}\n", encoding="ascii")

    parent_result = run_script(
        "status",
        env={
            "STATE_DIR": str(unsafe_parent),
            "PID_FILE": str(unsafe_parent_pid),
        },
    )

    assert parent_result.returncode == 2
    assert "owner-controlled directory" in parent_result.stderr

    safe_parent = tmp_path / "safe-state"
    safe_parent.mkdir(mode=0o700)
    unsafe_pid_file = safe_parent / "web.pid"
    unsafe_pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")
    unsafe_pid_file.chmod(0o666)

    file_result = run_script(
        "status",
        env={
            "STATE_DIR": str(safe_parent),
            "PID_FILE": str(unsafe_pid_file),
        },
    )

    assert file_result.returncode == 2
    assert "owner-controlled regular file" in file_result.stderr

    fifo_pid_file = safe_parent / "web-fifo.pid"
    os.mkfifo(fifo_pid_file, mode=0o600)
    fifo_result = run_script(
        "status",
        env={
            "STATE_DIR": str(safe_parent),
            "PID_FILE": str(fifo_pid_file),
        },
    )

    assert fifo_result.returncode == 2
    assert "owner-controlled regular file" in fifo_result.stderr


def test_run_web_script_rejects_pid_file_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text(f"{os.getpid()}\n", encoding="ascii")
    pid_file = state_dir / "web.pid"
    pid_file.symlink_to(target)

    result = run_script(
        "status",
        env={
            "STATE_DIR": str(state_dir),
            "PID_FILE": str(pid_file),
        },
    )

    assert result.returncode == 2
    assert "PID file is unavailable" in result.stderr
    assert target.read_text(encoding="ascii") == f"{os.getpid()}\n"


def test_run_web_control_removes_only_the_expected_pid(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    pid_file.write_text("222\n", encoding="ascii")

    changed = subprocess.run(
        [
            sys.executable,
            str(CONTROL_HELPER),
            "remove-pid",
            "--pid-file",
            str(pid_file),
            "--expected-pid",
            "333",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert changed.returncode == 4
    assert pid_file.read_text(encoding="ascii") == "222\n"

    removed = subprocess.run(
        [
            sys.executable,
            str(CONTROL_HELPER),
            "remove-pid",
            "--pid-file",
            str(pid_file),
            "--expected-pid",
            "222",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert removed.returncode == 0
    assert not pid_file.exists()


def test_run_web_script_serializes_concurrent_starts(
    tmp_path: Path,
) -> None:
    fake_web = tmp_path / "fake-web"
    fake_web.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$$" >> "${FAKE_WEB_STARTS}"
trap 'exit 0' TERM INT
while true; do
  sleep 0.05
done
""",
        encoding="utf-8",
    )
    fake_web.chmod(0o700)
    state_dir = tmp_path / "state"
    working_dir = tmp_path / "working"
    pid_file = state_dir / "web.pid"
    starts_file = tmp_path / "starts"
    environment = script_environment(
        {
            "STATE_DIR": str(state_dir),
            "WORKING_DIR": str(working_dir),
            "PID_FILE": str(pid_file),
            "SKIP_FRONTEND_BUILD": "1",
            "WEB_CMD": str(fake_web),
            "FAKE_WEB_STARTS": str(starts_file),
        },
    )
    command = ["bash", str(SCRIPT), "start"]
    first = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    service_pid: int | None = None
    try:
        first_stdout, first_stderr = first.communicate(timeout=15)
        second_stdout, second_stderr = second.communicate(timeout=15)
        assert first.returncode == 0, (first_stdout, first_stderr)
        assert second.returncode == 0, (second_stdout, second_stderr)
        starts_deadline = time.monotonic() + 3
        while not starts_file.exists() and time.monotonic() < starts_deadline:
            time.sleep(0.01)
        assert starts_file.exists(), (
            first_stdout,
            first_stderr,
            second_stdout,
            second_stderr,
        )
        assert len(starts_file.read_text(encoding="ascii").splitlines()) == 1
        assert pid_file.read_text(encoding="ascii").strip().isdigit()
        service_pid = int(pid_file.read_text(encoding="ascii").strip())
        assert service_pid > 1
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(pid_file.stat().st_mode) == 0o600
        assert (
            stat.S_IMODE((state_dir / "web.pid.control.lock").stat().st_mode)
            == 0o600
        )
        assert (
            stat.S_IMODE((state_dir / "web.pid.instance").stat().st_mode)
            == 0o600
        )

        stopped = run_script(
            "stop",
            env={
                "STATE_DIR": str(state_dir),
                "WORKING_DIR": str(working_dir),
                "PID_FILE": str(pid_file),
                "SKIP_FRONTEND_BUILD": "1",
                "WEB_CMD": str(fake_web),
                "FAKE_WEB_STARTS": str(starts_file),
            },
        )
        assert stopped.returncode == 0, (stopped.stdout, stopped.stderr)
        assert not pid_file.exists()
        service_pid = None
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        if service_pid is not None:
            try:
                os.kill(service_pid, 15)
            except ProcessLookupError:
                pass


def test_run_web_control_anchor_serializes_across_pid_parent_rotation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    pid_file = state_dir / "web.pid"
    ready = tmp_path / "first-ready"
    release = tmp_path / "release-first"
    second_entered = tmp_path / "second-entered"
    first_code = (
        "import pathlib,time;"
        f"ready=pathlib.Path({str(ready)!r});"
        f"release=pathlib.Path({str(release)!r});"
        "ready.write_text('ready');"
        "\nwhile not release.exists(): time.sleep(0.01)"
    )
    second_code = (
        "import pathlib;"
        f"pathlib.Path({str(second_entered)!r}).write_text('entered')"
    )
    first = subprocess.Popen(
        [
            sys.executable,
            str(CONTROL_HELPER),
            "with-lock",
            "--pid-file",
            str(pid_file),
            "--",
            sys.executable,
            "-c",
            first_code,
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()

        rotated = tmp_path / "state-rotated"
        state_dir.rename(rotated)
        state_dir.mkdir(mode=0o700)
        second = subprocess.Popen(
            [
                sys.executable,
                str(CONTROL_HELPER),
                "with-lock",
                "--pid-file",
                str(pid_file),
                "--",
                sys.executable,
                "-c",
                second_code,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.25)
        assert not second_entered.exists()

        release.write_text("release", encoding="ascii")
        first_stdout, first_stderr = first.communicate(timeout=5)
        second_stdout, second_stderr = second.communicate(timeout=5)
        assert first.returncode == 0, (first_stdout, first_stderr)
        assert second.returncode == 0, (second_stdout, second_stderr)
        assert second_entered.read_text(encoding="ascii") == "entered"
    finally:
        release.touch(exist_ok=True)
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
        if second is not None and second.poll() is None:
            second.terminate()
            second.wait(timeout=5)


def test_run_web_script_rejects_unlocked_internal_control_action(
    tmp_path: Path,
) -> None:
    result = run_script(
        "__control_stop",
        env={"STATE_DIR": str(tmp_path / "state")},
    )

    assert result.returncode == 2
    assert "require the control lock" in result.stderr


def test_run_web_script_rejects_forged_locked_environment(
    tmp_path: Path,
) -> None:
    result = run_script(
        "__control_stop",
        env={
            "STATE_DIR": str(tmp_path / "state"),
            "VLA_RUN_WEB_CONTROL_LOCKED": "1",
        },
    )

    assert result.returncode == 2
    assert "lock capability is unavailable" in result.stderr
