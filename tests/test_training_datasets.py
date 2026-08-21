from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import _safe_event, create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training import migrations as training_migrations
from vla_data_juicer_agents.training.datasets import DatasetManifestPreparationWorker
from vla_data_juicer_agents.training.resources import FakeResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore, canonical_json, now_iso


class FakeCatalog:
    def __init__(self, root: Path, dates: tuple[str, ...] = ("20260806",)) -> None:
        self.root = root
        self.releases = [
            {
                "release_ref": f"dataset_release_{date}",
                "dataset_date": date,
                "status": "released",
                "source_clip_count": 1,
                "total_duration_ns": 10,
                "released_at": "2026-08-18T00:00:00+00:00",
            }
            for date in dates
        ]
        self.build_calls = 0
        for date in dates:
            date_root = root / date
            date_root.mkdir(parents=True)
            (date_root / "a.json").write_text(f"{date}\n", encoding="utf-8")
            (date_root / "b.bin").write_bytes(b"12")

    def list_releases(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.releases]

    def build_inventory(self, release_ref: str) -> dict[str, Any]:
        self.build_calls += 1
        release = next(item for item in self.releases if item["release_ref"] == release_ref)
        source_root = self.root / release["dataset_date"]
        files = []
        for path in sorted(source_root.iterdir()):
            content = path.read_bytes()
            files.append(
                {
                    "relative_path": path.name,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        return {
            **release,
            "source_root": str(source_root),
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(item["size_bytes"] for item in files),
            "inventory_sha256": "f" * 64,
        }


def _setup(tmp_path: Path, dates: tuple[str, ...] = ("20260806",)) -> tuple[TrainingStore, TrainingService, TestClient, FakeCatalog, str]:
    store = TrainingStore(tmp_path / "training.sqlite")
    catalog = FakeCatalog(tmp_path / "finish_data", dates)
    service = TrainingService(store, FakeResourceProvider(store), dataset_catalog=catalog)
    app = FastAPI()
    app.include_router(
        create_training_router(
            service,
            settings=TrainingSettings(simulation_enabled=True, development_admin=True),
        )
    )
    token = "worker_" + "a" * 48
    timestamp = now_iso()
    with store.transaction() as db:
        db.execute(
            """INSERT INTO training_nodes(
            node_ref,name,address,ssh_port,ssh_username,status,state_revision,
            deployment_status,heartbeat_revision,worker_instance_id,
            worker_token_sha256,capabilities_json,host_key_fingerprint,
            created_at,updated_at)
            VALUES('fake-local','Data node','127.0.0.1',22,'worker','online',1,
            'succeeded',1,'worker-1',?,?,?,?,?)""",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                canonical_json({"worker_features": ["directory_browser_v1", "dataset_transfer_v1", "dataset_replica_recovery_v1"]}),
                "SHA256:same-physical-host",
                timestamp,
                timestamp,
            ),
        )
    return store, service, TestClient(app), catalog, token


def _poll(client: TestClient, token: str) -> list[dict[str, Any]]:
    response = client.post(
        "/api/training/nodes/fake-local/commands/poll",
        headers={"Authorization": f"Bearer {token}"},
        json={"worker_instance_id": "worker-1", "wait_seconds": 0, "limit": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()["commands"]


def _result(
    client: TestClient,
    token: str,
    command: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    response = client.post(
        f"/api/training/nodes/fake-local/commands/{command['command_ref']}/result",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "worker_instance_id": "worker-1",
            "claim_token": command["claim_token"],
            **payload,
        },
    )
    assert response.status_code == 200, response.text


def test_transfer_submission_is_nonblocking_and_manifest_is_prepared_in_background(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    response = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "transfer-one"},
        json={
            "node_ref": "fake-local",
            "release_refs": ["dataset_release_20260806"],
            "target_parent_directory": "/data/free",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["transfers"][0]["status"] == "preparing"
    assert catalog.build_calls == 0
    assert _poll(client, token) == []

    preparer = DatasetManifestPreparationWorker(store, catalog)
    assert preparer.run_once() is True
    assert catalog.build_calls == 1
    transfer = client.get("/api/training/dataset-transfers").json()["transfers"][0]
    assert transfer["status"] == "queued"
    commands = _poll(client, token)
    assert commands[0]["kind"] == "transfer_dataset"
    assert commands[0]["payload"]["destination_parent"] == "/data/free"


def test_existing_v9_database_migrates_commands_and_old_cancellation_to_pause(tmp_path: Path) -> None:
    path = tmp_path / "training-v9.sqlite"
    names = (
        "training_platform_m1",
        "training_nodes_m2",
        "training_node_deployment_m3",
        "model_families_m4",
        "model_worker_verification_m5",
        "training_node_revision_split_m6",
        "training_workflows_m7",
        "training_node_deletion_history_m8",
        "training_datasets_m9",
    )
    applied_at = "2026-08-18T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE training_schema_migrations (
            version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)"""
        )
        for version, name in enumerate(names, start=1):
            db.executescript(getattr(training_migrations, f"_MIGRATION_{version:03d}"))
            db.execute(
                """INSERT INTO training_schema_migrations(version,name,applied_at)
                VALUES(?,?,?)""",
                (version, name, applied_at),
            )
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(training_node_commands)")
        }
        assert "claim_token_sha256" not in columns
        db.execute(
            """INSERT INTO training_node_commands(
            command_ref,node_id,node_ref_snapshot,kind,status,request_json,
            created_at,updated_at)
            VALUES('command_existing',NULL,'node_deleted','list_directories',
            'queued','{"path":"/data"}',?,?)""",
            (applied_at, applied_at),
        )
        db.execute(
            """INSERT INTO dataset_source_manifests(
            manifest_ref,release_ref,domain,dataset_date,status,inventory_sha256,
            file_count,total_bytes,created_at,updated_at)
            VALUES('manifest_existing','release_existing','navigation','20260416',
            'ready',?,1,5,?,?)""",
            ("f" * 64, applied_at, applied_at),
        )
        db.execute(
            """INSERT INTO dataset_transfers(
            transfer_ref,node_id,node_ref_snapshot,source_manifest_id,status,
            target_parent_directory,final_directory,bytes_transferred,files_completed,
            created_at,updated_at,started_at,finished_at)
            VALUES('transfer_existing',NULL,'node_deleted',1,'cancelled','/data',
            '/data/datapilot-managed/20260416-existing',3,0,?,?,?,?)""",
            (applied_at, applied_at, applied_at, applied_at),
        )
        db.execute(
            """INSERT INTO dataset_replicas(
            replica_ref,node_id,node_ref_snapshot,source_manifest_id,status,
            local_root,inventory_sha256,file_count,total_bytes,created_at,updated_at)
            VALUES('replica_detached',NULL,'node_deleted',1,'ready',
            '/data/datapilot-managed/20260416-existing',?,1,5,?,?)""",
            ("f" * 64, applied_at, applied_at),
        )
        db.execute(
            """INSERT INTO audit_events(actor,action,target_ref,payload_json,created_at)
            VALUES('tester','node.deployment_started','node_deleted',?,?)""",
            (
                canonical_json({"host_key_fingerprint": "SHA256:historical-host"}),
                applied_at,
            ),
        )
        db.commit()

    TrainingStore(path)
    TrainingStore(path)

    with sqlite3.connect(path) as db:
        ledger = db.execute(
            "SELECT version,name FROM training_schema_migrations ORDER BY version"
        ).fetchall()
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(training_node_commands)")
        }
        existing = db.execute(
            "SELECT command_ref,status FROM training_node_commands"
        ).fetchall()
        migrated_transfer = db.execute(
            "SELECT transfer_ref,status,bytes_transferred FROM dataset_transfers"
        ).fetchone()
        recovered_identity = db.execute(
            """SELECT node_host_key_fingerprint_snapshot
            FROM dataset_replicas WHERE replica_ref='replica_detached'"""
        ).fetchone()
    assert ledger[-4:] == [
        (11, "dataset_transfer_pause_cancel_m11"),
        (12, "real_training_execution_m12"),
        (13, "model_version_library_m13"),
        (14, "dataset_replica_node_recovery_m14"),
    ]
    assert "claim_token_sha256" in columns
    assert existing == [("command_existing", "queued")]
    assert migrated_transfer == ("transfer_existing", "paused", 3)
    assert recovered_identity == ("SHA256:historical-host",)


def test_directory_listing_and_transfer_result_create_ready_replica(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    listing = client.post(
        "/api/training/nodes/fake-local/directory-listings", json={"path": "/data"}
    )
    assert listing.status_code == 202
    command = _poll(client, token)[0]
    _result(client, token, command, {
        "status": "succeeded",
        "path": "/data",
        "parent_path": "/",
        "writable": True,
        "free_bytes": 123,
        "directories": [{"name": "free", "path": "/data/free", "writable": True}],
    })
    fetched = client.get(f"/api/training/directory-listings/{listing.json()['listing']['listing_ref']}")
    assert fetched.json()["listing"]["free_bytes"] == 123

    created = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "transfer-ready"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806"], "target_parent_directory": "/data/free"},
    ).json()["transfers"][0]
    DatasetManifestPreparationWorker(store, catalog).run_once()
    transfer_command = _poll(client, token)[0]
    _result(client, token, transfer_command, {
        "status": "succeeded",
        "transfer_ref": created["transfer_ref"],
        "progress": {"bytes_transferred": 11, "total_bytes": 11, "files_completed": 2, "total_files": 2},
        "replica": {"local_root": "/data/free/datapilot-managed/20260806-20260806", "inventory_sha256": "f" * 64, "total_bytes": 11, "file_count": 2},
    })
    replicas = client.get("/api/training/nodes/fake-local/dataset-replicas").json()["replicas"]
    assert len(replicas) == 1
    assert replicas[0]["status"] == "ready"


def test_same_physical_host_recovers_detached_replica_without_new_download(
    tmp_path: Path,
) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    response = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "recover-source"},
        json={
            "node_ref": "fake-local",
            "release_refs": ["dataset_release_20260806"],
            "target_parent_directory": "/data/free",
        },
    )
    assert response.status_code == 202
    DatasetManifestPreparationWorker(store, catalog).run_once()
    transfer_command = _poll(client, token)[0]
    _result(
        client,
        token,
        transfer_command,
        {
            "status": "succeeded",
            "transfer_ref": transfer_command["payload"]["transfer_ref"],
            "replica": {},
        },
    )
    original = store.list_dataset_replicas("fake-local")[0]
    store.delete_node("fake-local", 1, "tester")

    new_token = "worker_" + "b" * 48
    timestamp = now_iso()
    with store.transaction() as db:
        db.execute(
            """INSERT INTO training_nodes(
            node_ref,name,address,ssh_port,ssh_username,status,state_revision,
            deployment_status,heartbeat_revision,worker_instance_id,
            worker_token_sha256,capabilities_json,host_key_fingerprint,
            created_at,updated_at)
            VALUES('fake-local-new','Data node again','127.0.0.1',22,'worker',
            'online',1,'succeeded',1,'worker-2',?,?,?,?,?)""",
            (
                hashlib.sha256(new_token.encode()).hexdigest(),
                canonical_json({"worker_features": ["dataset_transfer_v1", "dataset_replica_recovery_v1"]}),
                "SHA256:same-physical-host",
                timestamp,
                timestamp,
            ),
        )
    store.record_node_heartbeat(
        "fake-local-new",
        new_token,
        {
            "worker_instance_id": "worker-2",
            "worker_version": "0.2.0",
            "protocol_version": 1,
            "health": "healthy",
            "health_message": None,
            "capabilities": {"worker_features": ["dataset_transfer_v1", "dataset_replica_recovery_v1"]},
            "resources": {},
        },
    )
    commands = store.claim_node_commands(
        "fake-local-new", new_token, "worker-2", 1
    )
    assert len(commands) == 1
    recovery = commands[0]
    assert recovery["kind"] == "transfer_dataset"
    assert recovery["payload"]["recovery_replica_ref"] == original["replica_ref"]
    assert recovery["payload"]["destination_parent"] == "/data/free"

    store.finish_node_command(
        "fake-local-new",
        new_token,
        recovery["command_ref"],
        {
            "worker_instance_id": "worker-2",
            "claim_token": recovery["claim_token"],
            "status": "succeeded",
            "transfer_ref": recovery["payload"]["transfer_ref"],
            "replica": {
                "local_root": original["local_root"],
                "inventory_sha256": original["inventory_sha256"],
                "file_count": original["file_count"],
                "total_bytes": original["total_bytes"],
            },
        },
    )

    recovered = store.list_dataset_replicas("fake-local-new")
    assert [item["replica_ref"] for item in recovered] == [original["replica_ref"]]
    assert recovered[0]["local_root"] == original["local_root"]


def test_different_host_fingerprint_never_claims_detached_replica(
    tmp_path: Path,
) -> None:
    store, _service, _client, _catalog, token = _setup(tmp_path)
    timestamp = now_iso()
    with store.transaction() as db:
        source_id = db.execute(
            """INSERT INTO dataset_source_manifests(
            manifest_ref,release_ref,domain,dataset_date,status,inventory_sha256,
            file_count,total_bytes,created_at,updated_at)
            VALUES('manifest_old','release_old','navigation','20260416','ready',?,
            1,5,?,?) RETURNING id""",
            ("f" * 64, timestamp, timestamp),
        ).fetchone()[0]
        db.execute(
            """INSERT INTO dataset_replicas(
            replica_ref,node_id,node_ref_snapshot,source_manifest_id,status,
            local_root,inventory_sha256,file_count,total_bytes,created_at,updated_at,
            node_host_key_fingerprint_snapshot)
            VALUES('replica_old',NULL,'deleted-node',?,'ready',
            '/data/datapilot-managed/20260416-old',?,1,5,?,?,?)""",
            (
                source_id,
                "f" * 64,
                timestamp,
                timestamp,
                "SHA256:another-physical-host",
            ),
        )
    store.record_node_heartbeat(
        "fake-local",
        token,
        {
            "worker_instance_id": "worker-1",
            "worker_version": "0.2.0",
            "protocol_version": 1,
            "health": "healthy",
            "health_message": None,
            "capabilities": {"worker_features": ["dataset_transfer_v1", "dataset_replica_recovery_v1"]},
            "resources": {},
        },
    )
    assert store.claim_node_commands("fake-local", token, "worker-1", 10) == []


def test_batch_transfers_are_claimed_serially_and_manifest_is_paginated(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path, ("20260806", "20260807"))
    response = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "two-dates"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806", "dataset_release_20260807"], "target_parent_directory": "/data"},
    )
    assert response.status_code == 202
    preparer = DatasetManifestPreparationWorker(store, catalog)
    assert preparer.run_once() and preparer.run_once()
    claimed = client.post(
        "/api/training/nodes/fake-local/commands/poll",
        headers={"Authorization": f"Bearer {token}"},
        json={"worker_instance_id": "worker-1", "wait_seconds": 0, "limit": 10},
    ).json()["commands"]
    assert len([item for item in claimed if item["kind"] == "transfer_dataset"]) == 1
    first = claimed[0]
    assert _poll(client, token) == []

    manifest = client.get(
        f"/api/training/nodes/fake-local/dataset-releases/{first['payload']['release_ref']}/manifest?cursor=0&limit=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert manifest.status_code == 200
    assert len(manifest.json()["files"]) == 1
    assert manifest.json()["next_cursor"] == 1
    last = client.get(
        f"/api/training/nodes/fake-local/dataset-releases/{first['payload']['release_ref']}/manifest?cursor=1&limit=1",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert len(last["files"]) == 1 and last["next_cursor"] is None
    _result(client, token, first, {"status": "succeeded", "transfer_ref": first["payload"]["transfer_ref"], "replica": {}})
    second = _poll(client, token)[0]
    assert second["payload"]["transfer_ref"] != first["payload"]["transfer_ref"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM dataset_source_files").fetchone()[0] == 4
        assert db.execute(
            "SELECT inventory_json FROM dataset_source_manifests LIMIT 1"
        ).fetchone()[0] is None


def test_cross_node_worker_cannot_download_unassigned_release(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "assigned-one"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806"], "target_parent_directory": "/data"},
    )
    DatasetManifestPreparationWorker(store, catalog).run_once()
    other_token = "worker_" + "b" * 48
    timestamp = now_iso()
    with store.transaction() as db:
        db.execute(
            """INSERT INTO training_nodes(node_ref,name,address,ssh_port,ssh_username,
            status,state_revision,deployment_status,heartbeat_revision,worker_instance_id,
            worker_token_sha256,created_at,updated_at)
            VALUES('other-node','Other','127.0.0.2',22,'worker','online',1,'succeeded',1,
            'worker-2',?,?,?)""",
            (hashlib.sha256(other_token.encode()).hexdigest(), timestamp, timestamp),
        )
    denied = client.get(
        "/api/training/nodes/other-node/dataset-releases/dataset_release_20260806/manifest",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert denied.status_code == 403
    assert "/finish_data" not in denied.text


def test_managed_run_requires_description_and_persists_one_snapshot(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "managed-data"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806"], "target_parent_directory": "/data"},
    )
    DatasetManifestPreparationWorker(store, catalog).run_once()
    command = _poll(client, token)[0]
    _result(client, token, command, {"status": "succeeded", "transfer_ref": command["payload"]["transfer_ref"], "replica": {}})
    replica_ref = client.get("/api/training/nodes/fake-local/dataset-replicas").json()["replicas"][0]["replica_ref"]

    model = client.post("/api/training/models", json={
        "family_name": "Managed NaVILA",
        "configuration": {
            "data_access_mode": "datapilot_managed",
            "launch_template": {"domain": "vla", "server_ref": "fake-local", "working_directory": "/workspace", "executable": "python", "entrypoint": "train.py", "fixed_argv": [], "output_root": "/outputs"},
            "parameter_definitions": [{"key": "max_steps", "label": "Steps", "type": "integer", "default": 2, "cli_flag": "--max_steps"}],
        },
    }).json()["model"]
    request = {
        "family_ref": model["family_ref"], "server_ref": "fake-local", "gpu_uuids": ["fake-a100-00"], "execution_mode": "simulation",
        "stages": [{"stage_input_source": "manual", "parameters": {"max_steps": 2}}],
        "dataset_selection": {"train_replica_refs": [replica_ref], "test_replica_refs": []},
    }
    preview = client.post("/api/training/runs/preview", json=request)
    assert preview.status_code == 200, preview.text
    assert "--dataset_manifest" in preview.json()["stages"][0]["command_preview"]
    assert preview.json()["dataset_manifest_preview"]["splits"]["train"][0]["replica_ref"] == replica_ref
    rejected = client.post("/api/training/runs", json=request, headers={"Idempotency-Key": "missing-description"})
    assert rejected.status_code == 400
    request["version_description"] = "加入 8 月 6 日训练数据"
    created = client.post("/api/training/runs", json=request, headers={"Idempotency-Key": "managed-run"})
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["version_description"] == "加入 8 月 6 日训练数据"
    assert run["dataset_snapshot"]["manifest"]["contract"] == "datapilot_dataset_manifest_v1"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM dataset_snapshots").fetchone()[0] == 1
        stored = db.execute("SELECT manifest_json FROM dataset_snapshots").fetchone()[0]
    assert canonical_json(run["dataset_snapshot"]["manifest"]) == canonical_json(__import__("json").loads(stored))


def test_managed_model_reserves_dataset_manifest_output_flag(tmp_path: Path) -> None:
    _store, _service, client, _catalog, _token = _setup(tmp_path)
    response = client.post(
        "/api/training/models",
        json={
            "family_name": "Invalid managed model",
            "configuration": {
                "data_access_mode": "datapilot_managed",
                "launch_template": {
                    "domain": "vla",
                    "server_ref": "fake-local",
                    "working_directory": "/workspace",
                    "executable": "python",
                    "entrypoint": "train.py",
                    "fixed_argv": [],
                    "output_root": "/outputs",
                    "output_flag": "--dataset_manifest",
                },
                "parameter_definitions": [
                    {
                        "key": "max_steps",
                        "label": "Steps",
                        "type": "integer",
                        "default": 1,
                        "cli_flag": "--max_steps",
                    }
                ],
            },
        },
    )
    assert response.status_code == 422
    assert "dataset_manifest is reserved" in response.text


def test_expired_manifest_preparation_lease_is_reclaimable(tmp_path: Path) -> None:
    store, _service, _client, catalog, _token = _setup(tmp_path)
    store.ensure_source_manifest_placeholder(catalog.releases[0])
    first = store.claim_source_manifest_preparation()
    assert first is not None
    assert store.claim_source_manifest_preparation() is None
    with store.transaction() as db:
        db.execute(
            """UPDATE dataset_source_manifests
            SET preparation_lease_expires_at='2000-01-01T00:00:00+00:00'
            WHERE manifest_ref=?""",
            (first["manifest_ref"],),
        )
    reclaimed = store.claim_source_manifest_preparation()
    assert reclaimed is not None
    assert reclaimed["manifest_ref"] == first["manifest_ref"]


def test_failed_inventory_waits_for_explicit_retry_and_hides_center_path(tmp_path: Path) -> None:
    store, _service, client, catalog, _token = _setup(tmp_path)
    client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "inventory-fails"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806"], "target_parent_directory": "/data"},
    )

    build_inventory = catalog.build_inventory

    def fail(_release_ref: str) -> dict[str, Any]:
        raise PermissionError("/center/private/finish_data/20260806")

    catalog.build_inventory = fail  # type: ignore[method-assign]
    worker = DatasetManifestPreparationWorker(store, catalog)
    assert worker.run_once() is True
    assert worker.run_once() is False
    response = client.get("/api/training/dataset-releases")
    assert response.status_code == 200
    assert "/center/private" not in response.text
    failed = client.get("/api/training/dataset-transfers").json()["transfers"][0]
    retried = client.post(
        f"/api/training/dataset-transfers/{failed['transfer_ref']}/retry",
        headers={"Idempotency-Key": "retry-inventory"},
        json={},
    )
    assert retried.status_code == 202
    assert retried.json()["transfer"]["status"] == "preparing"
    catalog.build_inventory = build_inventory  # type: ignore[method-assign]
    assert worker.run_once() is True
    assert client.get(
        f"/api/training/dataset-transfers/{failed['transfer_ref']}"
    ).json()["transfer"]["status"] == "queued"


def test_paused_transfer_is_not_reclaimed_after_worker_restart(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    created = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "cancel-restart"},
        json={
            "node_ref": "fake-local",
            "release_refs": ["dataset_release_20260806"],
            "target_parent_directory": "/data",
        },
    ).json()["transfers"][0]
    DatasetManifestPreparationWorker(store, catalog).run_once()
    transfer_command = _poll(client, token)[0]
    paused = client.post(
        f"/api/training/dataset-transfers/{created['transfer_ref']}/pause",
        json={},
    )
    assert paused.status_code == 202
    assert paused.json()["transfer"]["status"] == "pause_requested"

    with store.transaction() as db:
        db.execute(
            "UPDATE training_nodes SET worker_instance_id='worker-2' WHERE node_ref='fake-local'"
        )
    commands = store.claim_node_commands("fake-local", token, "worker-2", 10)
    assert [item["kind"] for item in commands] == ["cancel_dataset_transfer"]
    store.finish_node_command(
        "fake-local",
        token,
        commands[0]["command_ref"],
        {
            "worker_instance_id": "worker-2",
            "claim_token": commands[0]["claim_token"],
            "status": "failed",
            "error": {
                "code": "dataset_transfer_not_active",
                "message": "This dataset transfer is not active.",
            },
        },
    )
    with store.transaction() as db:
        db.execute(
            """UPDATE training_node_commands
            SET lease_expires_at='2000-01-01T00:00:00+00:00'
            WHERE command_ref=?""",
            (transfer_command["command_ref"],),
        )
    assert store.claim_node_commands("fake-local", token, "worker-2", 10) == []
    assert store.get_dataset_transfer(created["transfer_ref"])["status"] == "paused"


def test_full_cancellation_waits_for_worker_cleanup_and_allows_a_fresh_transfer(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    created = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "cancel-cleanup"},
        json={
            "node_ref": "fake-local",
            "release_refs": ["dataset_release_20260806"],
            "target_parent_directory": "/data",
        },
    ).json()["transfers"][0]
    DatasetManifestPreparationWorker(store, catalog).run_once()
    transfer_command = _poll(client, token)[0]

    requested = client.post(
        f"/api/training/dataset-transfers/{created['transfer_ref']}/cancel",
        json={},
    )
    assert requested.status_code == 202
    assert requested.json()["transfer"]["status"] == "cancel_requested"
    cleanup_command = _poll(client, token)[0]
    assert cleanup_command["kind"] == "cancel_dataset_transfer"
    assert cleanup_command["payload"]["action"] == "cancel"

    _result(
        client,
        token,
        transfer_command,
        {"status": "cancelled", "transfer_ref": created["transfer_ref"]},
    )
    _result(
        client,
        token,
        cleanup_command,
        {"status": "succeeded", "transfer_ref": created["transfer_ref"]},
    )
    assert store.get_dataset_transfer(created["transfer_ref"])["status"] == "cancelled"

    restarted = client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "cancel-cleanup-restart"},
        json={
            "node_ref": "fake-local",
            "release_refs": ["dataset_release_20260806"],
            "target_parent_directory": "/data",
        },
    )
    assert restarted.status_code == 202, restarted.text
    assert restarted.json()["transfers"][0]["status"] == "queued"


def test_completed_transfer_no_longer_authorizes_source_download(tmp_path: Path) -> None:
    store, _service, client, catalog, token = _setup(tmp_path)
    client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "complete-auth"},
        json={
            "node_ref": "fake-local",
            "release_refs": ["dataset_release_20260806"],
            "target_parent_directory": "/data",
        },
    )
    DatasetManifestPreparationWorker(store, catalog).run_once()
    command = _poll(client, token)[0]
    manifest = client.get(
        "/api/training/nodes/fake-local/dataset-releases/dataset_release_20260806/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert manifest.status_code == 200
    _result(
        client,
        token,
        command,
        {
            "status": "succeeded",
            "transfer_ref": command["payload"]["transfer_ref"],
            "replica": {},
        },
    )
    denied = client.get(
        "/api/training/nodes/fake-local/dataset-releases/dataset_release_20260806/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403


def test_expired_command_claim_rejects_late_worker_result(tmp_path: Path) -> None:
    store, _service, client, _catalog, token = _setup(tmp_path)
    listing = client.post(
        "/api/training/nodes/fake-local/directory-listings",
        json={"path": "/data"},
    )
    assert listing.status_code == 202
    first = _poll(client, token)[0]
    with store.transaction() as db:
        db.execute(
            """UPDATE training_node_commands
            SET lease_expires_at='2000-01-01T00:00:00+00:00'
            WHERE command_ref=?""",
            (first["command_ref"],),
        )
    second = _poll(client, token)[0]
    assert second["command_ref"] == first["command_ref"]
    assert second["claim_token"] != first["claim_token"]

    late = client.post(
        f"/api/training/nodes/fake-local/commands/{first['command_ref']}/result",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "worker_instance_id": "worker-1",
            "claim_token": first["claim_token"],
            "status": "succeeded",
            "path": "/late",
            "writable": True,
            "free_bytes": 1,
            "directories": [],
        },
    )
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "training_node_command_claim_stale"

    accepted = client.post(
        f"/api/training/nodes/fake-local/commands/{second['command_ref']}/result",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "worker_instance_id": "worker-1",
            "claim_token": second["claim_token"],
            "status": "succeeded",
            "path": "/data",
            "writable": True,
            "free_bytes": 2,
            "directories": [],
        },
    )
    assert accepted.status_code == 200


def test_dataset_event_projection_keeps_only_safe_cursor_fields() -> None:
    safe = _safe_event(
        {
            "event_id": 12,
            "type": "dataset.transfer.updated",
            "run_ref": None,
            "transfer_ref": "transfer_safe",
            "status": "running",
            "final_directory": "/private/data/secret",
        }
    )
    assert safe == {
        "event_id": 12,
        "type": "dataset.transfer.updated",
        "transfer_ref": "transfer_safe",
        "status": "running",
    }


def test_read_only_dataset_projection_does_not_expose_node_paths(tmp_path: Path) -> None:
    store, service, admin, catalog, token = _setup(tmp_path)
    admin.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "readonly-path"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806"], "target_parent_directory": "/secret/data"},
    )
    DatasetManifestPreparationWorker(store, catalog).run_once()
    command = _poll(admin, token)[0]
    _result(admin, token, command, {"status": "succeeded", "transfer_ref": command["payload"]["transfer_ref"], "replica": {}})
    app = FastAPI()
    app.include_router(
        create_training_router(
            service, settings=TrainingSettings(simulation_enabled=True, development_admin=False)
        )
    )
    read_only = TestClient(app)
    assert "/secret/data" not in read_only.get("/api/training/dataset-transfers").text
    assert "/secret/data" not in read_only.get("/api/training/nodes/fake-local/dataset-replicas").text
    listing = admin.post("/api/training/nodes/fake-local/directory-listings", json={"path": "/secret"}).json()["listing"]
    denied = read_only.get(f"/api/training/directory-listings/{listing['listing_ref']}")
    assert denied.status_code == 403


def test_create_run_revalidates_replica_inside_atomic_transaction(tmp_path: Path) -> None:
    store, service, client, catalog, token = _setup(tmp_path)
    client.post(
        "/api/training/dataset-transfers",
        headers={"Idempotency-Key": "atomic-data"},
        json={"node_ref": "fake-local", "release_refs": ["dataset_release_20260806"], "target_parent_directory": "/data"},
    )
    DatasetManifestPreparationWorker(store, catalog).run_once()
    command = _poll(client, token)[0]
    _result(client, token, command, {"status": "succeeded", "transfer_ref": command["payload"]["transfer_ref"], "replica": {}})
    replica_ref = client.get("/api/training/nodes/fake-local/dataset-replicas").json()["replicas"][0]["replica_ref"]
    model = client.post("/api/training/models", json={
        "family_name": "Atomic managed model",
        "configuration": {
            "data_access_mode": "datapilot_managed",
            "launch_template": {"domain": "vla", "server_ref": "fake-local", "working_directory": "/workspace", "executable": "python", "entrypoint": "train.py", "fixed_argv": [], "output_root": "/outputs"},
            "parameter_definitions": [{"key": "max_steps", "label": "Steps", "type": "integer", "default": 1, "cli_flag": "--max_steps"}],
        },
    }).json()["model"]
    original_create_run = store.create_run

    def raced_create_run(**kwargs: Any) -> dict[str, Any]:
        with store.transaction() as db:
            db.execute("UPDATE dataset_replicas SET status='removing' WHERE replica_ref=?", (replica_ref,))
        return original_create_run(**kwargs)

    store.create_run = raced_create_run  # type: ignore[method-assign]
    response = client.post("/api/training/runs", headers={"Idempotency-Key": "atomic-run"}, json={
        "family_ref": model["family_ref"], "server_ref": "fake-local", "gpu_uuids": ["fake-a100-00"], "execution_mode": "simulation", "version_description": "Atomic selection",
        "stages": [{"stage_input_source": "manual", "parameters": {"max_steps": 1}}],
        "dataset_selection": {"train_replica_refs": [replica_ref], "test_replica_refs": []},
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "dataset_replica_changed"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM model_versions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 0
