from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from vla_data_juicer_agents.core.cancellation import CancellationContext

_logger = logging.getLogger(__name__)


class _AmbiguousAppliedAckWriteError(RuntimeError):
    """The ACK write may be durable, but its result could not be reconciled."""


@dataclass(frozen=True)
class OwnerLease:
    agentscope_session_id: str
    generation: int
    cancellation: CancellationContext
    tool_call_ids: frozenset[str]
    wait_for_quiescence: Callable[[float], Awaitable[bool]] | None = None
    publish_owner: bool = True
    suppress_for_ack: Callable[[], bool] | None = None
    restore_after_ack_failure: Callable[[], None] | None = None
    on_acknowledged: Callable[[], None] | None = None


class StopCoordinator:
    """Cross-process cooperative stop discovery and quiescence acknowledgement.

    An applied ACK means the owning process set the cooperative cancellation
    token and every worker tracked by that token actually returned.  Redis
    publish completion and cancellation of an asyncio wrapper are never ACKs.
    """

    REQUEST_CHANNEL = "datapilot:stop:requests"
    _OWNER_PREFIX = "datapilot:stop:owners:"
    _ACK_PREFIX = "datapilot:stop:acks:"

    def __init__(
        self,
        message_bus: Any,
        owner_leases: Callable[[], Iterable[OwnerLease]],
        *,
        runtime_id: str,
        ack_timeout: float = 10.0,
        retry_interval: float = 0.1,
        heartbeat_interval: float = 1.0,
        owner_ttl: float = 5.0,
    ) -> None:
        self._bus = message_bus
        self._owner_leases = owner_leases
        self.runtime_id = runtime_id
        self.ack_timeout = ack_timeout
        self.retry_interval = retry_interval
        self.heartbeat_interval = heartbeat_interval
        self.owner_ttl = owner_ttl
        self._subscriber_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._handler_tasks: set[asyncio.Task] = set()
        self._registered_fields: set[tuple[str, str]] = set()
        self._started = False
        self._refresh_lock = asyncio.Lock()
        self._owner_registry_healthy = False

    @classmethod
    def owner_namespace(cls, agentscope_session_id: str) -> str:
        return f"{cls._OWNER_PREFIX}{agentscope_session_id}"

    @classmethod
    def ack_namespace(cls, request_id: str) -> str:
        return f"{cls._ACK_PREFIX}{request_id}"

    @property
    def started(self) -> bool:
        return self._started

    @property
    def healthy(self) -> bool:
        """Whether owner publication and both coordinator loops are live."""
        return bool(
            self._started
            and self._owner_registry_healthy
            and self._subscriber_task is not None
            and not self._subscriber_task.done()
            and self._heartbeat_task is not None
            and not self._heartbeat_task.done()
        )

    async def start(self) -> None:
        if self._started:
            return
        ready = asyncio.Event()

        async def subscribe_loop() -> None:
            async for payload in self._bus.subscribe(
                self.REQUEST_CHANNEL,
                on_ready=ready.set,
            ):
                task = asyncio.create_task(
                    self._handle_request(payload),
                    name=f"datapilot-stop-handler-{self.runtime_id}",
                )
                self._handler_tasks.add(task)
                task.add_done_callback(self._handler_task_done)

        self._subscriber_task = asyncio.create_task(
            subscribe_loop(),
            name=f"datapilot-stop-subscriber-{self.runtime_id}",
        )
        await ready.wait()
        self._started = True
        await self.refresh_owners()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"datapilot-stop-heartbeat-{self.runtime_id}",
        )

    async def stop(self) -> None:
        self._started = False
        for task in (self._heartbeat_task, self._subscriber_task):
            if task is not None:
                task.cancel()
        for task in tuple(self._handler_tasks):
            task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (
                    self._heartbeat_task,
                    self._subscriber_task,
                    *tuple(self._handler_tasks),
                )
                if task is not None
            ),
            return_exceptions=True,
        )
        for namespace, field in tuple(self._registered_fields):
            with suppress(Exception):
                await self._bus.registry_del(namespace, field)
        self._registered_fields.clear()
        self._heartbeat_task = None
        self._subscriber_task = None
        self._handler_tasks.clear()
        self._owner_registry_healthy = False

    def _handler_task_done(self, task: asyncio.Task) -> None:
        self._handler_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # pylint: disable=broad-except
            _logger.exception("Stop request handler failed")

    async def refresh_owners(self) -> None:
        try:
            async with self._refresh_lock:
                await self._refresh_owners_serialized()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._owner_registry_healthy = False
            raise
        else:
            self._owner_registry_healthy = True

    async def _refresh_owners_serialized(self) -> None:
        now = time.time()
        current: set[tuple[str, str]] = set()
        for lease in self._owner_leases():
            if lease.generation < 0 or not lease.publish_owner:
                continue
            namespace = self.owner_namespace(lease.agentscope_session_id)
            field = f"{self.runtime_id}:{lease.generation}"
            value = json.dumps(
                {
                    "runtime_id": self.runtime_id,
                    "agentscope_session_id": lease.agentscope_session_id,
                    "generation": lease.generation,
                    "heartbeat_at": now,
                    "tool_call_ids": sorted(lease.tool_call_ids),
                },
                sort_keys=True,
            )
            try:
                await self._bus.registry_set(
                    namespace,
                    field,
                    value,
                    ttl_secs=max(1, int(self.owner_ttl * 2)),
                )
            except Exception:
                # A timed-out Redis write may have reached the server. Remove
                # that possible ghost field before reporting publication
                # failure to admission.
                with suppress(Exception):
                    await self._bus.registry_del(namespace, field)
                raise
            current.add((namespace, field))
            self._registered_fields.add((namespace, field))
        for namespace, field in self._registered_fields - current:
            await self._bus.registry_del(namespace, field)
            self._registered_fields.discard((namespace, field))
        self._registered_fields = current

    async def request_and_wait(
        self,
        *,
        request_id: str,
        target_generation: int,
        agentscope_session_ids: list[str],
        require_owner: bool = False,
        expected_owners: dict[str, dict[str, Any]] | None = None,
    ) -> int:
        expected = expected_owners
        if expected is None:
            expected = await self.snapshot_expected_owners(
                agentscope_session_ids,
                target_generation,
            )
        if require_owner and not expected:
            raise TimeoutError("no live owner available for stop acknowledgement")
        if not expected:
            return 0

        deadline = asyncio.get_running_loop().time() + self.ack_timeout
        missing = dict(expected)
        while missing:
            payload = {
                "request_id": request_id,
                "target_generation": target_generation,
                "expected": list(missing.values()),
            }
            await self._bus.publish(self.REQUEST_CHANNEL, payload)
            await asyncio.sleep(self.retry_interval)
            acknowledgements = await self._bus.registry_getall(
                self.ack_namespace(request_id)
            )
            missing = {
                field: owner
                for field, owner in expected.items()
                if not self._ack_is_applied(acknowledgements.get(field))
            }
            if not missing:
                return len(expected)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("timed out waiting for owner acknowledgement")
        return len(expected)

    async def snapshot_expected_owners(
        self,
        agentscope_session_ids: list[str],
        target_generation: int,
        *,
        required_runtime_ids: Iterable[str] = (),
    ) -> dict[str, dict[str, Any]]:
        """Freeze the owner set before any cancellation can release a lease."""
        await self.refresh_owners()
        try:
            expected = await self._snapshot_expected_owners(
                agentscope_session_ids,
                target_generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._owner_registry_healthy = False
            raise
        self._owner_registry_healthy = True
        required = frozenset(required_runtime_ids)
        available = {
            str(owner["runtime_id"])
            for owner in expected.values()
            if "runtime_id" in owner
        }
        if not required.issubset(available):
            raise TimeoutError(
                "no live owner available for admitted runtime acknowledgement"
            )
        return expected

    async def _heartbeat_loop(self) -> None:
        consecutive_failures = 0
        while True:
            delay = (
                self.heartbeat_interval
                if consecutive_failures == 0
                else min(
                    max(self.retry_interval, self.heartbeat_interval)
                    * (2 ** min(consecutive_failures - 1, 8)),
                    max(self.heartbeat_interval, self.owner_ttl / 2),
                )
            )
            await asyncio.sleep(delay)
            try:
                await self.refresh_owners()
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                consecutive_failures += 1
                _logger.exception(
                    "Stop owner heartbeat failed; retrying",
                    extra={"runtime_id": self.runtime_id},
                )
            else:
                consecutive_failures = 0

    async def _snapshot_expected_owners(
        self,
        agentscope_session_ids: list[str],
        target_generation: int,
    ) -> dict[str, dict[str, Any]]:
        expected: dict[str, dict[str, Any]] = {}
        freshness_cutoff = time.time() - self.owner_ttl
        for session_id in dict.fromkeys(agentscope_session_ids):
            entries = await self._bus.registry_getall(self.owner_namespace(session_id))
            for owner_field, raw in entries.items():
                try:
                    owner = json.loads(raw)
                    generation = int(owner["generation"])
                    heartbeat_at = float(owner["heartbeat_at"])
                    runtime_id = str(owner["runtime_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if generation > target_generation or heartbeat_at < freshness_cutoff:
                    continue
                ack_field = self._ack_field(runtime_id, session_id, generation)
                expected[ack_field] = {
                    "ack_field": ack_field,
                    "owner_field": owner_field,
                    "runtime_id": runtime_id,
                    "agentscope_session_id": session_id,
                    "generation": generation,
                }
        return expected

    async def _handle_request(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        request_id = payload.get("request_id")
        target_generation = payload.get("target_generation")
        expected = payload.get("expected")
        if not isinstance(request_id, str) or not isinstance(expected, list):
            return
        try:
            target_generation = int(target_generation)
        except (TypeError, ValueError):
            return

        leases = list(self._owner_leases())
        grouped: dict[str, list[tuple[str, int]]] = {}
        for owner in expected:
            if not isinstance(owner, dict) or owner.get("runtime_id") != self.runtime_id:
                continue
            session_id = owner.get("agentscope_session_id")
            ack_field = owner.get("ack_field")
            generation = owner.get("generation")
            if not isinstance(session_id, str) or not isinstance(ack_field, str):
                continue
            try:
                generation = int(generation)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(session_id, []).append((ack_field, generation))

        for session_id, frozen in grouped.items():
            # Owner refresh publishes the admitted generation before it
            # removes the baseline generation. A requester can therefore
            # freeze either field (or both), while the process can still hold
            # both an old pending-stop tombstone and a newer active lease. All
            # frozen fields for one runtime/session form one acknowledgement
            # group; pruning the union after only its first field would make
            # the remaining fields impossible to ACK.
            oldest_frozen_generation = min(generation for _field, generation in frozen)
            matching = [
                lease
                for lease in leases
                if lease.agentscope_session_id == session_id
                and lease.generation <= target_generation
                and (
                    oldest_frozen_generation <= lease.generation
                    or not lease.publish_owner
                )
            ]
            if not matching:
                continue
            for lease in matching:
                lease.cancellation.cancel()
            quiescent = await asyncio.gather(
                *(self._wait_for_lease(lease) for lease in matching)
            )
            if not all(quiescent):
                continue
            async with self._refresh_lock:
                prepared: list[OwnerLease] = []
                try:
                    for lease in matching:
                        if (
                            lease.suppress_for_ack is not None
                            and not lease.suppress_for_ack()
                        ):
                            raise RuntimeError(
                                "stop owner became active before acknowledgement"
                            )
                        prepared.append(lease)
                    # Heartbeat uses the same lock. Remove every quiescent
                    # matching owner before the applied ACK can be observed.
                    await self._refresh_owners_serialized()
                except Exception:
                    for lease in prepared:
                        if lease.restore_after_ack_failure is not None:
                            lease.restore_after_ack_failure()
                    with suppress(Exception):
                        await self._refresh_owners_serialized()
                    raise

                acknowledged_fields: list[str] = []
                try:
                    for ack_field, generation in frozen:
                        await self._write_applied_ack(
                            request_id=request_id,
                            ack_field=ack_field,
                            agentscope_session_id=session_id,
                            generation=generation,
                        )
                        acknowledged_fields.append(ack_field)
                except _AmbiguousAppliedAckWriteError:
                    # registry_set may already be visible to the requester even
                    # though this process could not read it back. Re-publishing
                    # the owner after an applied ACK is therefore unsafe. Keep
                    # the exact quiescent union suppressed so a later replay can
                    # reconcile the ACK and invoke on_acknowledged.
                    raise
                except Exception:
                    # With no visible field, the group can safely return to
                    # normal owner publication. After a partial write, keep
                    # the complete union suppressed: the requester resends
                    # only missing fields, and suppressed older leases remain
                    # matchable until the group converges.
                    if not acknowledged_fields:
                        for lease in prepared:
                            if lease.restore_after_ack_failure is not None:
                                lease.restore_after_ack_failure()
                        await self._refresh_owners_serialized()
                    raise

                for lease in matching:
                    if lease.on_acknowledged is not None:
                        lease.on_acknowledged()

    async def _write_applied_ack(
        self,
        *,
        request_id: str,
        ack_field: str,
        agentscope_session_id: str,
        generation: int,
    ) -> None:
        ack_namespace = self.ack_namespace(request_id)
        payload = json.dumps(
            {
                "status": "applied",
                "runtime_id": self.runtime_id,
                "agentscope_session_id": agentscope_session_id,
                "generation": generation,
                "quiescent_at": time.time(),
            },
            sort_keys=True,
        )
        try:
            await self._bus.registry_set(
                ack_namespace,
                ack_field,
                payload,
                ttl_secs=max(30, int(self.ack_timeout * 4)),
            )
            return
        except Exception as write_error:
            deadline = asyncio.get_running_loop().time() + self.ack_timeout
            while True:
                try:
                    acknowledgements = await self._bus.registry_getall(ack_namespace)
                except Exception:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise _AmbiguousAppliedAckWriteError(
                            "applied ACK write outcome could not be reconciled"
                        ) from write_error
                    await asyncio.sleep(self.retry_interval)
                    continue
                if self._ack_is_applied(acknowledgements.get(ack_field)):
                    return
                raise write_error

    async def _wait_for_lease(self, lease: OwnerLease) -> bool:
        if lease.wait_for_quiescence is not None:
            return await lease.wait_for_quiescence(self.ack_timeout)
        return await lease.cancellation.wait_for_quiescence(
            timeout=self.ack_timeout
        )

    @staticmethod
    def _ack_field(runtime_id: str, session_id: str, generation: int) -> str:
        identity = f"{runtime_id}:{session_id}:{generation}"
        return hashlib.sha256(identity.encode()).hexdigest()

    @staticmethod
    def _ack_is_applied(raw: str | None) -> bool:
        if raw is None:
            return False
        try:
            return json.loads(raw).get("status") == "applied"
        except (AttributeError, json.JSONDecodeError):
            return False
