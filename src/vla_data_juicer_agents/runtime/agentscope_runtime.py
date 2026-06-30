from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import agentscope.app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import ChatModelConfig, RedisStorage, SessionConfig
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.message import UserMsg

from vla_data_juicer_agents.adapters.agentscope import AgentScopeEventAdapter
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.navigation.routing import is_high_confidence_navigation_request
from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig

_EVENT_STARTUP_GRACE_SECS = 1.0
_EVENT_IDLE_POLL_SECS = 0.03


@dataclass
class AgentScopeRuntime:
    config: AgentScopeRuntimeConfig
    storage: Any
    message_bus: Any
    workspace_manager: Any
    app: Any
    web_sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    bootstrapped: bool = False

    def __post_init__(self) -> None:
        self._bootstrap_lock = asyncio.Lock()

    async def ensure_bootstrapped(self) -> None:
        if self.bootstrapped:
            return

        async with self._bootstrap_lock:
            if self.bootstrapped:
                return
            await bootstrap_agentscope_records(self.storage, self.config)
            self.bootstrapped = True

    async def ensure_web_session(self, web_session_id: str, *, agent_id: str, model: str) -> str:
        existing = self.web_sessions.get(web_session_id)
        if existing and existing[0] == agent_id:
            return existing[1]

        session_id = f"{web_session_id}__{agent_id}"
        session = await self.storage.upsert_session(
            self.config.user_id,
            agent_id,
            config=SessionConfig(
                workspace_id=f"workspace-{web_session_id}",
                name=web_session_id,
                chat_model_config=ChatModelConfig(
                    type="dashscope_chat",
                    credential_id=self.config.credential_id,
                    model=model,
                    parameters={"parallel_tool_calls": False},
                ),
            ),
            session_id=session_id,
        )
        self.web_sessions[web_session_id] = (agent_id, session.id)
        return session.id

    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        await self.ensure_bootstrapped()

        chat_service = self.app.state.chat_service
        chat_run_registry = getattr(self.app.state, "chat_run_registry", None)
        if chat_run_registry is None:
            raise RuntimeError("AgentScope chat_run_registry is not initialized")

        if is_high_confidence_navigation_request(message):
            agent_id = self.config.navigation_agent_id
            model = self.config.navigation_model
        else:
            agent_id = self.config.main_router_agent_id
            model = self.config.router_model

        session_id = await self.ensure_web_session(
            web_session_id,
            agent_id=agent_id,
            model=model,
        )
        run_coroutine = chat_service.run(
            user_id=self.config.user_id,
            session_id=session_id,
            agent_id=agent_id,
            input_msg=UserMsg(name="user", content=message),
        )
        try:
            chat_run_registry.spawn(run_coroutine, session_id=session_id)
        except Exception:
            run_coroutine.close()
            raise
        return f"turn_{uuid4()}"

    async def subscribe_web_session_events(self, *, web_session_id: str):
        mapped = self.web_sessions.get(web_session_id)
        if mapped is None:
            return

        _agent_id, agentscope_session_id = mapped
        translated_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        live_events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        live_ready = asyncio.Event()
        scope = EventEmitter(CallbackEventSink(translated_events.put_nowait)).scope(
            "agentscope",
            run_id=agentscope_session_id,
        )
        adapter = AgentScopeEventAdapter(
            scope,
            emit_text_events=True,
            emit_final_events=True,
        )
        seen_entry_ids: set[str] = set()
        saw_event = False
        saw_reply_end = False
        saw_running = False
        startup_deadline = asyncio.get_running_loop().time() + _EVENT_STARTUP_GRACE_SECS
        events_key = _session_events_key(self.message_bus, agentscope_session_id)

        async def feed_live_events() -> None:
            try:
                async for event in self.message_bus.subscribe(
                    events_key,
                    on_ready=live_ready.set,
                ):
                    await live_events.put(event)
            finally:
                await live_events.put(None)

        feeder_task = asyncio.create_task(
            feed_live_events(),
            name=f"agentscope-web-events:{agentscope_session_id}",
        )

        def accept_raw_event(raw_event: dict[str, Any]) -> list[dict[str, Any]]:
            adapter.accept(_to_attribute_event(_strip_internal_event_fields(raw_event)))
            events = []
            while not translated_events.empty():
                events.append(translated_events.get_nowait())
            return events

        try:
            with suppress(TimeoutError):
                await asyncio.wait_for(live_ready.wait(), timeout=_EVENT_STARTUP_GRACE_SECS)

            for entry_id, raw_event in await self.message_bus.session_read_events(
                agentscope_session_id,
            ):
                seen_entry_ids.add(entry_id)
                saw_event = True
                if _raw_event_type(raw_event) == "REPLY_END":
                    saw_reply_end = True
                for event in accept_raw_event(raw_event):
                    yield event

            while True:
                running = bool(await self.message_bus.session_is_running(agentscope_session_id))
                saw_running = saw_running or running

                try:
                    raw_event = await asyncio.wait_for(
                        live_events.get(),
                        timeout=_EVENT_IDLE_POLL_SECS,
                    )
                except TimeoutError:
                    raw_event = None
                else:
                    if raw_event is None:
                        break
                    entry_id = _raw_event_entry_id(raw_event)
                    if entry_id and entry_id in seen_entry_ids:
                        continue
                    if entry_id:
                        seen_entry_ids.add(entry_id)
                    saw_event = True
                    if _raw_event_type(raw_event) == "REPLY_END":
                        saw_reply_end = True
                    for event in accept_raw_event(raw_event):
                        yield event
                    continue

                now = asyncio.get_running_loop().time()
                if running:
                    continue
                if saw_reply_end:
                    break
                if saw_event and saw_running:
                    break
                if now >= startup_deadline:
                    break
        finally:
            feeder_task.cancel()
            with suppress(asyncio.CancelledError):
                await feeder_task


def _to_attribute_event(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _to_attribute_event(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_to_attribute_event(item) for item in value]
    return value


def _session_events_key(message_bus: Any, session_id: str) -> str:
    template = getattr(
        message_bus,
        "_SESSION_EVENTS_KEY",
        "agentscope:session:events:{sid}",
    )
    return str(template).format(sid=session_id)


def _strip_internal_event_fields(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "_entry_id"}


def _raw_event_entry_id(event: dict[str, Any]) -> str | None:
    entry_id = event.get("_entry_id")
    return entry_id if isinstance(entry_id, str) and entry_id else None


def _raw_event_type(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    return str(event_type) if event_type is not None else ""


def create_agentscope_runtime(config: AgentScopeRuntimeConfig) -> AgentScopeRuntime:
    redis_kwargs = config.redis_connection_kwargs()
    storage = RedisStorage(**redis_kwargs)
    message_bus = RedisMessageBus(**redis_kwargs)
    workspace_manager = LocalWorkspaceManager(
        basedir=str(config.workspace_root / "agentscope-workspaces"),
    )
    app = agentscope.app.create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_tools=None,
        title="DataPilot AgentScope Runtime",
    )

    return AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        app=app,
    )
