from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from vla_data_juicer_agents.core.cancellation import CancellationContext


@dataclass(frozen=True)
class OwnerLease:
    agentscope_session_id: str
    generation: int
    cancellation: CancellationContext
    tool_call_ids: frozenset[str]
    wait_for_quiescence: Callable[[float], Awaitable[bool]] | None = None


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

    @classmethod
    def owner_namespace(cls, agentscope_session_id: str) -> str:
        return f"{cls._OWNER_PREFIX}{agentscope_session_id}"

    @classmethod
    def ack_namespace(cls, request_id: str) -> str:
        return f"{cls._ACK_PREFIX}{request_id}"

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        ready = asyncio.Event()

        async def subscribe_loop() -> None:
            async for payload in self._bus.subscribe(
                self.REQUEST_CHANNEL,
                on_ready=ready.set,
            ):
                task = asyncio.create_task(self._handle_request(payload))
                self._handler_tasks.add(task)
                task.add_done_callback(self._handler_tasks.discard)

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

    async def refresh_owners(self) -> None:
        now = time.time()
        current: set[tuple[str, str]] = set()
        for lease in self._owner_leases():
            if lease.generation < 0:
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
            await self._bus.registry_set(
                namespace,
                field,
                value,
                ttl_secs=max(1, int(self.owner_ttl * 2)),
            )
            current.add((namespace, field))
        for namespace, field in self._registered_fields - current:
            await self._bus.registry_del(namespace, field)
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
    ) -> dict[str, dict[str, Any]]:
        """Freeze the owner set before any cancellation can release a lease."""
        await self.refresh_owners()
        return await self._snapshot_expected_owners(
            agentscope_session_ids,
            target_generation,
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            await self.refresh_owners()

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
            matching = [
                lease
                for lease in leases
                if lease.agentscope_session_id == session_id
                and lease.generation == generation
                and lease.generation <= target_generation
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
            await self._bus.registry_set(
                self.ack_namespace(request_id),
                ack_field,
                json.dumps(
                    {
                        "status": "applied",
                        "runtime_id": self.runtime_id,
                        "agentscope_session_id": session_id,
                        "generation": generation,
                        "quiescent_at": time.time(),
                    },
                    sort_keys=True,
                ),
                ttl_secs=max(30, int(self.ack_timeout * 4)),
            )

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
