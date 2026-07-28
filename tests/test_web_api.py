from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vla_data_juicer_agents.web.app as web_app_module
from vla_data_juicer_agents.navigation import dataset_catalog
from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationServiceOnlineError,
)
from vla_data_juicer_agents.web.app import (
    _create_logged_task,
    create_app,
)


class FakeAgentScopeRuntime:
    def __init__(self, *, submit_error: str | None = None) -> None:
        self.app = FastAPI()
        self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")
        self.submit_error = submit_error
        self.submitted: list[tuple[str, str, str]] = []
        self.interrupts: list[str] = []
        self.events: dict[str, list[dict]] = {}

    async def submit_user_message(
        self,
        *,
        web_session_id: str,
        message: str,
        turn_id: str,
    ) -> str:
        if self.submit_error is not None:
            raise RuntimeError(self.submit_error)
        self.submitted.append((web_session_id, message, turn_id))
        self.events.setdefault(web_session_id, []).append(
            {
                "contract_version": 1,
                "type": "final",
                "turn_id": turn_id,
                "payload": {"text": f"完成: {message}", "stop": False},
            }
        )
        return turn_id

    @staticmethod
    def session_task_snapshots(_session_id: str) -> list[dict]:
        return []

    @staticmethod
    def pending_interaction_snapshot(_session_id: str) -> None:
        return None

    async def interrupt_web_session(self, *, web_session_id: str) -> bool:
        self.interrupts.append(web_session_id)
        return True

    async def subscribe_web_session_events(self, *, web_session_id: str):
        for event in self.events.pop(web_session_id, []):
            yield event


def make_client(tmp_path: Path) -> TestClient:
    runtime = FakeAgentScopeRuntime()
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        model="qwen-test",
        agentscope_runtime=runtime,
    )
    return TestClient(app)


def test_create_session_returns_title(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})

    assert response.status_code == 200
    body = response.json()
    assert body["session"]["id"].startswith("session_")
    assert body["session"]["title"] == "处理 20270605 的室外导航数据"
    assert body["session"]["contract_version"] == 1


def test_submit_turn_returns_turn_id(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})

    assert response.status_code == 200
    assert response.json()["turn_id"].startswith("turn_")


def test_submit_turn_replays_same_invocation_without_resubmitting(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)
    payload = {"message": "开始处理", "invocation_id": "navigation-request-1"}

    first = client.post(f"/api/sessions/{session_id}/turns", json=payload)
    replay = client.post(f"/api/sessions/{session_id}/turns", json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["turn_id"] == first.json()["turn_id"]
    runtime = client.app.state.agentscope_runtime
    assert [message for _session, message, _turn in runtime.submitted] == ["开始处理"]
    detail = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert len(detail["turns"]) == 1
    assert [message["content"] for message in detail["messages"]].count("开始处理") == 1


def test_submit_turn_same_invocation_always_returns_original_turn(tmp_path: Path):
    client = make_client(tmp_path)
    session_id = _create_session(client)

    first = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"message": "开始处理", "invocation_id": "navigation-request-1"},
    )
    replay = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"message": "这条消息不会被再次执行", "invocation_id": "navigation-request-1"},
    )

    assert replay.status_code == 200
    assert replay.json()["turn_id"] == first.json()["turn_id"]
    runtime = client.app.state.agentscope_runtime
    assert [message for _session, message, _turn in runtime.submitted] == ["开始处理"]


def test_create_app_accepts_positional_configuration(tmp_path: Path):
    runtime = FakeAgentScopeRuntime()
    app = create_app(
        str(tmp_path / ".djx"),
        "qwen-positional",
        tmp_path / "sessions.sqlite",
        None,
        runtime,
    )
    client = TestClient(app)

    session_id = _create_session(client)

    assert session_id.startswith("session_")
    assert app.state.agentscope_runtime is runtime


def test_create_app_fails_closed_without_agentscope_runtime(tmp_path: Path):
    with pytest.raises(RuntimeError, match="requires an AgentScope runtime"):
        create_app(db_path=tmp_path / "sessions.sqlite")

    assert not (tmp_path / "sessions.sqlite").exists()


def test_conflicting_web_start_does_not_create_or_migrate_session_store(
    tmp_path: Path,
):
    working_dir = tmp_path / ".djx"
    runtime = FakeAgentScopeRuntime()
    active_app = create_app(
        working_dir=str(working_dir),
        db_path=working_dir / "active-sessions.sqlite",
        agentscope_runtime=runtime,
    )
    new_session_database = working_dir / "new-sessions.sqlite"
    existing_session_database = working_dir / "existing-sessions.sqlite"
    with sqlite3.connect(existing_session_database) as connection:
        connection.execute(
            "CREATE TABLE preexisting_schema (value TEXT NOT NULL)",
        )
        connection.execute(
            "INSERT INTO preexisting_schema VALUES ('unchanged')",
        )
    existing_bytes = existing_session_database.read_bytes()

    try:
        with pytest.raises(AnnotationServiceOnlineError):
            create_app(
                working_dir=str(working_dir),
                db_path=new_session_database,
                agentscope_runtime=FakeAgentScopeRuntime(),
            )
        with pytest.raises(AnnotationServiceOnlineError):
            create_app(
                working_dir=str(working_dir),
                db_path=existing_session_database,
                agentscope_runtime=FakeAgentScopeRuntime(),
            )
    finally:
        active_app.state.annotation_maintenance_lease.close()

    assert not new_session_database.exists()
    assert existing_session_database.read_bytes() == existing_bytes


def test_create_app_mounts_agentscope_when_runtime_factory_provided(tmp_path: Path):
    fake_runtime = SimpleNamespace(
        app=FastAPI(),
        config=SimpleNamespace(agentscope_mount_path="/api/agentscope"),
    )

    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
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
        agentscope_runtime=runtime,
    )

    with TestClient(app):
        assert sub_app.state.ready is True
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]


def test_web_app_lifespan_is_one_shot_and_releases_maintenance_lease(
    tmp_path: Path,
):
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.annotation_maintenance_lease.closed is False

        assert app.state.annotation_maintenance_lease.closed is True
        with pytest.raises(RuntimeError, match="can only start once"):
            async with app.router.lifespan_context(app):
                pytest.fail("a completed Web app lifespan was restarted")

    asyncio.run(exercise())


def test_web_app_rejects_overlapping_lifespan_without_releasing_active_lease(
    tmp_path: Path,
):
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            with pytest.raises(RuntimeError, match="can only start once"):
                async with app.router.lifespan_context(app):
                    pytest.fail("overlapping Web app lifespans were accepted")
            assert app.state.annotation_maintenance_lease.closed is False

        assert app.state.annotation_maintenance_lease.closed is True

    asyncio.run(exercise())


def test_lifespan_cancellation_waits_for_worker_thread_before_releasing_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = threading.Event()
    worker_stop_requested = threading.Event()
    worker_return = threading.Event()

    class BlockingAnnotationWorker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run_forever(self) -> None:
            worker_started.set()
            await asyncio.to_thread(worker_return.wait)

        async def stop(self) -> None:
            worker_stop_requested.set()

    monkeypatch.setattr(
        web_app_module,
        "AnnotationWorker",
        BlockingAnnotationWorker,
    )
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
    )

    async def exercise() -> None:
        leave_lifespan = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                await leave_lifespan.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        while not worker_started.is_set():
            await asyncio.sleep(0)
        leave_lifespan.set()
        while not worker_stop_requested.is_set():
            await asyncio.sleep(0)

        lifespan_task.cancel()
        await asyncio.sleep(0)
        assert lifespan_task.done() is False
        assert app.state.annotation_maintenance_lease.closed is False

        lifespan_task.cancel()
        await asyncio.sleep(0)
        assert lifespan_task.done() is False
        assert app.state.annotation_maintenance_lease.closed is False

        worker_return.set()
        with pytest.raises(asyncio.CancelledError):
            await lifespan_task
        assert app.state.annotation_maintenance_lease.closed is True

    asyncio.run(exercise())


def test_create_app_releases_maintenance_lease_when_last_spa_route_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    original_get = FastAPI.get

    def fail_on_last_spa_route(self, path, *args, **kwargs):
        route_decorator = original_get(self, path, *args, **kwargs)
        if path != "/agent":
            return route_decorator

        def fail_registration(_endpoint):
            raise RuntimeError("injected final route registration failure")

        return fail_registration

    with monkeypatch.context() as scoped:
        scoped.setattr(FastAPI, "get", fail_on_last_spa_route)
        with pytest.raises(
            RuntimeError,
            match="injected final route registration failure",
        ):
            create_app(
                working_dir=str(tmp_path / ".djx"),
                db_path=tmp_path / "sessions.sqlite",
                frontend_dist=dist,
                agentscope_runtime=FakeAgentScopeRuntime(),
            )

    replacement = create_app(
        working_dir=str(tmp_path / "replacement-djx"),
        db_path=tmp_path / "replacement-sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
    )
    replacement.state.annotation_maintenance_lease.close()


def test_create_app_uses_agentscope_session_manager_when_runtime_present(tmp_path: Path):
    class FakeRuntime:
        def __init__(self) -> None:
            self.app = FastAPI()
            self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")
            self.submitted = []

        async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
            self.submitted.append((web_session_id, message))
            return "turn-agent-1"

        @staticmethod
        def session_task_snapshots(_session_id: str) -> list[dict]:
            return []

        @staticmethod
        def pending_interaction_snapshot(_session_id: str) -> None:
            return None

    runtime = FakeRuntime()
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=runtime,
    )
    client = TestClient(app)

    session_id = _create_session(client)
    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})

    assert response.status_code == 200
    turn_id = response.json()["turn_id"]
    assert turn_id.startswith("turn_")
    detail = client.get(f"/api/sessions/{session_id}").json()["session"]
    assert detail["turns"][0]["id"] == turn_id
    assert detail["messages"][0]["turn_id"] == turn_id
    assert runtime.submitted == [(session_id, "开始处理")]


def test_frontend_index_served_when_dist_provided(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><main>DataPilot</main>", encoding="utf-8")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        frontend_dist=dist,
        agentscope_runtime=FakeAgentScopeRuntime(),
    )
    client = TestClient(app)

    response = client.get("/")
    api_response = client.get("/api/sessions")

    assert response.status_code == 200
    assert response.text == "<!doctype html><main>DataPilot</main>"
    assert response.headers["content-type"].startswith("text/html")
    assert api_response.status_code == 200


def test_annotation_workspace_deep_links_serve_spa_index(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    index = "<!doctype html><main>Annotation workspace</main>"
    (dist / "index.html").write_text(index, encoding="utf-8")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        frontend_dist=dist,
        agentscope_runtime=FakeAgentScopeRuntime(),
    )
    client = TestClient(app)

    for path in (
        "/annotation",
        "/annotation/jobs",
        "/annotation/jobs/job_0123456789abcdef0123456789abcdef",
        (
            "/annotation/jobs/job_0123456789abcdef0123456789abcdef/"
            "segments/segment_0123456789abcdef0123456789abcdef"
        ),
        "/annotation/reviews",
        "/annotation/reviews/review_0123456789abcdef0123456789abcdef",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.text == index
        assert response.headers["content-type"].startswith("text/html")


def test_frontend_index_served_from_env_dist(monkeypatch, tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><main>DataPilot env</main>", encoding="utf-8")
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_FRONTEND_DIST", str(dist))
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
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
        frontend_dist=dist,
        agentscope_runtime=FakeAgentScopeRuntime(),
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
        frontend_dist=dist,
        agentscope_runtime=FakeAgentScopeRuntime(),
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
        frontend_dist=dist,
        agentscope_runtime=FakeAgentScopeRuntime(),
    )
    client = TestClient(app)

    root_response = client.get("/")
    api_response = client.get("/api/sessions")

    assert root_response.status_code == 404
    assert api_response.status_code == 200


def test_submit_turn_runtime_error_returns_409(tmp_path: Path):
    runtime = FakeAgentScopeRuntime(submit_error="A session turn is already active.")
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=runtime,
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
            events = [
                executor.submit(websocket.receive_json).result(timeout=1),
                executor.submit(websocket.receive_json).result(timeout=1),
            ]

    assert [event["type"] for event in events] == ["turn_start", "final"]
    assert events[-1]["payload"]["text"] == "完成: 开始处理"


def test_list_sessions_returns_session_records(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})

    response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert len(sessions) == 1
    assert set(sessions[0]) == {
        "id",
        "title",
        "status",
        "contract_version",
        "created_at",
        "updated_at",
    }
    assert sessions[0]["contract_version"] == 1


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


def test_create_app_reads_working_dir_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(tmp_path / "env-djx"))
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_MODEL", "qwen-env")
    app = create_app(agentscope_runtime=FakeAgentScopeRuntime())
    client = TestClient(app)

    session_id = _create_session(client)

    assert session_id.startswith("session_")
    assert app.state.store.db_path == tmp_path / "env-djx" / "sessions.sqlite"


def test_create_app_accepts_empty_legacy_model_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(tmp_path / "env-djx"))
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_MODEL", "")
    app = create_app(
        db_path=tmp_path / "sessions.sqlite",
        agentscope_runtime=FakeAgentScopeRuntime(),
    )
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={"message": "处理 20270605 的室外导航数据"},
    )

    assert response.status_code == 200


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


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/navigation/datasets/summary",
        "/api/navigation/datasets/20270605",
    ],
)
def test_navigation_dataset_success_response_does_not_expose_missing_metadata_path(
    endpoint: str,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "private-user-heying-VLADatasets"
    (root / "raw_data" / "20270605" / "missing_metadata").mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    response = client.get(endpoint)

    assert response.status_code == 200
    body = response.json()
    clips = body["dates"][0]["clips"] if endpoint.endswith("summary") else body["clips"]
    assert clips[0]["status"] == "error"
    assert clips[0]["errors"] == ["metadata.yaml: file not found"]
    assert str(root) not in response.text
    assert "/private-user-heying-" not in response.text


def test_navigation_date_success_response_does_not_expose_sync_scan_error(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "VLADatasets"
    _write_dataset_metadata(root / "raw_data" / "20270605" / "private_clip")
    _write_sync_file(
        root,
        "20270605",
        "private_clip",
        "0001",
        "front.jpg",
        b"jpg-bytes",
    )
    private_error = "/media/heying/private/data Bearer secret-navigation-token"
    original_visible_files = dataset_catalog._visible_files

    def fail_for_sync_scan(path: Path):
        if path.name == "fisheye_front":
            raise OSError(private_error)
        return original_visible_files(path)

    monkeypatch.setattr(dataset_catalog, "_visible_files", fail_for_sync_scan)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    response = client.get("/api/navigation/datasets/20270605")

    assert response.status_code == 200
    clip = response.json()["clips"][0]
    assert clip["status"] == "error"
    assert clip["errors"] == [
        "sync_data: unreadable",
        "clip_data exists without tmp_dir or synced frames",
    ]
    assert private_error not in response.text
    assert "/media/" not in response.text
    assert "Bearer" not in response.text


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


def test_navigation_missing_resource_does_not_expose_absolute_path(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "private-VLADatasets"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    client = make_client(tmp_path)

    response = client.get(
        "/api/navigation/datasets/20270605/clips/missing/sync-images",
    )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "The requested navigation dataset resource was not found."
    )
    assert str(root) not in response.text


def test_navigation_invalid_request_does_not_echo_private_error(
    tmp_path: Path,
    monkeypatch,
):
    private_error = f"{tmp_path}/private sk-abcdefghijklmnop"

    def fail_with_private_error(*_args, **_kwargs):
        raise ValueError(private_error)

    monkeypatch.setattr(
        web_app_module,
        "list_sync_images",
        fail_with_private_error,
    )
    client = make_client(tmp_path)

    response = client.get(
        "/api/navigation/datasets/20270605/clips/missing/sync-images",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "The navigation dataset request is invalid."
    )
    assert str(tmp_path) not in response.text
    assert "sk-abcdefghijklmnop" not in response.text


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"message": "处理 20270605 的室外导航数据"})
    return response.json()["session"]["id"]


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
