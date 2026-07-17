from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import TurnSubmission, WebSessionStore

EventCallback = Callable[[str, dict[str, Any]], Any]
logger = logging.getLogger(__name__)


@dataclass
class AgentScopeEventBridgeMetrics:
    worker_starts: int = 0
    worker_stops: int = 0
    subscription_reconnects: int = 0
    projected_events: int = 0
    duplicate_events: int = 0
    last_cursor: str | None = None
    projection_latency_seconds: float | None = None
    unprocessed_events: int = 0


class AgentScopeEventBridge:
    """Continuously project mapped AgentScope sessions into Web sessions."""

    def __init__(
        self,
        *,
        store: WebSessionStore,
        runtime: Any,
        event_callback: EventCallback | None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._event_callback = event_callback
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._stopping = False
        self.metrics = AgentScopeEventBridgeMetrics()

    async def start(self) -> None:
        self._stopping = False
        stale_reply_count = self._store.reconcile_stale_reply_leases()
        if stale_reply_count:
            logger.warning(
                "Released stale AgentScope reply leases during bridge startup: count=%d",
                stale_reply_count,
            )
        await self._reconcile_background_tools()
        for mapping in self._store.list_agentscope_session_mappings():
            await self.ensure_mapping(
                web_session_id=mapping.web_session_id,
                agent_id=mapping.agent_id,
                agentscope_session_id=mapping.agentscope_session_id,
            )

    async def _reconcile_background_tools(self) -> None:
        resolver = getattr(self._runtime, "reconcile_background_tool_status", None)
        if not callable(resolver):
            return
        for background in self._store.list_unresolved_background_tools():
            status = resolver(
                web_session_id=background.web_session_id,
                agentscope_session_id=background.agentscope_session_id,
                tool=background.tool,
            )
            record = self._store.append_background_tool_reconciliation(
                background,
                status=status,
            )
            if record is None:
                continue
            logger.warning(
                "Reconciled orphaned background tool: web_session_id=%s "
                "agentscope_session_id=%s tool=%s status=%s",
                background.web_session_id,
                background.agentscope_session_id,
                background.tool,
                status,
            )
            await self._publish(
                background.web_session_id,
                record.model_dump(mode="json"),
            )

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._workers.values())
        self._workers.clear()
        self._ready.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def ensure_web_session(self, web_session_id: str) -> None:
        mappings = [
            mapping
            for mapping in self._store.list_agentscope_session_mappings()
            if mapping.web_session_id == web_session_id
        ]
        for mapping in mappings:
            await self.ensure_mapping(
                web_session_id=mapping.web_session_id,
                agent_id=mapping.agent_id,
                agentscope_session_id=mapping.agentscope_session_id,
            )

    async def ensure_mapping(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        agentscope_session_id: str,
    ) -> None:
        task = self._workers.get(agentscope_session_id)
        if task is not None and not task.done():
            await self._ready[agentscope_session_id].wait()
            return
        ready = asyncio.Event()
        self._ready[agentscope_session_id] = ready
        task = asyncio.create_task(
            self._run_worker(
                web_session_id=web_session_id,
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
                ready=ready,
            ),
            name=f"agentscope-event-bridge:{agentscope_session_id}",
        )
        self._workers[agentscope_session_id] = task
        await ready.wait()
        if task.done() and (error := task.exception()) is not None:
            raise error

    async def _run_worker(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        agentscope_session_id: str,
        ready: asyncio.Event,
    ) -> None:
        self.metrics.worker_starts += 1
        logger.info(
            "AgentScope event bridge worker starting: web_session_id=%s "
            "agent_id=%s agentscope_session_id=%s",
            web_session_id,
            agent_id,
            agentscope_session_id,
        )
        try:
            while not self._stopping:
                subscription_ready = asyncio.Event()

                def mark_subscription_ready() -> None:
                    subscription_ready.set()
                    ready.set()

                try:
                    async for batch in self._runtime.subscribe_agentscope_session_event_batches(
                        web_session_id=web_session_id,
                        agent_id=agent_id,
                        agentscope_session_id=agentscope_session_id,
                        continuous=True,
                        on_ready=mark_subscription_ready,
                    ):
                        if not ready.is_set():
                            ready.set()
                        records = self._store.append_projected_event_batch(
                            web_session_id=web_session_id,
                            agentscope_session_id=agentscope_session_id,
                            entry_id=batch.entry_id,
                            events=list(batch.events),
                            raw_event_type=batch.raw_event_type,
                            reply_id=getattr(batch, "reply_id", None),
                        )
                        remember_cursor = getattr(self._runtime, "_remember_event_cursor", None)
                        if callable(remember_cursor):
                            remember_cursor(agentscope_session_id, batch.entry_id)
                        self.metrics.last_cursor = batch.entry_id
                        self.metrics.projected_events += len(records)
                        duplicate_count = max(0, len(batch.events) - len(records))
                        self.metrics.duplicate_events += duplicate_count
                        projected_at = getattr(
                            batch,
                            "projected_at_monotonic",
                            time.monotonic(),
                        )
                        self.metrics.projection_latency_seconds = max(
                            0.0,
                            time.monotonic() - projected_at,
                        )
                        self.metrics.unprocessed_events = 0
                        logger.debug(
                            "AgentScope event bridge projected batch: "
                            "web_session_id=%s agentscope_session_id=%s cursor=%s "
                            "raw_type=%s projected=%d duplicates=%d latency_secs=%.6f "
                            "unprocessed=%d",
                            web_session_id,
                            agentscope_session_id,
                            batch.entry_id,
                            batch.raw_event_type,
                            len(records),
                            duplicate_count,
                            self.metrics.projection_latency_seconds,
                            self.metrics.unprocessed_events,
                        )
                        for record in records:
                            await self._publish(web_session_id, record.model_dump(mode="json"))
                    if subscription_ready.is_set() and not ready.is_set():
                        ready.set()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not ready.is_set():
                        ready.set()
                    logger.exception(
                        "AgentScope event bridge worker failed; reconnecting: "
                        "agentscope_session_id=%s",
                        agentscope_session_id,
                    )
                if not self._stopping:
                    self.metrics.subscription_reconnects += 1
                    logger.info(
                        "AgentScope event bridge subscription reconnecting: "
                        "agentscope_session_id=%s reconnect_count=%d",
                        agentscope_session_id,
                        self.metrics.subscription_reconnects,
                    )
                    await asyncio.sleep(0.1)
        finally:
            self.metrics.worker_stops += 1
            if not ready.is_set():
                ready.set()
            logger.info(
                "AgentScope event bridge worker stopped: agentscope_session_id=%s",
                agentscope_session_id,
            )

    async def _publish(self, session_id: str, event: dict[str, Any]) -> None:
        if self._event_callback is None:
            return
        result = self._event_callback(session_id, event)
        if inspect.isawaitable(result):
            await result

    async def publish_records(self, session_id: str, records: list[Any]) -> None:
        for record in records:
            await self._publish(session_id, record.model_dump(mode="json"))


class AgentScopeWebSessionManager:
    def __init__(
        self,
        *,
        store: WebSessionStore,
        runtime: Any,
        event_callback: EventCallback | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._event_callback = event_callback
        self._forward_locks: dict[str, asyncio.Lock] = {}
        self._submit_locks: dict[str, asyncio.Lock] = {}
        set_store = getattr(self._runtime, "set_web_session_store", None)
        if callable(set_store):
            set_store(self._store)
        self.event_bridge = (
            AgentScopeEventBridge(
                store=store,
                runtime=runtime,
                event_callback=event_callback,
            )
            if callable(getattr(runtime, "subscribe_agentscope_session_event_batches", None))
            else None
        )
        set_bridge = getattr(self._runtime, "set_web_event_bridge", None)
        if callable(set_bridge):
            set_bridge(self.event_bridge)

    async def create_session(self, first_message: str) -> SessionRecord:
        return self._store.create_session(title=generate_session_title(first_message))

    async def submit_turn(
        self,
        session_id: str,
        message: str,
        invocation_id: str | None = None,
    ) -> TurnSubmission:
        submit_lock = self._submit_locks.setdefault(session_id, asyncio.Lock())
        async with submit_lock:
            submission = self._store.begin_user_turn(
                session_id,
                message,
                invocation_id=invocation_id,
            )
            if not submission.created:
                return submission
            try:
                submit_message = self._runtime.submit_user_message
                parameters = inspect.signature(submit_message).parameters
                if "turn_id" in parameters:
                    await submit_message(
                        web_session_id=session_id,
                        message=message,
                        turn_id=submission.turn.id,
                    )
                else:
                    await submit_message(web_session_id=session_id, message=message)
            except Exception:
                self._store.abort_initial_turn(submission.turn.id)
                raise
            for event in submission.events:
                await self._publish_record(session_id, event)
            return submission

    async def interrupt(self, session_id: str) -> bool:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        interrupt_web_session = getattr(self._runtime, "interrupt_web_session", None)
        if interrupt_web_session is None:
            return False
        interrupted = bool(await interrupt_web_session(web_session_id=session_id))
        if interrupted:
            for event in self._store.interrupt_active_turn(session_id):
                await self._publish_record(session_id, event)
        return interrupted

    async def submit_human_decision(self, session_id: str, decision: dict[str, Any]) -> bool:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        submit_decision = getattr(self._runtime, "submit_human_decision", None)
        if submit_decision is None:
            return False
        resumed = self._store.resume_active_turn(session_id)
        try:
            accepted = bool(await submit_decision(web_session_id=session_id, decision=decision))
        except Exception:
            self._store.restore_waiting_turn(session_id)
            raise
        if not accepted:
            self._store.restore_waiting_turn(session_id)
        if accepted:
            for event in resumed:
                await self._publish_record(session_id, event)
        return accepted

    async def _publish_record(self, session_id: str, record: Any) -> None:
        if self._event_callback is None:
            return
        result = self._event_callback(session_id, record.model_dump(mode="json"))
        if inspect.isawaitable(result):
            await result

    async def recover_human_decision_handoff(
        self,
        session_id: str,
        recovery: dict[str, Any],
    ) -> dict[str, Any]:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        recover = getattr(self._runtime, "recover_human_decision_handoff", None)
        if recover is None:
            raise RuntimeError("Human decision recovery is not supported")
        result = await recover(web_session_id=session_id, recovery=recovery)
        return dict(result)

    async def forward_events_until_idle(self, session_id: str) -> None:
        if self.event_bridge is not None:
            await self.event_bridge.ensure_web_session(session_id)
            return
        async with self._forward_lock(session_id):
            await self._forward_events_until_idle_unlocked(session_id)

    def _forward_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._forward_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._forward_locks[session_id] = lock
        return lock

    async def _forward_events_until_idle_unlocked(self, session_id: str) -> None:
        subscribe_events = getattr(self._runtime, "subscribe_web_session_events", None)
        if subscribe_events is None:
            return

        seen_subscription_keys: set[object] = set()
        while True:
            before_key = self._runtime_subscription_key(session_id)
            if before_key in seen_subscription_keys:
                return
            seen_subscription_keys.add(before_key)

            persisted_final_texts: set[str] = set()
            async for event in subscribe_events(web_session_id=session_id):
                self._store.append_timeline_event(session_id, event)
                if self._event_callback is not None:
                    callback_result = self._event_callback(session_id, event)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                text = _final_event_text(event)
                if text is not None and text not in persisted_final_texts:
                    self._store.append_message(session_id, role="assistant", content=text)
                    persisted_final_texts.add(text)

            after_key = self._runtime_subscription_key(session_id)
            if after_key == before_key:
                return

    def _runtime_subscription_key(self, session_id: str) -> object:
        subscription_key = getattr(self._runtime, "web_session_subscription_key", None)
        if not callable(subscription_key):
            return None
        return subscription_key(web_session_id=session_id)


def _final_event_text(event: dict[str, Any]) -> str | None:
    if event.get("type") != "final":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    return text if isinstance(text, str) and text else None
