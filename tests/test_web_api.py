from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime
from vla_data_juicer_agents.web.app import (
    _consume_turn_result_when_idle,
    _create_logged_task,
    _drain_controller_events,
    create_app,
)
from vla_data_juicer_agents.web.schemas import PublicEventRecord


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


class RecordingSessionService:
    def __init__(self, *, fail_on_session: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_on_session = fail_on_session

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        self.calls.append((user_id, agent_id, session_id))
        if session_id == self.fail_on_session:
            raise RuntimeError(f"delete failed for {agent_id} / {session_id}")
        return True


class FailOnceSessionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.deleted_sessions: set[str] = set()
        self.failed_once = False

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        self.calls.append((user_id, agent_id, session_id))
        if session_id in self.deleted_sessions:
            return False
        if session_id == "worker-session" and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("AgentScope deletion failed once")
        self.deleted_sessions.add(session_id)
        return True


class IdempotentSessionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.deleted_sessions: set[str] = set()

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        self.calls.append((user_id, agent_id, session_id))
        if session_id in self.deleted_sessions:
            return False
        self.deleted_sessions.add(session_id)
        return True


def make_deletion_client(
    tmp_path: Path,
    session_service: (
        RecordingSessionService | FailOnceSessionService | IdempotentSessionService
    ),
) -> tuple[TestClient, AgentScopeRuntime]:
    agentscope_app = FastAPI()
    agentscope_app.state.session_service = session_service
    runtime = AgentScopeRuntime(
        config=AgentScopeRuntimeConfig(
            user_id="delete-user",
            redis_url="redis://localhost:6379/0",
            workspace_root=tmp_path / "workspace",
            dashscope_api_key="test-key",
            dashscope_base_url=None,
            default_model="qwen-test",
            router_model="qwen-test",
            navigation_model="qwen-test",
        ),
        storage=object(),
        message_bus=object(),
        workspace_manager=object(),
        app=agentscope_app,
    )
    app = create_app(
        working_dir=str(tmp_path / "workspace"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=runtime,
    )
    return TestClient(app), runtime


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


def test_create_app_mounts_agentscope_when_runtime_factory_provided(tmp_path: Path):
    fake_runtime = SimpleNamespace(
        app=FastAPI(),
        config=SimpleNamespace(agentscope_mount_path="/api/agentscope"),
    )

    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        agentscope_runtime=fake_runtime,
    )

    assert "/api/agentscope" in [route.path for route in app.routes]
    assert app.state.agentscope_runtime is fake_runtime


def test_create_app_enters_agentscope_sub_app_lifespan(tmp_path: Path):
    events = []

    @asynccontextmanager
    async def lifespan(sub_app: FastAPI):
        events.append("startup")
        sub_app.state.ready = True
        yield
        events.append("shutdown")

    sub_app = FastAPI(lifespan=lifespan)
    runtime = SimpleNamespace(
        app=sub_app,
        config=SimpleNamespace(agentscope_mount_path="/api/agentscope"),
    )
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        agentscope_runtime=runtime,
    )

    with TestClient(app):
        assert sub_app.state.ready is True
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]


def test_create_app_uses_agentscope_session_manager_when_runtime_present(tmp_path: Path):
    class FakeRuntime:
        def __init__(self) -> None:
            self.app = FastAPI()
            self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")
            self.submitted = []

        async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
            self.submitted.append((web_session_id, message))
            return "turn-agent-1"

    runtime = FakeRuntime()
    FakeController.created = []
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        agentscope_runtime=runtime,
    )
    client = TestClient(app)

    session_id = _create_session(client)
    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})

    assert response.status_code == 200
    assert response.json()["turn_id"] == "turn-agent-1"
    assert FakeController.created == []
    assert runtime.submitted == [(session_id, "开始处理")]


def test_create_app_keeps_legacy_controller_when_agentscope_runtime_missing(tmp_path: Path):
    FakeController.created = []
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    client = TestClient(app)

    session_id = _create_session(client)

    assert session_id.startswith("session_")
    assert FakeController.created[0].started is True


def test_frontend_index_served_when_dist_provided(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><main>DataPilot</main>", encoding="utf-8")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        frontend_dist=dist,
    )
    client = TestClient(app)

    response = client.get("/")
    api_response = client.get("/api/sessions")

    assert response.status_code == 200
    assert response.text == "<!doctype html><main>DataPilot</main>"
    assert response.headers["content-type"].startswith("text/html")
    assert api_response.status_code == 200


def test_frontend_index_served_from_env_dist(monkeypatch, tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><main>DataPilot env</main>", encoding="utf-8")
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_FRONTEND_DIST", str(dist))
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "<!doctype html><main>DataPilot env</main>"


def test_frontend_assets_served_when_assets_dir_exists(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('datapilot');", encoding="utf-8")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        frontend_dist=dist,
    )
    client = TestClient(app)

    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log('datapilot');"
    assert response.headers["content-type"].split(";")[0] in {
        "application/javascript",
        "text/javascript",
    }


def test_frontend_brand_assets_served_when_brand_dir_exists(tmp_path: Path):
    dist = tmp_path / "dist"
    brand = dist / "brand"
    brand.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (brand / "logo.png").write_bytes(b"fake-png")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        frontend_dist=dist,
    )
    client = TestClient(app)

    response = client.get("/brand/logo.png")

    assert response.status_code == 200
    assert response.content == b"fake-png"
    assert response.headers["content-type"].split(";")[0] == "image/png"


def test_frontend_dist_without_index_leaves_root_404_and_api_available(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        frontend_dist=dist,
    )
    client = TestClient(app)

    root_response = client.get("/")
    api_response = client.get("/api/sessions")

    assert root_response.status_code == 404
    assert api_response.status_code == 200


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


def test_session_stream_returns_404_for_unknown_session(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/api/sessions/missing/stream")

    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found"}


@pytest.mark.asyncio
async def test_session_stream_is_sse_and_honors_after_sequence(tmp_path: Path) -> None:
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    session = app.state.manager.create_session("stream cursor")
    first = app.state.store.append_public_event(
        session.id,
        "1" * 64,
        {"type": "first"},
    )
    second = app.state.store.append_public_event(
        session.id,
        "2" * 64,
        {"type": "second"},
    )
    endpoint = _route_endpoint(app, f"/api/sessions/{{session_id}}/stream")

    response = await endpoint(session.id, after_sequence=first.sequence)
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert response.media_type == "text/event-stream"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert json.loads(frame.removeprefix(b"data: ").removesuffix(b"\n\n"))["id"] == second.id


@pytest.mark.asyncio
async def test_session_stream_emits_heartbeat_comment(tmp_path: Path) -> None:
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        sse_heartbeat_seconds=0.01,
    )
    session = app.state.manager.create_session("heartbeat")
    endpoint = _route_endpoint(app, f"/api/sessions/{{session_id}}/stream")

    response = await endpoint(session.id, after_sequence=0)
    frame = await asyncio.wait_for(anext(response.body_iterator), timeout=1)
    await response.body_iterator.aclose()

    assert frame == b": heartbeat\n\n"


@pytest.mark.asyncio
async def test_session_stream_does_not_receive_other_session_events(tmp_path: Path) -> None:
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        sse_heartbeat_seconds=0.01,
    )
    selected = app.state.manager.create_session("selected")
    other = app.state.manager.create_session("other")
    other_record = app.state.store.append_public_event(
        other.id,
        "3" * 64,
        {"type": "wrong-session"},
    )
    endpoint = _route_endpoint(app, f"/api/sessions/{{session_id}}/stream")

    response = await endpoint(selected.id, after_sequence=0)
    await app.state.bus.publish(other.id, other_record)
    frame = await asyncio.wait_for(anext(response.body_iterator), timeout=1)
    await response.body_iterator.aclose()

    assert frame == b": heartbeat\n\n"


def test_list_sessions_returns_session_records(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})

    response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert len(sessions) == 1
    assert set(sessions[0]) == {"id", "title", "created_at", "updated_at"}


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


@pytest.mark.asyncio
async def test_controller_drain_persists_entire_batch_when_live_publish_fails(
    tmp_path: Path,
    caplog,
) -> None:
    class FailFirstPublishBus:
        def __init__(self) -> None:
            self.published: list[tuple[str, PublicEventRecord]] = []

        async def publish(self, session_id: str, record: PublicEventRecord) -> None:
            self.published.append((session_id, record))
            if len(self.published) == 1:
                raise ConnectionError("browser disconnected")

    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    session = app.state.manager.create_session("persist the entire batch")
    controller = FakeController.created[-1]
    controller._events = [
        {"type": "reasoning", "payload": {"summary": "first"}},
        {"type": "reasoning", "payload": {"summary": "second"}},
    ]
    controller._result = SimpleNamespace(text="turn complete")
    bus = FailFirstPublishBus()
    caplog.set_level(logging.WARNING)

    await _drain_controller_events(session.id, app.state.manager, app.state.store, bus)

    records = app.state.store.list_public_events(session.id)
    assert all(isinstance(record, PublicEventRecord) for record in records)
    assert [record.event["payload"]["summary"] for record in records] == [
        "first",
        "second",
    ]
    assert [record.sequence for _, record in bus.published] == [1, 2]
    detail = app.state.store.get_session(session.id)
    assert detail is not None
    assert [(message.role, message.content) for message in detail.messages] == [
        ("assistant", "turn complete")
    ]
    assert "Live controller event publish failed" in caplog.text


def test_cleanup_waits_for_idle_without_timeout_parameter():
    assert "timeout_sec" not in inspect.signature(_consume_turn_result_when_idle).parameters

    class SlowIdleController:
        def __init__(self):
            self.is_running = True
            self.consume_running_states = []
            self._result = SimpleNamespace(text="cleanup text")

        def consume_turn_result(self):
            self.consume_running_states.append(self.is_running)
            if self.is_running:
                raise RuntimeError("Turn is still running.")
            return self._result

    controller = SlowIdleController()

    async def flip_idle_after_ticks() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        controller.is_running = False

    async def exercise() -> SimpleNamespace:
        flip_task = asyncio.create_task(flip_idle_after_ticks())
        result = await _consume_turn_result_when_idle(controller)
        await flip_task
        return result

    result = asyncio.run(exercise())

    assert result.text == "cleanup text"
    assert controller.consume_running_states == [False]


def test_create_logged_task_logs_background_failure(caplog):
    async def failing_task() -> None:
        raise RuntimeError("background exploded")

    async def exercise() -> None:
        task = _create_logged_task(failing_task(), name="failing-test-task")
        with pytest.raises(RuntimeError, match="background exploded"):
            await task
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="vla_data_juicer_agents.web.app"):
        asyncio.run(exercise())

    assert "Background task failed: failing-test-task" in caplog.text


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


def test_delete_session_removes_only_control_state_and_preserves_artifact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    datasets = tmp_path / "VLADatasets"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(datasets))
    raw_artifact = datasets / "raw_data" / "20270623" / "clip-a" / "raw.db3"
    sync_artifact = (
        datasets / "raw_data" / "20270623" / "clip-a" / "sync_data" / "frame.jpg"
    )
    clip_artifact = datasets / "clip_data" / "20270623" / "clip-a" / "clip.db3"
    finish_artifact = datasets / "finish_data" / "20270623" / "clip-a" / "result.bin"
    for path, payload in (
        (raw_artifact, b"raw"),
        (sync_artifact, b"sync"),
        (clip_artifact, b"clip"),
        (finish_artifact, b"finish"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    service = RecordingSessionService()
    client, runtime = make_deletion_client(tmp_path, service)
    session_id = _create_session(client)
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="worker-agent",
        agentscope_session_id="inactive-worker-session",
    )
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="navigation-data-agent",
        agentscope_session_id="active-navigation-session",
    )
    navigation_store = SqliteNavigationTaskStore(
        runtime.config.workspace_root / "navigation-tasks.sqlite"
    )
    task = navigation_store.create_task_attempt(
        request="process",
        target="20270623",
        date="20270623",
        segments=["clip-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=session_id,
        agentscope_session_id="active-navigation-session",
    ).task
    evidence = (
        runtime.config.workspace_root
        / "navigation-evidence"
        / task.task_id
        / "1"
        / "e.json"
    )
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"control evidence")

    response = client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 204
    assert runtime.web_session_store.get_session(session_id) is None
    assert navigation_store.find_by_web_session(session_id) == []
    assert not evidence.exists()
    assert raw_artifact.read_bytes() == b"raw"
    assert sync_artifact.read_bytes() == b"sync"
    assert clip_artifact.read_bytes() == b"clip"
    assert finish_artifact.read_bytes() == b"finish"
    assert service.calls == [
        ("delete-user", "navigation-data-agent", "active-navigation-session"),
        ("delete-user", "worker-agent", "inactive-worker-session"),
    ]
    assert client.delete(f"/api/sessions/{session_id}").status_code == 404


def test_agentscope_delete_failure_keeps_public_and_navigation_control_state(tmp_path: Path):
    service = RecordingSessionService(fail_on_session="internal-as-session")
    client, runtime = make_deletion_client(tmp_path, service)
    session_id = _create_session(client)
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="navigation-data-agent",
        agentscope_session_id="navigation-session",
    )
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="internal-agent",
        agentscope_session_id="internal-as-session",
    )
    navigation_store = SqliteNavigationTaskStore(
        runtime.config.workspace_root / "navigation-tasks.sqlite"
    )
    task = navigation_store.create_task_attempt(
        request="process",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id=session_id,
        agentscope_session_id="navigation-session",
    ).task

    response = client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "session_delete_failed",
            "message": "DataPilot could not delete this session. Please retry.",
        }
    }
    assert "internal-agent" not in response.text
    assert "internal-as-session" not in response.text
    assert runtime.web_session_store.get_session(session_id) is not None
    assert [item.task_id for item in navigation_store.find_by_web_session(session_id)] == [
        task.task_id
    ]


def test_agentscope_delete_retry_accepts_already_absent_earlier_mapping(tmp_path: Path):
    service = FailOnceSessionService()
    client, runtime = make_deletion_client(tmp_path, service)
    session_id = _create_session(client)
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="navigation-data-agent",
        agentscope_session_id="navigation-session",
    )
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="worker-agent",
        agentscope_session_id="worker-session",
    )

    first = client.delete(f"/api/sessions/{session_id}")
    second = client.delete(f"/api/sessions/{session_id}")

    assert first.status_code == 409
    assert second.status_code == 204
    assert runtime.web_session_store.get_session(session_id) is None
    assert service.calls == [
        ("delete-user", "navigation-data-agent", "navigation-session"),
        ("delete-user", "worker-agent", "worker-session"),
        ("delete-user", "navigation-data-agent", "navigation-session"),
        ("delete-user", "worker-agent", "worker-session"),
    ]


@pytest.mark.parametrize(
    "error",
    [
        OSError("private evidence path /raw/internal"),
        sqlite3.OperationalError("private table internal_control"),
        RuntimeError("private runtime internal-agent"),
        ValueError("private task internal-as-session"),
    ],
)
def test_delete_error_boundary_returns_stable_non_sensitive_response(
    tmp_path: Path,
    error: Exception,
):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    def fail_delete(_session_id: str) -> None:
        raise error

    client.app.state.manager.delete_session = fail_delete

    response = client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "session_delete_failed",
            "message": "DataPilot could not delete this session. Please retry.",
        }
    }
    assert "private" not in response.text
    assert "internal" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), KeyboardInterrupt()])
async def test_delete_error_boundary_does_not_swallow_base_exceptions(
    tmp_path: Path,
    error: BaseException,
):
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
    )
    session = app.state.manager.create_session("delete")

    async def fail_delete(_session_id: str) -> None:
        raise error

    app.state.manager.delete_session = fail_delete
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/sessions/{session_id}"
        and "DELETE" in getattr(route, "methods", set())
    )

    with pytest.raises(type(error)):
        await endpoint(session.id)


def test_evidence_root_symlink_failure_preserves_raw_navigation_and_public_state(
    tmp_path: Path,
):
    raw_artifact = tmp_path / "raw_data" / "clip" / "raw.db3"
    raw_artifact.parent.mkdir(parents=True)
    raw_artifact.write_bytes(b"raw")
    service = IdempotentSessionService()
    client, runtime = make_deletion_client(tmp_path, service)
    session_id = _create_session(client)
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="navigation-data-agent",
        agentscope_session_id="navigation-session",
    )
    navigation_store = SqliteNavigationTaskStore(
        runtime.config.workspace_root / "navigation-tasks.sqlite"
    )
    task = navigation_store.create_task_attempt(
        request="process",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id=session_id,
        agentscope_session_id="navigation-session",
    ).task
    evidence_root = runtime.config.workspace_root / "navigation-evidence"
    evidence_root.parent.mkdir(parents=True, exist_ok=True)
    evidence_root.symlink_to(raw_artifact.parent, target_is_directory=True)

    response = client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_delete_failed"
    assert runtime.web_session_store.get_session(session_id) is not None
    assert navigation_store.get_task(task.task_id) is not None
    assert raw_artifact.read_bytes() == b"raw"


def test_navigation_db_failure_after_evidence_delete_retries_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = IdempotentSessionService()
    client, runtime = make_deletion_client(tmp_path, service)
    session_id = _create_session(client)
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="navigation-data-agent",
        agentscope_session_id="navigation-session",
    )
    services = runtime._navigation_services()
    task = services.task_store.create_task_attempt(
        request="process",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id=session_id,
        agentscope_session_id="navigation-session",
    ).task
    evidence = (
        runtime.config.workspace_root
        / "navigation-evidence"
        / task.task_id
        / "e.json"
    )
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"evidence")
    original_connect = services.task_store._connect
    fail_transaction = True

    class InjectedFailureConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql: str, parameters=()):
            if (
                fail_transaction
                and 'DELETE FROM "navigation_human_decision_handoffs"' in sql
            ):
                raise sqlite3.OperationalError("internal navigation table")
            return self.connection.execute(sql, parameters)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self.connection.__exit__(exc_type, exc, traceback)

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

    def injected_connect():
        return InjectedFailureConnection(original_connect())

    monkeypatch.setattr(runtime, "_navigation_services", lambda: services)
    monkeypatch.setattr(services.task_store, "_connect", injected_connect)

    first = client.delete(f"/api/sessions/{session_id}")

    assert first.status_code == 409
    assert first.json()["detail"]["code"] == "session_delete_failed"
    assert "internal navigation table" not in first.text
    assert not evidence.exists()
    assert services.task_store.get_task(task.task_id) is not None
    assert runtime.web_session_store.get_session(session_id) is not None

    fail_transaction = False
    second = client.delete(f"/api/sessions/{session_id}")

    assert second.status_code == 204
    assert runtime.web_session_store.get_session(session_id) is None


def test_public_db_failure_after_dependencies_delete_retries_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = IdempotentSessionService()
    client, runtime = make_deletion_client(tmp_path, service)
    session_id = _create_session(client)
    runtime.web_session_store.save_agentscope_session_mapping(
        session_id,
        agent_id="navigation-data-agent",
        agentscope_session_id="navigation-session",
    )
    navigation_store = SqliteNavigationTaskStore(
        runtime.config.workspace_root / "navigation-tasks.sqlite"
    )
    task = navigation_store.create_task_attempt(
        request="process",
        target="20270623",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id=session_id,
        agentscope_session_id="navigation-session",
    ).task
    original_delete = runtime.web_session_store.delete_session
    failed_once = False

    def fail_public_once(web_session_id: str) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise sqlite3.OperationalError(
                "public db failed for internal-agent / internal-as-session"
            )
        original_delete(web_session_id)

    monkeypatch.setattr(runtime.web_session_store, "delete_session", fail_public_once)

    first = client.delete(f"/api/sessions/{session_id}")
    second = client.delete(f"/api/sessions/{session_id}")

    assert first.status_code == 409
    assert first.json()["detail"]["code"] == "session_delete_failed"
    assert "internal-agent" not in first.text
    assert "internal-as-session" not in first.text
    assert navigation_store.get_task(task.task_id) is None
    assert second.status_code == 204
    assert runtime.web_session_store.get_session(session_id) is None
    assert service.calls == [
        ("delete-user", "navigation-data-agent", "navigation-session"),
        ("delete-user", "navigation-data-agent", "navigation-session"),
    ]


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


def test_navigation_dataset_summary_returns_scanned_totals_and_sync_distribution(tmp_path: Path, monkeypatch):
    root = tmp_path / "VLADatasets"
    _write_dataset_metadata(root / "raw_data" / "20270605" / "clip_a")
    _write_dataset_metadata(root / "raw_data" / "20270605" / "clip_b")
    _write_sync_file(root, "20270605", "clip_a", "0001", "front.jpg", b"jpg-bytes")
    _write_sync_file(root, "20270605", "clip_a", "0001", "front.png", b"png-bytes")
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    response = client.get("/api/navigation/datasets/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["date_count"] == 1
    assert body["totals"]["clip_count"] == 2
    assert body["totals"]["total_duration_ns"] == 6_000_000_000
    assert body["totals"]["raw_message_count"] == 40
    assert body["sync_distribution"]["image"] == 2


def test_navigation_date_returns_clip_detail_and_raw_only_status(tmp_path: Path, monkeypatch):
    root = tmp_path / "VLADatasets"
    _write_dataset_metadata(root / "raw_data" / "20270605" / "raw_clip")
    _write_dataset_metadata(root / "raw_data" / "20270605" / "synced_clip")
    _write_sync_file(root, "20270605", "synced_clip", "0001", "front.jpg", b"jpg-bytes")
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    response = client.get("/api/navigation/datasets/20270605")

    assert response.status_code == 200
    body = response.json()
    clips = {clip["clip"]: clip for clip in body["clips"]}
    assert body["date"] == "20270605"
    assert body["clip_count"] == 2
    assert clips["raw_clip"]["status"] == "raw_only"
    assert clips["raw_clip"]["has_tmp_dir"] is False
    assert clips["raw_clip"]["sync_frame_counts"]["image"] == 0
    assert clips["synced_clip"]["status"] == "synced"


def test_navigation_sync_images_listing_and_file_route_serves_bytes(tmp_path: Path, monkeypatch):
    root = tmp_path / "VLADatasets"
    _write_dataset_metadata(root / "raw_data" / "20270605" / "clip_a")
    _write_sync_file(root, "20270605", "clip_a", "0002", "b.png", b"png-bytes")
    _write_sync_file(root, "20270605", "clip_a", "0002", "a.jpeg", b"jpeg-bytes")
    _write_sync_file(root, "20270605", "clip_a", "0001", "c.jpg", b"jpg-bytes")
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    listing_response = client.get("/api/navigation/datasets/20270605/clips/clip_a/sync-images")
    file_response = client.get("/api/navigation/datasets/20270605/clips/clip_a/sync-images/0002/a.jpeg")

    assert listing_response.status_code == 200
    assert listing_response.json()["sequences"] == [
        {"sequence": "0001", "images": ["c.jpg"]},
        {"sequence": "0002", "images": ["a.jpeg", "b.png"]},
    ]
    assert file_response.status_code == 200
    assert file_response.content == b"jpeg-bytes"


def test_navigation_sync_image_route_rejects_unsafe_sequence(tmp_path: Path, monkeypatch):
    root = tmp_path / "VLADatasets"
    _write_dataset_metadata(root / "raw_data" / "20270605" / "clip_a")
    _write_sync_file(root, "20270605", "clip_a", "0002", "a.jpeg", b"jpeg-bytes")
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    response = client.get("/api/navigation/datasets/20270605/clips/clip_a/sync-images/%2E%2E/a.jpeg")

    assert response.status_code in {400, 404}


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})
    return response.json()["session"]["id"]


def _route_endpoint(app: FastAPI, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def _write_dataset_metadata(clip_dir: Path) -> None:
    clip_dir.mkdir(parents=True)
    (clip_dir / "metadata.yaml").write_text(
        """
rosbag2_bagfile_information:
  version: 4
  storage_identifier: sqlite3
  relative_file_paths:
    - sample_0.db3
  duration:
    nanoseconds: 3000000000
  starting_time:
    nanoseconds_since_epoch: 1778812189469693651
  message_count: 20
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
        serialization_format: cdr
      message_count: 10
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
        serialization_format: cdr
      message_count: 10
  compression_format: ""
  compression_mode: ""
""".lstrip(),
        encoding="utf-8",
    )


def _write_sync_file(root: Path, date: str, clip: str, sequence: str, filename: str, content: bytes) -> None:
    image_dir = root / "clip_data" / date / clip / "sync_data" / sequence / "fisheye_front"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / filename).write_bytes(content)
