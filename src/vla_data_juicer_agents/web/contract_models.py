from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from vla_data_juicer_agents.web.schemas import ChatMessageRecord, TimelineEventRecord


AgentRole = Literal["router", "navigation"]
AuthorityProducer = Literal["router", "navigation", "system_controller"]
InteractionStatus = Literal["open", "resolved", "expired", "cancelled"]
OutboxStatus = Literal["pending", "claimed", "completed", "failed"]


class ContractConflictError(RuntimeError):
    """A durable contract precondition did not match current state."""

    def __init__(self, code: str, message: str, *, current: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.current = current


@dataclass(frozen=True)
class ConversationAgentSession:
    id: str
    web_session_id: str
    agent_role: AgentRole
    agent_id: str
    agentscope_session_id: str
    task_id: str | None
    event_cursor: str | None
    active_turn_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskBinding:
    task_id: str
    web_session_id: str
    task_ref: str
    navigation_session_id: str
    domain: str
    status: str
    slot_state: Literal["open", "closed"]
    state_revision: int
    scope: dict[str, Any]
    latest_public_update: str | None
    created_at: str
    updated_at: str
    terminal_at: str | None


@dataclass(frozen=True)
class TaskFocus:
    web_session_id: str
    task_id: str
    generation: int
    updated_at: str


@dataclass(frozen=True)
class TaskBindingCreation:
    binding: TaskBinding
    focus: TaskFocus
    navigation_session: ConversationAgentSession
    outbox: RuntimeOutboxItem
    created: bool


@dataclass(frozen=True)
class TurnRun:
    run_id: str
    turn_id: str
    task_id: str | None
    producer: str
    parent_run_id: str | None
    agentscope_session_id: str | None
    status: str
    created_at: str
    updated_at: str
    finished_at: str | None


@dataclass(frozen=True)
class ResponseAuthority:
    turn_id: str
    producer: str
    generation: int
    lease_state: Literal["open", "closed"]
    final_message_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuthorizedFinalCommit:
    message: ChatMessageRecord
    events: tuple[TimelineEventRecord, ...]
    terminal_status: Literal["completed", "failed"]


@dataclass(frozen=True)
class InteractionRecord:
    interaction_id: str
    web_session_id: str
    task_id: str
    task_ref: str
    origin_turn_id: str | None
    kind: str
    blocking: bool
    risk: str
    title: str
    summary: str | None
    options: tuple[dict[str, Any], ...]
    private_payload: dict[str, Any]
    status: InteractionStatus
    revision: int
    expected_task_revision: int
    expires_at: str | None
    response: dict[str, Any] | None
    idempotency_key: str | None
    created_at: str
    updated_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class InteractionConsumption:
    interaction: InteractionRecord
    created: bool


@dataclass(frozen=True)
class RuntimeOutboxItem:
    outbox_id: str
    kind: str
    aggregate_type: str
    aggregate_id: str
    web_session_id: str | None
    task_id: str | None
    turn_id: str | None
    payload: dict[str, Any]
    status: OutboxStatus
    idempotency_key: str
    available_at: str
    claimed_by: str | None
    lease_expires_at: str | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class ResourceLease:
    lease_id: str
    resource_key: str
    owner_id: str
    task_id: str | None
    run_id: str | None
    kind: str
    expires_at: str
    created_at: str
    updated_at: str
