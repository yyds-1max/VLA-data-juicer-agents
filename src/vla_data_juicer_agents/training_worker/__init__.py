"""Node-local Training Worker primitives.

The Worker inventories the host, persists its identity, executes fixed managed
data operations, and supervises structured real-training actions without a
shell.
"""

from .daemon import TrainingWorkerDaemon
from .identity import (
    WorkerIdentity,
    load_or_create_identity,
    load_worker_token,
    store_worker_token,
)
from .ledger import ReconciliationResult, WorkerLedger
from .execution import TrainingExecutionManager
from .resources import ResourceCollector

__all__ = [
    "ReconciliationResult",
    "ResourceCollector",
    "TrainingWorkerDaemon",
    "TrainingExecutionManager",
    "WorkerIdentity",
    "WorkerLedger",
    "load_or_create_identity",
    "load_worker_token",
    "store_worker_token",
]
