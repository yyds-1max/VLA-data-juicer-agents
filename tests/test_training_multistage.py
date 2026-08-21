from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.migrations import (
    _MIGRATION_001,
    _MIGRATION_002,
    _MIGRATION_003,
    _MIGRATION_004,
    _MIGRATION_005,
    _MIGRATION_006,
)
from vla_data_juicer_agents.training.resources import FakeResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore
from vla_data_juicer_agents.training.worker import TrainingWorker


def _configuration(*, entrypoint: str = "train.py", stage_input: bool = True) -> dict[str, object]:
    definitions: list[dict[str, object]] = [
        {
            "key": "max_steps",
            "label": "训练步数",
            "type": "integer",
            "default": 2,
            "minimum": 1,
            "maximum": 10,
            "cli_flag": "--max_steps",
        },
        {
            "key": "simulate_failure",
            "label": "模拟失败",
            "type": "boolean",
            "default": False,
            "argument_style": "explicit_boolean",
            "cli_flag": "--simulate_failure",
        },
    ]
    if stage_input:
        definitions.insert(
            0,
            {
                "key": "model_name_or_path",
                "label": "预训练参数加载地址",
                "type": "string",
                "semantic_role": "stage_input",
                "default": "/models/base",
                "cli_flag": "--model_name_or_path",
            },
        )
    return {
        "launch_template": {
            "domain": "vla",
            "server_ref": "fake-local",
            "working_directory": "/workspace/navila",
            "launcher_kind": "torchrun",
            "executable": "torchrun",
            "entrypoint": entrypoint,
            "fixed_argv": [],
            "output_root": "/workspace/outputs",
            "output_flag": "--output_dir",
        },
        "parameter_definitions": definitions,
    }


def _family_payload(*, name: str = "NaVILA", stage_input: bool = True) -> dict[str, object]:
    return {
        "family_name": name,
        "configuration": _configuration(stage_input=stage_input),
    }


def _run_payload(
    family_ref: str,
    *,
    stages: list[dict[str, object]] | None = None,
    gpu_uuids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "family_ref": family_ref,
        "server_ref": "fake-local",
        "gpu_uuids": gpu_uuids or ["fake-a100-00"],
        "execution_mode": "simulation",
        "version_description": "Regression training workflow",
        "stages": stages
        or [
            {
                "stage_input_source": "manual",
                "parameters": {
                    "model_name_or_path": "/models/base",
                    "max_steps": 2,
                    "simulate_failure": False,
                },
            }
        ],
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


def _create_family(client: TestClient, **overrides: object) -> dict[str, object]:
    payload = _family_payload()
    payload.update(overrides)
    response = client.post("/api/training/models", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["model"]


def test_v6_to_v7_keeps_nodes_and_latest_family_configuration_but_clears_runs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "training-v6.sqlite"
    now = "2026-08-14T10:00:00+00:00"
    configuration = _configuration()
    launch_template = configuration["launch_template"]
    definitions = configuration["parameter_definitions"]
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE training_schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"""
        )
        for version, name, migration in (
            (1, "training_platform_m1", _MIGRATION_001),
            (2, "training_nodes_m2", _MIGRATION_002),
            (3, "training_node_deployment_m3", _MIGRATION_003),
            (4, "model_families_m4", _MIGRATION_004),
            (5, "model_worker_verification_m5", _MIGRATION_005),
            (6, "training_node_revision_split_m6", _MIGRATION_006),
        ):
            db.executescript(migration)
            db.execute(
                "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (version, name, now),
            )
        family_id = db.execute(
            """INSERT INTO model_families(family_ref,name,created_at,updated_at)
            VALUES('family_navila','NaVILA',?,?)""",
            (now, now),
        ).lastrowid
        model_ids: list[int] = []
        revision_ids: list[int] = []
        for version, entrypoint in ((1, "old.py"), (2, "latest.py")):
            model_id = int(
                db.execute(
                    """INSERT INTO registered_models(
                    model_ref,name,description,status,current_revision,created_at,updated_at,
                    family_id,version_number,version_description)
                    VALUES(?,?,?,?,1,?,?,?,?,?)""",
                    (
                        f"model_v{version}",
                        "NaVILA",
                        "",
                        "verified",
                        now,
                        now,
                        family_id,
                        version,
                        f"legacy v{version}",
                    ),
                ).lastrowid
            )
            template = dict(launch_template)  # type: ignore[arg-type]
            template["entrypoint"] = entrypoint
            revision_id = int(
                db.execute(
                    """INSERT INTO model_revisions(
                    revision_ref,model_id,revision_number,working_directory,entrypoint,
                    fixed_argv_json,output_template,parameter_definitions_json,
                    launch_template_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"mrev_v{version}",
                        model_id,
                        1,
                        "/workspace/navila",
                        entrypoint,
                        "[]",
                        "/workspace/outputs/{run_ref}",
                        json.dumps(definitions),
                        json.dumps(template),
                        now,
                    ),
                ).lastrowid
            )
            model_ids.append(model_id)
            revision_ids.append(revision_id)
        db.execute(
            "UPDATE registered_models SET based_on_model_id=? WHERE id=?",
            (model_ids[0], model_ids[1]),
        )
        db.execute(
            "UPDATE registered_models SET based_on_model_id=? WHERE id=?",
            (model_ids[0], model_ids[1]),
        )
        node_id = db.execute(
            """INSERT INTO training_nodes(
            node_ref,name,address,ssh_port,ssh_username,status,state_revision,
            deployment_status,heartbeat_revision,created_at,updated_at)
            VALUES('node_keep','Shared node','10.0.0.12',22,'worker','online',2,
            'succeeded',3,?,?)""",
            (now, now),
        ).lastrowid
        run_id = db.execute(
            """INSERT INTO training_runs(
            run_ref,model_id,model_revision_id,mode,server_ref,gpu_uuids_json,
            parameters_json,run_spec_json,command_preview,status,state_revision,seed,
            total_steps,created_at,updated_at)
            VALUES('run_old',?,?,'simulation','fake-local','["fake-a100-00"]',
            '{}','{}','','running',1,1,2,?,?)""",
            (model_ids[0], revision_ids[0], now, now),
        ).lastrowid
        db.execute(
            "INSERT INTO gpu_leases(gpu_uuid,run_id,acquired_at) VALUES('fake-a100-00',?,?)",
            (run_id, now),
        )
        db.execute(
            """INSERT INTO training_node_resource_snapshots(node_id,captured_at,resources_json)
            VALUES(?,?,?)""",
            (node_id, now, json.dumps({"cpu": {"logical_cores": 8}})),
        )
        db.commit()

    TrainingStore(path)
    TrainingStore(path)

    with sqlite3.connect(path) as db:
        family = db.execute(
            """SELECT family.family_ref,model.model_ref,model.status,revision.entrypoint
            FROM model_families AS family
            JOIN registered_models AS model ON model.id=family.current_model_id
            JOIN model_revisions AS revision
              ON revision.model_id=model.id AND revision.revision_number=model.current_revision"""
        ).fetchone()
        assert family == ("family_navila", "model_v2", "draft", "latest.py")
        assert db.execute("SELECT COUNT(*) FROM registered_models").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM model_revisions").fetchone()[0] == 1
        assert db.execute(
            "SELECT revision_number FROM model_revisions"
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM training_nodes").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM training_node_resource_snapshots").fetchone()[0] == 1
        for table in (
            "training_runs",
            "gpu_leases",
            "port_leases",
            "run_logs",
            "metric_samples",
            "model_versions",
            "training_stages",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute(
            "SELECT version,name FROM training_schema_migrations ORDER BY version DESC LIMIT 1"
                ).fetchone() == (14, "dataset_replica_node_recovery_m14")


def test_model_registration_creates_an_editable_family_without_public_version(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)

    assert family["family_name"] == "NaVILA"
    assert family["status"] == "draft"
    assert family["trained_version_count"] == 0
    assert "version_number" not in family
    assert "version_description" not in family
    assert "configuration_locked" not in family

    update = client.put(
        f"/api/training/models/{family['family_ref']}",
        json={
            "expected_revision": family["edit_revision"],
            "configuration": _configuration(entrypoint="train-new.py"),
        },
    )
    assert update.status_code == 200, update.text
    edited = update.json()["model"]
    assert edited["edit_revision"] == family["edit_revision"] + 1
    assert edited["status"] == "draft"
    assert edited["configuration"]["launch_template"]["entrypoint"] == "train-new.py"


def test_get_model_record_reads_configuration_and_revision_in_one_snapshot(
    service: TrainingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    family = _create_family(_client(service))
    original_connection = service.store.connection
    connection_count = 0

    @contextmanager
    def counted_connection():
        nonlocal connection_count
        connection_count += 1
        with original_connection() as db:
            yield db

    monkeypatch.setattr(service.store, "connection", counted_connection)
    record = service.store.get_model_record(str(family["family_ref"]))

    assert connection_count == 1
    assert record["internal_revision"] == family["edit_revision"]
    assert record["revision_ref"]
    assert record["launch_template"]["entrypoint"] == "train.py"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda definitions: definitions.append(
            {
                "key": "second_input",
                "label": "另一阶段输入",
                "type": "string",
                "semantic_role": "stage_input",
                "default": "/models/other",
                "cli_flag": "--second_input",
            }
        ),
        lambda definitions: definitions.__setitem__(
            0,
            {
                **definitions[0],
                "type": "integer",
                "default": 1,
            },
        ),
        lambda definitions: definitions.__setitem__(
            0,
            {
                **definitions[0],
                "visible_when": {
                    "parameter_key": "simulate_failure",
                    "equals": False,
                },
            },
        ),
    ],
)
def test_stage_input_must_be_a_unique_string(service: TrainingService, mutate: object) -> None:
    payload = _family_payload()
    definitions = payload["configuration"]["parameter_definitions"]  # type: ignore[index]
    mutate(definitions)  # type: ignore[operator]
    response = _client(service).post("/api/training/models", json=payload)
    assert response.status_code == 422


def test_multistage_preview_resolves_previous_output_without_creating_a_version(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    request = _run_payload(
        str(family["family_ref"]),
        stages=[
            {
                "stage_input_source": "manual",
                "parameters": {
                    "model_name_or_path": "/models/base",
                    "max_steps": 2,
                    "simulate_failure": False,
                },
            },
            {
                "stage_input_source": "previous_stage_output",
                "parameters": {"max_steps": 3, "simulate_failure": False},
            },
        ],
    )

    preview = client.post("/api/training/runs/preview", json=request)
    assert preview.status_code == 200, preview.text
    stages = preview.json()["stages"]
    assert [stage["stage_number"] for stage in stages] == [1, 2]
    assert stages[0]["output_directory"].endswith("/preview/stage-01")
    assert stages[1]["output_directory"].endswith("/preview/stage-02")
    assert stages[1]["parameters"]["model_name_or_path"] == stages[0]["output_directory"]
    assert stages[1]["run_spec"]["parameters"]["model_name_or_path"] == stages[0]["output_directory"]
    assert stages[0]["output_directory"] in stages[1]["command_preview"]
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM model_versions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM training_runs").fetchone()[0] == 0


def test_previous_output_source_requires_a_registered_stage_input(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client, **_family_payload(stage_input=False))
    response = client.post(
        "/api/training/runs/preview",
        json=_run_payload(
            str(family["family_ref"]),
            stages=[
                {
                    "stage_input_source": "manual",
                    "parameters": {"max_steps": 1, "simulate_failure": False},
                },
                {
                    "stage_input_source": "previous_stage_output",
                    "parameters": {"max_steps": 1, "simulate_failure": False},
                },
            ],
        ),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "stage_input_not_registered"


def test_previous_output_validates_the_actual_generated_path(
    service: TrainingService,
) -> None:
    client = _client(service)
    payload = _family_payload()
    stage_input = payload["configuration"]["parameter_definitions"][0]  # type: ignore[index]
    stage_input["string_max_length"] = 20  # type: ignore[index]
    family = _create_family(client, **payload)

    response = client.post(
        "/api/training/runs/preview",
        json=_run_payload(
            str(family["family_ref"]),
            stages=[
                {
                    "stage_input_source": "manual",
                    "parameters": {
                        "model_name_or_path": "/models/base",
                        "max_steps": 1,
                        "simulate_failure": False,
                    },
                },
                {
                    "stage_input_source": "previous_stage_output",
                    "parameters": {"max_steps": 1, "simulate_failure": False},
                },
            ],
        ),
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_parameter"


def test_run_creation_allocates_one_version_and_idempotency_reuses_it(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    request = _run_payload(
        str(family["family_ref"]),
        stages=[
            {
                "stage_input_source": "manual",
                "parameters": {
                    "model_name_or_path": "/models/base",
                    "max_steps": 1,
                    "simulate_failure": False,
                },
            },
            {
                "stage_input_source": "previous_stage_output",
                "parameters": {"max_steps": 1, "simulate_failure": False},
            },
        ],
    )
    headers = {"Idempotency-Key": "same-workflow"}

    first = client.post("/api/training/runs", json=request, headers=headers)
    second = client.post("/api/training/runs", json=request, headers=headers)
    assert first.status_code == 201, first.text
    assert second.status_code in {200, 201}, second.text
    first_run = first.json()["run"]
    second_run = second.json()["run"]
    assert second_run["run_ref"] == first_run["run_ref"]
    assert second_run["version_ref"] == first_run["version_ref"]
    assert first_run["version_number"] == 1
    assert first_run["version_date"] == datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    assert first_run["version_label"] == f"v1-{first_run['version_date']}"
    assert first_run["stage_count"] == 2
    assert [stage["stage_name"] for stage in first_run["stages"]] == ["第一阶段", "第二阶段"]
    assert first_run["stages"][1]["parameters"]["model_name_or_path"] == first_run["stages"][0]["output_directory"]
    assert f"/{family['family_ref']}/{first_run['version_label']}/stage-01" in first_run["stages"][0]["output_directory"]
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM model_versions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM training_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM training_stages").fetchone()[0] == 2


def test_create_idempotency_survives_family_edit_and_rejects_changed_request(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    request = _run_payload(str(family["family_ref"]))
    headers = {"Idempotency-Key": "public-request-idempotency"}
    created_response = client.post(
        "/api/training/runs", json=request, headers=headers
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["run"]

    updated = client.put(
        f"/api/training/models/{family['family_ref']}",
        json={
            "expected_revision": family["edit_revision"],
            "configuration": _configuration(entrypoint="train-updated.py"),
        },
    )
    assert updated.status_code == 200, updated.text

    repeated_response = client.post(
        "/api/training/runs", json=request, headers=headers
    )
    assert repeated_response.status_code == 201, repeated_response.text
    repeated = repeated_response.json()["run"]
    assert repeated["run_ref"] == created["run_ref"]
    assert repeated["version_ref"] == created["version_ref"]

    changed_request = {
        **request,
        "gpu_uuids": ["fake-a100-01"],
    }
    conflict = client.post(
        "/api/training/runs", json=changed_request, headers=headers
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_failed_submission_does_not_consume_a_version_number(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    invalid = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"]), gpu_uuids=["missing-gpu"]),
        headers={"Idempotency-Key": "invalid-resource"},
    )
    assert invalid.status_code in {400, 409}

    created = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"])),
        headers={"Idempotency-Key": "first-valid"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["run"]["version_number"] == 1


def test_family_edit_after_training_does_not_change_the_historical_stage_snapshot(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"])),
        headers={"Idempotency-Key": "historical-snapshot"},
    )
    assert created.status_code == 201, created.text
    run_ref = created.json()["run"]["run_ref"]

    updated = client.put(
        f"/api/training/models/{family['family_ref']}",
        json={
            "expected_revision": family["edit_revision"],
            "configuration": _configuration(entrypoint="future.py"),
        },
    )
    assert updated.status_code == 200, updated.text
    historical = client.get(f"/api/training/runs/{run_ref}").json()["run"]
    assert historical["stages"][0]["run_spec"]["entrypoint"] == "train.py"


def test_fake_worker_runs_stages_in_order_and_keeps_run_wide_sequences(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(
            str(family["family_ref"]),
            stages=[
                {
                    "stage_input_source": "manual",
                    "parameters": {
                        "model_name_or_path": "/models/base",
                        "max_steps": 2,
                        "simulate_failure": False,
                    },
                },
                {
                    "stage_input_source": "previous_stage_output",
                    "parameters": {"max_steps": 2, "simulate_failure": False},
                },
            ],
        ),
        headers={"Idempotency-Key": "two-stage-success"},
    )
    assert created.status_code == 201, created.text
    run_ref = created.json()["run"]["run_ref"]

    async def execute() -> None:
        worker = TrainingWorker(service.store, tick_seconds=0.001, worker_id="test-worker")
        task = asyncio.create_task(worker.run_forever())
        for _ in range(500):
            if service.store.get_run(run_ref)["status"] == "succeeded":
                break
            await asyncio.sleep(0.002)
        await worker.stop()
        await task

    asyncio.run(execute())
    run = client.get(f"/api/training/runs/{run_ref}").json()["run"]
    assert run["status"] == "succeeded"
    assert [stage["status"] for stage in run["stages"]] == ["succeeded", "succeeded"]
    assert run["current_stage_number"] == 2
    assert run["version_model"]["output_directory"] == run["stages"][1]["output_directory"]
    logs = client.get(f"/api/training/runs/{run_ref}/logs?after_seq=0&limit=100").json()["logs"]
    metrics = client.get(f"/api/training/runs/{run_ref}/metrics?after_seq=0&limit=100").json()["metrics"]
    assert [item["seq"] for item in logs] == list(range(1, len(logs) + 1))
    assert [item["seq"] for item in metrics] == list(range(1, len(metrics) + 1))
    assert {item["stage_number"] for item in metrics} == {1, 2}
    stage_two = run["stages"][1]["stage_ref"]
    filtered = client.get(
        f"/api/training/runs/{run_ref}/metrics?after_seq=0&limit=100&stage_ref={stage_two}"
    ).json()["metrics"]
    assert filtered and {item["stage_ref"] for item in filtered} == {stage_two}
    with service.store.connection() as db:
        artifacts = [
            tuple(row)
            for row in db.execute(
                "SELECT kind,path FROM training_artifacts ORDER BY id"
            ).fetchall()
        ]
    assert artifacts == [
        ("stage_output", run["stages"][0]["output_directory"]),
        ("stage_output", run["stages"][1]["output_directory"]),
        ("version_model", run["stages"][1]["output_directory"]),
    ]


def test_stage_failure_fails_parent_and_skips_later_stages(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(
            str(family["family_ref"]),
            stages=[
                {
                    "stage_input_source": "manual",
                    "parameters": {
                        "model_name_or_path": "/models/base",
                        "max_steps": 1,
                        "simulate_failure": False,
                    },
                },
                {
                    "stage_input_source": "previous_stage_output",
                    "parameters": {"max_steps": 2, "simulate_failure": True},
                },
                {
                    "stage_input_source": "previous_stage_output",
                    "parameters": {"max_steps": 1, "simulate_failure": False},
                },
            ],
        ),
        headers={"Idempotency-Key": "stage-two-fails"},
    )
    assert created.status_code == 201, created.text
    run_ref = created.json()["run"]["run_ref"]

    async def execute() -> None:
        worker = TrainingWorker(service.store, tick_seconds=0.001, worker_id="failure-worker")
        task = asyncio.create_task(worker.run_forever())
        for _ in range(500):
            if service.store.get_run(run_ref)["status"] == "failed":
                break
            await asyncio.sleep(0.002)
        await worker.stop()
        await task

    asyncio.run(execute())
    run = client.get(f"/api/training/runs/{run_ref}").json()["run"]
    assert run["status"] == "failed"
    assert [stage["status"] for stage in run["stages"]] == [
        "succeeded",
        "failed",
        "skipped",
    ]
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM gpu_leases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM port_leases").fetchone()[0] == 0


def test_concurrent_runs_receive_unique_consecutive_family_versions(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    family_ref = str(family["family_ref"])

    def create(index: int) -> tuple[int, int, str]:
        response = _client(service).post(
            "/api/training/runs",
            json=_run_payload(
                family_ref,
                gpu_uuids=[f"fake-a100-0{index}"],
            ),
            headers={"Idempotency-Key": f"concurrent-version-{index}"},
        )
        body = response.json()
        return (
            response.status_code,
            int(body.get("run", {}).get("version_number", 0)),
            str(body.get("run", {}).get("version_ref", "")),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))

    assert [status for status, _, _ in results] == [201, 201, 201, 201]
    assert sorted(number for _, number, _ in results) == [1, 2, 3, 4]
    assert len({version_ref for _, _, version_ref in results}) == 4


def test_stop_cancels_current_and_pending_stages_and_releases_workflow_leases(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    stages = [
        {
            "stage_input_source": "manual" if index == 0 else "previous_stage_output",
            "parameters": {
                **({"model_name_or_path": "/models/base"} if index == 0 else {}),
                "max_steps": 3,
                "simulate_failure": False,
            },
        }
        for index in range(3)
    ]
    created = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"]), stages=stages),
        headers={"Idempotency-Key": "stop-workflow"},
    )
    run = created.json()["run"]
    service.store.claim_next_run("stop-worker")
    running = service.store.transition_running(run["run_ref"], "stop-worker")

    requested = client.post(
        f"/api/training/runs/{run['run_ref']}/stop",
        json={"expected_revision": running["state_revision"]},
        headers={"Idempotency-Key": "stop-workflow-once"},
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["run"]["status"] == "stop_requested"
    cancelled = service.store.transition_running(run["run_ref"], "stop-worker")
    assert cancelled["status"] == "cancelled"
    assert [stage["status"] for stage in cancelled["stages"]] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]
    assert service.store.active_gpu_leases() == {}
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM port_leases").fetchone()[0] == 0


def test_stop_idempotency_key_cannot_be_reused_for_another_run(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    first = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"]), gpu_uuids=["fake-a100-00"]),
        headers={"Idempotency-Key": "stop-idempotency-run-one"},
    ).json()["run"]
    second = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"]), gpu_uuids=["fake-a100-01"]),
        headers={"Idempotency-Key": "stop-idempotency-run-two"},
    ).json()["run"]

    stopped = client.post(
        f"/api/training/runs/{first['run_ref']}/stop",
        json={"expected_revision": first["state_revision"]},
        headers={"Idempotency-Key": "shared-stop-key"},
    )
    assert stopped.status_code == 200, stopped.text

    conflict = client.post(
        f"/api/training/runs/{second['run_ref']}/stop",
        json={"expected_revision": second["state_revision"]},
        headers={"Idempotency-Key": "shared-stop-key"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert service.store.get_run(second["run_ref"])["status"] == "queued"


def test_expired_worker_lease_marks_active_stage_lost_and_future_stages_skipped(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(
            str(family["family_ref"]),
            stages=[
                {
                    "stage_input_source": "manual",
                    "parameters": {
                        "model_name_or_path": "/models/base",
                        "max_steps": 2,
                        "simulate_failure": False,
                    },
                },
                {
                    "stage_input_source": "previous_stage_output",
                    "parameters": {"max_steps": 2, "simulate_failure": False},
                },
            ],
        ),
        headers={"Idempotency-Key": "lost-workflow"},
    )
    run_ref = created.json()["run"]["run_ref"]
    service.store.claim_next_run("orphan-worker", lease_seconds=0.001)
    service.store.transition_running(run_ref, "orphan-worker")
    time.sleep(0.01)

    assert service.store.recover_stale_runs() == 1
    lost = service.store.get_run(run_ref)
    assert lost["status"] == "lost"
    assert [stage["status"] for stage in lost["stages"]] == ["lost", "skipped"]
    assert lost["failure"]["code"] == "worker_lease_expired"
    assert service.store.active_gpu_leases() == {}


@pytest.mark.asyncio
async def test_worker_periodically_recovers_a_lease_that_expires_after_startup(
    service: TrainingService,
) -> None:
    client = _client(service)
    family = _create_family(client)
    created = client.post(
        "/api/training/runs",
        json=_run_payload(str(family["family_ref"])),
        headers={"Idempotency-Key": "future-expiring-worker-lease"},
    ).json()["run"]
    service.store.claim_next_run("dead-worker", lease_seconds=0.05)

    worker = TrainingWorker(
        service.store,
        tick_seconds=0.01,
        worker_id="replacement-worker",
        recovery_interval_seconds=0.01,
    )
    task = asyncio.create_task(worker.run_forever())
    try:
        for _ in range(100):
            if service.store.get_run(created["run_ref"])["status"] == "lost":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the replacement worker never recovered the stale run")
    finally:
        await worker.stop()
        await task

    recovered = service.store.get_run(created["run_ref"])
    assert recovered["status"] == "lost"
    assert service.store.active_gpu_leases() == {}
