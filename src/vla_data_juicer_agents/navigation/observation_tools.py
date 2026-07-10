from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.context_budget import ensure_payload_within_limit
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.inspection import (
    inspect_gridmap_artifacts,
    inspect_navigation_calibration_inventory,
    inspect_navigation_localization_sources,
    inspect_navigation_sensor_candidates,
    inspect_navigation_topic_candidates,
    inspect_raw_date,
    inspect_runtime_assets,
)
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    EvidenceWrite,
    GridmapArtifactsObservation,
    ObservationKind,
    ObservationPayload,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    TopicMeasurement,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.planning_context import (
    PHASE_REQUIRED_OBSERVATIONS,
    build_phase_planning_context,
)
from vla_data_juicer_agents.navigation.task_reconciliation import (
    build_navigation_artifact_snapshot,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask


INSPECTION_RESULT_MAX_CHARS = 4_000
COGNITIVE_RESULT_MAX_CHARS = 5_500


def _make_tool(func: Callable[..., dict[str, Any]], name: str) -> FunctionTool:
    return FunctionTool(func, name=name, is_read_only=True)


def _raw_metadata_observation(raw_payload: dict[str, Any]) -> RawMetadataObservation:
    selected_segments = raw_payload.get("segments", [])
    measurements: dict[str, dict[str, Any]] = {}
    for segment in selected_segments:
        for topic in segment.get("topics", []):
            name = topic["name"]
            measurement = measurements.setdefault(
                name,
                {
                    "topic": name,
                    "message_type": topic.get("type"),
                    "message_count": 0,
                },
            )
            measurement["message_count"] += topic.get("message_count", 0)
            if measurement["message_type"] != topic.get("type"):
                measurement["message_type"] = None
    return RawMetadataObservation(
        segments=[segment["name"] for segment in selected_segments],
        topics=[
            TopicMeasurement.model_validate(measurements[name])
            for name in sorted(measurements)
        ],
    )


def _normalize_fields(fields: list[str] | str | None) -> list[str] | None:
    if fields is None or isinstance(fields, list):
        return fields
    stripped = fields.strip()
    if not stripped:
        return None
    if stripped.startswith("["):
        payload = json.loads(stripped)
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise ValueError("fields must be a list of strings")
        return payload
    return [stripped]


def build_navigation_observation_tools(
    *,
    task: NavigationTask,
    observation_store: SqliteNavigationObservationStore,
    evidence_store: FileNavigationEvidenceStore,
    settings: NavigationSettings,
) -> list[FunctionTool]:
    required = PHASE_REQUIRED_OBSERVATIONS.get(task.phase.value)
    if required is None:
        raise ValueError(f"task phase does not support observation tools: {task.phase.value}")

    def append_observation(
        *,
        kind: ObservationKind,
        payload: ObservationPayload,
        raw_payload: dict[str, Any] | list[Any],
        source_tool: str,
        summary: str,
    ) -> dict[str, Any]:
        revision = observation_store.append(
            task.task_id,
            task.phase,
            kind,
            [payload],
            [
                EvidenceWrite(
                    kind=kind,
                    source_tool=source_tool,
                    payload=raw_payload,
                    summary=summary,
                )
            ],
            evidence_store,
        )
        missing = [
            item
            for item in required
            if item not in revision.completed_kinds
        ]
        result = {
            "ok": True,
            "observation_delta": payload.model_dump(mode="json"),
            "evidence_refs": revision.evidence_refs,
            "observation_revision": revision.revision,
            "remaining_missing_observations": missing,
        }
        return ensure_payload_within_limit(
            result,
            max_chars=INSPECTION_RESULT_MAX_CHARS,
            label=source_tool,
        )

    def inspect_navigation_raw_metadata() -> dict[str, Any]:
        """Inspect selected raw ROS metadata and record factual topic measurements."""
        raw = inspect_raw_date(task.date, settings=settings).model_dump(mode="json")
        if task.segments is not None:
            selected = set(task.segments)
            existing = {segment["name"] for segment in raw["segments"]}
            missing = sorted(selected - existing)
            if missing:
                raise FileNotFoundError(
                    f"requested raw segment(s) not found for {task.date}: {', '.join(missing)}"
                )
            raw["segments"] = [
                segment for segment in raw["segments"] if segment["name"] in selected
            ]
        payload = _raw_metadata_observation(raw)
        return append_observation(
            kind="raw_metadata",
            payload=payload,
            raw_payload=raw,
            source_tool="inspect_navigation_raw_metadata_tool",
            summary=f"Raw metadata for {len(payload.segments)} selected segment(s).",
        )

    def inspect_sensor_candidates() -> dict[str, Any]:
        """List every measured navigation sensor-role candidate without selecting bindings."""
        payload = inspect_navigation_sensor_candidates(
            task.date,
            segments=task.segments,
            settings=settings,
        )
        return append_observation(
            kind="sensor_candidates",
            payload=payload,
            raw_payload=payload.model_dump(mode="json"),
            source_tool="inspect_navigation_sensor_candidates_tool",
            summary=f"Found {len(payload.candidates)} sensor-role candidate(s).",
        )

    def inspect_topic_candidates() -> dict[str, Any]:
        """List available topics and possible role names without producing final parameters."""
        payload = inspect_navigation_topic_candidates(
            task.date,
            segments=task.segments,
            settings=settings,
        )
        return append_observation(
            kind="topic_candidates",
            payload=payload,
            raw_payload=payload.model_dump(mode="json"),
            source_tool="inspect_navigation_topic_candidates_tool",
            summary=f"Found {len(payload.available_topics)} available topic(s).",
        )

    def inspect_artifact_state() -> dict[str, Any]:
        """Inspect existing task artifacts without modifying dataset state."""
        snapshot = build_navigation_artifact_snapshot(
            task.date,
            task.segments,
            settings=settings,
        )
        payload = ArtifactStateObservation(snapshot=snapshot)
        return append_observation(
            kind="artifact_state",
            payload=payload,
            raw_payload=snapshot.model_dump(mode="json"),
            source_tool="inspect_navigation_artifact_state_tool",
            summary="Navigation artifact snapshot.",
        )

    def inspect_gridmap_state() -> dict[str, Any]:
        """Inspect existing gridmap artifacts and projection readiness."""
        raw = inspect_gridmap_artifacts(
            task.date,
            segments=task.segments,
            settings=settings,
        )
        payload = GridmapArtifactsObservation(
            existing_gridmap_paths=raw["available_gridmap_paths"],
            pcd_sources=raw["pcd_sources"],
            projection_ready=raw["projection_input_ready"],
        )
        return append_observation(
            kind="gridmap_artifacts",
            payload=payload,
            raw_payload=raw,
            source_tool="inspect_navigation_gridmap_artifacts_tool",
            summary=f"Found {len(payload.existing_gridmap_paths)} existing gridmap path(s).",
        )

    def inspect_runtime_asset_inventory() -> dict[str, Any]:
        """Inspect the availability of processing scripts and variants."""
        raw = inspect_runtime_assets(settings=settings)
        payload = RuntimeAssetsObservation.model_validate(raw)
        return append_observation(
            kind="runtime_assets",
            payload=payload,
            raw_payload=raw,
            source_tool="inspect_navigation_runtime_assets_tool",
            summary="Navigation runtime asset availability.",
        )

    def inspect_calibration_inventory() -> dict[str, Any]:
        """List existing calibration sensor directories without selecting one."""
        payload = inspect_navigation_calibration_inventory(settings=settings)
        return append_observation(
            kind="calibration_inventory",
            payload=payload,
            raw_payload=payload.model_dump(mode="json"),
            source_tool="inspect_navigation_calibration_inventory_tool",
            summary=f"Found {len(payload.sensor_sources)} calibration sensor source(s).",
        )

    def inspect_localization_inventory() -> dict[str, Any]:
        """List measured localization source kinds and converter availability."""
        payload = inspect_navigation_localization_sources(
            task.date,
            segments=task.segments,
            settings=settings,
        )
        return append_observation(
            kind="localization_sources",
            payload=payload,
            raw_payload=payload.model_dump(mode="json"),
            source_tool="inspect_navigation_localization_sources_tool",
            summary=f"Found {len(payload.available_sources)} localization source kind(s).",
        )

    def get_phase_planning_context() -> dict[str, Any]:
        """Return the bounded factual planning context for the bound task and active phase."""
        observation = observation_store.latest(task.task_id)
        if observation is None:
            raise ValueError("no observations recorded for the active task")
        evidence = observation_store.list_evidence(
            task.task_id,
            limit=100,
        )
        return build_phase_planning_context(
            task=task,
            observation=observation,
            evidence=evidence,
            capabilities=list_navigation_tool_capabilities(),
        ).model_dump(mode="json")

    def list_observation_evidence(
        kind: str | None = None,
        observation_revision: int | None = None,
        cursor: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List paginated evidence descriptors owned by the bound task."""
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        descriptors = observation_store.list_evidence(
            task.task_id,
            kind=kind,
            observation_revision=observation_revision,
            cursor=cursor,
            limit=limit + 1,
        )
        has_more = len(descriptors) > limit
        result = {
            "evidence": [
                descriptor.model_dump(mode="json")
                for descriptor in descriptors[:limit]
            ],
            "next_cursor": cursor + limit if has_more else None,
        }
        return ensure_payload_within_limit(
            result,
            max_chars=COGNITIVE_RESULT_MAX_CHARS,
            label="observation_evidence_list",
        )

    def read_observation_evidence(
        ref: str,
        fields: list[str] | str | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read selected, paginated evidence fields owned by the bound task."""
        return evidence_store.read(
            task.task_id,
            ref,
            fields=_normalize_fields(fields),
            cursor=cursor,
            limit=limit,
        )

    def describe_processing_action(action_id: str) -> dict[str, Any]:
        """Describe one executor action available in the bound task's active phase."""
        for capability in list_navigation_tool_capabilities():
            if (
                capability.tool_name == action_id
                and capability.phase == task.phase.value
                and capability.executor_agent_allowed
                and any(variant.status == "available" for variant in capability.variants)
            ):
                return capability.model_dump(mode="json")
        raise KeyError(f"action is not available in active phase: {action_id}")

    return [
        _make_tool(inspect_navigation_raw_metadata, "inspect_navigation_raw_metadata_tool"),
        _make_tool(inspect_sensor_candidates, "inspect_navigation_sensor_candidates_tool"),
        _make_tool(inspect_topic_candidates, "inspect_navigation_topic_candidates_tool"),
        _make_tool(inspect_artifact_state, "inspect_navigation_artifact_state_tool"),
        _make_tool(inspect_gridmap_state, "inspect_navigation_gridmap_artifacts_tool"),
        _make_tool(inspect_runtime_asset_inventory, "inspect_navigation_runtime_assets_tool"),
        _make_tool(inspect_calibration_inventory, "inspect_navigation_calibration_inventory_tool"),
        _make_tool(inspect_localization_inventory, "inspect_navigation_localization_sources_tool"),
        _make_tool(get_phase_planning_context, "get_phase_planning_context_tool"),
        _make_tool(list_observation_evidence, "list_observation_evidence_tool"),
        _make_tool(read_observation_evidence, "read_observation_evidence_tool"),
        _make_tool(describe_processing_action, "describe_processing_action_tool"),
    ]
