from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import agentscope.app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import ChatModelConfig, RedisStorage, SessionConfig
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import TextBlock, ToolCallState, ToolResultBlock, ToolResultState, UserMsg
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from vla_data_juicer_agents.adapters.agentscope import AgentScopeEventAdapter
from vla_data_juicer_agents.core.cancellation import CancellationContext, bind_cancellation
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.navigation.agent_tools import build_navigation_agent_tools
from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig

_EVENT_STARTUP_GRACE_SECS = 1.0
_EVENT_IDLE_POLL_SECS = 0.03
_logger = logging.getLogger(__name__)


@dataclass
class AgentScopeRuntime:
    config: AgentScopeRuntimeConfig
    storage: Any
    message_bus: Any
    workspace_manager: Any
    app: Any
    web_sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    event_cursors: dict[str, str | None] = field(default_factory=dict)
    web_session_store: Any | None = None
    _active_human_decision_claims: set[str] = field(default_factory=set)
    _run_cancellations: dict[str, CancellationContext] = field(default_factory=dict)
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

    def set_web_session_store(self, store: Any) -> None:
        self.web_session_store = store

    def web_session_subscription_key(self, *, web_session_id: str) -> tuple[str, str] | None:
        return self._web_session_mapping(web_session_id)

    async def ensure_web_session(self, web_session_id: str, *, agent_id: str, model: str) -> str:
        existing = self.web_sessions.get(web_session_id)
        if existing and existing[0] == agent_id:
            return existing[1]

        persisted = self._load_web_session_mapping(web_session_id, agent_id=agent_id)
        if persisted and persisted[0] == agent_id:
            self.web_sessions[web_session_id] = persisted
            self._save_web_session_mapping(web_session_id, agent_id, persisted[1])
            return persisted[1]

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
        self._save_web_session_mapping(web_session_id, agent_id, session.id)
        return session.id

    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        await self.ensure_bootstrapped()

        agent_id = self.config.main_router_agent_id
        model = self.config.router_model
        await self._start_agent_run(
            web_session_id=web_session_id,
            agent_id=agent_id,
            model=model,
            message=message,
        )
        return f"turn_{uuid4()}"

    async def start_navigation_agent_task(self, *, web_session_id: str, message: str) -> str:
        await self.ensure_bootstrapped()

        session_id = await self._start_agent_run(
            web_session_id=web_session_id,
            agent_id=self.config.navigation_agent_id,
            model=self.config.navigation_model,
            message=message,
        )
        return session_id

    async def _start_agent_run(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        model: str,
        message: str,
    ) -> str:
        chat_service = self.app.state.chat_service
        session_id = await self.ensure_web_session(
            web_session_id,
            agent_id=agent_id,
            model=model,
        )
        tail_cursor = await self._event_log_tail_cursor(session_id)
        cancellation = CancellationContext()
        previous_cancellation = self.run_cancellation(session_id)
        self.register_run_cancellation(session_id, cancellation)

        async def run_with_cancellation() -> None:
            try:
                async with cancellation.track_agent(session_id):
                    with bind_cancellation(cancellation):
                        await chat_service.run(
                            user_id=self.config.user_id,
                            session_id=session_id,
                            agent_id=agent_id,
                            input_msg=UserMsg(name="user", content=message),
                        )
            finally:
                self.clear_run_cancellation(session_id, cancellation)

        try:
            self._spawn_chat_run(run_with_cancellation(), session_id=session_id)
        except Exception:
            if previous_cancellation is not None:
                self.register_run_cancellation(session_id, previous_cancellation)
            else:
                self.clear_run_cancellation(session_id, cancellation)
            raise
        if tail_cursor is not None:
            self._remember_event_cursor(session_id, tail_cursor)
        return session_id

    async def interrupt_web_session(self, *, web_session_id: str) -> bool:
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            return False

        _agent_id, agentscope_session_id = mapped
        interrupted = False
        cancellation = self.run_cancellation(agentscope_session_id)
        if cancellation is not None:
            interrupted = cancellation.cancel() or interrupted

        publish_cancel = getattr(self.message_bus, "session_publish_cancel", None)
        if callable(publish_cancel):
            await publish_cancel(agentscope_session_id)
            interrupted = True
        return interrupted

    def register_run_cancellation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> None:
        self._run_cancellations[agentscope_session_id] = cancellation

    def run_cancellation(self, agentscope_session_id: str) -> CancellationContext | None:
        return self._run_cancellations.get(agentscope_session_id)

    def clear_run_cancellation(
        self,
        agentscope_session_id: str,
        cancellation: CancellationContext,
    ) -> None:
        if self._run_cancellations.get(agentscope_session_id) is cancellation:
            self._run_cancellations.pop(agentscope_session_id, None)

    def record_navigation_handoff(self, payload: dict[str, Any]) -> None:
        _logger.info("Navigation handoff: %s", payload)

    async def submit_human_decision(self, *, web_session_id: str, decision: dict[str, Any]) -> bool:
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            return False

        agent_id, agentscope_session_id = mapped
        claim_key = _human_decision_claim_key(agentscope_session_id, decision)
        claim = await self._try_acquire_human_decision_claim(claim_key)
        if claim is None:
            return False

        claim_handoff = False
        try:
            if not await self._has_pending_human_decision(
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
                decision=decision,
            ):
                return False

            result = ToolResultBlock(
                id=decision["tool_call_id"],
                name="request_human_decision",
                output=json.dumps(
                    {
                        "action": decision["action"],
                        "text": decision.get("text"),
                        "request_id": decision["request_id"],
                    },
                    ensure_ascii=False,
                ),
                state=ToolResultState.SUCCESS,
            )
            input_msg = ExternalExecutionResultEvent(
                reply_id=decision["reply_id"],
                execution_results=[result],
            )
            cancellation = CancellationContext()
            previous_cancellation = self.run_cancellation(agentscope_session_id)
            self.register_run_cancellation(agentscope_session_id, cancellation)

            async def run_with_claim() -> None:
                try:
                    async with cancellation.track_agent(agentscope_session_id):
                        with bind_cancellation(cancellation):
                            await self.app.state.chat_service.run(
                                user_id=self.config.user_id,
                                session_id=agentscope_session_id,
                                agent_id=agent_id,
                                input_msg=input_msg,
                            )
                finally:
                    self.clear_run_cancellation(agentscope_session_id, cancellation)
                    await claim.release()

            run_coroutine = run_with_claim()
            try:
                self._spawn_chat_run(run_coroutine, session_id=agentscope_session_id)
            except Exception:
                if previous_cancellation is not None:
                    self.register_run_cancellation(
                        agentscope_session_id,
                        previous_cancellation,
                    )
                else:
                    self.clear_run_cancellation(agentscope_session_id, cancellation)
                raise
            claim_handoff = True
            return True
        finally:
            if not claim_handoff:
                await claim.release()

    async def _has_pending_human_decision(
        self,
        *,
        agent_id: str,
        agentscope_session_id: str,
        decision: dict[str, Any],
    ) -> bool:
        get_session = getattr(self.storage, "get_session", None)
        if get_session is None:
            return False

        record = await get_session(self.config.user_id, agent_id, agentscope_session_id)
        if record is None:
            return False

        state = getattr(record, "state", None)
        if getattr(state, "reply_id", None) != decision["reply_id"]:
            return False

        for message in getattr(state, "context", []) or []:
            for tool_call in _tool_call_blocks(message):
                if (
                    getattr(tool_call, "id", None) == decision["tool_call_id"]
                    and getattr(tool_call, "name", None) == "request_human_decision"
                    and _state_value(getattr(tool_call, "state", None))
                    == ToolCallState.SUBMITTED.value
                ):
                    return True
        return False

    async def _try_acquire_human_decision_claim(
        self,
        claim_key: str,
    ) -> "_HumanDecisionClaim | None":
        acquire_lock = getattr(self.message_bus, "acquire_lock", None)
        if callable(acquire_lock):
            lock_cm = acquire_lock(claim_key, ttl_secs=600)
            try:
                await asyncio.wait_for(lock_cm.__aenter__(), timeout=0.1)
            except TimeoutError:
                return None

            async def release_distributed() -> None:
                await lock_cm.__aexit__(None, None, None)

            return _HumanDecisionClaim(release_distributed)

        if claim_key in self._active_human_decision_claims:
            return None
        self._active_human_decision_claims.add(claim_key)

        async def release_local() -> None:
            self._active_human_decision_claims.discard(claim_key)

        return _HumanDecisionClaim(release_local)

    def _spawn_chat_run(self, run_coroutine: Any, *, session_id: str) -> None:
        chat_run_registry = getattr(self.app.state, "chat_run_registry", None)
        if chat_run_registry is None:
            run_coroutine.close()
            raise RuntimeError("AgentScope chat_run_registry is not initialized")
        try:
            chat_run_registry.spawn(run_coroutine, session_id=session_id)
        except Exception:
            run_coroutine.close()
            raise

    async def _event_log_tail_cursor(self, agentscope_session_id: str) -> str | None:
        read_events = getattr(self.message_bus, "session_read_events", None)
        if read_events is None:
            return None
        entries = await read_events(
            agentscope_session_id,
            since=self._event_cursor(agentscope_session_id),
        )
        if not entries:
            return None
        return entries[-1][0]

    async def subscribe_web_session_events(self, *, web_session_id: str):
        mapped = self._web_session_mapping(web_session_id)
        if mapped is None:
            return

        agent_id, agentscope_session_id = mapped
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

            pending_event = await self._pending_human_decision_event(
                agent_id=agent_id,
                agentscope_session_id=agentscope_session_id,
            )
            if pending_event is not None:
                yield pending_event

            cursor = self._event_cursor(agentscope_session_id)
            for entry_id, raw_event in await self.message_bus.session_read_events(
                agentscope_session_id,
                since=cursor,
            ):
                if not self._is_new_event(agentscope_session_id, entry_id):
                    continue
                seen_entry_ids.add(entry_id)
                saw_event = True
                if _raw_event_type(raw_event) == "REPLY_END":
                    saw_reply_end = True
                for event in accept_raw_event(raw_event):
                    yield event
                self._remember_event_cursor(
                    agentscope_session_id,
                    entry_id,
                )

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
                        if not self._is_new_event(agentscope_session_id, entry_id):
                            continue
                        seen_entry_ids.add(entry_id)
                    saw_event = True
                    if _raw_event_type(raw_event) == "REPLY_END":
                        saw_reply_end = True
                    for event in accept_raw_event(raw_event):
                        yield event
                    if entry_id:
                        self._remember_event_cursor(
                            agentscope_session_id,
                            entry_id,
                        )
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

    def _is_new_event(self, agentscope_session_id: str, entry_id: str) -> bool:
        cursor = self._event_cursor(agentscope_session_id)
        return cursor is None or _stream_id_is_newer(entry_id, cursor)

    def _remember_event_cursor(
        self,
        agentscope_session_id: str,
        entry_id: str,
    ) -> None:
        if self._is_new_event(agentscope_session_id, entry_id):
            self.event_cursors[agentscope_session_id] = entry_id
            self._save_web_session_event_cursor(agentscope_session_id, entry_id)

    def _event_cursor(self, agentscope_session_id: str) -> str | None:
        cursor = self.event_cursors.get(agentscope_session_id)
        if cursor is not None:
            return cursor
        mapping = self._web_session_mapping_for_agentscope_session(agentscope_session_id)
        if mapping is None:
            return None
        _agent_id, _session_id, persisted_cursor = mapping
        if persisted_cursor:
            self.event_cursors[agentscope_session_id] = persisted_cursor
        return persisted_cursor

    def _web_session_mapping(self, web_session_id: str) -> tuple[str, str] | None:
        mapped = self.web_sessions.get(web_session_id)
        if mapped is not None:
            return mapped
        persisted = self._load_web_session_mapping(web_session_id)
        if persisted is not None:
            self.web_sessions[web_session_id] = persisted
        return persisted

    def _load_web_session_mapping(
        self,
        web_session_id: str,
        *,
        agent_id: str | None = None,
    ) -> tuple[str, str] | None:
        if self.web_session_store is None:
            return None
        if agent_id is not None:
            get_mapping = getattr(
                self.web_session_store,
                "get_agentscope_session_mapping_for_agent",
                None,
            )
            if callable(get_mapping):
                mapping = get_mapping(web_session_id, agent_id)
                if mapping is None:
                    return None
                return mapping.agent_id, mapping.agentscope_session_id
        get_mapping = getattr(self.web_session_store, "get_agentscope_session_mapping", None)
        if not callable(get_mapping):
            return None
        mapping = get_mapping(web_session_id)
        if mapping is None:
            return None
        return mapping.agent_id, mapping.agentscope_session_id

    def _web_session_mapping_for_agentscope_session(
        self,
        agentscope_session_id: str,
    ) -> tuple[str, str, str | None] | None:
        if self.web_session_store is None:
            return None
        get_mapping = getattr(
            self.web_session_store,
            "get_agentscope_session_mapping_by_agentscope_session",
            None,
        )
        if not callable(get_mapping):
            return None
        mapping = get_mapping(agentscope_session_id)
        if mapping is None:
            return None
        return mapping.agent_id, mapping.agentscope_session_id, mapping.event_cursor

    def _save_web_session_mapping(
        self,
        web_session_id: str,
        agent_id: str,
        agentscope_session_id: str,
    ) -> None:
        if self.web_session_store is None:
            return
        save_mapping = getattr(self.web_session_store, "save_agentscope_session_mapping", None)
        if callable(save_mapping):
            save_mapping(web_session_id, agent_id=agent_id, agentscope_session_id=agentscope_session_id)

    def _save_web_session_event_cursor(self, agentscope_session_id: str, cursor: str) -> None:
        if self.web_session_store is None:
            return
        save_cursor = getattr(self.web_session_store, "save_agentscope_event_cursor", None)
        if callable(save_cursor):
            save_cursor(agentscope_session_id, cursor)

    async def _pending_human_decision_event(
        self,
        *,
        agent_id: str,
        agentscope_session_id: str,
    ) -> dict[str, Any] | None:
        get_session = getattr(self.storage, "get_session", None)
        if get_session is None:
            return None
        record = await get_session(self.config.user_id, agent_id, agentscope_session_id)
        if record is None:
            return None
        state = getattr(record, "state", None)
        reply_id = getattr(state, "reply_id", None)
        if not reply_id:
            return None
        for message in getattr(state, "context", []) or []:
            for tool_call in _tool_call_blocks(message):
                if (
                    getattr(tool_call, "name", None) == "request_human_decision"
                    and _state_value(getattr(tool_call, "state", None))
                    == ToolCallState.SUBMITTED.value
                ):
                    payload = _human_decision_payload_from_tool_call(tool_call)
                    if payload is None:
                        continue
                    claim_key = _human_decision_claim_key(
                        agentscope_session_id,
                        {
                            "reply_id": reply_id,
                            "tool_call_id": getattr(tool_call, "id", ""),
                        },
                    )
                    if await self._is_human_decision_claim_active(claim_key):
                        continue
                    payload["reply_id"] = reply_id
                    payload["tool_call_id"] = getattr(tool_call, "id", "")
                    return {
                        "type": "human_decision_required",
                        "source": "NavigationDataAgent",
                        "run_id": agentscope_session_id,
                        "parent_run_id": None,
                        "payload": payload,
                    }
        return None

    async def _is_human_decision_claim_active(self, claim_key: str) -> bool:
        if claim_key in self._active_human_decision_claims:
            return True
        is_locked = getattr(self.message_bus, "is_locked", None)
        if callable(is_locked):
            return bool(await is_locked(claim_key))
        return False


def _to_attribute_event(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _to_attribute_event(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_to_attribute_event(item) for item in value]
    return value


class _HumanDecisionClaim:
    def __init__(self, release_callback: Any) -> None:
        self._release_callback = release_callback
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._release_callback()


def _tool_call_blocks(message: Any) -> list[Any]:
    get_content_blocks = getattr(message, "get_content_blocks", None)
    if callable(get_content_blocks):
        return list(get_content_blocks("tool_call"))
    return [
        block
        for block in getattr(message, "content", []) or []
        if getattr(block, "type", None) == "tool_call"
    ]


def _state_value(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value)


def _human_decision_claim_key(agentscope_session_id: str, decision: dict[str, Any]) -> str:
    return (
        "vla:human-decision:"
        f"{agentscope_session_id}:{decision['reply_id']}:{decision['tool_call_id']}"
    )


def _human_decision_payload_from_tool_call(tool_call: Any) -> dict[str, Any] | None:
    tool_input = getattr(tool_call, "input", None)
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            return None
    if not isinstance(tool_input, dict):
        return None

    request_id = tool_input.get("request_id")
    summary = tool_input.get("summary")
    if not isinstance(request_id, str) or not isinstance(summary, str):
        return None
    decision_type = tool_input.get("decision_type")
    if not isinstance(decision_type, str) or not decision_type:
        decision_type = "other"
    return {
        "request_id": request_id,
        "decision_type": decision_type,
        "summary": summary,
    }


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


def _stream_id_is_newer(entry_id: str, cursor: str) -> bool:
    entry_parts = _stream_id_parts(entry_id)
    cursor_parts = _stream_id_parts(cursor)
    if entry_parts is None or cursor_parts is None:
        return entry_id > cursor
    return entry_parts > cursor_parts


def _stream_id_parts(entry_id: str) -> tuple[int, int] | None:
    try:
        first, second = entry_id.split("-", 1)
        return int(first), int(second)
    except ValueError:
        return None


def _web_session_id_from_agentscope_session(session_id: str, *, agent_id: str) -> str:
    suffix = f"__{agent_id}"
    if session_id.endswith(suffix):
        return session_id[: -len(suffix)]
    return session_id


def _handoff_error(message: str, payload: dict[str, Any]) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
        metadata=payload,
    )


def _navigation_handoff_message(
    *,
    request: str,
    target: str,
    scene_mode: str,
    clips: list[str],
    reason: str,
) -> str:
    clip_text = ", ".join(clips) if clips else "all"
    return "\n".join(
        [
            "Navigation data processing request:",
            f"- request: {request}",
            f"- target: {target}",
            f"- scene_mode: {scene_mode}",
            f"- clips: {clip_text}",
            f"- reason: {reason}",
        ]
    )


class NavigationHandoffTool(ToolBase):
    name = "start_navigation_data_task"
    description = (
        "Start the dedicated VLA navigation data processing agent after you "
        "have determined that the user wants to begin a concrete navigation "
        "data task. Do not use this for capability questions or ordinary "
        "conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "The user's concrete navigation task request with relevant context.",
            },
            "target": {
                "type": "string",
                "description": "The concrete date, path, or dataset target to process.",
            },
            "scene_mode": {
                "type": "string",
                "enum": ["indoor", "outdoor", "unknown"],
                "description": (
                    "Whether the navigation data is indoor or outdoor. Use unknown only "
                    "with missing_fields when scene mode is not available."
                ),
            },
            "clips": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific clips to process. Use an empty array when the user did not specify clips.",
            },
            "reason": {
                "type": "string",
                "description": "Brief reason why this should be handed off to the navigation data agent.",
            },
            "missing_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["request", "target", "scene_mode", "clips", "other"],
                },
                "description": "Fields that are still missing. Must be empty before starting processing.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Confidence that this is a concrete navigation data processing task.",
            },
        },
        "required": [
            "request",
            "target",
            "scene_mode",
            "reason",
            "missing_fields",
            "confidence",
        ],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False

    def __init__(self, *, runtime: AgentScopeRuntime, web_session_id: str) -> None:
        self._runtime = runtime
        self._web_session_id = web_session_id

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Navigation handoff is allowed.",
        )

    async def __call__(
        self,
        request: str,
        target: str,
        scene_mode: str,
        reason: str,
        missing_fields: list[str],
        confidence: str,
        clips: list[str] | None = None,
    ) -> ToolChunk:
        normalized_clips = list(clips or [])
        payload = {
            "web_session_id": self._web_session_id,
            "request": request,
            "target": target,
            "scene_mode": scene_mode,
            "clips": normalized_clips,
            "reason": reason,
            "missing_fields": list(missing_fields),
            "confidence": confidence,
            "started": False,
        }

        if confidence not in {"medium", "high"}:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because confidence must be medium or high.",
                payload,
            )
        if missing_fields:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because missing_fields is not empty.",
                payload,
            )
        if not target.strip():
            self._record_handoff(payload)
            return _handoff_error("Navigation handoff rejected because target is missing.", payload)
        if scene_mode not in {"indoor", "outdoor"}:
            self._record_handoff(payload)
            return _handoff_error(
                "Navigation handoff rejected because scene_mode must be indoor or outdoor.",
                payload,
            )

        navigation_request = _navigation_handoff_message(
            request=request,
            target=target,
            scene_mode=scene_mode,
            clips=normalized_clips,
            reason=reason,
        )
        await self._runtime.start_navigation_agent_task(
            web_session_id=self._web_session_id,
            message=navigation_request,
        )
        payload["started"] = True
        self._record_handoff(payload)
        return ToolChunk(
            content=[
                TextBlock(
                    text="Navigation data task started.",
                ),
            ],
            state=ToolResultState.SUCCESS,
            metadata=payload,
        )

    def _record_handoff(self, payload: dict[str, Any]) -> None:
        record = getattr(self._runtime, "record_navigation_handoff", None)
        if callable(record):
            record(payload)


def build_extra_agent_tools_factory(
    config: AgentScopeRuntimeConfig,
    *,
    runtime: AgentScopeRuntime | None = None,
):
    async def extra_agent_tools(_user_id: str, agent_id: str, _session_id: str) -> list[Any]:
        if agent_id == config.navigation_agent_id:
            cancellation = (
                runtime.run_cancellation(_session_id)
                if runtime is not None and hasattr(runtime, "run_cancellation")
                else None
            )
            return build_navigation_agent_tools(
                dry_run=False,
                cancellation=cancellation,
            )
        if agent_id == config.main_router_agent_id and runtime is not None:
            web_session_id = _web_session_id_from_agentscope_session(
                _session_id,
                agent_id=config.main_router_agent_id,
            )
            return [
                NavigationHandoffTool(
                    runtime=runtime,
                    web_session_id=web_session_id,
                )
            ]
        return []

    return extra_agent_tools


def create_agentscope_runtime(config: AgentScopeRuntimeConfig) -> AgentScopeRuntime:
    redis_kwargs = config.redis_connection_kwargs()
    storage = RedisStorage(**redis_kwargs)
    message_bus = RedisMessageBus(**redis_kwargs)
    workspace_manager = LocalWorkspaceManager(
        basedir=str(config.workspace_root / "agentscope-workspaces"),
    )
    runtime_holder: dict[str, AgentScopeRuntime] = {}

    async def extra_agent_tools(user_id: str, agent_id: str, session_id: str) -> list[Any]:
        runtime = runtime_holder.get("runtime")
        return await build_extra_agent_tools_factory(config, runtime=runtime)(
            user_id,
            agent_id,
            session_id,
        )

    app = agentscope.app.create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_tools=extra_agent_tools,
        title="DataPilot AgentScope Runtime",
    )

    runtime = AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        app=app,
    )
    runtime_holder["runtime"] = runtime
    return runtime
