from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.resources import TrainingResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore


def _client(service: TrainingService, *, admin: bool = True) -> TestClient:
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


def _resources() -> dict[str, object]:
    return {
        "cpu": {"logical_cores": 16, "load_1m": 1.0},
        "memory": {"total_bytes": 1000, "available_bytes": 500},
        "disks": [{"mount": "/data", "total_bytes": 2000, "available_bytes": 1000}],
        "gpus": [],
    }


def _register_online_node(client: TestClient) -> tuple[dict[str, object], str]:
    node = client.post(
        "/api/training/nodes",
        json={
            "name": "NaVILA node",
            "address": "192.0.2.20",
            "ssh_port": 22,
        },
    ).json()["node"]
    token = client.post(
        f"/api/training/nodes/{node['node_ref']}/enrollment-tokens",
        json={"expected_revision": node["state_revision"], "expires_in_seconds": 600},
    ).json()["enrollment_token"]
    enrolled = client.post(
        "/api/training/nodes/enroll",
        json={
            "enrollment_token": token,
            "worker_instance_id": "worker-test-1",
            "worker_version": "0.2.0",
            "protocol_version": 1,
            "capabilities": {
                "hostname": "training-node",
                "operating_system": "Linux",
                "architecture": "x86_64",
                "python_version": "3.12",
                "conda_environments": [],
                "worker_features": ["model_configuration_verification"],
            },
        },
    ).json()
    return enrolled["node"], enrolled["worker_token"]


def _heartbeat(client: TestClient, node_ref: str, worker_token: str) -> dict[str, object]:
    response = client.post(
        f"/api/training/nodes/{node_ref}/heartbeat",
        headers={"Authorization": f"Bearer {worker_token}"},
        json={
            "worker_instance_id": "worker-test-1",
            "worker_version": "0.2.0",
            "protocol_version": 1,
            "health": "healthy",
            "resources": _resources(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _model_payload(node_ref: str) -> dict[str, object]:
    return {
        "family_name": "NaVILA",
        "configuration": {
            "launch_template": {
                "domain": "vla",
                "server_ref": node_ref,
                "working_directory": "/data/caiji_test/NaVILA",
                "launcher_kind": "torchrun",
                "executable": "torchrun",
                "entrypoint": "llava/train/train_mem.py",
                "fixed_argv": [],
                "output_root": "/data/caiji_test/outputs",
                "runtime_environment": {"kind": "system"},
                "monitoring": {"source": "stdout", "format": "plain"},
            },
            "parameter_definitions": [
                {
                    "key": "num_video_frames",
                    "label": "视频帧数",
                    "type": "integer",
                    "default": 4,
                    "minimum": 1,
                    "maximum": 32,
                }
            ],
        },
    }


def test_worker_claims_and_completes_read_only_model_verification(tmp_path: Path) -> None:
    store = TrainingStore(tmp_path / "training.sqlite")
    service = TrainingService(store, TrainingResourceProvider(store))
    admin = _client(service)
    node, worker_token = _register_online_node(admin)
    _heartbeat(admin, str(node["node_ref"]), worker_token)
    created = admin.post("/api/training/models", json=_model_payload(str(node["node_ref"])))
    assert created.status_code == 201, created.text
    model = created.json()["model"]

    queued = admin.post(
        f"/api/training/models/{model['family_ref']}/verify",
        json={"expected_revision": model["edit_revision"]},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["model"]["verification"]["status"] == "queued"

    heartbeat = _heartbeat(admin, str(node["node_ref"]), worker_token)
    command = heartbeat["command"]
    assert command["kind"] == "verify_model_configuration"
    assert command["payload"]["working_directory"] == "/data/caiji_test/NaVILA"
    assert "fixed_argv" not in command["payload"]

    result = admin.post(
        f"/api/training/nodes/{node['node_ref']}/commands/{command['command_ref']}/result",
        headers={"Authorization": f"Bearer {worker_token}"},
        json={
            "worker_instance_id": "worker-test-1",
            "status": "succeeded",
            "checks": [
                {
                    "code": "working_directory",
                    "label": "工程目录",
                    "status": "passed",
                    "detail": "工程目录存在且 Worker 可以读取。",
                }
            ],
        },
    )
    assert result.status_code == 200, result.text
    verified = admin.get(f"/api/training/models/{model['family_ref']}").json()["model"]
    assert verified["status"] == "verified"
    assert verified["trained_version_count"] == 0
    assert verified["edit_revision"] == model["edit_revision"]
    assert verified["verification"]["status"] == "succeeded"
    assert verified["verification"]["checks"][0]["code"] == "working_directory"

    readonly = _client(service, admin=False)
    safe = readonly.get(f"/api/training/models/{model['family_ref']}").json()["model"]
    assert safe["configuration"]["launch_template"] == {"domain": "vla", "server_ref": node["node_ref"]}
    assert "checks" not in safe["verification"]

    edited_payload = _model_payload(str(node["node_ref"]))
    edited_payload = {
        "expected_revision": verified["edit_revision"],
        "configuration": edited_payload["configuration"],
    }
    edited = admin.put(
        f"/api/training/models/{model['family_ref']}", json=edited_payload
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["model"]["status"] == "draft"
    assert "verification" not in edited.json()["model"]


def test_verification_requires_online_real_worker_and_worker_authentication(tmp_path: Path) -> None:
    store = TrainingStore(tmp_path / "training.sqlite")
    service = TrainingService(store, TrainingResourceProvider(store))
    admin = _client(service)
    pending_node = admin.post(
        "/api/training/nodes",
        json={
            "name": "Pending node",
            "address": "192.0.2.21",
            "ssh_port": 22,
        },
    ).json()["node"]
    pending_model = admin.post(
        "/api/training/models",
        json=_model_payload(str(pending_node["node_ref"])),
    ).json()["model"]
    rejected = admin.post(
        f"/api/training/models/{pending_model['family_ref']}/verify",
        json={"expected_revision": pending_model["edit_revision"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "model_verification_node_unavailable"

    node, worker_token = _register_online_node(admin)
    _heartbeat(admin, str(node["node_ref"]), worker_token)
    model = admin.post(
        "/api/training/models", json=_model_payload(str(node["node_ref"]))
    ).json()["model"]
    admin.post(
        f"/api/training/models/{model['family_ref']}/verify",
        json={"expected_revision": model["edit_revision"]},
    )
    command = _heartbeat(admin, str(node["node_ref"]), worker_token)["command"]
    unauthenticated = admin.post(
        f"/api/training/nodes/{node['node_ref']}/commands/{command['command_ref']}/result",
        headers={"Authorization": "Bearer worker_" + "x" * 48},
        json={
            "worker_instance_id": "worker-test-1",
            "status": "failed",
            "checks": [{"code": "entrypoint", "label": "入口", "status": "failed", "detail": "不存在"}],
        },
    )
    assert unauthenticated.status_code == 403
    assert unauthenticated.json()["detail"]["code"] == "worker_authentication_failed"
