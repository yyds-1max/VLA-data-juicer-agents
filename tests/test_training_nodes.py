from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.errors import TrainingValidationError
from vla_data_juicer_agents.training.migrations import _MIGRATION_001
from vla_data_juicer_agents.training.resources import FakeResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore


@pytest.fixture
def store(tmp_path: Path) -> TrainingStore:
    return TrainingStore(tmp_path / "training.sqlite")


@pytest.fixture
def service(store: TrainingStore) -> TrainingService:
    return TrainingService(store, FakeResourceProvider(store))


def _client(service: TrainingService, *, admin: bool) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_training_router(
            service,
            settings=TrainingSettings(
                simulation_enabled=True,
                development_admin=admin,
            ),
        )
    )
    return TestClient(app)


def _node_payload() -> dict[str, object]:
    return {
        "name": "共享训练节点",
        "description": "Metadata only; SSH credentials are never persisted.",
        "address": "192.0.2.10",
        "ssh_port": 2222,
        "ssh_username": "trainer",
    }


def _capabilities() -> dict[str, object]:
    return {
        "hostname": "training-12",
        "operating_system": "Linux 6.8",
        "architecture": "x86_64",
        "python_version": "3.11.9",
        "nvidia_driver_version": "550.54",
        "cuda_version": "12.4",
        "conda_environments": ["navila"],
        "worker_features": ["resource_reporting"],
    }


def _resources() -> dict[str, object]:
    return {
        "cpu": {"logical_cores": 64, "load_1m": 1.5},
        "memory": {
            "total_bytes": 274_877_906_944,
            "available_bytes": 137_438_953_472,
        },
        "disks": [
            {
                "mount": "/data",
                "total_bytes": 10_000_000,
                "available_bytes": 4_000_000,
            }
        ],
        "gpus": [
            {
                "uuid": "GPU-0001",
                "index": 0,
                "name": "NVIDIA A100",
                "memory_total_bytes": 85_899_345_920,
                "memory_used_bytes": 42_949_672_960,
                "utilization_percent": 92.5,
                "temperature_celsius": 71.0,
            }
        ],
    }


def _create_node(client: TestClient) -> dict[str, object]:
    response = client.post("/api/training/nodes", json=_node_payload())
    assert response.status_code == 201, response.text
    return response.json()["node"]


class _FakeDeploymentManager:
    def __init__(self, *, insufficient: bool = False) -> None:
        self.insufficient = insufficient
        self.seen_ssh_password: str | None = None
        self.seen_sudo_password: str | None = None
        self.seen_enrollment_token: str | None = None
        self.removed = False
        self.preflight_calls = 0

    def discover_host_key(self, node: dict[str, object]) -> dict[str, str]:
        assert node["address"] == "192.0.2.10"
        return {
            "algorithm": "ssh-ed25519",
            "public_key": "A" * 40,
            "sha256_fingerprint": "SHA256:" + "B" * 43,
        }

    def deploy_worker(self, **kwargs: object) -> dict[str, str]:
        self.seen_ssh_password = str(kwargs["ssh_password"])
        self.seen_sudo_password = str(kwargs["sudo_password"])
        self.seen_enrollment_token = str(kwargs["enrollment_token"])
        if self.insufficient:
            raise TrainingValidationError(
                "training_node_deployment_account_insufficient",
                "部署账号权限不足",
            )
        return {"worker_version": "0.1.0", "message": "Worker deployed."}

    def preflight_worker(self, **kwargs: object) -> dict[str, object]:
        self.preflight_calls += 1
        self.seen_ssh_password = str(kwargs["ssh_password"])
        return {
            "ready": not self.insufficient,
            "checked_at": "2026-08-13T08:00:00+00:00",
            "checks": [
                {
                    "code": "deployment_privilege",
                    "label": "部署账号权限",
                    "status": "failed" if self.insufficient else "passed",
                    "detail": "部署账号权限不足" if self.insufficient else "已验证 sudo 权限。",
                }
            ],
        }

    def remove_worker(self, **kwargs: object) -> dict[str, str]:
        self.seen_ssh_password = str(kwargs["ssh_password"])
        self.seen_sudo_password = str(kwargs["sudo_password"])
        self.removed = True
        return {"message": "Worker removed."}


def _deployment_client(
    store: TrainingStore, manager: _FakeDeploymentManager, *, admin: bool = True
) -> TestClient:
    service = TrainingService(
        store,
        FakeResourceProvider(store),
        node_deployment_manager=manager,
    )
    return _client(service, admin=admin)


def test_existing_m1_database_is_upgraded_without_recreating_training_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "training.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE training_schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"""
        )
        db.executescript(_MIGRATION_001)
        db.execute(
            """INSERT INTO training_schema_migrations(version,name,applied_at)
            VALUES(1,'training_platform_m1','2026-01-01T00:00:00+00:00')"""
        )
        db.execute(
            """INSERT INTO registered_models(
            model_ref,name,description,status,current_revision,created_at,updated_at)
            VALUES('model_existing','Existing','','draft',1,
            '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"""
        )
        db.commit()

    TrainingStore(path)

    with sqlite3.connect(path) as db:
        versions = db.execute(
            "SELECT version FROM training_schema_migrations ORDER BY version"
        ).fetchall()
        model_name = db.execute(
            "SELECT name FROM registered_models WHERE model_ref='model_existing'"
        ).fetchone()[0]
        node_table = db.execute(
            """SELECT name FROM sqlite_master
            WHERE type='table' AND name='training_nodes'"""
        ).fetchone()
    assert versions == [(1,), (2,), (3,), (4,)]
    assert model_name == "Existing"
    assert node_table == ("training_nodes",)


def _issue_token(client: TestClient, node: dict[str, object]) -> str:
    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/enrollment-tokens",
        json={
            "expected_revision": node["state_revision"],
            "expires_in_seconds": 600,
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return str(response.json()["enrollment_token"])


def _enroll(client: TestClient, token: str) -> dict[str, object]:
    response = client.post(
        "/api/training/nodes/enroll",
        json={
            "enrollment_token": token,
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "capabilities": _capabilities(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_default_principal_reads_redacted_node_but_cannot_create(
    service: TrainingService,
) -> None:
    admin = _client(service, admin=True)
    node = _create_node(admin)

    readonly = _client(service, admin=False)
    capabilities = readonly.get("/api/training/capabilities").json()
    assert capabilities["permissions"] == ["training:view"]
    listed = readonly.get("/api/training/nodes")
    assert listed.status_code == 200
    projection = listed.json()["nodes"][0]
    assert projection["node_ref"] == node["node_ref"]
    assert "address" not in projection
    assert "ssh_port" not in projection
    assert "ssh_username" not in projection
    assert "host_public_key" not in projection

    denied = readonly.post("/api/training/nodes", json=_node_payload())
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "training_write_forbidden"


def test_one_click_deployment_uses_ephemeral_credentials_and_persists_only_host_pin(
    store: TrainingStore,
) -> None:
    manager = _FakeDeploymentManager()
    client = _deployment_client(store, manager)
    node = _create_node(client)
    discovered = client.post(
        f"/api/training/nodes/{node['node_ref']}/host-key"
    )
    assert discovered.status_code == 200
    host_key = discovered.json()["host_key"]

    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/deploy-worker",
        json={
            "expected_revision": node["state_revision"],
            "confirmed_host_key": host_key,
            "host_key_confirmed": True,
            "ssh_password": "ephemeral-ssh-password",
            "sudo_password_mode": "same_as_ssh",
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["node"]["deployment_status"] == "succeeded"
    assert result["node"]["installed_worker_version"] == "0.1.0"
    assert result["node"]["host_key_fingerprint"] == host_key["sha256_fingerprint"]
    assert manager.seen_ssh_password == "ephemeral-ssh-password"
    assert manager.seen_sudo_password == "None"
    assert manager.seen_enrollment_token is not None
    with store.connection() as db:
        database_dump = "\n".join(db.iterdump())
        active_tokens = db.execute(
            "SELECT COUNT(*) FROM training_node_enrollment_tokens WHERE consumed_at IS NULL"
        ).fetchone()[0]
    assert "ephemeral-ssh-password" not in database_dump
    assert manager.seen_enrollment_token not in database_dump
    assert active_tokens == 0
    assert response.headers["cache-control"] == "no-store"


def test_worker_preflight_is_read_only_and_never_persists_credentials(
    store: TrainingStore,
) -> None:
    manager = _FakeDeploymentManager()
    client = _deployment_client(store, manager)
    node = _create_node(client)
    host_key = manager.discover_host_key(node)

    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/preflight-worker",
        json={
            "expected_revision": node["state_revision"],
            "confirmed_host_key": host_key,
            "host_key_confirmed": True,
            "ssh_password": "preflight-only-secret",
            "sudo_password_mode": "same_as_ssh",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["ready"] is True
    assert manager.preflight_calls == 1
    unchanged = client.get(f"/api/training/nodes/{node['node_ref']}").json()["node"]
    assert unchanged["state_revision"] == node["state_revision"]
    assert unchanged["deployment_status"] == "not_started"
    with store.connection() as db:
        database_dump = "\n".join(db.iterdump())
    assert "preflight-only-secret" not in database_dump


def test_worker_removal_revokes_access_and_returns_node_to_pending(
    store: TrainingStore,
) -> None:
    manager = _FakeDeploymentManager()
    client = _deployment_client(store, manager)
    node = _create_node(client)
    host_key = manager.discover_host_key(node)
    deployed = client.post(
        f"/api/training/nodes/{node['node_ref']}/deploy-worker",
        json={
            "expected_revision": node["state_revision"],
            "confirmed_host_key": host_key,
            "host_key_confirmed": True,
            "ssh_password": "deployment-secret",
            "sudo_password_mode": "same_as_ssh",
        },
    ).json()["node"]

    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/remove-worker",
        json={
            "expected_revision": deployed["state_revision"],
            "ssh_password": "removal-secret",
            "sudo_password_mode": "same_as_ssh",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    removed = response.json()["node"]
    assert manager.removed is True
    assert manager.seen_ssh_password == "removal-secret"
    assert removed["status"] == "pending_enrollment"
    assert removed["deployment_status"] == "not_started"
    assert removed["installed_worker_version"] is None
    assert removed["worker_version"] is None
    assert removed["enrolled_at"] is None
    assert client.get(
        f"/api/training/nodes/{node['node_ref']}/resources"
    ).json()["resources"] is None
    with store.connection() as db:
        database_dump = "\n".join(db.iterdump())
        worker_digest = db.execute(
            "SELECT worker_token_sha256 FROM training_nodes WHERE node_ref=?",
            (node["node_ref"],),
        ).fetchone()[0]
    assert worker_digest is None
    assert "removal-secret" not in database_dump


def test_heartbeat_does_not_change_management_revision(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    node = _create_node(client)
    enrolled = _enroll(client, _issue_token(client, node))
    before = enrolled["node"]["state_revision"]

    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/heartbeat",
        headers={"Authorization": f"Bearer {enrolled['worker_token']}"},
        json={
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "health": "healthy",
            "resources": _resources(),
        },
    )

    assert response.status_code == 200
    assert response.json()["state_revision"] == before


def test_deployment_account_without_root_or_sudo_returns_explicit_error(
    store: TrainingStore,
) -> None:
    manager = _FakeDeploymentManager(insufficient=True)
    client = _deployment_client(store, manager)
    node = _create_node(client)
    host_key = manager.discover_host_key(node)

    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/deploy-worker",
        json={
            "expected_revision": node["state_revision"],
            "confirmed_host_key": host_key,
            "host_key_confirmed": True,
            "ssh_password": "ephemeral-ssh-password",
            "sudo_password_mode": "same_as_ssh",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "training_node_deployment_account_insufficient",
        "message": "部署账号权限不足",
    }
    failed = client.get(f"/api/training/nodes/{node['node_ref']}").json()["node"]
    assert failed["deployment_status"] == "failed"
    with store.connection() as db:
        active_tokens = db.execute(
            "SELECT COUNT(*) FROM training_node_enrollment_tokens WHERE consumed_at IS NULL"
        ).fetchone()[0]
    assert active_tokens == 0


def test_readonly_principal_cannot_discover_deploy_or_remove_node_worker(
    store: TrainingStore,
) -> None:
    manager = _FakeDeploymentManager()
    admin = _deployment_client(store, manager)
    node = _create_node(admin)
    readonly = _deployment_client(store, manager, admin=False)

    discovered = readonly.post(
        f"/api/training/nodes/{node['node_ref']}/host-key"
    )
    assert discovered.status_code == 403
    deployed = readonly.post(
        f"/api/training/nodes/{node['node_ref']}/deploy-worker",
        json={
            "expected_revision": node["state_revision"],
            "confirmed_host_key": manager.discover_host_key(node),
            "host_key_confirmed": True,
            "ssh_password": "must-not-be-used",
            "sudo_password_mode": "same_as_ssh",
        },
    )
    assert deployed.status_code == 403
    assert manager.seen_ssh_password is None
    preflight = readonly.post(
        f"/api/training/nodes/{node['node_ref']}/preflight-worker",
        json={
            "expected_revision": node["state_revision"],
            "confirmed_host_key": manager.discover_host_key(node),
            "host_key_confirmed": True,
            "ssh_password": "must-not-be-used",
            "sudo_password_mode": "same_as_ssh",
        },
    )
    assert preflight.status_code == 403
    assert manager.preflight_calls == 0
    removed = readonly.post(
        f"/api/training/nodes/{node['node_ref']}/remove-worker",
        json={
            "expected_revision": node["state_revision"],
            "ssh_password": "must-not-be-used",
            "sudo_password_mode": "same_as_ssh",
        },
    )
    assert removed.status_code == 403
    assert manager.removed is False


def test_enrollment_token_is_hashed_short_lived_and_single_use(
    service: TrainingService, store: TrainingStore
) -> None:
    client = _client(service, admin=True)
    node = _create_node(client)
    token = _issue_token(client, node)

    with store.connection() as db:
        row = db.execute(
            "SELECT token_sha256 FROM training_node_enrollment_tokens"
        ).fetchone()
        database_dump = "\n".join(db.iterdump())
    assert row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in database_dump

    enrolled = _enroll(client, token)
    assert enrolled["node"]["status"] == "online"
    assert enrolled["worker_token"].startswith("worker_")

    reused = client.post(
        "/api/training/nodes/enroll",
        json={
            "enrollment_token": token,
            "worker_instance_id": "worker-instance-2",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "capabilities": _capabilities(),
        },
    )
    assert reused.status_code == 403
    assert reused.json()["detail"]["code"] == "invalid_enrollment_token"

    with store.connection() as db:
        worker_digest = db.execute(
            "SELECT worker_token_sha256 FROM training_nodes"
        ).fetchone()[0]
        database_dump = "\n".join(db.iterdump())
    assert worker_digest == hashlib.sha256(
        enrolled["worker_token"].encode()
    ).hexdigest()
    assert enrolled["worker_token"] not in database_dump


def test_expired_enrollment_token_is_rejected(
    service: TrainingService, store: TrainingStore
) -> None:
    client = _client(service, admin=True)
    token = _issue_token(client, _create_node(client))
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
        timespec="milliseconds"
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE training_node_enrollment_tokens SET expires_at=?",
            (expired_at,),
        )

    response = client.post(
        "/api/training/nodes/enroll",
        json={
            "enrollment_token": token,
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "capabilities": _capabilities(),
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "invalid_enrollment_token"


def test_authenticated_heartbeat_records_health_and_resource_snapshot(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    node = _create_node(client)
    enrolled = _enroll(client, _issue_token(client, node))
    node_ref = enrolled["node"]["node_ref"]
    worker_token = enrolled["worker_token"]

    unauthenticated = client.post(
        f"/api/training/nodes/{node_ref}/heartbeat",
        json={
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "health": "healthy",
            "resources": _resources(),
        },
    )
    assert unauthenticated.status_code == 403

    response = client.post(
        f"/api/training/nodes/{node_ref}/heartbeat",
        headers={"Authorization": f"Bearer {worker_token}"},
        json={
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.1",
            "protocol_version": 1,
            "health": "degraded",
            "health_message": "One GPU is occupied by a colleague.",
            "resources": _resources(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "degraded"
    assert response.json()["next_heartbeat_seconds"] == 15

    resources = client.get(
        f"/api/training/nodes/{node_ref}/resources"
    ).json()
    assert resources["stale"] is False
    assert resources["resources"]["gpus"][0]["utilization_percent"] == 92.5
    detail = client.get(f"/api/training/nodes/{node_ref}").json()["node"]
    assert detail["worker_version"] == "0.1.1"
    assert detail["health_message"] == "One GPU is occupied by a colleague."


def test_offline_is_server_derived_and_disabled_revokes_worker(
    service: TrainingService, store: TrainingStore
) -> None:
    client = _client(service, admin=True)
    node = _create_node(client)
    enrolled = _enroll(client, _issue_token(client, node))
    node_ref = enrolled["node"]["node_ref"]

    old_heartbeat = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
        timespec="milliseconds"
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE training_nodes SET last_heartbeat_at=? WHERE node_ref=?",
            (old_heartbeat, node_ref),
        )
    detail = client.get(f"/api/training/nodes/{node_ref}").json()["node"]
    assert detail["status"] == "offline"

    disabled = client.put(
        f"/api/training/nodes/{node_ref}",
        json={
            "expected_revision": detail["state_revision"],
            "desired_state": "disabled",
        },
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["node"]["status"] == "disabled"
    heartbeat = client.post(
        f"/api/training/nodes/{node_ref}/heartbeat",
        headers={"Authorization": f"Bearer {enrolled['worker_token']}"},
        json={
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "health": "healthy",
            "resources": _resources(),
        },
    )
    assert heartbeat.status_code == 403
    assert heartbeat.json()["detail"]["code"] == "worker_authentication_failed"


def test_revision_conflict_and_delete_safety(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    pending = _create_node(client)
    conflict = client.put(
        f"/api/training/nodes/{pending['node_ref']}",
        json={"expected_revision": 99, "name": "stale update"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "training_node_revision_conflict"

    deleted = client.delete(
        f"/api/training/nodes/{pending['node_ref']}",
        params={"expected_revision": pending["state_revision"]},
    )
    assert deleted.status_code == 204

    enrolled_node = _create_node(client)
    enrolled = _enroll(client, _issue_token(client, enrolled_node))
    cannot_delete = client.delete(
        f"/api/training/nodes/{enrolled_node['node_ref']}",
        params={"expected_revision": enrolled["node"]["state_revision"]},
    )
    assert cannot_delete.status_code == 409
    assert cannot_delete.json()["detail"]["code"] == "training_node_has_history"


def test_heartbeat_rejects_invalid_resource_invariants(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    node = _create_node(client)
    enrolled = _enroll(client, _issue_token(client, node))
    resources = _resources()
    resources["memory"] = {"total_bytes": 10, "available_bytes": 11}

    response = client.post(
        f"/api/training/nodes/{node['node_ref']}/heartbeat",
        headers={"Authorization": f"Bearer {enrolled['worker_token']}"},
        json={
            "worker_instance_id": "worker-instance-1",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "health": "healthy",
            "resources": resources,
        },
    )
    assert response.status_code == 422
