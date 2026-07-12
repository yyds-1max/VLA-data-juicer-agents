import asyncio
import inspect
import json
from pathlib import Path

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_store import SqliteNavigationObservationStore
from vla_data_juicer_agents.navigation.observation_tools import build_navigation_observation_tools
from vla_data_juicer_agents.navigation.task_state import (
    NavigationTask,
    NavigationTaskPhase,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
INSPECTION_RESULT_KEYS = {
    "ok",
    "observation_delta",
    "evidence_refs",
    "observation_revision",
    "remaining_missing_observations",
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


def _task(phase=NavigationTaskPhase.EXTRACT_SYNC):
    return NavigationTask(
        task_id="nav-observe-1",
        date="20270605",
        segments=["20260605_152856"],
        scene_mode="out" if phase == NavigationTaskPhase.FINISH_PROCESSING else None,
        phase=phase,
    )


def _tools(tmp_path, *, task=None, settings=None):
    observation_store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    built = build_navigation_observation_tools(
        task=task or _task(),
        observation_store=observation_store,
        evidence_store=evidence_store,
        settings=settings or NavigationSettings(vladatasets_root=FIXTURE_ROOT),
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
        "get_phase_planning_context_tool",
        "list_observation_evidence_tool",
        "read_observation_evidence_tool",
        "describe_processing_action_tool",
    }
    assert not any("infer_" in name or "profile" in name for name in tools)
    assert all(tool.is_read_only for tool in tools.values())
    assert all("task_id" not in tool.input_schema.get("properties", {}) for tool in tools.values())


def test_stale_inspection_toolkit_cannot_write_revision_or_evidence_after_rebind(tmp_path):
    db_path = tmp_path / "state.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20270605",
        segments=["20260605_152856"],
        scene_mode=None,
        web_session_id="web-owner",
        agentscope_session_id="as-old",
    )
    task = task_store.update_task(task.task_id, phase="extract_sync")
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    tools = {
        tool.name: tool
        for tool in build_navigation_observation_tools(
            task=task,
            observation_store=observation_store,
            evidence_store=evidence_store,
            settings=NavigationSettings(vladatasets_root=FIXTURE_ROOT),
            expected_web_session_id="web-owner",
            expected_agentscope_session_id="as-old",
        )
    }
    task_store.create_or_update_task(
        date=task.date,
        segments=task.segments,
        scene_mode=None,
        web_session_id="web-owner",
        agentscope_session_id="as-new",
    )

    with pytest.raises(PermissionError, match="session mismatch"):
        _invoke_tool(tools["inspect_navigation_raw_metadata_tool"], {})

    assert observation_store.latest(task.task_id) is None
    assert not (tmp_path / "evidence" / task.task_id).exists()


def test_inspection_tool_returns_only_compact_delta_and_external_evidence(tmp_path):
    tools, observation_store, evidence_store = _tools(tmp_path)

    result = _invoke_tool(tools["inspect_navigation_raw_metadata_tool"], {})

    assert set(result) == INSPECTION_RESULT_KEYS
    assert result["ok"] is True
    assert result["observation_delta"]["kind"] == "raw_metadata"
    assert result["observation_revision"] == 1
    assert result["remaining_missing_observations"] == [
        "artifact_state",
        "sensor_candidates",
        "topic_candidates",
    ]
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
        date="20270605",
        segments=["segment_a"],
        phase=NavigationTaskPhase.EXTRACT_SYNC,
    )
    tools, observation_store, evidence_store = _tools(
        tmp_path,
        task=task,
        settings=NavigationSettings(vladatasets_root=root),
    )

    result = _invoke_tool(tools["inspect_navigation_topic_candidates_tool"], {})

    compact = result["observation_delta"]
    assert compact["kind"] == "topic_candidates"
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
    context = _invoke_tool(tools["get_phase_planning_context_tool"], {})
    assert context["observation_status"]["complete"] is True
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
        task=_task(NavigationTaskPhase.FINISH_PROCESSING),
        settings=settings,
    )

    calibration = _invoke_tool(tools["inspect_navigation_calibration_inventory_tool"], {})
    localization = _invoke_tool(tools["inspect_navigation_localization_sources_tool"], {})

    assert calibration["observation_delta"] == {
        "kind": "calibration_inventory",
        "sensor_source_count": 1,
        "sensor_sources_preview": ["NoobScenes/params/20260529_go2w/sensors"],
    }
    assert localization["observation_delta"] == {
        "kind": "localization_sources",
        "available_source_count": 1,
        "available_sources_preview": ["odom"],
        "conversion_available": True,
    }


def test_cognitive_tools_bind_task_paginate_evidence_and_describe_active_action(tmp_path):
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
    assert described["tool_name"] == "prepare_raw_data"
    assert described["phase"] == "extract_sync"
    assert described["executor_agent_allowed"] is True


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

    context = _invoke_tool(tools["get_phase_planning_context_tool"], {})
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 5_500
    assert context["evidence_next_cursor"] == len(context["evidence_catalog"])
    assert context["evidence_next_cursor"] < 30
