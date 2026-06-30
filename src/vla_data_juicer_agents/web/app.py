from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from vla_data_juicer_agents.navigation.dataset_catalog import (
    list_sync_images,
    resolve_sync_image_path,
    scan_navigation_dataset,
    scan_navigation_date,
)
from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import (
    CreateSessionResponse,
    CreateTurnRequest,
    CreateTurnResponse,
    InterruptResponse,
)
from vla_data_juicer_agents.web.session_manager import ControllerFactory, WebSessionManager
from vla_data_juicer_agents.web.session_store import WebSessionStore


def create_app(
    working_dir: str | None = None,
    model: str | None = None,
    db_path: str | Path | None = None,
    controller_factory: ControllerFactory = SessionController,
    frontend_dist: str | Path | None = None,
    agentscope_runtime: Any | None = None,
) -> FastAPI:
    if working_dir is None:
        working_dir = os.environ.get("VLA_DATA_AGENT_WEB_WORKING_DIR", "./.djx")
    if model is None:
        model = os.environ.get("VLA_DATA_AGENT_WEB_MODEL") or None
    if frontend_dist is None:
        frontend_dist = os.environ.get("VLA_DATA_AGENT_WEB_FRONTEND_DIST") or None

    database_path = Path(db_path) if db_path is not None else Path(working_dir) / "sessions.sqlite"
    store = WebSessionStore(database_path)
    manager = WebSessionManager(
        store=store,
        working_dir=working_dir,
        model=model,
        controller_factory=controller_factory,
    )
    bus = SessionEventBus()

    app = FastAPI(title="DataPilot Web API")
    app.state.store = store
    app.state.manager = manager
    app.state.bus = bus
    app.state.agentscope_runtime = agentscope_runtime

    if agentscope_runtime is not None:
        app.mount(agentscope_runtime.config.agentscope_mount_path, agentscope_runtime.app)

    @app.post("/api/sessions", response_model=CreateSessionResponse)
    async def create_session(request: CreateTurnRequest) -> CreateSessionResponse:
        session = manager.create_session(request.message)
        return CreateSessionResponse(session=session)

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, list[dict[str, Any]]]:
        return {"sessions": [session.model_dump() for session in store.list_sessions()]}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, dict[str, Any]]:
        session = store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session": session.model_dump()}

    @app.get("/api/navigation/datasets/summary")
    async def navigation_dataset_summary() -> dict[str, Any]:
        try:
            return scan_navigation_dataset().model_dump(mode="json")
        except (ValueError, FileNotFoundError) as exc:
            _raise_navigation_http_error(exc)

    @app.get("/api/navigation/datasets/{date}")
    async def navigation_date_summary(date: str) -> dict[str, Any]:
        try:
            return scan_navigation_date(date).model_dump(mode="json")
        except (ValueError, FileNotFoundError) as exc:
            _raise_navigation_http_error(exc)

    @app.get("/api/navigation/datasets/{date}/clips/{clip}/sync-images")
    async def navigation_sync_images(date: str, clip: str) -> dict[str, Any]:
        try:
            return list_sync_images(date, clip).model_dump(mode="json")
        except (ValueError, FileNotFoundError) as exc:
            _raise_navigation_http_error(exc)

    @app.get("/api/navigation/datasets/{date}/clips/{clip}/sync-images/{sequence}/{filename}")
    async def navigation_sync_image_file(date: str, clip: str, sequence: str, filename: str) -> FileResponse:
        try:
            return FileResponse(resolve_sync_image_path(date, clip, sequence, filename))
        except (ValueError, FileNotFoundError) as exc:
            _raise_navigation_http_error(exc)

    @app.post("/api/sessions/{session_id}/turns", response_model=CreateTurnResponse)
    async def submit_turn(session_id: str, request: CreateTurnRequest) -> CreateTurnResponse:
        try:
            turn_id = manager.submit_turn(session_id, request.message)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        asyncio.create_task(_drain_controller_events(session_id, manager, store, bus))
        return CreateTurnResponse(turn_id=turn_id)

    @app.post("/api/sessions/{session_id}/interrupt", response_model=InterruptResponse)
    async def interrupt(session_id: str) -> InterruptResponse:
        try:
            interrupted = manager.interrupt(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        return InterruptResponse(interrupted=interrupted)

    @app.websocket("/api/sessions/{session_id}/events")
    async def session_events(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        try:
            async with bus.subscribe(session_id) as queue:
                while True:
                    await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            return

    if frontend_dist is not None:
        frontend_path = Path(frontend_dist)
        if frontend_path.exists():
            assets_path = frontend_path / "assets"
            if assets_path.exists():
                app.mount("/assets", StaticFiles(directory=assets_path), name="frontend-assets")

            brand_path = frontend_path / "brand"
            if brand_path.exists():
                app.mount("/brand", StaticFiles(directory=brand_path), name="frontend-brand")

            index_path = frontend_path / "index.html"
            if index_path.exists():
                @app.get("/", include_in_schema=False)
                async def frontend_index() -> FileResponse:
                    return FileResponse(index_path)

    return app


def _raise_navigation_http_error(exc: ValueError | FileNotFoundError) -> None:
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _drain_controller_events(
    session_id: str,
    manager: WebSessionManager,
    store: WebSessionStore,
    bus: SessionEventBus,
) -> None:
    try:
        controller = manager.get_controller(session_id)
    except KeyError:
        return

    persisted_final_texts: set[str] = set()

    async def drain_once() -> None:
        for event in controller.drain_events():
            await bus.publish(session_id, event)
            text = _final_event_text(event)
            if text is not None and text not in persisted_final_texts:
                store.append_message(session_id, role="assistant", content=text)
                persisted_final_texts.add(text)

    drained_to_completion = False
    try:
        while controller.is_running:
            await drain_once()
            await asyncio.sleep(0.03)

        await drain_once()
        drained_to_completion = True
    finally:
        result = await _consume_turn_result_when_idle(controller)
        if drained_to_completion and result is not None:
            text = getattr(result, "text", None)
            if isinstance(text, str) and text and text not in persisted_final_texts:
                store.append_message(session_id, role="assistant", content=text)
                persisted_final_texts.add(text)


def _final_event_text(event: dict[str, Any]) -> str | None:
    if event.get("type") != "final":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    return text if isinstance(text, str) and text else None


async def _consume_turn_result_when_idle(controller: Any) -> Any | None:
    while getattr(controller, "is_running", False):
        await asyncio.sleep(0.03)
    return _consume_turn_result(controller)


def _consume_turn_result(controller: Any) -> Any | None:
    consume_turn_result = getattr(controller, "consume_turn_result", None)
    if not callable(consume_turn_result):
        return None
    try:
        return consume_turn_result()
    except RuntimeError:
        return None
