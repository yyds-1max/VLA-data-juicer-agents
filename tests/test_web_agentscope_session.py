import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import Msg, ToolCallBlock, ToolCallState, ToolResultState

from vla_data_juicer_agents.navigation.routing import is_high_confidence_navigation_request
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
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


class FakeAgentScopeMessageBus:
    _SESSION_EVENTS_KEY = "agentscope:session:events:{sid}"

    def __init__(
        self,
        *,
        replay_events: list[tuple[str, dict]] | None = None,
        live_events: list[dict] | None = None,
        running_states: list[bool] | None = None,
    ) -> None:
        self.replay_events = replay_events or []
        self.live_events = live_events or []
        self.running_states = running_states or []
        self.read_sessions: list[str] = []
        self.read_since: list[str | None] = []
        self.subscribe_keys: list[str] = []

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


class FakeChatService:
    def __init__(self) -> None:
        self.runs = []

    async def run(self, *, user_id, session_id, agent_id, input_msg):
        self.runs.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "message": input_msg,
            }
        )


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
) -> AgentScopeRuntime:
    storage = storage or FakeAgentScopeStorage()
    chat_service = FakeChatService()
    state = SimpleNamespace(chat_service=chat_service)
    if chat_run_registry is not None:
        state.chat_run_registry = chat_run_registry
    return AgentScopeRuntime(
        config=_agentscope_config(),
        storage=storage,
        message_bus=message_bus or object(),
        workspace_manager=object(),
        app=SimpleNamespace(state=state),
    )


def _message_text(message) -> str:
    return message.content[0].text


def _agentscope_session_record(
    *,
    reply_id: str = "reply-1",
    tool_call_id: str = "tool-call-1",
    tool_name: str = "request_human_decision",
    tool_state=ToolCallState.SUBMITTED,
):
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
                            input="{}",
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


@pytest.mark.asyncio
async def test_runtime_submit_user_message_routes_navigation_request_to_navigation_agent() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    turn_id = await runtime.submit_user_message(
        web_session_id="web-1",
        message="同步 rosbag db3 odom 和 gridmap 数据",
    )

    assert turn_id.startswith("turn_")
    assert runtime.web_sessions == {"web-1": ("navigation-data-agent", "web-1__navigation-data-agent")}
    session = runtime.storage.sessions[0]
    assert session["user_id"] == "alice"
    assert session["agent_id"] == "navigation-data-agent"
    assert session["id"] == "web-1__navigation-data-agent"
    assert session["config"].workspace_id == "workspace-web-1"
    assert session["config"].name == "web-1"
    assert session["config"].chat_model_config.type == "dashscope_chat"
    assert session["config"].chat_model_config.credential_id == "dashscope-env"
    assert session["config"].chat_model_config.model == "qwen-navigation"
    assert session["config"].chat_model_config.parameters == {"parallel_tool_calls": False}
    assert runtime.app.state.chat_service.runs == []
    assert [spawn["session_id"] for spawn in chat_run_registry.spawns] == [
        "web-1__navigation-data-agent"
    ]
    await chat_run_registry.drain()
    assert len(runtime.app.state.chat_service.runs) == 1
    run = runtime.app.state.chat_service.runs[0]
    assert run["user_id"] == "alice"
    assert run["session_id"] == "web-1__navigation-data-agent"
    assert run["agent_id"] == "navigation-data-agent"
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
async def test_runtime_submit_user_message_recreates_session_when_agent_changes() -> None:
    chat_run_registry = FakeChatRunRegistry()
    runtime = _runtime(chat_run_registry=chat_run_registry)

    await runtime.submit_user_message(web_session_id="web-1", message="你好")
    await chat_run_registry.drain()
    await runtime.submit_user_message(web_session_id="web-1", message="处理导航数据")
    await chat_run_registry.drain()

    assert len(runtime.storage.sessions) == 2
    assert runtime.web_sessions == {"web-1": ("navigation-data-agent", "web-1__navigation-data-agent")}
    assert [session["agent_id"] for session in runtime.storage.sessions] == [
        "main-router-agent",
        "navigation-data-agent",
    ]
    assert [session["id"] for session in runtime.storage.sessions] == [
        "web-1__main-router-agent",
        "web-1__navigation-data-agent",
    ]


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
async def test_runtime_submit_user_message_requires_chat_run_registry() -> None:
    runtime = _runtime(chat_run_registry=None)

    with pytest.raises(RuntimeError, match="chat_run_registry"):
        await runtime.submit_user_message(web_session_id="web-1", message="你好")

    assert runtime.app.state.chat_service.runs == []


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
        _agentscope_session_record(tool_state=tool_state)
    )
    runtime = _runtime(storage=storage, chat_run_registry=chat_run_registry)
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision={
            "action": "guide",
            "text": "先 dry-run",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
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
    assert result.id == "tool-call-1"
    assert result.name == "request_human_decision"
    assert result.state == ToolResultState.SUCCESS
    assert json.loads(result.output) == {
        "action": "guide",
        "text": "先 dry-run",
        "request_id": "request-1",
    }


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

    assert retry_after_release is True
    assert len(chat_run_registry.spawns) == 1
    await chat_run_registry.drain()


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
        ("assistant_delta", {"delta": "处理"}),
        ("final", {"text": "处理"}),
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
        ("assistant_delta", {"delta": "旧"}),
        ("final", {"text": "旧"}),
    ]
    assert [(event["type"], event["payload"]) for event in second_events] == [
        ("assistant_delta", {"delta": "新"}),
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
    assert detail.model_dump(exclude={"messages"}) == session.model_dump()
    assert detail.messages == []


@pytest.mark.asyncio
async def test_submit_turn_appends_user_message_calls_runtime_and_returns_turn_id(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = FakeAgentScopeRuntime(turn_id="turn_agentscope_1")
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("处理 20270605")

    turn_id = await manager.submit_turn(session.id, "开始处理")

    assert turn_id == "turn_agentscope_1"
    assert runtime.submissions == [{"web_session_id": session.id, "message": "开始处理"}]
    detail = store.get_session(session.id)
    assert detail is not None
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
