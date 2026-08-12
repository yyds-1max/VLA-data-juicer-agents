from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from vla_data_juicer_agents.training.ssh_bootstrap import RemoteExecution
from vla_data_juicer_agents.training.worker_deployment import (
    PASSWORD_SUDO_PROBE_ARGV,
    ROOT_IDENTITY_PROBE_ARGV,
    SudoPasswordMode,
    WORKER_CENTER_CA_PATH,
    TrainingWorkerSystemDeployer,
    WorkerDeploymentRequest,
    WorkerRelease,
)
from vla_data_juicer_agents.training.worker_deployment_ssh import (
    OpenSshWorkerDeploymentBackend,
    _REMOTE_INSTALLER,
    _REMOTE_SUDO_BRIDGE,
)


ARTIFACT = b"test-only worker zip application"
SSH_PASSWORD = "ssh-test-password"
ENROLLMENT_TOKEN = "enroll_" + "x" * 48
TEST_CENTER_CA = (Path(__file__).parent / "fixtures" / "training_center_ca.pem").read_bytes()


class _FakeFixedSshSession:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes, str]] = []

    def run_probe(self, *args, **kwargs):  # pragma: no cover - protocol only
        raise AssertionError("deployment adapter must not use preflight probes")

    def run_fixed_argv(
        self,
        remote_argv: tuple[str, ...],
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        operation_name: str,
    ) -> RemoteExecution:
        self.calls.append((remote_argv, stdin_payload, operation_name))
        if remote_argv == ROOT_IDENTITY_PROBE_ARGV:
            return RemoteExecution(0, b"1000\n")
        if remote_argv == PASSWORD_SUDO_PROBE_ARGV:
            return RemoteExecution(0)
        if operation_name == "deploy:is_enrolled":
            return RemoteExecution(0, b'{"changed":false,"value":false}\n')
        if operation_name == "deploy:is_active":
            return RemoteExecution(0, b'{"changed":false,"value":true}\n')
        return RemoteExecution(0, b'{"changed":true,"value":null}\n')


def test_openssh_deployment_adapter_uses_only_fixed_installer_and_stdin_secrets() -> None:
    session = _FakeFixedSshSession()
    backend = OpenSshWorkerDeploymentBackend(
        session,  # type: ignore[arg-type]
        _ssh_password=SSH_PASSWORD,
    )
    release = WorkerRelease(
        "0.3.0",
        hashlib.sha256(ARTIFACT).hexdigest(),
        ARTIFACT,
    )

    result = TrainingWorkerSystemDeployer().deploy(
        backend,
        WorkerDeploymentRequest(
            release=release,
            center_base_url="https://datapilot.example.internal",
            node_ref="node_adapter01",
            enrollment_token=ENROLLMENT_TOKEN,
            center_ca_certificate=TEST_CENTER_CA,
            sudo_password_mode=SudoPasswordMode.SAME_AS_SSH,
        ),
    )

    assert result.service_active is True
    assert result.privilege.value == "sudo"
    assert SSH_PASSWORD not in repr(backend)
    assert ENROLLMENT_TOKEN not in repr(backend)
    assert session.calls[0] == (ROOT_IDENTITY_PROBE_ARGV, b"", "privilege_probe")
    assert session.calls[1] == (
        PASSWORD_SUDO_PROBE_ARGV,
        (SSH_PASSWORD + "\n").encode(),
        "privilege_probe",
    )
    deployment_calls = session.calls[2:]
    assert deployment_calls
    for argv, _stdin, operation_name in deployment_calls:
        assert argv[:2] == ("/usr/bin/python3", "-c")
        assert argv[4:6] == ("/usr/bin/python3", "-c")
        assert len(argv) == 10
        assert all("\n" not in value and "\r" not in value for value in argv)
        assert operation_name.startswith("deploy:")
        serialized_argv = repr(argv)
        assert SSH_PASSWORD not in serialized_argv
        assert ENROLLMENT_TOKEN not in serialized_argv
        assert ARTIFACT.decode() not in serialized_argv
        arguments = json.loads(argv[-1])
        assert isinstance(arguments, dict)
    release_call = next(call for call in deployment_calls if call[2] == "deploy:install_release")
    assert release_call[1] == (SSH_PASSWORD + "\n").encode() + ARTIFACT
    enroll_call = next(call for call in deployment_calls if call[2] == "deploy:enroll")
    assert enroll_call[1] == (SSH_PASSWORD + "\n" + ENROLLMENT_TOKEN).encode()
    assert json.loads(enroll_call[0][-1])["center_ca_path"] == WORKER_CENTER_CA_PATH
    ca_call = next(
        call
        for call in deployment_calls
        if call[2] == "deploy:write_file"
        and json.loads(call[0][-1])["path"] == WORKER_CENTER_CA_PATH
    )
    assert ca_call[1] == (SSH_PASSWORD + "\n").encode() + TEST_CENTER_CA
    backend.clear_ephemeral_credentials()
    assert SSH_PASSWORD not in repr(backend)


def test_openssh_backend_rejects_non_catalogue_privilege_command() -> None:
    backend = OpenSshWorkerDeploymentBackend(_FakeFixedSshSession())  # type: ignore[arg-type]

    try:
        backend.run_fixed_privilege_probe(
            ("/bin/sh", "-c", "id"),
            stdin_secret=None,
            timeout_seconds=10,
        )
    except ValueError as exc:
        assert "non-catalogue" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("non-catalogue command was accepted")


def test_remote_sudo_bridge_keeps_password_separate_from_installer_payload(
    tmp_path: Path,
) -> None:
    fake_sudo = tmp_path / "sudo"
    fake_sudo.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[1:]
password = subprocess.run(
    [os.environ["SUDO_ASKPASS"]],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=os.environ,
    check=False,
)
if password.returncode != 0 or password.stdout != b"sudo-secret\\n":
    raise SystemExit(1)
separator = arguments.index("--")
completed = subprocess.run(
    arguments[separator + 1:],
    input=sys.stdin.buffer.read(),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
sys.stdout.buffer.write(completed.stdout)
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(completed.returncode)
""",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o700)
    bridge = _REMOTE_SUDO_BRIDGE.replace("/usr/bin/sudo", str(fake_sudo))
    payload = b"worker-artifact-binary-payload"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            bridge,
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        input=b"sudo-secret\n" + payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert completed.stdout == payload
    assert b"sudo-secret" not in completed.stdout + completed.stderr


def test_remote_enrollment_suppresses_worker_stdout_from_protocol(
    tmp_path: Path,
) -> None:
    fake_runuser = tmp_path / "runuser"
    fake_runuser.write_text(
        """#!/usr/bin/env python3
import subprocess
import sys

separator = sys.argv.index("--")
completed = subprocess.run(
    sys.argv[separator + 1:],
    input=sys.stdin.buffer.read(),
    stdout=sys.stdout.buffer,
    stderr=sys.stderr.buffer,
    check=False,
)
raise SystemExit(completed.returncode)
""",
        encoding="utf-8",
    )
    fake_runuser.chmod(0o700)
    noisy_worker = tmp_path / "worker.py"
    noisy_worker.write_text(
        "import sys\nsys.stdin.buffer.read()\nprint('{\"worker\":\"noise\"}')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _REMOTE_INSTALLER,
            "enroll",
            json.dumps(
                {
                    "artifact_path": str(noisy_worker),
                    "state_directory": str(tmp_path / "state"),
                    "center_base_url": "https://127.0.0.1:8777",
                    "node_ref": "node_test1234",
                    "center_ca_path": None,
                    "run_as": "datapilot-worker",
                }
            ),
        ],
        input=b"enroll_" + b"x" * 48,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"changed": True, "value": None}
    assert b"worker" not in completed.stdout
