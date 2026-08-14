from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.resources import FakeResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore


def _configuration(*, entrypoint: str = "train.py") -> dict[str, object]:
    return {
        "launch_template": {
            "domain": "vla",
            "server_ref": "fake-local",
            "working_directory": "/workspace/navila",
            "launcher_kind": "direct",
            "executable": "python",
            "entrypoint": entrypoint,
            "fixed_argv": [],
            "output_root": "/workspace/outputs",
            "output_flag": "--output_dir",
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
            },
            {
                "key": "hub_token",
                "label": "Hub token",
                "type": "string",
                "default": "local-secret",
                "sensitive": True,
                "cli_flag": "--hub_token",
            },
        ],
    }


def _service(tmp_path: Path) -> TrainingService:
    store = TrainingStore(tmp_path / "training.sqlite")
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


def test_model_family_uses_optimistic_editing_and_resets_verification(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    client = _client(service, admin=True)
    created = client.post(
        "/api/training/models",
        json={"family_name": "NaVILA", "configuration": _configuration()},
    )
    assert created.status_code == 201, created.text
    family = created.json()["model"]

    updated = client.put(
        f"/api/training/models/{family['family_ref']}",
        json={
            "expected_revision": family["edit_revision"],
            "configuration": _configuration(entrypoint="train-v2.py"),
        },
    )
    assert updated.status_code == 200, updated.text
    edited = updated.json()["model"]
    assert edited["edit_revision"] == 2
    assert edited["status"] == "draft"
    assert edited["configuration"]["launch_template"]["entrypoint"] == "train-v2.py"

    stale = client.put(
        f"/api/training/models/{family['family_ref']}",
        json={
            "expected_revision": family["edit_revision"],
            "configuration": _configuration(entrypoint="stale.py"),
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "model_configuration_edit_conflict"


def test_read_only_family_projection_hides_paths_and_sensitive_defaults(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    admin = _client(service, admin=True)
    created = admin.post(
        "/api/training/models",
        json={"family_name": "NaVILA", "configuration": _configuration()},
    )
    family_ref = created.json()["model"]["family_ref"]

    readonly = _client(service, admin=False)
    model = readonly.get(f"/api/training/models/{family_ref}").json()["model"]
    serialized = json.dumps(model)
    assert "/workspace/navila" not in serialized
    assert "/workspace/outputs" not in serialized
    assert "local-secret" not in serialized
    token = next(
        definition
        for definition in model["configuration"]["parameter_definitions"]
        if definition["key"] == "hub_token"
    )
    assert token["default"] == "********"
    assert "revision_ref" not in serialized
    assert "edit_revision" not in model


def test_model_family_write_remains_forbidden_without_admin_permission(
    tmp_path: Path,
) -> None:
    response = _client(_service(tmp_path), admin=False).post(
        "/api/training/models",
        json={"family_name": "NaVILA", "configuration": _configuration()},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "training_write_forbidden"
