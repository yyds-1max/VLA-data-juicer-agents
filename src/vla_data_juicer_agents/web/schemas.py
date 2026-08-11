from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


SessionStatus = Literal["draft", "active", "historical"]
MessageRole = Literal["user", "assistant", "system"]
HumanDecisionAction = Literal["confirm", "stop", "guide"]
TurnOrigin = Literal["user", "system", "interaction"]
TurnStatus = Literal["running", "waiting", "completed", "failed", "interrupted"]
_MAX_REQUEST_CONTEXT_BYTES = 3_000


def generate_session_title(message: str, *, limit: int = 30) -> str:
    normalized = " ".join(str(message).split())
    return normalized[:limit] if normalized else "未命名任务"


class SessionRecord(BaseModel):
    id: str
    title: str
    status: SessionStatus
    created_at: str
    updated_at: str
    contract_version: Literal[1] = 1

    @model_serializer(mode="wrap")
    def serialize_by_contract(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for event in data.get("events", []):
            if not isinstance(event, dict):
                continue
            event["contract_version"] = 1
            event.pop("source", None)
            event.pop("run_id", None)
            event.pop("parent_run_id", None)
        return data


class ChatMessageRecord(BaseModel):
    id: str
    session_id: str
    role: MessageRole
    content: str
    created_at: str
    turn_id: str | None = None


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
    turn_id: str | None = None


class TurnRecord(BaseModel):
    id: str
    web_session_id: str
    origin: TurnOrigin
    status: TurnStatus
    started_at: str
    finished_at: str | None = None
    final_message_id: str | None = None


class SessionDetail(SessionRecord):
    messages: list[ChatMessageRecord] = Field(default_factory=list)
    events: list[TimelineEventRecord] = Field(default_factory=list)
    turns: list[TurnRecord] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    pending_interaction: dict[str, Any] | None = None
    snapshot_seq: int = Field(default=0, ge=0)


class CreateSessionResponse(BaseModel):
    session: SessionRecord


class AllClipsSelectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["all_clips"]


class SelectedClipsSelectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["selected_clips"]
    clips: list[str] = Field(min_length=1, max_length=200)

    @field_validator("clips")
    @classmethod
    def clips_must_be_safe_unique_components(cls, value: list[str]) -> list[str]:
        normalized = [clip.strip() for clip in value]
        if any(
            not clip
            or clip in {".", ".."}
            or "/" in clip
            or "\\" in clip
            or "\r" in clip
            or "\n" in clip
            or len(clip) > 200
            for clip in normalized
        ):
            raise ValueError("clips must be safe non-empty path components")
        if len(set(normalized)) != len(normalized):
            raise ValueError("clips must be unique")
        return normalized


NavigationClipSelectionV1 = AllClipsSelectionV1 | SelectedClipsSelectionV1


class NavigationDatasetSelectionContextV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["navigation_dataset_selection_v1"]
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    selection: NavigationClipSelectionV1 = Field(discriminator="kind")

    @model_validator(mode="after")
    def must_fit_router_envelope_budget(self) -> "NavigationDatasetSelectionContextV1":
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_CONTEXT_BYTES:
            raise ValueError("request_context is too large for the Router context budget")
        return self


class CreateSessionRequest(BaseModel):
    message: str
    entrypoint: Literal[
        "chat",
        "data_management_shortcut",
        "annotation_processing_shortcut",
    ] = "chat"
    request_context: NavigationDatasetSelectionContextV1 | None = None

    @field_validator("message")
    @classmethod
    def message_must_not_be_empty(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be empty")
        return message

    @model_validator(mode="after")
    def shortcut_context_matches_entrypoint(self) -> "CreateSessionRequest":
        if (
            self.entrypoint
            in {"data_management_shortcut", "annotation_processing_shortcut"}
            and self.request_context is None
        ):
            raise ValueError(f"{self.entrypoint} requires request_context")
        if self.entrypoint == "chat" and self.request_context is not None:
            raise ValueError("request_context is only accepted for a trusted shortcut")
        return self


class CreateDatasetReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_scope_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str | None = Field(default=None, max_length=1000)


class CreateTurnRequest(CreateSessionRequest):
    entrypoint: Literal["chat"] = "chat"
    invocation_id: str | None = Field(default=None, max_length=200)

    @field_validator("invocation_id")
    @classmethod
    def invocation_id_must_not_be_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        invocation_id = value.strip()
        if not invocation_id:
            raise ValueError("invocation_id must not be empty")
        return invocation_id


class CreateTurnResponse(BaseModel):
    turn_id: str


class InterruptResponse(BaseModel):
    interrupted: bool


class InteractionResponseRequest(BaseModel):
    option_id: str | None = Field(default=None, min_length=1, max_length=200)
    option_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    interaction_revision: int = Field(ge=1)
    expected_task_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("option_ids")
    @classmethod
    def option_ids_must_be_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("option_ids must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def validate_selection(self) -> "InteractionResponseRequest":
        if (self.option_id is None) == (self.option_ids is None):
            raise ValueError("provide exactly one of option_id or option_ids")
        return self


class InteractionResponse(BaseModel):
    accepted: bool
    turn_id: str | None = None
    session: SessionDetail | None = None


class HumanDecisionRequest(BaseModel):
    action: HumanDecisionAction
    request_id: str = Field(max_length=512)
    plan_id: str | None = Field(default=None, max_length=512)
    step_id: str | None = Field(default=None, max_length=512)
    tool_call_id: str = Field(max_length=512)
    reply_id: str = Field(max_length=512)
    text: str | None = Field(default=None, max_length=4000, validate_default=True)

    @field_validator("text")
    @classmethod
    def guide_text_must_not_be_empty(cls, value: str | None, info: Any) -> str | None:
        if info.data.get("action") == "guide" and (value is None or not value.strip()):
            raise ValueError("text must not be empty when action is guide")
        return value


class HumanDecisionResponse(BaseModel):
    accepted: bool


class HumanDecisionRecoveryRequest(BaseModel):
    action: Literal["quarantine_and_replan"]
    plan_id: str = Field(max_length=512)
    step_id: str = Field(max_length=512)
    reason: str = Field(min_length=1, max_length=4000)


class HumanDecisionRecoveryResponse(BaseModel):
    recovered: Literal[True]
    plan_id: str
    step_id: str
    handoff_status: Literal["quarantined"]
    task_status: Literal["needs_replan"]
    next_action: Literal["submit_complete_plan"]


class AgentEvent(BaseModel):
    type: str
    source: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    timestamp: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
