from __future__ import annotations

from datetime import UTC, datetime
import math
import time
from typing import Any

from .errors import (
    TrainingConflictError,
    TrainingNotFoundError,
    TrainingValidationError,
)
from .store import TrainingStore


class FakeResourceProvider:
    """Deterministic, read-only inventory for local simulation."""

    server_ref = "fake-local"
    compatible_server_refs = frozenset({"fake-a100", "fake-local"})

    def __init__(self, store: TrainingStore) -> None:
        self.store = store

    def list_servers(self) -> list[dict[str, Any]]:
        return [{"server_ref": self.server_ref, "name": "Local simulation (8× A100)", "kind": "simulation", "online": True, "gpu_count": 8}]

    def resources(self, server_ref: str) -> dict[str, Any]:
        if server_ref not in self.compatible_server_refs:
            raise TrainingNotFoundError("server_not_found", "Training server was not found.")
        leases = self.store.active_gpu_leases()
        phase = time.monotonic() / 8
        gpus = []
        for index in range(8):
            gpu_uuid = f"fake-a100-{index:02d}"
            leased_by = leases.get(gpu_uuid)
            external = index == 7
            utilization = round(7 + 4 * (1 + math.sin(phase + index)), 1)
            if leased_by:
                utilization = round(58 + 22 * (1 + math.sin(phase + index)) / 2, 1)
            if external:
                utilization = 91.0
            used = 2400 + index * 180 if not leased_by else 34_000 + index * 320
            if external:
                used = 61_000
            gpus.append({"gpu_uuid": gpu_uuid, "index": index, "name": "NVIDIA A100 80GB", "total_memory_mib": 81920, "used_memory_mib": used, "utilization_percent": utilization, "temperature_c": 37 + index + (12 if leased_by else 0), "externally_occupied": external, "lease_run_ref": leased_by, "available": not external and leased_by is None})
        server = dict(self.list_servers()[0])
        server["server_ref"] = server_ref
        return {"server": server, "sampled_at": datetime.now(UTC).isoformat(), "gpus": gpus}

    def require_available(self, server_ref: str, gpu_uuids: list[str], *, ignore_platform_leases: bool = False) -> list[dict[str, Any]]:
        resources = self.resources(server_ref)
        by_uuid = {gpu["gpu_uuid"]: gpu for gpu in resources["gpus"]}
        missing = [item for item in gpu_uuids if item not in by_uuid]
        if missing:
            raise TrainingValidationError("unknown_gpu", "One or more selected GPUs do not exist.", current={"gpu_uuids": missing})
        occupied = [
            item
            for item in gpu_uuids
            if by_uuid[item]["externally_occupied"]
            or (by_uuid[item]["lease_run_ref"] is not None and not ignore_platform_leases)
        ]
        if occupied:
            raise TrainingConflictError("gpu_unavailable", "One or more selected GPUs are unavailable.", current={"gpu_uuids": occupied})
        return [by_uuid[item] for item in gpu_uuids]


class TrainingResourceProvider:
    """Inventory backed exclusively by enrolled Training Nodes.

    This provider is deliberately an inventory adapter, not an execution
    switch.  A real node appearing here does not enable real training.  The
    service's execution-mode gate remains authoritative.

    Node resources are trusted, authenticated Worker snapshots: selection
    checks only node health and GPU identity.  It never guesses external
    occupancy from utilisation or memory consumption.  Local simulation tests
    inject ``FakeResourceProvider`` directly instead of exposing fake hardware
    through this production catalog.
    """

    _SCHEDULABLE_STATUS = "online"

    def __init__(self, store: TrainingStore) -> None:
        self.store = store

    def list_servers(self) -> list[dict[str, Any]]:
        nodes = self.store.list_nodes()
        servers: list[dict[str, Any]] = []
        for node in nodes:
            snapshot = self.store.get_node_resources(node["node_ref"])
            resources = snapshot.get("resources")
            gpu_count = (
                len(resources.get("gpus", []))
                if isinstance(resources, dict)
                else 0
            )
            connected = node["status"] == self._SCHEDULABLE_STATUS
            servers.append(
                {
                    "server_ref": node["node_ref"],
                    "name": node["name"],
                    "kind": "training_node",
                    "status": node["status"],
                    "online": connected,
                    "available": node["status"] == self._SCHEDULABLE_STATUS
                    and not bool(snapshot.get("stale", True)),
                    "stale": bool(snapshot.get("stale", True)),
                    # Keep the last reported inventory visible even when the
                    # node is offline/degraded. ``available`` remains false,
                    # so stale visibility never becomes schedulability.
                    "gpu_count": gpu_count,
                }
            )
        return servers

    def resources(self, server_ref: str) -> dict[str, Any]:
        node = self._get_node(server_ref)
        snapshot = self.store.get_node_resources(server_ref)
        connected = node["status"] == self._SCHEDULABLE_STATUS
        raw_resources = snapshot.get("resources")
        has_snapshot = isinstance(raw_resources, dict)
        stale = bool(snapshot.get("stale", True)) or not connected or not has_snapshot
        server = {
            "server_ref": node["node_ref"],
            "name": node["name"],
            "kind": "training_node",
            "status": node["status"],
            "online": connected,
            "available": node["status"] == self._SCHEDULABLE_STATUS and not stale,
            "stale": stale,
            "gpu_count": 0,
        }
        if not has_snapshot:
            return {
                "server": server,
                "sampled_at": snapshot.get("captured_at"),
                "stale": True,
                "cpu": None,
                "memory": None,
                "disks": [],
                "gpus": [],
            }

        gpus = [
            self._node_gpu_projection(gpu, available=server["available"])
            for gpu in raw_resources.get("gpus", [])
            if isinstance(gpu, dict)
        ]
        server["gpu_count"] = len(gpus)
        return {
            "server": server,
            "sampled_at": snapshot.get("captured_at"),
            "stale": stale,
            "cpu": raw_resources.get("cpu"),
            "memory": raw_resources.get("memory"),
            "disks": [
                disk
                for disk in raw_resources.get("disks", [])
                if isinstance(disk, dict)
            ],
            "gpus": gpus,
        }

    def require_available(
        self,
        server_ref: str,
        gpu_uuids: list[str],
        *,
        ignore_platform_leases: bool = False,
    ) -> list[dict[str, Any]]:
        # ``ignore_platform_leases`` belongs exclusively to an explicitly
        # injected simulation provider. Real-node validation intentionally
        # does not infer external occupancy from utilization or memory use.
        del ignore_platform_leases
        node = self._get_node(server_ref)
        resources = self.resources(server_ref)
        if node["status"] != self._SCHEDULABLE_STATUS or resources["stale"]:
            raise TrainingConflictError(
                "training_node_unavailable",
                "The selected training node is not available.",
                current={"node_ref": server_ref, "status": node["status"]},
            )
        by_uuid = {gpu["gpu_uuid"]: gpu for gpu in resources["gpus"]}
        missing = [gpu_uuid for gpu_uuid in gpu_uuids if gpu_uuid not in by_uuid]
        if missing:
            raise TrainingValidationError(
                "unknown_gpu",
                "One or more selected GPUs do not exist.",
                current={"gpu_uuids": missing},
            )
        return [by_uuid[gpu_uuid] for gpu_uuid in gpu_uuids]

    def _get_node(self, server_ref: str) -> dict[str, Any]:
        try:
            return self.store.get_node(server_ref)
        except TrainingNotFoundError as exc:
            raise TrainingNotFoundError(
                "server_not_found", "Training server was not found."
            ) from exc

    @staticmethod
    def _node_gpu_projection(
        gpu: dict[str, Any], *, available: bool
    ) -> dict[str, Any]:
        temperature = gpu.get("temperature_celsius")
        return {
            "gpu_uuid": gpu["uuid"],
            "index": int(gpu["index"]),
            "name": gpu.get("name", "GPU"),
            "total_memory_mib": _bytes_to_mib(gpu.get("memory_total_bytes", 0)),
            "used_memory_mib": _bytes_to_mib(gpu.get("memory_used_bytes", 0)),
            "utilization_percent": float(gpu.get("utilization_percent", 0)),
            "temperature_c": float(temperature) if temperature is not None else 0.0,
            "externally_occupied": False,
            "lease_run_ref": None,
            "available": available,
        }


def _bytes_to_mib(value: Any) -> int:
    return max(0, int(value)) // (1024 * 1024)
