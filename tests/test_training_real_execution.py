from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training import migrations
from vla_data_juicer_agents.training.api import (
    TrainingRunUpdateRequest,
    create_training_router,
)
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.resources import TrainingResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore


def _client(service: TrainingService) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_training_router(
            service,
            settings=TrainingSettings(simulation_enabled=True, development_admin=True),
        )
    )
    return TestClient(app)


def _real_setup(tmp_path: Path) -> tuple[TrainingStore, TestClient, dict, str]:
    store = TrainingStore(tmp_path / "training.sqlite")
    service = TrainingService(
        store,
        TrainingResourceProvider(store),
        real_execution_enabled=True,
    )
    client = _client(service)
    node = store.create_node(
        {
            "name": "real-node",
            "description": "test",
            "address": "192.0.2.10",
            "ssh_port": 22,
            "ssh_username": "trainer",
        },
        "test",
    )
    enrollment = store.create_enrollment_token(
        node["node_ref"], node["state_revision"], 600, "test"
    )
    enrolled = store.enroll_node(
        {
            "enrollment_token": enrollment["enrollment_token"],
            "worker_instance_id": "worker-real-1",
            "worker_version": "0.2.0",
            "protocol_version": 1,
            "capabilities": {
                "hostname": "real-node",
                "operating_system": "Linux",
                "architecture": "x86_64",
                "worker_features": ["resource_reporting", "training_execution_v1"],
                "training_execution_v1": True,
            },
        }
    )
    worker_token = enrolled["worker_token"]
    store.record_node_heartbeat(
        node["node_ref"],
        worker_token,
        {
            "worker_instance_id": "worker-real-1",
            "worker_version": "0.2.0",
            "protocol_version": 1,
            "health": "healthy",
            "health_message": None,
            "capabilities": enrolled["node"]["capabilities"],
            "resources": {
                "cpu": {"logical_cores": 16},
                "memory": {"total_bytes": 64_000, "available_bytes": 32_000},
                "disks": [],
                "gpus": [
                    {
                        "uuid": "GPU-real-0",
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
    model_response = client.post(
        "/api/training/models",
        json={
            "family_name": "NaVILA",
            "configuration": {
                "launch_template": {
                    "domain": "vla",
                    "server_ref": node["node_ref"],
                    "working_directory": "/data/navila",
                    "executable": "python",
                    "entrypoint": "train.py",
                    "fixed_argv": [],
                    "output_root": "/data/outputs",
                },
                "parameter_definitions": [
                    {
                        "key": "max_steps",
                        "label": "steps",
                        "type": "integer",
                        "default": 2,
                        "minimum": 1,
                        "maximum": 100,
                        "cli_flag": "--max_steps",
                    },
                    {
                        "key": "private_token",
                        "label": "token",
                        "type": "string",
                        "default": "secret-for-redaction",
                        "sensitive": True,
                        "cli_flag": "--private_token",
                    },
                ],
            },
        },
    )
    assert model_response.status_code == 201, model_response.text
    model = model_response.json()["model"]
    with store.transaction() as db:
        db.execute(
            "UPDATE registered_models SET status='verified' WHERE family_id=(SELECT id FROM model_families WHERE family_ref=?)",
            (model["family_ref"],),
        )
    return store, client, {"node": node, "model": model}, worker_token


def _run_payload(state: dict) -> dict:
    return {
        "family_ref": state["model"]["family_ref"],
        "server_ref": state["node"]["node_ref"],
        "gpu_uuids": ["GPU-real-0"],
        "stages": [{"parameters": {"max_steps": 2}, "stage_input_source": "manual"}],
        "execution_mode": "real",
        "version_description": "real runner regression",
    }


def test_real_run_is_atomic_and_fake_worker_cannot_claim_it(tmp_path: Path) -> None:
    store, client, state, _ = _real_setup(tmp_path)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(state),
        headers={"Idempotency-Key": "real-create-1"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["execution_mode"] == "real"
    assert run["execution_control_status"] == "unreachable"
    assert store.claim_next_run("fake-worker") is None
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM model_versions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM port_leases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM training_execution_actions").fetchone()[0] == 1


def test_worker_action_and_ordered_updates_drive_real_run(tmp_path: Path) -> None:
    store, client, state, worker_token = _real_setup(tmp_path)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(state),
        headers={"Idempotency-Key": "real-create-2"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    stage = run["stages"][0]
    auth = {"Authorization": f"Bearer {worker_token}"}
    polled = client.post(
        f"/api/training/nodes/{state['node']['node_ref']}/training-actions/poll",
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    )
    assert polled.status_code == 200, polled.text
    action = polled.json()["actions"][0]
    assert action["kind"] == "start_training_stage"
    assert action["claim_token"].startswith("claim_")
    assert action["owner_epoch"] == 1
    assert action["payload"]["stage_ref"] == stage["stage_ref"]
    assert isinstance(action["payload"]["argv"], list)
    assert action["payload"]["family_ref"] == state["model"]["family_ref"]
    assert action["payload"]["entrypoint"] == "train.py"
    assert action["payload"]["redactions"] == ["secret-for-redaction"]
    assert "secret-for-redaction" not in str(run)
    result = client.post(
        f"/api/training/nodes/{state['node']['node_ref']}/training-actions/{action['action_ref']}/result",
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "claim_token": action["claim_token"],
            "status": "succeeded",
            "result": {},
        },
    )
    assert result.status_code == 200, result.text
    update_url = f"/api/training/nodes/{state['node']['node_ref']}/runs/{run['run_ref']}/updates"
    started = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 1,
            "updates": [{"kind": "started", "stage_ref": stage["stage_ref"]}],
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    updates = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 2,
            "updates": [
                {"kind": "log", "stage_ref": stage["stage_ref"], "level": "info", "message": "training"},
                {"kind": "metric", "stage_ref": stage["stage_ref"], "step": 1, "total_steps": 2, "loss": 0.5},
                {"kind": "checkpoint", "stage_ref": stage["stage_ref"], "relative_path": "checkpoint-1", "step": 1},
            ],
        },
    )
    assert updates.status_code == 200, updates.text
    duplicate = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 2,
            "updates": [{"kind": "heartbeat", "stage_ref": stage["stage_ref"]}],
        },
    )
    assert duplicate.status_code == 200
    unsafe_checkpoint = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 3,
            "updates": [
                {
                    "kind": "checkpoint",
                    "stage_ref": stage["stage_ref"],
                    "relative_path": "../outside",
                }
            ],
        },
    )
    assert unsafe_checkpoint.status_code == 409
    assert unsafe_checkpoint.json()["detail"]["code"] == "invalid_checkpoint_path"
    finished = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 3,
            "updates": [{"kind": "exited", "stage_ref": stage["stage_ref"], "status": "succeeded"}],
        },
    )
    assert finished.status_code == 200, finished.text
    body = finished.json()
    assert body["status"] == "succeeded"
    assert {item["kind"] for item in body["artifacts"]} == {
        "stage_output", "checkpoint", "version_model"
    }
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 0


def test_real_multistage_queues_next_stage_and_stop_is_worker_action(tmp_path: Path) -> None:
    store, client, state, worker_token = _real_setup(tmp_path)
    payload = _run_payload(state)
    payload["stages"].append(  # type: ignore[union-attr]
        {"parameters": {"max_steps": 2}, "stage_input_source": "manual"}
    )
    created = client.post(
        "/api/training/runs",
        json=payload,
        headers={"Idempotency-Key": "real-multistage"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    first, second = run["stages"]
    auth = {"Authorization": f"Bearer {worker_token}"}
    poll_url = f"/api/training/nodes/{state['node']['node_ref']}/training-actions/poll"
    first_action = client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    ).json()["actions"][0]
    update_url = f"/api/training/nodes/{state['node']['node_ref']}/runs/{run['run_ref']}/updates"
    exited = client.post(
        update_url,
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 1,
            "updates": [{"kind": "exited", "stage_ref": first["stage_ref"], "status": "succeeded"}],
        },
    )
    assert exited.status_code == 200, exited.text
    second_action = client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    ).json()["actions"][0]
    assert second_action["payload"]["stage_ref"] == second["stage_ref"]
    stopped = client.post(
        f"/api/training/runs/{run['run_ref']}/stop",
        headers={"Idempotency-Key": "stop-real-multistage"},
        json={"expected_revision": exited.json()["state_revision"]},
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["run"]["status"] == "stop_requested"
    stop_actions = client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 2},
    ).json()["actions"]
    assert any(action["kind"] == "stop_training_run" for action in stop_actions)
    assert first_action["kind"] == "start_training_stage"


def test_stop_requested_wins_when_current_stage_exits_successfully(tmp_path: Path) -> None:
    store, client, state, worker_token = _real_setup(tmp_path)
    payload = _run_payload(state)
    payload["stages"].append(  # type: ignore[union-attr]
        {"parameters": {"max_steps": 2}, "stage_input_source": "manual"}
    )
    created = client.post(
        "/api/training/runs",
        json=payload,
        headers={"Idempotency-Key": "stop-exit-race"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    first_stage = run["stages"][0]
    auth = {"Authorization": f"Bearer {worker_token}"}
    poll_url = f"/api/training/nodes/{state['node']['node_ref']}/training-actions/poll"
    first_action = client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    ).json()["actions"][0]
    assert first_action["kind"] == "start_training_stage"
    current = client.get(f"/api/training/runs/{run['run_ref']}").json()["run"]
    stopped = client.post(
        f"/api/training/runs/{run['run_ref']}/stop",
        headers={"Idempotency-Key": "stop-exit-race-request"},
        json={"expected_revision": current["state_revision"]},
    )
    assert stopped.status_code == 200, stopped.text
    exited = client.post(
        f"/api/training/nodes/{state['node']['node_ref']}/runs/{run['run_ref']}/updates",
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "owner_epoch": 1,
            "worker_seq": 1,
            "updates": [
                {
                    "kind": "exited",
                    "stage_ref": first_stage["stage_ref"],
                    "status": "succeeded",
                }
            ],
        },
    )
    assert exited.status_code == 200, exited.text
    body = exited.json()
    assert body["status"] == "cancelled"
    assert [stage["status"] for stage in body["stages"]] == ["cancelled", "cancelled"]
    assert not body["artifacts"]
    assert client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 10},
    ).json() == {"actions": []}
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM port_leases").fetchone()[0] == 0


def test_expired_start_is_not_reclaimed_after_stop_and_unresolved_is_visible(
    tmp_path: Path,
) -> None:
    store, client, state, worker_token = _real_setup(tmp_path)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(state),
        headers={"Idempotency-Key": "stop-expired-start"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    auth = {"Authorization": f"Bearer {worker_token}"}
    poll_url = f"/api/training/nodes/{state['node']['node_ref']}/training-actions/poll"
    start_action = client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    ).json()["actions"][0]
    current = client.get(f"/api/training/runs/{run['run_ref']}").json()["run"]
    stopped = client.post(
        f"/api/training/runs/{run['run_ref']}/stop",
        headers={"Idempotency-Key": "stop-expired-start-request"},
        json={"expected_revision": current["state_revision"]},
    )
    assert stopped.status_code == 200, stopped.text
    with store.transaction() as db:
        db.execute(
            """UPDATE training_execution_actions SET lease_expires_at=?
            WHERE action_ref=?""",
            ("2000-01-01T00:00:00Z", start_action["action_ref"]),
        )
    stop_action = client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    ).json()["actions"][0]
    assert stop_action["kind"] == "stop_training_run"
    failed = client.post(
        f"/api/training/nodes/{state['node']['node_ref']}/training-actions/{stop_action['action_ref']}/result",
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "claim_token": stop_action["claim_token"],
            "status": "failed",
            "error": {
                "code": "training_stop_unresolved",
                "message": "The process identity could not be confirmed.",
            },
        },
    )
    assert failed.status_code == 200, failed.text
    unresolved = client.get(f"/api/training/runs/{run['run_ref']}").json()["run"]
    assert unresolved["status"] == "stop_requested"
    assert unresolved["execution_control_status"] == "unresolved"
    assert client.post(
        poll_url,
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 10},
    ).json() == {"actions": []}
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 1


@pytest.mark.parametrize("request_stop", [False, True])
def test_unresolved_start_keeps_run_and_resource_leases(
    tmp_path: Path, request_stop: bool,
) -> None:
    store, client, state, worker_token = _real_setup(tmp_path)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(state),
        headers={"Idempotency-Key": "unresolved-start"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    auth = {"Authorization": f"Bearer {worker_token}"}
    action = client.post(
        f"/api/training/nodes/{state['node']['node_ref']}/training-actions/poll",
        headers=auth,
        json={"worker_instance_id": "worker-real-1", "wait_seconds": 0, "limit": 1},
    ).json()["actions"][0]
    if request_stop:
        current = client.get(f"/api/training/runs/{run['run_ref']}").json()["run"]
        stopped = client.post(
            f"/api/training/runs/{run['run_ref']}/stop",
            headers={"Idempotency-Key": "unresolved-start-stop"},
            json={"expected_revision": current["state_revision"]},
        )
        assert stopped.status_code == 200, stopped.text
    result = client.post(
        f"/api/training/nodes/{state['node']['node_ref']}/training-actions/{action['action_ref']}/result",
        headers=auth,
        json={
            "worker_instance_id": "worker-real-1",
            "claim_token": action["claim_token"],
            "status": "failed",
            "error": {
                "code": "training_launch_unresolved",
                "message": "The launch outcome could not be confirmed.",
            },
        },
    )
    assert result.status_code == 200, result.text
    unresolved = client.get(f"/api/training/runs/{run['run_ref']}").json()["run"]
    assert unresolved["status"] == ("stop_requested" if request_stop else "preparing")
    assert unresolved["execution_control_status"] == "unresolved"
    assert unresolved["stages"][0]["status"] == "preparing"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 1


def test_central_run_log_limit_appends_one_truncation_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    store, client, state, _ = _real_setup(tmp_path)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(state),
        headers={"Idempotency-Key": "real-log-limit"},
    )
    assert created.status_code == 201, created.text
    run_ref = created.json()["run"]["run_ref"]
    monkeypatch.setattr(TrainingStore, "_RUN_LOG_MAX_LINES", 2)
    with store.transaction() as db:
        run_id = int(
            db.execute(
                "SELECT id FROM training_runs WHERE run_ref=?", (run_ref,)
            ).fetchone()[0]
        )
        assert store._log(db, run_id, "info", "last stored line", "2026-08-20T00:00:00Z") == 2
        assert store._log(db, run_id, "info", "overflow", "2026-08-20T00:00:01Z") == 3
        assert store._log(db, run_id, "info", "ignored", "2026-08-20T00:00:02Z") is None
    logs = store.list_logs(run_ref, after_seq=0, limit=20)
    assert [item["message"] for item in logs["items"]][-2:] == [
        "last stored line",
        TrainingStore._RUN_LOG_TRUNCATED_MESSAGE,
    ]


def test_v11_database_upgrades_through_v13_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "training-v11.sqlite"
    names = [
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
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE training_schema_migrations(
            version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)"""
        )
        for version, name in enumerate(names, start=1):
            db.executescript(getattr(migrations, f"_MIGRATION_{version:03d}"))
            db.execute(
                """INSERT INTO training_schema_migrations(version,name,applied_at)
                VALUES(?,?,?)""",
                (version, name, "2026-08-20T00:00:00Z"),
            )
            db.commit()
    TrainingStore(path)
    TrainingStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT MAX(version) FROM training_schema_migrations"
        ).fetchone()[0] == 14
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(training_runs)")
        }
        assert {"execution_mode", "execution_owner_epoch", "execution_update_seq"} <= columns
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='training_execution_actions'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='training_run_log_storage'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='training_artifact_inspections'"
        ).fetchone() is not None


def test_training_update_wire_limits() -> None:
    base = {
        "worker_instance_id": "worker-real-1",
        "owner_epoch": 1,
        "worker_seq": 1,
    }
    TrainingRunUpdateRequest.model_validate(
        {**base, "updates": [{"kind": "log", "lines": ["x"] * 200}]}
    )
    TrainingRunUpdateRequest.model_validate(
        {**base, "updates": [{"kind": "log", "message": "x" * 16384}]}
    )
    TrainingRunUpdateRequest.model_validate(
        {
            **base,
            "updates": [
                {
                    "kind": "metric",
                    "gpus": [
                        {
                            "uuid": "GPU-real-0",
                            "index": 0,
                            "utilization_percent": 50.0,
                            "gpu_memory_mib": 1024.0,
                            "temperature_celsius": 42.0,
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {**base, "updates": [{"kind": "log", "lines": ["x"] * 201}]}
        )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {**base, "updates": [{"kind": "log", "message": "x" * 16385}]}
        )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {**base, "updates": [{"kind": "log", "lines": ["x" * 1311] * 200}]}
        )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {**base, "updates": [{"kind": "metric", "loss": float("nan")}]}
        )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {
                **base,
                "updates": [
                    {
                        "kind": "metric",
                        "gpus": [
                            {
                                "uuid": "GPU-real-0",
                                "index": 0,
                                "utilization_percent": float("nan"),
                            }
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {
                **base,
                "updates": [
                    {
                        "kind": "metric",
                        "gpus": [
                            {
                                "uuid": "GPU-real-0",
                                "index": 0,
                                "metadata": {"unexpected": True},
                            }
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValueError):
        TrainingRunUpdateRequest.model_validate(
            {**base, "updates": [{"kind": "checkpoint", "relative_path": "x" * 1025}]}
        )


def test_training_action_poll_honors_wait_seconds(tmp_path: Path) -> None:
    store, _, state, worker_token = _real_setup(tmp_path)
    elapsed = [0.0]
    service = TrainingService(
        store,
        TrainingResourceProvider(store),
        real_execution_enabled=True,
        monotonic_clock=lambda: elapsed[0],
        sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    result = service.poll_training_actions(
        state["node"]["node_ref"],
        worker_token,
        {"worker_instance_id": "worker-real-1", "wait_seconds": 1, "limit": 1},
    )
    assert result == {"actions": []}
    assert elapsed[0] == pytest.approx(1.0)
