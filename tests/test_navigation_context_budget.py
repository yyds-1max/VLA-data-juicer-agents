import asyncio
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.services import build_navigation_services
from vla_data_juicer_agents.runtime.agentscope_prompts import navigation_agent_prompt
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GUIDANCE_PATH = REPOSITORY_ROOT / "docs" / "navigation-plan-agent-guidance.md"
SERVER_ACCEPTANCE_PATH = REPOSITORY_ROOT / "docs" / "navigation-plan-server-acceptance.md"


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
            web_session_id=session_id,
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


@dataclass(frozen=True)
class TurnMeasurement:
    name: str
    retained_history_chars: int
    current_schema_chars: int
    current_context_chars: int
    estimated_tokens: int


def test_navigation_guidance_has_exact_playbook_sections_and_four_bounded_few_shots():
    guidance = GUIDANCE_PATH.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", guidance, flags=re.MULTILINE)

    assert headings == [
        "Product dependency map",
        "Recommended investigation order",
        "Common extract-sync work",
        "Common finish-processing work",
        "Model/code decision ownership",
        "User-confirmation points",
        "Failure/retry behavior",
        "Four bounded few-shots",
    ]
    assert len(re.findall(r"^### Few-shot \d+: ", guidance, flags=re.MULTILINE)) == 4
    for required_case in [
        "user claims sync is complete",
        "new session",
        "extract/sync just completed",
        "invalid complete Plan",
    ]:
        assert required_case in guidance
    assert len(guidance) <= 12_000


def test_navigation_guidance_excludes_operator_acceptance_runbook():
    guidance = GUIDANCE_PATH.read_text(encoding="utf-8").lower()
    acceptance = SERVER_ACCEPTANCE_PATH.read_text(encoding="utf-8").lower()
    operator_topics = [
        "deployment synchronization",
        "git checks",
        "token measurement",
        "server log queries",
        "real-data acceptance",
        "legacy storage cleanup",
    ]

    for topic in operator_topics:
        assert topic not in guidance
        assert topic in acceptance


def test_compact_navigation_anchor_contains_only_durable_execution_coordinates():
    task = SimpleNamespace(
        task_id="attempt-1",
        accepted_plan_phase=SimpleNamespace(value="extract_sync"),
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        plan_revision=3,
    )
    services = SimpleNamespace(
        task_store=SimpleNamespace(find_by_session=lambda **_kwargs: task),
        observation_store=SimpleNamespace(latest=lambda _task_id: SimpleNamespace(revision=7)),
        plan_store=SimpleNamespace(
            get_active=lambda _task_id, _phase: plan,
            get_current_step=lambda _plan_id: {
                "step": {"step_id": "sync", "status": "running"}
            },
        ),
    )
    runtime = object.__new__(AgentScopeRuntime)
    runtime._navigation_services = lambda: services

    anchor = runtime._navigation_durable_state_anchor("as-1", web_session_id="web-1")

    assert anchor == {
        "task_attempt_id": "attempt-1",
        "observation_revision": 7,
        "accepted_plan_id": "plan-1",
        "accepted_plan_revision": 3,
        "current_ledger_step": "sync",
        "execution_status": "running",
    }


def test_static_prompt_guidance_submission_schemas_and_anchor_fit_context_budget(tmp_path):
    services = build_navigation_services(tmp_path)
    session_id = "static-budget"
    services.task_store.create_task_attempt(
        request="Process navigation data.",
        target="20260710",
        date="20260710",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id=session_id,
        agentscope_session_id=session_id,
    )
    tools = _tool_map(services, session_id)
    submission_names = {
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    }
    assert submission_names <= tools.keys()
    schemas = json.dumps(
        [
            {
                "name": tools[name].name,
                "description": tools[name].description,
                "input_schema": tools[name].input_schema,
            }
            for name in sorted(submission_names)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    compact_anchor = json.dumps(
        {
            "task_attempt_id": "attempt-1",
            "observation_revision": 7,
            "accepted_plan_id": "plan-1",
            "accepted_plan_revision": 3,
            "current_ledger_step": "sync",
            "execution_status": "running",
        },
        separators=(",", ":"),
    )
    static_context = "\n".join(
        [
            navigation_agent_prompt(),
            GUIDANCE_PATH.read_text(encoding="utf-8"),
            schemas,
            compact_anchor,
        ]
    )

    assert (len(static_context) + 3) // 4 <= 83_885


def test_representative_model_authored_transcript_stays_bounded_without_compaction(tmp_path):
    # This models four real AgentScope invocations. The runtime refreshes extra
    # tools only at an invocation boundary, appends one durable anchor to that
    # invocation's new user message, and retains that message and tool results.
    # Tool schemas belong only to the current invocation and never enter history.
    date, segment, session_id = "20260710", "20260710_120000", "direct-budget"
    token_limit = 83_885
    data_root = tmp_path / "VLADatasets"
    _write_raw_metadata(data_root, date, segment)
    services = build_navigation_services(
        tmp_path,
        NavigationSettings(vladatasets_root=data_root),
    )
    system_chars = len(navigation_agent_prompt()) + 1 + len(
        GUIDANCE_PATH.read_text(encoding="utf-8")
    )
    retained_history: list[str] = []
    transcript: list[tuple[str, str]] = []
    turn_measurements: list[TurnMeasurement] = []
    compact_events: list[dict[str, int | str]] = []
    external_invocations: list[str] = []
    result_counts: dict[tuple[str, str], int] = {}
    current_invocation = ""
    current_schema_chars = 0

    def current_anchor():
        task = services.task_store.find_latest_by_agentscope_session(session_id)
        observation = services.observation_store.latest(task.task_id) if task else None
        plan = (
            services.plan_store.get_active(
                task.task_id,
                task.accepted_plan_phase.value,
            )
            if task
            and task.accepted_plan_phase is not None
            and task.accepted_plan_phase.value in {"extract_sync", "finish_processing"}
            else None
        )
        current = services.plan_store.get_current_step(plan.plan_id) if plan else None
        return {
            "task_attempt_id": task.task_id if task else None,
            "observation_revision": observation.revision if observation else None,
            "accepted_plan_id": plan.plan_id if plan else None,
            "accepted_plan_revision": plan.plan_revision if plan else None,
            "current_ledger_step": current["step"]["step_id"] if current else None,
            "execution_status": (
                current["step"]["status"] if current else getattr(plan, "status", None)
            ),
        }

    def measure_model_call(label):
        retained_chars = sum(len(message) for message in retained_history)
        context_chars = system_chars + retained_chars + current_schema_chars
        estimated_tokens = (context_chars + 3) // 4
        measurement = TurnMeasurement(
            name=label,
            retained_history_chars=retained_chars,
            current_schema_chars=current_schema_chars,
            current_context_chars=context_chars,
            estimated_tokens=estimated_tokens,
        )
        turn_measurements.append(measurement)
        if estimated_tokens >= token_limit:
            compact_events.append(
                {"turn": label, "estimated_tokens": estimated_tokens}
            )

    def start_invocation(name, user_text):
        nonlocal current_invocation, current_schema_chars
        tools = _tool_map(services, session_id)
        user_message = json.dumps(
            {
                "role": "user",
                "content": user_text,
                "durable_navigation_state": current_anchor(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        retained_history.append(user_message)
        schema_payload = [
            {
                "name": tool.name,
                "description": getattr(tool, "description", ""),
                "input_schema": getattr(tool, "input_schema", {}),
            }
            for tool in tools.values()
        ]
        encoded_schemas = json.dumps(
            schema_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert "schema_snapshot" not in encoded_schemas
        assert "data_profile_draft" not in encoded_schemas
        external_invocations.append(name)
        current_invocation = name
        current_schema_chars = len(encoded_schemas)
        measure_model_call(f"{name}:start")
        return tools

    def record(kind, payload, hard_max):
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert len(encoded_payload) <= hard_max
        assert "schema_snapshot" not in encoded_payload
        assert "data_profile_draft" not in encoded_payload
        assert "validation_history" not in encoded_payload
        transcript.append((kind, encoded_payload))
        retained_history.append(
            json.dumps(
                {"role": "tool", "name": kind, "content": payload},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        count_key = (current_invocation, kind)
        result_counts[count_key] = result_counts.get(count_key, 0) + 1
        measure_model_call(
            f"{current_invocation}:after:{kind}:{result_counts[count_key]}"
        )

    entered_task = services.task_store.create_task_attempt(
        request="Process the selected navigation dataset.",
        target=f"{date}/{segment}",
        date=date,
        segments=[segment],
        scene_mode=None,
        dry_run=True,
        web_session_id=session_id,
        agentscope_session_id=session_id,
    ).task
    entry_tools = start_invocation("entry", "Process the selected navigation dataset.")
    entry = _call(
        entry_tools["record_navigation_user_guidance_tool"],
        text="Process the selected navigation dataset.",
    )
    record("entry", entry, 4_000)

    inspection_tools = start_invocation(
        "inspection",
        "Continue from durable state and record every required factual observation.",
    )
    inspection_names = sorted(
        name
        for name in inspection_tools
        if name.startswith("inspect_navigation_")
    )
    assert inspection_names
    for name in inspection_names:
        record("inspection", _call(inspection_tools[name]), 4_000)

    planning_tools = start_invocation(
        "planning",
        "Use the completed facts to submit one complete extract-sync plan.",
    )
    context = _call(planning_tools["get_navigation_task_context_tool"])
    record("planning_context", context, 5_500)
    evidence_list = _call(
        planning_tools["list_observation_evidence_tool"],
        limit=20,
    )
    record("evidence_list", evidence_list, 5_500)
    first_ref = evidence_list["evidence"][0]["ref"]
    record(
        "evidence_detail",
        _call(
            planning_tools["read_observation_evidence_tool"],
            ref=first_ref,
            limit=10,
        ),
        5_500,
    )
    evidence_by_kind = {
        row.kind: row.ref
        for row in services.observation_store.list_evidence(
            entered_task.task_id,
            limit=50,
        )
    }
    valid_plan = _extract_plan(evidence_by_kind)
    invalid_plan = copy.deepcopy(valid_plan)
    invalid_plan["decisions"]["time_sync"]["reference_sensor"] = (
        "unobserved_sensor"
    )
    submit_name = "submit_extract_sync_plan_tool"
    invalid = _call(
        planning_tools[submit_name],
        planning_context_revision=context["planning_context_revision"],
        plan=invalid_plan,
    )
    assert invalid["ok"] is False
    record("submit_failure", invalid, 3_000)
    success = _call(
        planning_tools[submit_name],
        planning_context_revision=context["planning_context_revision"],
        plan=valid_plan,
    )
    assert success["ok"] is True
    record("submit_success", success, 2_000)

    execution_tools = start_invocation(
        "execution",
        "Execute every remaining stored plan step in order.",
    )
    while services.plan_store.get(success["plan_id"]).status == "active":
        current = services.plan_store.get_current_step(success["plan_id"])
        record(
            "execution_overview",
            _call(
                execution_tools["get_plan_execution_overview_tool"],
                plan_id=success["plan_id"],
            ),
            4_000,
        )
        step = current["step"]
        result = _call(
            execution_tools[f"{step['action']}_tool"],
            plan_id=success["plan_id"],
            step_id=step["step_id"],
        )
        record("processing_result", result, 4_000)

    peak = max(turn_measurements, key=lambda turn: turn.estimated_tokens)
    metrics = {
        "invocations": external_invocations,
        "measurement_labels": [turn.name for turn in turn_measurements],
        "peak_turn": peak.name,
        "peak_estimated_tokens": peak.estimated_tokens,
        "compact_events": compact_events,
    }
    assert metrics["invocations"] == ["entry", "inspection", "planning", "execution"]
    assert any(":after:" in turn.name for turn in turn_measurements)
    assert ":after:" in metrics["peak_turn"]
    assert metrics["peak_estimated_tokens"] == max(
        turn.estimated_tokens for turn in turn_measurements
    )
    assert metrics["peak_estimated_tokens"] < token_limit
    assert metrics["compact_events"] == []
