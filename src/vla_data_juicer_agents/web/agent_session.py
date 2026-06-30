from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import WebSessionStore

EventCallback = Callable[[str, dict[str, Any]], None]


class AgentScopeWebSessionManager:
    def __init__(
        self,
        *,
        store: WebSessionStore,
        runtime: Any,
        event_callback: EventCallback | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._event_callback = event_callback

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

    async def forward_events_until_idle(self, session_id: str) -> None:
        subscribe_events = getattr(self._runtime, "subscribe_web_session_events", None)
        if subscribe_events is None:
            return

        persisted_final_texts: set[str] = set()
        async for event in subscribe_events(web_session_id=session_id):
            if self._event_callback is not None:
                self._event_callback(session_id, event)
            text = _final_event_text(event)
            if text is not None and text not in persisted_final_texts:
                self._store.append_message(session_id, role="assistant", content=text)
                persisted_final_texts.add(text)


def _final_event_text(event: dict[str, Any]) -> str | None:
    if event.get("type") != "final":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    return text if isinstance(text, str) and text else None
