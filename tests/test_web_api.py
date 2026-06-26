from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vla_data_juicer_agents.web.app import _drain_controller_events, create_app


class FakeController:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.submitted = []
        self.interrupts = 0
        self.is_running = False
        self._events = []
        self._result = None
        FakeController.created.append(self)

    def start(self):
        self.started = True

    def submit_turn(self, message):
        self.submitted.append(message)
        self._events.append(
            {
                "type": "final",
                "source": "main",
                "payload": {"text": f"完成: {message}", "stop": False},
            }
        )
        self._result = SimpleNamespace(text=f"完成: {message}", stop=False, interrupted=False)

    def request_interrupt(self):
        self.interrupts += 1
        return True

    def drain_events(self):
        events = self._events
        self._events = []
        return events

    def consume_turn_result(self):
        if self._result is None:
            raise RuntimeError("No completed turn result is available.")
        result = self._result
        self._result = None
        return result


def make_client(tmp_path: Path) -> TestClient:
    FakeController.created = []
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        model="qwen-test",
        controller_factory=FakeController,
    )
    return TestClient(app)


def test_create_session_returns_title(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["id"].startswith("session_")
    assert body["session"]["title"] == "处理 20270605 的室外导航数据"
    assert FakeController.created[0].started is True


def test_submit_turn_returns_turn_id(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})

    assert response.status_code == 200
    assert response.json()["turn_id"].startswith("turn_")


def test_create_app_accepts_positional_configuration(tmp_path: Path):
    FakeController.created = []
    app = create_app(str(tmp_path / ".djx"), "qwen-positional", tmp_path / "sessions.sqlite", FakeController)
    client = TestClient(app)

    session_id = _create_session(client)

    assert FakeController.created[0].kwargs["working_dir"] == str(tmp_path / ".djx" / session_id)
    assert FakeController.created[0].kwargs["model"] == "qwen-positional"


def test_submit_turn_runtime_error_returns_409(tmp_path: Path):
    class ActiveTurnController(FakeController):
        def submit_turn(self, message):
            raise RuntimeError("A session turn is already active.")

    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=ActiveTurnController,
    )
    client = TestClient(app)
    session_id = _create_session(client)

    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})

    assert response.status_code == 409
    assert response.json()["detail"] == "A session turn is already active."


def test_session_events_websocket_receives_background_turn_events(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    with client.websocket_connect(f"/api/sessions/{session_id}/events") as websocket:
        response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})
        assert response.status_code == 200
        with ThreadPoolExecutor(max_workers=1) as executor:
            event = executor.submit(websocket.receive_json).result(timeout=1)

    assert event["type"] == "final"
    assert event["payload"]["text"] == "完成: 开始处理"


def test_list_sessions_returns_session_records(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})

    response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert len(sessions) == 1
    assert set(sessions[0]) == {"id", "title", "status", "created_at", "updated_at"}


def test_get_session_returns_persisted_messages_after_turn_submission(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    turn_response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})
    detail_response = client.get(f"/api/sessions/{session_id}")

    assert turn_response.status_code == 200
    assert detail_response.status_code == 200
    messages = detail_response.json()["session"]["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "开始处理"

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        messages = client.get(f"/api/sessions/{session_id}").json()["session"]["messages"]
        if any(message["role"] == "assistant" and message["content"] == "完成: 开始处理" for message in messages):
            break
        time.sleep(0.01)
    assert [message["content"] for message in messages].count("完成: 开始处理") == 1


def test_failed_event_drain_consumes_result_after_controller_stops(tmp_path: Path):
    class FailingDrainController(FakeController):
        created = []

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.is_running = True
            self.consume_called = False
            self.consumed = False
            self._result = SimpleNamespace(text="cleanup text")
            FailingDrainController.created.append(self)

        def drain_events(self):
            asyncio.get_running_loop().call_later(0.01, setattr, self, "is_running", False)
            raise RuntimeError("drain failed")

        def consume_turn_result(self):
            self.consume_called = True
            if self.is_running:
                raise RuntimeError("Turn is still running.")
            self.consumed = True
            result = self._result
            self._result = None
            return result

    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FailingDrainController,
    )
    session = app.state.manager.create_session("处理 20270605 的室外导航数据")
    controller = FailingDrainController.created[0]

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="drain failed"):
            await _drain_controller_events(session.id, app.state.manager, app.state.store, app.state.bus)

    asyncio.run(exercise())

    assert controller.consume_called is True
    assert controller.consumed is True
    assert controller.is_running is False
    assert [message.content for message in app.state.store.get_session(session.id).messages] == []


def test_interrupt_returns_true_for_active_session(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    response = client.post(f"/api/sessions/{session_id}/interrupt")

    assert response.status_code == 200
    assert response.json() == {"interrupted": True}


def test_turn_and_interrupt_unknown_active_session_return_404(tmp_path: Path):
    client = make_client(tmp_path)

    turn_response = client.post("/api/sessions/missing/turns", json={"message": "开始处理"})
    interrupt_response = client.post("/api/sessions/missing/interrupt")

    assert turn_response.status_code == 404
    assert interrupt_response.status_code == 404


def test_get_unknown_session_returns_404(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/api/sessions/missing")

    assert response.status_code == 404


def test_create_app_reads_working_dir_and_model_from_env(tmp_path: Path, monkeypatch):
    FakeController.created = []
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(tmp_path / "env-djx"))
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_MODEL", "qwen-env")
    app = create_app(db_path=tmp_path / "sessions.sqlite", controller_factory=FakeController)
    client = TestClient(app)

    session_id = _create_session(client)

    assert FakeController.created[0].kwargs["working_dir"] == str(tmp_path / "env-djx" / session_id)
    assert FakeController.created[0].kwargs["model"] == "qwen-env"


def test_create_app_treats_empty_model_env_as_none(tmp_path: Path, monkeypatch):
    FakeController.created = []
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(tmp_path / "env-djx"))
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_MODEL", "")
    app = create_app(db_path=tmp_path / "sessions.sqlite", controller_factory=FakeController)
    client = TestClient(app)

    client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})

    assert FakeController.created[0].kwargs["model"] is None


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})
    return response.json()["session"]["id"]
