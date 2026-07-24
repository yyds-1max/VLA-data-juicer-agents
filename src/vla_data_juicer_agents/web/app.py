from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import weakref
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from vla_data_juicer_agents.annotation.api import create_annotation_router
from vla_data_juicer_agents.annotation.application import AnnotationApplicationService
from vla_data_juicer_agents.annotation.catalog import CalibrationCatalog
from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationMaintenanceLease,
    acquire_annotation_maintenance,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.annotation.worker import AnnotationWorker
from vla_data_juicer_agents.navigation.dataset_catalog import (
    list_sync_images,
    resolve_sync_image_path,
    scan_navigation_dataset,
    scan_navigation_date,
)
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import (
    CreateSessionResponse,
    CreateSessionRequest,
    CreateTurnRequest,
    CreateTurnResponse,
    HumanDecisionRequest,
    HumanDecisionRecoveryRequest,
    HumanDecisionRecoveryResponse,
    HumanDecisionResponse,
    InterruptResponse,
    InteractionResponse,
    InteractionResponseRequest,
)
from vla_data_juicer_agents.web.contract_models import ContractConflictError
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
from vla_data_juicer_agents.web.session_store import WebSessionStore

logger = logging.getLogger(__name__)


def create_app(
    working_dir: str | None = None,
    model: str | None = None,
    db_path: str | Path | None = None,
    frontend_dist: str | Path | None = None,
    agentscope_runtime: Any | None = None,
    annotation_db_path: str | Path | None = None,
    annotation_runtime: Any | None = None,
    annotation_work_root: str | Path | None = None,
    annotation_clip_data_root: str | Path | None = None,
    annotation_catalog: Any | None = None,
) -> FastAPI:
    if agentscope_runtime is None:
        raise RuntimeError(
            "DataPilot Web requires an AgentScope runtime; "
            "the legacy controller runtime is unsupported"
        )
    if working_dir is None:
        working_dir = os.environ.get("VLA_DATA_AGENT_WEB_WORKING_DIR", "./.djx")
    if model is None:
        model = os.environ.get("VLA_DATA_AGENT_WEB_MODEL") or None
    if frontend_dist is None:
        frontend_dist = os.environ.get("VLA_DATA_AGENT_WEB_FRONTEND_DIST") or None

    database_path = (
        Path(db_path)
        if db_path is not None
        else Path(working_dir) / "sessions.sqlite"
    )
    annotation_database_path = (
        Path(annotation_db_path)
        if annotation_db_path is not None
        else Path(working_dir) / "annotation.sqlite"
    )
    annotation_maintenance = acquire_annotation_maintenance(
        annotation_database_path,
        create_parent=True,
        create_lock_file=True,
    )
    annotation_maintenance_holder = {"lease": annotation_maintenance}
    try:
        store = WebSessionStore(database_path)
        annotation_store = AnnotationStore(annotation_database_path)
        annotation_worker = AnnotationWorker(annotation_store, annotation_runtime)
        annotation_service = AnnotationApplicationService(
            store=annotation_store,
            worker=annotation_worker,
            catalog=annotation_catalog or CalibrationCatalog.default(),
            work_root=annotation_work_root,
            clip_data_root=annotation_clip_data_root,
        )
    except BaseException:
        annotation_maintenance.close()
        raise
    bus = SessionEventBus()

    async def publish_session_event(session_id: str, event: dict[str, Any]) -> None:
        await bus.publish(session_id, event)

    try:
        manager = AgentScopeWebSessionManager(
            store=store,
            runtime=agentscope_runtime,
            event_callback=publish_session_event,
        )
    except BaseException:
        annotation_maintenance.close()
        raise

    lifespan_lock = threading.Lock()
    lifespan_started = False

    @asynccontextmanager
    async def lifespan(_parent_app: FastAPI):
        nonlocal lifespan_started
        active_maintenance = annotation_maintenance_holder["lease"]
        with lifespan_lock:
            if lifespan_started or active_maintenance.closed:
                raise RuntimeError(
                    "DataPilot Web application lifespan can only start once",
                )
            lifespan_started = True
        try:
            async with agentscope_runtime.app.router.lifespan_context(
                agentscope_runtime.app,
            ):
                event_bridge = getattr(manager, "event_bridge", None)
                if event_bridge is not None:
                    await event_bridge.start()
                recovery_loop = getattr(
                    agentscope_runtime,
                    "run_agent_wakeup_recovery_loop",
                    None,
                )
                recovery_task = (
                    asyncio.create_task(
                        recovery_loop(),
                        name="agentscope-wakeup-recovery",
                    )
                    if callable(recovery_loop)
                    else None
                )
                try:
                    annotation_worker_task = asyncio.create_task(
                        annotation_worker.run_forever(),
                        name="annotation-worker",
                    )
                    try:
                        yield
                    finally:
                        cleanup_task = asyncio.create_task(
                            _stop_and_drain_annotation_worker(
                                annotation_worker,
                                annotation_worker_task,
                            ),
                            name="annotation-worker-cleanup",
                        )
                        # Runtime cancellation owns SIGTERM→SIGKILL and process
                        # group cleanup. An ASGI shutdown cancellation must not
                        # abandon the asyncio.to_thread call or release the
                        # maintenance lease while that Runtime is still active.
                        await _await_cleanup_before_propagating_cancellation(
                            cleanup_task,
                        )
                finally:
                    if recovery_task is not None:
                        recovery_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await recovery_task
                    if event_bridge is not None:
                        await event_bridge.stop()
        finally:
            active_maintenance.close()

    try:
        app = FastAPI(title="DataPilot Web API", lifespan=lifespan)
        app.state.annotation_maintenance_lease = annotation_maintenance
        app.state.annotation_maintenance_finalizer = weakref.finalize(
            app,
            _close_annotation_maintenance,
            annotation_maintenance_holder,
        )
        app.state.store = store
        app.state.manager = manager
        app.state.bus = bus
        app.state.agentscope_runtime = agentscope_runtime
        app.state.annotation_store = annotation_store
        app.state.annotation_service = annotation_service
        app.state.annotation_worker = annotation_worker
        app.mount(
            agentscope_runtime.config.agentscope_mount_path,
            agentscope_runtime.app,
        )
        # Extend with concrete routes so every app route retains Starlette's
        # ``path`` contract (some FastAPI versions add a private include marker).
        app.router.routes.extend(
            create_annotation_router(annotation_service).routes,
        )

        @app.exception_handler(RequestValidationError)
        async def safe_annotation_validation_error(
            request: Request,
            exc: RequestValidationError,
        ):
            if request.url.path.startswith("/api/annotation"):
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "invalid_annotation_request",
                            "message": "The annotation request is invalid.",
                        }
                    },
                )
            return await request_validation_exception_handler(request, exc)

        @app.post("/api/sessions", response_model=CreateSessionResponse)
        async def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
            session = await _maybe_await(
                manager.create_session(
                    request.message,
                    request.entrypoint,
                    request.request_context.model_dump(mode="json")
                    if request.request_context is not None
                    else None,
                )
            )
            return CreateSessionResponse(session=session)

        @app.get("/api/sessions")
        async def list_sessions() -> dict[str, list[dict[str, Any]]]:
            return {
                "sessions": [
                    session.model_dump() for session in store.list_sessions()
                ]
            }

        @app.get("/api/sessions/{session_id}")
        async def get_session(session_id: str) -> dict[str, dict[str, Any]]:
            get_detail = getattr(manager, "get_session_detail", None)
            session = (
                get_detail(session_id)
                if callable(get_detail)
                else store.get_session(session_id)
            )
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

        @app.get(
            "/api/navigation/datasets/{date}/clips/{clip}/sync-images/"
            "{sequence}/{filename}"
        )
        async def navigation_sync_image_file(
            date: str,
            clip: str,
            sequence: str,
            filename: str,
        ) -> FileResponse:
            try:
                return FileResponse(resolve_sync_image_path(date, clip, sequence, filename))
            except (ValueError, FileNotFoundError) as exc:
                _raise_navigation_http_error(exc)

        @app.post(
            "/api/sessions/{session_id}/turns",
            response_model=CreateTurnResponse,
        )
        async def submit_turn(
            session_id: str,
            request: CreateTurnRequest,
        ) -> CreateTurnResponse:
            try:
                submission = await _maybe_await(
                    manager.submit_turn(session_id, request.message, request.invocation_id)
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="Session not found",
                ) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            turn_id = submission.turn.id
            if submission.created and getattr(manager, "event_bridge", None) is None:
                _create_logged_task(
                    manager.forward_events_until_idle(session_id),
                    name=f"agentscope-events:{session_id}",
                )
            return CreateTurnResponse(turn_id=turn_id)

        @app.post(
            "/api/sessions/{session_id}/interrupt",
            response_model=InterruptResponse,
        )
        async def interrupt(session_id: str) -> InterruptResponse:
            try:
                interrupted = await _maybe_await(manager.interrupt(session_id))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Session not found") from exc
            return InterruptResponse(interrupted=interrupted)

        @app.post(
            "/api/sessions/{session_id}/interactions/{interaction_id}/responses",
            response_model=InteractionResponse,
        )
        async def submit_interaction_response(
            session_id: str,
            interaction_id: str,
            request: InteractionResponseRequest,
        ) -> InteractionResponse:
            submit_response = getattr(manager, "submit_interaction_response", None)
            if submit_response is None:
                raise HTTPException(
                    status_code=409,
                    detail="Structured interactions are not supported",
                )
            try:
                result = await _maybe_await(
                    submit_response(
                        session_id,
                        interaction_id,
                        request.model_dump(exclude_none=True),
                    )
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="Session or interaction not found",
                ) from exc
            except ContractConflictError as exc:
                get_detail = getattr(manager, "get_session_detail", None)
                snapshot = (
                    get_detail(session_id)
                    if callable(get_detail)
                    else store.get_session(session_id)
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": exc.code,
                        "message": str(exc),
                        "session": snapshot.model_dump() if snapshot is not None else None,
                    },
                ) from exc
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if getattr(manager, "event_bridge", None) is None:
                _create_logged_task(
                    manager.forward_events_until_idle(session_id),
                    name=f"agentscope-events:{session_id}",
                )
            return InteractionResponse.model_validate(result)

        @app.post(
            "/api/sessions/{session_id}/human-decisions",
            response_model=HumanDecisionResponse,
        )
        async def submit_human_decision(
            session_id: str,
            request: HumanDecisionRequest,
        ) -> HumanDecisionResponse:
            submit_decision = getattr(manager, "submit_human_decision", None)
            if submit_decision is None:
                raise HTTPException(
                    status_code=409,
                    detail="Human decisions are not supported",
                )
            try:
                accepted = await _maybe_await(
                    submit_decision(session_id, request.model_dump(exclude_none=True))
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Session not found") from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not accepted:
                raise HTTPException(
                    status_code=409,
                    detail="Human decision was not accepted",
                )
            if getattr(manager, "event_bridge", None) is None:
                _create_logged_task(
                    manager.forward_events_until_idle(session_id),
                    name=f"agentscope-events:{session_id}",
                )
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

        @app.websocket("/api/sessions/{session_id}/events")
        async def session_events(websocket: WebSocket, session_id: str) -> None:
            await websocket.accept()
            if getattr(manager, "event_bridge", None) is None:
                _create_logged_task(
                    manager.forward_events_until_idle(session_id),
                    name=f"agentscope-events-ws:{session_id}",
                )
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
                    app.mount(
                        "/assets",
                        StaticFiles(directory=assets_path),
                        name="frontend-assets",
                    )

                brand_path = frontend_path / "brand"
                if brand_path.exists():
                    app.mount(
                        "/brand",
                        StaticFiles(directory=brand_path),
                        name="frontend-brand",
                    )

                index_path = frontend_path / "index.html"
                if index_path.exists():
                    @app.get("/", include_in_schema=False)
                    async def frontend_index() -> FileResponse:
                        return FileResponse(index_path)

                    @app.get("/agent", include_in_schema=False)
                    @app.get("/data", include_in_schema=False)
                    @app.get("/annotation/jobs", include_in_schema=False)
                    @app.get("/annotation/jobs/{job_ref}", include_in_schema=False)
                    @app.get(
                        "/annotation/jobs/{job_ref}/segments/{segment_ref}",
                        include_in_schema=False,
                    )
                    @app.get("/model", include_in_schema=False)
                    @app.get("/simulation", include_in_schema=False)
                    async def frontend_route(
                        job_ref: str | None = None,
                        segment_ref: str | None = None,
                    ) -> FileResponse:
                        del job_ref, segment_ref
                        return FileResponse(index_path)

        return app
    except BaseException:
        annotation_maintenance.close()
        raise


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _stop_and_drain_annotation_worker(
    worker: AnnotationWorker,
    worker_task: asyncio.Task[Any],
) -> None:
    stop_error: BaseException | None = None
    try:
        await worker.stop()
    except BaseException as exc:
        stop_error = exc
    try:
        await worker_task
    except BaseException as drain_error:
        if stop_error is not None:
            raise stop_error from drain_error
        raise
    if stop_error is not None:
        raise stop_error


async def _await_cleanup_before_propagating_cancellation(
    cleanup_task: asyncio.Task[Any],
) -> None:
    pending_cancellation: asyncio.CancelledError | None = None
    cleanup_error: BaseException | None = None
    while True:
        try:
            await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if cleanup_task.cancelled() and (
                current is None or current.cancelling() == 0
            ):
                cleanup_error = exc
                break
            if pending_cancellation is None:
                pending_cancellation = exc
            if cleanup_task.done():
                break
        except BaseException as exc:
            cleanup_error = exc
            break

    if cleanup_error is None:
        try:
            cleanup_task.result()
        except BaseException as exc:
            cleanup_error = exc
    if pending_cancellation is not None:
        if cleanup_error is not None:
            raise pending_cancellation from cleanup_error
        raise pending_cancellation
    if cleanup_error is not None:
        raise cleanup_error


def _create_logged_task(coroutine: Any, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coroutine, name=name)
    task.add_done_callback(_log_background_task_failure)
    return task


def _close_annotation_maintenance(
    holder: dict[str, AnnotationMaintenanceLease],
) -> None:
    holder["lease"].close()


def _log_background_task_failure(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Background task failed: %s", task.get_name())


def _raise_navigation_http_error(exc: ValueError | FileNotFoundError) -> None:
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=400,
            detail="The navigation dataset request is invalid.",
        ) from exc
    raise HTTPException(
        status_code=404,
        detail="The requested navigation dataset resource was not found.",
    ) from exc
