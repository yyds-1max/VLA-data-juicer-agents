from pathlib import Path

import pytest

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
