from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from vla_data_juicer_agents.navigation.catalog import (
    CAPABILITY_CATALOG_REVISION,
    ToolCapability,
)
from vla_data_juicer_agents.navigation.context_budget import ensure_payload_within_limit
from vla_data_juicer_agents.navigation.observation_models import (
    EvidenceDescriptor,
    NavigationObservationRevision,
    ObservationKind,
    StrictModel,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask


PLANNING_CONTEXT_MAX_CHARS = 5_500


class ObservationStatus(StrictModel):
    complete: bool
    required: list[str]
    completed: list[str]
    missing: list[str]


class PhasePlanningContext(StrictModel):
    task_id: str
    phase: Literal["extract_sync", "finish_processing"]
    planning_context_revision: str
    observation_status: ObservationStatus
    fact_summary: dict[str, Any]
    available_action_ids: list[str]
    evidence_catalog: list[EvidenceDescriptor]


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
        "date": task.date,
        "segments": task.segments,
        "scene_mode": task.scene_mode,
        "phase": task.phase.value,
        "guidance_revision": task.guidance_revision,
        "observation_revision": observation_revision,
        "capability_revision": capability_revision,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_phase_planning_context(
    *,
    task: NavigationTask,
    observation: NavigationObservationRevision,
    evidence: Sequence[EvidenceDescriptor] | None = None,
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> PhasePlanningContext:
    phase = task.phase.value
    if phase not in PHASE_REQUIRED_OBSERVATIONS:
        raise ValueError(f"task phase does not support planning context: {phase}")
    if observation.task_id != task.task_id:
        raise PermissionError("observation belongs to another task")
    if observation.phase.value != phase:
        raise ValueError("observation phase does not match the active task phase")

    required = list(PHASE_REQUIRED_OBSERVATIONS[phase])
    completed_set = set(observation.completed_kinds)
    completed = [kind for kind in required if kind in completed_set]
    missing = [kind for kind in required if kind not in completed_set]
    capability_items, capability_revision = _capability_items_and_revision(capabilities)
    descriptors = list(evidence or [])
    for descriptor in descriptors:
        if descriptor.task_id != task.task_id:
            raise PermissionError("evidence belongs to another task")
    descriptors = [
        descriptor
        for descriptor in descriptors
        if descriptor.observation_revision <= observation.revision
    ]
    context = PhasePlanningContext(
        task_id=task.task_id,
        phase=phase,
        planning_context_revision=compute_planning_context_revision(
            task=task,
            observation_revision=observation.revision,
            capability_revision=capability_revision,
        ),
        observation_status=ObservationStatus(
            complete=not missing,
            required=required,
            completed=completed,
            missing=missing,
        ),
        fact_summary=_project_fact_summary(task, observation, required),
        available_action_ids=_available_action_ids(capability_items, phase),
        evidence_catalog=descriptors,
    )
    ensure_payload_within_limit(
        context.model_dump(mode="json"),
        max_chars=PLANNING_CONTEXT_MAX_CHARS,
        label="planning_context",
    )
    return context


def _project_fact_summary(
    task: NavigationTask,
    observation: NavigationObservationRevision,
    required: list[ObservationKind],
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "date": task.date,
        "segments": task.segments,
        "scene_mode": task.scene_mode,
    }
    allowed = {*required, "user_guidance"}
    for payload in observation.payloads:
        if payload.kind not in allowed:
            continue
        facts[payload.kind] = payload.model_dump(
            mode="json",
            exclude={"kind"},
            exclude_none=True,
        )
    return facts


def _capability_items_and_revision(
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> tuple[list[ToolCapability], str]:
    revision = CAPABILITY_CATALOG_REVISION
    raw_items: Sequence[ToolCapability | dict[str, Any]]
    if isinstance(capabilities, dict):
        revision = str(capabilities.get("revision", revision))
        raw_items = capabilities.get("capabilities", [])
    else:
        raw_items = capabilities
    return [
        item if isinstance(item, ToolCapability) else ToolCapability.model_validate(item)
        for item in raw_items
    ], revision


def _available_action_ids(capabilities: list[ToolCapability], phase: str) -> list[str]:
    action_ids: list[str] = []
    for capability in capabilities:
        if capability.phase != phase or not capability.executor_agent_allowed:
            continue
        if not any(
            variant.status == "available" for variant in capability.variants
        ):
            continue
        if capability.tool_name not in action_ids:
            action_ids.append(capability.tool_name)
    return action_ids
