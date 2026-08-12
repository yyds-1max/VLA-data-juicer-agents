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
    SshEndpoint,
    SshTransportError,
)
from .worker_artifact import build_training_worker_release
from .worker_deployment import (
    DEPLOYMENT_ACCOUNT_INSUFFICIENT_CODE,
    DEPLOYMENT_INVALID_REQUEST_CODE,
    SudoPasswordMode,
    SystemWorkerDeploymentBackend,
    TrainingNodeDeploymentError,
    TrainingWorkerSystemDeployer,
    WorkerDeploymentRequest,
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
        release_builder: Callable[[], Any] = build_training_worker_release,
    ) -> None:
        self._center_base_url = center_base_url
        self._center_ca_certificate = center_ca_certificate
        self._backend_factory = backend_factory
        self._host_key_observer = host_key_observer or OpenSshHostKeyObserver()
        self._release_builder = release_builder
        self._deployer = TrainingWorkerSystemDeployer()

    def discover_host_key(self, node: dict[str, Any]) -> dict[str, str]:
        endpoint = _endpoint(node)
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

    def deploy_worker(
        self,
        *,
        node: dict[str, Any],
        confirmed_host_key: dict[str, str],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
        enrollment_token: str,
    ) -> dict[str, str]:
        endpoint = _endpoint(node)
        pin = PinnedHostKey(
            algorithm=confirmed_host_key["algorithm"],
            public_key=confirmed_host_key["public_key"],
            sha256_fingerprint=confirmed_host_key["sha256_fingerprint"],
        )
        # Validate the caller-confirmed key material before opening SSH.
        try:
            pin.known_hosts_line(endpoint)
        except HostKeyValidationError as exc:
            raise TrainingValidationError(
                "training_node_host_key_mismatch",
                "The confirmed training node host key is invalid.",
            ) from exc
        request = WorkerDeploymentRequest(
            release=self._release_builder(),
            center_base_url=self._center_base_url,
            node_ref=str(node["node_ref"]),
            enrollment_token=enrollment_token,
            center_ca_certificate=self._center_ca_certificate,
            sudo_password_mode=SudoPasswordMode(sudo_password_mode),
            sudo_password=sudo_password,
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


def _endpoint(node: dict[str, Any]) -> SshEndpoint:
    return SshEndpoint(
        host=str(node["address"]),
        port=int(node.get("ssh_port", 22)),
        username=str(node["ssh_username"]),
    )


__all__ = [
    "AutomatedNodeDeploymentManager",
    "DeploymentBackendContextFactory",
]
