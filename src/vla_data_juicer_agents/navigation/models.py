import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


DATE_RE = r"^[0-9]{8}$"


def _validate_date(value: str) -> str:
    if not re.match(DATE_RE, value):
        raise ValueError("date must use YYYYMMDD format")
    return value


class NavigationRequest(BaseModel):
    date: str
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"] | None = None
    dry_run: bool = False

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class TopicInfo(BaseModel):
    name: str
    type: str | None = None
    message_count: int = 0


class SegmentInspection(BaseModel):
    name: str
    path: Path
    metadata_path: Path | None = None
    topics: list[TopicInfo] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RawDateInspection(BaseModel):
    date: str
    path: Path
    exists: bool
    segments: list[SegmentInspection] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class ProfileClassification(BaseModel):
    profile_name: str | None
    confidence: float
    matched_topics: list[str] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class WorkflowStep(BaseModel):
    step_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    human_blocking: bool = False
    failure_behavior: str = "stop"


class WorkflowPlan(BaseModel):
    date: str
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"]
    dataset_profile: str
    steps: list[WorkflowStep]

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class CommandRecord(BaseModel):
    command: list[str]
    cwd: Path | None = None
    dry_run: bool = False
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class ToolResult(BaseModel):
    ok: bool
    tool_name: str
    message: str
    produced_paths: list[Path] = Field(default_factory=list)
    commands: list[CommandRecord] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class RunStatus(BaseModel):
    status: Literal["planned", "running", "failed", "completed"]
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
