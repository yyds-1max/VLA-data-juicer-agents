from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


SessionStatus = Literal["draft", "active", "historical"]
MessageRole = Literal["user", "assistant", "system"]


def generate_session_title(message: str, *, limit: int = 30) -> str:
    normalized = " ".join(str(message).split())
    return normalized[:limit] if normalized else "未命名任务"


class SessionRecord(BaseModel):
    id: str
    title: str
    status: SessionStatus
    created_at: str
    updated_at: str


class ChatMessageRecord(BaseModel):
    id: str
    session_id: str
    role: MessageRole
    content: str
    created_at: str


class SessionDetail(SessionRecord):
    messages: list[ChatMessageRecord] = Field(default_factory=list)


class CreateSessionResponse(BaseModel):
    session: SessionRecord


class CreateTurnRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_must_not_be_empty(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be empty")
        return message


class CreateTurnResponse(BaseModel):
    turn_id: str


class InterruptResponse(BaseModel):
    interrupted: bool


class AgentEvent(BaseModel):
    type: str
    source: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
