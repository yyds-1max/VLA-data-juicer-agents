"""Node-local Training Worker primitives.

The Worker inventories the host, persists its identity, reconciles process
observations, and executes the fixed managed-dataset protocol. It still does
not start, stop, or signal training processes.
"""

from .daemon import TrainingWorkerDaemon
from .identity import (
    WorkerIdentity,
    load_or_create_identity,
    load_worker_token,
    store_worker_token,
)
from .ledger import ReconciliationResult, WorkerLedger
from .resources import ResourceCollector

__all__ = [
    "ReconciliationResult",
    "ResourceCollector",
    "TrainingWorkerDaemon",
    "WorkerIdentity",
    "WorkerLedger",
    "load_or_create_identity",
    "load_worker_token",
    "store_worker_token",
]
