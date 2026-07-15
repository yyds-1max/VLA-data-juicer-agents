import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from agentscope.app.message_bus import MessageBusKeys
from agentscope.event import (
    CustomEvent,
    ExternalExecutionResultEvent,
    HintBlockEvent,
    ReplyEndEvent,
    ReplyStartEvent,
)
from agentscope.message import Msg, ToolCallBlock, ToolCallState, ToolResultState, UserMsg
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.tool import ToolResponse
from fastapi import FastAPI

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
from vla_data_juicer_agents.runtime.datapilot_projection import (
    DataPilotReplyProjectionMiddleware,
    DataPilotRunBoundaryMiddleware,
    DataPilotToolOutcomeMiddleware,
)
from vla_data_juicer_agents.runtime.agentscope_prompts import navigation_agent_prompt
from vla_data_juicer_agents.runtime.navigation_tool_surface import (
    NavigationToolSurfaceMiddleware,
)
from vla_data_juicer_agents.web.agent_session import (
    AgentScopeWebSessionManager,
    TurnSubmissionPending,
    TurnSubmissionResult,
)
from vla_data_juicer_agents.web.app import create_app
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import (
    HumanDecisionRequest,
    InterruptResponse,
    SessionRecord,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore
from vla_data_juicer_agents.web.sse import stream_session_events


class FakeAgentScopeRuntime:
    def __init__(self, turn_id: str = "turn_runtime_1") -> None:
        self.turn_id = turn_id
        self.submissions: list[dict[str, str]] = []

    async def submit_user_message(
        self,
        *,
        web_session_id: str,
        message: str,
        message_id: str | None = None,
        turn_id: str | None = None,
        on_admitted=None,
    ) -> str:
        self.submissions.append({"web_session_id": web_session_id, "message": message})
        if on_admitted is not None:
            on_admitted()
        return turn_id or self.turn_id


class StoreAwareAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.web_session_store = None
        self.web_event_publisher = None

    def set_web_session_store(self, store) -> None:
        self.web_session_store = store

    def set_web_transport(self, store, publisher) -> None:
        self.web_session_store = store
        self.web_event_publisher = publisher

class RejectingAgentScopeRuntime(FakeAgentScopeRuntime):
    async def submit_user_message(
        self,
        *,
        web_session_id: str,
        message: str,
        message_id: str | None = None,
        turn_id: str | None = None,
        on_admitted=None,
    ) -> str:
        self.submissions.append({"web_session_id": web_session_id, "message": message})
        raise RuntimeError("turn rejected")


class InterruptingAgentScopeRuntime(FakeAgentScopeRuntime):
    def __init__(
        self,
        turn_id: str = "turn_runtime_1",
        interrupted: bool = True,
        stopped_tool_call_ids: list[str] | None = None,
    ) -> None:
        super().__init__(turn_id=turn_id)
        self.interrupted = interrupted
        self.stopped_tool_call_ids = stopped_tool_call_ids or []
        self.interrupts: list[str] = []

    async def interrupt_web_session(self, *, web_session_id: str) -> InterruptResponse:
        self.interrupts.append(web_session_id)
        return InterruptResponse(
            interrupted=self.interrupted,
            stopped_tool_call_ids=self.stopped_tool_call_ids,
        )


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
        self.published: list[tuple[str, dict]] = []
        self.background_tasks: dict[str, dict[str, str]] = {}
        self.registry_getall_calls: list[str] = []
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

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))

    async def registry_getall(self, namespace: str) -> dict[str, str]:
        self.registry_getall_calls.append(namespace)
        return dict(self.background_tasks.get(namespace, {}))

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
        self.interrupt_calls: list[tuple[str, str, str]] = []
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

    async def interrupt(self, user_id, session_id, agent_id):
        self.interrupt_calls.append((user_id, session_id, agent_id))


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


class AdmissionTaskRegistry:
    """Task registry that exposes the real owner task to admission-race tests."""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.spawned_tasks: list[asyncio.Task] = []

    def spawn(self, coroutine, *, session_id):
        task = asyncio.create_task(coroutine)
        self.tasks[session_id] = task
        self.spawned_tasks.append(task)

        def cleanup(completed: asyncio.Task) -> None:
            if self.tasks.get(session_id) is completed:
                self.tasks.pop(session_id, None)

        task.add_done_callback(cleanup)
        return task

    async def drain(self) -> None:
        await asyncio.gather(*self.spawned_tasks, return_exceptions=True)


class BoundaryAdmissionChatService(FakeChatService):
    """Drive the real run-boundary middleware after a deterministic gate."""

    def __init__(self) -> None:
        super().__init__()
        self.runtime = None
        self.run_started = asyncio.Event()
        self.allow_boundary = asyncio.Event()
        self.boundary_entered = asyncio.Event()
        self.finish_run = asyncio.Event()

    async def run(self, *, user_id, session_id, agent_id, input_msg):
        self.run_started.set()
        await self.allow_boundary.wait()
        middleware = DataPilotRunBoundaryMiddleware(session_id, self.runtime)

        async def handler(**_kwargs):
            self.boundary_entered.set()
            yield ReplyStartEvent(
                session_id=session_id,
                reply_id="admitted-reply",
                name="MainRouterAgent",
            )
            await self.finish_run.wait()

        async for _event in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": input_msg},
            handler,
        ):
            pass
        await super().run(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            input_msg=input_msg,
        )


class AdmissionSharedBus:
    def __init__(self) -> None:
        self.registries: dict[str, dict[str, str]] = {}
        self.subscribers: dict[str, list[asyncio.Queue]] = {}
        self.fail_owner_publish = False

    async def publish(self, key: str, payload: dict) -> None:
        for queue in list(self.subscribers.get(key, [])):
            await queue.put(dict(payload))

    async def subscribe(self, key: str, *, on_ready=None):
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(key, []).append(queue)
        if on_ready is not None:
            on_ready()
        try:
            while True:
                yield await queue.get()
        finally:
            self.subscribers[key].remove(queue)

    async def registry_set(
        self,
        namespace: str,
        field: str,
        value: str,
        *,
        ttl_secs: int | None = None,
    ) -> None:
        del ttl_secs
        if self.fail_owner_publish and namespace.startswith("datapilot:stop:owners:"):
            raise ConnectionError("owner registry unavailable")
        self.registries.setdefault(namespace, {})[field] = value

    async def registry_del(self, namespace: str, field: str) -> None:
        self.registries.setdefault(namespace, {}).pop(field, None)

    async def registry_getall(self, namespace: str) -> dict[str, str]:
        return dict(self.registries.get(namespace, {}))


class RecoveringAdmissionSubscriberBus(AdmissionSharedBus):
    def __init__(self) -> None:
        super().__init__()
        self.break_subscription = asyncio.Event()
        self.subscription_failed = asyncio.Event()
        self.allow_resubscribe = asyncio.Event()
        self.resubscribed = asyncio.Event()
        self.subscribe_attempts = 0
        self.gate_owner_refresh = False
        self.owner_refresh_started = asyncio.Event()
        self.allow_owner_refresh = asyncio.Event()

    async def subscribe(self, key: str, *, on_ready=None):
        self.subscribe_attempts += 1
        if self.subscribe_attempts > 1:
            await self.allow_resubscribe.wait()
            if on_ready is not None:
                on_ready()
            self.resubscribed.set()
            async for payload in super().subscribe(key):
                yield payload
            return

        if on_ready is not None:
            on_ready()
        await self.break_subscription.wait()
        self.subscription_failed.set()
        raise ConnectionError("stop subscriber connection lost")
        yield  # pragma: no cover

    async def registry_set(self, namespace, field, value, *, ttl_secs=None):
        if self.gate_owner_refresh and namespace.startswith("datapilot:stop:owners:"):
            self.owner_refresh_started.set()
            await self.allow_owner_refresh.wait()
        await super().registry_set(
            namespace,
            field,
            value,
            ttl_secs=ttl_secs,
        )


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
async def test_runtime_publishes_turn_terminal_after_registry_cleanup_on_pre_reply_failure(
    tmp_path: Path,
) -> None:
    class TaskRegistry:
        def __init__(self) -> None:
            self.tasks: dict[str, asyncio.Task] = {}

        def spawn(self, coroutine, *, session_id):
            task = asyncio.create_task(coroutine)
            self.tasks[session_id] = task

            def cleanup(completed: asyncio.Task) -> None:
                if self.tasks.get(session_id) is completed:
                    self.tasks.pop(session_id, None)

            task.add_done_callback(cleanup)
            return task

        def get(self, session_id):
            return self.tasks.get(session_id)

    class FailingBeforeReplyChatService:
        async def run(self, **_kwargs) -> None:
            raise RuntimeError("model failed before reply")

    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("early failure")
    registry = TaskRegistry()
    runtime = _runtime(chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = FailingBeforeReplyChatService()
    runtime.set_web_session_store(store)

    with pytest.raises(RuntimeError, match="model failed before reply"):
        await runtime.submit_user_message(
            web_session_id=public_session.id,
            message="fail before reply",
        )
    internal_session_id = f"{public_session.id}__main-router-agent"
    task = registry.get(internal_session_id)
    assert task is None
    for _ in range(10):
        if store.get_session(public_session.id).events:
            break
        await asyncio.sleep(0)

    assert registry.get(internal_session_id) is None
    terminal = store.get_session(public_session.id).events[-1].event
    assert terminal["type"] == "CUSTOM"
    assert terminal["name"] == "datapilot_run_terminal"
    assert terminal["value"]["turn_id"].startswith("turn_")
    assert terminal["value"]["status"] == "failure"


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
    assert len(middlewares) == 4
    assert isinstance(middlewares[0], DataPilotRunBoundaryMiddleware)
    assert isinstance(middlewares[1], DataPilotReplyProjectionMiddleware)
    assert isinstance(middlewares[2], DataPilotToolOutcomeMiddleware)
    middleware = middlewares[3]
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
async def test_standalone_ensure_rejects_deletion_before_agentscope_upsert(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "ensure-deleting.sqlite")
    web_session = store.create_session("deleting before ensure")
    store.begin_session_deletion(web_session.id)
    storage = FakeAgentScopeStorage()
    runtime = _runtime(storage=storage, chat_run_registry=FakeChatRunRegistry())
    runtime.set_web_session_store(store)

    with pytest.raises(RuntimeError, match="session deletion is pending"):
        await runtime.ensure_web_session(
            web_session.id,
            agent_id="navigation-data-agent",
            model="qwen-navigation",
        )

    assert storage.sessions == []


@pytest.mark.asyncio
async def test_standalone_ensure_warm_cache_rechecks_deletion_fence(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "warm-ensure-deleting.sqlite")
    web_session = store.create_session("warm cache before deletion")
    storage = FakeAgentScopeStorage()
    runtime = _runtime(storage=storage, chat_run_registry=FakeChatRunRegistry())
    runtime.set_web_session_store(store)
    cached_session_id = await runtime.ensure_web_session(
        web_session.id,
        agent_id="navigation-data-agent",
        model="qwen-navigation",
    )
    assert runtime.web_sessions[web_session.id] == (
        "navigation-data-agent",
        cached_session_id,
    )

    store.begin_session_deletion(web_session.id)

    with pytest.raises(RuntimeError, match="session deletion is pending"):
        await runtime.ensure_web_session(
            web_session.id,
            agent_id="navigation-data-agent",
            model="qwen-navigation",
        )

    assert len(storage.sessions) == 1


@pytest.mark.asyncio
async def test_mapping_store_failure_does_not_delete_unproven_upsert_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingSessionService:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_session(self, _user_id, _agent_id, session_id):
            self.deleted.append(session_id)
            return True

    store = WebSessionStore(tmp_path / "mapping-write-failure.sqlite")
    web_session = store.create_session("mapping write failure")
    storage = FakeAgentScopeStorage()
    runtime = _runtime(storage=storage, chat_run_registry=FakeChatRunRegistry())
    session_service = RecordingSessionService()
    runtime.app.state.session_service = session_service
    runtime.set_web_session_store(store)

    def fail_mapping(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("mapping store unavailable")

    monkeypatch.setattr(store, "save_agentscope_session_mapping", fail_mapping)

    with pytest.raises(sqlite3.OperationalError, match="mapping store unavailable"):
        await runtime.ensure_web_session(
            web_session.id,
            agent_id="navigation-data-agent",
            model="qwen-navigation",
        )

    assert len(storage.sessions) == 1
    assert session_service.deleted == []

def test_store_lists_all_agent_mappings_for_one_web_session(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    first_session = store.create_session("first")
    second_session = store.create_session("second")
    store.save_agentscope_session_mapping(
        first_session.id,
        agent_id="historical-worker-agent",
        agentscope_session_id="historical-worker-session",
    )
    store.save_agentscope_session_mapping(
        first_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="current-navigation-session",
    )
    store.save_agentscope_session_mapping(
        second_session.id,
        agent_id="other-agent",
        agentscope_session_id="other-session",
    )

    mappings = store.list_agentscope_session_mappings(first_session.id)

    assert {
        (mapping.agent_id, mapping.agentscope_session_id)
        for mapping in mappings
    } == {
        ("historical-worker-agent", "historical-worker-session"),
        ("navigation-data-agent", "current-navigation-session"),
    }


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

    result = await runtime.interrupt_web_session(web_session_id="web-1")

    assert result == InterruptResponse(interrupted=False, stopped_tool_call_ids=[])
    assert message_bus.published == []


@pytest.mark.asyncio
async def test_runtime_interrupt_web_session_stops_active_chat_and_public_tool(
    tmp_path: Path,
) -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("active stop")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="as-session-1",
    )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    published = []
    runtime.set_web_transport(
        store,
        lambda session_id, record: published.append((session_id, record)),
    )
    cancellation = CancellationContext()
    runtime.register_run_cancellation("as-session-1", cancellation)

    class OrderCheckingChatService(FakeChatService):
        async def interrupt(self, user_id, session_id, agent_id):
            assert cancellation.cancelled is True
            await super().interrupt(user_id, session_id, agent_id)

    runtime.app.state.chat_service = OrderCheckingChatService()

    result = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert result == InterruptResponse(
        interrupted=True,
        stopped_tool_call_ids=["call-1"],
    )
    assert cancellation.cancelled is True
    assert runtime.app.state.chat_service.interrupt_calls == [
        ("alice", "as-session-1", "main-router-agent")
    ]
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [(row.tool_call_id, row.status) for row in detail.tool_runs] == [
        ("call-1", "stopped")
    ]
    terminal = [
        record
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ]
    assert len(terminal) == 1
    assert terminal[0].event["value"] == {
        "tool_call_id": "call-1",
        "status": "stopped",
        "summary": "已由用户停止",
    }
    assert len(terminal[0].dedupe_key) == 64
    assert "call-1" not in terminal[0].dedupe_key
    assert [(session_id, record.id) for session_id, record in published] == [
        (public_session.id, record.id) for record in detail.events
    ]


@pytest.mark.asyncio
async def test_explicit_stop_preserves_public_tool_call_id_containing_private_identity(
    tmp_path: Path,
) -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("correlation identity")
    private_session_id = "private-as-session"
    public_tool_call_id = f"public-call-containing-{private_session_id}"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=private_session_id,
    )
    store.start_tool_run(
        public_session.id,
        public_tool_call_id,
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    runtime.set_web_session_store(store)

    result = await runtime.interrupt_web_session(web_session_id=public_session.id)

    detail = store.get_session(public_session.id)
    assert detail is not None
    assert result.stopped_tool_call_ids == [public_tool_call_id]
    assert detail.tool_runs[0].tool_call_id == public_tool_call_id
    terminal = next(
        record.event
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    )
    assert terminal["value"]["tool_call_id"] == public_tool_call_id


@pytest.mark.asyncio
async def test_tool_terminal_event_append_failure_rolls_back_ledger_transition(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("atomic terminal")
    internal_session_id = "internal-navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    runtime = _runtime(workspace_root=tmp_path)
    runtime.set_web_session_store(store)
    await runtime.start_public_tool(
        internal_session_id,
        tool_call_id="call-atomic",
        tool_name="extract",
    )

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_atomic_terminal
            BEFORE INSERT ON public_events
            BEGIN
                SELECT RAISE(ABORT, 'terminal append failed');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="terminal append failed"):
        await runtime.finish_public_tool(
            internal_session_id,
            tool_call_id="call-atomic",
            status="success",
            summary="done",
            error_type=None,
        )

    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "running"
    assert detail.events == []


@pytest.mark.asyncio
async def test_runtime_interrupt_web_session_interrupts_hitl_and_all_historical_mappings(
    tmp_path: Path,
) -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("parked HITL")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="historical-worker-agent",
        agentscope_session_id="historical-worker-session",
    )
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="parked-hitl-session",
    )
    published = []

    def publish(session_id, record):
        detail = store.get_session(session_id)
        assert detail is not None
        assert record.id in [persisted.id for persisted in detail.events]
        published.append(record)

    runtime.set_web_transport(store, publish)

    result = await runtime.interrupt_web_session(web_session_id=public_session.id)
    repeated = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert result == InterruptResponse(interrupted=True, stopped_tool_call_ids=[])
    assert repeated == InterruptResponse(interrupted=True, stopped_tool_call_ids=[])
    assert runtime.app.state.chat_service.interrupt_calls == [
        ("alice", "historical-worker-session", "historical-worker-agent"),
        ("alice", "parked-hitl-session", "navigation-data-agent"),
        ("alice", "historical-worker-session", "historical-worker-agent"),
        ("alice", "parked-hitl-session", "navigation-data-agent"),
    ]
    detail = store.get_session(public_session.id)
    assert detail is not None
    resolved = [
        record
        for record in detail.events
        if record.event.get("name") == "datapilot_human_decision_resolved"
    ]
    assert len(resolved) == 1
    assert resolved[0].event["value"] == {"all": True, "reason": "stopped"}
    assert len(resolved[0].dedupe_key) == 64
    serialized = json.dumps(resolved[0].model_dump(mode="json"), ensure_ascii=False)
    assert "historical-worker-session" not in serialized
    assert "parked-hitl-session" not in serialized
    assert published[0] == resolved[0]


@pytest.mark.asyncio
async def test_runtime_interrupt_web_session_cancels_background_registry_keys_once(
    tmp_path: Path,
) -> None:
    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("background tools")
    mappings = [
        ("main-router-agent", "router-session"),
        ("navigation-data-agent", "navigation-session"),
    ]
    for agent_id, session_id in mappings:
        store.save_agentscope_session_mapping(
            public_session.id,
            agent_id=agent_id,
            agentscope_session_id=session_id,
        )
    runtime.set_web_session_store(store)
    message_bus.background_tasks = {
        MessageBusKeys.bg_tasks("router-session"): {
            "bg-task-1": '{"tool_name": "extract"}',
            "shared-task": '{"tool_name": "shared-router"}',
        },
        MessageBusKeys.bg_tasks("navigation-session"): {
            "shared-task": '{"tool_name": "shared-navigation"}',
            "bg-task-2": '{"tool_name": "finish"}',
        },
    }

    result = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert result.interrupted is True
    assert message_bus.registry_getall_calls == [
        MessageBusKeys.bg_tasks("router-session"),
        MessageBusKeys.bg_tasks("navigation-session"),
    ]
    assert message_bus.published == [
        (MessageBusKeys.task_cancel_channel(), {"task_id": "bg-task-1"}),
        (MessageBusKeys.task_cancel_channel(), {"task_id": "shared-task"}),
        (MessageBusKeys.task_cancel_channel(), {"task_id": "bg-task-2"}),
    ]


@pytest.mark.asyncio
async def test_explicit_stop_of_remote_chat_turn_requires_owner_ack_before_terminal(
    tmp_path: Path,
) -> None:
    bus = AdmissionSharedBus()
    runtime = _runtime(message_bus=bus)
    owner = _runtime(message_bus=bus)
    store = WebSessionStore(tmp_path / "dead-owner-explicit-stop.sqlite")
    session = store.create_session("dead owner")
    internal_session_id = f"{session.id}__main-router-agent"
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    generation = store.begin_execution_generation(session.id)
    store.claim_user_message(
        session.id,
        "local-dead-owner",
        "run",
        runtime_id=owner.runtime_id,
        turn_id="turn_dead_owner",
        ttl_seconds=30.0,
    )
    store.commit_user_message(
        session.id,
        "local-dead-owner",
        "run",
        runtime_id=owner.runtime_id,
        ttl_seconds=30.0,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE session_turn_admissions SET expires_at = 100 WHERE message_id = ?",
            ("local-dead-owner",),
        )
    published = []
    runtime.set_web_transport(store, lambda _session_id, record: published.append(record))
    owner.set_web_session_store(WebSessionStore(store.db_path))

    await runtime.start_stop_coordinator()
    try:
        with pytest.raises(RuntimeError, match="owner acknowledgement"):
            await runtime.interrupt_web_session(web_session_id=session.id)

        assert store.user_message_turn_status(session.id, "local-dead-owner") == "admitted"
        assert [
            record
            for record in store.list_public_events(session.id)
            if record.event.get("name") == "datapilot_run_terminal"
        ] == []

        cancellation = CancellationContext()
        owner.register_run_cancellation(
            internal_session_id,
            cancellation,
            generation=generation,
        )

        async def finish_remote_chat() -> None:
            while not cancellation.cancelled:
                await asyncio.sleep(0)
            owner.clear_run_cancellation(internal_session_id, cancellation)

        remote_chat = asyncio.create_task(finish_remote_chat())
        await owner.start_stop_coordinator()
        assert owner.stop_coordinator is not None
        await owner.stop_coordinator.refresh_owners()

        result = await runtime.interrupt_web_session(web_session_id=session.id)
        await remote_chat
    finally:
        await owner.stop_stop_coordinator()
        await runtime.stop_stop_coordinator()

    assert result.interrupted is True
    assert store.user_message_turn_status(session.id, "local-dead-owner") == "terminal"
    terminal = [
        record
        for record in store.list_public_events(session.id)
        if record.event.get("name") == "datapilot_run_terminal"
    ]
    assert len(terminal) == 1
    assert terminal[0].event["value"]["status"] == "stopped"
    assert terminal[0] in published
    assert store.claim_user_message(
        session.id,
        "local-after-dead-owner",
        "continue",
        runtime_id="runtime-2",
        turn_id="turn_after_dead_owner",
        ttl_seconds=30.0,
    ) == "claimed"


@pytest.mark.asyncio
async def test_runtime_interrupt_failure_attempts_all_mappings_and_is_retryable(
    tmp_path: Path,
) -> None:
    class FailOnceChatService(FakeChatService):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def interrupt(self, user_id, session_id, agent_id):
            self.interrupt_calls.append((user_id, session_id, agent_id))
            if session_id == "first-session" and not self.failed:
                self.failed = True
                raise ConnectionError("interrupt transport unavailable")

    message_bus = FakeAgentScopeMessageBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    runtime.app.state.chat_service = FailOnceChatService()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("partial stop")
    for agent_id, session_id in (
        ("a-agent", "first-session"),
        ("b-agent", "second-session"),
    ):
        store.save_agentscope_session_mapping(
            public_session.id,
            agent_id=agent_id,
            agentscope_session_id=session_id,
        )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    runtime.set_web_session_store(store)

    with pytest.raises(RuntimeError, match="explicit stop cancellation failed"):
        await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert runtime.app.state.chat_service.interrupt_calls == [
        ("alice", "first-session", "a-agent"),
        ("alice", "second-session", "b-agent"),
    ]
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [(row.tool_call_id, row.status) for row in detail.tool_runs] == [
        ("call-1", "running")
    ]
    assert detail.events == []

    retried = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert retried.stopped_tool_call_ids == ["call-1"]


@pytest.mark.asyncio
async def test_runtime_background_cancel_failure_attempts_remaining_tasks_and_retries(
    tmp_path: Path,
) -> None:
    class FailOnceCancelBus(FakeAgentScopeMessageBus):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def publish(self, key: str, payload: dict) -> None:
            self.published.append((key, payload))
            if payload["task_id"] == "bg-task-1" and not self.failed:
                self.failed = True
                raise ConnectionError("task cancel broadcast unavailable")

    message_bus = FailOnceCancelBus()
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry(), message_bus=message_bus)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("partial background stop")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="router-session",
    )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    runtime.set_web_session_store(store)
    message_bus.background_tasks = {
        MessageBusKeys.bg_tasks("router-session"): {
            "bg-task-1": "metadata-one",
            "bg-task-2": "metadata-two",
        }
    }

    with pytest.raises(RuntimeError, match="explicit stop cancellation failed"):
        await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert message_bus.published == [
        (MessageBusKeys.task_cancel_channel(), {"task_id": "bg-task-1"}),
        (MessageBusKeys.task_cancel_channel(), {"task_id": "bg-task-2"}),
    ]
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "running"

    retried = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert retried.stopped_tool_call_ids == ["call-1"]
    assert message_bus.published[-2:] == [
        (MessageBusKeys.task_cancel_channel(), {"task_id": "bg-task-1"}),
        (MessageBusKeys.task_cancel_channel(), {"task_id": "bg-task-2"}),
    ]


@pytest.mark.asyncio
async def test_runtime_interrupt_treats_false_and_missing_chat_session_as_idempotent(
    tmp_path: Path,
) -> None:
    class StaleChatService(FakeChatService):
        async def interrupt(self, user_id, session_id, agent_id):
            self.interrupt_calls.append((user_id, session_id, agent_id))
            if session_id == "missing-session":
                raise LookupError("session was already removed")
            return False

    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime.app.state.chat_service = StaleChatService()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("stale mappings")
    for agent_id, session_id in (
        ("a-agent", "missing-session"),
        ("b-agent", "idle-session"),
    ):
        store.save_agentscope_session_mapping(
            public_session.id,
            agent_id=agent_id,
            agentscope_session_id=session_id,
        )
    runtime.set_web_session_store(store)

    result = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert result == InterruptResponse(interrupted=True, stopped_tool_call_ids=[])


@pytest.mark.asyncio
async def test_runtime_interrupt_durable_stop_failure_does_not_publish_stopped(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("durable failure")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="router-session",
    )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    published = []
    runtime.set_web_transport(store, lambda *_args: published.append(_args))

    def fail_stop(_session_id: str, _request_id: str, _terminal_event_factory):
        raise sqlite3.OperationalError("disk full")

    store.complete_stop_request_with_terminal_events = fail_stop

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        await runtime.interrupt_web_session(web_session_id=public_session.id)

    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [(row.tool_call_id, row.status) for row in detail.tool_runs] == [
        ("call-1", "running")
    ]
    assert detail.events == []
    assert published == []


@pytest.mark.asyncio
async def test_runtime_interrupt_live_publish_failure_keeps_durable_stopped(
    tmp_path: Path,
    caplog,
) -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("browser disconnected")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="router-session",
    )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )

    async def fail_publish(_session_id, _record):
        raise ConnectionError("browser disconnected")

    runtime.set_web_transport(store, fail_publish)

    result = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert result.stopped_tool_call_ids == ["call-1"]
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "stopped"
    assert [
        record.event["value"]["status"]
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ] == ["stopped"]
    assert "Live public event publish failed" in caplog.text


@pytest.mark.asyncio
async def test_explicit_stop_serializes_real_middleware_cancellation_as_stopped(
    tmp_path: Path,
) -> None:
    remote_entered = asyncio.Event()
    release_remote = asyncio.Event()

    class BlockingChatService(FakeChatService):
        async def interrupt(self, user_id, session_id, agent_id):
            self.interrupt_calls.append((user_id, session_id, agent_id))
            remote_entered.set()
            await release_remote.wait()

    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime.app.state.chat_service = BlockingChatService()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("cancel race")
    internal_session_id = "navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    runtime.set_web_session_store(store)
    cancellation = CancellationContext()
    runtime.register_run_cancellation(internal_session_id, cancellation)
    middleware = DataPilotToolOutcomeMiddleware(internal_session_id, runtime)
    tool_call = ToolCallBlock(id="call-race", name="extract", input="{}")
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def handler(**_kwargs):
        handler_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        yield  # pragma: no cover

    async def consume() -> None:
        async with cancellation.track_agent(internal_session_id):
            async for _ in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": tool_call},
                handler,
            ):
                pass

    consumer = asyncio.create_task(consume())
    await handler_started.wait()
    stop_task = asyncio.create_task(
        runtime.interrupt_web_session(web_session_id=public_session.id)
    )
    status_during_stop = None
    consumer_done_during_stop = None
    try:
        await remote_entered.wait()
        await handler_cancelled.wait()
        await asyncio.sleep(0)

        detail_during_stop = store.get_session(public_session.id)
        assert detail_during_stop is not None
        status_during_stop = detail_during_stop.tool_runs[0].status
        consumer_done_during_stop = consumer.done()
    finally:
        release_remote.set()
        stop_result, cancellation_result = await asyncio.gather(
            stop_task,
            consumer,
            return_exceptions=True,
        )

    assert status_during_stop == "running"
    # The pending-stop barrier lets the cancelled owner finish and release its
    # lease before the stopped transaction; this quiescence is now required.
    assert consumer_done_during_stop is True
    assert isinstance(stop_result, InterruptResponse)
    assert stop_result.stopped_tool_call_ids == ["call-race"]
    assert isinstance(cancellation_result, asyncio.CancelledError)
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "stopped"
    assert [
        record.event["value"]["status"]
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ] == ["stopped"]


@pytest.mark.asyncio
async def test_explicit_stop_serializes_real_tool_response_as_stopped(
    tmp_path: Path,
) -> None:
    remote_entered = asyncio.Event()
    release_remote = asyncio.Event()

    class BlockingChatService(FakeChatService):
        async def interrupt(self, user_id, session_id, agent_id):
            self.interrupt_calls.append((user_id, session_id, agent_id))
            remote_entered.set()
            await release_remote.wait()

    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime.app.state.chat_service = BlockingChatService()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("success race")
    internal_session_id = "navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    runtime.set_web_session_store(store)
    outcome_attempted = asyncio.Event()
    finish_public_tool = runtime.finish_public_tool

    async def observed_finish(*args, **kwargs):
        outcome_attempted.set()
        return await finish_public_tool(*args, **kwargs)

    runtime.finish_public_tool = observed_finish
    middleware = DataPilotToolOutcomeMiddleware(internal_session_id, runtime)
    tool_call = ToolCallBlock(id="call-race", name="extract", input="{}")
    emit_response = asyncio.Event()

    async def handler(**_kwargs):
        await emit_response.wait()
        yield ToolResponse(id="call-race", state=ToolResultState.SUCCESS)

    async def consume() -> list[ToolResponse]:
        return [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": tool_call},
                handler,
            )
        ]

    consumer = asyncio.create_task(consume())
    while store.get_session(public_session.id).tool_runs == []:
        await asyncio.sleep(0)
    stop_task = asyncio.create_task(
        runtime.interrupt_web_session(web_session_id=public_session.id)
    )
    status_during_stop = None
    consumer_done_during_stop = None
    try:
        await remote_entered.wait()
        emit_response.set()
        await outcome_attempted.wait()
        await asyncio.sleep(0)

        detail_during_stop = store.get_session(public_session.id)
        assert detail_during_stop is not None
        status_during_stop = detail_during_stop.tool_runs[0].status
        consumer_done_during_stop = consumer.done()
    finally:
        release_remote.set()
        stop_result, yielded = await asyncio.gather(
            stop_task,
            consumer,
            return_exceptions=True,
        )

    assert status_during_stop == "running"
    assert consumer_done_during_stop is True
    assert stop_result.stopped_tool_call_ids == ["call-race"]
    assert isinstance(yielded, asyncio.CancelledError)
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "stopped"
    assert [
        record.event["value"]["status"]
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ] == ["stopped"]


@pytest.mark.asyncio
async def test_failed_remote_stop_keeps_pending_ledger_running_for_retry(
    tmp_path: Path,
) -> None:
    remote_entered = asyncio.Event()
    release_remote = asyncio.Event()

    class FailingBlockingChatService(FakeChatService):
        async def interrupt(self, user_id, session_id, agent_id):
            self.interrupt_calls.append((user_id, session_id, agent_id))
            remote_entered.set()
            await release_remote.wait()
            raise ConnectionError("remote stop failed")

    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime.app.state.chat_service = FailingBlockingChatService()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("failed stop race")
    internal_session_id = "navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    runtime.set_web_session_store(store)
    cancellation = CancellationContext()
    runtime.register_run_cancellation(internal_session_id, cancellation)
    middleware = DataPilotToolOutcomeMiddleware(internal_session_id, runtime)
    tool_call = ToolCallBlock(id="call-race", name="extract", input="{}")
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def handler(**_kwargs):
        handler_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        yield  # pragma: no cover

    async def consume() -> None:
        async with cancellation.track_agent(internal_session_id):
            async for _ in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": tool_call},
                handler,
            ):
                pass

    consumer = asyncio.create_task(consume())
    await handler_started.wait()
    stop_task = asyncio.create_task(
        runtime.interrupt_web_session(web_session_id=public_session.id)
    )
    status_during_stop = None
    try:
        await remote_entered.wait()
        await handler_cancelled.wait()
        await asyncio.sleep(0)
        status_during_stop = store.get_session(public_session.id).tool_runs[0].status
    finally:
        release_remote.set()
        stop_result, cancellation_result = await asyncio.gather(
            stop_task,
            consumer,
            return_exceptions=True,
        )

    assert status_during_stop == "running"
    assert isinstance(stop_result, RuntimeError)
    assert isinstance(cancellation_result, asyncio.CancelledError)
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "running"
    assert detail.events == []
    assert store.stop_request_is_pending(public_session.id, 0) is True


@pytest.mark.asyncio
async def test_explicit_stop_rolls_back_all_tools_when_second_terminal_insert_fails(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("atomic stop")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="router-session",
    )
    store.append_public_event(
        public_session.id,
        hashlib.sha256(b"existing-event").hexdigest(),
        {"type": "REPLY_START", "reply_id": "reply-1", "name": "DataPilot"},
    )
    for tool_call_id in ("call-1", "call-2"):
        store.start_tool_run(
            public_session.id,
            tool_call_id,
            "extract",
            f"2026-07-15T00:00:0{tool_call_id[-1]}.000+00:00",
        )
    runtime.set_web_session_store(store)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_second_stop_terminal
            BEFORE INSERT ON public_events
            WHEN NEW.event_json LIKE '%\"tool_call_id\": \"call-2\"%'
            BEGIN
                SELECT RAISE(ABORT, 'second stop terminal failed');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="second stop terminal failed"):
        await runtime.interrupt_web_session(web_session_id=public_session.id)

    failed_detail = store.get_session(public_session.id)
    assert failed_detail is not None
    assert [row.status for row in failed_detail.tool_runs] == ["running", "running"]
    assert [record.sequence for record in failed_detail.events] == [1]

    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TRIGGER fail_second_stop_terminal")

    retried = await runtime.interrupt_web_session(web_session_id=public_session.id)

    assert retried.stopped_tool_call_ids == ["call-1", "call-2"]
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [row.status for row in detail.tool_runs] == ["stopped", "stopped"]
    terminals = [
        record
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ]
    assert [record.sequence for record in terminals] == [2, 3]
    assert [record.event["value"]["tool_call_id"] for record in terminals] == [
        "call-1",
        "call-2",
    ]
    assert all(len(record.dedupe_key) == 64 for record in terminals)
    assert all("call-" not in record.dedupe_key for record in terminals)


@pytest.mark.asyncio
async def test_concurrent_explicit_stops_are_serialized_and_terminalize_once(
    tmp_path: Path,
) -> None:
    first_remote_entered = asyncio.Event()
    release_first_remote = asyncio.Event()
    second_remote_entered = asyncio.Event()

    class SequencedChatService(FakeChatService):
        async def interrupt(self, user_id, session_id, agent_id):
            self.interrupt_calls.append((user_id, session_id, agent_id))
            if len(self.interrupt_calls) == 1:
                first_remote_entered.set()
                await release_first_remote.wait()
            else:
                second_remote_entered.set()

    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    runtime.app.state.chat_service = SequencedChatService()
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("concurrent stops")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id="router-session",
    )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    runtime.set_web_session_store(store)

    first = asyncio.create_task(
        runtime.interrupt_web_session(web_session_id=public_session.id)
    )
    await first_remote_entered.wait()
    second = asyncio.create_task(
        runtime.interrupt_web_session(web_session_id=public_session.id)
    )
    await asyncio.sleep(0)

    assert second_remote_entered.is_set() is False
    release_first_remote.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert second_remote_entered.is_set() is True
    assert first_result.stopped_tool_call_ids == ["call-1"]
    assert second_result.stopped_tool_call_ids == []
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].status == "stopped"
    assert [
        record.event["value"]["status"]
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ] == ["stopped"]


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


def test_runtime_cancels_retained_tool_leases_across_generations_and_multiple_tools() -> None:
    runtime = _runtime()
    first = CancellationContext()
    second = CancellationContext()
    session_id = "as-session-lease"

    runtime.register_run_cancellation(session_id, first, generation=1)
    runtime.retain_tool_cancellation(session_id, "call-1", first)
    runtime.retain_tool_cancellation(session_id, "call-2", first)
    runtime.clear_run_cancellation(session_id, first)
    runtime.register_run_cancellation(session_id, second, generation=2)
    runtime.retain_tool_cancellation(session_id, "call-3", second)
    runtime.clear_run_cancellation(session_id, second)

    assert runtime.run_cancellation(session_id) is second
    assert runtime.cancel_run_cancellations(session_id) is True
    assert first.cancelled is True
    assert second.cancelled is True
    runtime.discard_run_cancellations(session_id)
    assert runtime.run_cancellation(session_id) is None


@pytest.mark.asyncio
async def test_direct_wakeup_keeps_foreground_lease_when_last_background_tool_finishes() -> None:
    runtime = _runtime()
    cancellation = CancellationContext()
    session_id = "as-direct-wakeup"
    runtime.register_run_cancellation(session_id, cancellation, generation=1)
    runtime.retain_tool_cancellation(session_id, "call-1", cancellation)
    runtime.clear_run_cancellation(session_id, cancellation)
    middleware = DataPilotRunBoundaryMiddleware(session_id, runtime)

    async def handler(**_kwargs):
        runtime.release_tool_cancellation(session_id, "call-1", cancellation)
        assert runtime.run_cancellation(session_id) is cancellation
        yield ReplyStartEvent(
            session_id=session_id,
            reply_id="reply-wakeup",
            name="MainRouterAgent",
        )

    yielded = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": None},
            handler,
        )
    ]

    assert len(yielded) == 1
    assert runtime.run_cancellation(session_id) is None


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
async def test_submit_user_message_waits_until_run_boundary_admits_the_turn(
    tmp_path: Path,
) -> None:
    """A returned turn id is an admission ACK, not merely a local spawn ACK."""
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "admission-await.sqlite")
    session = store.create_session("admission await")
    runtime.set_web_session_store(store)

    submission = asyncio.create_task(
        runtime.submit_user_message(web_session_id=session.id, message="start")
    )
    await service.run_started.wait()
    await asyncio.sleep(0)
    returned_before_admission = submission.done()

    service.allow_boundary.set()
    await service.boundary_entered.wait()
    turn_id = await submission
    service.finish_run.set()
    await registry.drain()

    assert returned_before_admission is False
    assert turn_id.startswith("turn_")


@pytest.mark.asyncio
async def test_http_turn_response_waits_for_run_boundary_admission(
    tmp_path: Path,
) -> None:
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None)
    agentscope_app = FastAPI()
    agentscope_app.state.chat_run_registry = registry
    agentscope_app.state.chat_service = service
    runtime.app = agentscope_app
    service.runtime = runtime
    app = create_app(
        working_dir=str(tmp_path / "workspace"),
        db_path=tmp_path / "http-admission.sqlite",
        agentscope_runtime=runtime,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post("/api/sessions", json={"message": "start", "creation_id": "local-create-http-admission"})
        session_id = created.json()["session"]["id"]
        request = asyncio.create_task(
            client.post(
                f"/api/sessions/{session_id}/turns",
                json={"message": "start", "message_id": "local-http-admission-1"},
            )
        )
        await service.run_started.wait()
        await asyncio.sleep(0)
        responded_before_admission = request.done()
        messages_before_admission = app.state.store.get_session(session_id).messages

        service.allow_boundary.set()
        await service.boundary_entered.wait()
        response = await request
        messages_after_admission = app.state.store.get_session(session_id).messages
        service.finish_run.set()
        await registry.drain()

    assert responded_before_admission is False
    assert messages_before_admission == []
    assert response.status_code == 200
    assert [message.id for message in messages_after_admission] == [
        "local-http-admission-1"
    ]


@pytest.mark.asyncio
async def test_exact_message_commit_failure_blocks_model_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None)
    agentscope_app = FastAPI()
    agentscope_app.state.chat_run_registry = registry
    agentscope_app.state.chat_service = service
    runtime.app = agentscope_app
    service.runtime = runtime
    app = create_app(
        working_dir=str(tmp_path / "workspace"),
        db_path=tmp_path / "failed-exact-commit.sqlite",
        agentscope_runtime=runtime,
    )

    def fail_commit(*_args, **_kwargs):
        raise sqlite3.OperationalError("exact message commit failed")

    monkeypatch.setattr(app.state.store, "commit_user_message", fail_commit)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        created = await client.post("/api/sessions", json={"message": "start", "creation_id": "local-create-failed-commit"})
        session_id = created.json()["session"]["id"]
        request = asyncio.create_task(
            client.post(
                f"/api/sessions/{session_id}/turns",
                json={
                    "message": "must not execute",
                    "message_id": "local-failed-exact-commit",
                },
            )
        )
        await service.run_started.wait()
        service.allow_boundary.set()
        response = await request
        await registry.drain()

    assert response.status_code == 500
    assert service.boundary_entered.is_set() is False
    assert service.runs == []
    detail = app.state.store.get_session(session_id)
    assert detail is not None
    assert detail.messages == []
    assert detail.events == []


@pytest.mark.asyncio
async def test_admitted_turn_heartbeat_failure_cancels_live_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    runtime.admission_renew_interval_seconds = 0.01
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "turn-heartbeat-failure.sqlite")
    session = store.create_session("heartbeat failure")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    service.allow_boundary.set()

    result = await manager.submit_turn(
        session.id,
        "must cancel",
        message_id="local-heartbeat-failure",
    )
    assert isinstance(result, TurnSubmissionResult)
    await service.boundary_entered.wait()

    def fail_renew(*_args, **_kwargs):
        raise sqlite3.OperationalError("execution heartbeat unavailable")

    monkeypatch.setattr(store, "renew_user_message", fail_renew)
    await asyncio.wait_for(registry.drain(), timeout=0.5)
    for _ in range(50):
        detail = store.get_session(session.id)
        if detail and any(
            event.event.get("name") == "datapilot_run_terminal"
            for event in detail.events
        ):
            break
        await asyncio.sleep(0.01)

    assert service.finish_run.is_set() is False
    assert store.user_message_turn_status(
        session.id,
        "local-heartbeat-failure",
    ) == "terminal"
    detail = store.get_session(session.id)
    assert detail is not None
    terminal = [
        event
        for event in detail.events
        if event.event.get("name") == "datapilot_run_terminal"
    ]
    assert len(terminal) == 1
    assert terminal[0].event["value"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_turn_fence_waits_for_real_background_worker_quiescence(
    tmp_path: Path,
) -> None:
    class BackgroundWorkerChatService(BoundaryAdmissionChatService):
        def __init__(self) -> None:
            super().__init__()
            self.worker_started = threading.Event()
            self.release_worker = threading.Event()
            self.background_tasks: list[asyncio.Task] = []

        async def run(self, *, user_id, session_id, agent_id, input_msg):
            self.run_started.set()
            await self.allow_boundary.wait()
            middleware = DataPilotRunBoundaryMiddleware(session_id, self.runtime)

            async def handler(**_kwargs):
                cancellation = current_cancellation()
                assert cancellation is not None
                worker_token = cancellation.reserve_worker()
                await self.runtime.start_public_tool(
                    session_id,
                    tool_call_id="call-background-worker",
                    tool_name="extract_and_sync",
                )

                def work() -> None:
                    try:
                        self.worker_started.set()
                        assert self.release_worker.wait(timeout=2.0)
                    finally:
                        cancellation.finish_worker(worker_token)

                async def finish_background_tool() -> None:
                    worker_task = asyncio.create_task(asyncio.to_thread(work))
                    try:
                        await asyncio.shield(worker_task)
                    finally:
                        await self.runtime.finish_public_tool(
                            session_id,
                            tool_call_id="call-background-worker",
                            status="success",
                            summary="done",
                            error_type=None,
                        )

                self.background_tasks.append(asyncio.create_task(finish_background_tool()))
                self.boundary_entered.set()
                yield ReplyStartEvent(
                    session_id=session_id,
                    reply_id="background-reply",
                    name="MainRouterAgent",
                )

            async for _event in middleware.on_reply(
                SimpleNamespace(name="MainRouterAgent"),
                {"inputs": input_msg},
                handler,
            ):
                pass
            await super(BoundaryAdmissionChatService, self).run(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                input_msg=input_msg,
            )

    registry = AdmissionTaskRegistry()
    service = BackgroundWorkerChatService()
    runtime = _runtime(chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    service.allow_boundary.set()
    store = WebSessionStore(tmp_path / "turn-background-quiescence.sqlite")
    session = store.create_session("background quiescence")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)

    first = await manager.submit_turn(
        session.id,
        "first",
        message_id="local-background-first",
    )
    assert isinstance(first, TurnSubmissionResult)
    await asyncio.to_thread(service.worker_started.wait, 0.5)
    await registry.drain()

    assert store.user_message_turn_status(
        session.id,
        "local-background-first",
    ) == "admitted"
    with pytest.raises(RuntimeError, match="still active"):
        await manager.submit_turn(
            session.id,
            "second",
            message_id="local-background-second",
        )

    service.release_worker.set()
    await asyncio.gather(*service.background_tasks)
    for _ in range(50):
        if store.user_message_turn_status(
            session.id,
            "local-background-first",
        ) == "terminal":
            break
        await asyncio.sleep(0.01)

    assert store.user_message_turn_status(
        session.id,
        "local-background-first",
    ) == "terminal"
    second = await manager.submit_turn(
        session.id,
        "second",
        message_id="local-background-second",
    )
    assert isinstance(second, TurnSubmissionResult)


@pytest.mark.asyncio
async def test_session_snapshot_does_not_unlock_expired_possibly_live_turn(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    app = create_app(
        working_dir=str(tmp_path / "workspace"),
        db_path=tmp_path / "snapshot-recovers-turn.sqlite",
        agentscope_runtime=runtime,
    )
    store = app.state.store
    session = store.create_session("recover crashed turn")
    message_id = "local-snapshot-recovery"
    turn_id = "turn_snapshot_recovery"
    store.claim_user_message(
        session.id,
        message_id,
        "run",
        runtime_id=runtime.runtime_id,
        turn_id=turn_id,
        ttl_seconds=30.0,
    )
    store.commit_user_message(
        session.id,
        message_id,
        "run",
        runtime_id=runtime.runtime_id,
        ttl_seconds=30.0,
    )
    reply_start = ReplyStartEvent(
        session_id=f"{session.id}__main-router-agent",
        reply_id="crashed-reply",
        name="MainRouterAgent",
    ).model_dump(mode="json")
    store.append_public_event(
        session.id,
        hashlib.sha256(b"crashed-reply-start").hexdigest(),
        reply_start,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE session_turn_admissions SET expires_at = 100 WHERE message_id = ?",
            (message_id,),
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        async with app.state.bus.subscribe(session.id) as queue:
            response = await client.get(f"/api/sessions/{session.id}")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.05)

    assert response.status_code == 200
    terminal_events = [
        event
        for event in response.json()["session"]["events"]
        if event["event"].get("name") == "datapilot_run_terminal"
    ]
    assert terminal_events == []
    assert store.user_message_turn_status(session.id, message_id) == "admitted"


@pytest.mark.asyncio
async def test_admission_owner_is_published_before_submit_is_accepted(
    tmp_path: Path,
) -> None:
    bus = AdmissionSharedBus()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "admission-owner.sqlite")
    session = store.create_session("owner before accept")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    await runtime.start_stop_coordinator()

    submission = asyncio.create_task(manager.submit_turn(session.id, "start"))
    try:
        await service.run_started.wait()
        await asyncio.sleep(0)
        namespace = f"datapilot:stop:owners:{session.id}__main-router-agent"
        owners_before_boundary = await bus.registry_getall(namespace)
        accepted_before_boundary = submission.done()

        service.allow_boundary.set()
        await service.boundary_entered.wait()
        await submission
        service.finish_run.set()
        await registry.drain()
    finally:
        service.allow_boundary.set()
        service.finish_run.set()
        await asyncio.gather(submission, return_exceptions=True)
        await registry.drain()
        await runtime.stop_stop_coordinator()

    assert owners_before_boundary
    assert accepted_before_boundary is False


@pytest.mark.asyncio
async def test_stop_before_admission_rejects_turn_and_keeps_stop_fence(
    tmp_path: Path,
) -> None:
    bus = AdmissionSharedBus()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "stop-before-admission.sqlite")
    session = store.create_session("stop before admission")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.01
    await runtime.start_stop_coordinator()

    submission = asyncio.create_task(manager.submit_turn(session.id, "start"))
    try:
        await service.run_started.wait()
        stop_result = await runtime.interrupt_web_session(web_session_id=session.id)
        service.allow_boundary.set()
        service.finish_run.set()
        submission_result = (await asyncio.gather(submission, return_exceptions=True))[0]
        await registry.drain()
    finally:
        service.allow_boundary.set()
        service.finish_run.set()
        await asyncio.gather(submission, return_exceptions=True)
        await registry.drain()
        await runtime.stop_stop_coordinator()

    detail = store.get_session(session.id)
    assert stop_result.interrupted is True
    assert isinstance(submission_result, RuntimeError)
    assert detail is not None
    assert detail.messages == []
    assert store.execution_generation_is_fenced(session.id) is True


@pytest.mark.asyncio
async def test_stop_while_submit_is_blocked_in_session_upsert_rejects_stale_admission(
    tmp_path: Path,
) -> None:
    class GatedStorage(FakeAgentScopeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.upsert_started = asyncio.Event()
            self.allow_upsert = asyncio.Event()

        async def upsert_session(self, *args, **kwargs):
            self.upsert_started.set()
            await self.allow_upsert.wait()
            return await super().upsert_session(*args, **kwargs)

    bus = AdmissionSharedBus()
    storage = GatedStorage()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(storage=storage, chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "stop-during-upsert.sqlite")
    session = store.create_session("stop during upsert")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    await runtime.start_stop_coordinator()

    submission = asyncio.create_task(manager.submit_turn(session.id, "start"))
    try:
        await asyncio.wait_for(storage.upsert_started.wait(), timeout=0.3)
        stopped = await runtime.interrupt_web_session(web_session_id=session.id)
        assert store.execution_generation_is_fenced(session.id) is True

        storage.allow_upsert.set()
        service.allow_boundary.set()
        service.finish_run.set()
        submit_result = (await asyncio.gather(submission, return_exceptions=True))[0]
        await registry.drain()

        detail = store.get_session(session.id)
        assert stopped.interrupted is False
        assert isinstance(submit_result, RuntimeError)
        assert store.execution_generation_is_fenced(session.id) is True
        assert detail is not None
        assert detail.messages == []
    finally:
        storage.allow_upsert.set()
        service.allow_boundary.set()
        service.finish_run.set()
        await asyncio.gather(submission, return_exceptions=True)
        await registry.drain()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_owner_publish_failure_cancels_run_and_rejects_submission(
    tmp_path: Path,
) -> None:
    bus = AdmissionSharedBus()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "owner-publish-failure.sqlite")
    session = store.create_session("owner publish failure")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    await runtime.start_stop_coordinator()
    bus.fail_owner_publish = True

    submission = asyncio.create_task(manager.submit_turn(session.id, "start"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    service.allow_boundary.set()
    service.finish_run.set()
    result = (await asyncio.gather(submission, return_exceptions=True))[0]
    await registry.drain()
    bus.fail_owner_publish = False
    await runtime.stop_stop_coordinator()

    detail = store.get_session(session.id)
    assert isinstance(result, RuntimeError)
    assert registry.tasks == {}
    assert store.current_execution_generation(session.id) == 0
    assert detail is not None
    assert detail.messages == []


@pytest.mark.asyncio
async def test_dead_stop_subscriber_rejects_standalone_ensure_before_upsert(
    tmp_path: Path,
) -> None:
    bus = RecoveringAdmissionSubscriberBus()
    storage = FakeAgentScopeStorage()
    runtime = _runtime(storage=storage, message_bus=bus)
    store = WebSessionStore(tmp_path / "subscriber-dead-ensure.sqlite")
    session = store.create_session("subscriber dead ensure")
    runtime.set_web_session_store(store)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.005
    await runtime.start_stop_coordinator()
    try:
        bus.break_subscription.set()
        await asyncio.wait_for(bus.subscription_failed.wait(), timeout=0.2)
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="coordinator is unhealthy"):
            await runtime.ensure_web_session(
                session.id,
                agent_id=runtime.config.main_router_agent_id,
                model=runtime.config.router_model,
            )

        assert storage.sessions == []
        assert runtime.web_sessions == {}
        assert store.list_agentscope_session_mappings(session.id) == []
        assert store.current_execution_generation(session.id) == 0
        assert store.get_session(session.id).messages == []
    finally:
        bus.allow_resubscribe.set()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_dead_stop_subscriber_rejects_submit_then_recovery_accepts(
    tmp_path: Path,
) -> None:
    bus = RecoveringAdmissionSubscriberBus()
    storage = FakeAgentScopeStorage()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(
        storage=storage,
        chat_run_registry=None,
        message_bus=bus,
    )
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "subscriber-dead-submit.sqlite")
    session = store.create_session("subscriber dead submit")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.005
    await runtime.start_stop_coordinator()
    try:
        # If admission is accidentally allowed, let the run terminate so this
        # regression fails promptly instead of hanging at the boundary.
        service.allow_boundary.set()
        service.finish_run.set()
        bus.break_subscription.set()
        await asyncio.wait_for(bus.subscription_failed.wait(), timeout=0.2)
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="coordinator is unhealthy"):
            await manager.submit_turn(session.id, "must reject")

        assert storage.sessions == []
        assert registry.tasks == {}
        assert store.current_execution_generation(session.id) == 0
        assert store.get_session(session.id).messages == []

        bus.allow_resubscribe.set()
        await asyncio.wait_for(bus.resubscribed.wait(), timeout=0.2)
        assert runtime.stop_coordinator.healthy is True
        service.allow_boundary.set()
        service.finish_run.set()

        turn_id = await manager.submit_turn(session.id, "accept after recovery")
        await registry.drain()

        assert turn_id.startswith("turn_")
        assert len(storage.sessions) == 1
        assert len(service.runs) == 1
        assert store.current_execution_generation(session.id) == 1
    finally:
        bus.allow_resubscribe.set()
        service.allow_boundary.set()
        service.finish_run.set()
        await registry.drain()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_subscriber_death_between_preflight_and_boundary_keeps_generation_zero(
    tmp_path: Path,
) -> None:
    bus = RecoveringAdmissionSubscriberBus()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "subscriber-dies-at-boundary.sqlite")
    session = store.create_session("subscriber dies at boundary")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.005
    await runtime.start_stop_coordinator()

    submission = asyncio.create_task(manager.submit_turn(session.id, "race"))
    try:
        await asyncio.wait_for(service.run_started.wait(), timeout=0.2)
        bus.break_subscription.set()
        await asyncio.wait_for(bus.subscription_failed.wait(), timeout=0.2)
        await asyncio.sleep(0)
        service.allow_boundary.set()
        service.finish_run.set()

        result = (await asyncio.gather(submission, return_exceptions=True))[0]
        await registry.drain()

        assert store.current_execution_generation(session.id) == 0
        assert isinstance(result, RuntimeError)
        assert "coordinator is unhealthy" in str(result)
        assert store.get_session(session.id).messages == []
        assert service.runs == []
    finally:
        bus.allow_resubscribe.set()
        service.allow_boundary.set()
        service.finish_run.set()
        await asyncio.gather(submission, return_exceptions=True)
        await registry.drain()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_subscriber_death_during_boundary_refresh_keeps_generation_zero(
    tmp_path: Path,
) -> None:
    bus = RecoveringAdmissionSubscriberBus()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "subscriber-dies-during-refresh.sqlite")
    session = store.create_session("subscriber dies during refresh")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.005
    await runtime.start_stop_coordinator()

    submission = asyncio.create_task(manager.submit_turn(session.id, "refresh race"))
    try:
        await asyncio.wait_for(service.run_started.wait(), timeout=0.2)
        bus.gate_owner_refresh = True
        service.allow_boundary.set()
        await asyncio.wait_for(bus.owner_refresh_started.wait(), timeout=0.2)
        bus.break_subscription.set()
        await asyncio.wait_for(bus.subscription_failed.wait(), timeout=0.2)
        bus.allow_owner_refresh.set()
        service.finish_run.set()

        result = (await asyncio.gather(submission, return_exceptions=True))[0]
        await registry.drain()

        assert store.current_execution_generation(session.id) == 0
        assert isinstance(result, RuntimeError)
        assert "coordinator is unhealthy" in str(result)
        assert store.get_session(session.id).messages == []
        assert service.runs == []
    finally:
        bus.allow_owner_refresh.set()
        bus.allow_resubscribe.set()
        service.allow_boundary.set()
        service.finish_run.set()
        await asyncio.gather(submission, return_exceptions=True)
        await registry.drain()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_dead_stop_subscriber_keeps_stop_and_delete_fail_closed(
    tmp_path: Path,
) -> None:
    class RecordingSessionService:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_session(self, _user_id, _agent_id, session_id):
            self.deleted.append(session_id)
            return True

    class NavigationControl:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_control_state_for_web_session(self, web_session_id: str) -> None:
            self.deleted.append(web_session_id)

    bus = RecoveringAdmissionSubscriberBus()
    runtime = _runtime(message_bus=bus)
    service = RecordingSessionService()
    navigation = NavigationControl()
    runtime.app.state.session_service = service
    runtime._navigation_services = lambda: navigation
    store = WebSessionStore(tmp_path / "subscriber-dead-stop-delete.sqlite")
    session = store.create_session("subscriber dead stop delete")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id=runtime.config.main_router_agent_id,
        agentscope_session_id=f"{session.id}__main-router-agent",
    )
    runtime.set_web_session_store(store)
    assert runtime.stop_coordinator is not None
    runtime.stop_coordinator.retry_interval = 0.005
    await runtime.start_stop_coordinator()
    try:
        bus.break_subscription.set()
        await asyncio.wait_for(bus.subscription_failed.wait(), timeout=0.2)
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="coordinator is unhealthy"):
            await runtime.interrupt_web_session(web_session_id=session.id)
        with pytest.raises(RuntimeError, match="coordinator is unhealthy"):
            await runtime.delete_web_session(session.id)

        assert store.get_session(session.id) is not None
        assert service.deleted == []
        assert navigation.deleted == []
    finally:
        bus.allow_resubscribe.set()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_delete_rereads_mappings_after_remote_quiescence(
    tmp_path: Path,
) -> None:
    class RecordingSessionService:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str]] = []

        async def delete_session(self, _user_id, agent_id, session_id):
            self.deleted.append((agent_id, session_id))
            return True

    class NavigationControl:
        def delete_control_state_for_web_session(self, _web_session_id: str) -> None:
            return None

    store = WebSessionStore(tmp_path / "delete-late-mapping.sqlite")
    session = store.create_session("delete late mapping")
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id="router-session",
    )
    runtime = _runtime(
        chat_run_registry=FakeChatRunRegistry(),
        message_bus=FakeAgentScopeMessageBus(),
    )
    session_service = RecordingSessionService()
    runtime.app.state.session_service = session_service
    runtime._navigation_services = lambda: NavigationControl()
    runtime.set_web_session_store(store)
    runtime.stop_coordinator = SimpleNamespace(started=True)

    async def quiesce_then_publish_late_mapping(*, web_session_id: str):
        assert web_session_id == session.id
        # Simulate a legacy/external writer that bypassed the new store fence and
        # committed while distributed cancellation was reaching quiescence.
        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                """
                INSERT INTO agentscope_sessions (
                    web_session_id, agent_id, agentscope_session_id,
                    active, updated_at
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (
                    session.id,
                    "late-navigation-agent",
                    "late-navigation-session",
                    "2026-07-15T00:00:00.000+00:00",
                ),
            )
        return InterruptResponse(interrupted=True)

    runtime._interrupt_web_session_serialized = quiesce_then_publish_late_mapping

    deleted = await runtime.delete_web_session(session.id)

    assert deleted is True
    assert set(session_service.deleted) == {
        ("main-router-agent", "router-session"),
        ("late-navigation-agent", "late-navigation-session"),
    }


@pytest.mark.asyncio
async def test_delete_waits_for_submit_blocked_in_agentscope_session_upsert(
    tmp_path: Path,
) -> None:
    class GatedStorage(FakeAgentScopeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.upsert_started = asyncio.Event()
            self.allow_upsert = asyncio.Event()

        async def upsert_session(self, *args, **kwargs):
            self.upsert_started.set()
            await self.allow_upsert.wait()
            return await super().upsert_session(*args, **kwargs)

    class RecordingSessionService:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_session(self, _user_id, _agent_id, session_id):
            self.deleted.append(session_id)
            return True

    class NavigationControl:
        def delete_control_state_for_web_session(self, _web_session_id: str) -> None:
            return None

    bus = AdmissionSharedBus()
    storage = GatedStorage()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    session_service = RecordingSessionService()
    runtime = _runtime(storage=storage, chat_run_registry=None, message_bus=bus)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    runtime.app.state.session_service = session_service
    runtime._navigation_services = lambda: NavigationControl()
    runtime.admission_lease_ttl_seconds = 0.05
    runtime.admission_renew_interval_seconds = 0.01
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "delete-during-upsert.sqlite")
    session = store.create_session("delete during upsert")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    assert runtime.stop_coordinator is not None
    await runtime.start_stop_coordinator()

    submission = asyncio.create_task(manager.submit_turn(session.id, "start"))
    deletion: asyncio.Task | None = None
    try:
        await asyncio.wait_for(storage.upsert_started.wait(), timeout=0.3)
        deletion = asyncio.create_task(manager.delete_session(session.id))
        for _ in range(30):
            if store.session_deletion_is_pending(session.id):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.08)
        assert store.session_deletion_is_pending(session.id) is True
        assert deletion.done() is False
        assert session_service.deleted == []

        storage.allow_upsert.set()
        service.allow_boundary.set()
        service.finish_run.set()
        submit_result = (await asyncio.gather(submission, return_exceptions=True))[0]
        await deletion
        await registry.drain()

        assert isinstance(submit_result, RuntimeError)
        assert store.get_session(session.id) is None
        assert len(storage.sessions) == 1
        assert session_service.deleted == [storage.sessions[0]["id"]]
    finally:
        storage.allow_upsert.set()
        service.allow_boundary.set()
        service.finish_run.set()
        await asyncio.gather(submission, return_exceptions=True)
        if deletion is not None:
            await asyncio.gather(deletion, return_exceptions=True)
        await registry.drain()
        await runtime.stop_stop_coordinator()


@pytest.mark.asyncio
async def test_admission_renew_failure_cancels_submit_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GatedStorage(FakeAgentScopeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.upsert_started = asyncio.Event()
            self.allow_upsert = asyncio.Event()

        async def upsert_session(self, *args, **kwargs):
            self.upsert_started.set()
            await self.allow_upsert.wait()
            return await super().upsert_session(*args, **kwargs)

    storage = GatedStorage()
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(storage=storage, chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    runtime.admission_lease_ttl_seconds = 0.1
    runtime.admission_renew_interval_seconds = 0.01
    service.runtime = runtime
    store = WebSessionStore(tmp_path / "renew-failure.sqlite")
    session = store.create_session("renew failure")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    original_renew = store.renew_session_run_admission
    renew_calls = 0

    def fail_first_renew(*args, **kwargs):
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 1:
            raise sqlite3.OperationalError("renew transport failed")
        return original_renew(*args, **kwargs)

    monkeypatch.setattr(store, "renew_session_run_admission", fail_first_renew)

    first = asyncio.create_task(manager.submit_turn(session.id, "first"))
    await asyncio.wait_for(storage.upsert_started.wait(), timeout=0.3)
    first_result = (await asyncio.gather(first, return_exceptions=True))[0]

    assert isinstance(first_result, RuntimeError)
    assert "lease renewal failed" in str(first_result)
    assert store.session_run_admission_is_pending(session.id) is False
    assert storage.sessions == []

    storage.allow_upsert.set()
    service.allow_boundary.set()
    service.finish_run.set()
    turn_id = await manager.submit_turn(session.id, "retry")
    await registry.drain()

    detail = store.get_session(session.id)
    assert turn_id.startswith("turn_")
    assert detail is not None
    assert [message.content for message in detail.messages] == ["retry"]


@pytest.mark.asyncio
async def test_transient_admission_release_failure_retries_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    runtime.admission_release_retry_delays = (0.0, 0.0)
    service.runtime = runtime
    service.allow_boundary.set()
    service.finish_run.set()
    store = WebSessionStore(tmp_path / "release-retry.sqlite")
    session = store.create_session("release retry")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    original_release = store.release_session_run_admission
    release_calls = 0

    def fail_first_release(*args, **kwargs):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise sqlite3.OperationalError("release transport failed")
        return original_release(*args, **kwargs)

    monkeypatch.setattr(store, "release_session_run_admission", fail_first_release)

    turn_id = await manager.submit_turn(session.id, "start")
    await registry.drain()

    assert turn_id.startswith("turn_")
    assert release_calls == 2
    assert store.session_run_admission_is_pending(session.id) is False


@pytest.mark.asyncio
async def test_successful_admission_wins_same_tick_heartbeat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    store = WebSessionStore(tmp_path / "same-tick-heartbeat.sqlite")
    session = store.create_session("same tick heartbeat")
    runtime.set_web_session_store(store)

    async def admitted_run(**_kwargs) -> str:
        await asyncio.sleep(0)
        return "admitted-session"

    async def failed_heartbeat(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("renew failed after admission")

    monkeypatch.setattr(runtime, "_start_agent_run_with_ticket", admitted_run)
    monkeypatch.setattr(
        runtime,
        "_heartbeat_session_run_admission",
        failed_heartbeat,
    )

    result = await runtime._start_agent_run(
        web_session_id=session.id,
        agent_id="main-router-agent",
        model="qwen-test",
        message="start",
    )

    assert result == "admitted-session"
    assert store.session_run_admission_is_pending(session.id) is False


@pytest.mark.asyncio
async def test_successful_admission_is_not_reversed_by_permanent_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdmissionTaskRegistry()
    service = BoundaryAdmissionChatService()
    runtime = _runtime(chat_run_registry=None)
    runtime.app.state.chat_run_registry = registry
    runtime.app.state.chat_service = service
    runtime.admission_lease_ttl_seconds = 0.05
    runtime.admission_release_retry_delays = (0.0, 0.0)
    service.runtime = runtime
    service.allow_boundary.set()
    service.finish_run.set()
    store = WebSessionStore(tmp_path / "release-exhausted.sqlite")
    session = store.create_session("release exhausted")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)

    def fail_release(*_args, **_kwargs):
        raise sqlite3.OperationalError("release remained unavailable")

    monkeypatch.setattr(store, "release_session_run_admission", fail_release)

    turn_id = await manager.submit_turn(session.id, "start")
    await registry.drain()

    assert turn_id.startswith("turn_")
    assert len(service.runs) == 1
    assert store.current_execution_generation(session.id) == 1
    assert store.session_run_admission_is_pending(session.id) is True
    assert store.reap_expired_session_run_admissions(
        session.id,
        now=datetime.now(UTC) + timedelta(seconds=1),
    ) == 1


@pytest.mark.asyncio
async def test_reentered_run_boundary_commits_execution_generation_exactly_once(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "admission-exactly-once.sqlite")
    session = store.create_session("exactly once")
    internal_session_id = f"{session.id}__main-router-agent"
    store.save_agentscope_session_mapping(
        session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    runtime = _runtime()
    runtime.set_web_session_store(store)
    cancellation = CancellationContext()
    runtime.register_run_cancellation(internal_session_id, cancellation)
    middleware = DataPilotRunBoundaryMiddleware(internal_session_id, runtime)
    begin_calls: list[str] = []
    original_begin = store.begin_execution_generation

    def record_begin(
        session_id: str,
        *,
        expected_boundary: tuple[int, int | None] | None = None,
    ) -> int:
        begin_calls.append(session_id)
        return original_begin(
            session_id,
            expected_boundary=expected_boundary,
        )

    store.begin_execution_generation = record_begin  # type: ignore[method-assign]

    async def handler(**_kwargs):
        yield ReplyStartEvent(
            session_id=internal_session_id,
            reply_id="same-run-reply",
            name="MainRouterAgent",
        )

    for _ in range(2):
        async for _event in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": UserMsg(name="user", content="same admitted turn")},
            handler,
        ):
            pass

    runtime.clear_run_cancellation(internal_session_id, cancellation)

    assert begin_calls == [session.id]
    assert store.current_execution_generation(session.id) == 1


@pytest.mark.asyncio
async def test_rejected_turn_does_not_clear_stopped_generation_or_admit_stale_wakeup(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("stopped duplicate admission")
    internal_session_id = f"{public_session.id}__main-router-agent"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    store.begin_execution_generation(public_session.id)
    store.stop_open_tool_runs_with_terminal_events(
        public_session.id,
        lambda _row: pytest.fail("no running tool should require an event"),
    )
    chat_run_registry = FakeChatRunRegistry(reject_duplicate_active=True)
    chat_run_registry.active_session_ids.add(internal_session_id)
    runtime = _runtime(chat_run_registry=chat_run_registry)
    runtime.set_web_session_store(store)

    with pytest.raises(RuntimeError, match="already active"):
        await runtime.submit_user_message(
            web_session_id=public_session.id,
            message="should not be admitted",
        )

    assert store.execution_generation_is_stopped(public_session.id) is True
    middleware = DataPilotRunBoundaryMiddleware(internal_session_id, runtime)
    model_calls = 0

    async def handler(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        yield ReplyStartEvent(
            session_id=internal_session_id,
            reply_id="unexpected-reply",
            name="MainRouterAgent",
        )

    suppressed = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": None},
            handler,
        )
    ]

    assert model_calls == 0
    assert len(suppressed) == 1
    assert isinstance(suppressed[0], Msg)

    # A later retry clears the boundary only after it reaches middleware.
    chat_run_registry.active_session_ids.discard(internal_session_id)
    await runtime.submit_user_message(
        web_session_id=public_session.id,
        message="accepted retry",
    )
    assert store.execution_generation_is_stopped(public_session.id) is True
    admitted = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": UserMsg(name="user", content="accepted retry")},
            handler,
        )
    ]
    assert model_calls == 1
    assert isinstance(admitted[0], ReplyStartEvent)
    assert store.execution_generation_is_stopped(public_session.id) is False
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_accepted_user_run_advances_stopped_generation_once_at_run_boundary(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("accepted admission")
    internal_session_id = f"{public_session.id}__main-router-agent"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    store.begin_execution_generation(public_session.id)
    store.stop_open_tool_runs_with_terminal_events(
        public_session.id,
        lambda _row: pytest.fail("no running tool should require an event"),
    )
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)
    runtime.set_web_session_store(store)
    begin_calls: list[str] = []
    original_begin = store.begin_execution_generation

    def record_begin(
        session_id: str,
        *,
        expected_boundary: tuple[int, int | None] | None = None,
    ) -> int:
        begin_calls.append(session_id)
        return original_begin(
            session_id,
            expected_boundary=expected_boundary,
        )

    store.begin_execution_generation = record_begin  # type: ignore[method-assign]

    await runtime.submit_user_message(
        web_session_id=public_session.id,
        message="continue",
    )

    # Local registry admission alone must not clear the durable stop fence.
    assert store.execution_generation_is_stopped(public_session.id) is True
    cancellation = runtime.run_cancellation(internal_session_id)
    assert cancellation is not None
    middleware = DataPilotRunBoundaryMiddleware(internal_session_id, runtime)

    async def handler(**_kwargs):
        # Re-entry for the same admitted lease is idempotent.
        runtime.admit_user_execution_generation(
            internal_session_id,
            cancellation,
        )
        yield ReplyStartEvent(
            session_id=internal_session_id,
            reply_id="accepted-reply",
            name="MainRouterAgent",
        )

    yielded = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": UserMsg(name="user", content="continue")},
            handler,
        )
    ]

    assert isinstance(yielded[0], ReplyStartEvent)
    assert begin_calls == [public_session.id]
    assert store.execution_generation_is_stopped(public_session.id) is False

    async def wakeup_handler(**_kwargs):
        yield ReplyStartEvent(
            session_id=internal_session_id,
            reply_id="wakeup-reply",
            name="MainRouterAgent",
        )

    wakeup = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": None},
            wakeup_handler,
        )
    ]
    assert isinstance(wakeup[0], ReplyStartEvent)
    assert begin_calls == [public_session.id]
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
async def test_runtime_persists_human_decision_resolution_before_best_effort_publish(
    tmp_path: Path,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "private-as-session")] = (
        _agentscope_session_record()
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("decision resolution")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="private-as-session",
    )
    published = []

    def publish(session_id, record):
        detail = store.get_session(session_id)
        assert detail is not None
        assert record.id in [persisted.id for persisted in detail.events]
        published.append(record)

    runtime.set_web_transport(store, publish)
    decision = {
        "action": "confirm",
        "request_id": "public-request-1",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
    }

    accepted = await runtime.submit_human_decision(
        web_session_id=public_session.id,
        decision=decision,
    )
    await chat_run_registry.drain()

    assert accepted is True
    detail = store.get_session(public_session.id)
    assert detail is not None
    resolved = [
        record
        for record in detail.events
        if record.event.get("name") == "datapilot_human_decision_resolved"
    ]
    assert len(resolved) == 1
    assert resolved[0].event["value"] == {
        "request_id": "public-request-1",
        "reason": "submitted",
    }
    assert len(resolved[0].dedupe_key) == 64
    serialized = json.dumps(resolved[0].model_dump(mode="json"), ensure_ascii=False)
    assert "private-as-session" not in serialized
    assert "navigation-data-agent" not in serialized
    assert published == resolved


@pytest.mark.asyncio
async def test_runtime_human_decision_resolution_append_failure_can_be_retried(
    tmp_path: Path,
) -> None:
    chat_run_registry = FakeChatRunRegistry()
    storage = FakeAgentScopeStorage()
    storage.session_records[("alice", "navigation-data-agent", "as-session-1")] = (
        _agentscope_session_record()
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("retry resolution append")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    runtime.set_web_session_store(store)
    original_append = store.append_public_event
    fail_resolution_once = True

    def flaky_append(session_id, dedupe_key, event):
        nonlocal fail_resolution_once
        if (
            fail_resolution_once
            and event.get("name") == "datapilot_human_decision_resolved"
        ):
            fail_resolution_once = False
            raise RuntimeError("resolution append failed")
        return original_append(session_id, dedupe_key, event)

    store.append_public_event = flaky_append
    decision = {
        "action": "confirm",
        "request_id": "request-1",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
    }

    try:
        with pytest.raises(RuntimeError, match="resolution append failed"):
            await runtime.submit_human_decision(
                web_session_id=public_session.id,
                decision=decision,
            )
    finally:
        await chat_run_registry.drain()

    retried = await runtime.submit_human_decision(
        web_session_id=public_session.id,
        decision=decision,
    )

    assert retried is True
    assert chat_run_registry.spawns == []
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [
        record.event["value"]
        for record in detail.events
        if record.event.get("name") == "datapilot_human_decision_resolved"
    ] == [{"request_id": "request-1", "reason": "submitted"}]


@pytest.mark.asyncio
async def test_runtime_rejected_human_decision_does_not_persist_resolution(
    tmp_path: Path,
) -> None:
    runtime = _runtime(storage=FakeAgentScopeStorage(), chat_run_registry=FakeChatRunRegistry())
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("rejected decision")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="as-session-1",
    )
    runtime.set_web_session_store(store)

    accepted = await runtime.submit_human_decision(
        web_session_id=public_session.id,
        decision={
            "action": "confirm",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
            "reply_id": "reply-1",
        },
    )

    assert accepted is False
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.events == []


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
    assert len(runtime.app.state.chat_service.seen_cancellations) == 1
    assert isinstance(
        runtime.app.state.chat_service.seen_cancellations[0],
        CancellationContext,
    )
    assert runtime.run_cancellation("as-session-1") is None
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
async def test_reply_projection_hides_historical_and_in_memory_mapping_identities(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("处理历史导航数据")
    historical_agent_id = "historical-worker-agent"
    historical_session_id = "historical-worker-session"
    current_session_id = "current-navigation-session"
    memory_agent_id = "memory-installed-agent"
    memory_session_id = "memory-installed-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id=historical_agent_id,
        agentscope_session_id=historical_session_id,
    )
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=current_session_id,
    )
    runtime = _runtime(workspace_root=tmp_path)
    runtime.web_sessions[public_session.id] = (
        "navigation-data-agent",
        current_session_id,
    )
    runtime.web_sessions["memory-public-session"] = (
        memory_agent_id,
        memory_session_id,
    )
    runtime.set_web_transport(store, None)
    assert {
        historical_agent_id,
        historical_session_id,
        memory_agent_id,
        memory_session_id,
    } <= runtime.projection_private_identities()
    middleware = DataPilotReplyProjectionMiddleware(current_session_id, runtime)
    source_items = [
        ReplyStartEvent(
            session_id=current_session_id,
            reply_id="reply-identity",
            name="NavigationDataAgent",
        ),
        HintBlockEvent(
            reply_id="reply-identity",
            block_id="hint-identity",
            source=f"{historical_agent_id}/{historical_session_id}",
            hint="continue",
        ),
        CustomEvent(
            name="datapilot_progress",
            metadata={"safe": "keep"},
            value={
                "description": (
                    f"{historical_agent_id} at {historical_session_id}; "
                    f"{memory_agent_id} at {memory_session_id}"
                ),
                "safe": "keep-value",
            },
        ),
        ReplyEndEvent(
            session_id=current_session_id,
            reply_id="reply-identity",
        ),
    ]

    async def handler(**_kwargs):
        for item in source_items:
            yield item

    yielded = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="NavigationDataAgent"),
            {},
            handler,
        )
    ]

    assert yielded == source_items
    detail = store.get_session(public_session.id)
    assert detail is not None
    public_json = json.dumps(
        [record.event for record in detail.events],
        ensure_ascii=False,
    )
    for private_identity in (
        historical_agent_id,
        historical_session_id,
        memory_agent_id,
        memory_session_id,
    ):
        assert private_identity not in public_json
    assert detail.events[1].event["source"] == "DataPilot/DataPilot"
    assert detail.events[2].event["metadata"] == {"safe": "keep"}
    assert detail.events[2].event["value"]["safe"] == "keep-value"


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
    assert detail.model_dump(
        exclude={"messages", "events", "tool_runs", "last_sequence"}
    ) == session.model_dump()
    assert detail.messages == []
    assert detail.events == []


def test_agentscope_web_session_manager_attaches_transport_to_runtime(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = StoreAwareAgentScopeRuntime()
    publisher = lambda _session_id, _record: None

    AgentScopeWebSessionManager(
        store=store,
        runtime=runtime,
        event_callback=publisher,
    )

    assert runtime.web_session_store is store
    assert runtime.web_event_publisher is publisher


@pytest.mark.asyncio
async def test_runtime_projection_persists_then_broadcasts_and_ignores_late_terminal(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("处理导航数据")
    internal_session_id = "internal-navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    published = []

    async def publish(session_id, record) -> None:
        detail = store.get_session(session_id)
        assert detail is not None
        assert detail.events[-1].id == record.id
        published.append((session_id, record))

    runtime = _runtime(workspace_root=tmp_path)
    runtime.web_sessions["other-public"] = (
        "main-router-agent",
        "other-internal-router-session",
    )
    assert {
        "MainRouterAgent",
        "main-router-agent",
        "NavigationDataAgent",
        "navigation-data-agent",
        "other-internal-router-session",
    } <= runtime.projection_private_identities()
    runtime.set_web_transport(store, publish)
    event = {
        "type": "REPLY_START",
        "reply_id": "reply-1",
        "name": "DataPilot",
        "role": "assistant",
    }
    dedupe_key = hashlib.sha256(b"event-1").hexdigest()

    await runtime.project_agent_event(
        internal_session_id,
        dedupe_key=dedupe_key,
        event=event,
    )
    await runtime.start_public_tool(
        internal_session_id,
        tool_call_id="call-1",
        tool_name="extract",
    )
    await runtime.finish_public_tool(
        internal_session_id,
        tool_call_id="call-1",
        status="failure",
        summary="extract failed",
        error_type="extract_sync_failed",
    )
    await runtime.finish_public_tool(
        internal_session_id,
        tool_call_id="call-1",
        status="success",
        summary="late placeholder",
        error_type=None,
    )

    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [(run.tool_call_id, run.status, run.error_type) for run in detail.tool_runs] == [
        ("call-1", "failure", "extract_sync_failed")
    ]
    terminal_events = [
        record.event
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["type"] == "CUSTOM"
    assert terminal_events[0]["value"] == {
        "tool_call_id": "call-1",
        "status": "failure",
        "summary": "extract failed",
        "error_type": "extract_sync_failed",
    }
    assert [record.sequence for _, record in published] == [1, 2]


@pytest.mark.asyncio
async def test_tool_terminal_sanitizes_every_private_identity_before_ledger_write(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("private terminal")
    internal_session_id = "internal-navigation-session"
    other_session_id = "other-internal-router-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="historical-private-agent",
        agentscope_session_id=other_session_id,
    )
    runtime = _runtime(workspace_root=tmp_path)
    runtime.set_web_session_store(store)
    await runtime.start_public_tool(
        internal_session_id,
        tool_call_id="public-call-1",
        tool_name="extract",
    )

    await runtime.finish_public_tool(
        internal_session_id,
        tool_call_id="public-call-1",
        status="failure",
        summary=(
            f"nested error: {internal_session_id}; {other_session_id}; "
            "navigation-data-agent; historical-private-agent; alice"
        ),
        error_type=other_session_id,
    )

    detail = store.get_session(public_session.id)
    assert detail is not None
    serialized = json.dumps(detail.model_dump(mode="json"), ensure_ascii=False)
    for private in (
        internal_session_id,
        other_session_id,
        "navigation-data-agent",
        "historical-private-agent",
        "alice",
    ):
        assert private not in serialized
    assert "public-call-1" in serialized
    assert detail.tool_runs[0].error_type == "private_runtime_identity"
    assert detail.events[-1].event["value"]["error_type"] == (
        "private_runtime_identity"
    )
    bus = SessionEventBus()
    async with stream_session_events(store, bus, public_session.id, 0) as stream:
        replayed = [await anext(stream) for _ in detail.events]
    replay_json = json.dumps(
        [record.model_dump(mode="json") for record in replayed],
        ensure_ascii=False,
    )
    for private in (
        internal_session_id,
        other_session_id,
        "navigation-data-agent",
        "historical-private-agent",
        "alice",
    ):
        assert private not in replay_json


@pytest.mark.asyncio
async def test_tool_terminal_identity_lookup_failure_persists_only_safe_generic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("lookup failure")
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id="private-session",
    )
    runtime = _runtime(workspace_root=tmp_path)
    runtime.set_web_session_store(store)
    await runtime.start_public_tool(
        "private-session",
        tool_call_id="public-call-2",
        tool_name="extract",
    )
    monkeypatch.setattr(
        runtime,
        "projection_private_identities",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("identity db unavailable private-session")
        ),
    )

    await runtime.finish_public_tool(
        "private-session",
        tool_call_id="public-call-2",
        status="failure",
        summary="secret private-session",
        error_type="private-session",
    )

    detail = store.get_session(public_session.id)
    assert detail is not None
    assert detail.tool_runs[0].summary == "Tool execution details unavailable."
    assert detail.tool_runs[0].error_type == "public_sanitization_failed"
    serialized = json.dumps(detail.model_dump(mode="json"), ensure_ascii=False)
    assert "secret private-session" not in serialized


@pytest.mark.asyncio
async def test_sync_live_publisher_failure_keeps_persisted_reply_and_yields_event(
    tmp_path: Path,
    caplog,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("reply projection")
    internal_session_id = "internal-router-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )

    def publish(_session_id, _record) -> None:
        raise ConnectionError("browser disconnected")

    runtime = _runtime(workspace_root=tmp_path)
    runtime.set_web_transport(store, publish)
    middleware = DataPilotReplyProjectionMiddleware(internal_session_id, runtime)
    event = ReplyStartEvent(
        session_id=internal_session_id,
        reply_id="reply-1",
        name="MainRouterAgent",
    )

    async def handler(**_kwargs):
        yield event

    yielded = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {},
            handler,
        )
    ]

    detail = store.get_session(public_session.id)
    assert yielded == [event]
    assert detail is not None
    assert [record.event["type"] for record in detail.events] == ["REPLY_START"]
    assert "Live public event publish failed" in caplog.text


@pytest.mark.asyncio
async def test_async_live_publisher_failure_keeps_tool_terminal_and_yields_response(
    tmp_path: Path,
    caplog,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("tool outcome")
    internal_session_id = "internal-navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )

    async def publish(_session_id, _record) -> None:
        await asyncio.sleep(0)
        raise ConnectionError("slow browser disconnected")

    runtime = _runtime(workspace_root=tmp_path)
    runtime.set_web_transport(store, publish)
    finish_calls = 0
    finish_public_tool = runtime.finish_public_tool

    async def finish_spy(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        return await finish_public_tool(*args, **kwargs)

    runtime.finish_public_tool = finish_spy
    middleware = DataPilotToolOutcomeMiddleware(internal_session_id, runtime)
    tool_call = ToolCallBlock(
        id="call-1",
        name="extract",
        input="{}",
    )
    response = ToolResponse(id="call-1", state=ToolResultState.SUCCESS)

    async def handler(**_kwargs):
        yield response

    yielded = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": tool_call},
            handler,
        )
    ]

    detail = store.get_session(public_session.id)
    assert yielded == [response]
    assert detail is not None
    assert [(run.tool_call_id, run.status) for run in detail.tool_runs] == [
        ("call-1", "success")
    ]
    assert [
        record.event["name"]
        for record in detail.events
        if record.event["type"] == "CUSTOM"
    ] == ["datapilot_tool_terminal"]
    assert finish_calls == 1
    assert "Live public event publish failed" in caplog.text


@pytest.mark.asyncio
async def test_unrelated_tool_cancellation_is_projected_as_failure(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("system cancellation")
    internal_session_id = "internal-navigation-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="navigation-data-agent",
        agentscope_session_id=internal_session_id,
    )
    runtime = _runtime(workspace_root=tmp_path)
    runtime.set_web_session_store(store)
    middleware = DataPilotToolOutcomeMiddleware(internal_session_id, runtime)
    tool_call = ToolCallBlock(id="call-system-cancel", name="extract", input="{}")

    async def handler(**_kwargs):
        raise asyncio.CancelledError("worker shutdown")
        yield  # pragma: no cover

    with pytest.raises(asyncio.CancelledError):
        async for _ in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": tool_call},
            handler,
        ):
            pass

    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [(row.tool_call_id, row.status) for row in detail.tool_runs] == [
        ("call-system-cancel", "failure")
    ]
    assert [record.event["value"]["status"] for record in detail.events] == ["failure"]


@pytest.mark.asyncio
async def test_explicit_stop_is_idempotent_ignores_late_tool_outcome_and_allows_continuation(
    tmp_path: Path,
) -> None:
    message_bus = FakeAgentScopeMessageBus()
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry, message_bus=message_bus)
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("repeat stop")
    internal_session_id = "router-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    store.start_tool_run(
        public_session.id,
        "call-1",
        "extract",
        "2026-07-15T00:00:00.000+00:00",
    )
    runtime.set_web_session_store(store)

    first = await runtime.interrupt_web_session(web_session_id=public_session.id)
    second = await runtime.interrupt_web_session(web_session_id=public_session.id)
    late = await runtime.finish_public_tool(
        internal_session_id,
        tool_call_id="call-1",
        status="success",
        summary="late success",
        error_type=None,
    )

    assert first.stopped_tool_call_ids == ["call-1"]
    assert second.stopped_tool_call_ids == []
    assert late is None
    detail = store.get_session(public_session.id)
    assert detail is not None
    assert [(row.tool_call_id, row.status) for row in detail.tool_runs] == [
        ("call-1", "stopped")
    ]
    assert [
        record.event["value"]["status"]
        for record in detail.events
        if record.event.get("name") == "datapilot_tool_terminal"
    ] == ["stopped"]

    await runtime.submit_user_message(web_session_id=public_session.id, message="继续处理")

    assert chat_run_registry.spawns[-1]["session_id"] == internal_session_id
    assert runtime.run_cancellation(internal_session_id) is not None
    await chat_run_registry.drain()


@pytest.mark.asyncio
async def test_stopped_generation_suppresses_restart_wakeup_until_new_user_turn(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("restart stop boundary")
    internal_session_id = "router-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    store.begin_execution_generation(public_session.id)
    store.stop_open_tool_runs_with_terminal_events(
        public_session.id,
        lambda _row: pytest.fail("no running tool should require an event"),
    )

    restarted = _runtime(chat_run_registry=FakeChatRunRegistry())
    restarted.set_web_session_store(WebSessionStore(store.db_path))
    middleware = DataPilotRunBoundaryMiddleware(internal_session_id, restarted)
    model_calls = 0

    async def handler(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        yield ReplyStartEvent(
            session_id=internal_session_id,
            reply_id="unexpected-reply",
            name="MainRouterAgent",
        )

    suppressed = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": None},
            handler,
        )
    ]

    assert model_calls == 0
    assert len(suppressed) == 1
    assert isinstance(suppressed[0], Msg)

    admitted = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": UserMsg(name="user", content="continue after restart")},
            handler,
        )
    ]
    assert model_calls == 1
    assert isinstance(admitted[0], ReplyStartEvent)
    assert store.execution_generation_is_stopped(public_session.id) is False

    resumed_wakeup = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": None},
            handler,
        )
    ]
    assert model_calls == 2
    assert isinstance(resumed_wakeup[0], ReplyStartEvent)
    assert restarted.run_cancellation(internal_session_id) is None


@pytest.mark.asyncio
async def test_pending_stop_generation_suppresses_wakeup_before_ack(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    public_session = store.create_session("pending stop wake fence")
    internal_session_id = "pending-stop-session"
    store.save_agentscope_session_mapping(
        public_session.id,
        agent_id="main-router-agent",
        agentscope_session_id=internal_session_id,
    )
    store.begin_execution_generation(public_session.id)
    request = store.begin_or_resume_stop_request(public_session.id)
    runtime = _runtime(chat_run_registry=FakeChatRunRegistry())
    runtime.set_web_session_store(store)
    middleware = DataPilotRunBoundaryMiddleware(internal_session_id, runtime)
    model_calls = 0

    async def handler(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        yield ReplyStartEvent(
            session_id=internal_session_id,
            reply_id="unexpected",
            name="MainRouterAgent",
        )

    yielded = [
        item
        async for item in middleware.on_reply(
            SimpleNamespace(name="MainRouterAgent"),
            {"inputs": None},
            handler,
        )
    ]

    assert request.status == "pending"
    assert model_calls == 0
    assert len(yielded) == 1
    assert isinstance(yielded[0], Msg)


@pytest.mark.asyncio
async def test_submit_turn_appends_user_message_calls_runtime_and_returns_turn_id(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = FakeAgentScopeRuntime(turn_id="turn_agentscope_1")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    turn_id = await manager.submit_turn(
        session.id,
        "开始处理",
        message_id="local-message-1",
    )

    assert isinstance(turn_id, TurnSubmissionResult)
    assert turn_id.turn_id.startswith("turn_")
    assert turn_id.replayed is False
    assert runtime.submissions == [{"web_session_id": session.id, "message": "开始处理"}]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.messages[0].id == "local-message-1"
    assert [(message.role, message.content) for message in detail.messages] == [("user", "开始处理")]


@pytest.mark.asyncio
async def test_submit_turn_duplicate_message_id_is_409_style_without_second_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "duplicate-message.sqlite")
    runtime = FakeAgentScopeRuntime(turn_id="turn_agentscope_1")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("duplicate message")

    first_turn_id = await manager.submit_turn(
        session.id,
        "same text",
        message_id="local-duplicate-1",
    )
    replayed_turn_id = await manager.submit_turn(
        session.id,
        "same text",
        message_id="local-duplicate-1",
    )

    assert isinstance(first_turn_id, TurnSubmissionResult)
    assert isinstance(replayed_turn_id, TurnSubmissionResult)
    assert replayed_turn_id.turn_id == first_turn_id.turn_id
    assert replayed_turn_id.replayed is True
    assert len(runtime.submissions) == 1
    store.finish_user_message_turn_with_event(
        session.id,
        "local-duplicate-1",
        turn_id=first_turn_id.turn_id,
        terminal_status="success",
    )
    terminal_replay = await manager.submit_turn(
        session.id,
        "same text",
        message_id="local-duplicate-1",
    )
    assert isinstance(terminal_replay, TurnSubmissionResult)
    assert terminal_replay.replayed is True
    assert terminal_replay.terminal is True
    assert len(runtime.submissions) == 1
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.id for message in detail.messages] == ["local-duplicate-1"]


@pytest.mark.asyncio
async def test_submit_turn_live_duplicate_is_explicitly_pending_without_second_run(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "pending-message.sqlite")
    runtime = FakeAgentScopeRuntime()
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("pending message")
    store.claim_user_message(
        session.id,
        "local-pending-1",
        "same request",
        runtime_id="other-runtime",
        turn_id="turn_pending",
        ttl_seconds=30.0,
    )

    with pytest.raises(TurnSubmissionPending):
        await manager.submit_turn(
            session.id,
            "same request",
            message_id="local-pending-1",
        )

    assert runtime.submissions == []


@pytest.mark.asyncio
async def test_submit_turn_same_message_id_with_different_content_is_conflict(
    tmp_path: Path,
) -> None:
    store = WebSessionStore(tmp_path / "conflicting-message.sqlite")
    runtime = FakeAgentScopeRuntime()
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("conflicting message")
    await manager.submit_turn(
        session.id,
        "original",
        message_id="local-content-conflict",
    )

    with pytest.raises(RuntimeError, match="different content"):
        await manager.submit_turn(
            session.id,
            "changed",
            message_id="local-content-conflict",
        )

    assert len(runtime.submissions) == 1


@pytest.mark.asyncio
async def test_saved_session_remains_resumable_after_manager_restart(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    first_runtime = FakeAgentScopeRuntime()
    first_manager = AgentScopeWebSessionManager(store=store, runtime=first_runtime)
    session = await first_manager.create_session("first")
    await first_manager.submit_turn(session.id, "first")

    restarted_runtime = FakeAgentScopeRuntime(turn_id="turn-after-restart")
    recreated_manager = AgentScopeWebSessionManager(
        store=WebSessionStore(store.db_path),
        runtime=restarted_runtime,
    )

    turn_id = await recreated_manager.submit_turn(session.id, "continue")

    assert turn_id == "turn-after-restart"
    assert restarted_runtime.submissions[-1]["web_session_id"] == session.id
    assert [message.content for message in store.get_session(session.id).messages] == [
        "first",
        "continue",
    ]

@pytest.mark.asyncio
async def test_submit_turn_rejection_does_not_append_user_message(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = RejectingAgentScopeRuntime()
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    with pytest.raises(RuntimeError, match="turn rejected"):
        await manager.submit_turn(
            session.id,
            "开始处理",
            message_id="local-retry-after-rejection",
        )

    assert runtime.submissions == [{"web_session_id": session.id, "message": "开始处理"}]
    detail = store.get_session(session.id)
    assert detail is not None
    assert detail.messages == []

    accepted = FakeAgentScopeRuntime(turn_id="turn-retry")
    manager._runtime = accepted
    retry_turn_id = await manager.submit_turn(
        session.id,
        "开始处理",
        message_id="local-retry-after-rejection",
    )
    assert isinstance(retry_turn_id, TurnSubmissionResult)
    assert retry_turn_id.turn_id.startswith("turn_")
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.id for message in detail.messages] == [
        "local-retry-after-rejection"
    ]


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

    assert await manager.interrupt(session.id) == InterruptResponse(
        interrupted=False,
        stopped_tool_call_ids=[],
    )


@pytest.mark.asyncio
async def test_interrupt_delegates_to_runtime_interrupt(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = InterruptingAgentScopeRuntime(
        interrupted=True,
        stopped_tool_call_ids=["call-public-1"],
    )
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    assert await manager.interrupt(session.id) == InterruptResponse(
        interrupted=True,
        stopped_tool_call_ids=["call-public-1"],
    )
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


@pytest.mark.parametrize("request_id", ["", "   "])
def test_human_decision_request_requires_nonempty_public_request_id(
    request_id: str,
) -> None:
    with pytest.raises(ValueError, match="request_id must not be empty"):
        HumanDecisionRequest(
            action="confirm",
            request_id=request_id,
            tool_call_id="tool-1",
            reply_id="reply-1",
        )


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
