from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from vla_data_juicer_agents.navigation.catalog import (
    CAPABILITY_CATALOG_REVISION,
    ToolCapability,
)
from vla_data_juicer_agents.navigation.context_budget import (
    ensure_payload_within_limit,
    serialized_chars,
)
from vla_data_juicer_agents.navigation.observation_models import (
    AnnotationJobFactsObservation,
    EvidenceDescriptor,
    NavigationObservationRevision,
    ObservationKind,
    ObservationPayload,
    StrictModel,
)
from vla_data_juicer_agents.navigation.observation_projection import (
    compact_observation_payload,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask


PLANNING_CONTEXT_MAX_CHARS = 5_500
PLANNING_IDENTITY_MAX_CHARS = 1_600
NavigationStageId = Literal[
    "extract_sync",
    "finish_processing",
    "trajectory_review",
]
AVAILABLE_STAGE_IDS: list[NavigationStageId] = [
    "extract_sync",
    "finish_processing",
    "trajectory_review",
]
TRAJECTORY_REVIEW_OBSERVATION_KINDS = frozenset({"annotation_job_facts"})


class NavigationTaskContext(StrictModel):
    task_id: str
    request: str
    target: str
    date: str
    segments: list[str] | None
    scene_mode: Literal["in", "out"] | None
    planning_context_revision: str
    observation_revision: int
    observed_kinds: list[ObservationKind]
    fact_summary: dict[str, Any]
    available_stage_ids: list[NavigationStageId]
    evidence_catalog: list[EvidenceDescriptor]
    evidence_next_cursor: int | None = None


PLAN_REQUIRED_OBSERVATIONS: dict[str, tuple[ObservationKind, ...]] = {
    "extract_sync": (
        "artifact_state",
        "raw_metadata",
        "sensor_candidates",
        "topic_candidates",
    ),
    "finish_processing": (
        "artifact_state",
        "gridmap_artifacts",
        "runtime_assets",
        "calibration_inventory",
        "localization_sources",
    ),
    "trajectory_review": ("annotation_job_facts",),
}
M2_FINISH_REQUIRED_OBSERVATIONS = frozenset(
    {
        *PLAN_REQUIRED_OBSERVATIONS["finish_processing"],
        "annotation_job_facts",
    }
)


def m2_finish_observations_complete(
    observation: NavigationObservationRevision | None,
) -> bool:
    """Return whether the explicit M2 finish surface has all typed fact families."""
    if observation is None:
        return False
    completed_kinds = set(observation.completed_kinds)
    payload_kinds = {payload.kind for payload in observation.payloads}
    return M2_FINISH_REQUIRED_OBSERVATIONS <= completed_kinds & payload_kinds


def m2_annotation_ready_for_postprocessing(
    observation: NavigationObservationRevision | None,
) -> bool:
    """Return the bounded Annotation readiness fact used by the M2 surface."""
    if observation is None:
        return False
    return any(
        isinstance(payload, AnnotationJobFactsObservation)
        and payload.ready_for_postprocessing
        for payload in observation.payloads
    )


def compute_planning_context_revision(
    *,
    task: NavigationTask,
    observation_revision: int,
    capability_revision: str,
) -> str:
    payload = {
        "task_id": task.task_id,
        "request": task.request,
        "target": task.target,
        "date": task.date,
        "segments": task.segments,
        "scene_mode": task.scene_mode,
        "guidance_revision": task.guidance_revision,
        "observation_revision": observation_revision,
        "capability_revision": capability_revision,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_navigation_task_context(
    *,
    task: NavigationTask,
    observation: NavigationObservationRevision | None,
    evidence: Sequence[EvidenceDescriptor] | None = None,
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> NavigationTaskContext:
    if observation is not None and observation.task_id != task.task_id:
        raise PermissionError("observation belongs to another task")

    observation_revision = observation.revision if observation is not None else 0
    descriptors = list(evidence or [])
    for descriptor in descriptors:
        if descriptor.task_id != task.task_id:
            raise PermissionError("evidence belongs to another task")
    descriptors = [
        descriptor
        for descriptor in descriptors
        if descriptor.observation_revision <= observation_revision
    ]
    payloads = list(observation.payloads) if observation is not None else []
    available_stage_ids = list(AVAILABLE_STAGE_IDS)
    observed_kinds = (
        list(observation.completed_kinds) if observation is not None else []
    )
    if task.target == "trajectory_review":
        descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor.kind in TRAJECTORY_REVIEW_OBSERVATION_KINDS
        ]
        payloads = [
            payload
            for payload in payloads
            if payload.kind in TRAJECTORY_REVIEW_OBSERVATION_KINDS
        ]
        available_stage_ids = ["trajectory_review"]
        observed_kinds = [
            kind
            for kind in observed_kinds
            if kind in TRAJECTORY_REVIEW_OBSERVATION_KINDS
        ]
    request, target, segments = _bounded_task_identity(task)
    context = NavigationTaskContext(
        task_id=task.task_id,
        request=request,
        target=target,
        date=task.date,
        segments=segments,
        scene_mode=task.scene_mode,
        planning_context_revision=compute_planning_context_revision(
            task=task,
            observation_revision=observation_revision,
            capability_revision=_capability_revision(capabilities),
        ),
        observation_revision=observation_revision,
        observed_kinds=observed_kinds,
        fact_summary=_minimal_fact_summary(payloads),
        available_stage_ids=available_stage_ids,
        evidence_catalog=[],
        evidence_next_cursor=0 if descriptors else None,
    )
    ensure_payload_within_limit(
        context.model_dump(mode="json"),
        max_chars=PLANNING_CONTEXT_MAX_CHARS,
        label="planning_context",
    )
    prefix: list[EvidenceDescriptor] = []
    for index, descriptor in enumerate(descriptors):
        candidate_prefix = [*prefix, descriptor]
        candidate = context.model_copy(
            update={
                "evidence_catalog": candidate_prefix,
                "evidence_next_cursor": (
                    index + 1 if index + 1 < len(descriptors) else None
                ),
            }
        )
        if serialized_chars(candidate.model_dump(mode="json")) > PLANNING_CONTEXT_MAX_CHARS:
            break
        prefix = candidate_prefix
        context = candidate
    return _enrich_fact_summary(context, payloads)


def _bounded_task_identity(
    task: NavigationTask,
) -> tuple[str, str, list[str] | None]:
    segment_sources = list(task.segments[:5]) if task.segments is not None else None
    sources = [task.request, task.target, *(segment_sources or [])]
    values = ["" for _ in sources]

    for preview_chars in (40, 80, 160):
        for index, source in enumerate(sources):
            candidate_values = list(values)
            candidate_values[index] = _sanitized_identity_preview(
                source,
                max_chars=preview_chars,
            )
            candidate = {
                "request": candidate_values[0],
                "target": candidate_values[1],
                "segments": (
                    candidate_values[2:] if segment_sources is not None else None
                ),
            }
            if serialized_chars(candidate) <= PLANNING_IDENTITY_MAX_CHARS:
                values = candidate_values

    return (
        values[0],
        values[1],
        values[2:] if segment_sources is not None else None,
    )


def _sanitized_identity_preview(value: str, *, max_chars: int) -> str:
    preview = "".join(
        character if character.isprintable() else " "
        for character in value[:max_chars]
    )
    if len(value) <= max_chars:
        return preview
    return f"{preview[: max_chars - 1]}…"


def _minimal_fact_summary(
    payloads: Sequence[ObservationPayload],
) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for payload in payloads:
        if payload.kind == "raw_metadata":
            facts[payload.kind] = {
                "segment_count": len(payload.segments),
                "topic_count": len(payload.topics),
                "total_message_count": sum(
                    topic.message_count for topic in payload.topics
                ),
            }
        elif payload.kind == "sensor_candidates":
            role_counts: dict[str, int] = {}
            for candidate in payload.candidates:
                role_counts[candidate.role] = role_counts.get(candidate.role, 0) + 1
            facts[payload.kind] = {
                "candidate_count": len(payload.candidates),
                "role_counts": dict(sorted(role_counts.items())),
            }
        elif payload.kind == "topic_candidates":
            facts[payload.kind] = {
                "available_topic_count": len(payload.available_topics),
                "route_count": len(payload.routes),
                "suggested_role_count": len(payload.suggested_role_names),
                "suggested_topic_assignment_count": sum(
                    len(topics) for topics in payload.suggested_role_names.values()
                ),
            }
        elif payload.kind == "artifact_state":
            snapshot = payload.snapshot
            facts[payload.kind] = {
                "segment_count": len(snapshot.segments or []),
                "raw_input_exists": snapshot.raw_input_exists,
                "raw_temp_exists": snapshot.raw_temp_exists,
                "sync_data_exists": snapshot.sync_data_exists,
                "synced_segment_count": sum(snapshot.sync_data_by_segment.values()),
                "finish_temp_samples_exists": snapshot.finish_temp_samples_exists,
                "final_outputs_exist": snapshot.final_outputs_exist,
                "final_grid_map_exists": snapshot.final_grid_map_exists,
                "sync_image_sample_count": len(snapshot.sync_image_samples),
            }
        elif payload.kind == "gridmap_artifacts":
            facts[payload.kind] = {
                "existing_gridmap_count": len(payload.existing_gridmap_paths),
                "pcd_source_count": len(payload.pcd_sources),
                "projection_ready": payload.projection_ready,
            }
        elif payload.kind == "runtime_assets":
            facts[payload.kind] = {
                "pcd_gridmap_tool_available": payload.pcd_gridmap_tool_available,
                "manual_annotation_gui_available": payload.manual_annotation_gui_available,
                "projection_variant_count": len(payload.projection_variants),
                "available_projection_variant_count": sum(
                    bool(available)
                    for available in payload.projection_variants.values()
                ),
                "available_noobscene_localization_variants": sorted(
                    source
                    for source, available in payload.noobscene_localization_variants.items()
                    if available
                ),
                "available_speed_direction_variants": sorted(
                    source
                    for source, available in payload.speed_direction_variants.items()
                    if available
                ),
                "scene_environment_affects_execution": (
                    payload.scene_environment_affects_execution
                ),
            }
        elif payload.kind == "calibration_inventory":
            facts[payload.kind] = {
                "sensor_source_count": len(payload.sensor_sources),
            }
        elif payload.kind == "localization_sources":
            facts[payload.kind] = {
                "available_source_count": len(payload.available_sources),
                "available_sources_preview": list(payload.available_sources),
                "conversion_available": payload.conversion_available,
            }
        elif payload.kind == "annotation_job_facts":
            facts[payload.kind] = {
                "job_status": payload.job_status,
                "segment_count": payload.segment_count,
                "tracked_count": payload.tracked_count,
                "skipped_count": payload.skipped_count,
                "annotated_count": payload.annotated_count,
                "ready_for_postprocessing": payload.ready_for_postprocessing,
                "ready_for_trajectory_review": payload.ready_for_trajectory_review,
                "processing_calibration_snapshot_available": (
                    payload.processing_calibration_snapshot_available
                ),
                "reviews": payload.reviews.model_dump(mode="json"),
            }
        elif payload.kind == "user_guidance":
            facts[payload.kind] = {
                "guidance_revision": payload.guidance_revision,
                "text_length": len(payload.text),
                "text_truncated": bool(payload.text),
            }
    return facts


def _enrich_fact_summary(
    context: NavigationTaskContext,
    payloads: Sequence[ObservationPayload],
) -> NavigationTaskContext:
    for preview_items, string_chars in ((1, 80), (3, 160)):
        for payload in payloads:
            try:
                projection = compact_observation_payload(
                    payload,
                    preview_items=preview_items,
                    max_chars=1_800,
                )
            except ValueError:
                continue
            projection.pop("kind", None)
            projection = _clip_projection_strings(projection, string_chars)
            candidate = context.model_copy(
                update={
                    "fact_summary": {
                        **context.fact_summary,
                        payload.kind: projection,
                    }
                }
            )
            if serialized_chars(candidate.model_dump(mode="json")) <= PLANNING_CONTEXT_MAX_CHARS:
                context = candidate
    return context


def _clip_projection_strings(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return f"{value[: max_chars - 1]}…"
    if isinstance(value, list):
        return [_clip_projection_strings(item, max_chars) for item in value]
    if isinstance(value, dict):
        return {
            key: _clip_projection_strings(item, max_chars)
            for key, item in value.items()
        }
    return value


def _capability_revision(
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> str:
    if isinstance(capabilities, dict):
        return str(capabilities.get("revision", CAPABILITY_CATALOG_REVISION))
    return CAPABILITY_CATALOG_REVISION
