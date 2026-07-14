from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.app._manager import (
    BackgroundTaskManager,
    CancelDispatcher,
    WakeupDispatcher,
)
from agentscope.app._service._chat import ChatService
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit

import agentscope.app._service._chat as chat_service_module
from navigation_agentscope_harness import ScriptedChatModel, runtime_config
from navigation_chat_service_harness import (
    ChatServiceStorage,
    ChatServiceWorkspaceManager,
    DeterministicMessageBus,
    InertManager,
    RecordingChatRunRegistry,
)
from vla_data_juicer_agents.runtime.agentscope_bootstrap import (
    bootstrap_agentscope_records,
)
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    AgentScopeRuntime,
    build_extra_agent_middlewares_factory,
)
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.session_store import WebSessionStore
from vla_data_juicer_agents.web.sse import stream_session_events


FORBIDDEN_PUBLIC_TOOL_RESULT_EVENTS = {
    "TOOL_RESULT_START",
    "TOOL_RESULT_TEXT_DELTA",
    "TOOL_RESULT_DATA_DELTA",
    "TOOL_RESULT_END",
}


class SignalingScriptedChatModel(ScriptedChatModel):
    def __init__(self) -> None:
        super().__init__()
        self.wakeup_reasoned = asyncio.Event()

    async def _call_api(self, *args, **kwargs):
        response = await super()._call_api(*args, **kwargs)
        if len(self.invocations) >= 3:
            self.wakeup_reasoned.set()
        return response


class AlwaysAllowedFunctionTool(FunctionTool):
    async def check_permissions(self, *_args, **_kwargs) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="deterministic integration harness tool",
        )


@dataclass
class BackgroundCase:
    stack: AsyncExitStack
    runtime: AgentScopeRuntime
    store: WebSessionStore
    public_bus: SessionEventBus
    message_bus: DeterministicMessageBus
    background_manager: BackgroundTaskManager
    registry: RecordingChatRunRegistry
    model: SignalingScriptedChatModel
    web_session_id: str
    agentscope_session_id: str
    tool_started: asyncio.Event
    tool_cancelled: asyncio.Event
    release_tool: asyncio.Event
    background_wakeup_enqueued: asyncio.Event
    wakeup_snapshots: list[dict[str, Any]]
    real_offload_middlewares: list[ToolOffloadMiddleware]


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


async def _build_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    result: dict[str, Any],
    cancellation_resistant: bool = False,
) -> BackgroundCase:
    config = runtime_config(tmp_path)
    storage = ChatServiceStorage()
    await bootstrap_agentscope_records(storage, config)
    message_bus = DeterministicMessageBus()
    registry = RecordingChatRunRegistry()
    background_manager = BackgroundTaskManager(message_bus)
    workspace_manager = ChatServiceWorkspaceManager()
    store = WebSessionStore(tmp_path / "web.sqlite")
    public_session = store.create_session("background ToolOffload")
    public_bus = SessionEventBus()

    runtime = AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        app=None,
        bootstrapped=True,
    )
    runtime.set_web_transport(store, public_bus.publish)

    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    release_tool = asyncio.Event()

    async def delayed_extract() -> dict[str, Any]:
        tool_started.set()
        try:
            await release_tool.wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            if not cancellation_resistant:
                raise
            await release_tool.wait()
        return result

    model = SignalingScriptedChatModel()
    model.enqueue_tool("delayed_extract", {})
    model.enqueue_text("后台任务已经开始，我会在完成后继续。")
    model.enqueue_text("我已收到后台任务的真实结果并继续回复。")

    async def get_model(*_args, **_kwargs):
        return model

    async def get_toolkit(**_kwargs):
        return Toolkit(
            tools=[
                AlwaysAllowedFunctionTool(
                    delayed_extract,
                    name="delayed_extract",
                    is_read_only=True,
                )
            ]
        )

    real_offload_middlewares: list[ToolOffloadMiddleware] = []

    def shortened_real_tool_offload(**kwargs):
        middleware = ToolOffloadMiddleware(**kwargs, timeout_secs=0.01)
        real_offload_middlewares.append(middleware)
        return middleware

    monkeypatch.setattr(chat_service_module, "get_model", get_model)
    monkeypatch.setattr(chat_service_module, "get_toolkit", get_toolkit)
    monkeypatch.setattr(
        chat_service_module,
        "ToolOffloadMiddleware",
        shortened_real_tool_offload,
    )

    chat_service = ChatService(
        storage=storage,
        workspace_manager=workspace_manager,
        scheduler_manager=InertManager(),
        background_task_manager=background_manager,
        message_bus=message_bus,
        extra_agent_middlewares=build_extra_agent_middlewares_factory(
            config,
            runtime=runtime,
        ),
    )
    runtime.app = SimpleNamespace(
        state=SimpleNamespace(
            chat_service=chat_service,
            chat_run_registry=registry,
        )
    )

    background_wakeup_enqueued = asyncio.Event()
    wakeup_snapshots: list[dict[str, Any]] = []

    def capture_wakeup(payload: dict[str, Any]) -> None:
        if payload.get("kind") != MessageBusKeys.WAKEUP_KIND_WAKE:
            return
        assert public_bus._subscribers == {}
        detail = store.get_session(public_session.id)
        assert detail is not None
        wakeup_snapshots.append(
            {
                "tool_runs": [row.model_dump(mode="json") for row in detail.tool_runs],
                "events": [row.event for row in detail.events],
            }
        )
        background_wakeup_enqueued.set()

    message_bus.on_wakeup_enqueued = capture_wakeup

    stack = AsyncExitStack()
    await stack.__aenter__()
    await stack.enter_async_context(registry)
    await stack.enter_async_context(background_manager)
    await stack.enter_async_context(
        CancelDispatcher(message_bus, registry, background_manager)
    )
    await stack.enter_async_context(
        WakeupDispatcher(message_bus, storage, chat_service, registry)
    )

    agentscope_session_id = await runtime._start_agent_run(
        web_session_id=public_session.id,
        agent_id=config.main_router_agent_id,
        model=config.router_model,
        message="处理这批数据",
    )
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=1)
    except TimeoutError as error:
        raise AssertionError(
            "real ChatService run ended before invoking tool: "
            f"model_invocations={len(model.invocations)} "
            f"completed_runs={len(registry.completed_tasks)} "
            f"bus_operations={message_bus.operations!r}"
        ) from error
    await asyncio.wait_for(message_bus.background_registered.wait(), timeout=1)
    await registry.wait_for_completions(1)

    assert public_bus._subscribers == {}
    assert real_offload_middlewares
    assert all(
        type(middleware) is ToolOffloadMiddleware
        for middleware in real_offload_middlewares
    )

    return BackgroundCase(
        stack=stack,
        runtime=runtime,
        store=store,
        public_bus=public_bus,
        message_bus=message_bus,
        background_manager=background_manager,
        registry=registry,
        model=model,
        web_session_id=public_session.id,
        agentscope_session_id=agentscope_session_id,
        tool_started=tool_started,
        tool_cancelled=tool_cancelled,
        release_tool=release_tool,
        background_wakeup_enqueued=background_wakeup_enqueued,
        wakeup_snapshots=wakeup_snapshots,
        real_offload_middlewares=real_offload_middlewares,
    )


async def _finish_background(case: BackgroundCase) -> None:
    case.release_tool.set()
    await asyncio.wait_for(case.background_wakeup_enqueued.wait(), timeout=1)
    await asyncio.wait_for(case.model.wakeup_reasoned.wait(), timeout=1)
    task = case.registry.get(case.agentscope_session_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
    await _wait_until(
        lambda: case.registry.get(case.agentscope_session_id) is None,
    )


async def _replay_from_zero(case: BackgroundCase):
    expected = case.store.list_public_events(case.web_session_id)
    assert case.public_bus._subscribers == {}
    async with stream_session_events(
        case.store,
        case.public_bus,
        case.web_session_id,
        0,
    ) as records:
        assert len(case.public_bus._subscribers[case.web_session_id]) == 1
        replayed = [await anext(records) for _ in expected]
    assert case.public_bus._subscribers == {}
    return replayed


def _terminal_events(events) -> list[dict[str, Any]]:
    return [
        record.event["value"]
        for record in events
        if record.event.get("name") == "datapilot_tool_terminal"
    ]


def _assistant_reply_ids(events) -> list[str]:
    return [
        str(record.event["reply_id"])
        for record in events
        if str(record.event.get("type", "")).upper() == "REPLY_START"
    ]


def _assert_public_identity_boundary(case: BackgroundCase, events) -> None:
    serialized = json.dumps(
        [record.event for record in events],
        ensure_ascii=False,
    )
    assert case.agentscope_session_id not in serialized
    assert case.runtime.config.main_router_agent_id not in serialized
    assert case.runtime.config.user_id not in serialized
    assert "MainRouterAgent" not in serialized


def _assert_public_replay_contract(
    case: BackgroundCase,
    events,
    *,
    expected_status: str,
    expected_error_type: str | None,
) -> dict[str, Any]:
    assert [record.sequence for record in events] == list(
        range(1, len(events) + 1)
    )
    event_types = {
        str(record.event.get("type", "")).upper()
        for record in events
    }
    assert event_types.isdisjoint(FORBIDDEN_PUBLIC_TOOL_RESULT_EVENTS)

    terminals = _terminal_events(events)
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal["status"] == expected_status
    assert terminal.get("error_type") == expected_error_type

    reply_ids = _assistant_reply_ids(events)
    assert len(reply_ids) == 2
    assert reply_ids[0] != reply_ids[1]
    _assert_public_identity_boundary(case, events)
    return terminal


@pytest.mark.asyncio
async def test_real_agentscope_background_failure_replays_after_browser_connects(
    monkeypatch,
    tmp_path,
):
    case = await _build_case(
        monkeypatch,
        tmp_path,
        result={
            "ok": False,
            "message": "extract failed",
            "error_type": "extract_sync_failed",
        },
    )
    try:
        await _finish_background(case)
        replayed = await _replay_from_zero(case)

        detail = case.store.get_session(case.web_session_id)
        assert detail is not None
        assert [
            {
                "tool_call_id": row.tool_call_id,
                "status": row.status,
                "error_type": row.error_type,
            }
            for row in detail.tool_runs
        ] == [
            {
                "tool_call_id": "call-1-delayed_extract",
                "status": "failure",
                "error_type": "extract_sync_failed",
            }
        ]
        terminal = _assert_public_replay_contract(
            case,
            replayed,
            expected_status="failure",
            expected_error_type="extract_sync_failed",
        )
        assert terminal["error_type"] == "extract_sync_failed"
        assert case.wakeup_snapshots[0]["tool_runs"][0]["status"] == "failure"
        assert any(
            event.get("name") == "datapilot_tool_terminal"
            and event["value"]["status"] == "failure"
            for event in case.wakeup_snapshots[0]["events"]
        )
        case.model.assert_exhausted()
    finally:
        await case.stack.aclose()


@pytest.mark.asyncio
async def test_real_agentscope_background_success_has_one_terminal_and_wakeup_reply(
    monkeypatch,
    tmp_path,
):
    case = await _build_case(
        monkeypatch,
        tmp_path,
        result={"ok": True, "message": "extract completed"},
    )
    try:
        await _finish_background(case)
        replayed = await _replay_from_zero(case)

        detail = case.store.get_session(case.web_session_id)
        assert detail is not None
        assert [row.status for row in detail.tool_runs] == ["success"]
        _assert_public_replay_contract(
            case,
            replayed,
            expected_status="success",
            expected_error_type=None,
        )
        assert case.wakeup_snapshots[0]["tool_runs"][0]["status"] == "success"
        case.model.assert_exhausted()
    finally:
        await case.stack.aclose()


@pytest.mark.asyncio
async def test_real_agentscope_explicit_stop_wins_over_late_background_success(
    monkeypatch,
    tmp_path,
):
    case = await _build_case(
        monkeypatch,
        tmp_path,
        result={"ok": True, "message": "late success"},
        cancellation_resistant=True,
    )
    try:
        manager = AgentScopeWebSessionManager(
            store=case.store,
            runtime=case.runtime,
            event_callback=case.public_bus.publish,
        )
        response = await manager.interrupt(case.web_session_id)
        assert response.interrupted is True
        assert response.stopped_tool_call_ids == ["call-1-delayed_extract"]
        await asyncio.wait_for(case.tool_cancelled.wait(), timeout=1)

        detail = case.store.get_session(case.web_session_id)
        assert detail is not None
        assert [row.status for row in detail.tool_runs] == ["stopped"]
        assert any(
            event.event.get("name") == "datapilot_human_decision_resolved"
            and event.event.get("value") == {"reason": "stopped", "all": True}
            for event in detail.events
        )

        case.release_tool.set()
        await _wait_until(lambda: not case.background_manager.tasks)
        await asyncio.sleep(0)
        replayed = await _replay_from_zero(case)

        detail = case.store.get_session(case.web_session_id)
        assert detail is not None
        assert [row.status for row in detail.tool_runs] == ["stopped"]
        terminals = _terminal_events(replayed)
        assert [terminal["status"] for terminal in terminals] == ["stopped"]
        assert len(_assistant_reply_ids(replayed)) == 1
        assert len(case.model.invocations) == 2
        assert case.wakeup_snapshots == []
        assert case.message_bus._queues[MessageBusKeys.wakeup_queue()] == []
    finally:
        await case.stack.aclose()
