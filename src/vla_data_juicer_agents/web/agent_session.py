from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import (
    InterruptResponse,
    PublicEventRecord,
    SessionRecord,
    generate_session_title,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore

EventCallback = Callable[[str, PublicEventRecord], Any]


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
        set_transport = getattr(self._runtime, "set_web_transport", None)
        if callable(set_transport):
            set_transport(self._store, self._event_callback)
        else:
            set_store = getattr(self._runtime, "set_web_session_store", None)
            if callable(set_store):
                set_store(self._store)

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

    async def interrupt(self, session_id: str) -> InterruptResponse:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        interrupt_web_session = getattr(self._runtime, "interrupt_web_session", None)
        if interrupt_web_session is None:
            return InterruptResponse(interrupted=False)
        result = await interrupt_web_session(web_session_id=session_id)
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

    async def delete_session(self, session_id: str) -> None:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        deleted = await self._runtime.delete_web_session(session_id)
        if not deleted:
            raise RuntimeError("AgentScope Web session deletion failed")
        self._store.delete_session(session_id)

    async def submit_human_decision(self, session_id: str, decision: dict[str, Any]) -> bool:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        submit_decision = getattr(self._runtime, "submit_human_decision", None)
        if submit_decision is None:
            return False
        return bool(await submit_decision(web_session_id=session_id, decision=decision))

    async def recover_human_decision_handoff(
        self,
        session_id: str,
        recovery: dict[str, Any],
    ) -> dict[str, Any]:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        recover = getattr(self._runtime, "recover_human_decision_handoff", None)
        if recover is None:
            raise RuntimeError("Human decision recovery is not supported")
        result = await recover(web_session_id=session_id, recovery=recovery)
        return dict(result)
