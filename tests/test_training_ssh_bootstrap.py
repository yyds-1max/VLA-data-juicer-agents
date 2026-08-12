from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest

from vla_data_juicer_agents.training.ssh_bootstrap import (
    CredentialValidationError,
    DeploymentPlanKind,
    HostKeyObservation,
    HostKeyValidationError,
    OpenSshHostKeyObserver,
    OpenSshPasswordBackend,
    PASSWORD_AUTHENTICATION_SUPPORTED,
    PinnedHostKey,
    PreflightProbe,
    RemoteExecution,
    SshBootstrapService,
    SshEndpoint,
    SshSession,
    _probe_argv,
    _write_public_temporary_file,
)
import vla_data_juicer_agents.training.ssh_bootstrap as ssh_bootstrap


PRIVATE_KEY = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"test-only\n"
    b"-----END OPENSSH PRIVATE KEY-----\n"
)
HOST_KEY_ALGORITHM = b"ssh-ed25519"
HOST_KEY_BYTES = (
    len(HOST_KEY_ALGORITHM).to_bytes(4, byteorder="big")
    + HOST_KEY_ALGORITHM
    + b"a deliberately fake host public key body"
)
HOST_PUBLIC_KEY = base64.b64encode(HOST_KEY_BYTES).decode("ascii")
HOST_FINGERPRINT = "SHA256:" + base64.b64encode(
    hashlib.sha256(HOST_KEY_BYTES).digest()
).decode("ascii").rstrip("=")


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[PreflightProbe, str, int]] = []

    def run_probe(
        self,
        probe: PreflightProbe,
        *,
        install_directory: str,
        timeout_seconds: int,
    ) -> RemoteExecution:
        self.calls.append((probe, install_directory, timeout_seconds))
        if probe is PreflightProbe.SYSTEMD_USER:
            return RemoteExecution(1, b"degraded\n", b"")
        if probe is PreflightProbe.SYSTEMD_LINGER:
            return RemoteExecution(0, b"no\n", b"")
        if probe is PreflightProbe.NVIDIA_SMI:
            return RemoteExecution(
                0,
                b"0, GPU-test, Test GPU, 40960, 1024, 0\n",
                b"",
            )
        return RemoteExecution(0, probe.value.encode("ascii") + b"\n", b"")


class _FakeBackend:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.saw_expected_private_key = False
        self.known_hosts_line = ""
        self.session = _FakeSession()

    @contextmanager
    def open_session(
        self,
        *,
        endpoint: SshEndpoint,
        known_hosts_line: str,
        private_key: bytes,
        connect_timeout_seconds: int,
    ) -> Iterator[SshSession]:
        self.opened = True
        self.saw_expected_private_key = private_key == PRIVATE_KEY
        self.known_hosts_line = known_hosts_line
        assert endpoint == SshEndpoint("192.0.2.10", 2222, "trainer")
        assert connect_timeout_seconds == 10
        try:
            yield self.session
        finally:
            self.closed = True


class _FakePasswordBackend:
    def __init__(self, expected_password: str) -> None:
        self._expected_password = expected_password
        self.saw_expected_password = False
        self.session = _FakeSession()

    @contextmanager
    def open_password_session(
        self,
        *,
        endpoint: SshEndpoint,
        known_hosts_line: str,
        password: str,
        connect_timeout_seconds: int,
    ) -> Iterator[SshSession]:
        self.saw_expected_password = password == self._expected_password
        assert endpoint == _endpoint()
        assert known_hosts_line == _host_key().known_hosts_line(endpoint)
        assert connect_timeout_seconds == 10
        yield self.session


def _endpoint() -> SshEndpoint:
    return SshEndpoint("192.0.2.10", 2222, "trainer")


def _host_key() -> PinnedHostKey:
    return PinnedHostKey("ssh-ed25519", HOST_PUBLIC_KEY, HOST_FINGERPRINT)


def test_preflight_uses_only_fixed_read_only_probe_catalogue() -> None:
    backend = _FakeBackend()
    service = SshBootstrapService(backend)

    report = service.run_preflight(
        endpoint=_endpoint(),
        host_key=_host_key(),
        private_key=PRIVATE_KEY,
        install_directory="/data/datapilot-worker",
    )

    assert backend.opened and backend.closed and backend.saw_expected_private_key
    assert [call[0] for call in backend.session.calls] == list(PreflightProbe)
    assert all(call[1] == "/data/datapilot-worker" for call in backend.session.calls)
    assert report.read_only is True
    assert report.credential_mode == "ephemeral_private_key"
    by_probe = {result.probe: result for result in report.probes}
    assert by_probe[PreflightProbe.SYSTEMD_USER].availability == "unavailable"
    assert by_probe[PreflightProbe.SYSTEMD_LINGER].availability == "unavailable"
    assert by_probe[PreflightProbe.SYSTEMD_LINGER].stdout == "no"
    assert by_probe[PreflightProbe.NVIDIA_SMI].stdout.startswith("0, GPU-test")
    assert backend.known_hosts_line == (
        f"[192.0.2.10]:2222 ssh-ed25519 {HOST_PUBLIC_KEY}\n"
    )


def test_private_key_is_absent_from_result_repr_and_serialisation() -> None:
    backend = _FakeBackend()
    report = SshBootstrapService(backend).run_preflight(
        endpoint=_endpoint(),
        host_key=_host_key(),
        private_key=PRIVATE_KEY,
        install_directory="/data/datapilot-worker",
    )

    secret = PRIVATE_KEY.decode("ascii")
    assert secret not in repr(report)
    assert secret not in json.dumps(asdict(report), default=str)
    assert secret not in repr(backend.session.calls)


def test_mismatched_host_fingerprint_fails_before_backend_is_opened() -> None:
    backend = _FakeBackend()
    key = PinnedHostKey("ssh-ed25519", HOST_PUBLIC_KEY, "SHA256:" + "A" * 43)

    with pytest.raises(HostKeyValidationError, match="does not match"):
        SshBootstrapService(backend).run_preflight(
            endpoint=_endpoint(),
            host_key=key,
            private_key=PRIVATE_KEY,
            install_directory="/data/datapilot-worker",
        )

    assert backend.opened is False


def test_mismatched_host_algorithm_fails_before_backend_is_opened() -> None:
    backend = _FakeBackend()
    key = PinnedHostKey("ssh-rsa", HOST_PUBLIC_KEY, HOST_FINGERPRINT)

    with pytest.raises(HostKeyValidationError, match="algorithm does not match"):
        SshBootstrapService(backend).run_preflight(
            endpoint=_endpoint(),
            host_key=key,
            private_key=PRIVATE_KEY,
            install_directory="/data/datapilot-worker",
        )

    assert backend.opened is False


@pytest.mark.parametrize(
    "directory",
    [
        "relative/path",
        "/",
        "/data/../root",
        "/data/worker;touch-pwned",
        "/data/worker with spaces",
        "/data/worker\nwhoami",
        "/data/worker/",
    ],
)
def test_install_directory_rejects_shell_text_and_non_normal_paths(
    directory: str,
) -> None:
    backend = _FakeBackend()
    with pytest.raises(ValueError):
        SshBootstrapService(backend).run_preflight(
            endpoint=_endpoint(),
            host_key=_host_key(),
            private_key=PRIVATE_KEY,
            install_directory=directory,
        )
    assert backend.opened is False


def test_remote_probe_argv_is_a_closed_allowlist() -> None:
    install_directory = "/data/datapilot-worker"
    commands = {
        probe: _probe_argv(probe, install_directory, username="trainer")
        for probe in PreflightProbe
    }

    assert commands[PreflightProbe.OPERATING_SYSTEM] == (
        "/usr/bin/env",
        "uname",
        "-s",
    )
    assert commands[PreflightProbe.DISK] == (
        "/usr/bin/env",
        "df",
        "-Pk",
        "--",
        install_directory,
    )
    assert commands[PreflightProbe.SYSTEMD_LINGER] == (
        "/usr/bin/env",
        "loginctl",
        "show-user",
        "trainer",
        "--property=Linger",
        "--value",
    )
    assert commands[PreflightProbe.INSTALL_DIRECTORY_EXISTS] == (
        "/usr/bin/env",
        "test",
        "-d",
        install_directory,
    )
    assert commands[PreflightProbe.INSTALL_DIRECTORY_WRITABLE] == (
        "/usr/bin/env",
        "test",
        "-w",
        install_directory,
    )
    assert all(argv[0] == "/usr/bin/env" for argv in commands.values())


def test_password_mode_is_supported_and_key_must_be_bytes() -> None:
    assert PASSWORD_AUTHENTICATION_SUPPORTED is True
    backend = _FakeBackend()
    with pytest.raises(CredentialValidationError, match="bytes"):
        SshBootstrapService(backend).run_preflight(
            endpoint=_endpoint(),
            host_key=_host_key(),
            private_key="not-a-key",  # type: ignore[arg-type]
            install_directory="/data/datapilot-worker",
        )
    assert backend.opened is False


def test_injected_password_backend_has_ephemeral_password_boundary() -> None:
    password = "test-only password"
    password_backend = _FakePasswordBackend(password)
    report = SshBootstrapService(
        _FakeBackend(),
        password_backend=password_backend,
    ).run_password_preflight(
        endpoint=_endpoint(),
        host_key=_host_key(),
        password=password,
        install_directory="/data/datapilot-worker",
    )

    assert password_backend.saw_expected_password is True
    assert report.credential_mode == "ephemeral_password"
    assert password not in repr(report)
    assert password not in json.dumps(asdict(report), default=str)


def test_password_preflight_still_validates_host_pin_before_backend() -> None:
    password_backend = _FakePasswordBackend("test-only password")
    mismatched_key = PinnedHostKey(
        "ssh-ed25519",
        HOST_PUBLIC_KEY,
        "SHA256:" + "A" * 43,
    )
    with pytest.raises(HostKeyValidationError, match="does not match"):
        SshBootstrapService(
            _FakeBackend(),
            password_backend=password_backend,
        ).run_password_preflight(
            endpoint=_endpoint(),
            host_key=mismatched_key,
            password="test-only password",
            install_directory="/data/datapilot-worker",
        )
    assert password_backend.saw_expected_password is False


def test_default_password_backend_uses_private_one_use_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "test-only password"
    monkeypatch.setattr(
        ssh_bootstrap,
        "_required_executable",
        lambda _name: "/usr/bin/true",
    )

    with OpenSshPasswordBackend().open_password_session(
        endpoint=_endpoint(),
        known_hosts_line=_host_key().known_hosts_line(_endpoint()),
        password=password,
        connect_timeout_seconds=10,
    ) as session:
        assert password not in repr(session)
        with session._command_environment() as environment:  # type: ignore[attr-defined]
            assert password not in repr(environment)
            helper = str(session.askpass_path)  # type: ignore[attr-defined]
            assert password not in helper
            completed = subprocess.run(
                [helper],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                timeout=5,
            )
            assert completed.stdout.decode() == password
            assert stat.S_IMODE(session.askpass_path.stat().st_mode) == 0o700  # type: ignore[union-attr]
            for path in session.askpass_path.parent.iterdir():  # type: ignore[union-attr]
                if path.is_file():
                    assert password.encode() not in path.read_bytes()
    assert session.password is None  # type: ignore[attr-defined]


def test_host_key_observation_is_unconfirmed_and_uses_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> object:
        recorded["argv"] = argv
        recorded["environment"] = kwargs["env"]

        class Completed:
            returncode = 0
            stdout = (
                f"[192.0.2.10]:2222 ssh-ed25519 {HOST_PUBLIC_KEY}\n"
            ).encode("ascii")

        return Completed()

    monkeypatch.setattr(ssh_bootstrap, "_required_executable", lambda _name: "/usr/bin/ssh-keyscan")
    monkeypatch.setattr(ssh_bootstrap.subprocess, "run", fake_run)

    observations = OpenSshHostKeyObserver().observe(_endpoint())

    assert observations == (
        HostKeyObservation("ssh-ed25519", HOST_PUBLIC_KEY, HOST_FINGERPRINT),
    )
    assert observations[0].confirmed is False
    assert recorded["argv"] == [
        "/usr/bin/ssh-keyscan",
        "-T",
        "5",
        "-p",
        "2222",
        "192.0.2.10",
    ]
    assert "password" not in repr(recorded).lower()


def test_known_hosts_temporary_file_is_created_exclusively_with_mode_0600(
    tmp_path: Path,
) -> None:
    path = tmp_path / "known_hosts"
    _write_public_temporary_file(
        path,
        _host_key().known_hosts_line(_endpoint()).encode(),
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        _write_public_temporary_file(path, b"replacement")


def test_install_and_repair_plans_are_review_only() -> None:
    service = SshBootstrapService(_FakeBackend())
    install = service.build_install_plan(
        endpoint=_endpoint(),
        install_directory="/data/datapilot-worker",
        worker_version="0.1.0",
    )
    repair = service.build_repair_plan(
        endpoint=_endpoint(),
        install_directory="/data/datapilot-worker",
        worker_version="0.1.1",
    )

    assert install.kind is DeploymentPlanKind.INSTALL
    assert repair.kind is DeploymentPlanKind.REPAIR
    assert install.requires_explicit_write_authorization is True
    assert repair.remote_execution_supported is False
    assert any(step.mutates_remote_state for step in install.steps)
    assert repair.steps[0].step_id == "preserve_training"
    assert all("shell" not in step.description.lower() for step in repair.steps)
