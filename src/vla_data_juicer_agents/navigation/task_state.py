from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from vla_data_juicer_agents.navigation.models import _validate_date


TASK_SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NavigationTaskPhase(StrEnum):
    INTAKE = "intake"
    EXTRACT_SYNC = "extract_sync"
    WAITING_SCENE_MODE = "waiting_scene_mode"
    FINISH_PROCESSING = "finish_processing"
    COMPLETED = "completed"


class NavigationTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_RECONCILE = "needs_reconcile"
    NEEDS_RERUN = "needs_rerun"
    NEEDS_REPLAN = "needs_replan"
    SUPERSEDED = "superseded"


class NavigationTaskDrift(BaseModel):
    type: Literal[
        "missing_expected_artifact",
        "unexpected_existing_artifact",
        "partial_artifact",
        "manual_external_change",
    ]
    message: str
    evidence: list[str] = Field(default_factory=list)


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
    date: str
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"] | None = None
    dry_run: bool = False
    guidance_revision: int = 0
    state_revision: int = Field(default=0, ge=0)
    phase: NavigationTaskPhase = NavigationTaskPhase.INTAKE
    status: NavigationTaskStatus = NavigationTaskStatus.PENDING
    waiting_reason: str | None = None
    next_required_input: str | None = None
    created_by_web_session_id: str | None = None
    latest_web_session_id: str | None = None
    agentscope_session_id: str | None = None
    latest_run_id: str | None = None
    last_completed_step: str | None = None
    artifact_snapshot: NavigationArtifactSnapshot | None = None
    drift: NavigationTaskDrift | None = None
    schema_version: int = TASK_SCHEMA_VERSION
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)
