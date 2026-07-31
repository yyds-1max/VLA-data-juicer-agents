from __future__ import annotations

import json
from pathlib import Path

import pytest
from agentscope.app._manager import BackgroundTaskManager, SchedulerManager
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.message import ToolCallBlock, ToolResultState

from navigation_agentscope_harness import (
    ScriptedChatModel,
    latest_tool_result_json,
    runtime_config,
)
from vla_data_juicer_agents.evaluation.host import (
    EvaluationHost,
    RecordingRouterRuntime,
    run_navigation_case,
    run_router_case,
)
from vla_data_juicer_agents.evaluation.trace import (
    EvaluationSafetyMiddleware,
    TraceRecorder,
)
from vla_data_juicer_agents.runtime.agentscope_prompts import main_router_v1_prompt


def _model_factory(model):
    return lambda *_args, **_kwargs: model


def test_trace_drops_thinking_and_projects_only_public_answer(tmp_path):
    recorder = TraceRecorder.for_workspace(tmp_path)

    recorder.accept_event(
        {
            "type": "THINKING_BLOCK_DELTA",
            "delta": "private reasoning",
        },
    )
    recorder.accept_event(
        {"type": "REPLY_START", "reply_id": "reply-1"},
    )
    recorder.accept_event(
        {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "reply-1",
            "block_id": "text-1",
            "delta": "内部草稿不会展示。\nActivity: 正在检查数据。\nAns",
        },
    )
    recorder.accept_event(
        {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "reply-1",
            "block_id": "text-1",
            "delta": "wer:\n处理完成。",
            "api_key": "never-store-this",
        },
    )
    recorder.accept_event({"type": "REPLY_END", "reply_id": "reply-1"})

    assert len(recorder.events) == 4
    snapshot = recorder.sanitized_snapshot()
    serialized = str(snapshot["events"])
    assert "private reasoning" not in serialized
    assert snapshot["events"][2]["api_key"] == "[REDACTED]"
    assert recorder.final_text == "处理完成。"
    assert recorder.redact("eval-navigation-task-eval-case") == (
        "eval-navigation-task-eval-case"
    )


def test_snapshot_redacts_sensitive_values_split_across_stream_deltas(tmp_path):
    recorder = TraceRecorder.for_workspace(tmp_path)
    recorder.accept_event(
        {
            "type": "TOOL_CALL_START",
            "tool_call_id": "call-1",
            "tool_call_name": "Bash",
        },
    )
    recorder.accept_event(
        {
            "type": "TOOL_CALL_DELTA",
            "tool_call_id": "call-1",
            "delta": '{"command":"ls /Users/sfy/private',
        },
    )
    recorder.accept_event(
        {
            "type": "TOOL_CALL_DELTA",
            "tool_call_id": "call-1",
            "delta": '/data"}',
        },
    )
    recorder.accept_event(
        {
            "type": "TOOL_RESULT_TEXT_DELTA",
            "tool_call_id": "call-1",
            "delta": "sk-abcdefgh",
        },
    )
    recorder.accept_event(
        {
            "type": "TOOL_RESULT_TEXT_DELTA",
            "tool_call_id": "call-1",
            "delta": "ijklmnop",
        },
    )

    snapshot = recorder.sanitized_snapshot()
    serialized = str(snapshot)
    assert "/Users/sfy/private/data" not in serialized
    assert "sfy/private/data" not in serialized
    assert "sk-abcdefghijklmnop" not in serialized
    assert "[PATH]" in serialized
    assert "[REDACTED]" in serialized
    assert "[PATH]" in snapshot["tool_calls"][0]["input"]
    assert "[REDACTED]" in snapshot["tool_calls"][0]["result"]

    snapshot["tool_calls"][0]["input"] = "mutated"
    assert recorder.tool_calls[0]["input"] != "mutated"


def test_public_reply_projection_retracts_text_before_tool_call():
    recorder = TraceRecorder()
    for event in (
        {"type": "REPLY_START", "reply_id": "reply-1"},
        {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "reply-1",
            "block_id": "text-1",
            "delta": "Answer: 这段会被撤回。",
        },
        {
            "type": "TOOL_CALL_START",
            "reply_id": "reply-1",
            "tool_call_id": "call-1",
            "tool_call_name": "start_navigation_data_task",
        },
        {"type": "REPLY_END", "reply_id": "reply-1"},
        {"type": "REPLY_START", "reply_id": "reply-2"},
        {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "reply-2",
            "block_id": "text-2",
            "delta": "Activity: 正在汇总。\nAnswer: 最终公开回答。",
        },
        {"type": "REPLY_END", "reply_id": "reply-2"},
    ):
        recorder.accept_event(event)

    assert recorder.final_text == "最终公开回答。"


def test_public_reply_projection_recovers_unmarked_router_terminal_reply():
    recorder = TraceRecorder()
    for event in (
        {"type": "REPLY_START", "reply_id": "reply-unmarked"},
        {
            "type": "TEXT_BLOCK_DELTA",
            "reply_id": "reply-unmarked",
            "block_id": "text-unmarked",
            "delta": "当前任务正在准备中。\n- 尚未开始实际处理。",
        },
        {"type": "REPLY_END", "reply_id": "reply-unmarked"},
    ):
        recorder.accept_event(event)

    assert recorder.final_text == "当前任务正在准备中。\n- 尚未开始实际处理。"


@pytest.mark.asyncio
async def test_host_uses_production_assembly_and_records_visible_tools(tmp_path):
    config = runtime_config(tmp_path)
    model = ScriptedChatModel()
    model.enqueue_text("Answer:\n我是 DataPilot。")
    host = EvaluationHost(
        config=config,
        workspace_root=tmp_path,
        model_factory=_model_factory(model),
    )

    result = await host.run("你能做什么？", web_session_id="capability")

    assert isinstance(host.workspace_manager, LocalWorkspaceManager)
    assert isinstance(host.background_task_manager, BackgroundTaskManager)
    assert isinstance(host.scheduler_manager, SchedulerManager)
    assert result.session_id == "capability__main-router-agent"
    agent_record = await host.storage.get_agent(
        config.user_id,
        config.main_router_agent_id,
    )
    assert agent_record is not None
    assert agent_record.data.system_prompt == main_router_v1_prompt()
    session_record = await host.storage.get_session(
        config.user_id,
        config.main_router_agent_id,
        result.session_id,
    )
    assert session_record is not None
    assert session_record.config.chat_model_config.model == config.router_model
    assert session_record.config.chat_model_config.parameters == {
        "parallel_tool_calls": False,
    }
    assert result.final_text == "我是 DataPilot。"
    assert len(result.model_calls) == 1
    rendered_prompt = model.invocations[0].formatted_messages[0]["content"][0]["text"]
    assert rendered_prompt.startswith(main_router_v1_prompt())
    assert "RouterContextEnvelope" in rendered_prompt
    assert '"contract_version":1' in rendered_prompt
    model_call = result.model_calls[0]
    assert model_call["model_name"] == "offline-scripted-qwen"
    assert "start_navigation_data_task" in model_call["tools"]
    assert "continue_navigation_data_task" in model_call["tools"]
    assert "control_navigation_data_task" in model_call["tools"]
    assert "Bash" in model_call["tools"]
    assert len(model_call["schema_hash"]) == 64
    assert result.token_usage["input_tokens"] > 0
    assert result.token_usage["total_tokens"] == (
        result.token_usage["input_tokens"] + result.token_usage["output_tokens"]
    )
    assert result.tool_calls == ()
    assert result.forbidden_calls == ()
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_v1_start_tool_preserves_scope_and_ends_without_router_summary(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "scope_source": "interpreted_user_text",
            "dataset_date": "20260718",
            "selection": {
                "kind": "selected_clips",
                "clips": ["clip-a", "clip-b"],
            },
        },
    )

    result = await run_router_case(
        "处理 20260718 的 clip-a 和 clip-b",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="shortcut",
        model_factory=_model_factory(model),
    )

    assert len(result.handoffs) == 1
    handoff = result.handoffs[0]
    assert handoff["ok"] is True
    assert handoff["operation"] == "start"
    assert handoff["accepted"] is True
    assert handoff["started"] is True
    assert handoff["dataset_date"] == "20260718"
    assert handoff["selection"] == {
        "kind": "selected_clips",
        "clips": ["clip-a", "clip-b"],
    }
    assert handoff["scope_source"] == "interpreted_user_text"
    assert [call["name"] for call in result.tool_calls] == [
        "start_navigation_data_task",
    ]
    assert result.final_text == ""
    assert len(result.model_calls) == 1
    assert sum(event["type"] == "REPLY_END" for event in result.events) == 1
    assert result.forbidden_calls == ()
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_v1_date_only_start_uses_all_clips_without_scene_mode(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "scope_source": "interpreted_user_text",
            "dataset_date": "20270605",
            "selection": {"kind": "all_clips"},
        },
    )

    result = await run_router_case(
        "请处理 20270605 的导航数据。",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="all-clips",
        model_factory=_model_factory(model),
    )

    assert result.handoffs[0]["dataset_date"] == "20270605"
    assert result.handoffs[0]["selection"] == {"kind": "all_clips"}
    assert "scene_mode" not in result.handoffs[0]
    assert len(result.model_calls) == 1
    assert result.final_text == ""
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_v1_selected_clip_does_not_derive_dataset_date_from_clip_prefix(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "scope_source": "interpreted_user_text",
            "dataset_date": "20270605",
            "selection": {
                "kind": "selected_clips",
                "clips": ["20260605_152856"],
            },
        },
    )

    result = await run_router_case(
        "处理数据日期 20270605，只处理 clip 20260605_152856。",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="cross-prefix",
        model_factory=_model_factory(model),
    )

    assert result.handoffs[0]["dataset_date"] == "20270605"
    assert result.handoffs[0]["selection"] == {
        "kind": "selected_clips",
        "clips": ["20260605_152856"],
    }
    assert "target" not in result.handoffs[0]
    assert "clips" not in result.handoffs[0]
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_v1_trusted_request_context_is_visible_and_enforced_exactly(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "scope_source": "request_context",
            "dataset_date": "20270605",
            "selection": {
                "kind": "selected_clips",
                "clips": ["20260605_152856", "route_A_07"],
            },
        },
    )
    runtime_setup = {
        "request_context": {
            "kind": "navigation_dataset_selection_v1",
            "dataset_date": "20270605",
            "selection": {
                "kind": "selected_clips",
                "clips": ["20260605_152856", "route_A_07"],
            },
        },
    }

    result = await run_router_case(
        "处理我刚才在数据管理页选择的导航数据。",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="trusted-shortcut",
        model_factory=_model_factory(model),
        runtime_setup=runtime_setup,
    )

    assert result.handoffs == (
        {
            "ok": True,
            "operation": "start",
            "accepted": True,
            "started": True,
            "task_ref": "DP-EVALUATION",
            "scope_source": "request_context",
            "dataset_date": "20270605",
            "selection": {
                "kind": "selected_clips",
                "clips": ["20260605_152856", "route_A_07"],
            },
        },
    )
    rendered_prompt = model.invocations[0].formatted_messages[0]["content"][0]["text"]
    assert '"kind":"navigation_dataset_selection_v1"' in rendered_prompt
    assert '"clips":["20260605_152856","route_A_07"]' in rendered_prompt
    assert result.final_text == ""
    assert len(result.model_calls) == 1
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_evaluation_runtime_rejects_reinterpreted_trusted_scope(tmp_path):
    recorder = TraceRecorder.for_workspace(tmp_path)
    runtime = RecordingRouterRuntime(
        recorder,
        web_session_id="trusted-shortcut",
        router_session_id="trusted-shortcut__main-router-agent",
        runtime_setup={
            "request_context": {
                "kind": "navigation_dataset_selection_v1",
                "dataset_date": "20270605",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260605_152856"],
                },
            },
        },
    )

    with pytest.raises(RuntimeError, match="without reinterpretation"):
        await runtime.start_navigation_agent_task_v1(
            web_session_id="trusted-shortcut",
            router_session_id="trusted-shortcut__main-router-agent",
            scope_source="interpreted_user_text",
            dataset_date="20270605",
            selection={"kind": "all_clips"},
            scene_mode=None,
        )

    assert runtime.operations == []
    assert recorder.handoffs == []


@pytest.mark.asyncio
async def test_v1_missing_date_clarification_is_one_router_call(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_text("Answer:\n请提供要处理的数据日期（YYYYMMDD）？")

    result = await run_router_case(
        "请处理导航数据。",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="clarify",
        model_factory=_model_factory(model),
    )

    assert result.final_text == "请提供要处理的数据日期（YYYYMMDD）？"
    assert result.tool_calls == ()
    assert result.handoffs == ()
    assert len(result.model_calls) == 1
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_v1_multiturn_date_clarification_retains_selected_clip(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_text("Answer:\n请提供数据目录日期（YYYYMMDD）？")
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "scope_source": "interpreted_user_text",
            "dataset_date": "20270605",
            "selection": {
                "kind": "selected_clips",
                "clips": ["20260605_152856"],
            },
        },
    )
    host = EvaluationHost(
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        model_factory=_model_factory(model),
    )

    result = await host.run(
        [
            "处理 clip 20260605_152856 的导航数据。",
            "日期是 20270605。",
        ],
        web_session_id="clarify-retain-clip",
    )

    assert len(result.model_calls) == 2
    assert [call["name"] for call in result.tool_calls] == [
        "start_navigation_data_task",
    ]
    assert result.handoffs[0]["dataset_date"] == "20270605"
    assert result.handoffs[0]["selection"] == {
        "kind": "selected_clips",
        "clips": ["20260605_152856"],
    }
    assert result.final_text == "请提供数据目录日期（YYYYMMDD）？"
    model.assert_exhausted()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "operation", "status"),
    [
        ("continue_navigation_data_task", {}, "continue", "active"),
        ("control_navigation_data_task", {"action": "stop"}, "stop", "pausing"),
        (
            "control_navigation_data_task",
            {"action": "cancel"},
            "cancel",
            "cancelling",
        ),
    ],
)
async def test_v1_task_tools_are_available_and_terminal(
    tmp_path,
    tool_name,
    arguments,
    operation,
    status,
):
    model = ScriptedChatModel()
    model.enqueue_tool(tool_name, arguments)
    runtime_setup = {
        "focused_task": {
            "task_ref": "DP-EVAL-FOCUSED",
            "status": "waiting_user" if operation == "continue" else "active",
            "dataset_date": "20260718",
            "selection": {"kind": "all_clips"},
        },
    }

    result = await run_router_case(
        "继续或控制当前任务",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id=f"v1-{operation}",
        model_factory=_model_factory(model),
        runtime_setup=runtime_setup,
    )

    assert [call["name"] for call in result.tool_calls] == [tool_name]
    assert result.handoffs == (
        {
            "ok": True,
            "operation": operation,
            "accepted": True,
            "task_ref": "DP-EVAL-FOCUSED",
            "status": status,
        },
    )
    assert result.final_text == ""
    assert len(result.model_calls) == 1
    assert result.forbidden_calls == ()
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_v1_multiturn_start_then_stop_reuses_runtime_context(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "scope_source": "interpreted_user_text",
            "dataset_date": "20270605",
            "selection": {"kind": "all_clips"},
        },
    )
    model.enqueue_tool("control_navigation_data_task", {"action": "stop"})
    host = EvaluationHost(
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        model_factory=_model_factory(model),
    )

    result = await host.run(
        ["请处理 20270605 的导航数据。", "先停一下当前处理。"],
        web_session_id="start-stop",
    )

    assert [call["name"] for call in result.tool_calls] == [
        "start_navigation_data_task",
        "control_navigation_data_task",
    ]
    assert [handoff["operation"] for handoff in result.handoffs] == ["start", "stop"]
    assert len(result.model_calls) == 2
    assert sum(event["type"] == "REPLY_END" for event in result.events) == 2
    assert result.final_text == ""
    assert result.forbidden_calls == ()
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_navigation_evaluation_host_records_postprocessing_plan_without_io(
    tmp_path,
):
    model = ScriptedChatModel()
    model.enqueue_tool("inspect_navigation_annotation_job_facts_tool", {})
    model.enqueue_tool("inspect_navigation_localization_sources_tool", {})
    model.enqueue_tool("inspect_navigation_gridmap_artifacts_tool", {})
    model.enqueue_tool("inspect_navigation_runtime_assets_tool", {})
    model.enqueue_tool("inspect_navigation_artifact_state_tool", {})
    model.enqueue_tool("inspect_navigation_calibration_inventory_tool", {})
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool(
        "submit_finish_processing_plan_tool",
        lambda messages: {
            "planning_context_revision": latest_tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": {
                "decisions": {
                    "localization": {
                        "reason": "已观察到 odom 且转换能力可用",
                        "evidence_refs": ["localization_sources:eval"],
                        "source": "odom",
                        "conversion": "odom_to_ins",
                    },
                    "gridmap": {
                        "reason": "已有同步 gridmap 可复用",
                        "evidence_refs": ["gridmap_artifacts:eval"],
                        "source": "existing_gridmap",
                    },
                    "calibration": {
                        "reason": "沿用 Annotation Job 冻结的处理标定快照",
                        "evidence_refs": ["annotation_job_facts:eval"],
                        "mode": "annotation_snapshot",
                        "selected_sensor_source": None,
                        "requires_user_confirmation": False,
                    },
                },
                "steps": [
                    {
                        "step_id": "run_postprocessing",
                        "action": "run_annotation_postprocessing_workflow",
                        "variant": "plan_bound_runtime",
                        "arguments": {},
                        "depends_on": [],
                        "failure_policy": "stop",
                        "decision_refs": ["localization", "calibration", "gridmap"],
                    },
                    {
                        "step_id": "validate_outputs",
                        "action": "validate_navigation_outputs",
                        "variant": "expect_gridmap",
                        "arguments": {},
                        "depends_on": ["run_postprocessing"],
                        "failure_policy": "stop",
                        "decision_refs": ["localization", "gridmap"],
                    },
                ],
            },
        },
    )
    model.enqueue_tool(
        "get_current_plan_step_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
        },
    )
    model.enqueue_tool(
        "run_annotation_postprocessing_workflow_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
            "step_id": latest_tool_result_json(messages)["step_id"],
        },
    )

    result = await run_navigation_case(
        "执行自动标注并完成后处理",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="navigation-postprocessing",
        model_factory=_model_factory(model),
        runtime_setup={
            "navigation_task": {
                "dataset_date": "20270623",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260623_145550"],
                },
                "scene_mode": "out",
                "requested_outcome": "postprocessing",
            },
        },
    )

    assert result.session_id == "navigation-postprocessing__navigation-data-agent"
    visible_tools = set(result.model_calls[0]["tools"])
    assert visible_tools == {
        "inspect_navigation_annotation_job_facts_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "get_navigation_task_context_tool",
    }
    assert any(
        "submit_finish_processing_plan_tool" in model_call["tools"]
        for model_call in result.model_calls[1:]
    )
    assert set(result.model_calls[-1]["tools"]) == {
        "get_current_plan_step_tool",
        "run_annotation_postprocessing_workflow_tool",
    }
    context_result = json.loads(
        next(
            call["result"]
            for call in result.tool_calls
            if call["name"] == "get_navigation_task_context_tool"
        )
    )
    assert {
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
    } == set(context_result)
    assert "task" not in context_result
    assert result.handoffs == (
        {
            "operation": "submit_plan",
            "phase": "finish_processing",
            "decision_modes": {
                "calibration": "annotation_snapshot",
                "gridmap": "existing_gridmap",
                "localization": "odom",
            },
            "step_actions": [
                "run_annotation_postprocessing_workflow",
                "validate_navigation_outputs",
            ],
            "step_variants": {
                "run_annotation_postprocessing_workflow": "plan_bound_runtime",
                "validate_navigation_outputs": "expect_gridmap",
            },
        },
    )
    assert [call["name"] for call in result.tool_calls][-3:] == [
        "submit_finish_processing_plan_tool",
        "get_current_plan_step_tool",
        "run_annotation_postprocessing_workflow_tool",
    ]
    current_step = json.loads(result.tool_calls[-2]["result"])
    assert current_step == {
        "plan_id": "eval-plan",
        "step_id": "run_postprocessing",
        "action": "run_annotation_postprocessing_workflow",
        "status": "pending",
    }
    assert "eval-step-record" not in result.tool_calls[-2]["result"]
    assert '"status": "running_in_background"' in result.tool_calls[-1]["result"]
    assert '"action": "run_annotation_postprocessing_workflow"' in (
        result.tool_calls[-1]["result"]
    )
    assert result.final_text == ""
    assert result.forbidden_calls == ()
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_navigation_evaluation_host_rejects_inconsistent_finish_plan(
    tmp_path,
):
    model = ScriptedChatModel()
    for name in (
        "inspect_navigation_annotation_job_facts_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "get_navigation_task_context_tool",
    ):
        model.enqueue_tool(name, {})
    model.enqueue_tool(
        "submit_finish_processing_plan_tool",
        lambda messages: {
            "planning_context_revision": latest_tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": {
                "decisions": {
                    "localization": {
                        "reason": "已观察到 odom",
                        "evidence_refs": ["localization_sources:eval"],
                        "source": "odom",
                        "conversion": "odom_to_ins",
                    },
                    "gridmap": {
                        "reason": "已有同步 gridmap",
                        "evidence_refs": ["gridmap_artifacts:eval"],
                        "source": "existing_gridmap",
                    },
                    "calibration": {
                        "reason": "沿用冻结的处理标定",
                        "evidence_refs": ["annotation_job_facts:eval"],
                        "mode": "annotation_snapshot",
                        "selected_sensor_source": None,
                        "requires_user_confirmation": False,
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
                        "step_id": "run_postprocessing",
                        "action": "run_annotation_postprocessing_workflow",
                        "variant": "plan_bound_runtime",
                        "arguments": {},
                        "depends_on": ["confirm_calibration"],
                        "failure_policy": "stop",
                        "decision_refs": [
                            "localization",
                            "calibration",
                            "gridmap",
                        ],
                    },
                    {
                        "step_id": "validate_outputs",
                        "action": "validate_navigation_outputs",
                        "variant": "expect_gridmap",
                        "arguments": {},
                        "depends_on": ["run_postprocessing"],
                        "failure_policy": "stop",
                        "decision_refs": ["localization", "gridmap"],
                    },
                ],
            },
        },
    )
    model.enqueue_text("Answer:\n计划存在冲突，需要重新规划。")

    result = await run_navigation_case(
        "执行自动标注并完成后处理",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="navigation-invalid-plan",
        model_factory=_model_factory(model),
        runtime_setup={
            "navigation_task": {
                "dataset_date": "20270623",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260623_145550"],
                },
                "scene_mode": "out",
                "requested_outcome": "postprocessing",
            },
        },
    )

    assert result.handoffs == ()
    assert result.final_text == "计划存在冲突，需要重新规划。"
    assert "unexpected_calibration_confirmation" in (
        result.tool_calls[-1]["result"]
    )
    assert "run_annotation_postprocessing_workflow_tool" not in {
        call["name"] for call in result.tool_calls
    }
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_navigation_evaluation_host_records_linked_review_plan(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool("inspect_navigation_annotation_job_facts_tool", {})
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool(
        "submit_trajectory_review_plan_tool",
        lambda messages: {
            "planning_context_revision": latest_tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": {
                "decisions": {
                    "review": {
                        "reason": "存在待人工复核的轨迹版本",
                        "evidence_refs": ["annotation_job_facts:eval"],
                        "mode": "human_fix",
                    },
                },
                "steps": [
                    {
                        "step_id": "open_fix",
                        "action": "open_trajectory_fix_workbench",
                        "variant": "durable_human_handoff",
                        "arguments": {},
                        "depends_on": [],
                        "failure_policy": "stop",
                        "decision_refs": ["review"],
                    },
                    {
                        "step_id": "validate_review",
                        "action": "validate_trajectory_review_outcome",
                        "variant": "approved_or_terminal",
                        "arguments": {},
                        "depends_on": ["open_fix"],
                        "failure_policy": "stop",
                        "decision_refs": ["review"],
                    },
                ],
            },
        },
    )
    model.enqueue_tool(
        "get_current_plan_step_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
        },
    )
    model.enqueue_tool(
        "open_trajectory_fix_workbench_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
            "step_id": latest_tool_result_json(messages)["step_id"],
        },
    )

    result = await run_navigation_case(
        "继续 Fix",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="navigation-fix",
        model_factory=_model_factory(model),
        runtime_setup={
            "navigation_task": {
                "dataset_date": "20270623",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260623_145550"],
                },
                "scene_mode": "out",
                "requested_outcome": "trajectory_fix",
            },
        },
    )

    assert result.handoffs == (
        {
            "operation": "submit_plan",
            "phase": "trajectory_review",
            "decision_modes": {"review": "human_fix"},
            "step_actions": [
                "open_trajectory_fix_workbench",
                "validate_trajectory_review_outcome",
            ],
            "step_variants": {
                "open_trajectory_fix_workbench": "durable_human_handoff",
                "validate_trajectory_review_outcome": "approved_or_terminal",
            },
        },
    )
    visible_tools = set(result.model_calls[0]["tools"])
    assert visible_tools == {
        "inspect_navigation_annotation_job_facts_tool",
        "get_navigation_task_context_tool",
        "submit_trajectory_review_plan_tool",
    }
    assert set(result.model_calls[-1]["tools"]) == {
        "get_current_plan_step_tool",
        "open_trajectory_fix_workbench_tool",
    }
    assert {
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "describe_processing_action_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
        "prepare_gridmap_for_projection_tool",
        "run_annotation_postprocessing_workflow_tool",
    }.isdisjoint(visible_tools)
    assert [call["name"] for call in result.tool_calls][-3:] == [
        "submit_trajectory_review_plan_tool",
        "get_current_plan_step_tool",
        "open_trajectory_fix_workbench_tool",
    ]
    assert result.final_text == ""
    assert result.forbidden_calls == ()
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_safety_middleware_blocks_generic_tool_before_side_effect(tmp_path):
    sentinel = tmp_path / "must-not-exist.txt"
    model = ScriptedChatModel()
    model.enqueue_tool(
        "Bash",
        {"command": f"touch {sentinel}"},
    )

    result = await run_router_case(
        "请用命令检查并处理 20260718 的导航数据",
        config=runtime_config(tmp_path),
        workspace_root=tmp_path,
        web_session_id="unsafe",
        model_factory=_model_factory(model),
    )

    assert not sentinel.exists()
    assert len(result.forbidden_calls) == 1
    assert result.forbidden_calls[0]["name"] == "Bash"
    assert [call["name"] for call in result.tool_calls] == ["Bash"]
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_safety_on_acting_returns_structured_error_without_calling_tool():
    recorder = TraceRecorder()
    middleware = EvaluationSafetyMiddleware(recorder)
    reached_tool = False

    async def next_handler(**_kwargs):
        nonlocal reached_tool
        reached_tool = True
        if False:
            yield None

    responses = [
        item
        async for item in middleware.on_acting(
            None,
            {
                "tool_call": ToolCallBlock(
                    id="call-forbidden",
                    name="Write",
                    input='{"file_path":"/tmp/no","content":"no"}',
                ),
            },
            next_handler,
        )
    ]

    assert reached_tool is False
    assert len(responses) == 1
    assert responses[0].state == ToolResultState.ERROR
    assert responses[0].metadata["error_type"] == "evaluation_forbidden_tool"
    assert recorder.forbidden_calls == [
        {"id": "call-forbidden", "name": "Write"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "start_navigation_data_task",
        "continue_navigation_data_task",
        "control_navigation_data_task",
    ],
)
async def test_safety_allows_each_v1_router_tool(tool_name):
    recorder = TraceRecorder()
    middleware = EvaluationSafetyMiddleware(recorder)
    reached_tool = False

    async def next_handler(**_kwargs):
        nonlocal reached_tool
        reached_tool = True
        if False:
            yield None

    responses = [
        item
        async for item in middleware.on_acting(
            None,
            {
                "tool_call": ToolCallBlock(
                    id=f"call-{tool_name}",
                    name=tool_name,
                    input="{}",
                ),
            },
            next_handler,
        )
    ]

    assert reached_tool is True
    assert responses == []
    assert recorder.forbidden_calls == []
