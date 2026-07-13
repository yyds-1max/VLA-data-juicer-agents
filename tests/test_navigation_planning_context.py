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
    NavigationTaskContext,
    build_navigation_task_context,
    compute_planning_context_revision,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask


def _task() -> NavigationTask:
    return NavigationTask(
        task_id="nav-1",
        created_by_web_session_id="web-plan",
        agentscope_session_id="as-plan",
        request="Process the selected navigation clips.",
        target="20260710/20260710_120000",
        date="20260710",
        segments=["20260710_120000"],
        guidance_revision=2,
    )


def _revision() -> NavigationObservationRevision:
    return NavigationObservationRevision(
        task_id="nav-1",
        revision=3,
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
            UserGuidanceObservation(
                guidance_revision=2,
                text="Prefer measured timestamp facts.",
            ),
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
    ]


def test_planning_context_is_stage_neutral_and_exposes_only_bounded_facts():
    context = build_navigation_task_context(
        task=_task(), observation=_revision(), capabilities=_caps()
    )
    payload = context.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)

    assert set(payload) == {
        "task_id",
        "request",
        "target",
        "date",
        "segments",
        "scene_mode",
        "planning_context_revision",
        "observation_revision",
        "observed_kinds",
        "fact_summary",
        "available_stage_ids",
        "evidence_catalog",
        "evidence_next_cursor",
    }
    assert context.available_stage_ids == ["extract_sync", "finish_processing"]
    assert context.observation_revision == 3
    assert context.observed_kinds == [
        "artifact_state",
        "raw_metadata",
        "sensor_candidates",
    ]
    assert context.fact_summary["runtime_assets"]["pcd_gridmap_tool_available"] is True
    assert context.fact_summary["user_guidance"]["text"] == "Prefer measured timestamp facts."
    assert "raw_payload" not in text
    assert not ({"phase", "required", "missing", "recommended", "next_tool"} & set(payload))
    assert len(text) <= 5_500


def test_fresh_attempt_builds_revision_zero_context_with_request_facts():
    context = build_navigation_task_context(
        task=_task(), observation=None, capabilities=_caps()
    )

    assert context.task_id == "nav-1"
    assert context.request == "Process the selected navigation clips."
    assert context.target == "20260710/20260710_120000"
    assert context.date == "20260710"
    assert context.segments == ["20260710_120000"]
    assert context.observation_revision == 0
    assert context.observed_kinds == []
    assert context.available_stage_ids == ["extract_sync", "finish_processing"]
    assert context.evidence_catalog == []
    assert context.evidence_next_cursor is None


def test_planning_context_schema_has_no_code_authored_recommendations():
    properties = NavigationTaskContext.model_json_schema()["properties"]

    assert not (
        {"phase", "required", "missing", "recommended", "next_tool", "available_action_ids"}
        & set(properties)
    )


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

    context = build_navigation_task_context(
        task=_task(), observation=_revision(), evidence=[descriptor], capabilities=_caps()
    )

    assert context.evidence_catalog == [descriptor]
    with pytest.raises(PermissionError):
        build_navigation_task_context(
            task=_task(),
            observation=_revision(),
            evidence=[descriptor.model_copy(update={"task_id": "nav-2"})],
            capabilities=_caps(),
        )


def test_planning_context_revision_changes_with_facts():
    task = _task()

    first = compute_planning_context_revision(
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
    assert first != changed


def test_planning_context_rejects_cross_task_observation():
    with pytest.raises(PermissionError):
        build_navigation_task_context(
            task=_task(),
            observation=_revision().model_copy(update={"task_id": "nav-2"}),
            capabilities=_caps(),
        )


def test_planning_context_summarizes_large_fact_lists():
    topic_names = [f"/diagnostics/topic_{index:04d}_" + "x" * 160 for index in range(300)]
    revision = NavigationObservationRevision(
        task_id="nav-1",
        revision=4,
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

    context = build_navigation_task_context(
        task=_task(), observation=revision, capabilities=_caps()
    )

    serialized = json.dumps(
        context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
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

    context = build_navigation_task_context(
        task=_task(), observation=_revision(), evidence=descriptors, capabilities=_caps()
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
