"""Application adapter for one-click system Training Worker deployment."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from .errors import TrainingUnavailableError, TrainingValidationError
from .ssh_bootstrap import (
    HostKeyValidationError,
    OpenSshHostKeyObserver,
    PinnedHostKey,
    PreflightProbe,
    PreflightReport,
    ProbeAvailability,
    SshBootstrapError,
    SshBootstrapService,
    SshEndpoint,
    SshTransportError,
)
from .worker_artifact import build_training_worker_release
from .worker_deployment import (
    DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
    DEPLOYMENT_INVALID_REQUEST_CODE,
    DeploymentPrivilege,
    RuntimeIdentity,
    SudoPasswordMode,
    SystemWorkerDeploymentBackend,
    TrainingNodeDeploymentError,
    TrainingWorkerSystemDeployer,
    TrainingWorkerSystemRemover,
    WorkerDeploymentRequest,
    WorkerRemovalRequest,
)


class DeploymentBackendContextFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: SshEndpoint,
        host_key: PinnedHostKey,
        ssh_password: str,
    ) -> AbstractContextManager[SystemWorkerDeploymentBackend]: ...


class AutomatedNodeDeploymentManager:
    """Translate the Web node contract into a fixed system deployment."""

    def __init__(
        self,
        *,
        center_base_url: str,
        center_ca_certificate: bytes | None = None,
        backend_factory: DeploymentBackendContextFactory,
        host_key_observer: OpenSshHostKeyObserver | None = None,
        preflight_service: SshBootstrapService | None = None,
        release_builder: Callable[[], Any] = build_training_worker_release,
    ) -> None:
        self._center_base_url = center_base_url
        self._center_ca_certificate = center_ca_certificate
        self._backend_factory = backend_factory
        self._host_key_observer = host_key_observer or OpenSshHostKeyObserver()
        self._preflight_service = preflight_service or SshBootstrapService()
        self._release_builder = release_builder
        self._deployer = TrainingWorkerSystemDeployer()
        self._remover = TrainingWorkerSystemRemover()

    def discover_host_key(self, node: dict[str, Any]) -> dict[str, str]:
        # SSH host keys identify the host, not a login account.  A placeholder
        # username lets a freshly registered node be observed before the user
        # chooses the account that will own the Worker and training processes.
        endpoint = _endpoint(node, username_override="datapilot-host-key-observer")
        try:
            observations = self._host_key_observer.observe(endpoint)
        except SshTransportError as exc:
            raise TrainingValidationError(
                "training_node_host_key_observation_failed",
                "Unable to read the training node host key.",
            ) from exc
        preferred = next(
            (
                observation
                for observation in observations
                if observation.algorithm == "ssh-ed25519"
            ),
            observations[0],
        )
        return {
            "algorithm": preferred.algorithm,
            "public_key": preferred.public_key,
            "sha256_fingerprint": preferred.sha256_fingerprint,
        }

    def preflight_worker(
        self,
        *,
        node: dict[str, Any],
        confirmed_host_key: dict[str, str],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
    ) -> dict[str, Any]:
        """Run the fixed read-only catalogue and privilege probe.

        This method does not create an account, directory, token, service, or
        other remote state. Credentials live only for the two short SSH
        contexts used by the read-only probes.
        """

        endpoint = _endpoint(node)
        pin = _confirmed_pin(endpoint, confirmed_host_key)
        try:
            report = self._preflight_service.run_password_preflight(
                endpoint=endpoint,
                host_key=pin,
                password=ssh_password,
                install_directory="/opt/datapilot-training-worker",
            )
            with self._backend_factory(
                endpoint=endpoint,
                host_key=pin,
                ssh_password=ssh_password,
            ) as backend:
                runtime_identity = backend.inspect_runtime_identity()
                privilege = backend.inspect_privilege(
                    sudo_password_mode=SudoPasswordMode(sudo_password_mode),
                    sudo_password=sudo_password,
                )
        except SshTransportError as exc:
            raise TrainingValidationError(
                "training_node_ssh_authentication_failed",
                "SSH connection or authentication failed.",
            ) from exc
        except SshBootstrapError as exc:
            raise TrainingValidationError(
                "training_node_preflight_failed",
                "Training node preflight could not be completed.",
            ) from exc
        except (TrainingNodeDeploymentError, RuntimeError, ValueError) as exc:
            raise TrainingValidationError(
                "training_node_preflight_failed",
                "Training node identity or privilege could not be verified.",
            ) from exc
        checks = _product_preflight_checks(report, privilege, runtime_identity)
        return {
            "ready": all(check["status"] != "failed" for check in checks),
            "checked_at": report.completed_at.isoformat(),
            "checks": checks,
        }

    def deploy_worker(
        self,
        *,
        node: dict[str, Any],
        confirmed_host_key: dict[str, str],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
        enrollment_token: str,
        force_reenrollment: bool = False,
    ) -> dict[str, str]:
        endpoint = _endpoint(node)
        pin = _confirmed_pin(endpoint, confirmed_host_key)
        preflight = self.preflight_worker(
            node=node,
            confirmed_host_key=confirmed_host_key,
            ssh_password=ssh_password,
            sudo_password_mode=sudo_password_mode,
            sudo_password=sudo_password,
        )
        if not preflight["ready"]:
            privilege_failed = any(
                check["code"] == "deployment_privilege"
                and check["status"] == "failed"
                for check in preflight["checks"]
            )
            if privilege_failed:
                raise TrainingValidationError(
                    DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
                    "部署账号权限不足",
                )
            raise TrainingValidationError(
                "training_node_preflight_failed",
                "训练节点未通过部署条件检查。",
            )
        request = WorkerDeploymentRequest(
            release=self._release_builder(),
            center_base_url=self._center_base_url,
            node_ref=str(node["node_ref"]),
            enrollment_token=enrollment_token,
            center_ca_certificate=self._center_ca_certificate,
            sudo_password_mode=SudoPasswordMode(sudo_password_mode),
            sudo_password=sudo_password,
            force_reenrollment=force_reenrollment,
        )
        try:
            with self._backend_factory(
                endpoint=endpoint,
                host_key=pin,
                ssh_password=ssh_password,
            ) as backend:
                result = self._deployer.deploy(backend, request)
        except SshTransportError as exc:
            raise TrainingValidationError(
                "training_node_ssh_authentication_failed",
                "SSH connection or authentication failed.",
            ) from exc
        except TrainingNodeDeploymentError as exc:
            if exc.code == DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE:
                raise TrainingValidationError(
                    DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
                    "部署账号权限不足",
                ) from exc
            if exc.code == DEPLOYMENT_INVALID_REQUEST_CODE:
                raise TrainingValidationError(exc.code, exc.message) from exc
            raise TrainingUnavailableError(
                exc.code,
                "Training Worker deployment failed.",
            ) from exc
        return {
            "worker_version": result.worker_version,
            "message": "Training Worker deployed and system service started.",
        }

    def remove_worker(
        self,
        *,
        node: dict[str, Any],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
    ) -> dict[str, str]:
        if not all(
            node.get(field)
            for field in (
                "host_key_algorithm",
                "host_public_key",
                "host_key_fingerprint",
            )
        ):
            raise TrainingValidationError(
                "training_node_host_key_missing",
                "The training node has no confirmed SSH host key.",
            )
        endpoint = _endpoint(node)
        pin = PinnedHostKey(
            algorithm=str(node["host_key_algorithm"]),
            public_key=str(node["host_public_key"]),
            sha256_fingerprint=str(node["host_key_fingerprint"]),
        )
        try:
            pin.known_hosts_line(endpoint)
        except HostKeyValidationError as exc:
            raise TrainingValidationError(
                "training_node_host_key_mismatch",
                "The confirmed training node host key is invalid.",
            ) from exc
        request = WorkerRemovalRequest(
            node_ref=str(node["node_ref"]),
            sudo_password_mode=SudoPasswordMode(sudo_password_mode),
            sudo_password=sudo_password,
        )
        try:
            with self._backend_factory(
                endpoint=endpoint,
                host_key=pin,
                ssh_password=ssh_password,
            ) as backend:
                self._remover.remove(backend, request)
        except SshTransportError as exc:
            raise TrainingValidationError(
                "training_node_ssh_authentication_failed",
                "SSH connection or authentication failed.",
            ) from exc
        except TrainingNodeDeploymentError as exc:
            if exc.code == DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE:
                raise TrainingValidationError(
                    DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
                    "部署账号权限不足",
                ) from exc
            if exc.code == DEPLOYMENT_INVALID_REQUEST_CODE:
                raise TrainingValidationError(exc.code, exc.message) from exc
            raise TrainingUnavailableError(
                exc.code,
                "Training Worker removal failed.",
            ) from exc
        return {"message": "Training Worker removed from the node."}


def _endpoint(
    node: dict[str, Any], *, username_override: str | None = None
) -> SshEndpoint:
    username = username_override or node.get("ssh_username")
    if not username:
        raise TrainingValidationError(
            "training_node_ssh_username_required",
            "An SSH login account is required for this Worker operation.",
        )
    return SshEndpoint(
        host=str(node["address"]),
        port=int(node.get("ssh_port", 22)),
        username=str(username),
    )


def _confirmed_pin(
    endpoint: SshEndpoint, confirmed_host_key: dict[str, str]
) -> PinnedHostKey:
    pin = PinnedHostKey(
        algorithm=confirmed_host_key["algorithm"],
        public_key=confirmed_host_key["public_key"],
        sha256_fingerprint=confirmed_host_key["sha256_fingerprint"],
    )
    try:
        pin.known_hosts_line(endpoint)
    except HostKeyValidationError as exc:
        raise TrainingValidationError(
            "training_node_host_key_mismatch",
            "The confirmed training node host key is invalid.",
        ) from exc
    return pin


def _product_preflight_checks(
    report: PreflightReport,
    privilege: DeploymentPrivilege,
    runtime_identity: RuntimeIdentity,
) -> list[dict[str, str]]:
    by_probe = {result.probe: result for result in report.probes}

    def available(probe: PreflightProbe) -> bool:
        return by_probe[probe].availability is ProbeAvailability.AVAILABLE

    operating_system = by_probe[PreflightProbe.OPERATING_SYSTEM]
    linux = available(PreflightProbe.OPERATING_SYSTEM) and (
        operating_system.stdout.strip().casefold() == "linux"
    )
    checks: list[dict[str, str]] = [
        {
            "code": "runtime_identity",
            "label": "Worker 与训练运行身份",
            "status": "warning" if runtime_identity.uid == 0 else "passed",
            "detail": (
                "将以 root 身份运行 Worker 和训练任务，拥有该节点的完整权限。"
                if runtime_identity.uid == 0
                else f"将以 SSH 登录账号 {runtime_identity.username} 运行 Worker 和训练任务。"
            ),
        },
        {
            "code": "operating_system",
            "label": "Linux 操作系统",
            "status": "passed" if linux else "failed",
            "detail": "已识别 Linux。" if linux else "Worker 当前仅支持 Linux。",
        },
        {
            "code": "architecture",
            "label": "系统架构",
            "status": (
                "passed" if available(PreflightProbe.ARCHITECTURE) else "failed"
            ),
            "detail": (
                by_probe[PreflightProbe.ARCHITECTURE].stdout
                if available(PreflightProbe.ARCHITECTURE)
                else "无法读取系统架构。"
            ),
        },
        {
            "code": "systemd_system",
            "label": "系统服务管理",
            "status": (
                "passed" if available(PreflightProbe.SYSTEMD_SYSTEM) else "failed"
            ),
            "detail": (
                "systemd 可用。"
                if available(PreflightProbe.SYSTEMD_SYSTEM)
                else "未检测到 systemd，无法安装长期运行服务。"
            ),
        },
        {
            "code": "python",
            "label": "Python 3",
            "status": "passed" if available(PreflightProbe.PYTHON) else "failed",
            "detail": (
                by_probe[PreflightProbe.PYTHON].stdout
                if available(PreflightProbe.PYTHON)
                else "未检测到 python3。"
            ),
        },
        {
            "code": "install_disk",
            "label": "安装磁盘",
            "status": "passed" if available(PreflightProbe.DISK) else "failed",
            "detail": (
                "安装目录所在磁盘可访问。"
                if available(PreflightProbe.DISK)
                else "无法读取安装目录所在磁盘。"
            ),
        },
        {
            "code": "deployment_privilege",
            "label": "部署账号权限",
            "status": (
                "passed"
                if privilege in {DeploymentPrivilege.ROOT, DeploymentPrivilege.SUDO}
                else "failed"
            ),
            "detail": (
                "已验证 root 权限。"
                if privilege is DeploymentPrivilege.ROOT
                else "已验证 sudo 权限。"
                if privilege is DeploymentPrivilege.SUDO
                else "部署账号权限不足"
            ),
        },
        {
            "code": "nvidia_smi",
            "label": "NVIDIA GPU 工具",
            "status": (
                "passed" if available(PreflightProbe.NVIDIA_SMI) else "warning"
            ),
            "detail": (
                "nvidia-smi 可用。"
                if available(PreflightProbe.NVIDIA_SMI)
                else "未检测到 nvidia-smi；Worker 仍可安装，但无法上报 NVIDIA GPU。"
            ),
        },
        {
            "code": "install_directory",
            "label": "Worker 安装目录",
            "status": (
                "passed"
                if available(PreflightProbe.INSTALL_DIRECTORY_EXISTS)
                else "warning"
            ),
            "detail": (
                "安装目录已存在，将执行幂等更新。"
                if available(PreflightProbe.INSTALL_DIRECTORY_EXISTS)
                else "首次安装时将由系统自动创建。"
            ),
        },
    ]
    return checks


__all__ = [
    "AutomatedNodeDeploymentManager",
    "DeploymentBackendContextFactory",
]
