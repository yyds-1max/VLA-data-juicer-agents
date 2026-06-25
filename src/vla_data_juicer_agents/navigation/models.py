import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class StageVariantDecision(BaseModel):
    variant: str
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class PlanIssue(BaseModel):
    type: str
    message: str = ""
    evidence: list[str] = Field(default_factory=list)


class NavigationTopicParams(BaseModel):
    profile_hint: str | None = None
    confidence: float = 0.0
    topic_whitelist: list[str] = Field(default_factory=list)
    topic_map: dict[str, str] = Field(default_factory=dict)
    query_dir: str | None = None
    evidence: list[str] = Field(default_factory=list)
    warnings: list[PlanIssue] = Field(default_factory=list)
    blocking_issues: list[PlanIssue] = Field(default_factory=list)


class NavigationSensorBinding(BaseModel):
    role: Literal["fisheye_front", "lidar", "odom", "ins", "localization"]
    topic: str | None = None
    message_type: str | None = None
    kind: Literal["camera", "lidar", "odom", "ins", "missing"] | None = None
    candidates: list[str] = Field(default_factory=list)


class NavigationSensorBindings(BaseModel):
    fisheye_front: NavigationSensorBinding | None = None
    lidar: NavigationSensorBinding | None = None
    odom: NavigationSensorBinding | None = None
    ins: NavigationSensorBinding | None = None
    localization: NavigationSensorBinding | None = None
    warnings: list[PlanIssue] = Field(default_factory=list)
    blocking_issues: list[PlanIssue] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_binding_roles(self) -> "NavigationSensorBindings":
        for slot in ("fisheye_front", "lidar", "odom", "ins", "localization"):
            binding = getattr(self, slot)
            if binding is not None and binding.role != slot:
                raise ValueError(
                    f"sensor binding role mismatch for {slot}: expected {slot}, got {binding.role}"
                )
        return self


class NavigationLocalizationPolicy(BaseModel):
    source: Literal["odom", "ins", "unknown"]
    conversion: Literal["odom_to_ins", "none"] = "none"


class NavigationGridmapPolicy(BaseModel):
    source: Literal["existing_gridmap", "generated_from_pcd", "projection_ready", "unknown"] = "unknown"


class NavigationCalibrationPolicy(BaseModel):
    mode: Literal[
        "hardcoded_with_user_confirmation",
        "selected_profile",
        "unknown",
    ] = "hardcoded_with_user_confirmation"
    selected_sensor_source: str | None = None
    requires_user_confirmation: bool = True


class NavigationProcessingProfile(BaseModel):
    id: str = "parameterized_navigation_v1"
    platform_hint: str = "unknown"
    sensor_bindings: NavigationSensorBindings | None = None
    topic_params: NavigationTopicParams
    localization_policy: NavigationLocalizationPolicy
    gridmap_policy: NavigationGridmapPolicy = Field(default_factory=NavigationGridmapPolicy)
    calibration_policy: NavigationCalibrationPolicy = Field(default_factory=NavigationCalibrationPolicy)
    stage_variants: dict[str, StageVariantDecision] = Field(default_factory=dict)
    warnings: list[PlanIssue] = Field(default_factory=list)
    blocking_issues: list[PlanIssue] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(default_factory=dict)


class NavigationDataProfile(BaseModel):
    date: str
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"]
    processing_profile: NavigationProcessingProfile | None = None
    platform_hint: str = "unknown"
    sensor_bindings: NavigationSensorBindings | None = None
    localization_policy: NavigationLocalizationPolicy | None = None
    topic_params: NavigationTopicParams | None = None
    gridmap_source: Literal[
        "existing_gridmap",
        "generated_from_pcd",
        "projection_ready",
        "unknown",
    ] = "unknown"
    projection_input_ready: bool = False
    pcd_gridmap_tool_available: bool = True
    stage_variants: dict[str, StageVariantDecision] = Field(default_factory=dict)
    blocking_issues: list[PlanIssue] = Field(default_factory=list)
    warnings: list[PlanIssue] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date(value)


class WorkflowStep(BaseModel):
    step_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    human_blocking: bool = False
    failure_behavior: str = "stop"
    variant: str | None = None
    effects: Literal["read", "write", "execute", "external"] | None = None
    decision_ref: str | None = None
    evidence: list[str] = Field(default_factory=list)


class WorkflowPlan(BaseModel):
    date: str
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"]
    processing_profile: str = "parameterized_navigation_v1"
    platform_hint: str = "unknown"
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
