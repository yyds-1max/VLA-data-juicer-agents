"""Node-local Training Worker primitives.

The v1 worker is deliberately control-plane only.  It inventories the host,
persists its identity, and reconciles process observations recorded in its
local ledger.  It does not start, stop, or signal training processes.
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
