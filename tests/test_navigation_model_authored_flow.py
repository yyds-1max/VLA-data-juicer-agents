from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

import vla_data_juicer_agents.navigation.plan_execution as plan_execution
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
from test_navigation_context_budget import _call, _extract_plan, _write_raw_metadata
from vla_data_juicer_agents.navigation.models import ToolResult
from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    AgentScopeRuntime,
)


DATE = "20260710"
SEGMENT = "20260710_120000"
EXTRACT_INSPECTIONS = [
    "inspect_navigation_artifact_state_tool",
    "inspect_navigation_raw_metadata_tool",
    "inspect_navigation_topic_candidates_tool",
    "inspect_navigation_sensor_candidates_tool",
]
FINISH_INSPECTIONS = [
    "inspect_navigation_artifact_state_tool",
    "inspect_navigation_gridmap_artifacts_tool",
    "inspect_navigation_runtime_assets_tool",
    "inspect_navigation_calibration_inventory_tool",
    "inspect_navigation_localization_sources_tool",
    "inspect_navigation_annotation_job_facts_tool",
]


def _tool_result_json(messages: list[Msg]) -> dict:
    for message in reversed(messages):
        blocks = message.get_content_blocks("tool_result")
        if not blocks:
            continue
        output = blocks[-1].output
        if isinstance(output, str):
            text = output
        else:
            text = "".join(
                item.text for item in output if isinstance(item, TextBlock)
            )
        return json.loads(text)
    raise AssertionError("scripted model expected a prior tool result")


def _evidence_by_kind(services, task_id):
    return {
        descriptor.kind: descriptor.ref
        for descriptor in services.observation_store.list_evidence(task_id, limit=50)
    }


def _missing_annotation_gateway():
    return SimpleNamespace(
        get_processing_facts=lambda **_kwargs: {
            "job_status": "missing",
            "segment_count": 0,
            "tracked_count": 0,
            "skipped_count": 0,
            "annotated_count": 0,
            "ready_for_postprocessing": False,
            "ready_for_trajectory_review": False,
            "processing_calibration_snapshot_available": False,
            "reviews": {},
        }
    )


def _activate_extract_plan(services, task, session_id, web_session_id="direct-flow"):
    """Compatibility fixture used by the direct CLI integration test."""
    for name in EXTRACT_INSPECTIONS:
        tools = {
            tool.name: tool
            for tool in resolve_navigation_agent_tools(
                services=services,
                agentscope_session_id=session_id,
                web_session_id=web_session_id,
                cancellation=None,
            )
        }
        assert _call(tools[name])["ok"] is True
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id=session_id,
            web_session_id=web_session_id,
            cancellation=None,
        )
    }
    context = _call(tools["get_navigation_task_context_tool"])
    result = _call(
        tools["submit_extract_sync_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=_extract_plan(_evidence_by_kind(services, task.task_id)),
    )
    assert result["ok"] is True
    return result


def _finish_plan(evidence):
    return {
        "decisions": {
            "localization": {
                "source": "odom",
                "conversion": "odom_to_ins",
                "reason": "The observed odometry converter is available.",
                "evidence_refs": [evidence["localization_sources"]],
            },
            "gridmap": {
                "source": "existing_gridmap",
                "reason": "Use the measured existing grid map.",
                "evidence_refs": [evidence["gridmap_artifacts"]],
            },
            "calibration": {
                "mode": "selected_profile",
                "selected_sensor_source": None,
                "requires_user_confirmation": True,
                "reason": "Defer the observed profile choice to confirmation.",
                "evidence_refs": [evidence["calibration_inventory"]],
            },
        },
        "steps": [
            {
                "step_id": "confirm_calibration",
                "action": "confirm_navigation_calibration_params",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": ["calibration"],
            },
            {
                "step_id": "annotation_tracking",
                "action": "run_annotation_tracking_workflow",
                "variant": "durable_web_handoff",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["calibration", "localization"],
            },
            {
                "step_id": "annotation_postprocessing",
                "action": "run_annotation_postprocessing_workflow",
                "variant": "plan_bound_runtime",
                "arguments": {},
                "depends_on": ["annotation_tracking"],
                "failure_policy": "stop",
                "decision_refs": ["calibration", "localization", "gridmap"],
            },
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": ["annotation_postprocessing"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


def _write_finish_inputs(settings):
    sync_clip = settings.clip_data_root / DATE / SEGMENT / "sync_data" / "clip-1"
    sync_gridmap = sync_clip / "grid_map"
    sync_gridmap.mkdir(parents=True, exist_ok=True)
    (sync_gridmap / "map.json").write_text("{}", encoding="utf-8")
    for dirname in ("fisheye_front", "r32_rslidar_points"):
        sensor_dir = sync_clip / dirname
        sensor_dir.mkdir(parents=True, exist_ok=True)
        (sensor_dir / "1000.000000.data").write_text("data", encoding="utf-8")
    sensors = settings.processing_root / "NoobScenes" / "params" / "selected" / "sensors"
    sensors.mkdir(
        parents=True,
        exist_ok=True,
    )
    (sensors / "fisheye_front.json").write_text("{}", encoding="utf-8")
    (sensors / "r32_rslidar_points.json").write_text("{}", encoding="utf-8")
    converter = settings.processing_root / "NoobScenes" / "include" / "1_odom_convert.py"
    converter.parent.mkdir(parents=True, exist_ok=True)
    converter.write_text("# converter", encoding="utf-8")
    (settings.processing_root / "NoobScenes" / "main_smart_odom.py").write_text(
        "# odom", encoding="utf-8"
    )
    projection = settings.processing_root / "2_pt_project" / "2_othermethod_cjl_0525.py"
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text("# projection", encoding="utf-8")
    (projection.parent / "4_speed_direction_odom.py").write_text(
        "# odom speed", encoding="utf-8"
    )
    gen_box = settings.processing_root / "0_1th_box" / "gen_box.py"
    gen_box.parent.mkdir(parents=True, exist_ok=True)
    gen_box.write_text("# annotation", encoding="utf-8")


async def _create_navigation_attempt(runtime, web_session_id, request):
    """Create the specialist fixture without exercising the removed Router V0 path."""
    await runtime.ensure_bootstrapped()
    agentscope_session_id = f"{web_session_id}__navigation-data-agent"
    task = runtime._navigation_task_store().create_task_attempt(
        request=request,
        target=DATE,
        date=DATE,
        segments=[SEGMENT],
        scene_mode="out" if "室外" in request else None,
        dry_run=runtime.config.navigation_dry_run,
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    ).task
    return task, agentscope_session_id


def _direct_flat_navigation_agent(runtime, storage, web_session_id, session_id):
    """Build the direct model/CLI adapter path, not Web ChatService assembly."""
    model = ScriptedChatModel()
    agent = build_agent(
        storage.agents[runtime.config.navigation_agent_id],
        model,
        runtime._navigation_tools_for_session(
            web_session_id=web_session_id,
            agentscope_session_id=session_id,
        ),
    )
    return agent, model


def _refresh_direct_flat_tools(runtime, agent, web_session_id, session_id):
    """Refresh only the direct flat adapter; Web uses system-managed middleware."""
    refresh_tools(
        agent,
        runtime._navigation_tools_for_session(
            web_session_id=web_session_id,
            agentscope_session_id=session_id,
        ),
    )


def _queue_extract_plan(model, services, task):
    for name in EXTRACT_INSPECTIONS:
        model.enqueue_tool(name, {})
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool(
        "submit_extract_sync_plan_tool",
        lambda messages: {
            "planning_context_revision": _tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": _extract_plan(_evidence_by_kind(services, task.task_id)),
        },
    )
    model.enqueue_text("完整 extract-sync Plan 已接受。")


async def _submit_extract_plan(agent, model, services, task, request):
    before = len(tool_call_names(agent))
    _queue_extract_plan(model, services, task)
    events = await run_reply(agent, request)
    calls = tool_call_names(agent)[before:]
    assert calls == [
        *EXTRACT_INSPECTIONS,
        "get_navigation_task_context_tool",
        "submit_extract_sync_plan_tool",
    ]
    assert "REPLY_END" in event_types(events)
    assert text_deltas(events) == "完整 extract-sync Plan 已接受。"
    active = services.plan_store.get_active_for_task(task.task_id)
    assert active is not None and active.phase == "extract_sync"
    return active, events


async def _execute_extract_plan(
    runtime,
    agent,
    model,
    web_session_id,
    session_id,
    plan,
):
    _refresh_direct_flat_tools(
        runtime,
        agent,
        web_session_id,
        session_id,
    )
    before = len(tool_call_names(agent))
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_tool(
        "extract_and_sync_navigation_data_tool",
        {"plan_id": plan.plan_id, "step_id": "extract_sync"},
    )
    model.enqueue_text("extract-sync 执行完成。")
    events = await run_reply(agent, "执行已接受的 Plan。")
    assert tool_call_names(agent)[before:] == [
        "prepare_raw_data_tool",
        "extract_and_sync_navigation_data_tool",
    ]
    assert text_deltas(events) == "extract-sync 执行完成。"
    return events


@pytest.fixture
def navigation_environment(monkeypatch, tmp_path):
    data_root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(data_root))
    monkeypatch.setenv("VLA_PROCESSING_ROOT", str(processing_root))
    _write_raw_metadata(data_root, DATE, SEGMENT)
    return tmp_path, data_root, processing_root


@pytest.mark.asyncio
async def test_raw_only_router_and_navigation_agents_submit_and_execute_canonical_plan(
    monkeypatch,
    navigation_environment,
):
    tmp_path, _data_root, _processing_root = navigation_environment
    runtime, storage, registry = await build_runtime(tmp_path, dry_run=True)
    task, session_id = await _create_navigation_attempt(
        runtime, "web-raw", "处理这批导航数据"
    )
    services = runtime._navigation_services()
    assert services.observation_store.latest(task.task_id) is None
    assert not hasattr(task, "phase")
    agent, model = _direct_flat_navigation_agent(
        runtime, storage, "web-raw", session_id
    )
    plan, _planning_events = await _submit_extract_plan(
        agent,
        model,
        services,
        task,
        "检查当前事实并提交完整 Plan。",
    )

    captured = {}

    def record(action):
        def run(**kwargs):
            captured[action] = kwargs
            return ToolResult(ok=True, tool_name=action, message="completed")

        return run

    monkeypatch.setattr(plan_execution, "prepare_raw_data", record("prepare_raw_data"))
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        record("extract_and_sync_navigation_data"),
    )
    await _execute_extract_plan(runtime, agent, model, "web-raw", session_id, plan)

    assert captured["prepare_raw_data"] == {
        "date": DATE,
        "segments": [SEGMENT],
        "settings": services.settings,
        "dry_run": True,
    }
    assert captured["extract_and_sync_navigation_data"] == {
        "date": DATE,
        "segments": [SEGMENT],
        "processes_num": 2,
        "topic_whitelist": [
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "rs32_lidar_points",
        "settings": services.settings,
        "dry_run": True,
    }
    assert services.plan_store.get(plan.plan_id).status == "completed"
    assert services.task_store.get_task(task.task_id).status == NavigationTaskStatus.ACTIVE
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_same_session_agent_asks_before_finish_then_reinspects_after_user_reply(
    navigation_environment,
):
    tmp_path, _data_root, _processing_root = navigation_environment
    runtime, storage, registry = await build_runtime(tmp_path, dry_run=True)
    runtime.set_annotation_gateway(_missing_annotation_gateway())
    task, session_id = await _create_navigation_attempt(
        runtime, "web-same", "处理这批导航数据"
    )
    services = runtime._navigation_services()
    agent, model = _direct_flat_navigation_agent(
        runtime, storage, "web-same", session_id
    )
    plan, _ = await _submit_extract_plan(agent, model, services, task, "检查并规划。")
    await _execute_extract_plan(runtime, agent, model, "web-same", session_id, plan)
    assert services.plan_store.get(plan.plan_id).status == "completed"
    _write_finish_inputs(services.settings)

    _refresh_direct_flat_tools(
        runtime,
        agent,
        "web-same",
        session_id,
    )
    before = len(tool_call_names(agent))
    model.enqueue_tool("inspect_navigation_artifact_state_tool", {})
    public_question = "extract/sync 已核验完成。是否继续 finish processing？"
    question = "AwaitUser: " + json.dumps(
        {
            "version": 1,
            "kind": "await_user",
            "purpose": "stage_transition",
            "requested_fields": ["continue_processing", "scene_mode"],
            "response_channel": "router_text",
            "public_prompt": public_question,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model.enqueue_text(question)
    boundary_events = await run_reply(agent, "核验 extract/sync 结果。")

    assert tool_call_names(agent)[before:] == [
        "inspect_navigation_artifact_state_tool"
    ]
    assert text_deltas(boundary_events) == question
    assert "REPLY_END" in event_types(boundary_events)
    assert services.plan_store.get_active(task.task_id, "finish_processing") is None

    before = len(tool_call_names(agent))
    model.enqueue_tool(
        "record_navigation_user_guidance_tool",
        {"text": "继续室外 finish processing。", "scene_mode": "out"},
    )
    model.enqueue_text("已记录用户确认，将基于最新任务状态继续检查。")
    guidance_events = await run_reply(agent, "继续，室外场景。")

    assert tool_call_names(agent)[before:] == [
        "record_navigation_user_guidance_tool",
    ]
    assert text_deltas(guidance_events) == "已记录用户确认，将基于最新任务状态继续检查。"
    assert services.task_store.get_task(task.task_id).scene_mode == "out"
    assert services.plan_store.get_active(task.task_id, "finish_processing") is None

    # Guidance advances the durable planning fence, so the next direct-adapter
    # cycle must bind tools to that new revision before submission.
    _refresh_direct_flat_tools(
        runtime,
        agent,
        "web-same",
        session_id,
    )
    for name in FINISH_INSPECTIONS:
        model.enqueue_tool(name, {})
    model.enqueue_text("finish 事实已刷新，准备读取最新上下文。")
    inspection_events = await run_reply(agent, "依据已记录的确认刷新 finish 事实。")
    assert text_deltas(inspection_events) == "finish 事实已刷新，准备读取最新上下文。"

    _refresh_direct_flat_tools(
        runtime,
        agent,
        "web-same",
        session_id,
    )
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool(
        "submit_finish_processing_plan_tool",
        lambda messages: {
            "planning_context_revision": _tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": _finish_plan(_evidence_by_kind(services, task.task_id)),
        },
    )
    model.enqueue_text("完整 finish-processing Plan 已接受。")
    continued_events = await run_reply(agent, "依据最新事实提交 finish Plan。")
    submit_result = json.loads(tool_result_outputs(agent)[-1])
    assert submit_result["ok"] is True, submit_result

    assert tool_call_names(agent)[before:] == [
        "record_navigation_user_guidance_tool",
        *FINISH_INSPECTIONS,
        "get_navigation_task_context_tool",
        "submit_finish_processing_plan_tool",
    ]
    assert text_deltas(continued_events) == "完整 finish-processing Plan 已接受。"
    finish = services.plan_store.get_active_for_task(task.task_id)
    assert finish is not None and finish.phase == "finish_processing"
    assert services.task_store.get_task(task.task_id).scene_mode == "out"
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_new_session_agent_distrusts_sync_claim_and_inspects_before_finish(
    navigation_environment,
):
    tmp_path, _data_root, _processing_root = navigation_environment
    runtime, storage, registry = await build_runtime(tmp_path, dry_run=True)
    runtime.set_annotation_gateway(_missing_annotation_gateway())
    services = runtime._navigation_services()
    old = services.task_store.create_task_attempt(
        request="historical completed attempt",
        target=DATE,
        date=DATE,
        segments=[SEGMENT],
        scene_mode="out",
        dry_run=True,
        web_session_id="web-old",
        agentscope_session_id="as-old",
    ).task
    services.task_store.update_task_for_session(
        old.task_id,
        web_session_id="web-old",
        agentscope_session_id="as-old",
        status=NavigationTaskStatus.COMPLETED.value,
    )
    _write_finish_inputs(services.settings)

    new, session_id = await _create_navigation_attempt(
        runtime, "web-new", "同步已完成，请继续处理，数据为室外场景"
    )
    assert new.task_id != old.task_id
    assert services.observation_store.latest(new.task_id) is None
    assert services.plan_store.get_active_for_task(new.task_id) is None
    agent, model = _direct_flat_navigation_agent(
        runtime, storage, "web-new", session_id
    )
    for name in FINISH_INSPECTIONS:
        model.enqueue_tool(name, {})
    model.enqueue_text("finish 事实已核验。")
    inspection_events = await run_reply(
        agent,
        "同步已完成，请先核验当前 finish 事实。",
    )
    assert text_deltas(inspection_events) == "finish 事实已核验。"

    _refresh_direct_flat_tools(
        runtime,
        agent,
        "web-new",
        session_id,
    )
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool(
        "submit_finish_processing_plan_tool",
        lambda messages: {
            "planning_context_revision": _tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": _finish_plan(_evidence_by_kind(services, new.task_id)),
        },
    )
    model.enqueue_text("当前事实支持 finish-processing Plan。")

    events = await run_reply(agent, "基于刚核验的事实继续规划。")

    calls = tool_call_names(agent)
    assert calls == [
        *FINISH_INSPECTIONS,
        "get_navigation_task_context_tool",
        "submit_finish_processing_plan_tool",
    ]
    assert calls[0] == "inspect_navigation_artifact_state_tool"
    observation = services.observation_store.latest(new.task_id)
    assert observation is not None
    assert observation.payloads[0].snapshot.sync_data_exists is True
    assert text_deltas(events) == "当前事实支持 finish-processing Plan。"
    assert services.plan_store.get_active_for_task(new.task_id).phase == "finish_processing"
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_deleted_products_make_new_session_agent_choose_extract_again(
    navigation_environment,
):
    tmp_path, _data_root, _processing_root = navigation_environment
    runtime, storage, registry = await build_runtime(tmp_path, dry_run=True)
    services = runtime._navigation_services()
    _write_finish_inputs(services.settings)
    final = services.settings.finish_data_root / DATE / SEGMENT / "clip-1"
    final.mkdir(parents=True)
    historical = services.task_store.create_task_attempt(
        request="historical completed attempt",
        target=DATE,
        date=DATE,
        segments=[SEGMENT],
        scene_mode="out",
        dry_run=True,
        web_session_id="web-history",
        agentscope_session_id="as-history",
    ).task
    services.task_store.update_task_for_session(
        historical.task_id,
        web_session_id="web-history",
        agentscope_session_id="as-history",
        status=NavigationTaskStatus.COMPLETED.value,
    )
    shutil.rmtree(services.settings.clip_data_root / DATE)
    shutil.rmtree(services.settings.finish_data_root / DATE)

    current, session_id = await _create_navigation_attempt(
        runtime, "web-rerun", "同步已完成，请继续处理"
    )
    agent, model = _direct_flat_navigation_agent(
        runtime, storage, "web-rerun", session_id
    )
    _queue_extract_plan(model, services, current)
    events = await run_reply(agent, "同步已完成，请继续处理。")

    calls = tool_call_names(agent)
    assert calls == [
        *EXTRACT_INSPECTIONS,
        "get_navigation_task_context_tool",
        "submit_extract_sync_plan_tool",
    ]
    assert calls[0] == "inspect_navigation_artifact_state_tool"
    artifact = services.observation_store.latest(current.task_id).payloads[0].snapshot
    assert artifact.raw_input_exists is True
    assert artifact.sync_data_exists is False
    assert artifact.final_outputs_exist is False
    assert text_deltas(events) == "完整 extract-sync Plan 已接受。"
    assert services.plan_store.get_active_for_task(current.task_id).phase == "extract_sync"
    model.assert_exhausted()


def test_cleared_conversation_recovers_compact_plan_and_ledger_anchor_from_sqlite(tmp_path):
    runtime = object.__new__(AgentScopeRuntime)
    task = SimpleNamespace(
        task_id="attempt-1",
        accepted_plan_phase="extract_sync",
    )
    plan = SimpleNamespace(plan_id="plan-1", plan_revision=1)
    services = SimpleNamespace(
        task_store=SimpleNamespace(find_by_session=lambda **_kwargs: task),
        observation_store=SimpleNamespace(latest=lambda _task_id: SimpleNamespace(revision=4)),
        plan_store=SimpleNamespace(
            get_active=lambda _task_id, _phase: plan,
            get_current_step=lambda _plan_id: {
                "step": {"step_id": "prepare_raw", "status": "pending"}
            },
        ),
    )
    runtime._navigation_services = lambda: services

    anchor = runtime._navigation_durable_state_anchor(
        "as-owner",
        web_session_id="web-owner",
    )

    assert anchor == {
        "task_attempt_id": "attempt-1",
        "observation_revision": 4,
        "accepted_plan_id": "plan-1",
        "accepted_plan_revision": 1,
        "current_ledger_step": "prepare_raw",
        "execution_status": "pending",
    }
