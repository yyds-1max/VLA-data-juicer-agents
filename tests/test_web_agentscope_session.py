import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import Msg, ToolCallBlock, ToolCallState, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext

from vla_data_juicer_agents.core.cancellation import CancellationContext, current_cancellation
from vla_data_juicer_agents.navigation.observation_store import SqliteNavigationObservationStore
from vla_data_juicer_agents.navigation.plan_models import FinishProcessingPlanInput
from vla_data_juicer_agents.navigation.plan_store import (
    ActivePlanExecutionConflict,
    SqliteNavigationPlanRepository,
    StepClaimOutcome,
)
from vla_data_juicer_agents.navigation.routing import is_high_confidence_navigation_request
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTaskStatus,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
import vla_data_juicer_agents.runtime.agentscope_runtime as agentscope_runtime_module
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    AgentScopeRuntime,
    build_extra_agent_middlewares_factory,
    build_extra_agent_tools_factory,
)
from vla_data_juicer_agents.runtime.agentscope_prompts import navigation_agent_prompt
from vla_data_juicer_agents.runtime.navigation_tool_surface import (
    NavigationToolSurfaceMiddleware,
)
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
from vla_data_juicer_agents.web.schemas import HumanDecisionRequest, SessionRecord
from vla_data_juicer_agents.web.session_store import WebSessionStore


class FakeAgentScopeRuntime:
    def __init__(self, turn_id: str = "turn_runtime_1") -> None:
        self.turn_id = turn_id
        self.submissions: list[dict[str, str]] = []

    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        self.submissions.append({"web_session_id": web_session_id, "message": message})
        return self.turn_id


class StoreAwareAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.web_session_store = None

    def set_web_session_store(self, store) -> None:
        self.web_session_store = store


class EventingAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(self, events: list[dict]) -> None:
        super().__init__()
        self.events = events
        self.subscriptions: list[str] = []

    async def subscribe_web_session_events(self, *, web_session_id: str):
        self.subscriptions.append(web_session_id)
        for event in self.events:
            yield event


class ConcurrentEventingAgentScopeRuntime(EventingAgentScopeRuntime):
    def __init__(self, events: list[dict]) -> None:
        super().__init__(events)
        self.active = 0
        self.max_active = 0

    async def subscribe_web_session_events(self, *, web_session_id: str):
        self.subscriptions.append(web_session_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            for event in self.events:
                yield event
                await asyncio.sleep(0)
        finally:
            self.active -= 1


class SwitchingEventingAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.subscription_key = ("main-router-agent", "main-session")
        self.subscriptions: list[tuple[str, tuple[str, str]]] = []

    def web_session_subscription_key(self, *, web_session_id: str):
        return self.subscription_key

    async def subscribe_web_session_events(self, *, web_session_id: str):
        current_key = self.subscription_key
        self.subscriptions.append((web_session_id, current_key))
        if current_key[0] == "main-router-agent":
            yield {
                "type": "final",
                "source": "DataPilot",
                "payload": {"text": "我来交给导航处理"},
            }
            self.subscription_key = ("navigation-data-agent", "navigation-session")
        else:
            yield {
                "type": "final",
                "source": "NavigationDataAgent",
                "payload": {"text": "开始检查导航数据"},
            }


class PersistentBatchEventingRuntime(FakeAgentScopeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.subscriptions = 0
        self.cursors: list[tuple[str, str]] = []

    def set_web_session_store(self, store) -> None:
        self.web_session_store = store

    def set_web_event_bridge(self, bridge) -> None:
        self.web_event_bridge = bridge

    def _remember_event_cursor(self, session_id: str, cursor: str) -> None:
        self.cursors.append((session_id, cursor))

    async def subscribe_agentscope_session_event_batches(
        self,
        *,
        web_session_id: str,
        agent_id: str,
        agentscope_session_id: str,
        continuous: bool,
        on_ready,
    ):
        del web_session_id, agent_id, agentscope_session_id, continuous
        self.subscriptions += 1
        on_ready()
        while True:
            batch = await self.queue.get()
            if batch is None:
                return
            yield batch


class RejectingAgentScopeRuntime(FakeAgentScopeRuntime):
    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        self.submissions.append({"web_session_id": web_session_id, "message": message})
        raise RuntimeError("turn rejected")


class InterruptingAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(self, turn_id: str = "turn_runtime_1", interrupted: bool = True) -> None:
        super().__init__(turn_id=turn_id)
        self.interrupted = interrupted
        self.interrupts: list[str] = []

    async def interrupt_web_session(self, *, web_session_id: str) -> bool:
        self.interrupts.append(web_session_id)
        return self.interrupted


class HumanDecisionAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(self, *, accepted: bool = True) -> None:
        super().__init__()
        self.accepted = accepted
        self.decisions: list[tuple[str, dict]] = []

    async def submit_human_decision(self, *, web_session_id: str, decision: dict) -> bool:
        self.decisions.append((web_session_id, decision))
        return self.accepted


class CapturingNavigationTaskRuntime:
    def __init__(self) -> None:
        self.started_tasks: list[dict[str, str]] = []
        self.handoffs: list[dict] = []

    async def start_navigation_agent_task(self, *, web_session_id: str, message: str) -> str:
        self.started_tasks.append({"web_session_id": web_session_id, "message": message})
        return SimpleNamespace(
            task_id="nav-test-1",
            agentscope_session_id="navigation-session-1",
        )

    def record_navigation_handoff(self, payload: dict) -> None:
        self.handoffs.append(payload)


class FakeAgentScopeMessageBus:
    _SESSION_EVENTS_KEY = "agentscope:session:events:{sid}"

    def __init__(
        self,
        *,
        replay_events: list[tuple[str, dict]] | None = None,
        live_events: list[dict] | None = None,
        running_states: list[bool] | None = None,
        wakeups: list[dict] | None = None,
        dequeue_failures: int = 0,
        inbox_session_ids: list[str] | None = None,
        inbox_residual_count: int | None = None,
    ) -> None:
        self.replay_events = replay_events or []
        self.live_events = live_events or []
        self.running_states = running_states or []
        self.wakeups = wakeups or []
        self.dequeue_failures = dequeue_failures
        self.inbox_session_ids = inbox_session_ids or []
        self._inbox_residual_count = inbox_residual_count
        self.read_sessions: list[str] = []
        self.read_since: list[str | None] = []
        self.subscribe_keys: list[str] = []
        self.cancelled_sessions: list[str] = []
        self.dequeue_calls = 0

    async def session_read_events(self, session_id: str, since=None):
        self.read_sessions.append(session_id)
        self.read_since.append(since)
        if since is None:
            return self.replay_events
        try:
            cursor_index = next(
                index
                for index, (entry_id, _event) in enumerate(self.replay_events)
                if entry_id == since
            )
        except StopIteration:
            return self.replay_events
        return self.replay_events[cursor_index + 1:]

    async def subscribe(self, key: str, *, on_ready=None):
        self.subscribe_keys.append(key)
        if on_ready is not None:
            on_ready()
        for event in self.live_events:
            yield event

    async def session_is_running(self, session_id: str) -> bool:
        if self.running_states:
            return self.running_states.pop(0)
        return False

    async def session_publish_cancel(self, session_id: str) -> None:
        self.cancelled_sessions.append(session_id)

    async def dequeue_wakeups(self, max_count: int = 64):
        self.dequeue_calls += 1
        if self.dequeue_failures > 0:
            self.dequeue_failures -= 1
            raise TimeoutError("redis timeout")
        batch = self.wakeups[:max_count]
        self.wakeups = self.wakeups[max_count:]
        return batch

    async def list_inbox_session_ids(self):
        return list(self.inbox_session_ids)

    async def wakeup_queue_length(self):
        return len(self.wakeups)

    async def inbox_residual_count(self):
        if self._inbox_residual_count is not None:
            return self._inbox_residual_count
        return len(self.inbox_session_ids)


class DelayedLiveEventMessageBus(FakeAgentScopeMessageBus):
    def __init__(
        self,
        *,
        live_delay: float,
        running_states: list[bool] | None = None,
    ) -> None:
        super().__init__(running_states=running_states)
        self.live_delay = live_delay

    async def subscribe(self, key: str, *, on_ready=None):
        self.subscribe_keys.append(key)
        if on_ready is not None:
            on_ready()
        await asyncio.sleep(self.live_delay)
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "迟到事件", "_entry_id": "1-0"}
        yield {"type": "REPLY_END", "_entry_id": "2-0"}


class FakeAgentScopeStorage:
    def __init__(self) -> None:
        self.sessions = []
        self.session_records = {}

    async def upsert_credential(self, user_id, credential_data):
        return credential_data.id

    async def upsert_agent(self, user_id, agent_record):
        return agent_record.id

    async def upsert_session(self, user_id, agent_id, config, *, session_id=None):
        self.sessions.append(
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "config": config,
                "id": session_id,
            }
        )
        return SimpleNamespace(id=session_id)

    async def get_session(self, user_id, agent_id, session_id):
        return self.session_records.get((user_id, agent_id, session_id))


class DelayedPendingDecisionStorage(FakeAgentScopeStorage):
    def __init__(self, *, pending_record) -> None:
        super().__init__()
        self.pending_record = pending_record
        self.get_session_calls = 0

    async def get_session(self, user_id, agent_id, session_id):
        self.get_session_calls += 1
        if self.get_session_calls == 1:
            return SimpleNamespace(state=SimpleNamespace(reply_id=None, context=[]))
        return self.pending_record


class FakeChatService:
    def __init__(self, storage=None) -> None:
        self.runs = []
        self.seen_cancellations = []
        self.storage = storage

    async def run(self, *, user_id, session_id, agent_id, input_msg):
        self.seen_cancellations.append(current_cancellation())
        self.runs.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "message": input_msg,
            }
        )
        if isinstance(input_msg, ExternalExecutionResultEvent) and self.storage is not None:
            record = self.storage.session_records[(user_id, agent_id, session_id)]
            for message in record.state.context:
                for block in message.get_content_blocks("tool_call"):
                    if block.id == input_msg.execution_results[0].id:
                        block.state = ToolCallState.FINISHED


class FakeChatRunRegistry:
    def __init__(self, *, reject_duplicate_active: bool = False) -> None:
        self.reject_duplicate_active = reject_duplicate_active
        self.active_session_ids: set[str] = set()
        self.spawns = []

    def spawn(self, coroutine, *, session_id):
        if self.reject_duplicate_active and session_id in self.active_session_ids:
            raise RuntimeError(f"chat run already active for session {session_id}")
        self.active_session_ids.add(session_id)
        self.spawns.append({"coroutine": coroutine, "session_id": session_id})

    async def drain(self) -> None:
        while self.spawns:
            spawn = self.spawns.pop(0)
            await spawn["coroutine"]
            self.active_session_ids.discard(spawn["session_id"])


def _agentscope_config(**overrides) -> AgentScopeRuntimeConfig:
    values = {
        "user_id": "alice",
        "redis_url": "redis://localhost:6379/0",
        "workspace_root": Path("/tmp/vla-agent-workspace"),
        "dashscope_api_key": "test-key",
        "dashscope_base_url": None,
        "default_model": "qwen-default",
        "router_model": "qwen-router",
        "navigation_model": "qwen-navigation",
    }
    values.update(overrides)
    return AgentScopeRuntimeConfig(**values)


def _runtime(
    *,
    storage: FakeAgentScopeStorage | None = None,
    chat_run_registry: FakeChatRunRegistry | None = None,
    message_bus=None,
    workspace_root: Path | None = None,
) -> AgentScopeRuntime:
    storage = storage or FakeAgentScopeStorage()
    chat_service = FakeChatService(storage)
    state = SimpleNamespace(chat_service=chat_service)
    if chat_run_registry is not None:
        state.chat_run_registry = chat_run_registry
    return AgentScopeRuntime(
        config=_agentscope_config(
            workspace_root=workspace_root or Path("/tmp/vla-agent-workspace"),
        ),
        storage=storage,
        message_bus=message_bus or object(),
        workspace_manager=object(),
        app=SimpleNamespace(state=state),
    )


def _plan_bound_human_runtime(tmp_path: Path, chat_run_registry: FakeChatRunRegistry):
    workspace_root = tmp_path / "workspace"
    db_path = workspace_root / "navigation-tasks.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_task_attempt(
        request="Process navigation data",
        target="20260710",
        date="20260710",
        segments=["segment-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id="web-1",
        agentscope_session_id="as-session-1",
    ).task
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        1,
        FinishProcessingPlanInput.model_validate(
            {
                "decisions": {
                    "localization": {
                        "source": "odom",
                        "conversion": "odom_to_ins",
                        "reason": "observed",
                        "evidence_refs": ["evidence:localization"],
                    },
                    "gridmap": {
                        "source": "existing_gridmap",
                        "reason": "observed",
                        "evidence_refs": ["evidence:gridmap"],
                    },
                    "calibration": {
                        "mode": "hardcoded_with_user_confirmation",
                        "selected_sensor_source": "NoobScenes/params/selected/sensors",
                        "requires_user_confirmation": True,
                        "reason": "observed",
                        "evidence_refs": ["evidence:calibration"],
                    },
                },
                "steps": [
                    {
                        "step_id": "confirm",
                        "action": "confirm_navigation_calibration_params",
                        "variant": "default",
                        "arguments": {},
                        "depends_on": [],
                        "failure_policy": "stop",
                        "decision_refs": ["calibration"],
                    }
                ],
            }
        ),
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record(
            reply_id="reply-1",
            tool_call_id="confirm-1",
            tool_name="request_human_decision",
            tool_input={"plan_id": plan.plan_id, "step_id": "confirm"},
        )
    )
    runtime = _runtime(
        storage=storage,
        chat_run_registry=chat_run_registry,
        workspace_root=workspace_root,
    )
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")
    decision = {
        "action": "confirm",
        "request_id": f"{plan.plan_id}:confirm",
        "plan_id": plan.plan_id,
        "step_id": "confirm",
        "tool_call_id": "confirm-1",
        "reply_id": "reply-1",
    }
    return runtime, plan_store, plan, decision


def _message_text(message) -> str:
    return message.content[0].text


def _agentscope_session_record(
    *,
    reply_id: str = "reply-1",
    tool_call_id: str = "tool-call-1",
    tool_name: str = "request_human_decision",
    tool_state=ToolCallState.SUBMITTED,
    tool_input: str | dict = "{}",
):
    if isinstance(tool_input, dict):
        tool_input = json.dumps(tool_input, ensure_ascii=False)
    return SimpleNamespace(
        state=SimpleNamespace(
            reply_id=reply_id,
            context=[
                Msg(
                    name="assistant",
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id=tool_call_id,
                            name=tool_name,
                            input=tool_input,
                            state=tool_state,
                        )
                    ],
                )
            ],
        )
    )


def test_navigation_rule_fallback_is_narrow_and_explicit() -> None:
    assert is_high_confidence_navigation_request("请处理 20270605 的室外导航数据并生成标注")
    assert is_high_confidence_navigation_request("同步 rosbag db3 odom 和 gridmap 数据")
    assert is_high_confidence_navigation_request("导航 trajectory tracking projection annotation")
    assert is_high_confidence_navigation_request("20270605 tracking projection annotation for nav data")
    assert not is_high_confidence_navigation_request("你好，今天怎么样")
    assert not is_high_confidence_navigation_request("继续")
    assert not is_high_confidence_navigation_request("bug tracking")
    assert not is_high_confidence_navigation_request("database projection")
    assert not is_high_confidence_navigation_request("annotate this chart")


def test_web_navigation_prompt_describes_session_attempt_boundaries() -> None:
    prompt = navigation_agent_prompt()

    assert "same-session continuation" in prompt
    assert "new Web session is a fresh task attempt" in prompt
    assert "must investigate again" in prompt
    assert "runtime-selected phase" not in prompt

@pytest.mark.asyncio
async def test_runtime_submit_user_message_starts_navigation_requests_with_main_router() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    turn_id = await runtime.submit_user_message(
        web_session_id="web-1",
        message="同步 rosbag db3 odom 和 gridmap 数据",
    )

    assert turn_id.startswith("turn_")
    assert runtime.web_sessions == {"web-1": ("main-router-agent", "web-1__main-router-agent")}
    session = runtime.storage.sessions[0]
    assert session["user_id"] == "alice"
    assert session["agent_id"] == "main-router-agent"
    assert session["id"] == "web-1__main-router-agent"
    assert session["config"].workspace_id == "workspace-web-1"
    assert session["config"].name == "web-1"
    assert session["config"].chat_model_config.type == "dashscope_chat"
    assert session["config"].chat_model_config.credential_id == "dashscope-env"
    assert session["config"].chat_model_config.model == "qwen-router"
    assert session["config"].chat_model_config.parameters == {"parallel_tool_calls": False}
    assert runtime.app.state.chat_service.runs == []
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == [
        "web-1__main-router-agent"
    ]
    await chat_run_registry.drain()
    assert len(runtime.app.state.chat_service.runs) == 1
    run = runtime.app.state.chat_service.runs[0]
    assert run["user_id"] == "alice"
    assert run["session_id"] == "web-1__main-router-agent"
    assert run["agent_id"] == "main-router-agent"
    assert run["message"].name == "user"
    assert _message_text(run["message"]) == "同步 rosbag db3 odom 和 gridmap 数据"


@pytest.mark.asyncio
async def test_runtime_submit_user_message_routes_ordinary_request_to_main_router() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")

    assert runtime.web_sessions == {"web-1": ("main-router-agent", "web-1__main-router-agent")}
    session = runtime.storage.sessions[0]
    assert session["agent_id"] == "main-router-agent"
    assert session["id"] == "web-1__main-router-agent"
    assert session["config"].chat_model_config.model == "qwen-router"
    assert runtime.app.state.chat_service.runs == []
    await chat_run_registry.drain()
    run = runtime.app.state.chat_service.runs[0]
    assert run["session_id"] == "web-1__main-router-agent"
    assert run["agent_id"] == "main-router-agent"
    assert run["message"].name == "user"
    assert _message_text(run["message"]) == "你好"


@pytest.mark.asyncio
async def test_runtime_submit_user_message_reuses_session_for_same_agent() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")
    await chat_run_registry.drain()
    await runtime.submit_user_message(web_session_id="web-1", message="再聊一下")
    await chat_run_registry.drain()

    assert len(runtime.storage.sessions) == 1
    assert [run["session_id"] for run in runtime.app.state.chat_service.runs] == [
        "web-1__main-router-agent",
        "web-1__main-router-agent",
    ]


@pytest.mark.asyncio
async def test_runtime_submit_user_message_keeps_main_router_session_for_navigation_followup() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")
    await chat_run_registry.drain()
    await runtime.submit_user_message(web_session_id="web-1", message="处理导航数据")
    await chat_run_registry.drain()

    assert len(runtime.storage.sessions) == 1
    assert runtime.web_sessions == {"web-1": ("main-router-agent", "web-1__main-router-agent")}
    assert [session["agent_id"] for session in runtime.storage.sessions] == [
        "main-router-agent",
    ]
    assert [session["id"] for session in runtime.storage.sessions] == [
        "web-1__main-router-agent",
    ]


@pytest.mark.asyncio
async def test_runtime_start_navigation_agent_task_switches_mapping_and_spawns_navigation_run(
    tmp_path: Path,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        chat_run_registry=chat_run_registry,
        workspace_root=tmp_path / "workspace",
    )
    handoff_message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270605 的室外数据",
        target="20270605",
        date="20270605",
        scene_mode="out",
        clips=[],
        reason="用户请求导航处理",
        response_language="Chinese",
    )

    await runtime.submit_user_message(web_session_id="web-1", message="处理 20270605 的室外数据")
    await chat_run_registry.drain()
    started = await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=handoff_message,
    )

    assert started.agentscope_session_id == "web-1__navigation-data-agent"
    assert started.task_id.startswith("nav_")
    assert runtime.web_sessions == {"web-1": ("navigation-data-agent", "web-1__navigation-data-agent")}
    assert [session["agent_id"] for session in runtime.storage.sessions] == [
        "main-router-agent",
        "navigation-data-agent",
    ]
    assert runtime.storage.sessions[1]["config"].chat_model_config.model == "qwen-navigation"
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == [
        "web-1__navigation-data-agent"
    ]
    await chat_run_registry.drain()
    run = runtime.app.state.chat_service.runs[-1]
    assert run["session_id"] == "web-1__navigation-data-agent"
    assert run["agent_id"] == "navigation-data-agent"
    assert _message_text(run["message"]).startswith(handoff_message)
    assert "Durable navigation state anchor (authoritative):" in _message_text(run["message"])


@pytest.mark.asyncio
async def test_runtime_router_and_navigation_runs_inherit_the_authoritative_turn(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    submission = store.begin_user_turn(web_session.id, "处理 20270605 的室外数据")
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        chat_run_registry=chat_run_registry,
        workspace_root=tmp_path / "workspace",
    )
    runtime.set_web_session_store(store)

    await runtime.submit_user_message(
        web_session_id=web_session.id,
        message="处理 20270605 的室外数据",
        turn_id=submission.turn.id,
    )
    await chat_run_registry.drain()
    await runtime.start_navigation_agent_task(
        web_session_id=web_session.id,
        message=agentscope_runtime_module._navigation_handoff_message(
            request="处理 20270605 的室外数据",
            target="20270605",
            date="20270605",
            scene_mode="out",
            clips=[],
            reason="用户请求导航处理",
            response_language="Chinese",
        ),
    )

    mappings = store.list_agentscope_session_mappings()
    assert [mapping.agent_id for mapping in mappings] == [
        "main-router-agent",
        "navigation-data-agent",
    ]
    assert {mapping.active_turn_id for mapping in mappings} == {submission.turn.id}
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_async_run_failure_marks_authoritative_turn_failed(
    tmp_path: Path,
) -> None:
    class FailingChatService(FakeChatService):
        async def run(self, *, user_id, session_id, agent_id, input_msg):
            raise RuntimeError("model transport failed")

    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("failing run")
    submission = store.begin_user_turn(web_session.id, "处理数据")
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry, workspace_root=tmp_path / "workspace")
    runtime.app.state.chat_service = FailingChatService()
    runtime.set_web_session_store(store)

    await runtime.submit_user_message(
        web_session_id=web_session.id,
        message="处理数据",
        turn_id=submission.turn.id,
    )
    with pytest.raises(RuntimeError, match="model transport failed"):
        await chat_run_registry.drain()

    detail = store.get_session(web_session.id)
    assert detail is not None
    assert detail.turns[0].status == "failed"
    assert detail.events[-1].type == "turn_state"
    assert detail.events[-1].payload["status"] == "failed"


@pytest.mark.asyncio
async def test_runtime_submit_user_message_routes_to_active_navigation_agent_after_handoff(
    tmp_path: Path,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        chat_run_registry=chat_run_registry,
        workspace_root=tmp_path / "workspace",
    )

    await runtime.submit_user_message(web_session_id="web-1", message="处理 20270623 的导航数据")
    await chat_run_registry.drain()
    await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=agentscope_runtime_module._navigation_handoff_message(
            request="处理 20270623 的导航数据",
            target="20270623",
            date="20270623",
            scene_mode=None,
            clips=[],
            reason="用户请求导航处理",
            response_language="Chinese",
        ),
    )
    await chat_run_registry.drain()

    await runtime.submit_user_message(web_session_id="web-1", message="继续执行 室内")

    assert runtime.web_sessions == {"web-1": ("navigation-data-agent", "web-1__navigation-data-agent")}
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns][-1] == "web-1__navigation-data-agent"
    await chat_run_registry.drain()
    run = runtime.app.state.chat_service.runs[-1]
    assert run["session_id"] == "web-1__navigation-data-agent"
    assert run["agent_id"] == "navigation-data-agent"
    assert _message_text(run["message"]).startswith("继续执行 室内")
    assert "Durable navigation state anchor (authoritative):" in _message_text(run["message"])


@pytest.mark.asyncio
async def test_runtime_start_navigation_agent_task_creates_attempt_without_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(tmp_path / "vladatasets"))
    chat_run_registry = FakeChatRunRegistry()
    chat_service = FakeChatService()
    runtime = AgentScopeRuntime(
        config=_agentscope_config(workspace_root=tmp_path),
        storage=FakeAgentScopeStorage(),
        message_bus=object(),
        workspace_manager=object(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_service=chat_service,
                chat_run_registry=chat_run_registry,
            )
        ),
    )
    message = agentscope_runtime_module._navigation_handoff_message(
        request="请帮我处理一下20270605的导航数据，室外数据。只处理20260605_152856就可以。",
        target="20260605_152856",
        date="20270605",
        scene_mode="outdoor",
        clips=["20260605_152856"],
        reason="用户给出了日期、室外场景和指定 clip",
        response_language="Chinese",
    )

    started = await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=message,
    )
    task = runtime._navigation_task_store().find_by_session(
        web_session_id="web-1",
        agentscope_session_id=started.agentscope_session_id,
    )

    assert started.agentscope_session_id == "web-1__navigation-data-agent"
    assert started.task_id == task.task_id
    assert not (tmp_path / "navigation-plan-drafts").exists()
    assert task is not None
    assert task.request.startswith("请帮我处理")
    assert task.target == "20260605_152856"
    assert task.dry_run is False
    assert task.status == NavigationTaskStatus.ACTIVE
    assert task.guidance_revision == 0
    revision = SqliteNavigationObservationStore(
        tmp_path / "navigation-tasks.sqlite"
    ).latest(task.task_id)
    assert revision is None
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_start_navigation_agent_task_uses_trusted_dry_run_configuration(
    tmp_path: Path,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry, workspace_root=tmp_path)
    runtime.config = _agentscope_config(
        workspace_root=tmp_path,
        navigation_dry_run=True,
    )
    message = agentscope_runtime_module._navigation_handoff_message(
        request="请帮我 dry run 处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode=None,
        clips=[],
        reason="用户要求 dry run",
        response_language="Chinese",
    )

    started = await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=message,
    )
    task = runtime._navigation_task_store().find_by_session(
        web_session_id="web-1",
        agentscope_session_id=started.agentscope_session_id,
    )

    assert task is not None
    assert task.dry_run is True
    assert not (tmp_path / "navigation-plan-drafts").exists()
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_same_date_cross_web_handoff_creates_distinct_attempts(tmp_path: Path) -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry, workspace_root=tmp_path)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode=None,
        clips=[],
        reason="owner test",
        response_language="Chinese",
    )
    owner = await runtime.start_navigation_agent_task(
        web_session_id="web-owner",
        message=message,
    )
    foreign = await runtime.start_navigation_agent_task(
        web_session_id="web-foreign",
        message=message,
    )

    assert owner.task_id != foreign.task_id
    assert runtime.web_sessions["web-owner"][0] == "navigation-data-agent"
    assert runtime.web_sessions["web-foreign"][0] == "navigation-data-agent"
    await chat_run_registry.drain()


@pytest.mark.parametrize(
    "older_status",
    [
        NavigationTaskStatus.COMPLETED,
        NavigationTaskStatus.WAITING_USER,
        NavigationTaskStatus.FAILED,
    ],
)
@pytest.mark.asyncio
async def test_runtime_completed_waiting_or_failed_attempt_does_not_block_handoff(
    tmp_path: Path,
    older_status: NavigationTaskStatus,
) -> None:
    registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=registry, workspace_root=tmp_path)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode=None,
        clips=[],
        reason="status test",
        response_language="Chinese",
    )
    older = await runtime.start_navigation_agent_task(
        web_session_id="web-old",
        message=message,
    )
    runtime._navigation_task_store().update_task_for_session(
        older.task_id,
        web_session_id="web-old",
        agentscope_session_id=older.agentscope_session_id,
        status=older_status.value,
    )

    newer = await runtime.start_navigation_agent_task(
        web_session_id="web-new",
        message=message,
    )

    assert newer.task_id != older.task_id
    await registry.drain()


@pytest.mark.asyncio
async def test_completed_history_handoff_is_truthful_and_never_reports_cross_session_error(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=registry, workspace_root=tmp_path)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode=None,
        clips=["segment-a"],
        reason="completed history regression",
        response_language="Chinese",
    )
    old = await runtime.start_navigation_agent_task(
        web_session_id="web-old",
        message=message,
    )
    runtime._navigation_task_store().update_task_for_session(
        old.task_id,
        web_session_id="web-old",
        agentscope_session_id=old.agentscope_session_id,
        status=NavigationTaskStatus.COMPLETED.value,
    )
    tool = agentscope_runtime_module.NavigationHandoffTool(
        runtime=runtime,
        web_session_id="web-new",
    )

    result = await tool(
        request="处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode="unknown",
        clips=["segment-a"],
        reason="new session continuation",
        missing_fields=[],
        confidence="high",
        response_language="Chinese",
    )

    assert result.state == ToolResultState.SUCCESS
    assert result.metadata["ok"] is True
    assert result.metadata["started"] is True
    assert result.metadata["task_id"] != old.task_id
    assert "belongs to another Web session" not in result.content[0].text
    assert "不属于" not in result.content[0].text
    await registry.drain()


@pytest.mark.asyncio
async def test_real_running_writer_returns_bounded_truthful_busy_handoff(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        workspace_root=tmp_path,
    )
    task_store = runtime._navigation_task_store()
    writer = task_store.create_task_attempt(
        request="write navigation products",
        target="20270605/segment-a",
        date="20270605",
        segments=["segment-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id="web-writer",
        agentscope_session_id="as-writer",
    ).task
    plan_store = SqliteNavigationPlanRepository(tmp_path / "navigation-tasks.sqlite")
    plan = plan_store.activate(
        writer,
        "finish_processing",
        0,
        FinishProcessingPlanInput.model_validate(
            {
                "decisions": {
                    "localization": {
                        "source": "odom",
                        "conversion": "odom_to_ins",
                        "reason": "observed",
                        "evidence_refs": ["evidence:localization"],
                    },
                    "gridmap": {
                        "source": "existing_gridmap",
                        "reason": "observed",
                        "evidence_refs": ["evidence:gridmap"],
                    },
                    "calibration": {
                        "mode": "hardcoded_with_user_confirmation",
                        "selected_sensor_source": "NoobScenes/params/selected/sensors",
                        "requires_user_confirmation": True,
                        "reason": "observed",
                        "evidence_refs": ["evidence:calibration"],
                    },
                },
                "steps": [
                    {
                        "step_id": "assemble",
                        "action": "assemble_finish_temp",
                        "variant": "default",
                        "arguments": {},
                        "depends_on": [],
                        "failure_policy": "stop",
                        "decision_refs": ["calibration"],
                    }
                ],
            }
        ),
        expected_web_session_id="web-writer",
        expected_agentscope_session_id="as-writer",
    )
    assert plan_store.claim_step(
        plan.plan_id,
        "assemble",
        "assemble_finish_temp",
        expected_web_session_id="web-writer",
        expected_agentscope_session_id="as-writer",
    ) is StepClaimOutcome.CLAIMED
    runtime.web_sessions["web-blocked"] = ("main-router-agent", "as-router")
    tool = agentscope_runtime_module.NavigationHandoffTool(
        runtime=runtime,
        web_session_id="web-blocked",
    )

    result = await tool(
        request="处理 20270605 的 segment-a",
        target="20270605/segment-a",
        date="20270605",
        scene_mode="outdoor",
        clips=["segment-a"],
        reason="same target",
        missing_fields=[],
        confidence="high",
        response_language="Chinese",
    )

    assert result.state == ToolResultState.ERROR
    assert result.metadata == {
        "ok": False,
        "started": False,
        "error_type": "navigation_data_busy",
        "message": "该目标当前有正在运行的数据写入操作。",
    }
    assert len(result.content[0].text) <= 4_000
    assert runtime.web_sessions == {
        "web-blocked": ("main-router-agent", "as-router")
    }
    assert runtime._navigation_task_store().find_by_session(
        web_session_id="web-blocked",
        agentscope_session_id="web-blocked__navigation-data-agent",
    ) is None


@pytest.mark.asyncio
async def test_runtime_busy_preflight_does_not_change_web_mapping(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(workspace_root=tmp_path)
    runtime.web_sessions["web-1"] = ("main-router-agent", "as-main")
    services = runtime._navigation_services()
    monkeypatch.setattr(
        services.task_store,
        "find_running_target_writer",
        lambda **_kwargs: SimpleNamespace(task_id="nav-writer"),
    )
    monkeypatch.setattr(runtime, "_navigation_services", lambda: services)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=[],
        reason="busy test",
        response_language="Chinese",
    )

    with pytest.raises(agentscope_runtime_module.NavigationDataBusyError):
        await runtime.start_navigation_agent_task(web_session_id="web-1", message=message)

    assert runtime.web_sessions == {"web-1": ("main-router-agent", "as-main")}
    assert services.task_store.find_by_session(
        web_session_id="web-1",
        agentscope_session_id="web-1__navigation-data-agent",
    ) is None


@pytest.mark.asyncio
async def test_runtime_failed_navigation_entry_restores_absent_mapping_and_does_not_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        chat_run_registry=chat_run_registry,
        workspace_root=tmp_path,
    )
    runtime.set_web_session_store(store)

    with pytest.raises(ValueError, match="structured handoff"):
        await runtime.start_navigation_agent_task(
            web_session_id=web_session.id,
            message="invalid handoff",
        )

    assert runtime.web_sessions == {}
    assert store.get_agentscope_session_mapping(web_session.id) is None
    assert chat_run_registry.spawns == []


@pytest.mark.asyncio
async def test_runtime_failed_navigation_entry_restores_non_navigation_mapping_and_does_not_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("混合会话")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-main",
    )
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        chat_run_registry=chat_run_registry,
        workspace_root=tmp_path,
    )
    runtime.set_web_session_store(store)

    def fail_entry(_message):
        raise RuntimeError("handoff parsing failed")

    monkeypatch.setattr(
        agentscope_runtime_module,
        "parse_navigation_task_entry",
        fail_entry,
    )
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=[],
        reason="用户请求导航处理",
        response_language="Chinese",
    )

    with pytest.raises(RuntimeError, match="handoff parsing failed"):
        await runtime.start_navigation_agent_task(
            web_session_id=web_session.id,
            message=message,
        )

    assert runtime.web_sessions == {}
    mapping = store.get_agentscope_session_mapping(web_session.id)
    assert mapping is not None
    assert mapping.agent_id == "main-router-agent"
    assert mapping.agentscope_session_id == "as-main"
    assert chat_run_registry.spawns == []


@pytest.mark.asyncio
async def test_runtime_start_failure_deletes_new_attempt_and_restores_mapping(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        workspace_root=tmp_path,
    )
    runtime.web_sessions["web-1"] = ("main-router-agent", "as-main")

    async def fail_start(**_kwargs):
        raise RuntimeError("schedule failed")

    monkeypatch.setattr(runtime, "_start_agent_run", fail_start)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=[],
        reason="用户请求导航处理",
        response_language="Chinese",
    )

    with pytest.raises(RuntimeError, match="schedule failed"):
        await runtime.start_navigation_agent_task(web_session_id="web-1", message=message)

    assert runtime.web_sessions == {"web-1": ("main-router-agent", "as-main")}
    assert runtime._navigation_task_store().find_by_session(
        web_session_id="web-1",
        agentscope_session_id="web-1__navigation-data-agent",
    ) is None


@pytest.mark.asyncio
async def test_runtime_task_creation_failure_restores_mapping(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(workspace_root=tmp_path)
    runtime.web_sessions["web-1"] = ("main-router-agent", "as-main")
    services = runtime._navigation_services()

    def fail_creation(**_kwargs):
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(services.task_store, "create_task_attempt", fail_creation)
    monkeypatch.setattr(runtime, "_navigation_services", lambda: services)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=[],
        reason="failure test",
        response_language="Chinese",
    )

    with pytest.raises(RuntimeError, match="task creation failed"):
        await runtime.start_navigation_agent_task(web_session_id="web-1", message=message)

    assert runtime.web_sessions == {"web-1": ("main-router-agent", "as-main")}


@pytest.mark.asyncio
async def test_runtime_exact_retry_ignores_own_writer_and_does_not_spawn_again(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=registry, workspace_root=tmp_path)
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=["segment-a"],
        reason="exact retry",
        response_language="Chinese",
    )
    first = await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=message,
    )
    db_path = tmp_path / "navigation-tasks.sqlite"
    SqliteNavigationPlanRepository(db_path)
    timestamp = "2026-07-13T00:00:00.000+00:00"
    plan_id = f"plan-{first.task_id}"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO navigation_plans (
                   plan_id, task_id, phase, plan_revision, contract_version,
                   observation_revision, plan_json, validation_summary_json,
                   status, invalidation_reason, created_at, updated_at
               ) VALUES (?, ?, 'extract_sync', 1, 'test', 1, ?, '{}',
                         'active', NULL, ?, ?)""",
            (
                plan_id,
                first.task_id,
                json.dumps(
                    {
                        "steps": [
                            {
                                "step_id": "writer",
                                "action": "prepare_raw_data",
                            }
                        ]
                    }
                ),
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """INSERT INTO navigation_task_steps (
                   id, task_id, phase, step_id, tool_name, status,
                   plan_id, plan_revision, sequence, started_at
               ) VALUES (?, ?, 'extract_sync', 'writer', 'prepare_raw_data',
                         'running', ?, 1, 1, ?)""",
            (f"ledger-{first.task_id}", first.task_id, plan_id, timestamp),
        )
    replay = await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=message,
    )

    assert replay == first
    assert len(registry.spawns) == 1
    await registry.drain()


@pytest.mark.asyncio
async def test_runtime_anchor_failure_cleans_registration_mapping_and_attempt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=registry, workspace_root=tmp_path)
    session_id = "web-1__navigation-data-agent"
    monkeypatch.setattr(
        runtime,
        "_navigation_durable_state_anchor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("anchor failed")
        ),
    )
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=[],
        reason="anchor failure",
        response_language="Chinese",
    )

    with pytest.raises(RuntimeError, match="anchor failed"):
        await runtime.start_navigation_agent_task(web_session_id="web-1", message=message)

    assert runtime.web_sessions == {}
    assert runtime.run_cancellation(session_id) is None
    assert registry.spawns == []
    assert runtime._navigation_task_store().find_by_session(
        web_session_id="web-1",
        agentscope_session_id=session_id,
    ) is None


@pytest.mark.asyncio
async def test_runtime_cursor_persistence_failure_after_spawn_is_nonfatal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[("9-0", {"type": "REPLY_END"})]
    )
    registry = FakeChatRunRegistry()
    runtime = _runtime(
        chat_run_registry=registry,
        message_bus=message_bus,
        workspace_root=tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "_save_web_session_event_cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("cursor persistence failed")
        ),
    )
    message = agentscope_runtime_module._navigation_handoff_message(
        request="处理 20270623 的导航数据",
        target="20270623",
        date="20270623",
        scene_mode=None,
        clips=[],
        reason="cursor failure",
        response_language="Chinese",
    )

    started = await runtime.start_navigation_agent_task(
        web_session_id="web-1",
        message=message,
    )

    assert runtime.web_sessions == {
        "web-1": ("navigation-data-agent", started.agentscope_session_id)
    }
    assert runtime._navigation_task_store().get_task(started.task_id) is not None
    assert runtime.run_cancellation(started.agentscope_session_id) is not None
    assert len(registry.spawns) == 1
    assert runtime.event_cursors == {}
    await registry.drain()


def test_runtime_anchor_does_not_expose_phase_without_accepted_plan(
    tmp_path: Path,
) -> None:
    runtime = _runtime(workspace_root=tmp_path)
    session_id = "web-1__navigation-data-agent"
    task = runtime._navigation_task_store().create_task_attempt(
        request="处理导航数据",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-1",
        agentscope_session_id=session_id,
    ).task
    runtime._navigation_task_store().update_task_for_session(
        task.task_id,
        web_session_id="web-1",
        agentscope_session_id=session_id,
        phase="extract_sync",
    )

    anchor = runtime._navigation_durable_state_anchor(
        session_id,
        web_session_id="web-1",
    )

    assert anchor["accepted_plan_id"] is None
    assert anchor["accepted_plan_revision"] is None
    assert "phase" not in anchor


@pytest.mark.asyncio
async def test_web_navigation_assembly_uses_middleware_not_basic_domain_tools(
    monkeypatch,
    tmp_path,
):
    runtime = _runtime(workspace_root=tmp_path)
    await runtime.ensure_bootstrapped()
    session_id = await runtime.ensure_web_session(
        "web-1",
        agent_id=runtime.config.navigation_agent_id,
        model=runtime.config.navigation_model,
    )
    monkeypatch.setattr(
        runtime,
        "_navigation_tools_for_session",
        lambda **_kwargs: pytest.fail("Web assembly called the direct flat adapter"),
    )
    extra_tools = build_extra_agent_tools_factory(runtime.config, runtime=runtime)
    extra_middlewares = build_extra_agent_middlewares_factory(
        runtime.config,
        runtime=runtime,
    )
    tools = await extra_tools(
        runtime.config.user_id,
        runtime.config.navigation_agent_id,
        session_id,
    )
    middlewares = await extra_middlewares(
        runtime.config.user_id,
        runtime.config.navigation_agent_id,
        session_id,
    )

    assert tools == []
    assert len(middlewares) == 1
    middleware = middlewares[0]
    assert isinstance(middleware, NavigationToolSurfaceMiddleware)
    assert middleware._web_session_id == "web-1"
    assert middleware._agentscope_session_id == session_id
    assert (
        middleware._services.task_store.db_path
        == tmp_path / "navigation-tasks.sqlite"
    )


@pytest.mark.asyncio
async def test_navigation_handoff_tool_allows_unknown_scene_mode() -> None:
    runtime = CapturingNavigationTaskRuntime()
    tool = agentscope_runtime_module.NavigationHandoffTool(
        runtime=runtime,
        web_session_id="web-1",
    )

    assert "scene_mode" not in tool.input_schema["required"]
    assert "clips" not in tool.input_schema["required"]

    result = await tool(
        request="请处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode="unknown",
        reason="用户给出了日期但未说明室内室外",
        missing_fields=[],
        confidence="high",
        response_language="Chinese",
    )
    payload = agentscope_runtime_module._structured_handoff_payload_from_message(
        runtime.started_tasks[0]["message"]
    )

    assert result.state == ToolResultState.SUCCESS
    assert runtime.started_tasks[0]["web_session_id"] == "web-1"
    assert payload["date"] == "20270605"
    assert payload["scene_mode"] is None
    assert payload["segments"] is None
    assert runtime.handoffs[-1]["started"] is True


@pytest.mark.asyncio
async def test_navigation_handoff_tool_does_not_expose_dry_run_payload() -> None:
    runtime = CapturingNavigationTaskRuntime()
    tool = agentscope_runtime_module.NavigationHandoffTool(
        runtime=runtime,
        web_session_id="web-1",
    )

    assert "dry_run" not in tool.input_schema["properties"]

    result = await tool(
        request="请处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode="unknown",
        reason="用户请求导航处理",
        missing_fields=[],
        confidence="high",
        response_language="Chinese",
    )
    payload = agentscope_runtime_module._structured_handoff_payload_from_message(
        runtime.started_tasks[0]["message"]
    )

    assert result.state == ToolResultState.SUCCESS
    assert "dry_run" not in payload
    assert "dry_run" not in runtime.handoffs[-1]


@pytest.mark.asyncio
async def test_runtime_submit_user_message_reuses_deterministic_session_id_after_restart() -> None:
    storage = FakeAgentScopeStorage()
    first_registry = FakeChatRunRegistry()
    first_runtime = _runtime(storage=storage, chat_run_registry=first_registry)
    second_registry = FakeChatRunRegistry()
    second_runtime = _runtime(storage=storage, chat_run_registry=second_registry)

    await first_runtime.submit_user_message(web_session_id="web-1", message="你好")
    await first_registry.drain()
    await second_runtime.submit_user_message(web_session_id="web-1", message="你好 again")
    await second_registry.drain()

    assert [session["id"] for session in storage.sessions] == [
        "web-1__main-router-agent",
        "web-1__main-router-agent",
    ]
    assert second_runtime.web_sessions == {"web-1": ("main-router-agent", "web-1__main-router-agent")}


@pytest.mark.asyncio
async def test_runtime_persists_web_to_agentscope_session_mapping(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    storage = FakeAgentScopeStorage()
    first_runtime = _runtime(storage=storage, chat_run_registry=FakeChatRunRegistry())
    first_runtime.set_web_session_store(store)

    agentscope_session_id = await first_runtime.ensure_web_session(
        web_session.id,
        agent_id="navigation-data-agent",
        model="qwen-navigation",
    )

    assert agentscope_session_id == f"{web_session.id}__navigation-data-agent"
    mapping = store.get_agentscope_session_mapping(web_session.id)
    assert mapping is not None
    assert mapping.agent_id == "navigation-data-agent"
    assert mapping.agentscope_session_id == agentscope_session_id

    second_runtime = _runtime(storage=storage, chat_run_registry=FakeChatRunRegistry())
    second_runtime.set_web_session_store(store)

    assert second_runtime._web_session_mapping(web_session.id) == (
        "navigation-data-agent",
        agentscope_session_id,
    )


@pytest.mark.asyncio
async def test_runtime_preserves_per_agent_mapping_cursor_when_active_agent_changes(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("混合会话")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-main",
    )
    store.save_agentscope_event_cursor("as-main", "2-0")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-nav",
    )

    assert store.get_agentscope_session_mapping(web_session.id).agent_id == "navigation-data-agent"
    main_mapping = store.get_agentscope_session_mapping_for_agent(web_session.id, "main-router-agent")
    assert main_mapping is not None
    assert main_mapping.agentscope_session_id == "as-main"
    assert main_mapping.event_cursor == "2-0"

    runtime = _runtime(chat_run_registry=FakeChatRunRegistry())
    runtime.set_web_session_store(store)

    session_id = await runtime.ensure_web_session(
        web_session.id,
        agent_id="main-router-agent",
        model="qwen-router",
    )

    assert session_id == "as-main"
    assert runtime._event_cursor("as-main") == "2-0"
    assert store.get_agentscope_session_mapping(web_session.id).agent_id == "main-router-agent"


@pytest.mark.asyncio
async def test_runtime_submit_user_message_requires_chat_run_registry() -> None:
    runtime = _runtime(chat_run_registry=None)

    with pytest.raises(RuntimeError, match="chat_run_registry"):
        await runtime.submit_user_message(web_session_id="web-1", message="你好")

    assert runtime.app.state.chat_service.runs == []


@pytest.mark.asyncio
async def test_runtime_interrupt_web_session_returns_false_without_mapping() -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)

    interrupted = await runtime.interrupt_web_session(web_session_id="web-1")

    assert interrupted is False
    assert message_bus.cancelled_sessions == []


@pytest.mark.asyncio
async def test_runtime_interrupt_web_session_publishes_agentscope_cancel() -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    runtime.web_sessions["web-1"] = ("main-router-agent", "as-session-1")

    interrupted = await runtime.interrupt_web_session(web_session_id="web-1")

    assert interrupted is True
    assert message_bus.cancelled_sessions == ["as-session-1"]
    assert runtime.web_sessions["web-1"] == ("main-router-agent", "as-session-1")


@pytest.mark.asyncio
async def test_runtime_interrupt_web_session_cancels_registered_context() -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    cancellation = CancellationContext()
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")
    runtime.register_run_cancellation("as-session-1", cancellation)

    interrupted = await runtime.interrupt_web_session(web_session_id="web-1")

    assert interrupted is True
    assert cancellation.cancelled is True
    assert message_bus.cancelled_sessions == ["as-session-1"]
    assert runtime.web_sessions["web-1"] == ("navigation-data-agent", "as-session-1")


@pytest.mark.asyncio
async def test_runtime_interrupt_cancels_every_mapping_bound_to_the_active_turn(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("multi agent turn")
    submission = store.begin_user_turn(session.id, "处理导航数据")
    for agent_id, agentscope_session_id in (
        ("main-router-agent", "as-router"),
        ("navigation-data-agent", "as-navigation"),
    ):
        store.save_agentscope_session_mapping(
            session.id,
            agent_id=agent_id,
            agentscope_session_id=agentscope_session_id,
            active_turn_id=submission.turn.id,
        )
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    runtime.set_web_session_store(store)
    runtime.web_sessions[session.id] = ("navigation-data-agent", "as-navigation")
    router_cancellation = CancellationContext()
    navigation_cancellation = CancellationContext()
    runtime.register_run_cancellation("as-router", router_cancellation)
    runtime.register_run_cancellation("as-navigation", navigation_cancellation)

    interrupted = await runtime.interrupt_web_session(web_session_id=session.id)

    assert interrupted is True
    assert router_cancellation.cancelled is True
    assert navigation_cancellation.cancelled is True
    assert message_bus.cancelled_sessions == ["as-router", "as-navigation"]


def test_runtime_retains_cancellation_until_background_operation_finishes() -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    cancellation = CancellationContext()
    runtime.register_run_cancellation("as-session-1", cancellation)

    with cancellation.track_background_operation():
        runtime.clear_run_cancellation("as-session-1", cancellation)
        assert runtime.run_cancellation("as-session-1") is cancellation

    assert runtime.run_cancellation("as-session-1") is None


def test_runtime_reconciles_unknown_running_background_tool_as_failed() -> None:
    recovered: list[tuple] = []
    step = SimpleNamespace(
        action="extract_and_sync_navigation_data",
        step_id="extract_sync",
        status="running",
    )
    snapshot = SimpleNamespace(
        overview=SimpleNamespace(steps=[step]),
        staged_result=None,
        active_plan=SimpleNamespace(plan_id="plan-1"),
    )
    plan_store = SimpleNamespace(
        read_execution_snapshot=lambda **_kwargs: snapshot,
        recover_running_step_without_result=lambda *args, **kwargs: recovered.append(
            (args, kwargs)
        ),
    )
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime._navigation_plan_store = lambda: plan_store

    status = runtime.reconcile_background_tool_status(
        web_session_id="web-1",
        agentscope_session_id="as-session-1",
        tool="extract_and_sync_navigation_data_tool",
    )

    assert status == "failed"
    assert recovered[0][0][:2] == ("plan-1", "extract_sync")
    assert recovered[0][1]["expected_action"] == "extract_and_sync_navigation_data"


def test_runtime_reconciles_staged_background_result_from_durable_status() -> None:
    step = SimpleNamespace(
        action="extract_and_sync_navigation_data",
        step_id="extract_sync",
        status="running",
    )
    snapshot = SimpleNamespace(
        overview=SimpleNamespace(steps=[step]),
        staged_result=SimpleNamespace(
            step_id="extract_sync",
            target_status="completed",
        ),
        active_plan=SimpleNamespace(plan_id="plan-1"),
    )
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime._navigation_plan_store = lambda: SimpleNamespace(
        read_execution_snapshot=lambda **_kwargs: snapshot,
    )

    status = runtime.reconcile_background_tool_status(
        web_session_id="web-1",
        agentscope_session_id="as-session-1",
        tool="extract_and_sync_navigation_data_tool",
    )

    assert status == "completed"


@pytest.mark.asyncio
async def test_runtime_start_agent_run_registers_binds_and_cleans_cancellation() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry, message_bus=FakeAgentScopeMessageBus())

    await runtime.submit_user_message(web_session_id="web-1", message="你好")

    session_id = "web-1__main-router-agent"
    cancellation = runtime.run_cancellation(session_id)
    assert isinstance(cancellation, CancellationContext)

    await chat_run_registry.drain()

    assert runtime.run_cancellation(session_id) is None
    assert runtime.app.state.chat_service.seen_cancellations == [cancellation]


@pytest.mark.asyncio
async def test_runtime_submit_user_message_duplicate_active_run_raises() -> None:
    chat_run_registry = FakeChatRunRegistry(reject_duplicate_active=True)
    runtime = _runtime(chat_run_registry=chat_run_registry)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")
    with pytest.raises(RuntimeError, match="already active"):
        await runtime.submit_user_message(web_session_id="web-1", message="第二条")

    assert len(runtime.storage.sessions) == 1
    assert len(chat_run_registry.spawns) == 1
    assert runtime.app.state.chat_service.runs == []
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_submit_user_message_duplicate_active_run_preserves_cancellation() -> None:
    chat_run_registry = FakeChatRunRegistry(reject_duplicate_active=True)
    runtime = _runtime(chat_run_registry=chat_run_registry, message_bus=FakeAgentScopeMessageBus())

    await runtime.submit_user_message(web_session_id="web-1", message="你好")
    session_id = "web-1__main-router-agent"
    active_cancellation = runtime.run_cancellation(session_id)

    with pytest.raises(RuntimeError, match="already active"):
        await runtime.submit_user_message(web_session_id="web-1", message="第二条")

    assert runtime.run_cancellation(session_id) is active_cancellation
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_submit_user_message_advances_event_cursor_before_spawn() -> None:
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            ("1-0", {"type": "TEXT_BLOCK_DELTA", "delta": "旧"}),
            ("2-0", {"type": "REPLY_END"}),
        ],
    )
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry, message_bus=message_bus)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")

    assert message_bus.read_sessions == ["web-1__main-router-agent"]
    assert message_bus.read_since == [None]
    assert runtime.event_cursors == {"web-1__main-router-agent": "2-0"}
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == [
        "web-1__main-router-agent"
    ]
    await chat_run_registry.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_state", [ToolCallState.SUBMITTED, "submitted"])
async def test_runtime_submit_human_decision_spawns_external_execution_result_event(
    tool_state,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record(
            reply_id="reply-1",
            tool_call_id="tool-1",
            tool_state=tool_state,
        )
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision={
            "action": "guide",
            "text": "请改用另一组外参",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
        },
    )

    assert accepted is True
    assert runtime.app.state.chat_service.runs == []
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == ["as-session-1"]
    await chat_run_registry.drain()
    run = runtime.app.state.chat_service.runs[0]
    assert run["user_id"] == "alice"
    assert run["session_id"] == "as-session-1"
    assert run["agent_id"] == "navigation-data-agent"
    event = run["message"]
    assert isinstance(event, ExternalExecutionResultEvent)
    assert event.reply_id == "reply-1"
    assert len(event.execution_results) == 1
    result = event.execution_results[0]
    assert result.id == "tool-1"
    assert result.name == "request_human_decision"
    assert result.state == ToolResultState.SUCCESS
    assert json.loads(result.output) == {
        "action": "guide",
        "text": "请改用另一组外参",
        "request_id": "camera-1",
    }


@pytest.mark.asyncio
async def test_runtime_submit_human_decision_resumes_calibration_request_human_decision() -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record(
            reply_id="reply-1",
            tool_call_id="confirm-1",
            tool_name="request_human_decision",
            tool_input={
                "decision_type": "camera_params",
                "request_id": "confirm_navigation_calibration_params:20270605",
                "summary": "Confirm navigation camera calibration.",
            },
        )
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision={
            "action": "confirm",
            "request_id": "confirm_navigation_calibration_params:20270605",
            "tool_call_id": "confirm-1",
            "reply_id": "reply-1",
        },
    )

    assert accepted is True
    await chat_run_registry.drain()
    event = runtime.app.state.chat_service.runs[0]["message"]
    result = event.execution_results[0]
    assert result.id == "confirm-1"
    assert result.name == "request_human_decision"
    assert result.state == ToolResultState.SUCCESS
    assert json.loads(result.output) == {
        "action": "confirm",
        "text": None,
        "request_id": "confirm_navigation_calibration_params:20270605",
        "decision_type": "camera_params",
        "ok": True,
        "tool_name": "confirm_navigation_calibration_params",
        "message": "Camera parameters confirmed by user.",
    }


@pytest.mark.asyncio
async def test_plan_bound_human_decision_recovers_after_synchronous_spawn_failure(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry(reject_duplicate_active=True)
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    registry.active_session_ids.add("as-session-1")

    with pytest.raises(RuntimeError, match="already active"):
        await runtime.submit_human_decision(web_session_id="web-1", decision=decision)

    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is not None
    assert plan_store.get_current_step(plan.plan_id) is None
    conflicting = {**decision, "action": "stop"}
    assert (
        await runtime.submit_human_decision(
            web_session_id="web-1",
            decision=conflicting,
        )
        is False
    )

    registry.active_session_ids.clear()
    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision=decision,
    )
    assert accepted is True
    await registry.drain()
    assert len(runtime.app.state.chat_service.runs) == 1
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is None


@pytest.mark.asyncio
async def test_plan_bound_human_decision_recovers_after_async_run_failure(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)

    class FailOnceChatService(FakeChatService):
        def __init__(self) -> None:
            super().__init__(runtime.storage)
            self.attempts = 0

        async def run(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("async AgentScope failure")
            await super().run(**kwargs)

    runtime.app.state.chat_service = FailOnceChatService()

    assert await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    with pytest.raises(RuntimeError, match="async AgentScope failure"):
        await registry.drain()
    registry.active_session_ids.discard("as-session-1")
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is not None

    assert await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    duplicate_while_running = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision=decision,
    )
    assert duplicate_while_running is False
    await registry.drain()

    assert runtime.app.state.chat_service.attempts == 2
    assert len(runtime.app.state.chat_service.runs) == 1
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is None


@pytest.mark.asyncio
async def test_expired_delivering_handoff_with_submitted_call_fails_closed(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    status, token = plan_store.claim_human_decision_delivery(
        plan.plan_id, "confirm", agentscope_runtime_module.human_decision_key(
            agentscope_runtime_module._durable_plan_decision(decision)
        ), owner="dead-worker",
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    assert status == "claimed" and token
    with sqlite3.connect(plan_store.db_path) as connection:
        connection.execute(
            "UPDATE navigation_human_decision_handoffs SET expires_at = '2000-01-01T00:00:00+00:00'"
        )

    with pytest.raises(RuntimeError, match="human-decisions/recovery"):
        await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    assert registry.spawns == []
    assert runtime.app.state.chat_service.runs == []
    handoff = plan_store.get_human_decision_handoff(plan.plan_id, "confirm")
    assert handoff is not None
    assert handoff.status == "recovery_required"
    assert handoff.delivery_status == "recovery_required"
    assert handoff.delivery_token == token


@pytest.mark.asyncio
async def test_missing_agentscope_session_marks_existing_handoff_recovery_required(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    runtime.storage.session_records.clear()

    with pytest.raises(RuntimeError, match="missing_agentscope_session.*human-decisions/recovery"):
        await runtime.submit_human_decision(web_session_id="web-1", decision=decision)

    handoff = plan_store.get_human_decision_handoff(plan.plan_id, "confirm")
    assert handoff is not None and handoff.status == "recovery_required"
    assert registry.spawns == []


@pytest.mark.asyncio
async def test_cross_session_submit_cannot_mutate_or_deliver_foreign_handoff(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    runtime.web_sessions["attacker-web"] = (
        "navigation-data-agent",
        "as-session-1",
    )

    with pytest.raises(RuntimeError, match="does not belong"):
        await runtime.submit_human_decision(
            web_session_id="attacker-web",
            decision=decision,
        )

    handoff = plan_store.get_human_decision_handoff(plan.plan_id, "confirm")
    assert handoff is not None
    assert handoff.status == "pending"
    assert handoff.delivery_status == "pending"
    assert registry.spawns == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("missing_mapping", "missing_web_mapping"),
        ("missing_reply", "missing_agentscope_reply"),
        ("missing_tool_call", "missing_agentscope_tool_call"),
    ],
)
async def test_missing_handoff_identity_fails_closed(
    tmp_path: Path,
    mutation: str,
    reason_code: str,
) -> None:
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(
        tmp_path, FakeChatRunRegistry()
    )
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    record = runtime.storage.session_records[
        ("alice", "navigation-data-agent", "as-session-1")
    ]
    if mutation == "missing_mapping":
        runtime.web_sessions.clear()
    elif mutation == "missing_reply":
        record.state.reply_id = "different-reply"
    else:
        record.state.context = []

    with pytest.raises(RuntimeError, match=reason_code):
        await runtime.submit_human_decision(web_session_id="web-1", decision=decision)

    handoff = plan_store.get_human_decision_handoff(plan.plan_id, "confirm")
    assert handoff is not None
    assert handoff.status == "recovery_required"
    assert handoff.recovery_reason_code == reason_code


@pytest.mark.asyncio
async def test_runtime_controlled_handoff_recovery_delegates_with_web_ownership(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    plan_store.mark_human_decision_recovery_required(
        plan.plan_id,
        "confirm",
        reason_code="missing_agentscope_session",
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )

    recovered = await runtime.recover_human_decision_handoff(
        web_session_id="web-1",
        recovery={
            "action": "quarantine_and_replan",
            "plan_id": plan.plan_id,
            "step_id": "confirm",
            "reason": "operator confirmed abandoned worker",
        },
    )

    assert recovered["handoff_status"] == "quarantined"
    assert recovered["task_status"] == "needs_replan"
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is not None
    with pytest.raises(RuntimeError, match="quarantined.*submit_complete_plan"):
        await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    assert registry.spawns == []


@pytest.mark.asyncio
async def test_recovery_required_pending_event_points_only_to_controlled_recovery(
    tmp_path: Path,
) -> None:
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(
        tmp_path, FakeChatRunRegistry()
    )
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    plan_store.mark_human_decision_recovery_required(
        plan.plan_id,
        "confirm",
        reason_code="missing_agentscope_session",
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )

    event = await runtime._pending_human_decision_event(
        web_session_id="web-1",
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )

    assert event is not None
    payload = event["payload"]
    assert payload["recovery_required"] is True
    assert payload["submission_disabled"] is True
    assert payload["recovery_endpoint"] == (
        "/api/sessions/web-1/human-decisions/recovery"
    )
    assert payload["recovery"] == {
        "action": "quarantine_and_replan",
        "plan_id": plan.plan_id,
        "step_id": "confirm",
    }


@pytest.mark.asyncio
async def test_quarantined_pending_event_is_suppressed_and_marked_consumed(
    tmp_path: Path,
) -> None:
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(
        tmp_path, FakeChatRunRegistry()
    )
    store = WebSessionStore(tmp_path / "web.sqlite")
    web_session = store.create_session("recover")
    runtime.set_web_session_store(store)
    runtime.web_sessions[web_session.id] = ("navigation-data-agent", "as-session-1")
    with sqlite3.connect(plan_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_tasks
               SET created_by_web_session_id = ?
               WHERE task_id = ?""",
            (web_session.id, plan.task_id),
        )
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id=web_session.id,
        expected_agentscope_session_id="as-session-1",
    )
    plan_store.mark_human_decision_recovery_required(
        plan.plan_id,
        "confirm",
        reason_code="missing_agentscope_session",
        expected_web_session_id=web_session.id,
        expected_agentscope_session_id="as-session-1",
    )
    plan_store.quarantine_human_decision_handoff(
        plan.plan_id,
        "confirm",
        expected_web_session_id=web_session.id,
        reason="operator recovery",
    )

    event = await runtime._pending_human_decision_event(
        web_session_id=web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )

    assert event is None
    assert store.is_human_decision_consumed(
        agentscope_session_id="as-session-1",
        reply_id="reply-1",
        tool_call_id="confirm-1",
    )


@pytest.mark.asyncio
async def test_expired_delivering_handoff_with_completed_external_call_is_ack_only(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    assert agentscope_runtime_module.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=runtime._navigation_evidence_store(),
        plan_id=plan.plan_id,
        step_id="confirm",
        decision=decision,
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )
    key = agentscope_runtime_module.human_decision_key(
        agentscope_runtime_module._durable_plan_decision(decision)
    )
    assert plan_store.claim_human_decision_delivery(
        plan.plan_id,
        "confirm",
        key,
        owner="dead-worker",
        expected_web_session_id="web-1",
        expected_agentscope_session_id="as-session-1",
    )[0] == "claimed"
    with sqlite3.connect(plan_store.db_path) as connection:
        connection.execute(
            "UPDATE navigation_human_decision_handoffs SET expires_at = '2000-01-01T00:00:00+00:00'"
        )
    record = runtime.storage.session_records[("alice", "navigation-data-agent", "as-session-1")]
    record.state.context[0].get_content_blocks("tool_call")[0].state = ToolCallState.FINISHED

    assert await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    assert registry.spawns == []
    assert runtime.app.state.chat_service.runs == []
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is None


@pytest.mark.asyncio
async def test_run_that_consumes_external_result_then_raises_is_not_retried(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)

    class ConsumeThenRaise(FakeChatService):
        async def run(self, **kwargs):
            await super().run(**kwargs)
            raise RuntimeError("failed after durable consumption")

    runtime.app.state.chat_service = ConsumeThenRaise(runtime.storage)
    assert await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    with pytest.raises(RuntimeError, match="durable consumption"):
        await registry.drain()
    registry.active_session_ids.clear()
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is None

    assert not await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    assert len(runtime.app.state.chat_service.runs) == 1


@pytest.mark.asyncio
async def test_plan_bound_human_decision_success_then_ack_failure_is_ack_only_after_restart(
    monkeypatch, tmp_path: Path
) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)
    original_ack = SqliteNavigationPlanRepository.acknowledge_human_decision_handoff
    attempts = 0

    def fail_ack_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return original_ack(self, *args, **kwargs)

    monkeypatch.setattr(
        SqliteNavigationPlanRepository,
        "acknowledge_human_decision_handoff",
        fail_ack_once,
    )
    assert await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    with pytest.raises(RuntimeError, match="acknowledgement failed"):
        await registry.drain()
    handoff = plan_store.get_human_decision_handoff(plan.plan_id, "confirm")
    assert handoff is not None and handoff.delivery_status == "delivered"
    assert len(runtime.app.state.chat_service.runs) == 1

    fresh_registry = FakeChatRunRegistry()
    fresh = _runtime(
        storage=runtime.storage,
        chat_run_registry=fresh_registry,
        workspace_root=tmp_path / "workspace",
    )
    fresh.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    assert await fresh.submit_human_decision(web_session_id="web-1", decision=decision)
    assert fresh_registry.spawns == []
    assert fresh.app.state.chat_service.runs == []
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is None


@pytest.mark.asyncio
async def test_completed_final_human_handoff_blocks_same_phase_activation(tmp_path: Path) -> None:
    registry = FakeChatRunRegistry()
    runtime, plan_store, plan, decision = _plan_bound_human_runtime(tmp_path, registry)

    assert await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    assert plan_store.get(plan.plan_id).status == "completed"
    task = SqliteNavigationTaskStore(plan_store.db_path).get_task(plan.task_id)
    with pytest.raises(ActivePlanExecutionConflict):
            plan_store.activate(
                task,
                "finish_processing",
                2,
                plan.plan,
                expected_web_session_id="web-1",
                expected_agentscope_session_id="as-session-1",
            )
    await registry.drain()


@pytest.mark.asyncio
async def test_runtime_does_not_rehydrate_consumed_human_decision_after_claim_releases(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record(
            reply_id="reply-1",
            tool_call_id="confirm-1",
            tool_name="request_human_decision",
            tool_state=ToolCallState.SUBMITTED,
            tool_input={
                "decision_type": "camera_params",
                "request_id": "confirm_navigation_calibration_params:20270605",
                "summary": "请确认相机参数。",
            },
        )
    )
    runtime = _runtime(
        storage=storage,
        chat_run_registry=chat_run_registry,
        message_bus=FakeAgentScopeMessageBus(running_states=[False]),
    )
    runtime.set_web_session_store(store)

    accepted = await runtime.submit_human_decision(
        web_session_id=web_session.id,
        decision={
            "action": "confirm",
            "request_id": "confirm_navigation_calibration_params:20270605",
            "tool_call_id": "confirm-1",
            "reply_id": "reply-1",
        },
    )
    await chat_run_registry.drain()
    events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id=web_session.id)
    ]

    assert accepted is True
    assert events == []


@pytest.mark.asyncio
async def test_runtime_submit_human_decision_registers_binds_and_cleans_cancellation() -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record()
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision={
            "action": "confirm",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
            "reply_id": "reply-1",
        },
    )

    cancellation = runtime.run_cancellation("as-session-1")
    assert accepted is True
    assert isinstance(cancellation, CancellationContext)

    await chat_run_registry.drain()

    assert runtime.run_cancellation("as-session-1") is None
    assert runtime.app.state.chat_service.seen_cancellations == [cancellation]


@pytest.mark.asyncio
async def test_runtime_submit_human_decision_recovers_mapping_after_restart(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record(reply_id="reply-1", tool_call_id="tool-1")
    )
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.set_web_session_store(store)

    accepted = await runtime.submit_human_decision(
        web_session_id=web_session.id,
        decision={
            "action": "confirm",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
        },
    )

    assert accepted is True
    assert runtime.web_sessions == {
        web_session.id: ("navigation-data-agent", "as-session-1")
    }
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == ["as-session-1"]
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_recovers_wakeup_after_transient_dequeue_timeout() -> None:
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record()
    )
    message_bus = FakeAgentScopeMessageBus(
        wakeups=[
            {
                "user_id": "alice",
                "agent_id": "navigation-data-agent",
                "session_id": "as-session-1",
            }
        ],
        dequeue_failures=1,
    )
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        storage=storage,
        chat_run_registry=chat_run_registry,
        message_bus=message_bus,
    )

    recovered = await runtime.recover_pending_agent_wakeups_once(retry_delays=(0,))

    assert recovered == 1
    assert message_bus.dequeue_calls == 2
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == ["as-session-1"]
    await chat_run_registry.drain()
    assert runtime.app.state.chat_service.runs == [
        {
            "user_id": "alice",
            "session_id": "as-session-1",
            "agent_id": "navigation-data-agent",
            "message": None,
        }
    ]
    assert runtime.recovery_metrics.redis_timeout_count == 1


@pytest.mark.asyncio
async def test_runtime_recovers_orphan_inbox_for_idle_mapped_session(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record()
    )
    message_bus = FakeAgentScopeMessageBus(inbox_session_ids=["as-session-1"])
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(
        storage=storage,
        chat_run_registry=chat_run_registry,
        message_bus=message_bus,
    )
    runtime.set_web_session_store(store)

    recovered = await runtime.recover_orphan_agent_inboxes_once()

    assert recovered == 1
    assert runtime.web_sessions == {
        web_session.id: ("navigation-data-agent", "as-session-1")
    }
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == ["as-session-1"]
    await chat_run_registry.drain()
    assert runtime.app.state.chat_service.runs[0]["message"] is None


@pytest.mark.asyncio
async def test_runtime_records_wakeup_recovery_diagnostics() -> None:
    message_bus = FakeAgentScopeMessageBus(
        wakeups=[
            {"user_id": "alice", "agent_id": "navigation-data-agent", "session_id": "as-1"},
            {"user_id": "alice", "agent_id": "navigation-data-agent", "session_id": "as-2"},
        ],
        inbox_session_ids=["as-1", "as-2", "as-3"],
        inbox_residual_count=7,
    )
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=message_bus,
    )

    await runtime.record_recovery_diagnostics_once(event_loop_lag_seconds=0.125)

    assert runtime.recovery_metrics.wakeup_queue_length == 2
    assert runtime.recovery_metrics.inbox_residual_count == 7
    assert runtime.recovery_metrics.event_loop_lag_seconds == 0.125


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_record,decision_overrides",
    [
        (None, {}),
        (_agentscope_session_record(tool_call_id="other-tool-call"), {}),
        (_agentscope_session_record(reply_id="other-reply"), {}),
        (_agentscope_session_record(tool_state=ToolCallState.FINISHED), {}),
        (_agentscope_session_record(tool_name="other_tool"), {}),
        (_agentscope_session_record(tool_state="finished"), {}),
    ],
)
async def test_runtime_submit_human_decision_returns_false_without_matching_pending_tool_call(
    session_record,
    decision_overrides,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    if session_record is not None:
        storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
            session_record
        )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")
    decision = {
        "action": "confirm",
        "request_id": "request-1",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
        **decision_overrides,
    }

    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision=decision,
    )

    assert accepted is False
    assert chat_run_registry.spawns == []
    assert runtime.app.state.chat_service.runs == []


@pytest.mark.asyncio
async def test_runtime_submit_human_decision_claim_rejects_active_duplicate_until_run_finishes() -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record()
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")
    decision = {
        "action": "confirm",
        "request_id": "request-1",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
    }

    first = await runtime.submit_human_decision(web_session_id="web-1", decision=decision)
    duplicate = await runtime.submit_human_decision(web_session_id="web-1", decision=decision)

    assert first is True
    assert duplicate is False
    assert len(chat_run_registry.spawns) == 1
    await chat_run_registry.drain()

    retry_after_release = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision=decision,
    )

    assert retry_after_release is False
    assert len(chat_run_registry.spawns) == 0


@pytest.mark.asyncio
async def test_runtime_submit_human_decision_returns_false_for_unmapped_web_session() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    accepted = await runtime.submit_human_decision(
        web_session_id="missing",
        decision={
            "action": "confirm",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
            "reply_id": "reply-1",
        },
    )

    assert accepted is False
    assert chat_run_registry.spawns == []
    assert runtime.app.state.chat_service.runs == []


@pytest.mark.asyncio
async def test_runtime_subscribe_hydrates_pending_human_decision_after_restart(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record(
            reply_id="reply-1",
            tool_call_id="tool-1",
            tool_input={
                "decision_type": "camera_params",
                "request_id": "camera-1",
                "summary": "请确认相机参数。",
            },
        )
    )
    runtime = _runtime(
        storage=storage,
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
        workspace_root=tmp_path / "workspace",
    )
    runtime.set_web_session_store(store)

    events = [
        event async for event in runtime.subscribe_web_session_events(web_session_id=web_session.id)
    ]

    assert events == [
        {
            "type": "human_decision_required",
            "source": "NavigationDataAgent",
            "run_id": "as-session-1",
            "parent_run_id": None,
            "payload": {
                "decision_type": "camera_params",
                "request_id": "camera-1",
                "summary": "请确认相机参数。",
                "reply_id": "reply-1",
                "tool_call_id": "tool-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_runtime_subscribe_polls_pending_human_decision_before_idle_exit(
    tmp_path: Path,
) -> None:
    storage = DelayedPendingDecisionStorage(
        pending_record=_agentscope_session_record(
            reply_id="reply-1",
            tool_call_id="tool-1",
            tool_input={
                "decision_type": "camera_params",
                "request_id": "confirm_navigation_calibration_params:20270605",
                "summary": "请确认相机参数。",
            },
        )
    )
    runtime = _runtime(
        storage=storage,
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(running_states=[False]),
        workspace_root=tmp_path / "workspace",
    )
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    events = [
        event async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]

    assert events == [
        {
            "type": "human_decision_required",
            "source": "NavigationDataAgent",
            "run_id": "as-session-1",
            "parent_run_id": None,
            "payload": {
                "decision_type": "camera_params",
                "request_id": "confirm_navigation_calibration_params:20270605",
                "summary": "请确认相机参数。",
                "reply_id": "reply-1",
                "tool_call_id": "tool-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_plan_bound_human_decision_live_and_pending_paths_are_deduplicated(
    tmp_path: Path,
) -> None:
    registry = FakeChatRunRegistry()
    runtime, _plan_store, plan, _decision = _plan_bound_human_runtime(
        tmp_path,
        registry,
    )
    runtime.message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            (
                "1-0",
                {
                    "type": "REQUIRE_EXTERNAL_EXECUTION",
                    "reply_id": "reply-1",
                    "tool_calls": [
                        {
                            "id": "confirm-1",
                            "name": "request_human_decision",
                            "input": json.dumps(
                                {"plan_id": plan.plan_id, "step_id": "confirm"}
                            ),
                        }
                    ],
                },
            )
        ],
        running_states=[False],
    )

    events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]

    decisions = [event for event in events if event["type"] == "human_decision_required"]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["plan_id"] == plan.plan_id
    assert decisions[0]["payload"]["step_id"] == "confirm"


@pytest.mark.asyncio
async def test_same_subscription_emits_normal_to_recovery_required_upgrade() -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(running_states=[False, False, False]),
    )
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")
    calls = 0

    async def pending_event(**_kwargs):
        nonlocal calls
        calls += 1
        recovery_required = calls >= 2
        return {
            "type": "human_decision_required",
            "source": "NavigationDataAgent",
            "run_id": "as-session-1",
            "parent_run_id": None,
            "payload": {
                "request_id": "plan-1:confirm",
                "decision_type": "camera_params",
                "summary": "请确认。",
                "plan_id": "plan-1",
                "step_id": "confirm",
                "reply_id": "reply-1",
                "tool_call_id": "tool-1",
                **(
                    {
                        "recovery_required": True,
                        "submission_disabled": True,
                        "recovery_endpoint": (
                            "/api/sessions/web-1/human-decisions/recovery"
                        ),
                    }
                    if recovery_required
                    else {}
                ),
            },
        }

    runtime._pending_human_decision_event = pending_event

    events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]
    decisions = [event for event in events if event["type"] == "human_decision_required"]

    assert len(decisions) == 2
    assert decisions[0]["payload"].get("recovery_required") is None
    assert decisions[1]["payload"]["recovery_required"] is True


def test_human_decision_event_identity_requires_nonempty_ids_and_is_collision_free() -> None:
    assert agentscope_runtime_module._human_decision_event_key(
        {"payload": {"reply_id": "", "tool_call_id": ""}}
    ) is None
    assert agentscope_runtime_module._human_decision_event_key(
        {"payload": {"reply_id": "a:b", "tool_call_id": "c"}}
    ) != agentscope_runtime_module._human_decision_event_key(
        {"payload": {"reply_id": "a", "tool_call_id": "b:c"}}
    )


@pytest.mark.asyncio
async def test_runtime_submit_user_message_does_not_advance_event_cursor_when_spawn_fails() -> None:
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            ("1-0", {"type": "TEXT_BLOCK_DELTA", "delta": "旧"}),
            ("2-0", {"type": "REPLY_END"}),
        ],
    )
    chat_run_registry = FakeChatRunRegistry(reject_duplicate_active=True)
    runtime = _runtime(chat_run_registry=chat_run_registry, message_bus=message_bus)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")
    message_bus.replay_events = [
        ("1-0", {"type": "TEXT_BLOCK_DELTA", "delta": "旧"}),
        ("2-0", {"type": "REPLY_END"}),
        ("3-0", {"type": "TEXT_BLOCK_DELTA", "delta": "运行中"}),
    ]
    with pytest.raises(RuntimeError, match="already active"):
        await runtime.submit_user_message(web_session_id="web-1", message="第二条")

    assert message_bus.read_sessions == [
        "web-1__main-router-agent",
        "web-1__main-router-agent",
    ]
    assert message_bus.read_since == [None, "2-0"]
    assert runtime.event_cursors == {"web-1__main-router-agent": "2-0"}
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_runtime_subscribe_web_session_events_replays_dedupes_and_finishes() -> None:
    text_event = {"type": "TEXT_BLOCK_DELTA", "delta": "处理"}
    final_event = {"type": "REPLY_END"}
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            ("1-0", text_event),
        ],
        live_events=[
            {**text_event, "_entry_id": "1-0"},
            {**final_event, "_entry_id": "2-0"},
        ],
        running_states=[False],
    )
    runtime = _runtime(message_bus=message_bus)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]

    assert message_bus.read_sessions == ["as-session-1"]
    assert message_bus.subscribe_keys == ["agentscope:session:events:as-session-1"]
    assert [(event["type"], event["payload"]) for event in events] == [
        ("final", {"text": "处理"}),
    ]


@pytest.mark.asyncio
async def test_runtime_projects_public_activity_and_tool_name_without_internal_payloads() -> None:
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            (
                "1-0",
                {
                    "type": "TEXT_BLOCK_DELTA",
                    "delta": (
                        'Activity: {"observation":"发现已有处理方案。",'
                        '"analysis":"需要确认当前步骤。",'
                        '"action":"读取当前计划步骤。"}\n'
                    ),
                },
            ),
            (
                "2-0",
                {
                    "type": "TOOL_RESULT_START",
                    "tool_call_id": "call-1",
                    "tool_call_name": "get_current_plan_step_tool",
                },
            ),
            (
                "3-0",
                {
                    "type": "TOOL_RESULT_TEXT_DELTA",
                    "tool_call_id": "call-1",
                    "delta": '{"path":"/media/internal/plan.json","secret":"hidden"}',
                },
            ),
            (
                "4-0",
                {"type": "TOOL_RESULT_END", "tool_call_id": "call-1", "state": "success"},
            ),
            ("5-0", {"type": "TEXT_BLOCK_DELTA", "delta": "当前步骤已确认。"}),
            ("6-0", {"type": "REPLY_END"}),
        ],
        running_states=[False, False],
    )
    runtime = _runtime(message_bus=message_bus)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]

    assert [event["type"] for event in events] == [
        "activity_snapshot",
        "tool_start",
        "activity_delta",
        "tool_end",
        "activity_delta",
        "activity_delta",
        "final",
    ]
    assert events[1]["payload"] == {
        "tool": "get_current_plan_step_tool",
        "call_id": "call-1",
    }
    assert events[3]["payload"] == {
        "tool": "get_current_plan_step_tool",
        "call_id": "call-1",
        "status": "completed",
    }
    serialized = json.dumps(events, ensure_ascii=False)
    assert "/media/internal" not in serialized
    assert '"secret"' not in serialized
    assert events[-1]["payload"] == {"text": "当前步骤已确认。"}


@pytest.mark.asyncio
async def test_runtime_subscribe_web_session_events_waits_for_delayed_first_event(
    monkeypatch,
) -> None:
    monkeypatch.setattr(agentscope_runtime_module, "_EVENT_STARTUP_GRACE_SECS", 0.01)
    message_bus = DelayedLiveEventMessageBus(
        live_delay=0.03,
        running_states=[False, False, False, False],
    )
    runtime = _runtime(message_bus=message_bus)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")
    cancellation = CancellationContext()
    runtime.register_run_cancellation("as-session-1", cancellation)

    events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]

    assert [(event["type"], event["payload"]) for event in events] == [
        ("final", {"text": "迟到事件"}),
    ]


@pytest.mark.asyncio
async def test_runtime_event_cursor_skips_previous_turn_replay_for_same_agentscope_session() -> None:
    old_text = {"type": "TEXT_BLOCK_DELTA", "delta": "旧"}
    old_final = {"type": "REPLY_END"}
    new_text = {"type": "TEXT_BLOCK_DELTA", "delta": "新"}
    new_final = {"type": "REPLY_END"}
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            ("1-0", old_text),
            ("2-0", old_final),
        ],
        running_states=[False, False],
    )
    runtime = _runtime(message_bus=message_bus)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    first_events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]
    message_bus.replay_events = [
        ("1-0", old_text),
        ("2-0", old_final),
        ("3-0", new_text),
        ("4-0", new_final),
    ]
    second_events = [
        event
        async for event in runtime.subscribe_web_session_events(web_session_id="web-1")
    ]

    assert message_bus.read_since == [None, "2-0"]
    assert [(event["type"], event["payload"]) for event in first_events] == [
        ("final", {"text": "旧"}),
    ]
    assert [(event["type"], event["payload"]) for event in second_events] == [
        ("final", {"text": "新"}),
    ]


@pytest.mark.asyncio
async def test_runtime_event_cursor_persists_across_runtime_restart(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    web_session = store.create_session("处理导航数据")
    store.save_agentscope_session_mapping(
        web_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    old_text = {"type": "TEXT_BLOCK_DELTA", "delta": "旧"}
    old_final = {"type": "REPLY_END"}
    new_text = {"type": "TEXT_BLOCK_DELTA", "delta": "新"}
    new_final = {"type": "REPLY_END"}
    message_bus = FakeAgentScopeMessageBus(
        replay_events=[
            ("1-0", old_text),
            ("2-0", old_final),
        ],
        running_states=[False, False],
    )
    first_runtime = _runtime(message_bus=message_bus)
    first_runtime.set_web_session_store(store)

    first_events = [
        event
        async for event in first_runtime.subscribe_web_session_events(web_session_id=web_session.id)
    ]

    assert [(event["type"], event["payload"]) for event in first_events] == [
        ("final", {"text": "旧"}),
    ]
    assert store.get_agentscope_session_mapping(web_session.id).event_cursor == "2-0"

    message_bus.replay_events = [
        ("1-0", old_text),
        ("2-0", old_final),
        ("3-0", new_text),
        ("4-0", new_final),
    ]
    second_runtime = _runtime(message_bus=message_bus)
    second_runtime.set_web_session_store(store)
    second_events = [
        event
        async for event in second_runtime.subscribe_web_session_events(web_session_id=web_session.id)
    ]

    assert message_bus.read_since == [None, "2-0"]
    assert [(event["type"], event["payload"]) for event in second_events] == [
        ("final", {"text": "新"}),
    ]


@pytest.mark.asyncio
async def test_create_session_creates_compatible_record_and_persists(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = AgentScopeWebSessionManager(store=store, runtime=FakeAgentScopeRuntime())

    session = await manager.create_session("处理 20270605 的室外导航数据，并进行 dry-run 验证")

    assert isinstance(session, SessionRecord)
    assert session.id.startswith("session_")
    assert session.title == "处理 20270605 的室外导航数据，并进行 dry-ru"
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.model_dump(exclude={"messages", "events", "turns"}) == session.model_dump()
    assert detail.messages == []
    assert detail.events == []


def test_agentscope_web_session_manager_attaches_store_to_runtime(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = StoreAwareAgentScopeRuntime()

    AgentScopeWebSessionManager(store=store, runtime=runtime)

    assert runtime.web_session_store is store


@pytest.mark.asyncio
async def test_submit_turn_appends_user_message_calls_runtime_and_returns_turn_id(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = FakeAgentScopeRuntime(turn_id="turn_agentscope_1")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    turn_id = await manager.submit_turn(session.id, "开始处理")

    assert turn_id.startswith("turn_")
    assert runtime.submissions == [{"web_session_id": session.id, "message": "开始处理"}]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.turns[0].id == turn_id
    assert detail.messages[0].turn_id == turn_id
    assert [(message.role, message.content) for message in detail.messages] == [("user", "开始处理")]


@pytest.mark.asyncio
async def test_forward_events_until_idle_publishes_runtime_events(tmp_path: Path) -> None:
    event = {
        "type": "assistant_delta",
        "source": "NavigationDataAgent",
        "payload": {"delta": "处理中"},
    }
    published = []
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = EventingAgentScopeRuntime([event])
    manager = AgentScopeWebSessionManager(
        store=store,
        runtime=runtime,
        event_callback=lambda session_id, event: published.append((session_id, event)),
    )
    session = await manager.create_session("处理 20270605")

    await manager.forward_events_until_idle(session.id)

    assert runtime.subscriptions == [session.id]
    assert published == [(session.id, event)]


@pytest.mark.asyncio
async def test_persistent_event_bridge_projects_wakeup_run_after_first_reply_end(
    tmp_path: Path,
) -> None:
    published: list[tuple[str, dict]] = []
    published_both = asyncio.Event()

    async def publish(session_id: str, event: dict) -> None:
        published.append((session_id, event))
        if len(published) == 2:
            published_both.set()

    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = PersistentBatchEventingRuntime()
    manager = AgentScopeWebSessionManager(
        store=store,
        runtime=runtime,
        event_callback=publish,
    )
    session = await manager.create_session("background wakeup")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-navigation",
    )
    assert manager.event_bridge is not None

    await manager.event_bridge.start()
    try:
        runtime.queue.put_nowait(
            SimpleNamespace(
                entry_id="1-0",
                raw_event_type="REPLY_END",
                events=(
                    {
                        "type": "final",
                        "source": "agentscope",
                        "run_id": "as-navigation",
                        "payload": {"text": "工具正在后台执行。"},
                    },
                ),
            )
        )
        runtime.queue.put_nowait(
            SimpleNamespace(
                entry_id="2-0",
                raw_event_type="REPLY_END",
                events=(
                    {
                        "type": "final",
                        "source": "agentscope",
                        "run_id": "as-navigation",
                        "payload": {"text": "后台工具已完成，产物检查通过。"},
                    },
                ),
            )
        )
        await published_both.wait()
        await manager.forward_events_until_idle(session.id)
        await manager.forward_events_until_idle(session.id)
    finally:
        await manager.event_bridge.stop()

    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.content for message in detail.messages] == [
        "工具正在后台执行。",
        "后台工具已完成，产物检查通过。",
    ]
    assert [event.payload["text"] for event in detail.events] == [
        "工具正在后台执行。",
        "后台工具已完成，产物检查通过。",
    ]
    assert runtime.subscriptions == 1
    assert runtime.cursors == [("as-navigation", "1-0"), ("as-navigation", "2-0")]


@pytest.mark.asyncio
async def test_forward_events_until_idle_awaits_async_event_callback(tmp_path: Path) -> None:
    event = {
        "type": "assistant_delta",
        "source": "NavigationDataAgent",
        "payload": {"delta": "处理中"},
    }
    published = []

    async def publish(session_id: str, event: dict) -> None:
        await asyncio.sleep(0)
        published.append((session_id, event))

    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = EventingAgentScopeRuntime([event])
    manager = AgentScopeWebSessionManager(
        store=store,
        runtime=runtime,
        event_callback=publish,
    )
    session = await manager.create_session("处理 20270605")

    await manager.forward_events_until_idle(session.id)

    assert published == [(session.id, event)]


@pytest.mark.asyncio
async def test_forward_events_until_idle_persists_final_assistant_text(tmp_path: Path) -> None:
    final_event = {
        "type": "final",
        "source": "NavigationDataAgent",
        "payload": {"text": "处理完成"},
    }
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = EventingAgentScopeRuntime([final_event])
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    await manager.forward_events_until_idle(session.id)

    detail = store.get_session(session.id)
    assert detail is not None
    assert [(message.role, message.content) for message in detail.messages] == [
        ("assistant", "处理完成")
    ]


@pytest.mark.asyncio
async def test_forward_events_until_idle_dedupes_same_final_text_within_one_forward(tmp_path: Path) -> None:
    final_event = {
        "type": "final",
        "source": "NavigationDataAgent",
        "payload": {"text": "处理完成"},
    }
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = EventingAgentScopeRuntime([final_event, final_event])
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    await manager.forward_events_until_idle(session.id)

    detail = store.get_session(session.id)
    assert detail is not None
    assert [(message.role, message.content) for message in detail.messages] == [
        ("assistant", "处理完成")
    ]


@pytest.mark.asyncio
async def test_forward_events_until_idle_persists_same_final_text_across_forwards(tmp_path: Path) -> None:
    final_event = {
        "type": "final",
        "source": "NavigationDataAgent",
        "payload": {"text": "处理完成"},
    }
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = EventingAgentScopeRuntime([final_event])
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    await manager.forward_events_until_idle(session.id)
    await manager.forward_events_until_idle(session.id)

    detail = store.get_session(session.id)
    assert detail is not None
    assert [(message.role, message.content) for message in detail.messages] == [
        ("assistant", "处理完成"),
        ("assistant", "处理完成"),
    ]


@pytest.mark.asyncio
async def test_forward_events_until_idle_continues_after_active_agentscope_session_switch(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = SwitchingEventingAgentScopeRuntime()
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    await manager.forward_events_until_idle(session.id)

    assert runtime.subscriptions == [
        (session.id, ("main-router-agent", "main-session")),
        (session.id, ("navigation-data-agent", "navigation-session")),
    ]
    detail = store.get_session(session.id)
    assert detail is not None
    assert [(message.role, message.content) for message in detail.messages] == [
        ("assistant", "我来交给导航处理"),
        ("assistant", "开始检查导航数据"),
    ]


@pytest.mark.asyncio
async def test_forward_events_until_idle_serializes_same_session_subscriptions(tmp_path: Path) -> None:
    final_event = {
        "type": "final",
        "source": "NavigationDataAgent",
        "payload": {"text": "处理完成"},
    }
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = ConcurrentEventingAgentScopeRuntime([final_event])
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    await asyncio.gather(
        manager.forward_events_until_idle(session.id),
        manager.forward_events_until_idle(session.id),
    )

    assert runtime.subscriptions == [session.id, session.id]
    assert runtime.max_active == 1


@pytest.mark.asyncio
async def test_submit_turn_rejection_does_not_append_user_message(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = RejectingAgentScopeRuntime()
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    with pytest.raises(RuntimeError, match="turn rejected"):
        await manager.submit_turn(session.id, "开始处理")

    assert runtime.submissions == [{"web_session_id": session.id, "message": "开始处理"}]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.messages == []


@pytest.mark.asyncio
async def test_submit_turn_rejects_unknown_session(tmp_path: Path) -> None:
    manager = AgentScopeWebSessionManager(
        store=WebSessionStore(tmp_path / "sessions.sqlite"),
        runtime=FakeAgentScopeRuntime(),
    )

    with pytest.raises(KeyError):
        await manager.submit_turn("missing", "hello")


@pytest.mark.asyncio
async def test_interrupt_rejects_unknown_session(tmp_path: Path) -> None:
    manager = AgentScopeWebSessionManager(
        store=WebSessionStore(tmp_path / "sessions.sqlite"),
        runtime=FakeAgentScopeRuntime(),
    )

    with pytest.raises(KeyError):
        await manager.interrupt("missing")


@pytest.mark.asyncio
async def test_interrupt_returns_false_without_runtime_interrupt(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = AgentScopeWebSessionManager(store=store, runtime=FakeAgentScopeRuntime())
    session = await manager.create_session("处理 20270605")

    assert await manager.interrupt(session.id) is False


@pytest.mark.asyncio
async def test_interrupt_delegates_to_runtime_interrupt(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = InterruptingAgentScopeRuntime(interrupted=True)
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    assert await manager.interrupt(session.id) is True
    assert runtime.interrupts == [session.id]


@pytest.mark.asyncio
async def test_submit_human_decision_rejects_unknown_session(tmp_path: Path) -> None:
    manager = AgentScopeWebSessionManager(
        store=WebSessionStore(tmp_path / "sessions.sqlite"),
        runtime=HumanDecisionAgentScopeRuntime(),
    )

    with pytest.raises(KeyError):
        await manager.submit_human_decision("missing", {"action": "confirm"})


@pytest.mark.asyncio
async def test_submit_human_decision_returns_false_without_runtime_support(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    manager = AgentScopeWebSessionManager(store=store, runtime=FakeAgentScopeRuntime())
    session = await manager.create_session("处理 20270605")

    assert await manager.submit_human_decision(session.id, {"action": "confirm"}) is False


@pytest.mark.asyncio
async def test_submit_human_decision_delegates_to_runtime(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = HumanDecisionAgentScopeRuntime(accepted=True)
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")
    decision = {
        "action": "confirm",
        "request_id": "request-1",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
    }

    assert await manager.submit_human_decision(session.id, decision) is True
    assert runtime.decisions == [(session.id, decision)]


@pytest.mark.parametrize("text", [None, "   "])
def test_human_decision_request_requires_text_for_guidance(text: str | None) -> None:
    with pytest.raises(ValueError, match="text must not be empty"):
        HumanDecisionRequest(
            action="guide",
            request_id="request-1",
            tool_call_id="tool-1",
            reply_id="reply-1",
            text=text,
        )

    request = HumanDecisionRequest(
        action="guide",
        request_id="request-1",
        tool_call_id="tool-1",
        reply_id="reply-1",
        text="继续 dry-run",
    )

    assert request.text == "继续 dry-run"


@pytest.mark.parametrize("action", ["confirm", "stop"])
@pytest.mark.parametrize("text", [None, "   "])
def test_human_decision_request_allows_non_guidance_without_text(action: str, text: str | None) -> None:
    request = HumanDecisionRequest(
        action=action,
        request_id="request-1",
        tool_call_id="tool-1",
        reply_id="reply-1",
        text=text,
    )

    assert request.action == action
    assert request.text == text


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "x" * 513),
        ("plan_id", "x" * 513),
        ("step_id", "x" * 513),
        ("tool_call_id", "x" * 513),
        ("reply_id", "x" * 513),
        ("text", "x" * 4001),
    ],
)
def test_human_decision_request_enforces_string_capacity(field: str, value: str) -> None:
    payload = {
        "action": "guide" if field == "text" else "confirm",
        "request_id": "request-1",
        "tool_call_id": "tool-1",
        "reply_id": "reply-1",
        "text": "ok" if field == "text" else None,
        field: value,
    }
    with pytest.raises(ValueError, match="at most"):
        HumanDecisionRequest(**payload)
