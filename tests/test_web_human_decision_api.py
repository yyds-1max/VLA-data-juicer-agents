from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.web.app import create_app


class FakeAgentScopeRuntime:
    def __init__(self, *, accept_decision: bool = True) -> None:
        self.app = FastAPI()
        self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")
        self.accept_decision = accept_decision
        self.messages: list[tuple[str, str]] = []
        self.decisions: list[tuple[str, dict]] = []

    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        self.messages.append((web_session_id, message))
        return "turn-agent-1"

    async def submit_human_decision(self, *, web_session_id: str, decision: dict) -> bool:
        self.decisions.append((web_session_id, decision))
        return self.accept_decision


def _client(tmp_path, runtime: FakeAgentScopeRuntime) -> TestClient:
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=runtime,
    )
    return TestClient(app)


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"message": "处理导航数据"})
    assert response.status_code == 200
    return response.json()["session"]["id"]


def test_human_decision_confirm_is_accepted_and_forwarded(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)
    payload = {
        "action": "confirm",
        "request_id": "request-1",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
    }

    response = client.post(f"/api/sessions/{session_id}/human-decisions", json=payload)

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert runtime.decisions == [(session_id, payload)]


def test_human_decision_guide_requires_text(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "guide",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
            "reply_id": "reply-1",
            "text": "   ",
        },
    )

    assert response.status_code == 422
    assert runtime.decisions == []


def test_human_decision_runtime_rejection_returns_409(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime(accept_decision=False)
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "stop",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
            "reply_id": "reply-1",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Human decision was not accepted"


def test_human_decision_unknown_session_returns_404(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)

    response = client.post(
        "/api/sessions/missing/human-decisions",
        json={
            "action": "confirm",
            "request_id": "request-1",
            "tool_call_id": "tool-call-1",
            "reply_id": "reply-1",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"
