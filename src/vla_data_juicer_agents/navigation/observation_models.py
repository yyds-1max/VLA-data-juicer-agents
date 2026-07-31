from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vla_data_juicer_agents.navigation.task_state import NavigationArtifactSnapshot


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ObservationKind = Literal[
    "raw_metadata",
    "sensor_candidates",
    "topic_candidates",
    "artifact_state",
    "gridmap_artifacts",
    "runtime_assets",
    "calibration_inventory",
    "localization_sources",
    "annotation_job_facts",
    "user_guidance",
]


class TopicMeasurement(StrictModel):
    topic: str
    message_type: str | None = None
    message_count: int = 0
    frequency_hz: float | None = None
    time_range: tuple[float, float] | None = None
    timestamp_jitter_ms: float | None = None
    missing_ratio: float | None = None


class SensorRoleCandidate(StrictModel):
    role: Literal["fisheye_front", "lidar", "odom", "ins", "localization", "gridmap"]
    topic: str
    message_type: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class TopicRouteCandidate(StrictModel):
    role: Literal["fisheye_front", "lidar", "odom", "ins", "localization", "gridmap"]
    topic: str
    extracted_dir: str
    output_dir: str
    sync_reference_eligible: bool = False


class RawMetadataObservation(StrictModel):
    kind: Literal["raw_metadata"] = "raw_metadata"
    segments: list[str]
    topics: list[TopicMeasurement]


class SensorCandidatesObservation(StrictModel):
    kind: Literal["sensor_candidates"] = "sensor_candidates"
    candidates: list[SensorRoleCandidate]


class TopicCandidatesObservation(StrictModel):
    kind: Literal["topic_candidates"] = "topic_candidates"
    available_topics: list[str]
    suggested_role_names: dict[str, list[str]]
    routes: list[TopicRouteCandidate] = Field(default_factory=list)


class ArtifactStateObservation(StrictModel):
    kind: Literal["artifact_state"] = "artifact_state"
    snapshot: NavigationArtifactSnapshot


class GridmapArtifactsObservation(StrictModel):
    kind: Literal["gridmap_artifacts"] = "gridmap_artifacts"
    existing_gridmap_paths: list[str] = Field(default_factory=list)
    pcd_sources: list[str] = Field(default_factory=list)
    projection_ready: bool = False


class RuntimeAssetsObservation(StrictModel):
    kind: Literal["runtime_assets"] = "runtime_assets"
    pcd_gridmap_tool_available: bool
    manual_annotation_gui_available: bool
    projection_variants: dict[str, bool]
    noobscene_localization_variants: dict[str, bool] = Field(default_factory=dict)
    speed_direction_variants: dict[str, bool] = Field(default_factory=dict)
    scene_environment_affects_execution: bool = False


class CalibrationInventoryObservation(StrictModel):
    kind: Literal["calibration_inventory"] = "calibration_inventory"
    sensor_sources: list[str]


class LocalizationSourcesObservation(StrictModel):
    kind: Literal["localization_sources"] = "localization_sources"
    available_sources: list[Literal["odom", "ins"]]
    conversion_available: bool


class AnnotationReviewCounts(StrictModel):
    pending: int = Field(default=0, ge=0)
    in_progress: int = Field(default=0, ge=0)
    returned: int = Field(default=0, ge=0)
    approved: int = Field(default=0, ge=0)
    discarded: int = Field(default=0, ge=0)


class AnnotationJobFactsObservation(StrictModel):
    """Bounded Annotation facts; never contains refs, paths, or geometry."""

    kind: Literal["annotation_job_facts"] = "annotation_job_facts"
    job_status: Literal[
        "missing",
        "preparing",
        "waiting_initial_annotation",
        "tracking",
        "tracked",
        "postprocessing",
        "annotated",
        "failed",
        "cancelled",
    ]
    segment_count: int = Field(default=0, ge=0)
    tracked_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    annotated_count: int = Field(default=0, ge=0)
    ready_for_postprocessing: bool = False
    ready_for_trajectory_review: bool = False
    processing_calibration_snapshot_available: bool = False
    reviews: AnnotationReviewCounts = Field(default_factory=AnnotationReviewCounts)


class UserGuidanceObservation(StrictModel):
    kind: Literal["user_guidance"] = "user_guidance"
    guidance_revision: int
    text: str


ObservationPayload = Annotated[
    RawMetadataObservation
    | SensorCandidatesObservation
    | TopicCandidatesObservation
    | ArtifactStateObservation
    | GridmapArtifactsObservation
    | RuntimeAssetsObservation
    | CalibrationInventoryObservation
    | LocalizationSourcesObservation
    | AnnotationJobFactsObservation
    | UserGuidanceObservation,
    Field(discriminator="kind"),
]


class EvidenceWrite(StrictModel):
    kind: str
    source_tool: str
    payload: dict[str, Any] | list[Any]
    summary: str = Field(max_length=500)


class EvidenceDescriptor(StrictModel):
    ref: str
    task_id: str
    observation_revision: int
    kind: str
    summary: str
    byte_size: int = Field(ge=0)
    source_tool: str
    created_at: str


class NavigationObservationRevision(StrictModel):
    task_id: str
    revision: int = Field(ge=1)
    completed_kinds: list[ObservationKind]
    payloads: list[ObservationPayload]
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
