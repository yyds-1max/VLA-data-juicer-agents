"""Explicit legacy Web adapters used by tests only.

The production application factory always requires an AgentScope runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.web.app import _create_app_for_manager
from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import WebSessionStore

logger = logging.getLogger(__name__)
ControllerFactory = Callable[..., Any]


class WebSessionManager:
    """Legacy controller manager retained only for isolated test coverage."""

    def __init__(
        self,
        *,
        store: WebSessionStore,
        working_dir: str = "./.djx",
        model: str | None = None,
        controller_factory: ControllerFactory = SessionController,
    ) -> None:
        self._store = store
        self._working_dir = Path(working_dir)
        self._model = model
        self._controller_factory = controller_factory
        self._controllers: dict[str, Any] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        first_message: str,
        *,
        creation_id: str | None = None,
    ) -> SessionRecord:
        with self._lock:
            session = self._store.create_session(
                title=generate_session_title(first_message),
                creation_id=creation_id,
            )
            try:
                controller = self._controller_factory(
                    working_dir=str(self._working_dir / session.id),
                    model=self._model,
                )
                controller.start()
            except Exception:
                self._store.delete_session(session.id)
                raise
            self._controllers[session.id] = controller
            return session

    def get_controller(self, session_id: str) -> Any:
        with self._lock:
            return self._controllers[session_id]

    def submit_turn(
        self,
        session_id: str,
        message: str,
        *,
        message_id: str | None = None,
    ) -> str:
        with self._lock:
            controller = self.get_controller(session_id)
            controller.submit_turn(message)
            self._store.append_message(
                session_id,
                role="user",
                content=message,
                message_id=message_id,
            )
            return f"turn_{uuid4().hex}"

    def interrupt(self, session_id: str) -> bool:
        with self._lock:
            return self.get_controller(session_id).request_interrupt()

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._store.delete_session(session_id)
            self._controllers.pop(session_id, None)


def create_legacy_test_app(
    working_dir: str | None = None,
    model: str | None = None,
    db_path: str | Path | None = None,
    controller_factory: ControllerFactory | None = None,
    frontend_dist: str | Path | None = None,
    sse_heartbeat_seconds: float = 15.0,
) -> FastAPI:
    """Build the pre-AgentScope controller adapter for isolated API tests."""
    if controller_factory is None:
        raise RuntimeError("legacy test app requires an explicit controller_factory")
    if working_dir is None:
        working_dir = os.environ.get("VLA_DATA_AGENT_WEB_WORKING_DIR", "./.djx")
    if model is None:
        model = os.environ.get("VLA_DATA_AGENT_WEB_MODEL") or None
    if frontend_dist is None:
        frontend_dist = os.environ.get("VLA_DATA_AGENT_WEB_FRONTEND_DIST") or None

    return _create_app_for_manager(
        working_dir=working_dir,
        db_path=db_path,
        frontend_dist=frontend_dist,
        agentscope_runtime=None,
        sse_heartbeat_seconds=sse_heartbeat_seconds,
        manager_builder=lambda store, _publish: WebSessionManager(
            store=store,
            working_dir=working_dir,
            model=model,
            controller_factory=controller_factory,
        ),
        turn_submitted=_schedule_controller_drain,
    )


def _schedule_controller_drain(
    session_id: str,
    manager: WebSessionManager,
    store: WebSessionStore,
    bus: SessionEventBus,
) -> None:
    _create_logged_task(
        _drain_controller_events(session_id, manager, store, bus),
        name=f"controller-events:{session_id}",
    )


def _create_logged_task(coroutine: Any, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coroutine, name=name)
    task.add_done_callback(_log_background_task_failure)
    return task


def _log_background_task_failure(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Background task failed: %s", task.get_name())


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
            record = store.append_public_event(
                session_id,
                hashlib.sha256(uuid4().bytes).hexdigest(),
                event,
            )
            try:
                await bus.publish(session_id, record)
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "Live controller event publish failed; persisted replay remains "
                    "available: session_id=%s sequence=%s",
                    session_id,
                    record.sequence,
                    exc_info=True,
                )
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
