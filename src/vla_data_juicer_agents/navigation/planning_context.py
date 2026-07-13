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
    EvidenceDescriptor,
    NavigationObservationRevision,
    ObservationKind,
    StrictModel,
)
from vla_data_juicer_agents.navigation.observation_projection import (
    compact_observation_payload,
    preview_string,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask


PLANNING_CONTEXT_MAX_CHARS = 5_500
NavigationStageId = Literal["extract_sync", "finish_processing"]
AVAILABLE_STAGE_IDS: list[NavigationStageId] = ["extract_sync", "finish_processing"]


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


PHASE_REQUIRED_OBSERVATIONS: dict[str, tuple[ObservationKind, ...]] = {
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
}


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
    context = NavigationTaskContext(
        task_id=task.task_id,
        request=preview_string(task.request),
        target=preview_string(task.target),
        date=task.date,
        segments=(
            [preview_string(segment) for segment in task.segments[:5]]
            if task.segments is not None
            else None
        ),
        scene_mode=task.scene_mode,
        planning_context_revision=compute_planning_context_revision(
            task=task,
            observation_revision=observation_revision,
            capability_revision=_capability_revision(capabilities),
        ),
        observation_revision=observation_revision,
        observed_kinds=(list(observation.completed_kinds) if observation is not None else []),
        fact_summary=_project_fact_summary(observation),
        available_stage_ids=list(AVAILABLE_STAGE_IDS),
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
    return context


def _project_fact_summary(
    observation: NavigationObservationRevision | None,
) -> dict[str, Any]:
    if observation is None:
        return {}
    facts: dict[str, Any] = {}
    for payload in observation.payloads:
        compact = compact_observation_payload(
            payload,
            preview_items=3,
            max_chars=1_800,
        )
        compact.pop("kind", None)
        facts[payload.kind] = compact
    return facts


def _capability_revision(
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> str:
    if isinstance(capabilities, dict):
        return str(capabilities.get("revision", CAPABILITY_CATALOG_REVISION))
    return CAPABILITY_CATALOG_REVISION
