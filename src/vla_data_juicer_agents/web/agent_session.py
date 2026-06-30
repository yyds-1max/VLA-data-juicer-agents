from __future__ import annotations

from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import WebSessionStore


class AgentScopeWebSessionManager:
    def __init__(self, *, store: WebSessionStore, runtime: Any) -> None:
        self._store = store
        self._runtime = runtime

    async def create_session(self, first_message: str) -> SessionRecord:
        return self._store.create_session(title=generate_session_title(first_message))

    async def submit_turn(self, session_id: str, message: str) -> str:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        turn_id = await self._runtime.submit_user_message(web_session_id=session_id, message=message)
        self._store.append_message(session_id, role="user", content=message)
        if isinstance(turn_id, str):
            return turn_id
        return f"turn_{uuid4().hex}"

    async def interrupt(self, session_id: str) -> bool:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        interrupt_web_session = getattr(self._runtime, "interrupt_web_session", None)
        if interrupt_web_session is None:
            return False
        return bool(await interrupt_web_session(web_session_id=session_id))
