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
    TopicCandidatesObservation,
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


def test_planning_context_excludes_executor_capability_without_variants():
    capability = ToolCapability(
        tool_name="variantless_action",
        stage_kind="variantless_action",
        effects="execute",
        variants=[],
        executor_agent_allowed=True,
        phase="extract_sync",
    )

    context = build_phase_planning_context(
        task=_task(), observation=_revision(), capabilities=[capability]
    )

    assert context.available_action_ids == []


def test_planning_context_excludes_executor_capability_without_available_variant():
    capability = ToolCapability(
        tool_name="unavailable_action",
        stage_kind="unavailable_action",
        effects="execute",
        variants=[
            ToolVariantCapability(id="planned", status="planned"),
            ToolVariantCapability(id="placeholder", status="placeholder"),
            ToolVariantCapability(id="deprecated", status="deprecated"),
        ],
        executor_agent_allowed=True,
        phase="extract_sync",
    )

    context = build_phase_planning_context(
        task=_task(), observation=_revision(), capabilities=[capability]
    )

    assert context.available_action_ids == []


def test_planning_context_summarizes_large_fact_lists():
    topic_names = [f"/diagnostics/topic_{index:04d}_" + "x" * 160 for index in range(300)]
    revision = NavigationObservationRevision(
        task_id="nav-1",
        revision=4,
        phase=NavigationTaskPhase.EXTRACT_SYNC,
        completed_kinds=["raw_metadata", "topic_candidates"],
        payloads=[
            RawMetadataObservation(
                segments=["20260710_120000"],
                topics=[TopicMeasurement(topic=name, message_count=1) for name in topic_names],
            ),
            TopicCandidatesObservation(
                available_topics=topic_names,
                suggested_role_names={"diagnostics": topic_names},
            ),
        ],
    )

    context = build_phase_planning_context(
        task=_task(),
        observation=revision,
        capabilities=_caps(),
    )

    serialized = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= 5_500
    assert context.fact_summary["raw_metadata"]["topic_count"] == 300
    assert "topics" not in context.fact_summary["raw_metadata"]
    assert context.fact_summary["topic_candidates"]["available_topic_count"] == 300
    assert "available_topics" not in context.fact_summary["topic_candidates"]


def test_planning_context_uses_largest_descriptor_prefix_with_continuation_cursor():
    descriptors = [
        EvidenceDescriptor(
            ref=f"evidence-{index:03d}-" + "r" * 220,
            task_id="nav-1",
            observation_revision=2,
            kind="raw_metadata",
            summary=f"descriptor {index:03d} " + "s" * 400,
            byte_size=12,
            source_tool="inspect_navigation_raw_metadata_tool",
            created_at="2026-07-10T00:00:00+00:00",
        )
        for index in range(30)
    ]

    context = build_phase_planning_context(
        task=_task(),
        observation=_revision(),
        evidence=descriptors,
        capabilities=_caps(),
    )

    payload = context.model_dump(mode="json")
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 5_500
    assert 0 < len(context.evidence_catalog) < len(descriptors)
    assert context.evidence_next_cursor == len(context.evidence_catalog)
    next_descriptor = descriptors[context.evidence_next_cursor]
    oversized = {
        **payload,
        "evidence_catalog": [
            *payload["evidence_catalog"],
            next_descriptor.model_dump(mode="json"),
        ],
        "evidence_next_cursor": context.evidence_next_cursor + 1,
    }
    assert len(json.dumps(oversized, ensure_ascii=False, separators=(",", ":"))) > 5_500
