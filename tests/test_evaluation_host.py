from __future__ import annotations

from pathlib import Path

import pytest
from agentscope.app._manager import BackgroundTaskManager, SchedulerManager
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.message import ToolCallBlock, ToolResultState

from navigation_agentscope_harness import ScriptedChatModel, runtime_config
from vla_data_juicer_agents.evaluation.host import EvaluationHost, run_router_case
from vla_data_juicer_agents.evaluation.trace import (
    EvaluationSafetyMiddleware,
    TraceRecorder,
)
from vla_data_juicer_agents.runtime.agentscope_prompts import main_router_prompt


def _model_factory(model):
    return lambda *_args, **_kwargs: model


def test_trace_drops_thinking_and_redacts_secrets_and_workspace(tmp_path):
    recorder = TraceRecorder.for_workspace(tmp_path)

    recorder.accept_event(
        {
            "type": "THINKING_BLOCK_DELTA",
            "delta": "private reasoning",
        },
    )
    recorder.accept_event(
        {
            "type": "TEXT_BLOCK_DELTA",
            "delta": f"Answer: {tmp_path}/case sk-abcdefghijklmnopqrstuvwxyz",
            "api_key": "never-store-this",
        },
    )

    assert len(recorder.events) == 1
    serialized = str(recorder.events)
    assert "private reasoning" not in serialized
    assert str(tmp_path) not in serialized
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    assert recorder.events[0]["api_key"] == "[REDACTED]"
    assert "[WORKSPACE]" in recorder.final_text
    assert recorder.redact("eval-navigation-task-eval-case") == (
        "eval-navigation-task-eval-case"
    )


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
    assert agent_record.data.system_prompt == main_router_prompt()
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
    assert result.final_text == "Answer:\n我是 DataPilot。"
    assert len(result.model_calls) == 1
    model_call = result.model_calls[0]
    assert model_call["model_name"] == "offline-scripted-qwen"
    assert "start_navigation_data_task" in model_call["tools"]
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
async def test_formal_handoff_tool_validates_and_records_payload(tmp_path):
    model = ScriptedChatModel()
    model.enqueue_tool(
        "start_navigation_data_task",
        {
            "request": "处理 20260718 的指定导航数据",
            "target": "20260718",
            "date": "20260718",
            "clips": ["clip-a", "clip-b"],
            "reason": "用户给出了具体导航处理范围",
            "missing_fields": [],
            "confidence": "high",
            "response_language": "Chinese",
        },
    )
    model.enqueue_text("Answer:\n导航数据任务已启动。")

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
    assert handoff["started"] is True
    assert handoff["date"] == "20260718"
    assert handoff["clips"] == ["clip-a", "clip-b"]
    assert handoff["response_language"] == "Chinese"
    assert [call["name"] for call in result.tool_calls] == [
        "start_navigation_data_task",
    ]
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
        "运行命令",
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
