"""Safe, read-only SSH bootstrap discovery primitives.

This module deliberately does not install or repair a worker.  It provides the
small security boundary needed to inspect a node before a separately authorised
deployment workflow is implemented.

The public service accepts private-key material only as a call argument.  The
production backend loads that material into a dedicated, short-lived ssh-agent
through stdin; the private key is never written to a file or included in an
argv, environment mapping, error, result, or log message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from typing import Protocol


MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_REMOTE_OUTPUT_BYTES = 16 * 1024
PASSWORD_AUTHENTICATION_SUPPORTED = True
SUPPORTED_CREDENTIAL_MODE = "ephemeral_private_key"
PASSWORD_CREDENTIAL_MODE = "ephemeral_password"


class SshBootstrapError(RuntimeError):
    """A sanitised bootstrap failure safe to expose to an operator."""


class HostKeyValidationError(SshBootstrapError):
    """The caller-supplied host key pin is missing, malformed, or inconsistent."""


class CredentialValidationError(SshBootstrapError):
    """Ephemeral private-key material cannot be used safely."""


class SshTransportError(SshBootstrapError):
    """SSH transport or authentication failed without exposing raw diagnostics."""


@dataclass(frozen=True, slots=True)
class SshEndpoint:
    host: str
    port: int
    username: str

    def __post_init__(self) -> None:
        _validate_host(self.host)
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        if not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,63}", self.username):
            raise ValueError("SSH username has an unsupported format")

    @property
    def destination(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.username}@{host}"

    @property
    def known_hosts_name(self) -> str:
        if self.port == 22:
            return self.host
        return f"[{self.host}]:{self.port}"


@dataclass(frozen=True, slots=True)
class PinnedHostKey:
    """An exact OpenSSH public host key plus its independently supplied digest."""

    algorithm: str
    public_key: str
    sha256_fingerprint: str

    def known_hosts_line(self, endpoint: SshEndpoint) -> str:
        if not re.fullmatch(r"[A-Za-z0-9@._+-]{1,128}", self.algorithm):
            raise HostKeyValidationError("SSH host key algorithm is malformed")
        if not 20 <= len(self.public_key) <= 16 * 1024:
            raise HostKeyValidationError("SSH host public key has an unsupported size")
        if any(character.isspace() for character in self.public_key):
            raise HostKeyValidationError("SSH host public key must be one base64 token")
        try:
            padding = "=" * (-len(self.public_key) % 4)
            decoded = base64.b64decode(self.public_key + padding, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HostKeyValidationError(
                "SSH host public key is not valid base64"
            ) from exc
        if len(decoded) < 16:
            raise HostKeyValidationError("SSH host public key is unexpectedly short")
        algorithm_length = int.from_bytes(decoded[:4], byteorder="big")
        embedded_algorithm = decoded[4 : 4 + algorithm_length]
        if (
            algorithm_length == 0
            or 4 + algorithm_length > len(decoded)
            or embedded_algorithm != self.algorithm.encode("ascii")
        ):
            raise HostKeyValidationError(
                "SSH host key algorithm does not match the public key blob"
            )

        actual = "SHA256:" + base64.b64encode(hashlib.sha256(decoded).digest()).decode(
            "ascii"
        ).rstrip("=")
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", self.sha256_fingerprint):
            raise HostKeyValidationError("SSH host key fingerprint is malformed")
        if not hmac.compare_digest(actual, self.sha256_fingerprint):
            raise HostKeyValidationError(
                "SSH host public key does not match the supplied fingerprint"
            )
        return (
            f"{endpoint.known_hosts_name} {self.algorithm} {self.public_key}\n"
        )


@dataclass(frozen=True, slots=True)
class HostKeyObservation:
    """An explicitly untrusted host key returned for out-of-band confirmation."""

    algorithm: str
    public_key: str
    sha256_fingerprint: str
    confirmed: bool = False

    def as_pin(self) -> PinnedHostKey:
        """Return pin material only after the caller has separately confirmed it."""

        return PinnedHostKey(
            algorithm=self.algorithm,
            public_key=self.public_key,
            sha256_fingerprint=self.sha256_fingerprint,
        )


class OpenSshHostKeyObserver:
    """Discover untrusted host keys without credentials or trust-on-first-use."""

    def observe(
        self,
        endpoint: SshEndpoint,
        *,
        timeout_seconds: int = 5,
    ) -> tuple[HostKeyObservation, ...]:
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("Host key observation timeout must be between 1 and 30")
        keyscan_path = _required_executable("ssh-keyscan")
        try:
            completed = subprocess.run(
                [
                    keyscan_path,
                    "-T",
                    str(timeout_seconds),
                    "-p",
                    str(endpoint.port),
                    endpoint.host,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_minimal_ssh_environment(None),
                timeout=timeout_seconds + 2,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshTransportError("SSH host key observation timed out") from exc
        if completed.returncode != 0 and not completed.stdout:
            raise SshTransportError("SSH host key observation failed")
        observations: dict[tuple[str, str], HostKeyObservation] = {}
        for raw_line in completed.stdout.splitlines():
            if not raw_line or raw_line.startswith(b"#"):
                continue
            try:
                fields = raw_line.decode("ascii").split()
            except UnicodeDecodeError:
                continue
            if len(fields) < 3:
                continue
            algorithm, public_key = fields[1], fields[2]
            try:
                decoded = base64.b64decode(
                    public_key + "=" * (-len(public_key) % 4),
                    validate=True,
                )
            except (ValueError, binascii.Error):
                continue
            fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(decoded).digest()
            ).decode("ascii").rstrip("=")
            observation = HostKeyObservation(
                algorithm=algorithm,
                public_key=public_key,
                sha256_fingerprint=fingerprint,
            )
            try:
                observation.as_pin().known_hosts_line(endpoint)
            except HostKeyValidationError:
                continue
            observations[(algorithm, public_key)] = observation
        if not observations:
            raise SshTransportError("SSH host returned no supported host key")
        return tuple(
            sorted(observations.values(), key=lambda item: item.algorithm)
        )


class PreflightProbe(StrEnum):
    OPERATING_SYSTEM = "operating_system"
    OS_RELEASE = "os_release"
    ARCHITECTURE = "architecture"
    SYSTEMD_USER = "systemd_user"
    SYSTEMD_LINGER = "systemd_linger"
    PYTHON = "python"
    DISK = "disk"
    NVIDIA_SMI = "nvidia_smi"
    INSTALL_DIRECTORY_EXISTS = "install_directory_exists"
    INSTALL_DIRECTORY_WRITABLE = "install_directory_writable"


@dataclass(frozen=True, slots=True)
class RemoteExecution:
    return_code: int
    stdout: bytes = b""
    stderr: bytes = b""


class SshSession(Protocol):
    def run_probe(
        self,
        probe: PreflightProbe,
        *,
        install_directory: str,
        timeout_seconds: int,
    ) -> RemoteExecution: ...


class SshBackend(Protocol):
    def open_session(
        self,
        *,
        endpoint: SshEndpoint,
        known_hosts_line: str,
        private_key: bytes,
        connect_timeout_seconds: int,
    ) -> AbstractContextManager[SshSession]: ...


class PasswordSshBackend(Protocol):
    """Injection boundary for a future audited password transport.

    An implementation must consume ``password`` only during this context.  It
    must never place it in argv, the environment, a file, diagnostics, logs, or
    its return value.
    """

    def open_password_session(
        self,
        *,
        endpoint: SshEndpoint,
        known_hosts_line: str,
        password: str,
        connect_timeout_seconds: int,
    ) -> AbstractContextManager[SshSession]: ...


class ProbeAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe: PreflightProbe
    availability: ProbeAvailability
    return_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    endpoint: SshEndpoint
    install_directory: str
    read_only: bool
    credential_mode: str
    completed_at: datetime
    probes: tuple[ProbeResult, ...]


class DeploymentPlanKind(StrEnum):
    INSTALL = "install"
    REPAIR = "repair"


@dataclass(frozen=True, slots=True)
class DeploymentPlanStep:
    step_id: str
    description: str
    mutates_remote_state: bool


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """A reviewable plan only; this module has no method that executes it."""

    kind: DeploymentPlanKind
    endpoint: SshEndpoint
    install_directory: str
    worker_version: str
    requires_explicit_write_authorization: bool
    remote_execution_supported: bool
    steps: tuple[DeploymentPlanStep, ...]


class SshBootstrapService:
    """Run the fixed read-only probe catalogue over pinned-host-key SSH."""

    def __init__(
        self,
        backend: SshBackend | None = None,
        *,
        password_backend: PasswordSshBackend | None = None,
    ) -> None:
        self._backend = backend or OpenSshBackend()
        self._password_backend = password_backend or OpenSshPasswordBackend()

    def run_preflight(
        self,
        *,
        endpoint: SshEndpoint,
        host_key: PinnedHostKey,
        private_key: bytes,
        install_directory: str,
        connect_timeout_seconds: int = 10,
        probe_timeout_seconds: int = 15,
    ) -> PreflightReport:
        """Inspect a node without changing its filesystem, services, or processes."""

        validated_directory = _validate_install_directory(install_directory)
        if not isinstance(private_key, bytes):
            raise CredentialValidationError("SSH private key must be supplied as bytes")
        if not private_key or len(private_key) > MAX_PRIVATE_KEY_BYTES:
            raise CredentialValidationError("SSH private key has an unsupported size")
        if b"\x00" in private_key or b"PRIVATE KEY" not in private_key:
            raise CredentialValidationError("SSH private key format is unsupported")
        if not 1 <= connect_timeout_seconds <= 60:
            raise ValueError("SSH connect timeout must be between 1 and 60 seconds")
        if not 1 <= probe_timeout_seconds <= 120:
            raise ValueError("SSH probe timeout must be between 1 and 120 seconds")

        # Validation happens before the backend can start ssh-agent or ssh.
        known_hosts_line = host_key.known_hosts_line(endpoint)
        with self._backend.open_session(
            endpoint=endpoint,
            known_hosts_line=known_hosts_line,
            private_key=private_key,
            connect_timeout_seconds=connect_timeout_seconds,
        ) as session:
            return _run_preflight_session(
                session,
                endpoint=endpoint,
                install_directory=validated_directory,
                credential_mode=SUPPORTED_CREDENTIAL_MODE,
                probe_timeout_seconds=probe_timeout_seconds,
            )

    def run_password_preflight(
        self,
        *,
        endpoint: SshEndpoint,
        host_key: PinnedHostKey,
        password: str,
        install_directory: str,
        connect_timeout_seconds: int = 10,
        probe_timeout_seconds: int = 15,
    ) -> PreflightReport:
        """Use a separately audited ephemeral-password backend when provided.

        The OpenSSH implementation supplies the password through a one-use
        local Unix socket to a fixed SSH_ASKPASS helper.  Neither the secret nor
        a derivative is placed in argv, the environment, or a file.
        """

        validated_directory = _validate_install_directory(install_directory)
        if not isinstance(password, str):
            raise CredentialValidationError("SSH password must be supplied as text")
        if not password or len(password) > 1024 or any(
            character in password for character in ("\x00", "\n", "\r")
        ):
            raise CredentialValidationError("SSH password has an unsupported format")
        if not 1 <= connect_timeout_seconds <= 60:
            raise ValueError("SSH connect timeout must be between 1 and 60 seconds")
        if not 1 <= probe_timeout_seconds <= 120:
            raise ValueError("SSH probe timeout must be between 1 and 120 seconds")

        # Keep host-key pin validation ahead of credential/backend handling.
        known_hosts_line = host_key.known_hosts_line(endpoint)
        if self._password_backend is None:  # pragma: no cover - defensive typing
            raise CredentialValidationError("SSH password backend is unavailable")
        with self._password_backend.open_password_session(
            endpoint=endpoint,
            known_hosts_line=known_hosts_line,
            password=password,
            connect_timeout_seconds=connect_timeout_seconds,
        ) as session:
            return _run_preflight_session(
                session,
                endpoint=endpoint,
                install_directory=validated_directory,
                credential_mode=PASSWORD_CREDENTIAL_MODE,
                probe_timeout_seconds=probe_timeout_seconds,
            )

    def build_install_plan(
        self,
        *,
        endpoint: SshEndpoint,
        install_directory: str,
        worker_version: str,
    ) -> DeploymentPlan:
        return _deployment_plan(
            DeploymentPlanKind.INSTALL,
            endpoint=endpoint,
            install_directory=install_directory,
            worker_version=worker_version,
        )

    def build_repair_plan(
        self,
        *,
        endpoint: SshEndpoint,
        install_directory: str,
        worker_version: str,
    ) -> DeploymentPlan:
        return _deployment_plan(
            DeploymentPlanKind.REPAIR,
            endpoint=endpoint,
            install_directory=install_directory,
            worker_version=worker_version,
        )


class OpenSshBackend:
    """System OpenSSH backend using an isolated, in-memory agent identity."""

    @contextmanager
    def open_session(
        self,
        *,
        endpoint: SshEndpoint,
        known_hosts_line: str,
        private_key: bytes,
        connect_timeout_seconds: int,
    ) -> Iterator[SshSession]:
        ssh_path = _required_executable("ssh")
        ssh_agent_path = _required_executable("ssh-agent")
        ssh_add_path = _required_executable("ssh-add")
        agent: subprocess.Popen[bytes] | None = None

        with tempfile.TemporaryDirectory(prefix="datapilot-ssh-") as temporary:
            temporary_path = Path(temporary)
            os.chmod(temporary_path, stat.S_IRWXU)
            known_hosts_path = temporary_path / "known_hosts"
            _write_public_temporary_file(
                known_hosts_path,
                known_hosts_line.encode("ascii"),
            )
            agent_socket_path = temporary_path / "agent.sock"
            environment = _minimal_ssh_environment(agent_socket_path)
            try:
                agent = subprocess.Popen(
                    [ssh_agent_path, "-D", "-a", str(agent_socket_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                )
                _wait_for_agent_socket(agent, agent_socket_path)
                added = subprocess.run(
                    [ssh_add_path, "-"],
                    input=private_key,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    timeout=10,
                    check=False,
                )
                if added.returncode != 0:
                    raise CredentialValidationError(
                        "SSH private key could not be loaded without a passphrase"
                    )

                public_keys = subprocess.run(
                    [ssh_add_path, "-L"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    timeout=10,
                    check=False,
                )
                lines = [
                    line for line in public_keys.stdout.splitlines() if line.strip()
                ]
                if public_keys.returncode != 0 or len(lines) != 1:
                    raise CredentialValidationError(
                        "Exactly one SSH private identity must be supplied"
                    )
                public_key_fields = lines[0].split()
                if len(public_key_fields) < 2:
                    raise CredentialValidationError(
                        "SSH agent returned a malformed public identity"
                    )
                public_identity_path = temporary_path / "identity.pub"
                _write_public_temporary_file(
                    public_identity_path,
                    b" ".join(public_key_fields[:2]) + b"\n",
                )

                yield _OpenSshSession(
                    ssh_path=ssh_path,
                    endpoint=endpoint,
                    known_hosts_path=known_hosts_path,
                    public_identity_path=public_identity_path,
                    agent_socket_path=agent_socket_path,
                    environment=environment,
                    connect_timeout_seconds=connect_timeout_seconds,
                    authentication_options=(
                        "BatchMode=yes",
                        "PasswordAuthentication=no",
                        "KbdInteractiveAuthentication=no",
                        "PreferredAuthentications=publickey",
                        f"IdentityAgent={agent_socket_path}",
                        "IdentitiesOnly=yes",
                        f"IdentityFile={public_identity_path}",
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                raise SshTransportError("Local OpenSSH helper timed out") from exc
            finally:
                if agent is not None and agent.poll() is None:
                    agent.terminate()
                    try:
                        agent.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        agent.kill()
                        agent.wait(timeout=2)


class OpenSshPasswordBackend:
    """Password OpenSSH backend with an in-memory, one-use askpass channel."""

    @contextmanager
    def open_password_session(
        self,
        *,
        endpoint: SshEndpoint,
        known_hosts_line: str,
        password: str,
        connect_timeout_seconds: int,
    ) -> Iterator[SshSession]:
        ssh_path = _required_executable("ssh")
        with tempfile.TemporaryDirectory(prefix="datapilot-ssh-") as temporary:
            temporary_path = Path(temporary)
            os.chmod(temporary_path, stat.S_IRWXU)
            known_hosts_path = temporary_path / "known_hosts"
            _write_public_temporary_file(
                known_hosts_path,
                known_hosts_line.encode("ascii"),
            )
            askpass_path = temporary_path / "askpass"
            _write_private_executable(askpass_path, _ASKPASS_HELPER)
            session = _OpenSshSession(
                ssh_path=ssh_path,
                endpoint=endpoint,
                known_hosts_path=known_hosts_path,
                public_identity_path=None,
                agent_socket_path=None,
                environment=_minimal_ssh_environment(None),
                connect_timeout_seconds=connect_timeout_seconds,
                authentication_options=(
                    "BatchMode=no",
                    "PubkeyAuthentication=no",
                    "PasswordAuthentication=yes",
                    "KbdInteractiveAuthentication=no",
                    "PreferredAuthentications=password",
                    "NumberOfPasswordPrompts=1",
                ),
                password=password,
                askpass_path=askpass_path,
                askpass_socket_path=temporary_path / "askpass.sock",
            )
            try:
                yield session
            finally:
                session.clear_sensitive()


@dataclass(frozen=True, slots=True)
class _OpenSshSession:
    ssh_path: str
    endpoint: SshEndpoint
    known_hosts_path: Path
    public_identity_path: Path | None
    agent_socket_path: Path | None
    environment: Mapping[str, str]
    connect_timeout_seconds: int
    authentication_options: tuple[str, ...]
    password: str | None = field(default=None, repr=False)
    askpass_path: Path | None = None
    askpass_socket_path: Path | None = None

    def clear_sensitive(self) -> None:
        """Drop context-local credential references after the SSH context closes."""

        object.__setattr__(self, "password", None)

    def run_probe(
        self,
        probe: PreflightProbe,
        *,
        install_directory: str,
        timeout_seconds: int,
    ) -> RemoteExecution:
        remote_argv = _probe_argv(
            probe,
            install_directory,
            username=self.endpoint.username,
        )
        # OpenSSH transports remote commands through the login shell.  shlex.join
        # is used only on argv produced by the closed catalogue above; no caller
        # can submit shell text or choose an executable.
        return self.run_fixed_argv(
            remote_argv,
            stdin_payload=b"",
            timeout_seconds=timeout_seconds,
            operation_name=probe.value,
        )

    def run_fixed_argv(
        self,
        remote_argv: tuple[str, ...],
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        operation_name: str,
    ) -> RemoteExecution:
        """Internal transport for argv selected by a closed application catalogue."""

        if not remote_argv or not all(
            isinstance(value, str)
            and value
            and "\x00" not in value
            and "\n" not in value
            and "\r" not in value
            for value in remote_argv
        ):
            raise ValueError("Remote argv contains an unsupported value")
        if not isinstance(stdin_payload, bytes):
            raise TypeError("Remote stdin payload must be bytes")
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("Remote operation timeout must be between 1 and 600 seconds")
        if not re.fullmatch(r"[a-z0-9_:-]{1,80}", operation_name):
            raise ValueError("Remote operation name has an unsupported format")
        remote_command = shlex.join(remote_argv)
        argv = [
            self.ssh_path,
            "-F",
            "/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_path}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "RequestTTY=no",
            "-o",
            "LogLevel=ERROR",
            "-o",
            f"ConnectTimeout={self.connect_timeout_seconds}",
            *[
                value
                for option in self.authentication_options
                for value in ("-o", option)
            ],
            "-p",
            str(self.endpoint.port),
            "--",
            self.endpoint.destination,
            remote_command,
        ]
        try:
            with self._command_environment() as environment:
                completed = subprocess.run(
                    argv,
                    input=stdin_payload,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    start_new_session=True,
                    timeout=timeout_seconds,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise SshTransportError(
                f"SSH operation {operation_name} exceeded its time limit"
            ) from exc
        return RemoteExecution(
            return_code=completed.returncode,
            stdout=completed.stdout[:MAX_REMOTE_OUTPUT_BYTES],
            stderr=completed.stderr[:MAX_REMOTE_OUTPUT_BYTES],
        )

    @contextmanager
    def _command_environment(self) -> Iterator[dict[str, str]]:
        if self.password is None:
            yield dict(self.environment)
            return
        if self.askpass_path is None or self.askpass_socket_path is None:
            raise SshTransportError("SSH password helper is unavailable")
        with _ephemeral_password_environment(
            self.environment,
            askpass_path=self.askpass_path,
            socket_path=self.askpass_socket_path,
            password=self.password,
        ) as environment:
            yield environment


def _run_preflight_session(
    session: SshSession,
    *,
    endpoint: SshEndpoint,
    install_directory: str,
    credential_mode: str,
    probe_timeout_seconds: int,
) -> PreflightReport:
    results: list[ProbeResult] = []
    for probe in PreflightProbe:
        execution = session.run_probe(
            probe,
            install_directory=install_directory,
            timeout_seconds=probe_timeout_seconds,
        )
        if execution.return_code == 255:
            raise SshTransportError(
                f"SSH transport failed during the {probe.value} probe"
            )
        available = execution.return_code == 0
        if probe is PreflightProbe.SYSTEMD_LINGER:
            available = available and execution.stdout.strip() == b"yes"
        results.append(
            ProbeResult(
                probe=probe,
                availability=(
                    ProbeAvailability.AVAILABLE
                    if available
                    else ProbeAvailability.UNAVAILABLE
                ),
                return_code=execution.return_code,
                stdout=_safe_remote_output(execution.stdout),
                stderr=_safe_remote_output(execution.stderr),
            )
        )
    return PreflightReport(
        endpoint=endpoint,
        install_directory=install_directory,
        read_only=True,
        credential_mode=credential_mode,
        completed_at=datetime.now(timezone.utc),
        probes=tuple(results),
    )


def _probe_argv(
    probe: PreflightProbe,
    install_directory: str,
    *,
    username: str,
) -> tuple[str, ...]:
    directory = _validate_install_directory(install_directory)
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,63}", username):
        raise ValueError("SSH username has an unsupported format")
    commands: dict[PreflightProbe, tuple[str, ...]] = {
        PreflightProbe.OPERATING_SYSTEM: ("/usr/bin/env", "uname", "-s"),
        PreflightProbe.OS_RELEASE: ("/usr/bin/env", "cat", "/etc/os-release"),
        PreflightProbe.ARCHITECTURE: ("/usr/bin/env", "uname", "-m"),
        PreflightProbe.SYSTEMD_USER: (
            "/usr/bin/env",
            "systemctl",
            "--user",
            "is-system-running",
        ),
        PreflightProbe.SYSTEMD_LINGER: (
            "/usr/bin/env",
            "loginctl",
            "show-user",
            username,
            "--property=Linger",
            "--value",
        ),
        PreflightProbe.PYTHON: ("/usr/bin/env", "python3", "--version"),
        PreflightProbe.DISK: ("/usr/bin/env", "df", "-Pk", "--", directory),
        PreflightProbe.NVIDIA_SMI: (
            "/usr/bin/env",
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ),
        PreflightProbe.INSTALL_DIRECTORY_EXISTS: (
            "/usr/bin/env",
            "test",
            "-d",
            directory,
        ),
        PreflightProbe.INSTALL_DIRECTORY_WRITABLE: (
            "/usr/bin/env",
            "test",
            "-w",
            directory,
        ),
    }
    return commands[probe]


def _deployment_plan(
    kind: DeploymentPlanKind,
    *,
    endpoint: SshEndpoint,
    install_directory: str,
    worker_version: str,
) -> DeploymentPlan:
    directory = _validate_install_directory(install_directory)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", worker_version):
        raise ValueError("Worker version has an unsupported format")
    if kind is DeploymentPlanKind.INSTALL:
        steps = (
            DeploymentPlanStep(
                "verify_preflight", "Require a successful preflight", False
            ),
            DeploymentPlanStep(
                "verify_artifact", "Verify the signed worker artifact and digest", False
            ),
            DeploymentPlanStep(
                "stage_release", "Stage a versioned worker release", True
            ),
            DeploymentPlanStep(
                "install_user_service",
                "Install or update the user systemd service",
                True,
            ),
            DeploymentPlanStep(
                "enroll_worker",
                "Exchange a one-use enrollment token for worker identity",
                True,
            ),
            DeploymentPlanStep(
                "verify_heartbeat",
                "Verify worker health through the control channel",
                False,
            ),
        )
    else:
        steps = (
            DeploymentPlanStep(
                "preserve_training",
                "Discover and preserve existing training processes",
                False,
            ),
            DeploymentPlanStep(
                "collect_diagnostics",
                "Inspect worker and user-service diagnostics",
                False,
            ),
            DeploymentPlanStep(
                "verify_artifact",
                "Verify the signed replacement artifact and digest",
                False,
            ),
            DeploymentPlanStep(
                "replace_release", "Atomically replace the broken worker release", True
            ),
            DeploymentPlanStep(
                "restart_user_service", "Restart only the worker user service", True
            ),
            DeploymentPlanStep(
                "reconcile_runs",
                "Reconcile the worker ledger without relaunching jobs",
                False,
            ),
            DeploymentPlanStep(
                "verify_heartbeat",
                "Verify worker health through the control channel",
                False,
            ),
        )
    return DeploymentPlan(
        kind=kind,
        endpoint=endpoint,
        install_directory=directory,
        worker_version=worker_version,
        requires_explicit_write_authorization=True,
        remote_execution_supported=False,
        steps=steps,
    )


def _validate_host(host: str) -> None:
    if not host or len(host) > 253 or any(character.isspace() for character in host):
        raise ValueError("SSH host has an unsupported format")
    if any(character in host for character in ("\x00", "\n", "\r", "[", "]", "@")):
        raise ValueError("SSH host has an unsupported format")
    try:
        socket.inet_pton(socket.AF_INET, host)
        return
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return
    except OSError:
        pass
    labels = host.split(".")
    if any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("SSH host has an unsupported format")


def _validate_install_directory(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 512:
        raise ValueError("Worker install directory must be an absolute POSIX path")
    if not re.fullmatch(r"/[A-Za-z0-9._/+-]+", value):
        raise ValueError("Worker install directory contains unsupported characters")
    path = PurePosixPath(value)
    if ".." in path.parts or str(path) != value or path == PurePosixPath("/"):
        raise ValueError("Worker install directory must be normalised and non-root")
    return str(path)


def _safe_remote_output(value: bytes) -> str:
    decoded = value[:MAX_REMOTE_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return "".join(
        character
        for character in decoded
        if character in "\n\r\t" or ord(character) >= 32
    ).strip()


def _required_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SshTransportError(f"Required local OpenSSH helper is unavailable: {name}")
    return path


_ASKPASS_HELPER = b"""#!/usr/bin/env python3
import os
import sys

with open(os.environ["DATAPILOT_ASKPASS_PIPE"], "rb", buffering=0) as stream:
    sys.stdout.buffer.write(stream.read(2048))
"""


def _minimal_ssh_environment(agent_socket_path: Path | None) -> dict[str, str]:
    false_path = shutil.which("false") or "/usr/bin/false"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
        "SSH_ASKPASS": false_path,
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": "datapilot-no-display",
    }
    if agent_socket_path is not None:
        environment["SSH_AUTH_SOCK"] = str(agent_socket_path)
    return environment


@contextmanager
def _ephemeral_password_environment(
    base_environment: Mapping[str, str],
    *,
    askpass_path: Path,
    socket_path: Path,
    password: str,
) -> Iterator[dict[str, str]]:
    """Serve one password over a mode-0600 FIFO to SSH_ASKPASS."""

    password_buffer = bytearray(password.encode("utf-8"))
    pipe_descriptor: int | None = None
    try:
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        os.mkfifo(socket_path, stat.S_IRUSR | stat.S_IWUSR)
        os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
        pipe_descriptor = os.open(socket_path, os.O_RDWR | os.O_NONBLOCK)
        os.write(pipe_descriptor, password_buffer)
        environment = dict(base_environment)
        environment.update(
            {
                "SSH_ASKPASS": str(askpass_path),
                "SSH_ASKPASS_REQUIRE": "force",
                "DATAPILOT_ASKPASS_PIPE": str(socket_path),
                "DISPLAY": "datapilot-askpass",
            }
        )
        yield environment
    finally:
        if pipe_descriptor is not None:
            os.close(pipe_descriptor)
        for index in range(len(password_buffer)):
            password_buffer[index] = 0
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def _write_public_temporary_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _write_private_executable(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRWXU,
    )
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    os.chmod(path, stat.S_IRWXU)


def _wait_for_agent_socket(
    agent: subprocess.Popen[bytes],
    agent_socket_path: Path,
    *,
    timeout_seconds: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if agent.poll() is not None:
            raise SshTransportError("The isolated local ssh-agent failed to start")
        if agent_socket_path.exists():
            return
        time.sleep(0.02)
    raise SshTransportError("The isolated local ssh-agent did not become ready")
