from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from vla_data_juicer_agents.training.worker_deployment import (
    DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
    PASSWORDLESS_SUDO_PROBE_ARGV,
    PASSWORD_SUDO_PROBE_ARGV,
    ROOT_IDENTITY_PROBE_ARGV,
    WORKER_CENTER_CA_PATH,
    WORKER_CONFIG_ROOT,
    WORKER_CURRENT_LINK,
    WORKER_ENVIRONMENT_PATH,
    WORKER_OPT_ROOT,
    WORKER_STATE_ROOT,
    WORKER_SYSTEMD_UNIT_PATH,
    DeploymentPrivilege,
    FixedCommandResult,
    RuntimeIdentity,
    SudoPasswordMode,
    TrainingNodeDeploymentError,
    TrainingWorkerSystemDeployer,
    TrainingWorkerSystemRemover,
    WorkerDeploymentRequest,
    WorkerRemovalRequest,
    WorkerRelease,
    build_systemd_unit,
    inspect_deployment_privilege,
    system_directories,
)
from vla_data_juicer_agents.training.worker_deployment_ssh import (
    _REMOTE_INSTALLER,
)


ARTIFACT = b"test-only executable worker artifact"
DIGEST = hashlib.sha256(ARTIFACT).hexdigest()
ENROLLMENT_TOKEN = "enroll_" + "e" * 48
TEST_CENTER_CA = (Path(__file__).parent / "fixtures" / "training_center_ca.pem").read_bytes()


def _request(**overrides: object) -> WorkerDeploymentRequest:
    values: dict[str, object] = {
        "release": WorkerRelease("0.2.0", DIGEST, ARTIFACT),
        "center_base_url": "https://datapilot.example.internal",
        "node_ref": "node_test0001",
        "enrollment_token": ENROLLMENT_TOKEN,
        "sudo_password_mode": SudoPasswordMode.SAME_AS_SSH,
    }
    values.update(overrides)
    return WorkerDeploymentRequest(**values)  # type: ignore[arg-type]


class _FakeDeploymentBackend:
    def __init__(self, privilege: DeploymentPrivilege) -> None:
        self.privilege = privilege
        self.calls: list[tuple[str, object]] = []
        self.created: set[str] = set()
        self.enrolled = False
        self.active = False
        self.observed_sudo_password: str | None = None
        self.runtime_identity = RuntimeIdentity(
            username="trainer",
            primary_group="research",
            uid=1000,
            home_directory="/home/trainer",
            conda_executable="/home/trainer/miniconda3/bin/conda",
        )
        self.legacy_account = True

    def inspect_runtime_identity(self):
        self.calls.append(("inspect_runtime_identity", None))
        return self.runtime_identity

    def inspect_privilege(
        self,
        *,
        sudo_password_mode: SudoPasswordMode,
        sudo_password: str | None,
    ) -> DeploymentPrivilege:
        self.calls.append(("inspect_privilege", sudo_password_mode))
        self.observed_sudo_password = sudo_password
        return self.privilege

    def _ensure(self, key: str) -> bool:
        changed = key not in self.created
        self.created.add(key)
        return changed

    def ensure_directory(self, spec, *, privilege):
        self.calls.append(("ensure_directory", spec))
        return self._ensure("directory:" + spec.path)

    def install_release(self, release, *, owner, group, mode, privilege):
        self.calls.append(("install_release", (release.version, owner, group, mode)))
        return self._ensure("release:" + release.version)

    def write_managed_file(self, spec, content, *, privilege):
        self.calls.append(("write_managed_file", (spec, content)))
        return self._ensure("file:" + spec.path + ":" + hashlib.sha256(content).hexdigest())

    def activate_release(self, *, release_directory, current_link, privilege):
        self.calls.append(("activate_release", (release_directory, current_link)))
        return self._ensure("active:" + release_directory)

    def worker_is_enrolled(self, *, privilege):
        self.calls.append(("worker_is_enrolled", None))
        return self.enrolled

    def enroll_worker(self, **kwargs):
        token = kwargs.pop("enrollment_token")
        self.calls.append(("enroll_worker", kwargs))
        assert token == ENROLLMENT_TOKEN
        self.enrolled = True
        return True

    def reload_enable_and_start_system_service(self, unit_name, *, privilege):
        self.calls.append(("start_system_service", unit_name))
        changed = not self.active
        self.active = True
        return changed

    def system_service_is_active(self, unit_name, *, privilege):
        self.calls.append(("system_service_is_active", unit_name))
        return self.active

    def remove_system_worker(self, *, privilege):
        self.calls.append(("remove_system_worker", privilege))
        changed = bool(self.created or self.enrolled or self.active)
        self.created.clear()
        self.enrolled = False
        self.active = False
        return changed

    def remove_legacy_service_account(self, *, privilege):
        self.calls.append(("remove_legacy_service_account", privilege))
        changed = self.legacy_account
        self.legacy_account = False
        return changed


def test_system_deployment_is_fixed_complete_and_idempotent() -> None:
    backend = _FakeDeploymentBackend(DeploymentPrivilege.PASSWORDLESS_SUDO)
    deployer = TrainingWorkerSystemDeployer()

    first = deployer.deploy(backend, _request())
    first_call_count = len(backend.calls)
    second = deployer.deploy(backend, _request(enrollment_token=None))

    assert first.runtime_account == "trainer"
    assert first.artifact_path.startswith(WORKER_OPT_ROOT + "/releases/0.2.0-")
    assert DIGEST[:12] in first.artifact_path
    assert first.systemd_unit == WORKER_SYSTEMD_UNIT_PATH
    assert first.service_active is True
    assert "legacy_service_account" in first.changed_steps
    assert "legacy_service_account" in second.unchanged_steps
    assert "enrollment" in second.unchanged_steps
    assert len(backend.calls) > first_call_count

    directory_specs = [call[1] for call in backend.calls if call[0] == "ensure_directory"]
    directories = system_directories(backend.runtime_identity)
    assert directory_specs[: len(directories)] == list(directories)
    files = [call[1] for call in backend.calls if call[0] == "write_managed_file"]
    first_file_specs = {spec.path: (spec, content) for spec, content in files[:2]}
    assert first_file_specs[WORKER_ENVIRONMENT_PATH][0].owner == "root"
    assert first_file_specs[WORKER_ENVIRONMENT_PATH][0].mode == 0o640
    assert ENROLLMENT_TOKEN.encode() not in first_file_specs[WORKER_ENVIRONMENT_PATH][1]
    unit = build_systemd_unit(backend.runtime_identity)
    assert first_file_specs[WORKER_SYSTEMD_UNIT_PATH][1] == unit.encode()
    assert "User=trainer" in unit
    assert "Group=research" in unit
    assert "ProtectSystem=strict" not in unit
    assert "ProtectHome=true" not in unit
    assert WORKER_STATE_ROOT in unit
    assert WORKER_CONFIG_ROOT in WORKER_ENVIRONMENT_PATH
    assert WORKER_CURRENT_LINK in unit
    environment = first_file_specs[WORKER_ENVIRONMENT_PATH][1]
    assert b'DATAPILOT_CONDA_EXECUTABLE="/home/trainer/miniconda3/bin/conda"' in environment


def test_remote_installer_restarts_an_already_active_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, ...]] = []

    def fixed_systemctl(command: list[str], **_kwargs: object) -> object:
        calls.append(tuple(command))

        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr(subprocess, "run", fixed_systemctl)
    monkeypatch.setattr(
        sys,
        "argv",
        ["remote-installer", "start_service", json.dumps({"unit_name": "datapilot-training-worker.service"})],
    )

    exec(compile(_REMOTE_INSTALLER, "<remote-installer>", "exec"), {})

    assert ("/usr/bin/systemctl", "daemon-reload") in calls
    assert (
        "/usr/bin/systemctl",
        "restart",
        "datapilot-training-worker.service",
    ) in calls
    assert not any("enable --now" in " ".join(call) for call in calls)
    assert json.loads(capsys.readouterr().out) == {"changed": True, "value": None}


def test_system_worker_removal_is_fixed_idempotent_and_requires_privilege() -> None:
    backend = _FakeDeploymentBackend(DeploymentPrivilege.SUDO)
    TrainingWorkerSystemDeployer().deploy(backend, _request())
    remover = TrainingWorkerSystemRemover()
    request = WorkerRemovalRequest(
        node_ref="node_test0001",
        sudo_password_mode=SudoPasswordMode.SAME_AS_SSH,
    )

    first = remover.remove(backend, request)
    second = remover.remove(backend, request)

    assert first.removed is True
    assert second.removed is False
    assert backend.active is False
    assert backend.enrolled is False
    assert [call[0] for call in backend.calls].count("remove_system_worker") == 2

    insufficient = _FakeDeploymentBackend(DeploymentPrivilege.INSUFFICIENT)
    with pytest.raises(TrainingNodeDeploymentError) as raised:
        remover.remove(insufficient, request)
    assert raised.value.code == DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE
    assert insufficient.calls == [
        ("inspect_privilege", SudoPasswordMode.SAME_AS_SSH)
    ]


def test_insufficient_deployment_account_fails_before_any_write() -> None:
    backend = _FakeDeploymentBackend(DeploymentPrivilege.INSUFFICIENT)

    with pytest.raises(TrainingNodeDeploymentError) as raised:
        TrainingWorkerSystemDeployer().deploy(backend, _request())

    assert raised.value.code == DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE
    assert backend.calls == [
        ("inspect_runtime_identity", None),
        ("inspect_privilege", SudoPasswordMode.SAME_AS_SSH)
    ]
    assert "manual" not in raised.value.message.lower()
    assert "current user" not in raised.value.message.lower()


def test_privilege_probe_internal_failure_is_sanitized() -> None:
    class BrokenProbeBackend(_FakeDeploymentBackend):
        def inspect_privilege(self, **_kwargs):
            raise ValueError("raw transport implementation detail")

    backend = BrokenProbeBackend(DeploymentPrivilege.SUDO)

    with pytest.raises(TrainingNodeDeploymentError) as raised:
        TrainingWorkerSystemDeployer().deploy(backend, _request())

    assert raised.value.code == "training_node_deployment_failed"
    assert "raw transport" not in raised.value.message
    assert backend.calls == [("inspect_runtime_identity", None)]


def test_custom_center_ca_is_installed_and_used_for_enrollment() -> None:
    backend = _FakeDeploymentBackend(DeploymentPrivilege.SUDO)

    result = TrainingWorkerSystemDeployer().deploy(
        backend,
        _request(center_ca_certificate=TEST_CENTER_CA),
    )

    assert "center_ca_certificate" in result.changed_steps
    files = [call[1] for call in backend.calls if call[0] == "write_managed_file"]
    files_by_path = {spec.path: (spec, content) for spec, content in files}
    ca_spec, ca_content = files_by_path[WORKER_CENTER_CA_PATH]
    assert ca_spec.mode == 0o640
    assert ca_content == TEST_CENTER_CA
    environment = files_by_path[WORKER_ENVIRONMENT_PATH][1]
    assert f"DATAPILOT_CENTER_CA_CERT_PATH={WORKER_CENTER_CA_PATH}".encode() in environment
    enroll_call = next(call for call in backend.calls if call[0] == "enroll_worker")
    assert enroll_call[1]["center_ca_path"] == WORKER_CENTER_CA_PATH
    assert TEST_CENTER_CA.decode() not in repr(_request(center_ca_certificate=TEST_CENTER_CA))


def test_invalid_center_ca_is_rejected_before_remote_writes() -> None:
    backend = _FakeDeploymentBackend(DeploymentPrivilege.SUDO)

    with pytest.raises(TrainingNodeDeploymentError) as raised:
        TrainingWorkerSystemDeployer().deploy(
            backend,
            _request(center_ca_certificate=b"not a certificate"),
        )

    assert raised.value.code == "training_node_deployment_invalid_request"
    assert backend.calls == []


class _PrivilegeRunner:
    def __init__(self, root: bool, sudo: bool) -> None:
        self.root = root
        self.sudo = sudo
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run_fixed_privilege_probe(self, argv, *, stdin_secret, timeout_seconds):
        self.calls.append((argv, stdin_secret))
        if argv == ROOT_IDENTITY_PROBE_ARGV:
            return FixedCommandResult(0, b"0\n" if self.root else b"1000\n")
        return FixedCommandResult(0 if self.sudo else 1)


@pytest.mark.parametrize(
    ("mode", "ssh_password", "sudo_password", "expected_argv", "expected_secret"),
    [
        (
            SudoPasswordMode.SAME_AS_SSH,
            "ssh-secret",
            None,
            PASSWORD_SUDO_PROBE_ARGV,
            "ssh-secret",
        ),
        (
            SudoPasswordMode.SEPARATE,
            "ssh-secret",
            "sudo-secret",
            PASSWORD_SUDO_PROBE_ARGV,
            "sudo-secret",
        ),
        (
            SudoPasswordMode.NOT_REQUIRED,
            "ssh-secret",
            None,
            PASSWORDLESS_SUDO_PROBE_ARGV,
            None,
        ),
    ],
)
def test_privilege_probe_uses_fixed_argv_and_secret_stdin_only(
    mode: SudoPasswordMode,
    ssh_password: str,
    sudo_password: str | None,
    expected_argv: tuple[str, ...],
    expected_secret: str | None,
) -> None:
    runner = _PrivilegeRunner(False, True)

    privilege = inspect_deployment_privilege(
        runner,
        sudo_password_mode=mode,
        ssh_password=ssh_password,
        sudo_password=sudo_password,
    )

    assert privilege is DeploymentPrivilege.PASSWORDLESS_SUDO
    assert runner.calls == [
        (ROOT_IDENTITY_PROBE_ARGV, None),
        (expected_argv, expected_secret),
    ]
    assert "ssh-secret" not in repr(expected_argv)
    assert "sudo-secret" not in repr(expected_argv)


def test_root_account_never_attempts_sudo() -> None:
    runner = _PrivilegeRunner(True, False)

    assert inspect_deployment_privilege(
        runner,
        sudo_password_mode=SudoPasswordMode.SAME_AS_SSH,
        ssh_password="unused",
    ) is DeploymentPrivilege.ROOT
    assert runner.calls == [(ROOT_IDENTITY_PROBE_ARGV, None)]


def test_secrets_are_excluded_from_request_repr_and_result() -> None:
    request = _request(
        sudo_password_mode=SudoPasswordMode.SEPARATE,
        sudo_password="sudo-secret",
    )
    backend = _FakeDeploymentBackend(DeploymentPrivilege.PASSWORDLESS_SUDO)

    result = TrainingWorkerSystemDeployer().deploy(backend, request)

    assert ENROLLMENT_TOKEN not in repr(request)
    assert "sudo-secret" not in repr(request)
    assert ARTIFACT.decode() not in repr(request)
    serialized = json.dumps(asdict(result), default=str)
    assert ENROLLMENT_TOKEN not in serialized
    assert "sudo-secret" not in serialized
    assert backend.observed_sudo_password == "sudo-secret"


def test_invalid_or_missing_separate_sudo_password_is_rejected_before_backend() -> None:
    backend = _FakeDeploymentBackend(DeploymentPrivilege.PASSWORDLESS_SUDO)
    with pytest.raises(TrainingNodeDeploymentError) as raised:
        TrainingWorkerSystemDeployer().deploy(
            backend,
            _request(
                sudo_password_mode=SudoPasswordMode.SEPARATE,
                sudo_password=None,
            ),
        )
    assert raised.value.code == "training_node_deployment_invalid_request"
    assert backend.calls == []
