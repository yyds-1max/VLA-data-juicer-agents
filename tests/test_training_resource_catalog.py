from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vla_data_juicer_agents.training.errors import (
    TrainingConflictError,
    TrainingNotFoundError,
    TrainingValidationError,
)
from vla_data_juicer_agents.training.resources import (
    FakeResourceProvider,
    TrainingResourceProvider,
)
from vla_data_juicer_agents.training.store import TrainingStore


@pytest.fixture
def store(tmp_path: Path) -> TrainingStore:
    return TrainingStore(tmp_path / "training.sqlite")


@pytest.fixture
def provider(store: TrainingStore) -> TrainingResourceProvider:
    return TrainingResourceProvider(store)


def _create_node(store: TrainingStore, *, name: str = "Training 12") -> dict[str, object]:
    return store.create_node(
        {
            "name": name,
            "description": "Resource catalogue fixture.",
            "address": "192.0.2.12",
            "ssh_port": 1012,
            "ssh_username": "trainer",
        },
        "test-admin",
    )


def _enroll(store: TrainingStore, node: dict[str, object]) -> tuple[dict[str, object], str]:
    issued = store.create_enrollment_token(
        str(node["node_ref"]),
        int(node["state_revision"]),
        600,
        "test-admin",
    )
    enrolled = store.enroll_node(
        {
            "enrollment_token": issued["enrollment_token"],
            "worker_instance_id": "worker-instance-12",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "capabilities": {
                "hostname": "training-12",
                "operating_system": "Linux 6.8",
                "architecture": "x86_64",
                "conda_environments": ["navila"],
                "worker_features": ["resource_inventory"],
            },
        }
    )
    return enrolled["node"], str(enrolled["worker_token"])


def _heartbeat(
    store: TrainingStore,
    node_ref: str,
    worker_token: str,
    *,
    health: str = "healthy",
    utilization_percent: float = 99.0,
) -> None:
    store.record_node_heartbeat(
        node_ref,
        worker_token,
        {
            "worker_instance_id": "worker-instance-12",
            "worker_version": "0.1.0",
            "protocol_version": 1,
            "health": health,
            "health_message": None,
            "resources": {
                "cpu": {"logical_cores": 64, "load_1m": 4.5},
                "memory": {
                    "total_bytes": 256 * 1024**3,
                    "available_bytes": 128 * 1024**3,
                },
                "disks": [
                    {
                        "mount": "/data",
                        "total_bytes": 8 * 1024**4,
                        "available_bytes": 3 * 1024**4,
                    }
                ],
                "gpus": [
                    {
                        "uuid": "GPU-real-0",
                        "index": 0,
                        "name": "NVIDIA A100 80GB",
                        "memory_total_bytes": 80 * 1024**3,
                        "memory_used_bytes": 79 * 1024**3,
                        "utilization_percent": utilization_percent,
                        "temperature_celsius": 73.5,
                    },
                    {
                        "uuid": "GPU-real-1",
                        "index": 1,
                        "name": "NVIDIA A100 80GB",
                        "memory_total_bytes": 80 * 1024**3,
                        "memory_used_bytes": 0,
                        "utilization_percent": 0.0,
                        "temperature_celsius": None,
                    },
                ],
            },
        },
    )


def test_catalog_replaces_fake_server_after_first_node_registration(
    store: TrainingStore, provider: TrainingResourceProvider
) -> None:
    assert provider.list_servers() == FakeResourceProvider(store).list_servers()
    node = _create_node(store)

    servers = provider.list_servers()

    assert servers == [{
        "server_ref": node["node_ref"],
        "name": "Training 12",
        "kind": "training_node",
        "status": "pending_enrollment",
        "online": False,
        "available": False,
        "stale": True,
        "gpu_count": 0,
    }]


def test_pending_or_online_without_snapshot_is_honestly_empty_and_stale(
    store: TrainingStore, provider: TrainingResourceProvider
) -> None:
    pending = _create_node(store, name="Pending")
    pending_resources = provider.resources(str(pending["node_ref"]))
    assert pending_resources["sampled_at"] is None
    assert pending_resources["stale"] is True
    assert pending_resources["gpus"] == []
    assert pending_resources["server"]["online"] is False

    online, _ = _enroll(store, pending)
    online_resources = provider.resources(str(online["node_ref"]))
    assert online_resources["server"]["status"] == "online"
    assert online_resources["server"]["online"] is True
    assert online_resources["server"]["available"] is False
    assert online_resources["stale"] is True
    assert online_resources["gpus"] == []


def test_worker_snapshot_is_converted_to_existing_server_gpu_contract(
    store: TrainingStore, provider: TrainingResourceProvider
) -> None:
    node, worker_token = _enroll(store, _create_node(store))
    _heartbeat(store, str(node["node_ref"]), worker_token)

    resources = provider.resources(str(node["node_ref"]))

    assert resources["stale"] is False
    assert resources["server"]["kind"] == "training_node"
    assert resources["server"]["gpu_count"] == 2
    assert resources["cpu"] == {"logical_cores": 64, "load_1m": 4.5}
    assert resources["memory"] == {
        "total_bytes": 256 * 1024**3,
        "available_bytes": 128 * 1024**3,
    }
    assert resources["disks"] == [
        {
            "mount": "/data",
            "total_bytes": 8 * 1024**4,
            "available_bytes": 3 * 1024**4,
        }
    ]
    assert resources["gpus"][0] == {
        "gpu_uuid": "GPU-real-0",
        "index": 0,
        "name": "NVIDIA A100 80GB",
        "total_memory_mib": 81_920,
        "used_memory_mib": 80_896,
        "utilization_percent": 99.0,
        "temperature_c": 73.5,
        "externally_occupied": False,
        "lease_run_ref": None,
        "available": True,
    }
    assert resources["gpus"][1]["temperature_c"] == 0.0

    listed = {item["server_ref"]: item for item in provider.list_servers()}
    assert listed[str(node["node_ref"])]["gpu_count"] == 2
    assert listed[str(node["node_ref"])]["available"] is True


def test_real_gpu_selection_checks_identity_not_usage_or_fake_occupancy(
    store: TrainingStore, provider: TrainingResourceProvider
) -> None:
    node, worker_token = _enroll(store, _create_node(store))
    _heartbeat(
        store,
        str(node["node_ref"]),
        worker_token,
        utilization_percent=100.0,
    )

    selected = provider.require_available(
        str(node["node_ref"]), ["GPU-real-0"]
    )

    assert selected[0]["used_memory_mib"] == 80_896
    assert selected[0]["utilization_percent"] == 100.0
    assert selected[0]["externally_occupied"] is False
    with pytest.raises(TrainingValidationError) as raised:
        provider.require_available(str(node["node_ref"]), ["GPU-missing"])
    assert raised.value.code == "unknown_gpu"
    assert raised.value.current == {"gpu_uuids": ["GPU-missing"]}


@pytest.mark.parametrize("health", ["degraded", "repair_required"])
def test_non_healthy_real_node_is_not_schedulable(
    store: TrainingStore,
    provider: TrainingResourceProvider,
    health: str,
) -> None:
    node, worker_token = _enroll(store, _create_node(store))
    _heartbeat(store, str(node["node_ref"]), worker_token, health=health)

    resources = provider.resources(str(node["node_ref"]))
    assert resources["server"]["online"] is False
    assert len(resources["gpus"]) == 2
    assert all(gpu["available"] is False for gpu in resources["gpus"])
    assert resources["stale"] is True
    with pytest.raises(TrainingConflictError) as raised:
        provider.require_available(str(node["node_ref"]), ["GPU-real-0"])
    assert raised.value.code == "training_node_unavailable"


def test_expired_heartbeat_hides_old_snapshot_but_preserves_timestamp(
    store: TrainingStore, provider: TrainingResourceProvider
) -> None:
    node, worker_token = _enroll(store, _create_node(store))
    _heartbeat(store, str(node["node_ref"]), worker_token)
    sampled_at = provider.resources(str(node["node_ref"]))["sampled_at"]
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
        timespec="milliseconds"
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE training_nodes SET last_heartbeat_at=? WHERE node_ref=?",
            (old, node["node_ref"]),
        )

    resources = provider.resources(str(node["node_ref"]))

    assert resources["server"]["status"] == "offline"
    assert resources["server"]["online"] is False
    assert resources["sampled_at"] == sampled_at
    assert resources["stale"] is True
    assert len(resources["gpus"]) == 2
    assert all(gpu["available"] is False for gpu in resources["gpus"])


def test_fake_provider_selection_semantics_are_unchanged(
    provider: TrainingResourceProvider,
) -> None:
    selected = provider.require_available("fake-local", ["fake-a100-00"])
    assert selected[0]["gpu_uuid"] == "fake-a100-00"

    with pytest.raises(TrainingConflictError) as raised:
        provider.require_available("fake-local", ["fake-a100-07"])
    assert raised.value.code == "gpu_unavailable"


def test_unknown_catalog_server_uses_existing_server_error_contract(
    provider: TrainingResourceProvider,
) -> None:
    with pytest.raises(TrainingNotFoundError) as raised:
        provider.resources("node_missing")
    assert raised.value.code == "server_not_found"
