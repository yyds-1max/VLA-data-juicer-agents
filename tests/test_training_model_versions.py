from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training import migrations
from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.resources import TrainingResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore


_MIGRATION_NAMES = [
    "training_platform_m1",
    "training_nodes_m2",
    "training_node_deployment_m3",
    "model_families_m4",
    "model_worker_verification_m5",
    "training_node_revision_split_m6",
    "training_workflows_m7",
    "training_node_deletion_history_m8",
    "training_datasets_m9",
    "training_node_command_claim_tokens_m10",
    "dataset_transfer_pause_cancel_m11",
    "real_training_execution_m12",
]


def _create_v12_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE training_schema_migrations(
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
            )"""
        )
        for version, name in enumerate(_MIGRATION_NAMES, start=1):
            db.executescript(getattr(migrations, f"_MIGRATION_{version:03d}"))
            db.execute(
                """INSERT INTO training_schema_migrations(version,name,applied_at)
                VALUES(?,?,?)""",
                (version, name, "2026-08-21T00:00:00Z"),
            )
            db.commit()


def test_v12_to_v13_preserves_existing_node_commands_and_is_repeatable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "training-v12.sqlite"
    _create_v12_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO training_node_commands(
            command_ref,node_id,node_ref_snapshot,kind,status,request_json,
            result_json,worker_instance_id,lease_expires_at,created_at,
            started_at,finished_at,updated_at,claim_token_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "command_existing",
                None,
                "node_deleted_snapshot",
                "list_directories",
                "running",
                '{"path":"/data"}',
                None,
                "worker-existing",
                "2026-08-21T00:10:00Z",
                "2026-08-21T00:00:00Z",
                "2026-08-21T00:01:00Z",
                None,
                "2026-08-21T00:01:00Z",
                "digest-existing",
            ),
        )
        db.commit()

    TrainingStore(path)
    TrainingStore(path)

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT version,name FROM training_schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (14, "dataset_replica_node_recovery_m14")
        command = db.execute(
            """SELECT command_ref,node_ref_snapshot,kind,status,request_json,
            worker_instance_id,lease_expires_at,claim_token_sha256
            FROM training_node_commands WHERE command_ref=?""",
            ("command_existing",),
        ).fetchone()
        assert command == (
            "command_existing",
            "node_deleted_snapshot",
            "list_directories",
            "running",
            '{"path":"/data"}',
            "worker-existing",
            "2026-08-21T00:10:00Z",
            "digest-existing",
        )
        assert db.execute(
            """SELECT name FROM sqlite_master
            WHERE type='table' AND name='training_artifact_inspections'"""
        ).fetchone() == ("training_artifact_inspections",)
        metric_index = db.execute(
            """SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_metric_samples_run_stage_seq'"""
        ).fetchone()
        assert metric_index is not None
        assert "seq DESC" in str(metric_index[0])


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


def _setup_library(
    tmp_path: Path,
) -> tuple[TrainingStore, TrainingService, TestClient, dict[str, object], str]:
    store = TrainingStore(tmp_path / "training.sqlite")
    service = TrainingService(
        store,
        TrainingResourceProvider(store),
        real_execution_enabled=True,
    )
    client = _client(service)
    node = store.create_node(
        {
            "name": "版本资产训练节点",
            "description": "model version test",
            "address": "192.0.2.20",
            "ssh_port": 22,
            "ssh_username": "trainer",
        },
        "test",
    )
    enrollment = store.create_enrollment_token(
        str(node["node_ref"]), int(node["state_revision"]), 600, "test"
    )
    capabilities = {
        "hostname": "version-node",
        "operating_system": "Linux",
        "architecture": "x86_64",
        "worker_features": [
            "resource_reporting",
            "training_execution_v1",
            "training_artifact_inspection_v1",
        ],
        "training_execution_v1": True,
        "training_artifact_inspection_v1": True,
    }
    enrolled = store.enroll_node(
        {
            "enrollment_token": enrollment["enrollment_token"],
            "worker_instance_id": "worker-version-1",
            "worker_version": "0.3.0",
            "protocol_version": 1,
            "capabilities": capabilities,
        }
    )
    worker_token = str(enrolled["worker_token"])
    store.record_node_heartbeat(
        str(node["node_ref"]),
        worker_token,
        {
            "worker_instance_id": "worker-version-1",
            "worker_version": "0.3.0",
            "protocol_version": 1,
            "health": "healthy",
            "health_message": None,
            "capabilities": capabilities,
            "resources": {
                "cpu": {"logical_cores": 16},
                "memory": {"total_bytes": 64_000, "available_bytes": 32_000},
                "disks": [],
                "gpus": [
                    {
                        "uuid": "GPU-version-0",
                        "index": 0,
                        "name": "A100",
                        "memory_total_bytes": 80_000,
                        "memory_used_bytes": 0,
                        "utilization_percent": 0.0,
                        "temperature_celsius": 35.0,
                    }
                ],
            },
        },
    )
    return store, service, client, node, worker_token


def _create_model(
    store: TrainingStore,
    client: TestClient,
    node_ref: str,
    *,
    family_name: str,
) -> dict[str, object]:
    response = client.post(
        "/api/training/models",
        json={
            "family_name": family_name,
            "configuration": {
                "launch_template": {
                    "domain": "vla",
                    "server_ref": node_ref,
                    "working_directory": "/data/navila",
                    "launcher_kind": "direct",
                    "executable": "python",
                    "entrypoint": "train.py",
                    "fixed_argv": [],
                    "output_root": "/data/outputs",
                    "output_flag": "--output_dir",
                },
                "parameter_definitions": [
                    {
                        "key": "max_steps",
                        "label": "steps",
                        "type": "integer",
                        "default": 2,
                        "minimum": 1,
                        "maximum": 10,
                        "cli_flag": "--max_steps",
                    },
                    {
                        "key": "private_token",
                        "label": "token",
                        "type": "string",
                        "default": "model-secret",
                        "sensitive": True,
                        "cli_flag": "--private_token",
                    },
                ],
            },
        },
    )
    assert response.status_code == 201, response.text
    model = response.json()["model"]
    with store.transaction() as db:
        db.execute(
            """UPDATE registered_models SET status='verified'
            WHERE family_id=(SELECT id FROM model_families WHERE family_ref=?)""",
            (model["family_ref"],),
        )
    return model


def _create_and_finish_real_run(
    client: TestClient,
    node: dict[str, object],
    model: dict[str, object],
    worker_token: str,
    *,
    idempotency_key: str,
    terminal_status: str = "succeeded",
) -> dict[str, object]:
    created = client.post(
        "/api/training/runs",
        headers={"Idempotency-Key": idempotency_key},
        json={
            "family_ref": model["family_ref"],
            "server_ref": node["node_ref"],
            "gpu_uuids": ["GPU-version-0"],
            "stages": [
                {
                    "parameters": {"max_steps": 2},
                    "stage_input_source": "manual",
                }
            ],
            "execution_mode": "real",
            "version_description": f"{idempotency_key} 版本说明",
        },
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    stage = run["stages"][0]
    auth = {"Authorization": f"Bearer {worker_token}"}
    action_response = client.post(
        f"/api/training/nodes/{node['node_ref']}/training-actions/poll",
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "wait_seconds": 0,
            "limit": 1,
        },
    )
    assert action_response.status_code == 200, action_response.text
    action = action_response.json()["actions"][0]
    accepted = client.post(
        f"/api/training/nodes/{node['node_ref']}/training-actions/{action['action_ref']}/result",
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "claim_token": action["claim_token"],
            "status": "succeeded",
            "result": {},
        },
    )
    assert accepted.status_code == 200, accepted.text
    update_url = (
        f"/api/training/nodes/{node['node_ref']}/runs/{run['run_ref']}/updates"
    )
    started = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "owner_epoch": 1,
            "worker_seq": 1,
            "updates": [{"kind": "started", "stage_ref": stage["stage_ref"]}],
        },
    )
    assert started.status_code == 200, started.text
    metrics = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "owner_epoch": 1,
            "worker_seq": 2,
            "updates": [
                {
                    "kind": "metric",
                    "stage_ref": stage["stage_ref"],
                    "step": 2,
                    "total_steps": 2,
                    "loss": 0.25,
                    "learning_rate": 0.00001,
                },
                {
                    "kind": "metric",
                    "stage_ref": stage["stage_ref"],
                    "gpus": [
                        {
                            "uuid": "GPU-version-0",
                            "index": 0,
                            "utilization_percent": 50.0,
                            "gpu_memory_mib": 1024.0,
                            "temperature_celsius": 42.0,
                        }
                    ],
                },
                {
                    "kind": "metric",
                    "stage_ref": stage["stage_ref"],
                    "step": 2,
                    "total_steps": 2,
                },
            ],
        },
    )
    assert metrics.status_code == 200, metrics.text
    exited = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "owner_epoch": 1,
            "worker_seq": 3,
            "updates": [
                {
                    "kind": "exited",
                    "stage_ref": stage["stage_ref"],
                    "status": terminal_status,
                }
            ],
        },
    )
    assert exited.status_code == 200, exited.text
    return exited.json()


def test_model_version_library_filters_searches_and_paginates(
    tmp_path: Path,
) -> None:
    store, _service, client, node, worker_token = _setup_library(tmp_path)
    alpha = _create_model(
        store,
        client,
        str(node["node_ref"]),
        family_name="NaVILA Alpha",
    )
    alpha_v1 = _create_and_finish_real_run(
        client,
        node,
        alpha,
        worker_token,
        idempotency_key="alpha-v1",
    )
    alpha_v2 = _create_and_finish_real_run(
        client,
        node,
        alpha,
        worker_token,
        idempotency_key="alpha-v2",
    )
    simulated = _create_and_finish_real_run(
        client,
        node,
        alpha,
        worker_token,
        idempotency_key="alpha-simulated",
    )
    failed = _create_and_finish_real_run(
        client,
        node,
        alpha,
        worker_token,
        idempotency_key="alpha-failed",
        terminal_status="failed",
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE training_runs SET execution_mode='simulation' WHERE run_ref=?",
            (simulated["run_ref"],),
        )
        db.execute(
            """UPDATE training_artifacts SET simulated=1 WHERE version_id=(
            SELECT id FROM model_versions WHERE version_ref=?)""",
            (simulated["version_ref"],),
        )

    beta = _create_model(
        store,
        client,
        str(node["node_ref"]),
        family_name="NaVILA Beta",
    )
    beta_v1 = _create_and_finish_real_run(
        client,
        node,
        beta,
        worker_token,
        idempotency_key="beta-v1",
    )

    searched = client.get(
        "/api/training/model-version-families",
        params={"query": "alpha", "limit": 20},
    )
    assert searched.status_code == 200, searched.text
    assert [item["family_name"] for item in searched.json()["families"]] == [
        "NaVILA Alpha"
    ]
    assert searched.json()["families"][0]["available_version_count"] == 2

    first_page = client.get(
        "/api/training/model-version-families", params={"limit": 1}
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["families"]) == 1
    assert first_body["next_after"]
    second_page = client.get(
        "/api/training/model-version-families",
        params={"limit": 1, "after": first_body["next_after"]},
    )
    assert second_page.status_code == 200, second_page.text
    assert len(second_page.json()["families"]) == 1
    assert {
        first_body["families"][0]["family_ref"],
        second_page.json()["families"][0]["family_ref"],
    } == {alpha["family_ref"], beta["family_ref"]}

    versions_page = client.get(
        f"/api/training/model-version-families/{alpha['family_ref']}/versions",
        params={"limit": 1},
    )
    assert versions_page.status_code == 200, versions_page.text
    versions_body = versions_page.json()
    assert [item["version_ref"] for item in versions_body["versions"]] == [
        alpha_v2["version_ref"]
    ]
    assert versions_body["next_after"]
    older = client.get(
        f"/api/training/model-version-families/{alpha['family_ref']}/versions",
        params={"limit": 1, "after": versions_body["next_after"]},
    )
    assert older.status_code == 200, older.text
    assert [item["version_ref"] for item in older.json()["versions"]] == [
        alpha_v1["version_ref"]
    ]
    serialized = str(searched.json()) + str(versions_body) + str(older.json())
    assert simulated["version_ref"] not in serialized
    assert failed["version_ref"] not in serialized
    assert beta_v1["version_ref"] not in str(older.json())

    models = client.get("/api/training/models").json()["models"]
    counts = {model["family_ref"]: model["available_version_count"] for model in models}
    assert counts[alpha["family_ref"]] == 2
    assert counts[beta["family_ref"]] == 1


def test_model_version_detail_uses_trainer_metric_and_hides_private_paths(
    tmp_path: Path,
) -> None:
    store, service, admin, node, worker_token = _setup_library(tmp_path)
    model = _create_model(
        store,
        admin,
        str(node["node_ref"]),
        family_name="NaVILA Detail",
    )
    run = _create_and_finish_real_run(
        admin,
        node,
        model,
        worker_token,
        idempotency_key="detail-v1",
    )

    detail_response = admin.get(
        f"/api/training/model-versions/{run['version_ref']}"
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["version"]
    assert detail["final_step"] == 2
    assert detail["final_loss"] == 0.25
    assert detail["final_learning_rate"] == 0.00001
    assert detail["default_artifact"]["status"] == "unchecked"
    assert detail["default_artifact"]["path"].startswith("/data/outputs/")

    readonly = _client(service, admin=False)
    safe_response = readonly.get(
        f"/api/training/model-versions/{run['version_ref']}"
    )
    assert safe_response.status_code == 200, safe_response.text
    safe = safe_response.json()["version"]
    assert "path" not in safe["default_artifact"]
    serialized = str(safe)
    assert "/data/outputs" not in serialized
    assert "/data/navila" not in serialized
    assert "model-secret" not in serialized


def test_artifact_check_is_idempotent_and_worker_result_emits_safe_event(
    tmp_path: Path,
) -> None:
    store, _service, client, node, worker_token = _setup_library(tmp_path)
    model = _create_model(
        store,
        client,
        str(node["node_ref"]),
        family_name="NaVILA Artifact",
    )
    run = _create_and_finish_real_run(
        client,
        node,
        model,
        worker_token,
        idempotency_key="artifact-v1",
    )
    url = f"/api/training/model-versions/{run['version_ref']}/artifact-checks"
    requested = client.post(url, headers={"Idempotency-Key": "inspect-one"})
    assert requested.status_code == 202, requested.text
    inspection = requested.json()["inspection"]
    duplicate = client.post(url, headers={"Idempotency-Key": "inspect-two"})
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["inspection"]["inspection_ref"] == inspection["inspection_ref"]

    auth = {"Authorization": f"Bearer {worker_token}"}
    polled = client.post(
        f"/api/training/nodes/{node['node_ref']}/commands/poll",
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "wait_seconds": 0,
            "limit": 1,
        },
    )
    assert polled.status_code == 200, polled.text
    command = polled.json()["commands"][0]
    assert command["kind"] == "inspect_training_artifact"
    assert command["payload"]["artifact_ref"] == inspection["artifact_ref"]
    assert command["payload"]["version_ref"] == run["version_ref"]
    assert command["payload"]["run_ref"] == run["run_ref"]
    finished = client.post(
        f"/api/training/nodes/{node['node_ref']}/commands/{command['command_ref']}/result",
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "claim_token": command["claim_token"],
            "status": "succeeded",
            "artifact_ref": inspection["artifact_ref"],
            "version_ref": run["version_ref"],
            "availability": "available",
            "file_count": 17,
            "total_bytes": 123456,
        },
    )
    assert finished.status_code == 200, finished.text
    detail = client.get(
        f"/api/training/model-versions/{run['version_ref']}"
    ).json()["version"]
    assert detail["default_artifact"]["status"] == "available"
    assert detail["default_artifact"]["file_count"] == 17
    assert detail["default_artifact"]["total_bytes"] == 123456

    events = [
        event
        for event in store.list_events(after_seq=0, limit=100)["items"]
        if event["type"] == "model.version.artifact.updated"
    ]
    assert events
    event = events[-1]
    assert event["version_ref"] == run["version_ref"]
    assert event["status"] == "succeeded"
    assert event["availability"] == "available"
    serialized_event = str(event)
    assert "/data/outputs" not in serialized_event
    assert "path" not in event


def test_artifact_check_requires_online_capable_worker(tmp_path: Path) -> None:
    store, _service, client, node, worker_token = _setup_library(tmp_path)
    model = _create_model(
        store,
        client,
        str(node["node_ref"]),
        family_name="NaVILA Capability",
    )
    run = _create_and_finish_real_run(
        client,
        node,
        model,
        worker_token,
        idempotency_key="capability-v1",
    )
    url = f"/api/training/model-versions/{run['version_ref']}/artifact-checks"
    with store.transaction() as db:
        db.execute(
            """UPDATE training_nodes SET capabilities_json=? WHERE node_ref=?""",
            ('{"worker_features":["training_execution_v1"]}', node["node_ref"]),
        )
    unsupported = client.post(
        url, headers={"Idempotency-Key": "inspect-unsupported"}
    )
    assert unsupported.status_code == 409, unsupported.text
    assert unsupported.json()["detail"]["code"] == "training_worker_update_required"

    with store.transaction() as db:
        db.execute(
            """UPDATE training_nodes SET status='offline',capabilities_json=?
            WHERE node_ref=?""",
            (
                '{"worker_features":["training_execution_v1",'
                '"training_artifact_inspection_v1"],'
                '"training_artifact_inspection_v1":true}',
                node["node_ref"],
            ),
        )
    offline = client.post(url, headers={"Idempotency-Key": "inspect-offline"})
    assert offline.status_code == 409, offline.text
    assert offline.json()["detail"]["code"] == "training_node_unavailable"


def test_failed_artifact_check_does_not_leak_worker_path_to_readonly_view(
    tmp_path: Path,
) -> None:
    store, service, client, node, worker_token = _setup_library(tmp_path)
    model = _create_model(
        store,
        client,
        str(node["node_ref"]),
        family_name="NaVILA Failed Inspection",
    )
    run = _create_and_finish_real_run(
        client,
        node,
        model,
        worker_token,
        idempotency_key="failed-inspection-v1",
    )
    requested = client.post(
        f"/api/training/model-versions/{run['version_ref']}/artifact-checks",
        headers={"Idempotency-Key": "failed-inspection-check"},
    )
    assert requested.status_code == 202, requested.text
    auth = {"Authorization": f"Bearer {worker_token}"}
    command = client.post(
        f"/api/training/nodes/{node['node_ref']}/commands/poll",
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "wait_seconds": 0,
            "limit": 1,
        },
    ).json()["commands"][0]
    failed = client.post(
        f"/api/training/nodes/{node['node_ref']}/commands/{command['command_ref']}/result",
        headers=auth,
        json={
            "worker_instance_id": "worker-version-1",
            "claim_token": command["claim_token"],
            "status": "failed",
            "error": {
                "code": "artifact_inspection_failed",
                "message": "Cannot inspect /data/outputs/private-model",
            },
        },
    )
    assert failed.status_code == 200, failed.text
    admin_detail = client.get(
        f"/api/training/model-versions/{run['version_ref']}"
    ).json()["version"]
    assert admin_detail["default_artifact"]["status"] == "check_failed"

    readonly = _client(service, admin=False)
    safe = readonly.get(
        f"/api/training/model-versions/{run['version_ref']}"
    )
    assert safe.status_code == 200, safe.text
    assert "/data/outputs" not in str(safe.json())
    events = [
        event
        for event in store.list_events(after_seq=0, limit=100)["items"]
        if event["type"] == "model.version.artifact.updated"
    ]
    assert events[-1]["status"] == "failed"
    assert events[-1]["availability"] == "check_failed"
    assert "/data/outputs" not in str(events[-1])
