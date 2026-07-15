import asyncio
import inspect
import json
from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation import observation_tools as observation_tools_module
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    CalibrationInventoryObservation,
    EvidenceWrite,
    GridmapArtifactsObservation,
    LocalizationSourcesObservation,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    SensorCandidatesObservation,
    SensorRoleCandidate,
    TopicCandidatesObservation,
    TopicMeasurement,
    UserGuidanceObservation,
)
from vla_data_juicer_agents.navigation.observation_store import SqliteNavigationObservationStore
from vla_data_juicer_agents.navigation.observation_tools import build_navigation_observation_tools
from vla_data_juicer_agents.navigation.planning_context import NavigationTaskContext
from vla_data_juicer_agents.navigation.task_state import (
    NavigationArtifactSnapshot,
    NavigationTask,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
INSPECTION_RESULT_KEYS = {
    "ok",
    "observation_revision",
    "observed_kind",
    "summary",
    "evidence_refs",
}


def _invoke_tool(tool, arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        return _decode_tool_payload(payload)

    return asyncio.run(_call())


def _decode_tool_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "content"):
        return _decode_tool_payload(payload.content)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        return json.loads(
            "".join(
                block.text
                for block in payload
                if hasattr(block, "text") and isinstance(block.text, str)
            )
        )
    raise TypeError(f"unsupported tool payload: {type(payload)!r}")


def _task(*, scene_mode=None):
    return NavigationTask(
        task_id="nav-observe-1",
        created_by_web_session_id="web-observe",
        agentscope_session_id="as-observe",
        date="20270605",
        segments=["20260605_152856"],
        scene_mode=scene_mode,
    )


def _tools(tmp_path, *, task=None, settings=None):
    observation_store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    built = build_navigation_observation_tools(
        task=task or _task(),
        observation_store=observation_store,
        evidence_store=evidence_store,
        settings=settings or NavigationSettings(vladatasets_root=FIXTURE_ROOT),
        expected_web_session_id=(task or _task()).created_by_web_session_id,
        expected_agentscope_session_id=(task or _task()).agentscope_session_id,
    )
    return {tool.name: tool for tool in built}, observation_store, evidence_store


def test_builder_exposes_factual_and_cognitive_tools_without_semantic_inference(tmp_path):
    tools, _, _ = _tools(tmp_path)

    assert set(tools) == {
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "get_navigation_task_context_tool",
        "list_observation_evidence_tool",
        "read_observation_evidence_tool",
        "describe_processing_action_tool",
    }
    assert not any("infer_" in name or "profile" in name for name in tools)
    assert all(tool.is_read_only for tool in tools.values())
    assert all("task_id" not in tool.input_schema.get("properties", {}) for tool in tools.values())


def test_inspection_tool_returns_only_compact_delta_and_external_evidence(tmp_path):
    tools, observation_store, evidence_store = _tools(tmp_path)

    result = _invoke_tool(tools["inspect_navigation_raw_metadata_tool"], {})

    assert set(result) == INSPECTION_RESULT_KEYS
    assert result["ok"] is True
    assert result["observed_kind"] == "raw_metadata"
    assert result["summary"]["topic_count"] >= 1
    assert result["observation_revision"] == 1
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4_000
    revision = observation_store.latest("nav-observe-1")
    assert revision is not None
    assert revision.completed_kinds == ["raw_metadata"]
    assert revision.evidence_refs == result["evidence_refs"]
    evidence = evidence_store.read("nav-observe-1", result["evidence_refs"][0])
    assert evidence["data"]["date"] == "20270605"
    assert evidence["data"]["segments"][0]["metadata_path"].endswith("metadata.yaml")


def test_large_topic_inventory_is_compacted_before_single_persisted_revision(tmp_path):
    root = tmp_path / "VLADatasets"
    metadata_path = root / "raw_data" / "20270605" / "segment_a" / "metadata.yaml"
    metadata_path.parent.mkdir(parents=True)
    topics = [
        (f"/diagnostics/very_long_topic_name_{index:04d}_" + "x" * 80, "std_msgs/msg/String")
        for index in range(250)
    ] + [
        ("/cam_video4/csi_cam/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/rs32_lidar_points", "sensor_msgs/msg/PointCloud2"),
        ("/sport_odom", "nav_msgs/msg/Odometry"),
    ]
    entries = "\n".join(
        "    - topic_metadata:\n"
        f"        name: {name}\n"
        f"        type: {message_type}\n"
        "      message_count: 1"
        for name, message_type in topics
    )
    metadata_path.write_text(
        "rosbag2_bagfile_information:\n"
        "  topics_with_message_count:\n"
        f"{entries}\n",
        encoding="utf-8",
    )
    task = NavigationTask(
        task_id="nav-large-inventory",
        created_by_web_session_id="web-observe",
        agentscope_session_id="as-observe",
        date="20270605",
        segments=["segment_a"],
    )
    tools, observation_store, evidence_store = _tools(
        tmp_path,
        task=task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    result = _invoke_tool(tools["inspect_navigation_topic_candidates_tool"], {})

    compact = result["summary"]
    assert result["observed_kind"] == "topic_candidates"
    assert compact["available_topic_count"] == len(topics)
    assert "available_topics" not in compact
    assert len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) <= 2_000
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4_000
    revision = observation_store.latest(task.task_id)
    assert revision is not None
    assert revision.revision == 1
    topic_observation = next(payload for payload in revision.payloads if payload.kind == "topic_candidates")
    assert len(topic_observation.available_topics) == len(topics)
    collected = []
    cursor = 0
    while cursor is not None:
        page = evidence_store.read(
            task.task_id,
            result["evidence_refs"][0],
            fields=["available_topics"],
            cursor=cursor,
            limit=20,
        )
        collected.extend(page["data"]["available_topics"])
        cursor = page["next_cursor"]
    assert collected == sorted(name for name, _ in topics)


def test_extract_observation_tools_complete_phase_without_selecting_params(tmp_path):
    tools, observation_store, _ = _tools(tmp_path)

    for name in (
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
    ):
        result = _invoke_tool(tools[name], {})
        assert set(result) == INSPECTION_RESULT_KEYS
        assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4_000

    latest = observation_store.latest("nav-observe-1")
    assert latest is not None
    assert latest.revision == 4
    assert latest.completed_kinds == [
        "artifact_state",
        "raw_metadata",
        "sensor_candidates",
        "topic_candidates",
    ]
    serialized = json.dumps(latest.model_dump(mode="json"), ensure_ascii=False)
    assert "topic_whitelist" not in serialized
    assert "topic_map" not in serialized
    assert "query_dir" not in serialized
    context = _invoke_tool(tools["get_navigation_task_context_tool"], {})
    assert context["observed_kinds"] == [
        "artifact_state",
        "raw_metadata",
        "sensor_candidates",
        "topic_candidates",
    ]
    assert context["available_stage_ids"] == ["extract_sync", "finish_processing"]
    assert len(context["evidence_catalog"]) == 4


def test_finish_inventory_tools_report_only_measured_candidates(tmp_path):
    processing_root = tmp_path / "processing"
    sensor_dir = processing_root / "NoobScenes" / "params" / "20260529_go2w" / "sensors"
    sensor_dir.mkdir(parents=True)
    converter = processing_root / "NoobScenes" / "include" / "1_odom_convert.py"
    converter.parent.mkdir(parents=True, exist_ok=True)
    converter.write_text("# converter\n", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT, processing_root=processing_root)
    tools, _, _ = _tools(
        tmp_path,
        task=_task(scene_mode="out"),
        settings=settings,
    )

    calibration = _invoke_tool(tools["inspect_navigation_calibration_inventory_tool"], {})
    localization = _invoke_tool(tools["inspect_navigation_localization_sources_tool"], {})

    assert calibration["observed_kind"] == "calibration_inventory"
    assert calibration["summary"] == {
        "sensor_source_count": 1,
        "sensor_sources_preview": ["NoobScenes/params/20260529_go2w/sensors"],
    }
    assert localization["observed_kind"] == "localization_sources"
    assert localization["summary"] == {
        "available_source_count": 1,
        "available_sources_preview": ["odom"],
        "conversion_available": True,
    }


def test_cognitive_tools_bind_task_paginate_evidence_and_describe_requested_action(tmp_path):
    tools, _, _ = _tools(tmp_path)
    inspected = _invoke_tool(tools["inspect_navigation_raw_metadata_tool"], {})
    _invoke_tool(tools["inspect_navigation_sensor_candidates_tool"], {})

    listed = _invoke_tool(
        tools["list_observation_evidence_tool"],
        {"cursor": 0, "limit": 1},
    )
    read = _invoke_tool(
        tools["read_observation_evidence_tool"],
        {"ref": inspected["evidence_refs"][0], "fields": ["date"], "cursor": 0, "limit": 1},
    )
    described = _invoke_tool(
        tools["describe_processing_action_tool"],
        {"action_id": "prepare_raw_data"},
    )

    assert listed["evidence"][0]["task_id"] == "nav-observe-1"
    assert listed["next_cursor"] == 1
    second_page = _invoke_tool(
        tools["list_observation_evidence_tool"],
        {"cursor": listed["next_cursor"], "limit": 1},
    )
    assert second_page["evidence"][0]["observation_revision"] == 2
    assert second_page["next_cursor"] is None
    assert read == {"data": {"date": "20270605"}, "next_cursor": None}
    assert described["action_id"] == "prepare_raw_data"
    assert set(described) == {
        "action_id",
        "variants",
        "parameter_contract",
        "preconditions",
        "constraints",
    }
    assert described["variants"] == [{"id": "default"}]
    assert described["parameter_contract"]["additionalProperties"] is False
    assert set(described["constraints"]) == {
        "human_blocking",
        "locks_navigation_target",
        "supports_dry_run",
    }

    finish_action = _invoke_tool(
        tools["describe_processing_action_tool"],
        {"action_id": "run_tracking"},
    )
    assert finish_action["action_id"] == "run_tracking"


def test_read_observation_evidence_default_ref_pages_by_character_budget(tmp_path):
    tools, _, evidence_store = _tools(tmp_path)
    rows = [f"row-{index:03d}-" + "x" * 300 for index in range(80)]
    descriptor = evidence_store.write(
        "nav-observe-1",
        1,
        "large_rows",
        "inspect_large_rows_tool",
        {"rows": rows, "source": "measured"},
        "large rows",
    )

    first = _invoke_tool(
        tools["read_observation_evidence_tool"],
        {"ref": descriptor.ref},
    )

    first_rows = first["data"]["rows"]
    assert first["data"]["source"] == "measured"
    assert 0 < len(first_rows) < 50
    assert first["next_cursor"] == len(first_rows)
    assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":"))) <= 5_500
    second = _invoke_tool(
        tools["read_observation_evidence_tool"],
        {"ref": descriptor.ref, "cursor": first["next_cursor"]},
    )
    assert second["data"]["rows"][0] == rows[first["next_cursor"]]
    assert second["next_cursor"] == first["next_cursor"] + len(second["data"]["rows"])
    assert len(json.dumps(second, ensure_ascii=False, separators=(",", ":"))) <= 5_500


def test_repeated_inspections_paginate_evidence_and_context_by_character_budget(tmp_path):
    tools, _, _ = _tools(tmp_path)
    for _ in range(30):
        _invoke_tool(tools["inspect_navigation_artifact_state_tool"], {})

    first = _invoke_tool(tools["list_observation_evidence_tool"], {})

    assert 0 < len(first["evidence"]) < 20
    assert first["next_cursor"] == len(first["evidence"])
    assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":"))) <= 5_500
    second = _invoke_tool(
        tools["list_observation_evidence_tool"],
        {"cursor": first["next_cursor"]},
    )
    assert second["evidence"][0]["observation_revision"] == first["next_cursor"] + 1
    assert len(json.dumps(second, ensure_ascii=False, separators=(",", ":"))) <= 5_500

    context = _invoke_tool(tools["get_navigation_task_context_tool"], {})
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 5_500
    assert context["evidence_next_cursor"] == len(context["evidence_catalog"])
    assert context["evidence_next_cursor"] < 30


def test_real_context_tool_globally_bounds_all_observation_kinds_and_evidence(tmp_path):
    long_names = [f"item-{index:03d}-" + "x" * 300 for index in range(40)]
    payloads = [
        ArtifactStateObservation(
            snapshot=NavigationArtifactSnapshot(
                date="20270605",
                segments=long_names,
                raw_input_exists=True,
                sync_data_exists=True,
                sync_data_by_segment={name: index % 2 == 0 for index, name in enumerate(long_names)},
                finish_temp_samples_exists=True,
                final_outputs_exist=False,
                final_grid_map_exists=False,
                sync_image_samples=[f"clip/{name}/image.jpg" for name in long_names],
            )
        ),
        RawMetadataObservation(
            segments=long_names,
            topics=[
                TopicMeasurement(
                    topic=f"/topic/{name}",
                    message_type=f"custom_msgs/msg/{name}",
                    message_count=index + 1,
                )
                for index, name in enumerate(long_names)
            ],
        ),
        SensorCandidatesObservation(
            candidates=[
                SensorRoleCandidate(
                    role=("lidar" if index % 2 else "fisheye_front"),
                    topic=f"/sensor/{name}",
                    message_type=f"custom_msgs/msg/{name}",
                    confidence=0.5,
                )
                for index, name in enumerate(long_names)
            ]
        ),
        TopicCandidatesObservation(
            available_topics=[f"/available/{name}" for name in long_names],
            suggested_role_names={
                f"role-{index:03d}-" + "r" * 200: [f"/available/{name}"]
                for index, name in enumerate(long_names)
            },
        ),
        GridmapArtifactsObservation(
            existing_gridmap_paths=[f"gridmaps/{name}.pcd" for name in long_names],
            pcd_sources=[f"sources/{name}.pcd" for name in long_names],
            projection_ready=True,
        ),
        RuntimeAssetsObservation(
            pcd_gridmap_tool_available=True,
            manual_annotation_gui_available=False,
            projection_variants={f"variant-{name}": True for name in long_names},
        ),
        CalibrationInventoryObservation(
            sensor_sources=[f"params/{name}/sensors" for name in long_names]
        ),
        LocalizationSourcesObservation(
            available_sources=["odom", "ins"],
            conversion_available=True,
        ),
        UserGuidanceObservation(
            guidance_revision=7,
            text="guidance-" + "g" * 2_000,
        ),
    ]
    tools, observation_store, _ = _tools(tmp_path)
    for payload in payloads:
        observation_store.append(
            "nav-observe-1",
            payload.kind,
            [payload],
            [
                EvidenceWrite(
                    kind=payload.kind,
                    source_tool=f"inspect_{payload.kind}_tool",
                    payload=payload.model_dump(mode="json"),
                    summary=f"{payload.kind}: " + "s" * 450,
                )
            ],
            FileNavigationEvidenceStore(tmp_path / "evidence"),
            expected_web_session_id="web-observe",
            expected_agentscope_session_id="as-observe",
        )

    context = _invoke_tool(tools["get_navigation_task_context_tool"], {})
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))

    assert len(serialized) <= 5_500
    assert set(context["fact_summary"]) == {payload.kind for payload in payloads}
    assert context["fact_summary"]["raw_metadata"]["topic_count"] == len(long_names)
    assert context["fact_summary"]["artifact_state"]["sync_data_exists"] is True
    assert context["fact_summary"]["gridmap_artifacts"]["projection_ready"] is True
    assert context["fact_summary"]["runtime_assets"]["pcd_gridmap_tool_available"] is True
    assert context["fact_summary"]["localization_sources"]["conversion_available"] is True
    assert context["evidence_catalog"]
    assert context["evidence_next_cursor"] is not None


def test_real_context_tool_bounds_revision_zero_identity_after_json_escaping(tmp_path):
    adversarial = 'quoted-"-backslash-\\-newline-\n-' + "\x00" * 160
    task = NavigationTask(
        task_id="nav-observe-1",
        created_by_web_session_id="web-observe",
        agentscope_session_id="as-observe",
        request=adversarial,
        target=adversarial,
        date="20270605",
        segments=[f"segment-{index}-{adversarial}" for index in range(5)],
    )
    tools, observation_store, _ = _tools(tmp_path, task=task)

    context = _invoke_tool(tools["get_navigation_task_context_tool"], {})
    repeated = _invoke_tool(tools["get_navigation_task_context_tool"], {})
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))

    assert observation_store.latest(task.task_id) is None
    assert context == repeated
    assert set(context) == set(NavigationTaskContext.model_fields)
    assert context["observation_revision"] == 0
    assert context["observed_kinds"] == []
    assert context["fact_summary"] == {}
    assert context["evidence_catalog"] == []
    assert context["evidence_next_cursor"] is None
    assert context["segments"] is not None
    assert len(context["segments"]) == 5
    assert all(context[field] for field in ("request", "target"))
    assert all(context["segments"])
    assert all(
        character.isprintable()
        for value in [context["request"], context["target"], *context["segments"]]
        for character in value
    )
    assert len(serialized) <= 5_500


@pytest.mark.parametrize(
    "message",
    [
        'quoted "value" ' * 1_000,
        "backslash\\value " * 1_000,
        "line one\nline two\r\n" * 1_000,
        "\x00" * 1_000,
    ],
)
def test_inspection_failure_is_json_bounded_sanitized_and_never_persists(
    monkeypatch,
    tmp_path,
    message,
):
    def fail(*_args, **_kwargs):
        raise ValueError(message)

    monkeypatch.setattr(observation_tools_module, "inspect_raw_date", fail)
    tools, observation_store, _ = _tools(tmp_path)

    result = _invoke_tool(tools["inspect_navigation_raw_metadata_tool"], {})

    assert set(result) == {"ok", "error_type", "message"}
    assert result["ok"] is False
    assert result["error_type"] == "invalid_inspection_request"
    assert "\x00" not in result["message"]
    assert "\n" not in result["message"]
    assert "\r" not in result["message"]
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4_000
    assert observation_store.latest("nav-observe-1") is None
    assert not (tmp_path / "evidence" / "nav-observe-1").exists()


def test_inspection_failure_uses_fixed_fallback_when_exception_stringification_fails(
    monkeypatch,
    tmp_path,
):
    class BrokenMessageError(Exception):
        def __str__(self):
            raise RuntimeError("cannot stringify")

    def fail(*_args, **_kwargs):
        raise BrokenMessageError()

    monkeypatch.setattr(observation_tools_module, "inspect_raw_date", fail)
    tools, observation_store, _ = _tools(tmp_path)

    result = _invoke_tool(tools["inspect_navigation_raw_metadata_tool"], {})

    assert result == {
        "ok": False,
        "error_type": "inspection_failed",
        "message": "Inspection failed.",
    }
    assert observation_store.latest("nav-observe-1") is None
    assert not (tmp_path / "evidence" / "nav-observe-1").exists()
