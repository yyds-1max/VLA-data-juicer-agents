import asyncio
import copy
import json

from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.services import build_navigation_services
from vla_data_juicer_agents.runtime.agentscope_prompts import navigation_agent_prompt


def _decode(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    if hasattr(value, "content"):
        return _decode(value.content)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return json.loads("".join(block.text for block in value if hasattr(block, "text")))
    raise TypeError(type(value))


def _call(tool, **kwargs):
    return _decode(asyncio.run(tool(**kwargs)))


def _tool_map(services, session_id):
    return {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id=session_id,
            cancellation=None,
        )
    }


def _write_raw_metadata(root, date, segment):
    path = root / "raw_data" / date / segment / "metadata.yaml"
    path.parent.mkdir(parents=True)
    topics = [
        ("/cam_video4/csi_cam/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/rs32_lidar_points", "sensor_msgs/msg/PointCloud2"),
        ("/sport_odom", "nav_msgs/msg/Odometry"),
    ]
    entries = "\n".join(
        "    - topic_metadata:\n"
        f"        name: {name}\n"
        f"        type: {message_type}\n"
        "      message_count: 100"
        for name, message_type in topics
    )
    path.write_text(
        "rosbag2_bagfile_information:\n  topics_with_message_count:\n" + entries + "\n",
        encoding="utf-8",
    )


def _extract_plan(evidence_by_kind):
    return {
        "decisions": {
            "sensor_bindings": {
                "bindings": {
                    "fisheye_front": "/cam_video4/csi_cam/image_raw/compressed",
                    "lidar": "/rs32_lidar_points",
                    "odom": "/sport_odom",
                },
                "reason": "Use measured role candidates.",
                "evidence_refs": [evidence_by_kind["sensor_candidates"]],
            },
            "topic_selection": {
                "topic_whitelist": [
                    "/cam_video4/csi_cam/image_raw/compressed",
                    "/rs32_lidar_points",
                    "/sport_odom",
                ],
                "topic_map": {
                    "/cam_video4/csi_cam/image_raw/compressed": "fisheye_front",
                    "/rs32_lidar_points": "lidar",
                    "/sport_odom": "odom",
                },
                "query_dir": "rs32_lidar_points",
                "reason": "Use only observed topics.",
                "evidence_refs": [evidence_by_kind["topic_candidates"]],
            },
            "time_sync": {
                "reference_sensor": "lidar",
                "method": "nearest_timestamp",
                "tolerance_ms": 50,
                "reason": "Use lidar as the model-selected reference.",
                "evidence_refs": [evidence_by_kind["raw_metadata"]],
            },
        },
        "steps": [
            {
                "step_id": "prepare_raw",
                "action": "prepare_raw_data",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": [],
            },
            {
                "step_id": "extract_sync",
                "action": "extract_and_sync_navigation_data",
                "variant": "explicit_topic_params",
                "arguments": {"processes_num": 2},
                "depends_on": ["prepare_raw"],
                "failure_policy": "stop",
                "decision_refs": ["sensor_bindings", "topic_selection", "time_sync"],
            },
        ],
    }


def test_representative_model_authored_transcript_stays_bounded_without_compaction(tmp_path):
    date, segment, session_id = "20260710", "20260710_120000", "direct-budget"
    data_root = tmp_path / "VLADatasets"
    _write_raw_metadata(data_root, date, segment)
    services = build_navigation_services(
        tmp_path,
        NavigationSettings(vladatasets_root=data_root),
    )
    transcript = []
    peak_application_chars = len(navigation_agent_prompt())

    def record(kind, payload, hard_max):
        nonlocal peak_application_chars
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        assert len(encoded) <= hard_max
        assert "schema_snapshot" not in encoded
        assert "data_profile_draft" not in encoded
        assert "validation_history" not in encoded
        transcript.append((kind, encoded))
        peak_application_chars += len(encoded)

    def resolve_and_record_schemas():
        nonlocal peak_application_chars
        resolved = _tool_map(services, session_id)
        encoded = json.dumps(
            [getattr(tool, "input_schema", {}) for tool in resolved.values()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert "schema_snapshot" not in encoded
        assert "data_profile_draft" not in encoded
        peak_application_chars += len(encoded)
        task = services.task_store.find_latest_by_agentscope_session(session_id)
        observation = services.observation_store.latest(task.task_id) if task else None
        plan = (
            services.plan_store.get_active(task.task_id, task.phase.value)
            if task and task.phase.value in {"extract_sync", "finish_processing"}
            else None
        )
        current = services.plan_store.get_current_step(plan.plan_id) if plan else None
        anchor = {
            "task_id": task.task_id if task else None,
            "phase": task.phase.value if task else None,
            "task_status": task.status.value if task else None,
            "observation_revision": observation.revision if observation else None,
            "active_plan_id": plan.plan_id if plan else None,
            "active_plan_revision": plan.plan_revision if plan else None,
            "current_step_id": current["step"]["step_id"] if current else None,
        }
        peak_application_chars += len(
            json.dumps(anchor, ensure_ascii=False, separators=(",", ":"))
        )
        return resolved

    tools = resolve_and_record_schemas()
    entry = _call(
        tools["get_or_create_navigation_task_tool"],
        date=date,
        segments=[segment],
        scene_mode=None,
    )
    record("entry", entry, 4_000)
    entered_task = services.task_store.get_task(entry["task"]["task_id"])
    services.task_store.update_task_for_session(
        entered_task.task_id,
        web_session_id=None,
        agentscope_session_id=session_id,
        expected_state_revision=entered_task.state_revision,
        dry_run=True,
    )

    while True:
        tools = resolve_and_record_schemas()
        inspection_names = sorted(name for name in tools if name.startswith("inspect_navigation_"))
        if not inspection_names:
            break
        for name in inspection_names:
            record("inspection", _call(tools[name]), 4_000)

    tools = resolve_and_record_schemas()
    context = _call(tools["get_phase_planning_context_tool"])
    record("planning_context", context, 5_500)
    evidence_list = _call(tools["list_observation_evidence_tool"], limit=20)
    record("evidence_list", evidence_list, 5_500)
    first_ref = evidence_list["evidence"][0]["ref"]
    record(
        "evidence_detail",
        _call(tools["read_observation_evidence_tool"], ref=first_ref, limit=10),
        5_500,
    )
    evidence_by_kind = {
        row.kind: row.ref for row in services.observation_store.list_evidence(entry["task"]["task_id"], limit=50)
    }
    valid_plan = _extract_plan(evidence_by_kind)
    invalid_plan = copy.deepcopy(valid_plan)
    invalid_plan["decisions"]["time_sync"]["reference_sensor"] = "unobserved_sensor"
    submit_name = "submit_extract_sync_plan_tool"
    invalid = _call(
        tools[submit_name],
        planning_context_revision=context["planning_context_revision"],
        plan=invalid_plan,
    )
    assert invalid["ok"] is False
    record("submit_failure", invalid, 3_000)
    success = _call(
        tools[submit_name],
        planning_context_revision=context["planning_context_revision"],
        plan=valid_plan,
    )
    assert success["ok"] is True
    record("submit_success", success, 2_000)

    while True:
        tools = resolve_and_record_schemas()
        plan = services.plan_store.get(success["plan_id"])
        current = services.plan_store.get_current_step(success["plan_id"])
        if current is None or plan.status != "active":
            break
        record(
            "execution_overview",
            _call(tools["get_plan_execution_overview_tool"], plan_id=success["plan_id"]),
            4_000,
        )
        step = current["step"]
        result = _call(
            tools[f"{step['action']}_tool"],
            plan_id=success["plan_id"],
            step_id=step["step_id"],
        )
        record("processing_result", result, 4_000)

    estimated_tokens = (peak_application_chars + 3) // 4
    assert estimated_tokens < 83_885
    assert all(kind != "compact" for kind, _ in transcript)
