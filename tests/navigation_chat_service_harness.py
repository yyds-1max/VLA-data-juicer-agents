from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from agentscope.app.storage import SessionConfig, SessionRecord
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit


class ChatServiceStorage:
    """Small in-memory implementation of the ChatService storage protocol."""

    def __init__(self) -> None:
        self.agents: dict[str, Any] = {}
        self.sessions: dict[tuple[str, str, str], SessionRecord] = {}
        self.messages: dict[tuple[str, str, str], Any] = {}
        self.updated_state: AgentState | None = None

    async def upsert_credential(self, _user_id: str, credential: Any) -> str:
        return credential.id

    async def upsert_agent(self, _user_id: str, record: Any) -> str:
        self.agents[record.id] = record
        return record.id

    async def get_agent(self, _user_id: str, agent_id: str) -> Any | None:
        return self.agents.get(agent_id)

    async def upsert_session(
        self,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        *,
        session_id: str | None = None,
    ) -> SessionRecord:
        record = SessionRecord(
            id=session_id or "chat-service-session",
            user_id=user_id,
            agent_id=agent_id,
            config=config,
            state=AgentState(),
        )
        self.sessions[(user_id, agent_id, record.id)] = record
        return record

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> SessionRecord | None:
        record = self.sessions.get((user_id, agent_id, session_id))
        if record is None:
            return None
        state = AgentState.model_validate(record.state.model_dump(mode="json"))
        return record.model_copy(update={"state": state})

    async def upsert_message(self, user_id: str, session_id: str, message: Any) -> str:
        self.messages[(user_id, session_id, message.id)] = message
        return message.id

    async def get_message(
        self,
        user_id: str,
        session_id: str,
        message_id: str,
    ) -> Any | None:
        return self.messages.get((user_id, session_id, message_id))

    async def update_session_state(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        key = (user_id, agent_id, session_id)
        persisted = AgentState.model_validate(state.model_dump(mode="json"))
        self.sessions[key] = self.sessions[key].model_copy(
            update={"state": persisted},
        )
        self.updated_state = AgentState.model_validate(
            persisted.model_dump(mode="json"),
        )


class ChatServiceWorkspaceManager:
    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str,
    ) -> Any:
        return SimpleNamespace(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            workspace_id=workspace_id,
        )


class ChatServiceBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def session_run(self, _session_id: str):
        yield

    async def session_publish_event(
        self,
        _session_id: str,
        event: dict[str, Any],
    ) -> None:
        self.events.append(event)

    async def inbox_drain(
        self,
        _session_id: str,
        *,
        max_count: int,
    ) -> list[Any]:
        del max_count
        return []


class InertManager:
    """Marker dependency unused because the AgentScope assembly seam is patched."""


def generic_toolkit() -> Toolkit:
    def ok() -> dict[str, bool]:
        return {"ok": True}

    return Toolkit(
        tools=[
            FunctionTool(ok, name="bash", is_read_only=True),
            FunctionTool(ok, name="read", is_read_only=True),
            FunctionTool(ok, name="task", is_read_only=True),
        ],
    )
