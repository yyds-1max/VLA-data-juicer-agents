import asyncio
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

from navigation_agentscope_harness import (
    ScriptedChatModel,
    build_agent,
    build_runtime,
    event_types,
    refresh_tools,
    run_reply,
    text_deltas,
    tool_call_names,
    tool_result_outputs,
)
from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.services import build_navigation_services
from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GUIDANCE_PATH = REPOSITORY_ROOT / "docs" / "navigation-plan-agent-guidance.md"
SERVER_ACCEPTANCE_PATH = REPOSITORY_ROOT / "docs" / "navigation-plan-server-acceptance.md"


class _BootstrapStorage:
    def __init__(self):
        self.agents = []

    async def upsert_credential(self, _user_id, credential):
        return credential.id

    async def upsert_agent(self, _user_id, record):
        self.agents.append(record)
        return record.id


def _production_agent_prompts(workspace_root):
    storage = _BootstrapStorage()
    config = AgentScopeRuntimeConfig(
        user_id="budget-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=workspace_root,
        dashscope_api_key="budget-key",
        dashscope_base_url=None,
        default_model="budget-default",
        router_model="budget-router",
        navigation_model="budget-navigation",
    )
    asyncio.run(bootstrap_agentscope_records(storage, config))
    return {
        record.data.name: record.data.system_prompt
        for record in storage.agents
    }


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


def test_server_acceptance_requires_safe_execution_mode_and_attended_gui_boundary():
    acceptance = SERVER_ACCEPTANCE_PATH.read_text(encoding="utf-8")

    assert "`dry_run=False`" in acceptance
    assert "`dry_run=True` only for an explicitly selected test run" in acceptance
    assert "GUI/human steps" in acceptance
    assert "the user is present" in acceptance
    assert "explicitly asked to continue" in acceptance


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
    prompts = _production_agent_prompts(tmp_path)
    production_navigation_prompt = prompts["NavigationDataAgent"]
    assert production_navigation_prompt.count("# Navigation Plan Agent Guidance") == 1
    assert "# Navigation Plan Agent Guidance" not in prompts["MainRouterAgent"]
    static_context = "\n".join(
        [production_navigation_prompt, schemas, compact_anchor]
    )

    assert (len(static_context) + 3) // 4 <= 83_885


def _latest_tool_json(messages: list[Msg], required_key: str | None = None):
    for message in reversed(messages):
        for block in reversed(message.get_content_blocks("tool_result")):
            if isinstance(block.output, str):
                text = block.output
            else:
                text = "".join(
                    item.text for item in block.output if isinstance(item, TextBlock)
                )
            payload = json.loads(text)
            if required_key is None or required_key in payload:
                return payload
    raise AssertionError(f"no tool result contains {required_key!r}")


@pytest.mark.asyncio
async def test_representative_model_authored_transcript_stays_bounded_without_compaction(
    monkeypatch,
    tmp_path,
):
    date, segment = "20260710", "20260710_120000"
    web_session_id = "web-budget"
    agentscope_session_id = "as-budget"
    token_limit = 83_885
    data_root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(data_root))
    monkeypatch.setenv("VLA_PROCESSING_ROOT", str(processing_root))
    _write_raw_metadata(data_root, date, segment)

    runtime, storage, _registry = await build_runtime(tmp_path, dry_run=True)
    services = runtime._navigation_services()
    task = services.task_store.create_task_attempt(
        request="Process the selected navigation dataset.",
        target=f"{date}/{segment}",
        date=date,
        segments=[segment],
        scene_mode=None,
        dry_run=True,
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    ).task
    model = ScriptedChatModel()
    agent = build_agent(
        storage.agents[runtime.config.navigation_agent_id],
        model,
        runtime._navigation_tools_for_session(
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
        ),
    )

    inspection_names = [
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_topic_candidates_tool",
        "inspect_navigation_sensor_candidates_tool",
    ]
    for name in inspection_names:
        model.enqueue_tool(name, {})
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool("list_observation_evidence_tool", {"limit": 20})
    model.enqueue_tool(
        "read_observation_evidence_tool",
        lambda messages: {
            "ref": _latest_tool_json(messages, "evidence")["evidence"][0]["ref"],
            "limit": 10,
        },
    )

    def submission(messages: list[Msg], *, valid: bool):
        evidence_by_kind = {
            row.kind: row.ref
            for row in services.observation_store.list_evidence(task.task_id, limit=50)
        }
        plan = _extract_plan(evidence_by_kind)
        if not valid:
            plan = copy.deepcopy(plan)
            plan["decisions"]["time_sync"]["reference_sensor"] = "unobserved_sensor"
        return {
            "planning_context_revision": _latest_tool_json(
                messages,
                "planning_context_revision",
            )["planning_context_revision"],
            "plan": plan,
        }

    submit_name = "submit_extract_sync_plan_tool"
    model.enqueue_tool(submit_name, lambda messages: submission(messages, valid=False))
    model.enqueue_tool(submit_name, lambda messages: submission(messages, valid=True))
    planning_final = "The corrected complete extract-sync Plan was accepted."
    model.enqueue_text(planning_final)

    planning_events = await run_reply(
        agent,
        "Inspect durable facts, check evidence, and submit a complete Plan.",
    )
    planning_calls = tool_call_names(agent)
    assert planning_calls == [
        *inspection_names,
        "get_navigation_task_context_tool",
        "list_observation_evidence_tool",
        "read_observation_evidence_tool",
        submit_name,
        submit_name,
    ]
    assert "TOOL_RESULT_END" in event_types(planning_events)
    assert "REPLY_END" in event_types(planning_events)
    assert text_deltas(planning_events) == planning_final

    planning_results = tool_result_outputs(agent)
    invalid_result = json.loads(planning_results[-2])
    success = json.loads(planning_results[-1])
    assert invalid_result["ok"] is False
    assert invalid_result["error_type"] == "plan_validation_failed"
    assert success["ok"] is True
    plan_id = success["plan_id"]

    refresh_tools(
        agent,
        runtime._navigation_tools_for_session(
            web_session_id=web_session_id,
            agentscope_session_id=agentscope_session_id,
        ),
    )
    execution_start = len(tool_call_names(agent))
    model.enqueue_tool("get_plan_execution_overview_tool", {"plan_id": plan_id})
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_tool("get_plan_execution_overview_tool", {"plan_id": plan_id})
    model.enqueue_tool(
        "extract_and_sync_navigation_data_tool",
        {"plan_id": plan_id, "step_id": "extract_sync"},
    )
    execution_final = "Executed the accepted Plan and recorded its final ledger state."
    model.enqueue_text(execution_final)
    execution_events = await run_reply(
        agent,
        "Execute every remaining stored plan step in order.",
    )
    assert tool_call_names(agent)[execution_start:] == [
        "get_plan_execution_overview_tool",
        "prepare_raw_data_tool",
        "get_plan_execution_overview_tool",
        "extract_and_sync_navigation_data_tool",
    ]
    assert "TOOL_RESULT_END" in event_types(execution_events)
    assert "REPLY_END" in event_types(execution_events)
    assert text_deltas(execution_events) == execution_final
    assert services.plan_store.get(plan_id).status == "completed"
    model.assert_exhausted()

    all_results = tool_result_outputs(agent)
    result_measurements = [
        {
            "tool_name": name,
            "serialized_chars": len(output),
        }
        for name, output in zip(tool_call_names(agent), all_results, strict=True)
    ]
    schema_chars = [
        len(json.dumps(call.tools, ensure_ascii=False, separators=(",", ":")))
        for call in model.invocations
    ]
    assistant_blocks = [
        block
        for invocation in model.invocations
        for block in invocation.response_blocks
    ]
    final_texts = [
        block["text"] for block in assistant_blocks if block["type"] == "text"
    ]
    metrics = {
        "model_invocation_count": len(model.invocations),
        "peak_model_input_tokens": max(
            invocation.input_tokens for invocation in model.invocations
        ),
        "per_call_model_input_tokens": [
            invocation.input_tokens for invocation in model.invocations
        ],
        "formatted_message_counts": [
            len(invocation.formatted_messages) for invocation in model.invocations
        ],
        "exposed_tool_schema_chars": schema_chars,
        "tool_result_chars": result_measurements,
        "max_tool_result_chars": max(
            item["serialized_chars"] for item in result_measurements
        ),
        "validation_failure_chars": len(planning_results[-2]),
        "compact_events": model.compact_events,
        "compact_event_count": model.compact_event_count,
    }
    assert final_texts == [planning_final, execution_final]
    assert metrics["model_invocation_count"] == len(assistant_blocks)
    assert all(metrics["formatted_message_counts"])
    assert all(chars > 0 for chars in metrics["exposed_tool_schema_chars"])
    assert all(tokens > 0 for tokens in metrics["per_call_model_input_tokens"])
    assert metrics["max_tool_result_chars"] <= 5_500
    assert metrics["validation_failure_chars"] <= 3_000
    assert metrics["peak_model_input_tokens"] <= token_limit
    assert metrics["compact_event_count"] == 0
    assert metrics["compact_events"] == []
    assert not getattr(agent.state, "summary", None)
