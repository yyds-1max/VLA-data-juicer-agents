from __future__ import annotations

from datetime import UTC, datetime
import threading
import time
from typing import Callable

from .client import CenterClientError, OfflineCenterClient, WorkerCenterClient
from .identity import WorkerIdentity
from .ledger import WorkerLedger
from .resources import ResourceCollector


class TrainingWorkerDaemon:
    """Inventory and reconciliation loop; v1 has no execution capability."""

    def __init__(
        self,
        *,
        identity: WorkerIdentity,
        ledger: WorkerLedger,
        resource_collector: ResourceCollector,
        center_client: WorkerCenterClient | None = None,
        interval_seconds: float = 15.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.identity = identity
        self.ledger = ledger
        self.resource_collector = resource_collector
        self.center_client = center_client or OfflineCenterClient()
        self.interval_seconds = interval_seconds
        self._clock = monotonic_clock
        self._started_at = self._clock()
        self._sequence = 0
        self._stop_event = threading.Event()
        self._reconciled = False

    def run_once(self) -> dict[str, object]:
        reconciliation_payload: list[dict[str, str]] = []
        if not self._reconciled:
            reconciliation_payload = [
                result.to_payload() for result in self.ledger.reconcile_active_runs()
            ]
            self._reconciled = True
        self._sequence += 1
        resource_payload = self.resource_collector.collect()
        gpu_collection = resource_payload["gpu_collection"]
        degraded = isinstance(gpu_collection, dict) and gpu_collection.get("error") is not None
        payload: dict[str, object] = {
            "schema_version": 1,
            "worker_id": self.identity.worker_id,
            "sequence": self._sequence,
            "sampled_at": datetime.now(UTC).isoformat(),
            "health": {
                "status": "degraded" if degraded else "healthy",
                "uptime_seconds": round(self._clock() - self._started_at, 3),
                "execution_enabled": False,
                "ledger_state_counts": self.ledger.state_counts(),
            },
            "capabilities": {
                "resource_inventory": True,
                "restart_reconciliation": True,
                "training_execution": False,
                "arbitrary_command_execution": False,
            },
            "resources": resource_payload,
            "reconciliation": reconciliation_payload,
        }
        self.center_client.publish_heartbeat(self.identity, payload)
        return payload

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except CenterClientError:
                # A center outage must not turn into a systemd restart storm.
                # The exception is deliberately not interpolated here because
                # transport errors are operational data, not worker payloads.
                pass
            self._stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()
