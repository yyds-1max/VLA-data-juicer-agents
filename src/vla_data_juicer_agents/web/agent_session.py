from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import inspect
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import (
    InterruptResponse,
    PublicEventRecord,
    SessionRecord,
    generate_session_title,
)
from vla_data_juicer_agents.web.session_store import WebSessionStore


class TurnSubmissionPending(RuntimeError):
    """The same exact client request is still owned by a live submitter."""

    code = "turn_submission_pending"


class TurnSessionBusy(RuntimeError):
    """A different exact turn still owns the session execution slot."""

    code = "turn_session_busy"


@dataclass(frozen=True)
class TurnSubmissionResult:
    turn_id: str
    replayed: bool
    terminal: bool

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

    async def create_session(
        self,
        first_message: str,
        *,
        creation_id: str | None = None,
    ) -> SessionRecord:
        return self._store.create_session(
            title=generate_session_title(first_message),
            creation_id=creation_id,
        )

    async def submit_turn(
        self,
        session_id: str,
        message: str,
        *,
        message_id: str | None = None,
    ) -> str | TurnSubmissionResult:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)

        runtime_id = str(getattr(self._runtime, "runtime_id", "web-session-manager"))
        admission_ttl = float(
            getattr(self._runtime, "admission_lease_ttl_seconds", 30.0)
        )
        admission_renew_interval = float(
            getattr(self._runtime, "admission_renew_interval_seconds", 5.0)
        )
        durable_turn_id = (
            "turn_"
            + hashlib.sha256(f"{session_id}:{message_id}".encode("utf-8")).hexdigest()[:32]
            if message_id is not None
            else None
        )
        if message_id is not None:
            try:
                claim_status = self._store.claim_user_message(
                    session_id,
                    message_id,
                    message,
                    runtime_id=runtime_id,
                    turn_id=durable_turn_id,
                    ttl_seconds=admission_ttl,
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(str(exc)) from exc
            if claim_status == "orphaned":
                # Lease expiry is only evidence that heartbeats are currently
                # unavailable.  A paused process or detached worker can still
                # be producing side effects, so expiry alone must never write
                # a terminal or release the session execution fence.
                claim_status = "admitted"
            if claim_status == "admitted":
                replay_turn_id = self._store.user_message_turn_id(
                    session_id,
                    message_id,
                )
                if replay_turn_id is None:
                    raise RuntimeError("admitted turn identity is unavailable")
                return TurnSubmissionResult(
                    turn_id=replay_turn_id,
                    replayed=True,
                    terminal=(
                        self._store.user_message_turn_status(session_id, message_id)
                        == "terminal"
                    ),
                )
            if claim_status == "pending":
                raise TurnSubmissionPending("turn submission is still pending")
            if claim_status == "busy":
                raise TurnSessionBusy("another turn is still active for this session")
        message_committed = False

        def commit_message() -> None:
            nonlocal message_committed
            if message_id is None or message_committed:
                return
            self._store.commit_user_message(
                session_id,
                message_id,
                message,
                runtime_id=runtime_id,
                ttl_seconds=admission_ttl,
            )
            message_committed = True

        async def heartbeat_message_admission() -> None:
            while True:
                await asyncio.sleep(admission_renew_interval)
                self._store.renew_user_message(
                    session_id,
                    message_id,
                    runtime_id=runtime_id,
                    ttl_seconds=admission_ttl,
                )

        heartbeat_task = (
            asyncio.create_task(
                heartbeat_message_admission(),
                name=f"datapilot-message-admission:{message_id}",
            )
            if message_id is not None
            else None
        )

        try:
            turn_id = await self._runtime.submit_user_message(
                web_session_id=session_id,
                message=message,
                message_id=message_id,
                turn_id=durable_turn_id,
                on_admitted=commit_message if message_id is not None else None,
            )
        except BaseException:
            if message_id is not None:
                self._store.release_user_message(
                    session_id,
                    message_id,
                    message,
                    runtime_id=runtime_id,
                )
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        if message_id is None:
            self._store.append_message(session_id, role="user", content=message)
        elif not message_committed:
            commit_message()
        if message_id is not None:
            return TurnSubmissionResult(
                turn_id=durable_turn_id,
                replayed=False,
                terminal=False,
            )
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
