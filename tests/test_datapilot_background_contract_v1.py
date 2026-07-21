from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from vla_data_juicer_agents.adapters.agentscope.events import AgentScopeEventAdapter
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.navigation.plan_store import (
    SqliteNavigationPlanRepository,
    StepClaimOutcome,
)
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


def _completed_extract_sync_plan(
    repository: SqliteNavigationPlanRepository,
    task: Any,
):
    plan = repository.activate(
        task,
        "extract_sync",
        1,
        {
            "decisions": {
                "sensor_bindings": {
                    "bindings": {
                        "fisheye_front": "/camera/front/image",
                        "lidar": "/lidar/points",
                        "odom": "/localization/odom",
                    },
                    "reason": "Observed matching message types and rates.",
                    "evidence_refs": ["evidence:sensors"],
                },
                "topic_selection": {
                    "topic_whitelist": [
                        "/camera/front/image",
                        "/lidar/points",
                        "/localization/odom",
                    ],
                    "topic_map": {
                        "camera": "fisheye_front",
                        "lidar": "r32_rslidar_points",
                        "localization": "odom",
                    },
                    "query_dir": "lidar",
                    "reason": "All selected topics were observed.",
                    "evidence_refs": ["evidence:topics"],
                },
                "time_sync": {
                    "reference_sensor": "lidar",
                    "method": "nearest_timestamp",
                    "reason": "Lidar timestamps cover the selected streams.",
                    "evidence_refs": ["evidence:timing"],
                },
            },
            "steps": [
                {
                    "step_id": "prepare",
                    "action": "prepare_raw_data",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": [],
                    "failure_policy": "stop",
                    "decision_refs": [],
                },
                {
                    "step_id": "sync",
                    "action": "extract_and_sync_navigation_data",
                    "variant": "explicit_topic_params",
                    "arguments": {"processes_num": 8},
                    "depends_on": ["prepare"],
                    "failure_policy": "stop",
                    "decision_refs": ["sensor_bindings"],
                },
            ],
        },
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    for step_id, action in (
        ("prepare", "prepare_raw_data"),
        ("sync", "extract_and_sync_navigation_data"),
    ):
        assert repository.claim_step(
            plan.plan_id,
            step_id,
            action,
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        ) is StepClaimOutcome.CLAIMED
        staged = repository.stage_step_result(
            plan.plan_id,
            step_id,
            expected_action=action,
            target_status="completed",
            full_result={"ok": True},
            result_summary={"ok": True},
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
        assert staged.result_ref is not None
        assert repository.attach_staged_result_evidence(
            plan.plan_id,
            step_id,
            staged.result_ref,
            expected_action=action,
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
        assert repository.finalize_staged_step(
            plan.plan_id,
            step_id,
            expected_action=action,
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
    completed = repository.get(plan.plan_id)
    assert completed is not None and completed.status == "completed"
    return completed


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


@pytest.mark.asyncio
async def test_plan_bound_offload_completion_enqueues_same_session_wakeup() -> None:
    delivered = asyncio.Event()

    class MessageBus:
        def __init__(self) -> None:
            self.inbox: list[tuple[str, dict[str, Any]]] = []
            self.wakeups: list[dict[str, str]] = []

        async def inbox_push(self, session_id: str, payload: dict[str, Any]) -> None:
            self.inbox.append((session_id, payload))

        async def enqueue_wakeup(
            self,
            *,
            user_id: str,
            session_id: str,
            agent_id: str,
        ) -> None:
            self.wakeups.append(
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                }
            )
            delivered.set()

    class BackgroundManager:
        async def register_task(self, **_kwargs) -> str:
            return "background-task-1"

    class Toolkit:
        async def get_tool(self, _name: str):
            return SimpleNamespace(is_state_injected=False, is_external_tool=False)

    message_bus = MessageBus()
    middleware = DataPilotToolOffloadMiddleware(
        bg_manager=BackgroundManager(),
        message_bus=message_bus,
        user_id="test-user",
        agent_id="navigation-data-agent",
        timeout_secs=0.001,
    )
    agent = SimpleNamespace(
        name="navigation-data-agent",
        state=SimpleNamespace(session_id="navigation-session-1"),
        toolkit=Toolkit(),
    )
    tool_call = SimpleNamespace(
        id="extract-call-1",
        name="extract_and_sync_navigation_data_tool",
        input=json.dumps({"plan_id": "plan-1", "step_id": "sync"}),
    )

    async def delayed_result(**_kwargs):
        await asyncio.sleep(0.02)
        yield ToolResponse(
            content=[TextBlock(text='{"ok":true}')],
            state=ToolResultState.SUCCESS,
            id=tool_call.id,
        )

    responses = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": tool_call},
            delayed_result,
        )
    ]
    await asyncio.wait_for(delivered.wait(), timeout=1)

    assert responses[-1].state is ToolResultState.SUCCESS
    assert message_bus.inbox and message_bus.inbox[0][0] == "navigation-session-1"
    assert message_bus.wakeups == [
        {
            "user_id": "test-user",
            "session_id": "navigation-session-1",
            "agent_id": "navigation-data-agent",
        }
    ]


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
async def test_background_wakeup_reuses_navigation_session_and_persists_await_user(
    tmp_path: Path,
) -> None:
    """Join offload, wakeup, reinspection and the durable wait contract."""
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    task_store = SqliteNavigationTaskStore(tmp_path / "navigation.sqlite")
    session = store.create_session("后台完成后继续确认", contract_version=1)
    turn = store.begin_user_turn(session.id, "处理导航数据").turn
    binding, task = _create_task(
        store=store,
        task_store=task_store,
        web_session_id=session.id,
        task_id="task-background-wakeup",
        task_ref="DP-BACKGROUND-WAKEUP",
        navigation_session_id="navigation-background-wakeup",
    )
    plan_repository = SqliteNavigationPlanRepository(task_store.db_path)
    completed_plan = _completed_extract_sync_plan(plan_repository, task)
    task = task_store.get_task(task.task_id)
    assert task is not None and task.accepted_plan_phase == "extract_sync"
    store.bind_conversation_agent_session_to_turn(binding.navigation_session_id, turn.id)
    authority = store.get_response_authority(turn.id)
    assert authority is not None
    store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=authority.generation,
        new_producer="navigation",
    )
    wakeups = [
        {
            "user_id": "test-user",
            "agent_id": "navigation-data-agent",
            "session_id": binding.navigation_session_id,
        }
    ]

    class WakeMessageBus(_MessageBus):
        async def session_is_running(self, _session_id: str) -> bool:
            return False

        async def dequeue_wakeups(self, max_count: int = 64):
            batch = wakeups[:max_count]
            del wakeups[:max_count]
            return batch

    class RunRegistry:
        def __init__(self) -> None:
            self.tasks = []
            self.session_ids: list[str] = []

        def spawn(self, coroutine, *, session_id: str) -> None:
            self.session_ids.append(session_id)
            self.tasks.append(asyncio.create_task(coroutine))

        async def drain(self) -> None:
            await asyncio.gather(*self.tasks)

    message_bus = WakeMessageBus()
    class WakeStorage:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def get_session(self, user_id: str, agent_id: str, session_id: str):
            self.calls.append((user_id, agent_id, session_id))
            return SimpleNamespace(id=session_id)

    wake_storage = WakeStorage()
    runtime = _runtime(
        tmp_path,
        store=store,
        task_store=task_store,
        message_bus=message_bus,
    )
    runtime.storage = wake_storage
    runtime._navigation_services = (  # type: ignore[method-assign]
        lambda: SimpleNamespace(
            task_store=task_store,
            plan_store=plan_repository,
            observation_store=SimpleNamespace(latest=lambda _task_id: None),
        )
    )
    runtime._navigation_durable_state_anchor = (  # type: ignore[method-assign]
        AgentScopeRuntime._navigation_durable_state_anchor.__get__(
            runtime,
            AgentScopeRuntime,
        )
    )

    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="background-start",
        events=[],
        private_events=[],
        raw_event_type="REPLY_START",
        reply_id="background-reply",
    )
    background_private = (
        {
            "type": "tool_start",
            "payload": {
                "tool": "extract_and_sync_navigation_data_tool",
                "call_id": "extract-call",
            },
        },
        {
            "type": "tool_background",
            "payload": {
                "tool": "extract_and_sync_navigation_data_tool",
                "call_id": "extract-call",
            },
        },
    )
    projected_background = runtime.project_contract_v1_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="background-offload",
        reply_id="background-reply",
        events=background_private,
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="background-offload",
        events=list(projected_background),
        private_events=background_private,
        raw_event_type="TOOL_RESULT",
        reply_id="background-reply",
    )
    store.append_projected_event_batch(
        web_session_id=session.id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="background-reply-end",
        events=[
            {
                "contract_version": 1,
                "type": "task_state_updated",
                "payload": {"task_ref": binding.task_ref, "status": "active"},
            }
        ],
        private_events=[],
        raw_event_type="REPLY_END",
        reply_id="background-reply",
    )
    assert store.get_active_turn(session.id) is None

    run_registry = RunRegistry()
    wake_calls: list[dict[str, Any]] = []

    class WakeChatService:
        async def run(self, *, user_id, session_id, agent_id, input_msg):
            wake_calls.append(
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "input_msg": input_msg,
                }
            )
            anchor = runtime._navigation_durable_state_anchor(
                session_id,
                web_session_id=session.id,
            )
            assert anchor["execution_status"] == "completed"
            assert anchor["accepted_plan_id"] == completed_plan.plan_id
            private_events: list[dict[str, Any]] = []
            scope = EventEmitter(CallbackEventSink(private_events.append)).scope(
                "agentscope",
                run_id=session_id,
            )
            adapter = AgentScopeEventAdapter(
                scope,
                emit_tool_events=True,
                emit_reply_summary_events=True,
                emit_answer_delta_events=True,
                emit_progress_events=True,
                public_tool_events=True,
            )
            adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="wake-reply"))
            adapter.accept(
                SimpleNamespace(
                    type="TOOL_CALL_START",
                    tool_call_id="inspect-call",
                    tool_call_name="inspect_navigation_artifact_state_tool",
                )
            )
            adapter.accept(
                SimpleNamespace(
                    type="TOOL_RESULT_START",
                    tool_call_id="inspect-call",
                    tool_call_name="inspect_navigation_artifact_state_tool",
                )
            )
            adapter.accept(
                SimpleNamespace(
                    type="TOOL_RESULT_END",
                    tool_call_id="inspect-call",
                    state="success",
                )
            )
            adapter.accept(
                SimpleNamespace(
                    type="TEXT_BLOCK_DELTA",
                    delta=(
                        'AwaitUser: {"version":1,"kind":"await_user",'
                        '"purpose":"stage_transition",'
                        '"requested_fields":["continue_processing","scene_mode"],'
                        '"response_channel":"router_text",'
                        '"public_prompt":"拆解同步已完成。是否继续，并请说明室内或室外？"}'
                    ),
                )
            )
            adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="wake-reply"))
            projected = runtime.project_contract_v1_event_batch(
                web_session_id=session.id,
                agentscope_session_id=session_id,
                entry_id="wake-reply-end",
                reply_id="wake-reply",
                events=tuple(private_events),
            )
            store.append_projected_event_batch(
                web_session_id=session.id,
                agentscope_session_id=session_id,
                entry_id="wake-reply-end",
                events=list(projected),
                private_events=private_events,
                raw_event_type="REPLY_END",
                reply_id="wake-reply",
            )

    runtime.app.state.chat_service = WakeChatService()
    runtime.app.state.chat_run_registry = run_registry

    assert await runtime.recover_pending_agent_wakeups_once(retry_delays=(0,)) == 1
    await run_registry.drain()

    assert run_registry.session_ids == [binding.navigation_session_id]
    assert wake_storage.calls == [
        ("test-user", "navigation-data-agent", binding.navigation_session_id)
    ]
    assert wake_calls == [
        {
            "user_id": "test-user",
            "session_id": binding.navigation_session_id,
            "agent_id": "navigation-data-agent",
            "input_msg": None,
        }
    ]
    current_task = task_store.get_task(task.task_id)
    current_binding = store.get_task_binding(task.task_id)
    assert current_task is not None
    assert current_task.status == NavigationTaskStatus.WAITING_USER
    assert current_binding is not None and current_binding.status == "waiting_user"
    detail = store.get_session(session.id)
    assert detail is not None
    system_turns = [item for item in detail.turns if item.origin == "system"]
    assert len(system_turns) == 1 and system_turns[0].status == "completed"
    assert [item.type for item in detail.events].count("final") == 2
    assert [message.content for message in detail.messages][-1] == (
        "拆解同步已完成。是否继续，并请说明室内或室外？"
    )
    assert "未能生成可安全展示的回复" not in detail.model_dump_json()
    action_events = [item for item in detail.events if item.type == "action_start"]
    assert action_events[-1].payload["phase"] == "inspection"


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
    runtime.router_context_envelope(
        session.id,
        router_session_id=router_mapping.agentscope_session_id,
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
        action="cancel",
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
