"""System-level Training Worker deployment core.

The orchestrator in this module is deliberately transport-agnostic.  It accepts
only high-level, idempotent backend operations and never accepts a shell command
from an API caller.  An SSH adapter must establish a pinned-host-key session and
map these operations to its own fixed command catalogue.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import ssl
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit


DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE = (
    "training_node_deployment_account_insufficient"
)
DEPLOYMENT_INVALID_REQUEST_CODE = "training_node_deployment_invalid_request"
DEPLOYMENT_FAILED_CODE = "training_node_deployment_failed"
DEPLOYMENT_ENROLLMENT_REQUIRED_CODE = "training_node_deployment_enrollment_required"
WORKER_REMOVAL_FAILED_CODE = "training_node_worker_removal_failed"

WORKER_ACCOUNT = "datapilot-worker"
WORKER_GROUP = "datapilot-worker"
WORKER_OPT_ROOT = "/opt/datapilot-training-worker"
WORKER_RELEASES_ROOT = f"{WORKER_OPT_ROOT}/releases"
WORKER_CURRENT_LINK = f"{WORKER_OPT_ROOT}/current"
WORKER_STATE_ROOT = "/var/lib/datapilot-training-worker"
WORKER_CONFIG_ROOT = "/etc/datapilot-training-worker"
WORKER_ENVIRONMENT_PATH = f"{WORKER_CONFIG_ROOT}/worker.env"
WORKER_CENTER_CA_PATH = f"{WORKER_CONFIG_ROOT}/center-ca.pem"
WORKER_SYSTEMD_UNIT_PATH = "/etc/systemd/system/datapilot-training-worker.service"
WORKER_SYSTEMD_UNIT_NAME = "datapilot-training-worker.service"
WORKER_ARTIFACT_NAME = "datapilot-training-worker.pyz"
SUDO_PROMPT = "DataPilot sudo password:"

ROOT_IDENTITY_PROBE_ARGV = ("/usr/bin/id", "-u")
PASSWORDLESS_SUDO_PROBE_ARGV = ("/usr/bin/sudo", "-n", "--", "/usr/bin/true")
PASSWORD_SUDO_PROBE_ARGV = (
    "/usr/bin/sudo",
    "-S",
    "-k",
    "-p",
    SUDO_PROMPT,
    "--",
    "/usr/bin/true",
)

SYSTEMD_UNIT = """[Unit]
Description=DataPilot Training Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=datapilot-worker
Group=datapilot-worker
EnvironmentFile=/etc/datapilot-training-worker/worker.env
ExecStart=/usr/bin/python3 /opt/datapilot-training-worker/current/datapilot-training-worker.pyz --state-dir /var/lib/datapilot-training-worker --center-base-url ${DATAPILOT_CENTER_BASE_URL} --node-ref ${DATAPILOT_NODE_REF}
WorkingDirectory=/var/lib/datapilot-training-worker
Restart=on-failure
RestartSec=5s
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=/var/lib/datapilot-training-worker
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


class TrainingNodeDeploymentError(RuntimeError):
    """A stable, credential-free deployment error safe for the API boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DeploymentPrivilege(StrEnum):
    ROOT = "root"
    SUDO = "sudo"
    PASSWORDLESS_SUDO = "sudo"  # Backward-compatible alias for the first draft.
    INSUFFICIENT = "insufficient"


class SudoPasswordMode(StrEnum):
    SAME_AS_SSH = "same_as_ssh"
    SEPARATE = "separate"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True, slots=True)
class FixedCommandResult:
    return_code: int
    stdout: bytes = b""


class FixedPrivilegeProbeRunner(Protocol):
    """Runner limited by contract to the two exported fixed probe argv values."""

    def run_fixed_privilege_probe(
        self,
        argv: tuple[str, ...],
        *,
        stdin_secret: str | None,
        timeout_seconds: int,
    ) -> FixedCommandResult: ...


def inspect_deployment_privilege(
    runner: FixedPrivilegeProbeRunner,
    *,
    sudo_password_mode: SudoPasswordMode,
    ssh_password: str | None = None,
    sudo_password: str | None = None,
    timeout_seconds: int = 10,
) -> DeploymentPrivilege:
    """Detect root or non-interactive sudo without prompting or mutating state."""

    if not 1 <= timeout_seconds <= 60:
        raise ValueError("Privilege probe timeout must be between 1 and 60 seconds")
    identity = runner.run_fixed_privilege_probe(
        ROOT_IDENTITY_PROBE_ARGV,
        stdin_secret=None,
        timeout_seconds=timeout_seconds,
    )
    if identity.return_code == 0 and identity.stdout.strip() == b"0":
        return DeploymentPrivilege.ROOT
    if sudo_password_mode is SudoPasswordMode.NOT_REQUIRED:
        sudo_argv = PASSWORDLESS_SUDO_PROBE_ARGV
        secret = None
    elif sudo_password_mode is SudoPasswordMode.SAME_AS_SSH:
        sudo_argv = PASSWORD_SUDO_PROBE_ARGV
        secret = ssh_password
    elif sudo_password_mode is SudoPasswordMode.SEPARATE:
        sudo_argv = PASSWORD_SUDO_PROBE_ARGV
        secret = sudo_password
    else:
        return DeploymentPrivilege.INSUFFICIENT
    if sudo_password_mode is not SudoPasswordMode.NOT_REQUIRED and not secret:
        return DeploymentPrivilege.INSUFFICIENT
    sudo = runner.run_fixed_privilege_probe(
        sudo_argv,
        stdin_secret=secret,
        timeout_seconds=timeout_seconds,
    )
    if sudo.return_code == 0:
        return DeploymentPrivilege.SUDO
    return DeploymentPrivilege.INSUFFICIENT


@dataclass(frozen=True, slots=True)
class ServiceAccountSpec:
    username: str = WORKER_ACCOUNT
    group: str = WORKER_GROUP
    home_directory: str = WORKER_STATE_ROOT
    login_shell: str = "/usr/sbin/nologin"
    system_account: bool = True


@dataclass(frozen=True, slots=True)
class DirectorySpec:
    path: str
    owner: str
    group: str
    mode: int


SYSTEM_DIRECTORIES = (
    DirectorySpec(WORKER_OPT_ROOT, "root", "root", 0o755),
    DirectorySpec(WORKER_RELEASES_ROOT, "root", "root", 0o755),
    DirectorySpec(WORKER_STATE_ROOT, WORKER_ACCOUNT, WORKER_GROUP, 0o700),
    DirectorySpec(WORKER_CONFIG_ROOT, "root", WORKER_GROUP, 0o750),
)


@dataclass(frozen=True, slots=True)
class ManagedFileSpec:
    path: str
    owner: str
    group: str
    mode: int


@dataclass(frozen=True, slots=True)
class WorkerRelease:
    version: str
    sha256: str
    artifact: bytes = field(repr=False)

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", self.version):
            _invalid("Worker release version has an unsupported format")
        if not isinstance(self.artifact, bytes) or not 1 <= len(self.artifact) <= 512 * 1024 * 1024:
            _invalid("Worker release artifact has an unsupported size")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            _invalid("Worker release digest must be lowercase SHA-256")
        actual = hashlib.sha256(self.artifact).hexdigest()
        if not hmac.compare_digest(actual, self.sha256):
            _invalid("Worker release artifact does not match its SHA-256 digest")

    @property
    def release_directory(self) -> str:
        # Keep active artifacts immutable even when a development build reuses
        # the package version. The symlink switch below is then truly atomic.
        return f"{WORKER_RELEASES_ROOT}/{self.version}-{self.sha256[:12]}"

    @property
    def artifact_path(self) -> str:
        return f"{self.release_directory}/{WORKER_ARTIFACT_NAME}"


@dataclass(frozen=True, slots=True)
class WorkerDeploymentRequest:
    release: WorkerRelease
    center_base_url: str
    node_ref: str
    enrollment_token: str | None = field(default=None, repr=False)
    center_ca_certificate: bytes | None = field(default=None, repr=False)
    sudo_password_mode: SudoPasswordMode = SudoPasswordMode.SAME_AS_SSH
    sudo_password: str | None = field(default=None, repr=False)

    def validate(self) -> None:
        self.release.validate()
        _validate_center_base_url(self.center_base_url)
        _validate_center_ca_certificate(self.center_ca_certificate)
        if not re.fullmatch(r"node_[A-Za-z0-9_-]{8,120}", self.node_ref):
            _invalid("Training node reference has an unsupported format")
        if self.enrollment_token is not None and not re.fullmatch(
            r"enroll_[A-Za-z0-9_-]{33,249}", self.enrollment_token
        ):
            _invalid("Worker enrollment token has an unsupported format")
        if self.sudo_password_mode is SudoPasswordMode.SEPARATE:
            if not _valid_password(self.sudo_password):
                _invalid("Separate sudo password is missing or malformed")
        elif self.sudo_password is not None:
            _invalid("sudo_password is only valid in separate mode")


@dataclass(frozen=True, slots=True)
class WorkerDeploymentResult:
    node_ref: str
    worker_version: str
    privilege: DeploymentPrivilege
    service_account: str
    artifact_path: str
    systemd_unit: str
    service_active: bool
    changed_steps: tuple[str, ...]
    unchanged_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerRemovalRequest:
    node_ref: str
    sudo_password_mode: SudoPasswordMode = SudoPasswordMode.SAME_AS_SSH
    sudo_password: str | None = field(default=None, repr=False)

    def validate(self) -> None:
        if not re.fullmatch(r"node_[A-Za-z0-9_-]{8,120}", self.node_ref):
            _invalid("Training node reference has an unsupported format")
        if self.sudo_password_mode is SudoPasswordMode.SEPARATE:
            if not _valid_password(self.sudo_password):
                _invalid("Separate sudo password is missing or malformed")
        elif self.sudo_password is not None:
            _invalid("sudo_password is only valid in separate mode")


@dataclass(frozen=True, slots=True)
class WorkerRemovalResult:
    node_ref: str
    privilege: DeploymentPrivilege
    removed: bool


class SystemWorkerDeploymentBackend(Protocol):
    """High-level backend; every operation must be fixed and idempotent.

    Implementations may use pinned-host-key SSH, but must not expose a raw
    command method at the application boundary.  ``enrollment_token`` must be
    passed to the Worker process through stdin and never persisted by the
    deployment transport.
    """

    def inspect_privilege(
        self,
        *,
        sudo_password_mode: SudoPasswordMode,
        sudo_password: str | None,
    ) -> DeploymentPrivilege: ...

    def ensure_service_account(
        self,
        spec: ServiceAccountSpec,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def ensure_directory(
        self,
        spec: DirectorySpec,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def install_release(
        self,
        release: WorkerRelease,
        *,
        owner: str,
        group: str,
        mode: int,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def write_managed_file(
        self,
        spec: ManagedFileSpec,
        content: bytes,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def activate_release(
        self,
        *,
        release_directory: str,
        current_link: str,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def worker_is_enrolled(self, *, privilege: DeploymentPrivilege) -> bool: ...

    def enroll_worker(
        self,
        *,
        artifact_path: str,
        state_directory: str,
        center_base_url: str,
        node_ref: str,
        enrollment_token: str,
        center_ca_path: str | None,
        run_as: str,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def reload_enable_and_start_system_service(
        self,
        unit_name: str,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def system_service_is_active(
        self,
        unit_name: str,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool: ...

    def remove_system_worker(
        self,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool: ...


class TrainingWorkerSystemDeployer:
    """Apply the fixed system Worker layout through an idempotent backend."""

    def deploy(
        self,
        backend: SystemWorkerDeploymentBackend,
        request: WorkerDeploymentRequest,
    ) -> WorkerDeploymentResult:
        request.validate()
        try:
            privilege = backend.inspect_privilege(
                sudo_password_mode=request.sudo_password_mode,
                sudo_password=request.sudo_password,
            )
        except TrainingNodeDeploymentError:
            raise
        except Exception as exc:
            raise TrainingNodeDeploymentError(
                DEPLOYMENT_FAILED_CODE,
                "Training Worker deployment privilege check failed",
            ) from exc
        if privilege is DeploymentPrivilege.INSUFFICIENT:
            raise TrainingNodeDeploymentError(
                DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
                "The deployment account is neither root nor permitted to use non-interactive sudo",
            )
        if privilege not in {
            DeploymentPrivilege.ROOT,
            DeploymentPrivilege.SUDO,
        }:
            raise TrainingNodeDeploymentError(
                DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
                "The deployment account privilege could not be verified",
            )

        changed: list[str] = []
        unchanged: list[str] = []

        def record(step: str, did_change: bool) -> None:
            (changed if did_change else unchanged).append(step)

        try:
            record(
                "service_account",
                backend.ensure_service_account(
                    ServiceAccountSpec(),
                    privilege=privilege,
                ),
            )
            for directory in SYSTEM_DIRECTORIES:
                record(
                    f"directory:{directory.path}",
                    backend.ensure_directory(directory, privilege=privilege),
                )
            record(
                "release",
                backend.install_release(
                    request.release,
                    owner="root",
                    group="root",
                    mode=0o755,
                    privilege=privilege,
                ),
            )
            if request.center_ca_certificate is not None:
                record(
                    "center_ca_certificate",
                    backend.write_managed_file(
                        ManagedFileSpec(
                            WORKER_CENTER_CA_PATH,
                            "root",
                            WORKER_GROUP,
                            0o640,
                        ),
                        request.center_ca_certificate,
                        privilege=privilege,
                    ),
                )
            environment = _environment_content(request)
            record(
                "configuration",
                backend.write_managed_file(
                    ManagedFileSpec(
                        WORKER_ENVIRONMENT_PATH,
                        "root",
                        WORKER_GROUP,
                        0o640,
                    ),
                    environment,
                    privilege=privilege,
                ),
            )
            record(
                "systemd_unit",
                backend.write_managed_file(
                    ManagedFileSpec(
                        WORKER_SYSTEMD_UNIT_PATH,
                        "root",
                        "root",
                        0o644,
                    ),
                    SYSTEMD_UNIT.encode("utf-8"),
                    privilege=privilege,
                ),
            )
            record(
                "active_release",
                backend.activate_release(
                    release_directory=request.release.release_directory,
                    current_link=WORKER_CURRENT_LINK,
                    privilege=privilege,
                ),
            )
            if backend.worker_is_enrolled(privilege=privilege):
                unchanged.append("enrollment")
            else:
                if request.enrollment_token is None:
                    raise TrainingNodeDeploymentError(
                        DEPLOYMENT_ENROLLMENT_REQUIRED_CODE,
                        "An unregistered Worker requires a one-use enrollment token",
                    )
                record(
                    "enrollment",
                    backend.enroll_worker(
                        artifact_path=request.release.artifact_path,
                        state_directory=WORKER_STATE_ROOT,
                        center_base_url=request.center_base_url,
                        node_ref=request.node_ref,
                        enrollment_token=request.enrollment_token,
                        center_ca_path=(
                            WORKER_CENTER_CA_PATH
                            if request.center_ca_certificate is not None
                            else None
                        ),
                        run_as=WORKER_ACCOUNT,
                        privilege=privilege,
                    ),
                )
            record(
                "system_service",
                backend.reload_enable_and_start_system_service(
                    WORKER_SYSTEMD_UNIT_NAME,
                    privilege=privilege,
                ),
            )
            active = backend.system_service_is_active(
                WORKER_SYSTEMD_UNIT_NAME,
                privilege=privilege,
            )
        except TrainingNodeDeploymentError:
            raise
        except Exception as exc:
            # Never interpolate backend exceptions: a transport implementation
            # could contain credentials or raw SSH diagnostics in its message.
            raise TrainingNodeDeploymentError(
                DEPLOYMENT_FAILED_CODE,
                "Training Worker system deployment failed",
            ) from exc
        if not active:
            raise TrainingNodeDeploymentError(
                DEPLOYMENT_FAILED_CODE,
                "Training Worker system service did not become active",
            )
        return WorkerDeploymentResult(
            node_ref=request.node_ref,
            worker_version=request.release.version,
            privilege=privilege,
            service_account=WORKER_ACCOUNT,
            artifact_path=request.release.artifact_path,
            systemd_unit=WORKER_SYSTEMD_UNIT_PATH,
            service_active=True,
            changed_steps=tuple(changed),
            unchanged_steps=tuple(unchanged),
        )


class TrainingWorkerSystemRemover:
    """Remove only the fixed DataPilot Worker system installation."""

    def remove(
        self,
        backend: SystemWorkerDeploymentBackend,
        request: WorkerRemovalRequest,
    ) -> WorkerRemovalResult:
        request.validate()
        try:
            privilege = backend.inspect_privilege(
                sudo_password_mode=request.sudo_password_mode,
                sudo_password=request.sudo_password,
            )
        except TrainingNodeDeploymentError:
            raise
        except Exception as exc:
            raise TrainingNodeDeploymentError(
                WORKER_REMOVAL_FAILED_CODE,
                "Training Worker removal privilege check failed",
            ) from exc
        if privilege not in {DeploymentPrivilege.ROOT, DeploymentPrivilege.SUDO}:
            raise TrainingNodeDeploymentError(
                DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
                "The deployment account is neither root nor permitted to use sudo",
            )
        try:
            removed = backend.remove_system_worker(privilege=privilege)
        except TrainingNodeDeploymentError:
            raise
        except Exception as exc:
            raise TrainingNodeDeploymentError(
                WORKER_REMOVAL_FAILED_CODE,
                "Training Worker system removal failed",
            ) from exc
        return WorkerRemovalResult(
            node_ref=request.node_ref,
            privilege=privilege,
            removed=removed,
        )


def _environment_content(request: WorkerDeploymentRequest) -> bytes:
    # Validation restricts values to single-line, EnvironmentFile-safe tokens.
    content = (
        f"DATAPILOT_CENTER_BASE_URL={request.center_base_url}\n"
        f"DATAPILOT_NODE_REF={request.node_ref}\n"
    )
    if request.center_ca_certificate is not None:
        content += f"DATAPILOT_CENTER_CA_CERT_PATH={WORKER_CENTER_CA_PATH}\n"
    return content.encode("utf-8")


def _validate_center_base_url(value: str) -> None:
    if not isinstance(value, str) or len(value) > 2048 or any(
        character in value for character in ("\x00", "\r", "\n", " ", "\t")
    ):
        _invalid("Worker center URL has an unsupported format")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        _invalid("Worker center URL must be an HTTPS origin without credentials")
    if not re.fullmatch(r"https://[A-Za-z0-9.:[\]_-]+(?::[0-9]{1,5})?/?", value):
        _invalid("Worker center URL contains unsupported characters")


def _validate_center_ca_certificate(value: bytes | None) -> None:
    if value is None:
        return
    if not isinstance(value, bytes) or not 1 <= len(value) <= 256 * 1024:
        _invalid("Worker center CA certificate has an unsupported size")
    try:
        certificate = value.decode("ascii")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=certificate)
    except (UnicodeDecodeError, ssl.SSLError, ValueError):
        _invalid("Worker center CA certificate is not valid PEM")


def _valid_password(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 1024
        and not any(character in value for character in ("\x00", "\r", "\n"))
    )


def _invalid(message: str) -> None:
    raise TrainingNodeDeploymentError(DEPLOYMENT_INVALID_REQUEST_CODE, message)


__all__ = [
    "DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE",
    "DeploymentPrivilege",
    "DirectorySpec",
    "FixedCommandResult",
    "FixedPrivilegeProbeRunner",
    "ManagedFileSpec",
    "ServiceAccountSpec",
    "SudoPasswordMode",
    "SystemWorkerDeploymentBackend",
    "TrainingNodeDeploymentError",
    "TrainingWorkerSystemDeployer",
    "TrainingWorkerSystemRemover",
    "WORKER_CENTER_CA_PATH",
    "WorkerDeploymentRequest",
    "WorkerDeploymentResult",
    "WorkerRemovalRequest",
    "WorkerRemovalResult",
    "WorkerRelease",
    "WORKER_REMOVAL_FAILED_CODE",
    "inspect_deployment_privilege",
]
