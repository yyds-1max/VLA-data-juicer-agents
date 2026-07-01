from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


SessionStatus = Literal["draft", "active", "historical"]
MessageRole = Literal["user", "assistant", "system"]
HumanDecisionAction = Literal["confirm", "stop", "guide"]


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


class TimelineEventRecord(BaseModel):
    id: str
    session_id: str
    seq: int
    type: str
    source: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class SessionDetail(SessionRecord):
    messages: list[ChatMessageRecord] = Field(default_factory=list)
    events: list[TimelineEventRecord] = Field(default_factory=list)


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


class HumanDecisionRequest(BaseModel):
    action: HumanDecisionAction
    request_id: str
    tool_call_id: str
    reply_id: str
    text: str | None = Field(default=None, validate_default=True)

    @field_validator("text")
    @classmethod
    def guide_text_must_not_be_empty(cls, value: str | None, info: Any) -> str | None:
        if info.data.get("action") == "guide" and (value is None or not value.strip()):
            raise ValueError("text must not be empty when action is guide")
        return value


class HumanDecisionResponse(BaseModel):
    accepted: bool


class AgentEvent(BaseModel):
    type: str
    source: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
