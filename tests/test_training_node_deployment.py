from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.training.errors import TrainingValidationError
from vla_data_juicer_agents.training.node_deployment import (
    AutomatedNodeDeploymentManager,
)
from vla_data_juicer_agents.training.ssh_bootstrap import (
    PreflightProbe,
    PreflightReport,
    ProbeAvailability,
    ProbeResult,
    SshTransportError,
)
from vla_data_juicer_agents.training.worker_deployment import (
    DeploymentPrivilege,
    RuntimeIdentity,
    WorkerRelease,
)


TEST_CENTER_CA = (Path(__file__).parent / "fixtures" / "training_center_ca.pem").read_bytes()


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


def test_host_key_observation_does_not_require_or_bind_an_ssh_account() -> None:
    seen: dict[str, object] = {}
    host_key = _host_key()

    class Observer:
        def observe(self, endpoint: object) -> tuple[object, ...]:
            seen["endpoint"] = endpoint
            return (
                SimpleNamespace(
                    algorithm=host_key["algorithm"],
                    public_key=host_key["public_key"],
                    sha256_fingerprint=host_key["sha256_fingerprint"],
                ),
            )

    @contextmanager
    def factory(**_kwargs: object):
        yield _DeploymentBackend()

    manager = AutomatedNodeDeploymentManager(
        center_base_url="https://center.example.internal",
        backend_factory=factory,
        host_key_observer=Observer(),  # type: ignore[arg-type]
    )

    result = manager.discover_host_key(
        {key: value for key, value in _node().items() if key != "ssh_username"}
    )

    assert result == host_key
    assert seen["endpoint"].username == "datapilot-host-key-observer"  # type: ignore[union-attr]


class _PassingPreflightService:
    def run_password_preflight(self, **kwargs: object) -> PreflightReport:
        endpoint = kwargs["endpoint"]
        outputs = {
            PreflightProbe.OPERATING_SYSTEM: "Linux",
            PreflightProbe.ARCHITECTURE: "x86_64",
            PreflightProbe.PYTHON: "Python 3.11.9",
        }
        return PreflightReport(
            endpoint=endpoint,  # type: ignore[arg-type]
            install_directory=str(kwargs["install_directory"]),
            read_only=True,
            credential_mode="ephemeral_password",
            completed_at=datetime.now(timezone.utc),
            probes=tuple(
                ProbeResult(
                    probe=probe,
                    availability=ProbeAvailability.AVAILABLE,
                    return_code=0,
                    stdout=outputs.get(probe, probe.value),
                    stderr="",
                )
                for probe in PreflightProbe
            ),
        )


class _DeploymentBackend:
    def __init__(self, *, uid: int = 1000, username: str = "trainer") -> None:
        self.uid = uid
        self.username = username

    def inspect_runtime_identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(
            username=self.username,
            primary_group="root" if self.uid == 0 else "research",
            uid=self.uid,
            home_directory="/root" if self.uid == 0 else "/home/trainer",
        )

    def inspect_privilege(self, **_kwargs: object) -> DeploymentPrivilege:
        return DeploymentPrivilege.ROOT if self.uid == 0 else DeploymentPrivilege.SUDO


def test_root_ssh_identity_is_allowed_with_an_explicit_warning() -> None:
    @contextmanager
    def factory(**_kwargs: object):
        yield _DeploymentBackend(uid=0, username="root")

    manager = AutomatedNodeDeploymentManager(
        center_base_url="https://center.example.internal",
        backend_factory=factory,
        preflight_service=_PassingPreflightService(),  # type: ignore[arg-type]
    )

    result = manager.preflight_worker(
        node={**_node(), "ssh_username": "root"},
        confirmed_host_key=_host_key(),
        ssh_password="root-password",
        sudo_password_mode="not_required",
        sudo_password=None,
    )

    identity = next(
        check for check in result["checks"] if check["code"] == "runtime_identity"
    )
    assert result["ready"] is True
    assert identity["status"] == "warning"
    assert "root 身份" in identity["detail"]


def test_deployment_manager_scopes_backend_to_one_context() -> None:
    lifecycle: list[str] = []
    seen: dict[str, object] = {}
    backend = _DeploymentBackend()

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
        center_ca_certificate=TEST_CENTER_CA,
        backend_factory=factory,
        preflight_service=_PassingPreflightService(),  # type: ignore[arg-type]
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

    assert lifecycle == ["entered", "exited", "entered", "exited"]
    assert result["worker_version"] == "0.1.0"
    assert seen["ssh_password"] == "one-use-password"
    assert seen["request"].center_ca_certificate == TEST_CENTER_CA


def test_removal_manager_reuses_the_confirmed_host_pin_and_one_ssh_context() -> None:
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

    class Remover:
        def remove(self, actual_backend: object, request: object) -> object:
            assert actual_backend is backend
            seen["request"] = request
            return SimpleNamespace(removed=True)

    manager = AutomatedNodeDeploymentManager(
        center_base_url="https://center.example.internal",
        backend_factory=factory,
    )
    manager._remover = Remover()  # type: ignore[assignment]
    node = _node()
    pin = _host_key()
    node.update(
        {
            "host_key_algorithm": pin["algorithm"],
            "host_public_key": pin["public_key"],
            "host_key_fingerprint": pin["sha256_fingerprint"],
        }
    )

    result = manager.remove_worker(
        node=node,
        ssh_password="one-use-removal-password",
        sudo_password_mode="same_as_ssh",
        sudo_password=None,
    )

    assert result["message"] == "Training Worker removed from the node."
    assert lifecycle == ["entered", "exited"]
    assert seen["ssh_password"] == "one-use-removal-password"
    assert seen["request"].node_ref == "node_context01"


def test_deployment_manager_translates_sanitized_ssh_failure() -> None:
    @contextmanager
    def unavailable(**_kwargs: object):
        raise SshTransportError("safe transport failure")
        yield  # pragma: no cover

    manager = AutomatedNodeDeploymentManager(
        center_base_url="https://center.example.internal",
        backend_factory=unavailable,
        preflight_service=_PassingPreflightService(),  # type: ignore[arg-type]
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
