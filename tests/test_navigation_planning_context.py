import json

import pytest

from vla_data_juicer_agents.navigation.catalog import (
    CAPABILITY_CATALOG_REVISION,
    ToolCapability,
    ToolVariantCapability,
)
from vla_data_juicer_agents.navigation.observation_models import (
    EvidenceDescriptor,
    NavigationObservationRevision,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    TopicMeasurement,
    UserGuidanceObservation,
)
from vla_data_juicer_agents.navigation.planning_context import (
    build_phase_planning_context,
    compute_planning_context_revision,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask, NavigationTaskPhase


def _task() -> NavigationTask:
    return NavigationTask(
        task_id="nav-1",
        date="20260710",
        segments=["20260710_120000"],
        phase=NavigationTaskPhase.EXTRACT_SYNC,
        guidance_revision=2,
        data_profile={"phase_profile_schema": "must not leak", "data_profile_draft": "must not leak"},
    )


def _revision() -> NavigationObservationRevision:
    return NavigationObservationRevision(
        task_id="nav-1",
        revision=3,
        phase=NavigationTaskPhase.EXTRACT_SYNC,
        completed_kinds=["artifact_state", "raw_metadata", "sensor_candidates"],
        payloads=[
            RawMetadataObservation(
                segments=["20260710_120000"],
                topics=[TopicMeasurement(topic="/lidar/points", message_count=42)],
            ),
            RuntimeAssetsObservation(
                pcd_gridmap_tool_available=True,
                manual_annotation_gui_available=True,
                projection_variants={"cjl_with_gridmap": True},
            ),
            UserGuidanceObservation(guidance_revision=2, text="Prefer measured timestamp facts."),
        ],
    )


def _caps() -> list[ToolCapability]:
    return [
        ToolCapability(
            tool_name="prepare_raw_data",
            stage_kind="prepare_raw_data",
            effects="write",
            variants=[ToolVariantCapability(id="default")],
            executor_agent_allowed=True,
            phase="extract_sync",
        ),
        ToolCapability(
            tool_name="run_tracking",
            stage_kind="run_tracking",
            effects="execute",
            variants=[ToolVariantCapability(id="default")],
            executor_agent_allowed=True,
            phase="finish_processing",
        ),
        ToolCapability(
            tool_name="inspect_navigation_topic_candidates",
            stage_kind="inspect_navigation_topic_candidates",
            effects="read",
            variants=[ToolVariantCapability(id="default")],
            plan_agent_allowed=True,
            phase="extract_sync",
        ),
    ]


def test_planning_context_excludes_raw_evidence_schema_and_inactive_phase_facts():
    context = build_phase_planning_context(task=_task(), observation=_revision(), capabilities=_caps())
    payload = context.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)

    assert "phase_profile_schema" not in text
    assert "data_profile_draft" not in text
    assert "raw_payload" not in text
    assert "runtime_assets" not in text
    assert len(text) <= 5_500
    assert context.available_action_ids == ["prepare_raw_data"]
    assert context.observation_status.required == [
        "artifact_state",
        "raw_metadata",
        "sensor_candidates",
        "topic_candidates",
    ]
    assert context.observation_status.completed == [
        "artifact_state",
        "raw_metadata",
        "sensor_candidates",
    ]
    assert context.observation_status.missing == ["topic_candidates"]
    assert context.observation_status.complete is False
    assert context.fact_summary["user_guidance"]["text"] == "Prefer measured timestamp facts."


def test_planning_context_includes_only_owned_evidence_up_to_current_revision():
    descriptor = EvidenceDescriptor(
        ref="evidence-1",
        task_id="nav-1",
        observation_revision=2,
        kind="raw_metadata",
        summary="raw metadata",
        byte_size=12,
        source_tool="inspect_raw_date_tool",
        created_at="2026-07-10T00:00:00+00:00",
    )

    context = build_phase_planning_context(
        task=_task(), observation=_revision(), evidence=[descriptor], capabilities=_caps()
    )

    assert context.evidence_catalog == [descriptor]
    with pytest.raises(PermissionError):
        build_phase_planning_context(
            task=_task(),
            observation=_revision(),
            evidence=[descriptor.model_copy(update={"task_id": "nav-2"})],
            capabilities=_caps(),
        )


def test_planning_context_revision_is_stable_and_changes_with_inputs():
    task = _task()

    first = compute_planning_context_revision(
        task=task,
        observation_revision=3,
        capability_revision=CAPABILITY_CATALOG_REVISION,
    )
    repeated = compute_planning_context_revision(
        task=task,
        observation_revision=3,
        capability_revision=CAPABILITY_CATALOG_REVISION,
    )
    changed = compute_planning_context_revision(
        task=task,
        observation_revision=4,
        capability_revision=CAPABILITY_CATALOG_REVISION,
    )

    assert len(first) == 64
    assert first == repeated
    assert first != changed


def test_planning_context_rejects_cross_task_observation():
    with pytest.raises(PermissionError):
        build_phase_planning_context(
            task=_task(),
            observation=_revision().model_copy(update={"task_id": "nav-2"}),
            capabilities=_caps(),
        )
