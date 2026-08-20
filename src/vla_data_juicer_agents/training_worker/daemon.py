from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from typing import Callable

from .client import CenterClientError, OfflineCenterClient, WorkerCenterClient
from .datasets import (
    DatasetCommandError,
    DatasetTransferManager,
    discard_partial_dataset,
    list_directories,
    remove_dataset_replica,
)
from .identity import WorkerIdentity
from .ledger import WorkerLedger
from .resources import ResourceCollector
from .model_verification import verify_model_configuration
from .execution import TrainingExecutionManager


class TrainingWorkerDaemon:
    """Node inventory, reconciliation, and managed dataset command loop."""

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
        self._command_thread: threading.Thread | None = None
        self._training_action_thread: threading.Thread | None = None
        self._training_monitor_thread: threading.Thread | None = None
        self._transfer_manager = DatasetTransferManager(
            source_client=self.center_client,
            publish_result=self._publish_dataset_result,
            monotonic_clock=monotonic_clock,
        )
        self._execution_manager = TrainingExecutionManager(
            identity=identity,
            ledger=ledger,
            center_client=self.center_client,
            resource_collector=resource_collector,
            state_dir=ledger.path.parent,
            monotonic_clock=monotonic_clock,
        )

    def run_once(self) -> dict[str, object]:
        reconciliation_payload: list[dict[str, str]] = []
        if not self._reconciled:
            reconciliation_payload = self._execution_manager.reconciliation_updates()
            self._reconciled = True
        self._sequence += 1
        resource_payload = self.resource_collector.collect()
        gpu_collection = resource_payload["gpu_collection"]
        degraded = isinstance(gpu_collection, dict) and gpu_collection.get("error") is not None
        payload: dict[str, object] = {
            "schema_version": 1,
            "worker_id": self.identity.worker_id,
            "sequence": self._sequence,
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "health": {
                "status": "degraded" if degraded else "healthy",
                "uptime_seconds": round(self._clock() - self._started_at, 3),
                "execution_enabled": True,
                "ledger_state_counts": self.ledger.state_counts(),
            },
            "capabilities": {
                "resource_inventory": True,
                "restart_reconciliation": True,
                "directory_browser_v1": True,
                "dataset_transfer_v1": True,
                "training_execution": True,
                "training_execution_v1": True,
                "arbitrary_command_execution": False,
            },
            "resources": resource_payload,
            "reconciliation": reconciliation_payload,
        }
        response = self.center_client.publish_heartbeat(self.identity, payload)
        command = response.get("command") if isinstance(response, dict) else None
        if isinstance(command, dict):
            self._handle_command(command)
        return payload

    def _handle_command(self, command: dict[str, object]) -> None:
        command_ref = command.get("command_ref")
        claim_token = command.get("claim_token")
        kind = command.get("kind")
        command_payload = command.get("payload")
        if (
            not isinstance(command_ref, str)
            or not isinstance(command_payload, dict)
        ):
            return
        if kind != "verify_model_configuration" and (
            not isinstance(claim_token, str)
            or not claim_token.startswith("claim_")
        ):
            return
        if kind == "list_directories":
            try:
                result = {
                    "status": "succeeded",
                    **list_directories(command_payload.get("path")),
                }
            except DatasetCommandError as exc:
                result = _command_failure(exc)
            self.center_client.publish_command_result(
                self.identity,
                command_ref,
                {"claim_token": claim_token, **result},
            )
            return
        if kind == "transfer_dataset":
            try:
                self._transfer_manager.start(
                    command_ref,
                    command_payload,
                    claim_token=claim_token,
                )
            except DatasetCommandError as exc:
                self.center_client.publish_command_result(
                    self.identity,
                    command_ref,
                    {"claim_token": claim_token, **_command_failure(exc)},
                )
            return
        if kind == "cancel_dataset_transfer":
            action = command_payload.get("action", "pause")
            discard_partial = action == "cancel"
            try:
                self._transfer_manager.cancel(
                    command_payload.get("transfer_ref"),
                    discard_partial=discard_partial,
                )
                if not self._transfer_manager.wait(60):
                    raise DatasetCommandError(
                        "dataset_transfer_stop_timeout",
                        "The dataset transfer did not stop in time.",
                    )
                result = {
                    "status": "succeeded",
                    "transfer_ref": command_payload.get("transfer_ref"),
                }
            except DatasetCommandError as exc:
                if exc.code == "dataset_transfer_not_active" and discard_partial:
                    try:
                        result = discard_partial_dataset(command_payload)
                    except DatasetCommandError as cleanup_exc:
                        result = _command_failure(cleanup_exc)
                    except OSError:
                        result = _command_failure(
                            DatasetCommandError(
                                "dataset_partial_remove_failed",
                                "The Worker could not remove the partial dataset.",
                            )
                        )
                elif exc.code == "dataset_transfer_not_active":
                    result = {
                        "status": "succeeded",
                        "transfer_ref": command_payload.get("transfer_ref"),
                    }
                else:
                    result = _command_failure(exc)
            self.center_client.publish_command_result(
                self.identity,
                command_ref,
                {"claim_token": claim_token, **result},
            )
            return
        if kind == "remove_dataset_replica":
            try:
                result = remove_dataset_replica(command_payload)
            except DatasetCommandError as exc:
                result = _command_failure(exc)
            except OSError:
                result = _command_failure(
                    DatasetCommandError(
                        "dataset_replica_remove_failed",
                        "The Worker could not remove the dataset replica.",
                    )
                )
            self.center_client.publish_command_result(
                self.identity,
                command_ref,
                {"claim_token": claim_token, **result},
            )
            return
        if kind != "verify_model_configuration":
            return
        try:
            result = verify_model_configuration(command_payload)
        except (OSError, ValueError):
            result = {
                "status": "failed",
                "checks": [
                    {
                        "code": "verification_request",
                        "label": "验证请求",
                        "status": "failed",
                        "detail": "Worker 无法解析该验证请求。",
                    }
                ],
            }
        self.center_client.publish_command_result(
            self.identity, command_ref, result
        )

    def _publish_dataset_result(
        self, command_ref: str, payload: object
    ) -> None:
        if not isinstance(payload, dict):
            return
        try:
            self.center_client.publish_command_result(
                self.identity, command_ref, payload
            )
        except CenterClientError:
            # The center owns transfer reconciliation. A transient result upload
            # failure must not make the node-local file operation unsafe.
            pass

    def _poll_commands_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                response = self.center_client.poll_commands(
                    self.identity, wait_seconds=25, limit=1
                )
                raw_commands = response.get("commands")
                commands = raw_commands if isinstance(raw_commands, list) else []
                singular = response.get("command")
                if isinstance(singular, dict):
                    commands.append(singular)
                for command in commands:
                    if isinstance(command, dict):
                        self._handle_command(command)
                if not commands:
                    self._stop_event.wait(0.25)
            except CenterClientError:
                self._stop_event.wait(min(self.interval_seconds, 5.0))

    def _poll_training_actions_forever(self) -> None:
        poll = getattr(self.center_client, "poll_training_actions", None)
        publish = getattr(self.center_client, "publish_training_action_result", None)
        if not callable(poll) or not callable(publish):
            return
        while not self._stop_event.is_set():
            try:
                response = poll(self.identity, wait_seconds=25, limit=1)
                raw_actions = response.get("actions")
                actions = raw_actions if isinstance(raw_actions, list) else []
                singular = response.get("action")
                if isinstance(singular, dict):
                    actions.append(singular)
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    result = self._execution_manager.handle_action(action)
                    action_ref = result.pop("action_ref", None)
                    if isinstance(action_ref, str):
                        publish(self.identity, action_ref, result)
                if not actions:
                    self._stop_event.wait(0.25)
            except CenterClientError:
                self._stop_event.wait(min(self.interval_seconds, 5.0))
            except Exception:
                # Malformed actions must not terminate the node daemon.  The
                # center will expire and retry an unacknowledged claim.
                self._stop_event.wait(1.0)

    def _monitor_training_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._execution_manager.tick()
            except Exception:
                # Individual run errors are reported through the durable
                # outbox; an unexpected monitoring error is retried.
                pass
            self._stop_event.wait(1.0)

    def run_forever(self) -> None:
        if self._command_thread is None:
            self._command_thread = threading.Thread(
                target=self._poll_commands_forever,
                name="datapilot-worker-command-poll",
                daemon=True,
            )
            self._command_thread.start()
        if self._training_action_thread is None:
            self._training_action_thread = threading.Thread(
                target=self._poll_training_actions_forever,
                name="datapilot-worker-training-action-poll",
                daemon=True,
            )
            self._training_action_thread.start()
        if self._training_monitor_thread is None:
            self._training_monitor_thread = threading.Thread(
                target=self._monitor_training_forever,
                name="datapilot-worker-training-monitor",
                daemon=True,
            )
            self._training_monitor_thread.start()
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


def _command_failure(error: DatasetCommandError) -> dict[str, object]:
    return {
        "status": "failed",
        "error": {"code": error.code, "message": error.message},
    }
