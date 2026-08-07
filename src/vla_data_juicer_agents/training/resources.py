from __future__ import annotations

from datetime import UTC, datetime
import math
import time
from typing import Any

from .errors import TrainingNotFoundError
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
            from .errors import TrainingValidationError
            raise TrainingValidationError("unknown_gpu", "One or more selected GPUs do not exist.", current={"gpu_uuids": missing})
        occupied = [
            item
            for item in gpu_uuids
            if by_uuid[item]["externally_occupied"]
            or (by_uuid[item]["lease_run_ref"] is not None and not ignore_platform_leases)
        ]
        if occupied:
            from .errors import TrainingConflictError
            raise TrainingConflictError("gpu_unavailable", "One or more selected GPUs are unavailable.", current={"gpu_uuids": occupied})
        return [by_uuid[item] for item in gpu_uuids]
