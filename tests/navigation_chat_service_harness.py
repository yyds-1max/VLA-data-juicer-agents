from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from agentscope.app._manager import ChatRunRegistry
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import SessionConfig, SessionRecord
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit


class ChatServiceStorage:
    """Small in-memory implementation of the ChatService storage protocol."""

    def __init__(self) -> None:
        self.agents: dict[str, Any] = {}
        self.sessions: dict[tuple[str, str, str], SessionRecord] = {}
        self.messages: dict[tuple[str, str, str], Any] = {}
        self.updated_state: AgentState | None = None

    async def upsert_credential(self, _user_id: str, credential: Any) -> str:
        return credential.id

    async def upsert_agent(self, _user_id: str, record: Any) -> str:
        self.agents[record.id] = record
        return record.id

    async def get_agent(self, _user_id: str, agent_id: str) -> Any | None:
        return self.agents.get(agent_id)

    async def upsert_session(
        self,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        *,
        session_id: str | None = None,
    ) -> SessionRecord:
        record = SessionRecord(
            id=session_id or "chat-service-session",
            user_id=user_id,
            agent_id=agent_id,
            config=config,
            state=AgentState(),
        )
        self.sessions[(user_id, agent_id, record.id)] = record
        return record

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> SessionRecord | None:
        record = self.sessions.get((user_id, agent_id, session_id))
        if record is None:
            return None
        state = AgentState.model_validate(record.state.model_dump(mode="json"))
        return record.model_copy(update={"state": state})

    async def upsert_message(self, user_id: str, session_id: str, message: Any) -> str:
        self.messages[(user_id, session_id, message.id)] = message
        return message.id

    async def get_message(
        self,
        user_id: str,
        session_id: str,
        message_id: str,
    ) -> Any | None:
        return self.messages.get((user_id, session_id, message_id))

    async def update_session_state(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        key = (user_id, agent_id, session_id)
        persisted = AgentState.model_validate(state.model_dump(mode="json"))
        self.sessions[key] = self.sessions[key].model_copy(
            update={"state": persisted},
        )
        self.updated_state = AgentState.model_validate(
            persisted.model_dump(mode="json"),
        )


class ChatServiceWorkspaceManager:
    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str,
    ) -> Any:
        async def offload_context(_session_id: str, *, msgs: list[Any]) -> str:
            del msgs
            return "/tmp/chat-service-context-offload.json"

        return SimpleNamespace(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            workspace_id=workspace_id,
            workdir=f"/tmp/chat-service-workspace/{workspace_id}",
            offload_context=offload_context,
        )


class ChatServiceBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def session_run(self, _session_id: str):
        yield

    @asynccontextmanager
    async def acquire_lock(self, _key: str, *, ttl_secs: int):
        del ttl_secs
        yield

    async def log_append(
        self,
        _key: str,
        event: dict[str, Any],
        *,
        max_len: int,
    ) -> str:
        del max_len
        self.events.append(event)
        return str(len(self.events))

    async def publish(self, _key: str, _payload: dict[str, Any]) -> None:
        pass

    async def log_trim(self, _key: str) -> None:
        pass

    async def session_publish_event(
        self,
        _session_id: str,
        event: dict[str, Any],
    ) -> None:
        self.events.append(event)

    async def inbox_drain(
        self,
        _session_id: str,
        *,
        max_count: int,
    ) -> list[Any]:
        del max_count
        return []

    async def queue_drain(
        self,
        _key: str,
        *,
        max_count: int,
    ) -> list[Any]:
        del max_count
        return []


class DeterministicMessageBus:
    """In-memory Redis/message-bus contract used by real AgentScope services."""

    def __init__(self) -> None:
        self._queues: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._logs: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._registries: dict[str, dict[str, str]] = defaultdict(dict)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._entry_sequence = 0
        self.operations: list[tuple[str, str, dict[str, Any] | None]] = []
        self.background_registered = asyncio.Event()
        self.wakeup_enqueued = asyncio.Event()
        self.on_wakeup_enqueued = None

    def _next_entry_id(self) -> str:
        self._entry_sequence += 1
        return f"{self._entry_sequence}-0"

    @asynccontextmanager
    async def acquire_lock(self, key: str, *, ttl_secs: int = 600):
        del ttl_secs
        lock = self._locks[key]
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    async def is_locked(self, key: str) -> bool:
        return self._locks[key].locked()

    @asynccontextmanager
    async def session_run(self, session_id: str):
        async with self.acquire_lock(MessageBusKeys.session_lock(session_id)):
            yield

    async def session_is_running(self, session_id: str) -> bool:
        return await self.is_locked(MessageBusKeys.session_lock(session_id))

    async def log_append(self, key: str, event: dict[str, Any], *, max_len: int) -> str:
        entry_id = self._next_entry_id()
        self._logs[key].append((entry_id, dict(event)))
        if len(self._logs[key]) > max_len:
            self._logs[key] = self._logs[key][-max_len:]
        self.operations.append(("log_append", key, dict(event)))
        return entry_id

    async def log_trim(self, key: str) -> None:
        self.operations.append(("log_trim", key, None))

    async def publish(self, key: str, payload: dict[str, Any]) -> None:
        copied = dict(payload)
        self.operations.append(("publish", key, copied))
        for queue in list(self._subscribers.get(key, ())):
            await queue.put(dict(copied))

    async def subscribe(self, key: str, *, on_ready=None):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[key].add(queue)
        if on_ready is not None:
            on_ready()
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(key, None)

    async def queue_push(self, key: str, payload: dict[str, Any]) -> str:
        entry_id = self._next_entry_id()
        copied = dict(payload)
        self._queues[key].append((entry_id, copied))
        self.operations.append(("queue_push", key, copied))
        if key == MessageBusKeys.wakeup_queue():
            callback = self.on_wakeup_enqueued
            if callback is not None:
                callback(copied)
            self.wakeup_enqueued.set()
        return entry_id

    async def queue_drain(self, key: str, *, max_count: int) -> list[tuple[str, dict[str, Any]]]:
        drained = self._queues[key][:max_count]
        self._queues[key] = self._queues[key][max_count:]
        self.operations.append(("queue_drain", key, {"count": len(drained)}))
        return [(entry_id, dict(payload)) for entry_id, payload in drained]

    async def dequeue_wakeups(self, max_count: int = 64) -> list[dict[str, Any]]:
        entries = await self.queue_drain(
            MessageBusKeys.wakeup_queue(),
            max_count=max_count,
        )
        return [payload for _entry_id, payload in entries]

    async def registry_set(
        self,
        namespace: str,
        field: str,
        value: str,
        *,
        ttl_secs: int | None = None,
    ) -> None:
        del ttl_secs
        self._registries[namespace][field] = value
        self.operations.append(("registry_set", namespace, {"field": field}))
        if namespace.startswith("agentscope:bg_tasks:"):
            self.background_registered.set()

    async def registry_del(self, namespace: str, field: str) -> None:
        self._registries[namespace].pop(field, None)
        self.operations.append(("registry_del", namespace, {"field": field}))

    async def registry_getall(self, namespace: str) -> dict[str, str]:
        return dict(self._registries.get(namespace, {}))

    async def registry_exists(self, namespace: str, field: str) -> bool:
        return field in self._registries.get(namespace, {})


class RecordingChatRunRegistry(ChatRunRegistry):
    """Real AgentScope registry with deterministic completion barriers."""

    def __init__(self) -> None:
        super().__init__()
        self.spawned_tasks: list[asyncio.Task] = []
        self.completed_tasks: list[asyncio.Task] = []
        self._completion_changed = asyncio.Event()

    def spawn(self, coro, *, session_id: str, name: str | None = None):
        task = super().spawn(coro, session_id=session_id, name=name)
        self.spawned_tasks.append(task)

        def completed(done: asyncio.Task) -> None:
            self.completed_tasks.append(done)
            self._completion_changed.set()

        task.add_done_callback(completed)
        return task

    async def wait_for_completions(self, count: int, *, timeout: float = 2.0) -> None:
        async def wait() -> None:
            while len(self.completed_tasks) < count:
                self._completion_changed.clear()
                if len(self.completed_tasks) < count:
                    await self._completion_changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)


class InertManager:
    """Marker dependency unused because the AgentScope assembly seam is patched."""


def generic_toolkit() -> Toolkit:
    def ok() -> dict[str, bool]:
        return {"ok": True}

    return Toolkit(
        tools=[
            FunctionTool(ok, name="bash", is_read_only=True),
            FunctionTool(ok, name="read", is_read_only=True),
            FunctionTool(ok, name="task", is_read_only=True),
        ],
    )
