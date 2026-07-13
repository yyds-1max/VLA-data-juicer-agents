from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from vla_data_juicer_agents.navigation.models import _validate_date


TASK_SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NavigationTaskStatus(StrEnum):
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REPLAN = "needs_replan"
    SUPERSEDED = "superseded"


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
    accepted_plan_phase: Literal["extract_sync", "finish_processing"] | None = None
    created_by_web_session_id: str | None = None
    agentscope_session_id: str | None = None
    schema_version: int = TASK_SCHEMA_VERSION
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


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
