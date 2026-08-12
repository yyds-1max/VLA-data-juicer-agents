from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.training.errors import TrainingValidationError
from vla_data_juicer_agents.training.node_deployment import (
    AutomatedNodeDeploymentManager,
)
from vla_data_juicer_agents.training.ssh_bootstrap import SshTransportError
from vla_data_juicer_agents.training.worker_deployment import WorkerRelease


def _host_key() -> dict[str, str]:
    algorithm = "ssh-ed25519"
    decoded = len(algorithm).to_bytes(4, "big") + algorithm.encode() + b"x" * 32
    public_key = base64.b64encode(decoded).decode()
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(decoded).digest()
    ).decode().rstrip("=")
    return {
        "algorithm": algorithm,
        "public_key": public_key,
        "sha256_fingerprint": fingerprint,
    }


def _node() -> dict[str, object]:
    return {
        "node_ref": "node_context01",
        "address": "192.0.2.10",
        "ssh_port": 2222,
        "ssh_username": "trainer",
    }


def test_deployment_manager_scopes_backend_to_one_context() -> None:
    lifecycle: list[str] = []
    seen: dict[str, object] = {}
    backend = object()

    @contextmanager
    def factory(**kwargs: object):
        seen.update(kwargs)
        lifecycle.append("entered")
        try:
            yield backend
        finally:
            lifecycle.append("exited")

    class Deployer:
        def deploy(self, actual_backend: object, request: object) -> object:
            assert actual_backend is backend
            seen["request"] = request
            return SimpleNamespace(worker_version="0.1.0")

    release = WorkerRelease(
        version="0.1.0",
        sha256=hashlib.sha256(b"worker").hexdigest(),
        artifact=b"worker",
    )
    manager = AutomatedNodeDeploymentManager(
        center_base_url="https://center.example.internal",
        backend_factory=factory,
        release_builder=lambda: release,
    )
    manager._deployer = Deployer()  # type: ignore[assignment]

    result = manager.deploy_worker(
        node=_node(),
        confirmed_host_key=_host_key(),
        ssh_password="one-use-password",
        sudo_password_mode="same_as_ssh",
        sudo_password=None,
        enrollment_token="enroll_" + "x" * 48,
    )

    assert lifecycle == ["entered", "exited"]
    assert result["worker_version"] == "0.1.0"
    assert seen["ssh_password"] == "one-use-password"


def test_deployment_manager_translates_sanitized_ssh_failure() -> None:
    @contextmanager
    def unavailable(**_kwargs: object):
        raise SshTransportError("safe transport failure")
        yield  # pragma: no cover

    manager = AutomatedNodeDeploymentManager(
        center_base_url="https://center.example.internal",
        backend_factory=unavailable,
        release_builder=lambda: WorkerRelease(
            version="0.1.0",
            sha256=hashlib.sha256(b"worker").hexdigest(),
            artifact=b"worker",
        ),
    )

    with pytest.raises(TrainingValidationError) as captured:
        manager.deploy_worker(
            node=_node(),
            confirmed_host_key=_host_key(),
            ssh_password="one-use-password",
            sudo_password_mode="same_as_ssh",
            sudo_password=None,
            enrollment_token="enroll_" + "x" * 48,
        )

    assert captured.value.code == "training_node_ssh_authentication_failed"
