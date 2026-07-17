from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import TurnSubmission, WebSessionStore


ControllerFactory = Callable[..., Any]


class WebSessionManager:
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

    def create_session(self, first_message: str) -> SessionRecord:
        with self._lock:
            session = self._store.create_session(title=generate_session_title(first_message))
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
        invocation_id: str | None = None,
    ) -> TurnSubmission:
        with self._lock:
            submission = self._store.begin_user_turn(
                session_id,
                message,
                invocation_id=invocation_id,
            )
            if not submission.created:
                return submission
            try:
                controller = self.get_controller(session_id)
                controller.submit_turn(message)
            except Exception:
                self._store.abort_initial_turn(submission.turn.id)
                raise
            return submission

    def interrupt(self, session_id: str) -> bool:
        with self._lock:
            interrupted = self.get_controller(session_id).request_interrupt()
            if interrupted:
                self._store.interrupt_active_turn(session_id)
            return interrupted

    def mark_historical(self, session_id: str) -> None:
        with self._lock:
            self._store.mark_historical(session_id)
            self._controllers.pop(session_id, None)
