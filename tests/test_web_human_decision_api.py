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
        self.recoveries: list[tuple[str, dict]] = []

    async def submit_user_message(
        self,
        *,
        web_session_id: str,
        message: str,
        message_id: str | None = None,
        turn_id: str | None = None,
        on_admitted=None,
    ) -> str:
        self.messages.append((web_session_id, message))
        if on_admitted is not None:
            on_admitted()
        return turn_id or "turn-agent-1"

    async def submit_human_decision(self, *, web_session_id: str, decision: dict) -> bool:
        self.decisions.append((web_session_id, decision))
        return self.accept_decision

    async def recover_human_decision_handoff(self, *, web_session_id: str, recovery: dict):
        self.recoveries.append((web_session_id, recovery))
        if recovery["reason"] == "illegal":
            raise RuntimeError("handoff is not recovery_required")
        return {
            "recovered": True,
            "plan_id": recovery["plan_id"],
            "step_id": recovery["step_id"],
            "handoff_status": "quarantined",
            "task_status": "needs_replan",
            "next_action": "submit_complete_plan",
        }

def _client(tmp_path, runtime: FakeAgentScopeRuntime) -> TestClient:
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=runtime,
    )
    return TestClient(app)


def _create_session(client: TestClient) -> str:
    response = client.post(
        "/api/sessions",
        json={
            "message": "处理导航数据",
            "creation_id": "local-create-human-decision",
        },
    )
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


def test_plan_bound_human_decision_ids_are_forwarded_to_runtime(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)
    payload = {
        "action": "confirm",
        "request_id": "nav-plan-1:confirm",
        "plan_id": "nav-plan-1",
        "step_id": "confirm",
        "tool_call_id": "tool-call-1",
        "reply_id": "reply-1",
    }

    response = client.post(f"/api/sessions/{session_id}/human-decisions", json=payload)

    assert response.status_code == 200
    assert runtime.decisions == [(session_id, payload)]


def test_human_decision_guide_preserves_structured_text_payload(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "guide",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
            "text": "请改用另一组外参",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert runtime.decisions[0][0] == session_id
    assert runtime.decisions[0][1] == {
        "action": "guide",
        "request_id": "camera-1",
        "tool_call_id": "tool-1",
        "reply_id": "reply-1",
        "text": "请改用另一组外参",
    }


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


def test_human_decision_recovery_contract_and_forwarding(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)
    payload = {
        "action": "quarantine_and_replan",
        "plan_id": "plan-1",
        "step_id": "confirm",
        "reason": "operator confirmed abandoned worker",
    }

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions/recovery", json=payload
    )

    assert response.status_code == 200
    assert response.json() == {
        "recovered": True,
        "plan_id": "plan-1",
        "step_id": "confirm",
        "handoff_status": "quarantined",
        "task_status": "needs_replan",
        "next_action": "submit_complete_plan",
    }
    assert runtime.recoveries == [(session_id, payload)]


def test_human_decision_recovery_validation_ownership_and_state_errors(tmp_path) -> None:
    runtime = FakeAgentScopeRuntime()
    client = _client(tmp_path, runtime)
    session_id = _create_session(client)
    path = f"/api/sessions/{session_id}/human-decisions/recovery"

    assert client.post(path, json={}).status_code == 422
    assert client.post(
        "/api/sessions/missing/human-decisions/recovery",
        json={
            "action": "quarantine_and_replan",
            "plan_id": "plan-1",
            "step_id": "confirm",
            "reason": "operator recovery",
        },
    ).status_code == 404
    conflict = client.post(
        path,
        json={
            "action": "quarantine_and_replan",
            "plan_id": "plan-1",
            "step_id": "confirm",
            "reason": "illegal",
        },
    )
    assert conflict.status_code == 409
    assert "recovery_required" in conflict.json()["detail"]
