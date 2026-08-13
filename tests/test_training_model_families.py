from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.migrations import (
    _MIGRATION_001,
    _MIGRATION_002,
    _MIGRATION_003,
)
from vla_data_juicer_agents.training.resources import FakeResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore


def _configuration(*, entrypoint: str = "train.py") -> dict[str, object]:
    return {
        "launch_template": {
            "domain": "vla",
            "server_ref": "fake-local",
            "working_directory": "/workspace/navila",
            "executable": "python",
            "entrypoint": entrypoint,
            "fixed_argv": [],
            "output_root": "/workspace/outputs",
        },
        "parameter_definitions": [
            {
                "key": "max_steps",
                "label": "Max steps",
                "type": "integer",
                "default": 2,
                "minimum": 1,
                "maximum": 10,
                "cli_flag": "--max_steps",
            }
        ],
    }


def _create_payload(
    family_name: str = "NaVILA", *, version_description: str | None = None
) -> dict[str, object]:
    return {
        "family_name": family_name,
        "version_description": version_description,
        "configuration": _configuration(),
    }


def _run_payload(model_ref: str, *, gpu_uuid: str = "fake-a100-00") -> dict[str, object]:
    return {
        "model_ref": model_ref,
        "server_ref": "fake-local",
        "gpu_uuids": [gpu_uuid],
        "parameters": {"max_steps": 2},
        "execution_mode": "simulation",
    }


@pytest.fixture
def service(tmp_path: Path) -> TrainingService:
    store = TrainingStore(tmp_path / "training.sqlite")
    return TrainingService(store, FakeResourceProvider(store))


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


def _create_model(client: TestClient, **overrides: object) -> dict[str, object]:
    payload = _create_payload()
    payload.update(overrides)
    response = client.post("/api/training/models", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["model"]


def _update_payload(model: dict[str, object], *, entrypoint: str) -> dict[str, object]:
    return {
        "version_description": model.get("version_description"),
        "configuration": _configuration(entrypoint=entrypoint),
        "expected_revision": model["edit_revision"],
    }


def test_v3_migration_preserves_models_revisions_and_runs_and_locks_used_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "training-v3.sqlite"
    timestamp = "2026-01-01T00:00:00+00:00"
    template = _configuration()["launch_template"]
    definitions = _configuration()["parameter_definitions"]
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE training_schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"""
        )
        db.executescript(_MIGRATION_001)
        db.executescript(_MIGRATION_002)
        db.executescript(_MIGRATION_003)
        db.executemany(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            [
                (1, "training_platform_m1", timestamp),
                (2, "training_nodes_m2", timestamp),
                (3, "training_node_deployment_m3", timestamp),
            ],
        )
        first_id = db.execute(
            """INSERT INTO registered_models(
            model_ref,name,description,status,current_revision,created_at,updated_at)
            VALUES('model_used','Used model','legacy','draft',2,?,?)""",
            (timestamp, timestamp),
        ).lastrowid
        second_id = db.execute(
            """INSERT INTO registered_models(
            model_ref,name,description,status,current_revision,created_at,updated_at)
            VALUES('model_unused','Unused model','','draft',1,?,?)""",
            (timestamp, timestamp),
        ).lastrowid
        revision_ids: list[int] = []
        for model_id, revision_number in ((first_id, 1), (first_id, 2), (second_id, 1)):
            revision_ids.append(
                int(
                    db.execute(
                        """INSERT INTO model_revisions(
                        revision_ref,model_id,revision_number,working_directory,entrypoint,
                        fixed_argv_json,output_template,parameter_definitions_json,
                        launch_template_json,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"mrev_{model_id}_{revision_number}",
                            model_id,
                            revision_number,
                            "/workspace/navila",
                            "train.py",
                            "[]",
                            "/workspace/outputs/{run_ref}",
                            json.dumps(definitions),
                            json.dumps(template),
                            timestamp,
                        ),
                    ).lastrowid
                )
            )
        private_spec = {
            "model_ref": "model_used",
            "model_name": "Used model",
            "model_revision": 1,
            "parameters": {},
            "sensitive_parameters": [],
        }
        db.execute(
            """INSERT INTO training_runs(
            run_ref,model_id,model_revision_id,mode,server_ref,gpu_uuids_json,
            parameters_json,run_spec_json,command_preview,status,state_revision,seed,
            total_steps,created_at,updated_at)
            VALUES('run_legacy',?,?, 'simulation','fake-local','[]','{}',?,'','succeeded',
            1,1,1,?,?)""",
            (first_id, revision_ids[0], json.dumps(private_spec), timestamp, timestamp),
        )
        db.commit()

    TrainingStore(path)
    TrainingStore(path)

    with sqlite3.connect(path) as db:
        models = db.execute(
            """SELECT m.model_ref,f.family_ref,f.name,m.version_number,
            m.based_on_model_id,m.version_description,m.configuration_locked_at
            FROM registered_models m JOIN model_families f ON f.id=m.family_id
            ORDER BY m.id"""
        ).fetchall()
        revisions = db.execute(
            "SELECT model_id,revision_number FROM model_revisions ORDER BY id"
        ).fetchall()
        run = db.execute(
            "SELECT run_ref,model_id,model_revision_id,run_spec_json FROM training_runs"
        ).fetchone()
        ledger = db.execute(
            "SELECT version,name FROM training_schema_migrations ORDER BY version"
        ).fetchall()

    assert models[0][0:5] == (
        "model_used",
        "family_model_used",
        "Used model",
        1,
        None,
    )
    assert models[0][5] == "legacy"
    assert models[0][6] is not None
    assert models[1][0:5] == (
        "model_unused",
        "family_model_unused",
        "Unused model",
        1,
        None,
    )
    assert models[1][5] is None
    assert models[1][6] is None
    assert revisions == [(first_id, 1), (first_id, 2), (second_id, 1)]
    assert run[0:3] == ("run_legacy", first_id, revision_ids[0])
    assert json.loads(run[3])["model_revision"] == 1
    assert ledger[-1] == (5, "model_worker_verification_m5")


@pytest.mark.parametrize("version_description", [None, "", "x" * 500])
def test_first_registration_atomically_creates_family_v1_and_optional_description(
    service: TrainingService,
    version_description: str | None,
) -> None:
    client = _client(service)
    response = client.post(
        "/api/training/models",
        json=_create_payload(version_description=version_description),
    )

    assert response.status_code == 201, response.text
    model = response.json()["model"]
    assert model["family_name"] == "NaVILA"
    assert model["version_number"] == 1
    assert model["based_on_model_ref"] is None
    assert model["version_description"] == (version_description or None)
    assert model["configuration_editable"] is True
    assert model["has_runs"] is False
    assert model["configuration"]["launch_template"]["entrypoint"] == "train.py"
    assert model["configuration"]["parameter_definitions"][0]["key"] == "max_steps"
    assert "revision" not in model["configuration"]
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM model_families").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM registered_models").fetchone()[0] == 1


def test_registration_rejects_version_description_over_500_characters(
    service: TrainingService,
) -> None:
    response = _client(service).post(
        "/api/training/models",
        json=_create_payload(version_description="x" * 501),
    )
    assert response.status_code == 422


def test_new_versions_copy_any_same_family_version_and_allocate_monotonically(
    service: TrainingService,
) -> None:
    client = _client(service)
    v1 = _create_model(client)
    v2_payload = {
        "based_on_model_ref": v1["model_ref"],
        "version_description": "Second version",
        "configuration": _configuration(entrypoint="train-v2.py"),
    }
    v2_response = client.post(
        f"/api/training/model-families/{v1['family_ref']}/versions",
        json=v2_payload,
    )
    assert v2_response.status_code == 201, v2_response.text
    v2 = v2_response.json()["model"]
    assert v2["version_number"] == 2
    assert v2["based_on_model_ref"] == v1["model_ref"]
    assert v2["configuration"]["launch_template"]["entrypoint"] == "train-v2.py"

    v3_response = client.post(
        f"/api/training/model-families/{v1['family_ref']}/versions",
        json={
            "based_on_model_ref": v1["model_ref"],
            "version_description": None,
            "configuration": v1["configuration"],
        },
    )
    assert v3_response.status_code == 201, v3_response.text
    v3 = v3_response.json()["model"]
    assert v3["version_number"] == 3
    assert v3["based_on_model_ref"] == v1["model_ref"]

    listed = client.get("/api/training/models").json()["models"]
    family_versions = [
        item["version_number"]
        for item in listed
        if item["family_ref"] == v1["family_ref"]
    ]
    assert family_versions == [3, 2, 1]


def test_concurrent_new_versions_receive_unique_consecutive_numbers(
    service: TrainingService,
) -> None:
    client = _client(service)
    v1 = _create_model(client)
    payload = {
        "based_on_model_ref": v1["model_ref"],
        "version_description": "Concurrent version",
        "configuration": v1["configuration"],
    }

    def create_version(_: int) -> tuple[int, int]:
        response = _client(service).post(
            f"/api/training/model-families/{v1['family_ref']}/versions",
            json=payload,
        )
        return response.status_code, response.json().get("model", {}).get("version_number", 0)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(create_version, range(4)))

    assert [status for status, _ in results] == [201, 201, 201, 201]
    assert sorted(version for _, version in results) == [2, 3, 4, 5]


def test_new_version_rejects_a_base_model_from_another_family(
    service: TrainingService,
) -> None:
    client = _client(service)
    first = _create_model(client, family_name="First")
    second = _create_model(client, family_name="Second")

    response = client.post(
        f"/api/training/model-families/{first['family_ref']}/versions",
        json={
            "based_on_model_ref": second["model_ref"],
            "version_description": None,
            "configuration": second["configuration"],
        },
    )
    assert response.status_code in {400, 409}
    assert response.json()["detail"]["code"] == "model_version_family_mismatch"


def test_preview_does_not_lock_but_persisted_run_locks_configuration(
    service: TrainingService,
) -> None:
    client = _client(service)
    model = _create_model(client)
    model_ref = str(model["model_ref"])

    preview = client.post("/api/training/runs/preview", json=_run_payload(model_ref))
    assert preview.status_code == 200, preview.text
    after_preview = client.get(f"/api/training/models/{model_ref}").json()["model"]
    assert after_preview["configuration_editable"] is True
    edited = client.put(
        f"/api/training/models/{model_ref}",
        json=_update_payload(after_preview, entrypoint="preview-safe.py"),
    )
    assert edited.status_code == 200, edited.text
    edited_model = edited.json()["model"]

    created = client.post(
        "/api/training/runs",
        json=_run_payload(model_ref),
        headers={"Idempotency-Key": "locks-model-version"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["family_ref"] == edited_model["family_ref"]
    assert run["family_name"] == "NaVILA"
    assert run["model_version_number"] == 1
    assert "model_revision" not in run

    locked = client.get(f"/api/training/models/{model_ref}").json()["model"]
    assert locked["has_runs"] is True
    assert locked["configuration_editable"] is False
    rejected = client.put(
        f"/api/training/models/{model_ref}",
        json=_update_payload(locked, entrypoint="must-create-new-version.py"),
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "model_version_configuration_locked"


def test_failed_run_submission_does_not_lock_configuration(
    service: TrainingService,
) -> None:
    client = _client(service)
    model = _create_model(client)
    model_ref = str(model["model_ref"])
    response = client.post(
        "/api/training/runs",
        json=_run_payload(model_ref, gpu_uuid="missing-gpu"),
        headers={"Idempotency-Key": "failed-before-persist"},
    )
    assert response.status_code in {400, 409}
    current = client.get(f"/api/training/models/{model_ref}").json()["model"]
    assert current["configuration_editable"] is True
    assert current["has_runs"] is False
    assert client.put(
        f"/api/training/models/{model_ref}",
        json=_update_payload(current, entrypoint="still-editable.py"),
    ).status_code == 200


def test_run_request_rejects_model_revision_and_public_projections_hide_internals(
    service: TrainingService,
) -> None:
    admin = _client(service)
    model = _create_model(admin)
    request = _run_payload(str(model["model_ref"]))
    request["model_revision"] = 1
    rejected = admin.post("/api/training/runs/preview", json=request)
    assert rejected.status_code == 422

    created = admin.post(
        "/api/training/runs",
        json=_run_payload(str(model["model_ref"])),
        headers={"Idempotency-Key": "safe-projections"},
    )
    assert created.status_code == 201, created.text
    run_ref = created.json()["run"]["run_ref"]

    readonly = _client(service, admin=False)
    projected_model = readonly.get(
        f"/api/training/models/{model['model_ref']}"
    ).json()["model"]
    projected_run = readonly.get(f"/api/training/runs/{run_ref}").json()["run"]
    for projection in (projected_model, projected_run):
        serialized = json.dumps(projection)
        assert "revision_ref" not in serialized
        assert "model_revision" not in serialized
        assert "latest_revision" not in serialized
        assert "/workspace/navila" not in serialized
        assert "/workspace/outputs" not in serialized
