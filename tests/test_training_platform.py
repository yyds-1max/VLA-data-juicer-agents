from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.migrations import LATEST_TRAINING_SCHEMA_VERSION
from vla_data_juicer_agents.training.resources import (
    FakeResourceProvider,
    TrainingResourceProvider,
)
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore
from vla_data_juicer_agents.training.worker import TrainingWorker


def _model_payload(*, name: str = "NaVILA draft") -> dict[str, object]:
    return {
        "family_name": name,
        "version_description": "Simulation-only launch template.",
        "configuration": {
            "launch_template": {
                "domain": "vla",
                "server_ref": "fake-local",
                "working_directory": "/workspace/navila",
                "executable": "python",
                "entrypoint": "train.py",
                "fixed_argv": ["--deepspeed", "configs/zero3.json"],
                "output_root": "/workspace/outputs",
            },
            "parameter_definitions": [
                {
                    "key": "num_video_frames",
                    "label": "Video frames",
                    "type": "integer",
                    "default": 4,
                    "minimum": 1,
                    "maximum": 8,
                    "cli_flag": "--num_video_frames",
                },
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
                    "default": "local-token",
                    "sensitive": True,
                    "cli_flag": "--hub_token",
                },
                {
                    "key": "simulate_failure",
                    "label": "Simulate failure",
                    "type": "boolean",
                    "default": False,
                    "cli_flag": "--simulate_failure",
                },
            ],
        },
    }


def _run_payload(
    model_ref: str,
    *,
    gpu_uuids: list[str] | None = None,
    simulate_failure: bool = False,
) -> dict[str, object]:
    return {
        "model_ref": model_ref,
        "server_ref": "fake-local",
        "gpu_uuids": gpu_uuids or ["fake-a100-00", "fake-a100-01"],
        "parameters": {
            "num_video_frames": 8,
            "max_steps": 2,
            "hub_token": "private",
            "simulate_failure": simulate_failure,
        },
        "execution_mode": "simulation",
    }


def _conditional_model_payload() -> dict[str, object]:
    payload = _model_payload(name="NaVILA conditional draft")
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    definitions.extend(
        [
            {
                "key": "use_lora",
                "label": "Use LoRA",
                "type": "boolean",
                "default": False,
                "argument_style": "explicit_boolean",
                "cli_flag": "--use_lora",
            },
            {
                "key": "lora_rank",
                "label": "LoRA rank",
                "type": "integer",
                "default": 8,
                "minimum": 1,
                "maximum": 128,
                "cli_flag": "--lora_rank",
                "visible_when": {"parameter_key": "use_lora", "equals": True},
            },
        ]
    )
    return payload


def _typed_model_payload() -> dict[str, object]:
    payload = _model_payload(name="NaVILA typed parameter draft")
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    definitions.extend(
        [
            {
                "key": "learning_rate",
                "label": "Learning rate",
                "type": "number",
                "default": 0.0001,
                "minimum": 0,
                "maximum": 1,
                "cli_flag": "--learning_rate",
            },
            {
                "key": "use_cache",
                "label": "Use cache",
                "type": "boolean",
                "default": True,
                "argument_style": "explicit_boolean",
                "cli_flag": "--use_cache",
            },
            {
                "key": "run_name",
                "label": "Run name",
                "type": "string",
                "default": "baseline",
                "cli_flag": "--run_name",
            },
            {
                "key": "optimizer",
                "label": "Optimizer",
                "type": "enum",
                "default": "adamw",
                "choices": [
                    {"value": "adamw", "label": "AdamW (recommended)"},
                    {"value": "adafactor", "label": "Adafactor (low memory)"},
                ],
                "cli_flag": "--optimizer",
            },
        ]
    )
    return payload


def test_dataset_parameter_role_roundtrips_and_is_limited_to_one(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload(name="NaVILA dataset input")
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    definitions.append(
        {
            "key": "data_mixture",
            "label": "Dataset mixture",
            "type": "string",
            "semantic_role": "dataset",
            "default": "rxr",
            "cli_flag": "--data_mixture",
        }
    )

    response = client.post("/api/training/models", json=payload)

    assert response.status_code == 201
    registered = response.json()["model"]["configuration"]["parameter_definitions"]
    dataset = next(item for item in registered if item["key"] == "data_mixture")
    assert dataset["semantic_role"] == "dataset"

    definitions.append(
        {
            "key": "validation_data",
            "label": "Validation dataset",
            "type": "string",
            "semantic_role": "dataset",
            "default": "validation",
            "cli_flag": "--validation_data",
        }
    )
    rejected = client.post("/api/training/models", json=payload)
    assert rejected.status_code == 422


def test_runtime_and_monitoring_contracts_roundtrip_and_reach_run_spec(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload(name="NaVILA conda runtime")
    launch_template = payload["configuration"]["launch_template"]  # type: ignore[index]
    assert isinstance(launch_template, dict)
    launch_template["runtime_environment"] = {
        "kind": "conda",
        "conda_environment": "navila-train",
    }
    launch_template["monitoring"] = {
        "source": "stdout",
        "format": "transformers",
    }

    created = client.post("/api/training/models", json=payload)

    assert created.status_code == 201, created.text
    model = created.json()["model"]
    registered_template = model["configuration"]["launch_template"]
    assert registered_template["runtime_environment"] == {
        "kind": "conda",
        "conda_environment": "navila-train",
    }
    assert registered_template["monitoring"] == {
        "source": "stdout",
        "format": "transformers",
    }

    preview = client.post(
        "/api/training/runs/preview",
        json=_run_payload(str(model["model_ref"])),
    )
    assert preview.status_code == 200, preview.text
    spec = preview.json()["run_spec"]
    assert spec["runtime_environment"] == registered_template["runtime_environment"]
    assert spec["monitoring"] == registered_template["monitoring"]


def test_conda_runtime_requires_a_safe_environment_name(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    launch_template = payload["configuration"]["launch_template"]  # type: ignore[index]
    assert isinstance(launch_template, dict)
    launch_template["runtime_environment"] = {"kind": "conda"}

    missing = client.post("/api/training/models", json=payload)
    assert missing.status_code == 422

    launch_template["runtime_environment"] = {
        "kind": "conda",
        "conda_environment": "navila; touch /tmp/unsafe",
    }
    unsafe = client.post("/api/training/models", json=payload)
    assert unsafe.status_code == 422


@pytest.fixture
def service(tmp_path: Path) -> TrainingService:
    store = TrainingStore(tmp_path / "training.sqlite")
    return TrainingService(store, FakeResourceProvider(store))


def _client(service: TrainingService, *, admin: bool) -> TestClient:
    app = FastAPI()
    settings = TrainingSettings(simulation_enabled=True, development_admin=admin)
    app.include_router(create_training_router(service, settings=settings))
    return TestClient(app)


def test_training_migration_initializes_once_and_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "training.sqlite"
    TrainingStore(path)
    TrainingStore(path)

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT version, name FROM training_schema_migrations ORDER BY version"
        ).fetchall()
    assert rows == [
        (1, "training_platform_m1"),
        (2, "training_nodes_m2"),
        (3, "training_node_deployment_m3"),
        (LATEST_TRAINING_SCHEMA_VERSION, "model_families_m4"),
    ]


def _create_model(client: TestClient) -> dict[str, object]:
    response = client.post("/api/training/models", json=_model_payload())
    assert response.status_code == 201, response.text
    return response.json()["model"]


def test_default_principal_can_read_but_cannot_mutate(service: TrainingService) -> None:
    client = _client(service, admin=False)

    capabilities = client.get("/api/training/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["permissions"] == ["training:view"]
    assert client.get("/api/training/models").json() == {"models": []}

    response = client.post("/api/training/models", json=_model_payload())
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "training_write_forbidden"


def test_read_only_projection_hides_launch_paths_and_rejects_stop(
    service: TrainingService,
) -> None:
    admin = _client(service, admin=True)
    model_ref = str(_create_model(admin)["model_ref"])
    created = admin.post(
        "/api/training/runs",
        json=_run_payload(model_ref, gpu_uuids=["fake-a100-00"]),
        headers={"Idempotency-Key": "private-projection"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]

    readonly = _client(service, admin=False)
    model_body = readonly.get(f"/api/training/models/{model_ref}").json()["model"]
    serialized_model = str(model_body)
    assert "/workspace/navila" not in serialized_model
    assert "/workspace/outputs" not in serialized_model
    assert "configs/zero3.json" not in serialized_model
    hub_token = next(
        definition
        for definition in model_body["configuration"]["parameter_definitions"]
        if definition["key"] == "hub_token"
    )
    assert hub_token["default"] == "********"
    assert "local-token" not in serialized_model

    stop = readonly.post(
        f"/api/training/runs/{run['run_ref']}/stop",
        json={"expected_revision": run["state_revision"]},
        headers={"Idempotency-Key": "read-only-stop"},
    )
    assert stop.status_code == 403
    assert stop.json()["detail"]["code"] == "training_write_forbidden"


def test_frame_count_comes_from_parameter_not_entrypoint_name(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["launch_template"]["entrypoint"] = "train_4frames.py"  # type: ignore[index]
    created = client.post("/api/training/models", json=payload)
    assert created.status_code == 201, created.text

    preview = client.post(
        "/api/training/runs/preview",
        json=_run_payload(str(created.json()["model"]["model_ref"])),
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["run_spec"]["parameters"]["num_video_frames"] == 8
    assert "--num_video_frames 8" in body["command_preview"]


def test_argument_styles_and_platform_launch_arguments(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["launch_template"]["executable"] = "torchrun"  # type: ignore[index]
    payload["configuration"]["launch_template"]["output_flag"] = "--save_to"  # type: ignore[index]
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index,assignment]
    definitions.extend(  # type: ignore[union-attr]
        [
            {
                "key": "do_eval",
                "label": "Evaluate",
                "type": "boolean",
                "default": False,
                "argument_style": "explicit_boolean",
                "cli_flag": "--do_eval",
            },
            {
                "key": "gradient_checkpointing",
                "label": "Gradient checkpointing",
                "type": "boolean",
                "default": True,
                "argument_style": "flag_when_true",
                "cli_flag": "--gradient_checkpointing",
            },
        ]
    )

    created = client.post("/api/training/models", json=payload)
    assert created.status_code == 201, created.text
    model = created.json()["model"]
    projected = {
        definition["key"]: definition["argument_style"]
        for definition in model["configuration"]["parameter_definitions"]
    }
    assert projected["num_video_frames"] == "value"
    assert projected["do_eval"] == "explicit_boolean"
    assert projected["gradient_checkpointing"] == "flag_when_true"
    assert model["configuration"]["launch_template"]["output_flag"] == "--save_to"

    request = _run_payload(str(model["model_ref"]))
    request["parameters"].update(  # type: ignore[union-attr]
        {"do_eval": False, "gradient_checkpointing": True}
    )
    preview = client.post("/api/training/runs/preview", json=request)
    assert preview.status_code == 200, preview.text
    spec = preview.json()["run_spec"]
    argv = spec["argv"]
    assert spec["launcher_kind"] == "torchrun"
    assert spec["nproc_per_node"] == 2
    assert isinstance(spec["master_port"], int)
    assert spec["node_rank"] == 0
    assert argv.index("--node_rank=0") < argv.index("train.py")
    assert argv[argv.index("--do_eval") + 1] == "False"
    assert "--gradient_checkpointing" in argv
    assert "--simulate_failure" not in argv  # legacy omitted style stays presence-only
    assert argv[argv.index("--save_to") + 1] == "/workspace/outputs/preview"
    assert spec["output_preview"] == "/workspace/outputs/preview"
    assert spec["environment"] == {"CUDA_VISIBLE_DEVICES": "0,1"}


@pytest.mark.parametrize(
    ("parameter_type", "argument_style", "expected_message"),
    [
        ("integer", "flag_when_true", "only valid for boolean"),
        ("boolean", "value", "require explicit_boolean or flag_when_true"),
    ],
)
def test_registration_rejects_invalid_argument_style_combinations(
    service: TrainingService,
    parameter_type: str,
    argument_style: str,
    expected_message: str,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    definition = payload["configuration"]["parameter_definitions"][0]  # type: ignore[index]
    definition["type"] = parameter_type
    definition["default"] = False if parameter_type == "boolean" else 4
    definition["argument_style"] = argument_style

    response = client.post("/api/training/models", json=payload)
    assert response.status_code == 422
    assert expected_message in response.text


def test_registration_rejects_parameter_using_platform_output_flag(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["parameter_definitions"][0]["cli_flag"] = "--output_dir"  # type: ignore[index]

    response = client.post("/api/training/models", json=payload)
    assert response.status_code == 422
    assert "output_flag is platform-managed" in response.text


def test_registration_rejects_fixed_argv_redeclaring_parameter_flag(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["launch_template"]["fixed_argv"].append("--num_video_frames=8")  # type: ignore[index,union-attr]

    response = client.post("/api/training/models", json=payload)

    assert response.status_code == 422
    assert "fixed_argv cannot redeclare registered parameter flag --num_video_frames" in response.text


def test_registration_rejects_overlong_parameter_explanation(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["parameter_definitions"][0]["description"] = "x" * 121  # type: ignore[index]

    response = client.post("/api/training/models", json=payload)

    assert response.status_code == 422
    assert "120" in response.text


def test_enum_choice_value_and_label_roundtrip_through_read_and_configuration(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _typed_model_payload()
    expected_choices = [
        {"value": "adamw", "label": "AdamW (recommended)"},
        {"value": "adafactor", "label": "Adafactor (low memory)"},
    ]

    created = client.post("/api/training/models", json=payload)
    assert created.status_code == 201, created.text
    model = created.json()["model"]
    model_ref = model["model_ref"]

    def choices_from(body: dict[str, object]) -> list[dict[str, str]]:
        configuration = body["model"]["configuration"]  # type: ignore[index]
        definition = next(
            item for item in configuration["parameter_definitions"]  # type: ignore[index]
            if item["key"] == "optimizer"
        )
        return definition["choices"]

    assert choices_from(created.json()) == expected_choices
    assert choices_from(client.get(f"/api/training/models/{model_ref}").json()) == expected_choices

    update_source = _typed_model_payload()
    update_payload = {
        "version_description": "Configuration keeps presentation labels.",
        "configuration": update_source["configuration"],
        "expected_revision": model["edit_revision"],
    }
    updated = client.put(f"/api/training/models/{model_ref}", json=update_payload)
    assert updated.status_code == 200, updated.text
    assert choices_from(updated.json()) == expected_choices
    assert choices_from(client.get(f"/api/training/models/{model_ref}").json()) == expected_choices


@pytest.mark.parametrize(
    ("key", "invalid_default", "expected_message"),
    [
        ("num_video_frames", 1.5, "default must match the declared parameter type"),
        ("learning_rate", "0.0001", "default must match the declared parameter type"),
        ("use_cache", 1, "default must match the declared parameter type"),
        ("run_name", 42, "default must match the declared parameter type"),
        ("optimizer", "sgd", "enum default must be one of the choices"),
    ],
)
def test_registration_strictly_validates_parameter_type_defaults(
    service: TrainingService,
    key: str,
    invalid_default: object,
    expected_message: str,
) -> None:
    client = _client(service, admin=True)
    payload = _typed_model_payload()
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    definition = next(item for item in definitions if item["key"] == key)
    definition["default"] = invalid_default

    response = client.post("/api/training/models", json=payload)
    assert response.status_code == 422
    assert expected_message in response.text


@pytest.mark.parametrize(
    ("key", "changes", "expected_message"),
    [
        ("num_video_frames", {"minimum": 5}, "numeric default cannot be below minimum"),
        ("num_video_frames", {"maximum": 4.5}, "integer parameter bounds must be integers"),
        ("num_video_frames", {"default": 9_007_199_254_740_992}, "safe integer range"),
        ("run_name", {"default": "line one\nline two"}, "safe single-line value"),
        ("run_name", {"string_min_length": 9, "string_max_length": 32}, "shorter than string_min_length"),
        ("run_name", {"minimum": 1}, "only supported for numeric parameters"),
    ],
)
def test_registration_validates_type_specific_parameter_constraints(
    service: TrainingService,
    key: str,
    changes: dict[str, object],
    expected_message: str,
) -> None:
    client = _client(service, admin=True)
    payload = _typed_model_payload()
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    definition = next(item for item in definitions if item["key"] == key)
    definition.update(changes)

    response = client.post("/api/training/models", json=payload)

    assert response.status_code == 422
    assert expected_message in response.text


@pytest.mark.parametrize(
    ("key", "invalid_value", "expected_message"),
    [
        ("num_video_frames", 4.5, "must be an integer"),
        ("learning_rate", "0.001", "must be a number"),
        ("use_cache", 1, "must be a boolean"),
        ("run_name", 42, "must be a safe string"),
        ("optimizer", "sgd", "is not an allowed choice"),
    ],
)
def test_preview_strictly_validates_all_parameter_value_types(
    service: TrainingService,
    key: str,
    invalid_value: object,
    expected_message: str,
) -> None:
    client = _client(service, admin=True)
    created = client.post("/api/training/models", json=_typed_model_payload())
    assert created.status_code == 201, created.text
    request = _run_payload(created.json()["model"]["model_ref"])
    request["parameters"].update(  # type: ignore[union-attr]
        {
            "learning_rate": 0.001,
            "use_cache": True,
            "run_name": "strict-check",
            "optimizer": "adamw",
            key: invalid_value,
        }
    )

    response = client.post("/api/training/runs/preview", json=request)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert expected_message in response.json()["detail"]["message"]


def test_string_length_constraints_roundtrip_and_apply_to_run_values(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _typed_model_payload()
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    run_name = next(item for item in definitions if item["key"] == "run_name")
    run_name.update({"string_min_length": 3, "string_max_length": 12})

    created = client.post("/api/training/models", json=payload)
    assert created.status_code == 201, created.text
    model = created.json()["model"]
    projected = next(
        item
        for item in model["configuration"]["parameter_definitions"]
        if item["key"] == "run_name"
    )
    assert projected["string_min_length"] == 3
    assert projected["string_max_length"] == 12

    request = _run_payload(model["model_ref"])
    request["parameters"].update(  # type: ignore[union-attr]
        {
            "learning_rate": 0.001,
            "use_cache": True,
            "run_name": "name-that-is-too-long",
            "optimizer": "adamw",
        }
    )
    response = client.post("/api/training/runs/preview", json=request)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert "maximum length" in response.json()["detail"]["message"]


def test_parameter_display_group_roundtrips_with_model_configuration(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["parameter_definitions"][0].update(  # type: ignore[index,union-attr]
        {
            "display_group": "common",
            "display_group_label": "常用参数",
            "display_group_order": 0,
        }
    )

    response = client.post("/api/training/models", json=payload)

    assert response.status_code == 201, response.text
    definition = response.json()["model"]["configuration"]["parameter_definitions"][0]
    assert definition["display_group"] == "common"
    assert definition["display_group_label"] == "常用参数"
    assert definition["display_group_order"] == 0


def test_parameter_display_group_rejects_partial_metadata(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["parameter_definitions"][0]["display_group"] = "common"  # type: ignore[index]

    response = client.post("/api/training/models", json=payload)

    assert response.status_code == 422
    assert "requires a label and order" in response.text


def test_visible_when_roundtrips_and_omits_stale_values_until_enabled(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    created = client.post("/api/training/models", json=_conditional_model_payload())
    assert created.status_code == 201, created.text
    model = created.json()["model"]
    condition = next(
        definition["visible_when"]
        for definition in model["configuration"]["parameter_definitions"]
        if definition["key"] == "lora_rank"
    )
    assert condition == {"parameter_key": "use_lora", "equals": True}

    stale_request = _run_payload(model["model_ref"])
    stale_request["parameters"].update(  # type: ignore[union-attr]
        {"use_lora": False, "lora_rank": 64}
    )
    hidden = client.post("/api/training/runs/preview", json=stale_request)
    assert hidden.status_code == 200, hidden.text
    hidden_body = hidden.json()
    assert "lora_rank" not in hidden_body["run_spec"]["parameters"]
    assert "--lora_rank" not in hidden_body["run_spec"]["argv"]
    assert "--lora_rank" not in hidden_body["command_preview"]

    enabled_request = _run_payload(model["model_ref"])
    enabled_request["parameters"].update(  # type: ignore[union-attr]
        {"use_lora": True, "lora_rank": 32}
    )
    enabled = client.post("/api/training/runs/preview", json=enabled_request)
    assert enabled.status_code == 200, enabled.text
    enabled_body = enabled.json()
    assert enabled_body["run_spec"]["parameters"]["lora_rank"] == 32
    argv = enabled_body["run_spec"]["argv"]
    assert argv[argv.index("--lora_rank") + 1] == "32"
    assert "--lora_rank 32" in enabled_body["command_preview"]


@pytest.mark.parametrize(
    ("target_key", "invalid_condition", "expected_message"),
    [
        ("lora_rank", {"parameter_key": "missing_controller", "equals": True}, "unknown parameter"),
        ("num_video_frames", {"parameter_key": "num_video_frames", "equals": 4}, "cannot reference itself"),
        ("lora_rank", {"parameter_key": "use_lora", "equals": "true"}, "must match use_lora type"),
    ],
)
def test_registration_rejects_invalid_visible_when_dependencies(
    service: TrainingService,
    target_key: str,
    invalid_condition: dict[str, object],
    expected_message: str,
) -> None:
    client = _client(service, admin=True)
    payload = _conditional_model_payload()
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    target = next(item for item in definitions if item["key"] == target_key)
    target["visible_when"] = invalid_condition

    response = client.post("/api/training/models", json=payload)
    assert response.status_code == 422
    assert expected_message in response.text


def test_registration_rejects_visible_when_cycles_and_invalid_enum_value(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)

    cyclic = _conditional_model_payload()
    definitions = cyclic["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(definitions, list)
    use_lora = next(item for item in definitions if item["key"] == "use_lora")
    lora_rank = next(item for item in definitions if item["key"] == "lora_rank")
    use_lora["visible_when"] = {"parameter_key": "lora_rank", "equals": 8}
    lora_rank["visible_when"] = {"parameter_key": "use_lora", "equals": True}
    cycle_response = client.post("/api/training/models", json=cyclic)
    assert cycle_response.status_code == 422
    assert "cannot contain a cycle" in cycle_response.text

    enum_payload = _conditional_model_payload()
    enum_definitions = enum_payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    assert isinstance(enum_definitions, list)
    enum_definitions.extend(
        [
            {
                "key": "optimizer",
                "label": "Optimizer",
                "type": "enum",
                "default": "adamw",
                "choices": [
                    {"value": "adamw", "label": "AdamW"},
                    {"value": "adafactor", "label": "Adafactor"},
                ],
                "cli_flag": "--optimizer",
            },
            {
                "key": "beta1",
                "label": "Beta one",
                "type": "number",
                "default": 0.9,
                "cli_flag": "--beta1",
                "visible_when": {"parameter_key": "optimizer", "equals": "sgd"},
            },
        ]
    )
    enum_response = client.post("/api/training/models", json=enum_payload)
    assert enum_response.status_code == 422
    assert "not an allowed choice" in enum_response.text


def test_legacy_noneditable_parameter_is_normalized_and_can_be_overridden(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    payload = _model_payload()
    payload["configuration"]["parameter_definitions"][0]["editable"] = False  # type: ignore[index]

    created = client.post("/api/training/models", json=payload)
    assert created.status_code == 201, created.text
    model = created.json()["model"]
    frames = next(
        item
        for item in model["configuration"]["parameter_definitions"]
        if item["key"] == "num_video_frames"
    )
    assert frames["editable"] is True

    preview = client.post(
        "/api/training/runs/preview",
        json=_run_payload(model["model_ref"]),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["run_spec"]["parameters"]["num_video_frames"] == 8


def test_real_training_node_can_bind_a_model_but_cannot_create_a_fake_run(
    tmp_path: Path,
) -> None:
    store = TrainingStore(tmp_path / "real-node-training.sqlite")
    service = TrainingService(store, TrainingResourceProvider(store))
    client = _client(service, admin=True)
    node = store.create_node(
        {
            "name": "NaVILA test node",
            "description": "Registered compute target.",
            "address": "192.0.2.12",
            "ssh_port": 1012,
            "ssh_username": "trainer",
        },
        "development-admin",
    )
    payload = _model_payload(name="NaVILA real node draft")
    payload["configuration"]["launch_template"]["server_ref"] = node["node_ref"]  # type: ignore[index]

    created = client.post("/api/training/models", json=payload)

    assert created.status_code == 201, created.text
    model = created.json()["model"]
    assert model["configuration"]["launch_template"]["server_ref"] == node["node_ref"]
    listed = client.get("/api/training/servers").json()["servers"]
    real_server = next(item for item in listed if item["server_ref"] == node["node_ref"])
    assert real_server["kind"] == "training_node"
    preview = client.post(
        "/api/training/runs/preview",
        json={
            **_run_payload(str(model["model_ref"]), gpu_uuids=["GPU-real-0"]),
            "server_ref": node["node_ref"],
        },
    )
    assert preview.status_code == 400
    assert preview.json()["detail"]["code"] == "real_execution_disabled"


def test_draft_configuration_preview_and_submission_are_safe_and_idempotent(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    model = _create_model(client)
    model_ref = str(model["model_ref"])

    # Internal edits remain optimistic, but their revision is not user-facing.
    update_source = _model_payload(name="NaVILA draft v2")
    updated = {
        "version_description": "Updated draft configuration.",
        "configuration": update_source["configuration"],
        "expected_revision": model["edit_revision"],
    }
    edit = client.put(f"/api/training/models/{model_ref}", json=updated)
    assert edit.status_code == 200, edit.text
    assert edit.json()["model"]["edit_revision"] == model["edit_revision"] + 1
    assert "revision" not in edit.json()["model"]["configuration"]
    stale = client.put(f"/api/training/models/{model_ref}", json=updated)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "model_configuration_edit_conflict"

    request = _run_payload(model_ref)
    preview = client.post("/api/training/runs/preview", json=request)
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["run_spec"]["launcher_kind"] == "direct"
    assert preview_body["run_spec"]["nproc_per_node"] == 1
    assert preview_body["run_spec"]["master_addr"] is None
    assert preview_body["run_spec"]["master_port"] is None
    assert preview_body["run_spec"]["node_rank"] is None
    assert "--nproc_per_node" not in preview_body["command_preview"]
    assert preview_body["run_spec"]["parameters"]["num_video_frames"] == 8
    assert "--num_video_frames 8" in preview_body["command_preview"]
    assert "private" not in preview_body["command_preview"]
    # Preview must be non-mutating: no task or resource lease exists yet.
    assert client.get("/api/training/runs").json()["runs"] == []
    assert service.store.active_gpu_leases() == {}

    created = client.post("/api/training/runs", json=request, headers={"Idempotency-Key": "run-1"})
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    repeat = client.post("/api/training/runs", json=request, headers={"Idempotency-Key": "run-1"})
    assert repeat.status_code == 201
    assert repeat.json()["run"]["run_ref"] == run["run_ref"]
    assert set(service.store.active_gpu_leases()) == {"fake-a100-00", "fake-a100-01"}
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM port_leases").fetchone()[0] == 0

    conflict = client.post(
        "/api/training/runs",
        json=_run_payload(model_ref, gpu_uuids=["fake-a100-00"]),
        headers={"Idempotency-Key": "run-2"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] in {"gpu_unavailable", "gpu_lease_conflict"}


def test_metrics_stop_release_leases_and_event_cursor(service: TrainingService) -> None:
    client = _client(service, admin=True)
    model_ref = str(_create_model(client)["model_ref"])
    created = client.post(
        "/api/training/runs",
        json=_run_payload(model_ref, gpu_uuids=["fake-a100-02"]),
        headers={"Idempotency-Key": "run-3"},
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]

    claimed = service.store.claim_next_run("test-worker")
    assert claimed and claimed["run_ref"] == run["run_ref"]
    running = service.store.transition_running(str(run["run_ref"]), "test-worker")
    stepped = service.store.append_step(
        str(run["run_ref"]),
        "test-worker",
        {
            "step": 1,
            "total_steps": 2,
            "epoch": 1.5,
            "loss": 0.5,
            "learning_rate": 0.0001,
            "grad_norm": 1.2,
            "elapsed_seconds": 0.25,
            "gpus": [{"uuid": "fake-a100-02", "utilization_percent": 75, "memory_used_mib": 30000}],
        },
        "first metric",
    )
    assert stepped["current_step"] == 1
    metrics = client.get(f"/api/training/runs/{run['run_ref']}/metrics?after_seq=0")
    assert metrics.status_code == 200
    assert [item["seq"] for item in metrics.json()["metrics"]] == [1]
    assert client.get(f"/api/training/runs/{run['run_ref']}/logs?after_seq=0").json()["logs"]

    stop = client.post(
        f"/api/training/runs/{run['run_ref']}/stop",
        json={"expected_revision": running["state_revision"]},
        headers={"Idempotency-Key": "stop-1"},
    )
    assert stop.status_code == 200, stop.text
    cancelled = service.store.append_step(
        str(run["run_ref"]), "test-worker", {
            "step": 2, "total_steps": 2, "epoch": 3, "loss": 0.1,
            "learning_rate": 0.0, "grad_norm": 1, "elapsed_seconds": 0.5, "gpus": [],
        }, "cancel",
    )
    assert cancelled["status"] == "cancelled"
    assert service.store.active_gpu_leases() == {}

    events = service.list_events(after_seq=0, limit=100)["items"]
    assert events
    assert [event["event_id"] for event in events] == sorted(event["event_id"] for event in events)
    assert {event["type"] for event in events} >= {"run.updated", "run.metric.appended", "run.log.appended"}


@pytest.mark.asyncio
async def test_fake_worker_success_failure_and_lost_recovery(
    service: TrainingService,
) -> None:
    client = _client(service, admin=True)
    model_ref = str(_create_model(client)["model_ref"])

    async def run_worker_until_terminal(run_ref: str) -> dict[str, object]:
        worker = TrainingWorker(service.store, tick_seconds=0.01)
        worker_task = asyncio.create_task(worker.run_forever())
        try:
            for _ in range(200):
                snapshot = service.get_run(run_ref)
                if snapshot["status"] in {"succeeded", "failed", "cancelled", "lost"}:
                    return snapshot
                await asyncio.sleep(0.01)
            raise AssertionError("fake worker did not reach a terminal state")
        finally:
            await worker.stop()
            await worker_task

    success_response = client.post(
        "/api/training/runs",
        json=_run_payload(model_ref, gpu_uuids=["fake-a100-03"]),
        headers={"Idempotency-Key": "worker-success"},
    )
    success_ref = success_response.json()["run"]["run_ref"]
    success = await run_worker_until_terminal(success_ref)
    assert success["status"] == "succeeded"
    assert service.list_metrics(success_ref, after_seq=0, limit=100)["items"]

    failure_response = client.post(
        "/api/training/runs",
        json=_run_payload(
            model_ref,
            gpu_uuids=["fake-a100-04"],
            simulate_failure=True,
        ),
        headers={"Idempotency-Key": "worker-failure"},
    )
    failure_ref = failure_response.json()["run"]["run_ref"]
    failed = await run_worker_until_terminal(failure_ref)
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "simulated_failure"

    lost_response = client.post(
        "/api/training/runs",
        json=_run_payload(model_ref, gpu_uuids=["fake-a100-05"]),
        headers={"Idempotency-Key": "worker-lost"},
    )
    lost_ref = lost_response.json()["run"]["run_ref"]
    claimed = service.store.claim_next_run("orphaned-worker", lease_seconds=0.001)
    assert claimed and claimed["run_ref"] == lost_ref
    await asyncio.sleep(0.01)
    assert service.store.recover_stale_runs() == 1
    assert service.get_run(lost_ref)["status"] == "lost"
    assert "fake-a100-05" not in service.store.active_gpu_leases()
