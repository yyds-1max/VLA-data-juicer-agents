from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import WebSessionStore


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

    def create_session(self, first_message: str) -> SessionRecord:
        session = self._store.create_session(title=generate_session_title(first_message))
        controller = self._controller_factory(
            working_dir=str(self._working_dir / session.id),
            model=self._model,
        )
        controller.start()
        self._controllers[session.id] = controller
        return session

    def get_controller(self, session_id: str) -> Any:
        return self._controllers[session_id]

    def submit_turn(self, session_id: str, message: str) -> str:
        controller = self.get_controller(session_id)
        self._store.append_message(session_id, role="user", content=message)
        controller.submit_turn(message)
        return f"turn_{uuid4().hex}"

    def interrupt(self, session_id: str) -> bool:
        return self.get_controller(session_id).request_interrupt()

    def mark_historical(self, session_id: str) -> None:
        self._store.mark_historical(session_id)
        self._controllers.pop(session_id, None)
