from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.app.middleware import ToolOffloadMiddleware

from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    AgentScopeRuntime,
    DataPilotToolOffloadMiddleware,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore


class _MessageBus:
    def __init__(self) -> None:
        self.cancelled_sessions: list[str] = []

    async def session_publish_cancel(self, session_id: str) -> None:
        self.cancelled_sessions.append(session_id)


def _config(tmp_path: Path) -> AgentScopeRuntimeConfig:
    return AgentScopeRuntimeConfig(
        user_id="test-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="test-model",
        router_model="router-model",
        navigation_model="navigation-model",
        datapilot_single_agent_mode="new_sessions",
    )


def _runtime(
    tmp_path: Path,
    *,
    store: WebSessionStore,
    task_store: SqliteNavigationTaskStore,
    message_bus: Any | None = None,
) -> AgentScopeRuntime:
    runtime = AgentScopeRuntime(
        config=_config(tmp_path),
        storage=None,
        message_bus=message_bus or _MessageBus(),
        workspace_manager=None,
        app=SimpleNamespace(state=SimpleNamespace()),
        web_session_store=store,
    )
    runtime._navigation_task_store = lambda: task_store  # type: ignore[method-assign]
    runtime._navigation_durable_state_anchor = (  # type: ignore[method-assign]
        lambda _session_id, **_kwargs: {"execution_status": "running"}
    )
    return runtime


def _create_task(
    *,
    store: WebSessionStore,
    task_store: SqliteNavigationTaskStore,
    web_session_id: str,
    task_id: str = "task-private-1",
    task_ref: str = "task_public_A1B2",
    navigation_session_id: str = "navigation-private-1",
):
    creation = store.create_task_binding(
        web_session_id,
        task_id=task_id,
        task_ref=task_ref,
        navigation_session_id=navigation_session_id,
    )
    task = task_store.create_task_attempt(
        task_id=task_id,
        request="处理导航数据",
        target="navigation_data",
        date="20260720",
        segments=["clip_a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=web_session_id,
        agentscope_session_id=navigation_session_id,
    ).task
    return creation.binding, task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected_path"),
    [
        (
            "extract_and_sync_navigation_data_tool",
            {"plan_id": "plan-1", "step_id": "step-1"},
            "offload",
        ),
        (
            "extract_and_sync_navigation_data_tool",
            {"date": "20260720"},
            "synchronous",
        ),
        (
            "inspect_navigation_artifacts_tool",
            {"plan_id": "plan-1", "step_id": "step-1"},
            "synchronous",
        ),
        (
            "start_navigation_data_task",
            {"plan_id": "plan-1", "step_id": "step-1"},
            "synchronous",
        ),
        ("emit_activity_tool", {}, "synchronous"),
    ],
)
async def test_tool_offload_requires_allowlisted_plan_bound_action(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tool_input: dict[str, Any],
    expected_path: str,
) -> None:
    calls: list[str] = []

    async def parent_on_acting(
        _self: Any,
        _agent: Any,
        input_kwargs: dict[str, Any],
        _next_handler: Any,
    ):
        calls.append("offload")
        yield ("offload", input_kwargs["tool_call"].name)

    async def next_handler(**input_kwargs: Any):
        calls.append("synchronous")
        yield ("synchronous", input_kwargs["tool_call"].name)

    monkeypatch.setattr(ToolOffloadMiddleware, "on_acting", parent_on_acting)
    middleware = DataPilotToolOffloadMiddleware(
        bg_manager=SimpleNamespace(),
        message_bus=SimpleNamespace(),
        user_id="test-user",
        agent_id="navigation-data-agent",
    )
    tool_call = SimpleNamespace(
        name=tool_name,
        input=json.dumps(tool_input),
    )

    yielded = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": tool_call},
            next_handler,
        )
    ]

    assert calls == [expected_path]
    assert yielded == [(expected_path, tool_name)]


def test_active_background_wake_uses_navigation_authority_without_assistant_final(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.sqlite"
    navigation_database = tmp_path / "navigation.sqlite"
    store = WebSessionStore(database)
    task_store = SqliteNavigationTaskStore(navigation_database)
    session = store.create_session("后台导航任务", contract_version=1)
    binding, _task = _create_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)

    start_records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="wake-1-0",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="wake-reply-1",
    )
    mapping = store.get_conversation_agent_session_by_agentscope_session(
        binding.navigation_session_id
    )
    assert mapping is not None
    assert mapping.active_turn_id is None
    assert start_records == []

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="wake-1-1",
        events=(
            {
                "type": "final",
                "payload": {"text": "后台检查仍在继续。"},
            },
        ),
    )
    assert [event["type"] for event in projected] == ["task_state_updated"]

    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="wake-1-1",
        events=list(projected),
        raw_event_type="REPLY_END",
        reply_id="wake-reply-1",
    )

    detail = store.get_session(session.id)
    assert detail is not None
    assert all(turn.origin != "system" for turn in detail.turns)
    assert [message for message in detail.messages if message.role == "assistant"] == []
    task_events = [event for event in detail.events if event.type == "task_state_updated"]
    assert len(task_events) == 1
    assert task_events[0].payload["task_ref"] == binding.task_ref
    assert task_events[0].payload["status"] == "active"

    reopened_store = WebSessionStore(database)
    reopened_task_store = SqliteNavigationTaskStore(navigation_database)
    reopened_runtime = _runtime(
        tmp_path,
        store=reopened_store,
        task_store=reopened_task_store,
    )
    reopened_detail = reopened_store.get_session(session.id)
    assert reopened_detail is not None
    assert any(event.type == "task_state_updated" for event in reopened_detail.events)
    snapshots = reopened_runtime.session_task_snapshots(session.id)
    assert len(snapshots) == 1
    assert snapshots[0]["task_ref"] == binding.task_ref
    assert snapshots[0]["status"] == "active"


def test_terminal_background_update_creates_one_public_system_turn(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后台任务完成", contract_version=1)
    binding, task = _create_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    task_store.update_task_for_session(
        task.task_id,
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        expected_state_revision=task.state_revision,
        status=NavigationTaskStatus.COMPLETED,
    )
    store.mark_task_binding_terminal(
        task.task_id,
        expected_revision=binding.state_revision,
        status="completed",
        latest_public_update="任务已完成。",
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)

    projected = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="wake-terminal-1",
        events=({"type": "final", "payload": {"text": "后台任务已完成。"}},),
    )
    records = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="wake-terminal-1",
        events=list(projected),
        raw_event_type="REPLY_END",
        reply_id="wake-terminal-reply",
    )

    detail = store.get_session(session.id)
    assert detail is not None
    system_turns = [turn for turn in detail.turns if turn.origin == "system"]
    assert len(system_turns) == 1
    assert system_turns[0].status == "completed"
    assert [event.type for event in records].count("final") == 1
    assert [message.content for message in detail.messages if message.role == "assistant"] == [
        "后台任务已完成。"
    ]


@pytest.mark.asyncio
async def test_background_navigation_is_deferred_while_router_turn_has_authority(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("Router 正在回复", contract_version=1)
    router_turn = store.begin_user_turn(session.id, "先回答这个问题").turn
    router_mapping = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-private-1",
    )
    store.bind_conversation_agent_session_to_turn(
        router_mapping.agentscope_session_id,
        router_turn.id,
    )
    binding, _task = _create_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )

    deferred = store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="background-deferred-1",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="background-reply-1",
    )

    assert deferred == []
    outbox = store.get_outbox_by_idempotency_key(
        f"system_turn:{binding.navigation_session_id}:background-deferred-1"
    )
    assert outbox is not None
    assert outbox.status == "pending"
    assert store.get_active_turn(session.id) == router_turn
    authority = store.get_response_authority(router_turn.id)
    assert authority is not None
    assert authority.producer == "router"
    assert authority.lease_state == "open"
    navigation_mapping = store.get_conversation_agent_session_by_agentscope_session(
        binding.navigation_session_id
    )
    assert navigation_mapping is not None
    assert navigation_mapping.active_turn_id is None
    detail = store.get_session(session.id)
    assert detail is not None
    assert all(turn.origin != "system" for turn in detail.turns)
    assert all(event.turn_id == router_turn.id for event in detail.events)

    store.commit_authorized_final(
        router_turn.id,
        producer="router",
        response_generation=authority.generation,
        text="Router 回答已结束。",
    )
    runtime = _runtime(tmp_path, store=store, task_store=task_store)
    assert await runtime.recover_contract_v1_outbox_once() == 1
    completed_outbox = store.get_outbox(outbox.outbox_id)
    assert completed_outbox is not None
    assert completed_outbox.status == "completed"
    navigation_mapping = store.get_conversation_agent_session_by_agentscope_session(
        binding.navigation_session_id
    )
    assert navigation_mapping is not None
    assert navigation_mapping.active_turn_id is None


@pytest.mark.asyncio
async def test_stop_button_pauses_run_and_keeps_task_slot_open(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    message_bus = _MessageBus()
    session = store.create_session("停止当前处理", contract_version=1)
    turn = store.begin_user_turn(session.id, "开始导航处理").turn
    binding, task = _create_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        turn.id,
    )
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=1,
    )
    runtime = _runtime(
        tmp_path,
        store=store,
        task_store=task_store,
        message_bus=message_bus,
    )
    transitions: list[NavigationTaskStatus] = []
    update_task = task_store.update_task_for_session

    def track_update(task_id: str, **changes: Any):
        transitions.append(changes["status"])
        return update_task(task_id, **changes)

    task_store.update_task_for_session = track_update  # type: ignore[method-assign]

    assert await runtime.interrupt_web_session(web_session_id=session.id) is True

    paused_task = task_store.get_task(task.task_id)
    paused_binding = store.get_task_binding(task.task_id)
    closed_turn = store.get_turn(turn.id)
    assert paused_task is not None
    assert paused_task.status == NavigationTaskStatus.PAUSED
    assert paused_binding is not None
    assert paused_binding.status == "paused"
    assert paused_binding.slot_state == "open"
    assert closed_turn is not None
    assert closed_turn.status == "completed"
    assert transitions == [NavigationTaskStatus.PAUSING, NavigationTaskStatus.PAUSED]
    assert store.get_active_turn(session.id) is None
    assert message_bus.cancelled_sessions == [binding.navigation_session_id]
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.content for message in detail.messages if message.role == "assistant"] == [
        "已停止当前处理，你可以继续补充要求或稍后恢复。"
    ]


@pytest.mark.asyncio
async def test_cancel_closes_navigation_task_slot(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    message_bus = _MessageBus()
    session = store.create_session("取消导航任务", contract_version=1)
    turn = store.begin_user_turn(session.id, "取消这个任务").turn
    router_mapping = store.save_conversation_agent_session(
        session.id,
        agent_role="router",
        agent_id="main-router-agent",
        agentscope_session_id="router-private-1",
    )
    store.bind_conversation_agent_session_to_turn(
        router_mapping.agentscope_session_id,
        turn.id,
    )
    binding, task = _create_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
    )
    runtime = _runtime(
        tmp_path,
        store=store,
        task_store=task_store,
        message_bus=message_bus,
    )
    transitions: list[NavigationTaskStatus] = []
    update_task = task_store.update_task_for_session

    def track_update(task_id: str, **changes: Any):
        transitions.append(changes["status"])
        return update_task(task_id, **changes)

    task_store.update_task_for_session = track_update  # type: ignore[method-assign]

    result = await runtime.control_navigation_agent_task_v1(
        web_session_id=session.id,
        router_session_id=router_mapping.agentscope_session_id,
        task_ref=binding.task_ref,
        action="cancel",
        response_language="zh-CN",
        expected_task_revision=task.state_revision,
    )

    cancelled_task = task_store.get_task(task.task_id)
    cancelled_binding = store.get_task_binding(task.task_id)
    assert result["status"] == "cancelled"
    assert cancelled_task is not None
    assert cancelled_task.status == NavigationTaskStatus.CANCELLED
    assert cancelled_binding is not None
    assert cancelled_binding.status == "cancelled"
    assert cancelled_binding.slot_state == "closed"
    assert transitions == [
        NavigationTaskStatus.CANCELLING,
        NavigationTaskStatus.CANCELLED,
    ]
    assert store.get_active_turn(session.id) is None
    assert message_bus.cancelled_sessions == [binding.navigation_session_id]
