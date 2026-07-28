from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vla_data_juicer_agents.navigation.models import _validate_date


TASK_SCHEMA_VERSION = 4


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NavigationTaskStatus(StrEnum):
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REPLAN = "needs_replan"
    SUPERSEDED = "superseded"


TERMINAL_NAVIGATION_TASK_STATUSES = frozenset(
    {
        NavigationTaskStatus.COMPLETED,
        NavigationTaskStatus.FAILED,
        NavigationTaskStatus.CANCELLED,
        NavigationTaskStatus.SUPERSEDED,
    }
)


# This is the single authority for durable Navigation Task state changes.  It is
# intentionally stricter than the UI action list: recovery code and model tools
# must obey the same state machine as interactive requests.
NAVIGATION_TASK_STATUS_TRANSITIONS: dict[
    NavigationTaskStatus,
    frozenset[NavigationTaskStatus],
] = {
    NavigationTaskStatus.ACTIVE: frozenset(
        {
            NavigationTaskStatus.WAITING_USER,
            NavigationTaskStatus.PAUSING,
            NavigationTaskStatus.CANCELLING,
            NavigationTaskStatus.COMPLETED,
            NavigationTaskStatus.FAILED,
            NavigationTaskStatus.NEEDS_REPLAN,
        }
    ),
    NavigationTaskStatus.WAITING_USER: frozenset(
        {
            NavigationTaskStatus.ACTIVE,
            NavigationTaskStatus.CANCELLING,
            NavigationTaskStatus.COMPLETED,
        }
    ),
    NavigationTaskStatus.PAUSING: frozenset(
        {
            NavigationTaskStatus.PAUSED,
            NavigationTaskStatus.CANCELLING,
            NavigationTaskStatus.FAILED,
        }
    ),
    NavigationTaskStatus.PAUSED: frozenset(
        {
            NavigationTaskStatus.ACTIVE,
            NavigationTaskStatus.CANCELLING,
        }
    ),
    NavigationTaskStatus.CANCELLING: frozenset(
        {
            NavigationTaskStatus.CANCELLED,
            NavigationTaskStatus.FAILED,
        }
    ),
    NavigationTaskStatus.NEEDS_REPLAN: frozenset(
        {
            NavigationTaskStatus.ACTIVE,
            NavigationTaskStatus.CANCELLING,
            NavigationTaskStatus.FAILED,
        }
    ),
    NavigationTaskStatus.COMPLETED: frozenset(),
    NavigationTaskStatus.FAILED: frozenset(),
    NavigationTaskStatus.CANCELLED: frozenset(),
    NavigationTaskStatus.SUPERSEDED: frozenset(),
}


class NavigationTaskTransitionError(RuntimeError):
    def __init__(
        self,
        current: NavigationTaskStatus,
        target: NavigationTaskStatus,
    ) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"illegal navigation task status transition: {current.value} -> {target.value}"
        )


def validate_navigation_task_status_transition(
    current: NavigationTaskStatus | str,
    target: NavigationTaskStatus | str,
) -> NavigationTaskStatus:
    current_status = NavigationTaskStatus(current)
    target_status = NavigationTaskStatus(target)
    if current_status == target_status:
        return target_status
    if target_status not in NAVIGATION_TASK_STATUS_TRANSITIONS[current_status]:
        raise NavigationTaskTransitionError(current_status, target_status)
    return target_status


class NavigationArtifactSnapshot(BaseModel):
    date: str
    segments: list[str] | None = None
    raw_input_exists: bool = False
    raw_temp_exists: bool = False
    sync_data_exists: bool = False
    sync_data_by_segment: dict[str, bool] = Field(default_factory=dict)
    finish_temp_samples_exists: bool = False
    final_outputs_exist: bool = False
    final_grid_map_exists: bool = False
    sync_image_samples: list[str] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class NavigationTask(BaseModel):
    task_id: str
    request: str = ""
    target: str = ""
    date: str
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"] | None = None
    dry_run: bool = False
    guidance_revision: int = 0
    state_revision: int = Field(default=0, ge=0)
    status: NavigationTaskStatus = NavigationTaskStatus.ACTIVE
    accepted_plan_phase: Literal[
        "extract_sync",
        "finish_processing",
        "trajectory_review",
    ] | None = None
    created_by_web_session_id: str
    agentscope_session_id: str
    schema_version: int = TASK_SCHEMA_VERSION
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)

    @field_validator("created_by_web_session_id", "agentscope_session_id")
    @classmethod
    def validate_session_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("navigation session identity must be non-empty")
        return normalized


class TaskAttemptCreation(BaseModel):
    task: NavigationTask
    created: bool

    @property
    def task_id(self) -> str:
        return self.task.task_id


class NavigationRunningWriter(BaseModel):
    task_id: str
    plan_id: str
    step_id: str
    action: str
    date: str
    segments: list[str] | None = None


class NavigationTaskOutcome(BaseModel):
    """Immutable public-workflow outcome attached outside the core task row."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    requested_outcome: Literal[
        "auto",
        "extract_sync",
        "postprocessing",
        "postprocessing_and_fix",
        "trajectory_fix",
    ] = "auto"
    completion_outcome: Literal[
        "extract_sync_completed",
        "postprocessing_completed_fix_pending",
        "trajectory_review_completed",
        "processing_completed_no_fix",
    ] | None = None
    revision: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class NavigationTaskLineage(BaseModel):
    """Server-owned relationship between a completed task and a linked child."""

    model_config = ConfigDict(extra="forbid")

    parent_task_id: str
    child_task_id: str
    relation: Literal["trajectory_fix"]
    created_at: str = Field(default_factory=utc_now)
