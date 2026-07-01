import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.event import RequireExternalExecutionEvent, RequireUserConfirmEvent
from agentscope.message import ToolCallBlock

from vla_data_juicer_agents.adapters.agentscope.events import (
    AgentScopeEventAdapter,
    summarize_progress,
)
from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.navigation.workflow import _run_agent_stream


def _scope_and_events():
    events = []
    scope = EventEmitter(CallbackEventSink(events.append)).scope("plan-agent", run_id="run-1")
    return scope, events


def test_thinking_end_emits_normalized_bounded_reasoning():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_DELTA", block_id="thought-1", delta="Thought:  inspect   inputs. "))
    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_DELTA", block_id="thought-1", delta="Then choose a tool! Ignore this third sentence."))
    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_END", block_id="thought-1"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("reasoning", {"summary": "inspect inputs. Then choose a tool!"})
    ]
    assert summarize_progress("思考：  查看\n状态。 继续执行。 第三句。") == "查看 状态。 继续执行。"
    assert summarize_progress("思考：一。二。三。") == "一。 二。"
    assert len(summarize_progress("Thought: " + "x" * 300)) <= 240


def test_progress_marker_text_becomes_reasoning_and_is_removed_from_output():
    scope, events = _scope_and_events()

    class ProgressAgent:
        async def reply_stream(self, _message):
            yield SimpleNamespace(
                type="TEXT_BLOCK_DELTA",
                delta="Progress: Raw data exists; next I will inspect the profile.\n",
            )
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="final answer")

    output = asyncio.run(_run_agent_stream(ProgressAgent(), "prompt", event_scope=scope))

    assert output == "final answer"
    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("reasoning", {"summary": "Raw data exists; next I will inspect the profile."}),
        ("assistant_delta", {"delta": "final answer"}),
        ("agent_end", {"status": "completed"}),
    ]


def test_plain_text_delta_emits_assistant_delta_for_streaming_ui():
    scope, events = _scope_and_events()

    class StreamingAgent:
        async def reply_stream(self, _message):
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="你好，")
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="我是 DataPilot。")

    output = asyncio.run(_run_agent_stream(StreamingAgent(), "prompt", event_scope=scope))

    assert output == "你好，我是 DataPilot。"
    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("assistant_delta", {"delta": "你好，"}),
        ("assistant_delta", {"delta": "我是 DataPilot。"}),
        ("agent_end", {"status": "completed"}),
    ]


def test_adapter_can_emit_text_delta_and_final_from_raw_agentscope_events():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_text_events=True,
        emit_final_events=True,
    )

    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="Progress: Inspecting data.\n"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="处理"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="完成"))
    adapter.accept(SimpleNamespace(type="REPLY_END"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("reasoning", {"summary": "Inspecting data."}),
        ("assistant_delta", {"delta": "处理"}),
        ("assistant_delta", {"delta": "完成"}),
        ("final", {"text": "处理完成"}),
    ]


def test_progress_marker_without_newline_flushes_before_tool_event():
    scope, events = _scope_and_events()

    class ProgressBeforeToolAgent:
        async def reply_stream(self, _message):
            yield SimpleNamespace(
                type="TEXT_BLOCK_DELTA",
                delta="Progress: Need the raw segment metadata; next I will inspect the date.",
            )
            yield SimpleNamespace(
                type="TOOL_RESULT_START",
                tool_call_id="call-1",
                tool_call_name="inspect_raw_date_tool",
            )
            yield SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call-1", delta="done")
            yield SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success")
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="final")

    output = asyncio.run(_run_agent_stream(ProgressBeforeToolAgent(), "prompt", event_scope=scope))

    assert output == "final"
    assert [(event["type"], event["payload"].get("summary")) for event in events] == [
        ("agent_start", None),
        ("reasoning", "Need the raw segment metadata; next I will inspect the date."),
        ("tool_start", None),
        ("tool_end", "done"),
        ("assistant_delta", None),
        ("agent_end", None),
    ]


def test_tool_result_emits_paired_start_and_end_with_result_state():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(SimpleNamespace(type="TOOL_CALL_START", tool_call_id="call-1", tool_call_name="inspect"))
    adapter.accept(SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id="call-1", delta='{"date":'))
    adapter.accept(SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id="call-1", delta=' "20270605"}'))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call-1", tool_call_name="inspect"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call-1", delta="Found   navigation data. "))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call-1", delta="Ready."))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("tool_start", {"tool": "inspect", "call_id": "call-1", "args": '{"date": "20270605"}'}),
        ("tool_end", {"tool": "inspect", "call_id": "call-1", "status": "completed", "summary": "Found navigation data. Ready."}),
    ]


def test_require_external_execution_emits_human_decision_required():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)
    tool_input = {
        "decision_type": "camera_params",
        "request_id": "req-1",
        "summary": "Confirm fisheye camera parameters.",
    }

    adapter.accept(
        RequireExternalExecutionEvent(
            reply_id="reply-1",
            tool_calls=[
                ToolCallBlock(
                    id="decision-1",
                    name="request_human_decision",
                    input=json.dumps(tool_input),
                )
            ],
        )
    )

    assert len(events) == 1
    timestamp = events[0]["timestamp"]
    assert events == [
        {
            "type": "human_decision_required",
            "source": "plan-agent",
            "run_id": "run-1",
            "parent_run_id": None,
            "timestamp": timestamp,
            "payload": {
                "reply_id": "reply-1",
                "tool_call_id": "decision-1",
                "decision_type": "camera_params",
                "request_id": "req-1",
                "summary": "Confirm fisheye camera parameters.",
            },
        }
    ]


@pytest.mark.parametrize(
    ("state", "status"),
    [("error", "failed"), ("denied", "failed"), ("interrupted", "interrupted")],
)
def test_tool_result_maps_non_success_states(state, status):
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call-1", tool_call_name="inspect"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state=state))

    assert events[-1]["payload"]["status"] == status


def test_tool_result_success_with_false_ok_payload_maps_failed():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call-1", tool_call_name="track"))
    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_TEXT_DELTA",
            tool_call_id="call-1",
            delta='{"ok": false, "message": "Tracking failed."}',
        )
    )
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))

    assert events[-1]["payload"]["status"] == "failed"
    assert "Tracking failed." in events[-1]["payload"]["summary"]


def test_tool_result_end_includes_error_type_from_full_json_details():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)
    result = {
        "ok": False,
        "message": "Calibration parameters still need user confirmation.",
        "details": {
            "notes": ["x" * 300],
            "error_type": "calibration_params_not_confirmed",
        },
    }

    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_START",
            tool_call_id="call-1",
            tool_call_name="confirm_navigation_calibration_params",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_TEXT_DELTA",
            tool_call_id="call-1",
            delta=json.dumps(result),
        )
    )
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))

    assert "calibration_params_not_confirmed" not in events[-1]["payload"]["summary"]
    assert events[-1]["payload"]["error_type"] == "calibration_params_not_confirmed"


def test_emit_tool_events_false_suppresses_tool_events():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope, emit_tool_events=False)

    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call-1", tool_call_name="inspect"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call-1", delta="done"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))

    assert events == []


def test_close_active_tools_is_idempotent():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call-1", tool_call_name="inspect"))
    adapter.close_active_tools("failed")
    adapter.close_active_tools("failed")

    assert [(event["type"], event["payload"]) for event in events] == [
        ("tool_start", {"tool": "inspect", "call_id": "call-1", "args": ""}),
        ("tool_end", {"tool": "inspect", "call_id": "call-1", "status": "failed", "summary": ""}),
    ]


def test_run_agent_stream_does_not_auto_confirm_user_confirmation_events():
    scope, events = _scope_and_events()

    class ConfirmingAgent:
        async def reply_stream(self, _message):
            yield RequireUserConfirmEvent(
                reply_id="reply-1",
                tool_calls=[ToolCallBlock(id="call-1", name="inspect", input="{}")],
            )

    with pytest.raises(RuntimeError, match="requires user confirmation"):
        asyncio.run(_run_agent_stream(ConfirmingAgent(), "prompt", event_scope=scope))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("agent_end", {"status": "failed"}),
    ]


def test_agent_cancellation_emits_interrupted_end_and_tracks_agent():
    scope, events = _scope_and_events()
    cancellation = CancellationContext()
    started = asyncio.Event()

    class BlockingAgent:
        async def reply_stream(self, _message):
            yield SimpleNamespace(
                type="TOOL_RESULT_START",
                tool_call_id="call-1",
                tool_call_name="inspect",
            )
            started.set()
            await asyncio.Future()
            yield

    async def exercise():
        task = asyncio.create_task(
            _run_agent_stream(
                BlockingAgent(),
                "prompt",
                event_scope=scope,
                cancellation=cancellation,
            )
        )
        await started.wait()
        assert cancellation.cancel() is True
        with pytest.raises(TurnCancelled):
            await task
        assert cancellation.cancel() is False

    asyncio.run(exercise())

    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("tool_start", {"tool": "inspect", "call_id": "call-1", "args": ""}),
        ("tool_end", {"tool": "inspect", "call_id": "call-1", "status": "interrupted", "summary": ""}),
        ("agent_end", {"status": "interrupted"}),
    ]


def test_agent_failure_closes_active_tool_before_failed_end():
    scope, events = _scope_and_events()

    class FailingAgent:
        async def reply_stream(self, _message):
            yield SimpleNamespace(
                type="TOOL_RESULT_START",
                tool_call_id="call-1",
                tool_call_name="inspect",
            )
            raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError, match="stream failed"):
        asyncio.run(_run_agent_stream(FailingAgent(), "prompt", event_scope=scope))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("tool_start", {"tool": "inspect", "call_id": "call-1", "args": ""}),
        ("tool_end", {"tool": "inspect", "call_id": "call-1", "status": "failed", "summary": ""}),
        ("agent_end", {"status": "failed"}),
    ]


def test_asyncio_timeout_preserves_timeout_error_and_emits_interrupted_end():
    scope, events = _scope_and_events()
    cancellation = CancellationContext()

    class BlockingAgent:
        async def reply_stream(self, _message):
            await asyncio.Future()
            yield

    async def exercise():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await _run_agent_stream(
                    BlockingAgent(),
                    "prompt",
                    event_scope=scope,
                    cancellation=cancellation,
                )

    asyncio.run(exercise())

    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("agent_end", {"status": "interrupted"}),
    ]
