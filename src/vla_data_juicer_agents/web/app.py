from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import weakref
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from vla_data_juicer_agents.annotation.api import create_annotation_router
from vla_data_juicer_agents.annotation.application import AnnotationApplicationService
from vla_data_juicer_agents.annotation.catalog import CalibrationCatalog
from vla_data_juicer_agents.annotation.maintenance import (
    AnnotationMaintenanceLease,
    acquire_annotation_maintenance,
)
from vla_data_juicer_agents.annotation.navigation_gateway import (
    AnnotationNavigationGateway,
)
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationValidationError,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.annotation.worker import AnnotationWorker
from vla_data_juicer_agents.annotation.workflow_coordinator import (
    AnnotationWorkflowCoordinator,
)
from vla_data_juicer_agents.navigation.dataset_catalog import (
    list_sync_images,
    merge_annotation_lifecycle,
    resolve_sync_image_path,
    scan_navigation_dataset,
    scan_navigation_date,
)
from vla_data_juicer_agents.training.api import create_training_router
from vla_data_juicer_agents.training.auth import TrainingSettings
from vla_data_juicer_agents.training.node_deployment import (
    AutomatedNodeDeploymentManager,
)
from vla_data_juicer_agents.training.resources import FakeResourceProvider
from vla_data_juicer_agents.training.service import TrainingService
from vla_data_juicer_agents.training.store import TrainingStore
from vla_data_juicer_agents.training.worker import TrainingWorker
from vla_data_juicer_agents.training.worker_deployment_ssh import (
    OpenSshWorkerDeploymentBackend,
)
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import (
    CreateSessionResponse,
    CreateSessionRequest,
    CreateDatasetReleaseRequest,
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


def _password_worker_deployment_context(
    *, endpoint: Any, host_key: Any, ssh_password: str
):
    """Open one pinned, short-lived SSH password deployment session."""

    return OpenSshWorkerDeploymentBackend.password_session(
        endpoint=endpoint,
        host_key=host_key,
        password=ssh_password,
    )


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
    training_db_path: str | Path | None = None,
    training_tick_seconds: float | None = None,
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
    training_database_path = (
        Path(training_db_path)
        if training_db_path is not None
        else Path(
            os.environ.get("VLA_TRAINING_DB_PATH")
            or Path(working_dir) / "training.sqlite"
        )
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
        from vla_data_juicer_agents.annotation.fix_runtime import (
            CommandLogFixDraftAdapter,
            FixCompatibilityPublisher,
        )

        fix_compatibility_publisher = FixCompatibilityPublisher()
        annotation_worker = AnnotationWorker(
            annotation_store,
            annotation_runtime,
            fix_publisher=fix_compatibility_publisher,
        )
        annotation_service = AnnotationApplicationService(
            store=annotation_store,
            worker=annotation_worker,
            catalog=annotation_catalog or CalibrationCatalog.default(),
            work_root=annotation_work_root,
            clip_data_root=annotation_clip_data_root,
            fix_runtime=CommandLogFixDraftAdapter(),
        )
        training_settings = TrainingSettings.from_env()
        training_store = TrainingStore(training_database_path)
        training_provider = FakeResourceProvider(training_store)
        node_deployment_manager = (
            AutomatedNodeDeploymentManager(
                center_base_url=training_settings.center_base_url,
                backend_factory=_password_worker_deployment_context,
            )
            if training_settings.center_base_url is not None
            else None
        )
        training_service = TrainingService(
            training_store,
            training_provider,
            simulation_enabled=training_settings.simulation_enabled,
            node_deployment_manager=node_deployment_manager,
        )
        training_worker = TrainingWorker(
            training_store,
            tick_seconds=(
                training_tick_seconds
                if training_tick_seconds is not None
                else _training_tick_seconds_from_env()
            ),
        )
        runtime_workspace_root = Path(
            getattr(
                getattr(agentscope_runtime, "config", None),
                "workspace_root",
                working_dir,
            )
        )
        annotation_gateway = AnnotationNavigationGateway(
            service=annotation_service,
            navigation_db_path=runtime_workspace_root
            / "navigation-tasks.sqlite",
        )
        set_annotation_gateway = getattr(
            agentscope_runtime,
            "set_annotation_gateway",
            None,
        )
        annotation_coordinator = (
            AnnotationWorkflowCoordinator(
                service=annotation_service,
                agentscope_runtime=agentscope_runtime,
                navigation_workspace_root=runtime_workspace_root,
            )
            if callable(set_annotation_gateway)
            else None
        )
        if callable(set_annotation_gateway):
            set_annotation_gateway(annotation_gateway)
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
                    training_worker_task = asyncio.create_task(
                        training_worker.run_forever(),
                        name="training-simulation-worker",
                    )
                    annotation_coordinator_task = (
                        asyncio.create_task(
                            annotation_coordinator.run_forever(),
                            name="annotation-workflow-coordinator",
                        )
                        if annotation_coordinator is not None
                        else None
                    )
                    try:
                        yield
                    finally:
                        await training_worker.stop()
                        with suppress(asyncio.CancelledError):
                            await training_worker_task
                        if annotation_coordinator is not None:
                            await annotation_coordinator.stop()
                        if annotation_coordinator_task is not None:
                            with suppress(asyncio.CancelledError):
                                await annotation_coordinator_task
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
        app.state.annotation_gateway = annotation_gateway
        app.state.annotation_workflow_coordinator = annotation_coordinator
        app.state.training_store = training_store
        app.state.training_provider = training_provider
        app.state.training_service = training_service
        app.state.training_worker = training_worker
        app.state.training_settings = training_settings
        app.mount(
            agentscope_runtime.config.agentscope_mount_path,
            agentscope_runtime.app,
        )
        # Extend with concrete routes so every app route retains Starlette's
        # ``path`` contract (some FastAPI versions add a private include marker).
        app.router.routes.extend(
            create_annotation_router(annotation_service).routes,
        )
        app.router.routes.extend(
            create_training_router(
                training_service,
                settings=training_settings,
            ).routes,
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
            if request.url.path.startswith("/api/training"):
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "invalid_training_request",
                            "message": "The training request is invalid.",
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
                summary = scan_navigation_dataset()
                return merge_annotation_lifecycle(
                    summary,
                    annotation_store.asset_lifecycle_snapshot(),
                ).model_dump(mode="json")
            except (ValueError, FileNotFoundError) as exc:
                _raise_navigation_http_error(exc)

        @app.get("/api/navigation/datasets/events/cursor")
        async def navigation_dataset_event_cursor() -> dict[str, int]:
            return {"cursor": store.navigation_dataset_event_cursor()}

        @app.get("/api/navigation/datasets/events")
        async def navigation_dataset_events(
            request: Request,
            after_seq: int = Query(default=0, ge=0),
            last_event_id: str | None = Header(
                default=None,
                alias="Last-Event-ID",
                max_length=32,
            ),
        ) -> StreamingResponse:
            cursor = after_seq
            if last_event_id is not None:
                try:
                    resumed_cursor = int(last_event_id, 10)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail="Last-Event-ID must be a non-negative integer",
                    ) from exc
                if resumed_cursor < 0:
                    raise HTTPException(
                        status_code=400,
                        detail="Last-Event-ID must be a non-negative integer",
                    )
                cursor = max(cursor, resumed_cursor)

            async def stream():
                nonlocal cursor
                loop = asyncio.get_running_loop()
                heartbeat_at = loop.time() + 15.0
                yield "retry: 1000\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    events = await asyncio.to_thread(
                        store.list_navigation_dataset_events_after,
                        after_seq=cursor,
                    )
                    for event in events:
                        cursor = int(event["seq"])
                        payload = json.dumps(
                            event,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        yield (
                            f"id: {cursor}\n"
                            "event: navigation_dataset\n"
                            f"data: {payload}\n\n"
                        )
                    now = loop.time()
                    if now >= heartbeat_at:
                        yield ": keepalive\n\n"
                        heartbeat_at = now + 15.0
                    if not events:
                        await asyncio.sleep(0.25)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.get("/api/navigation/datasets/releases")
        async def navigation_dataset_releases() -> dict[str, Any]:
            try:
                summary = scan_navigation_dataset()
                return {
                    "releases": [
                        annotation_store.dataset_release_candidate(
                            dataset_date=date.date,
                            managed_clips=[
                                {
                                    "source_clip": clip.clip,
                                    "status": clip.status,
                                    "duration_ns": clip.duration_ns,
                                }
                                for clip in date.clips
                            ],
                        )
                        for date in summary.dates
                    ]
                }
            except (ValueError, FileNotFoundError) as exc:
                _raise_navigation_http_error(exc)

        @app.post("/api/navigation/datasets/releases/{date}")
        async def create_navigation_dataset_release(
            date: str,
            request: CreateDatasetReleaseRequest,
            idempotency_key: str = Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=200,
            ),
        ) -> dict[str, Any]:
            try:
                date_summary = scan_navigation_date(date)
                managed_clips = [
                    {
                        "source_clip": clip.clip,
                        "status": clip.status,
                        "duration_ns": clip.duration_ns,
                    }
                    for clip in date_summary.clips
                ]
                return annotation_store.create_dataset_release(
                    dataset_date=date,
                    managed_clips=managed_clips,
                    expected_scope_manifest_sha256=(
                        request.expected_scope_manifest_sha256
                    ),
                    note=request.note,
                    idempotency_key=idempotency_key,
                )
            except AnnotationConflictError as exc:
                detail: dict[str, Any] = {
                    "code": exc.code,
                    "message": str(exc),
                }
                if exc.current is not None:
                    detail["current"] = exc.current
                raise HTTPException(status_code=409, detail=detail) from exc
            except AnnotationValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            except (ValueError, FileNotFoundError) as exc:
                _raise_navigation_http_error(exc)

        @app.get("/api/navigation/datasets/{date}")
        async def navigation_date_summary(date: str) -> dict[str, Any]:
            try:
                summary = scan_navigation_date(date)
                return merge_annotation_lifecycle(
                    summary,
                    annotation_store.asset_lifecycle_snapshot(),
                ).model_dump(mode="json")
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
        async def session_events(
            websocket: WebSocket,
            session_id: str,
            after_seq: int = 0,
        ) -> None:
            await websocket.accept()
            if after_seq < 0:
                await websocket.close(code=1008, reason="after_seq must be non-negative")
                return
            if getattr(manager, "event_bridge", None) is None:
                _create_logged_task(
                    manager.forward_events_until_idle(session_id),
                    name=f"agentscope-events-ws:{session_id}",
                )
            try:
                async with bus.subscribe(session_id) as queue:
                    last_seq = after_seq
                    try:
                        replay = store.list_timeline_events_after(
                            session_id,
                            after_seq=last_seq,
                        )
                    except KeyError:
                        await websocket.close(code=1008, reason="Session not found")
                        return
                    for record in replay:
                        await websocket.send_json(record.model_dump(mode="json"))
                        last_seq = record.seq
                    while True:
                        event = await queue.get()
                        event_seq = event.get("seq")
                        if isinstance(event_seq, int):
                            if event_seq <= last_seq:
                                continue
                            if event_seq > last_seq + 1:
                                for record in store.list_timeline_events_after(
                                    session_id,
                                    after_seq=last_seq,
                                ):
                                    if record.seq >= event_seq:
                                        break
                                    await websocket.send_json(record.model_dump(mode="json"))
                                    last_seq = record.seq
                            if event_seq <= last_seq:
                                continue
                            last_seq = event_seq
                        await websocket.send_json(event)
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
                    @app.get("/data/releases", include_in_schema=False)
                    @app.get("/annotation", include_in_schema=False)
                    @app.get("/annotation/jobs", include_in_schema=False)
                    @app.get("/annotation/jobs/{job_ref}", include_in_schema=False)
                    @app.get(
                        "/annotation/jobs/{job_ref}/segments/{segment_ref}",
                        include_in_schema=False,
                    )
                    @app.get("/annotation/reviews", include_in_schema=False)
                    @app.get(
                        "/annotation/reviews/{review_ref}",
                        include_in_schema=False,
                    )
                    @app.get(
                        "/annotation/verified/{asset_ref}",
                        include_in_schema=False,
                    )
                    @app.get("/model", include_in_schema=False)
                    @app.get(
                        "/model/runs/{training_run_ref}",
                        include_in_schema=False,
                    )
                    @app.get("/simulation", include_in_schema=False)
                    async def frontend_route(
                        job_ref: str | None = None,
                        segment_ref: str | None = None,
                        review_ref: str | None = None,
                        training_run_ref: str | None = None,
                        asset_ref: str | None = None,
                    ) -> FileResponse:
                        del job_ref, segment_ref, review_ref, training_run_ref, asset_ref
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


def _training_tick_seconds_from_env() -> float:
    raw = os.environ.get("VLA_TRAINING_FAKE_TICK_SECONDS", "0.25")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            "VLA_TRAINING_FAKE_TICK_SECONDS must be a positive number"
        ) from exc
    if value <= 0 or value > 60:
        raise RuntimeError(
            "VLA_TRAINING_FAKE_TICK_SECONDS must be between 0 and 60 seconds"
        )
    return value
