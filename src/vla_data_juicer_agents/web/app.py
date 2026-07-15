from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from vla_data_juicer_agents.navigation.dataset_catalog import (
    list_sync_images,
    resolve_sync_image_path,
    scan_navigation_dataset,
    scan_navigation_date,
)
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import (
    CreateSessionResponse,
    CreateTurnRequest,
    CreateTurnResponse,
    HumanDecisionRequest,
    HumanDecisionRecoveryRequest,
    HumanDecisionRecoveryResponse,
    HumanDecisionResponse,
    InterruptResponse,
    PublicEventRecord,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore
from vla_data_juicer_agents.web.sse import iter_sse

logger = logging.getLogger(__name__)
SESSION_DELETE_ERROR = {
    "code": "session_delete_failed",
    "message": "DataPilot could not delete this session. Please retry.",
}


def create_app(
    working_dir: str | None = None,
    model: str | None = None,
    db_path: str | Path | None = None,
    frontend_dist: str | Path | None = None,
    agentscope_runtime: Any | None = None,
    sse_heartbeat_seconds: float = 15.0,
) -> FastAPI:
    if working_dir is None:
        working_dir = os.environ.get("VLA_DATA_AGENT_WEB_WORKING_DIR", "./.djx")
    if model is None:
        model = os.environ.get("VLA_DATA_AGENT_WEB_MODEL") or None
    if frontend_dist is None:
        frontend_dist = os.environ.get("VLA_DATA_AGENT_WEB_FRONTEND_DIST") or None

    if agentscope_runtime is None:
        raise RuntimeError(
            "AgentScope runtime is required for the production Web application"
        )

    return _create_app_for_manager(
        working_dir=working_dir,
        db_path=db_path,
        frontend_dist=frontend_dist,
        agentscope_runtime=agentscope_runtime,
        sse_heartbeat_seconds=sse_heartbeat_seconds,
        manager_builder=lambda store, publish: AgentScopeWebSessionManager(
            store=store,
            runtime=agentscope_runtime,
            event_callback=publish,
        ),
    )


def _create_app_for_manager(
    *,
    working_dir: str,
    db_path: str | Path | None,
    frontend_dist: str | Path | None,
    agentscope_runtime: Any | None,
    sse_heartbeat_seconds: float,
    manager_builder: Callable[[WebSessionStore, Callable[..., Any]], Any],
    turn_submitted: Callable[[str, Any, WebSessionStore, SessionEventBus], Any]
    | None = None,
) -> FastAPI:
    database_path = Path(db_path) if db_path is not None else Path(working_dir) / "sessions.sqlite"
    store = WebSessionStore(database_path)
    bus = SessionEventBus()

    async def publish_session_event(session_id: str, event: PublicEventRecord) -> None:
        await bus.publish(session_id, event)

    manager = manager_builder(store, publish_session_event)

    @asynccontextmanager
    async def lifespan(_parent_app: FastAPI):
        if agentscope_runtime is None:
            yield
            return

        async with agentscope_runtime.app.router.lifespan_context(agentscope_runtime.app):
            start_stop_coordinator = getattr(
                agentscope_runtime,
                "start_stop_coordinator",
                None,
            )
            stop_stop_coordinator = getattr(
                agentscope_runtime,
                "stop_stop_coordinator",
                None,
            )
            if callable(start_stop_coordinator):
                await start_stop_coordinator()
            recovery_loop = getattr(agentscope_runtime, "run_agent_wakeup_recovery_loop", None)
            recovery_task = (
                asyncio.create_task(
                    recovery_loop(),
                    name="agentscope-wakeup-recovery",
                )
                if callable(recovery_loop)
                else None
            )
            try:
                yield
            finally:
                if recovery_task is not None:
                    recovery_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await recovery_task
                if callable(stop_stop_coordinator):
                    await stop_stop_coordinator()

    app = FastAPI(title="DataPilot Web API", lifespan=lifespan)
    app.state.store = store
    app.state.manager = manager
    app.state.bus = bus
    app.state.agentscope_runtime = agentscope_runtime

    if agentscope_runtime is not None:
        app.mount(agentscope_runtime.config.agentscope_mount_path, agentscope_runtime.app)

    @app.post("/api/sessions", response_model=CreateSessionResponse)
    async def create_session(request: CreateTurnRequest) -> CreateSessionResponse:
        session = await _maybe_await(manager.create_session(request.message))
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

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> Response:
        try:
            session = store.get_session(session_id)
            if session is not None:
                await _maybe_await(manager.delete_session(session_id))
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("DataPilot session deletion failed: session_id=%s", session_id)
            raise HTTPException(status_code=409, detail=SESSION_DELETE_ERROR) from exc
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return Response(status_code=204)

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
            turn_id = await _maybe_await(manager.submit_turn(session_id, request.message))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if turn_submitted is not None:
            await _maybe_await(turn_submitted(session_id, manager, store, bus))
        return CreateTurnResponse(turn_id=turn_id)

    @app.post("/api/sessions/{session_id}/interrupt", response_model=InterruptResponse)
    async def interrupt(session_id: str) -> InterruptResponse:
        try:
            result = await _maybe_await(manager.interrupt(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("DataPilot session stop failed")
            raise HTTPException(
                status_code=503,
                detail="Session stop failed; retry the request",
            ) from exc
        if isinstance(result, InterruptResponse):
            return result
        interrupted = getattr(result, "interrupted", None)
        stopped_tool_call_ids = getattr(result, "stopped_tool_call_ids", None)
        if isinstance(interrupted, bool):
            return InterruptResponse(
                interrupted=interrupted,
                stopped_tool_call_ids=list(stopped_tool_call_ids or ()),
            )
        return InterruptResponse(interrupted=bool(result))

    @app.post("/api/sessions/{session_id}/human-decisions", response_model=HumanDecisionResponse)
    async def submit_human_decision(
        session_id: str,
        request: HumanDecisionRequest,
    ) -> HumanDecisionResponse:
        submit_decision = getattr(manager, "submit_human_decision", None)
        if submit_decision is None:
            raise HTTPException(status_code=409, detail="Human decisions are not supported")
        try:
            accepted = await _maybe_await(
                submit_decision(session_id, request.model_dump(exclude_none=True))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not accepted:
            raise HTTPException(status_code=409, detail="Human decision was not accepted")
        return HumanDecisionResponse(accepted=True)

    @app.post(
        "/api/sessions/{session_id}/human-decisions/recovery",
        response_model=HumanDecisionRecoveryResponse,
    )
    async def recover_human_decision_handoff(
        session_id: str,
        request: HumanDecisionRecoveryRequest,
    ) -> HumanDecisionRecoveryResponse:
        recover = getattr(manager, "recover_human_decision_handoff", None)
        if recover is None:
            raise HTTPException(
                status_code=409,
                detail="Human decision recovery is not supported",
            )
        try:
            result = await _maybe_await(
                recover(session_id, request.model_dump(mode="json"))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return HumanDecisionRecoveryResponse.model_validate(result)

    @app.get("/api/sessions/{session_id}/stream")
    async def session_stream(
        session_id: str,
        after_sequence: int = 0,
    ) -> StreamingResponse:
        if store.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return StreamingResponse(
            iter_sse(
                store,
                bus,
                session_id,
                after_sequence,
                heartbeat_seconds=sse_heartbeat_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _raise_navigation_http_error(exc: ValueError | FileNotFoundError) -> None:
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail=str(exc)) from exc
