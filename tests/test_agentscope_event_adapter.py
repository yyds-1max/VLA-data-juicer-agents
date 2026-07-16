import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.event import RequireExternalExecutionEvent, RequireUserConfirmEvent
from agentscope.message import ToolCallBlock

from vla_data_juicer_agents.adapters.agentscope.events import (
    AgentScopeEventAdapter,
    sanitize_public_reply,
    summarize_progress,
)
from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.navigation.workflow import _run_agent_stream


def _scope_and_events():
    events = []
    scope = EventEmitter(CallbackEventSink(events.append)).scope("plan-agent", run_id="run-1")
    return scope, events


def _public_progress_texts(events):
    active = {}
    completed = []
    for event in events:
        payload = event["payload"]
        if event["type"] == "progress_update":
            completed.append(payload["text"])
        elif event["type"] == "progress_start":
            active[payload["progress_id"]] = ""
        elif event["type"] == "progress_delta":
            active[payload["progress_id"]] += payload["delta"]
        elif event["type"] == "progress_end":
            completed.append(active.pop(payload["progress_id"]).strip())
    return completed


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


def test_turn_mode_emits_progress_and_reply_summary_without_react_labels():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        public_tool_events=True,
        suppress_pre_tool_text=True,
    )

    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta='Activity: {"summary":"已确认原始数据，接下来开始提取。"}\n',
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta=(
                'Activity: {"observation":"同步结果已生成。",'
                '"analysis":"需要检查产物完整性。",'
                '"action":"核对输出。"}\n'
            ),
        )
    )
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="处理完成。"))
    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-1"))

    assert _public_progress_texts(events) == [
        "已确认原始数据，接下来开始提取。",
        "同步结果已生成。 需要检查产物完整性。 核对输出。",
    ]
    assert events[-1] == {
        "type": "reply_summary",
        "source": "plan-agent",
        "run_id": "run-1",
        "parent_run_id": None,
        "timestamp": events[-1]["timestamp"],
        "payload": {"text": "处理完成。", "reply_id": "reply-1"},
    }


def test_turn_mode_streams_only_answer_segment_with_reply_identity_and_spacing():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        emit_answer_delta_events=True,
        public_tool_events=True,
        suppress_pre_tool_text=True,
    )

    adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-stream"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="内部草稿不会进入消息。\n"))
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta='Activity: {"summary":"检查已完成，正在汇总结果。"}\nAns',
        )
    )
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="wer:\n"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="你好，"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=" "))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="结果已准备好。\n\n- 可以继续处理"))
    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-stream"))

    assert _public_progress_texts(events) == ["检查已完成，正在汇总结果。"]
    answer_events = [
        (event["type"], event["payload"])
        for event in events
        if event["type"] in {"answer_delta", "reply_summary"}
    ]
    assert answer_events == [
        (
            "answer_delta",
            {"delta": "你好， 结果已准备好。\n\n", "reply_id": "reply-stream"},
        ),
        ("answer_delta", {"delta": "- 可以继续处理", "reply_id": "reply-stream"}),
        (
            "reply_summary",
            {"text": "你好， 结果已准备好。\n\n- 可以继续处理", "reply_id": "reply-stream"},
        ),
    ]


def test_turn_mode_streams_safe_natural_activity_across_model_deltas():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_progress_events=True,
        public_tool_events=True,
        suppress_pre_tool_text=True,
    )

    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="Act"))
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta="ivity: 已确认原始数据，",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta="接下来开始提取。\n",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_CALL_START",
            tool_call_id="extract-1",
            tool_call_name="extract_and_sync_navigation_data_tool",
        )
    )

    progress = [event for event in events if event["type"].startswith("progress_")]
    assert [event["type"] for event in progress] == [
        "progress_start",
        "progress_delta",
        "progress_delta",
        "progress_end",
    ]
    progress_id = progress[0]["payload"]["progress_id"]
    assert [event["payload"] for event in progress] == [
        {"progress_id": progress_id},
        {"progress_id": progress_id, "delta": "已确认原始数据，"},
        {"progress_id": progress_id, "delta": "接下来开始提取。"},
        {"progress_id": progress_id},
    ]
    assert _public_progress_texts(events) == ["已确认原始数据，接下来开始提取。"]
    assert not any(
        event["type"] == "progress_update"
        and event["payload"].get("text") == "正在提取并同步导航数据，这一步可能需要一些时间。"
        for event in events
    )


@pytest.mark.parametrize(
    "unsafe_fragment",
    [
        "已读取 /media/internal/result.json，",
        "密码是 11，",
        "Authorization: Bearer secret-token，",
        "Authorization，sk-abcdefgh。",
        "Password，11。",
        "API key 是 sk-abcdefgh，",
        "密码为 11，",
        "当前任务ID是 nav_secret，",
        "计划ID：plan_12345678，",
        "会话是 123e4567-e89b-12d3-a456-426614174000，",
        "准备调用 hidden_internal_tool，",
    ],
)
def test_turn_mode_drops_unsafe_activity_fragments_before_public_streaming(
    unsafe_fragment,
):
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_progress_events=True,
        suppress_pre_tool_text=True,
    )

    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta=f"Activity: {unsafe_fragment}",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta="接下来核对处理结果。\n",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_CALL_START",
            tool_call_id="inspect-1",
            tool_call_name="inspect_navigation_artifact_state_tool",
        )
    )

    assert _public_progress_texts(events) == [
        "正在核对原始数据、传感器与可用处理条件。"
    ]
    serialized = json.dumps(events, ensure_ascii=False)
    assert unsafe_fragment not in serialized


def test_progress_id_is_stable_when_an_open_reply_is_replayed():
    def project_once():
        scope, events = _scope_and_events()
        adapter = AgentScopeEventAdapter(scope, emit_progress_events=True)
        adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-stable"))
        adapter.accept(
            SimpleNamespace(
                type="TEXT_BLOCK_DELTA",
                delta="Activity: 已确认原始数据，接下来开始提取。\n",
            )
        )
        return [
            event["payload"]["progress_id"]
            for event in events
            if event["type"] == "progress_start"
        ][0]

    assert project_once() == project_once()


def test_natural_activity_deduplicates_adjacent_repeated_updates():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope, emit_progress_events=True)

    for _ in range(2):
        adapter.accept(
            SimpleNamespace(
                type="TEXT_BLOCK_DELTA",
                delta="Activity: 正在核对数据，接下来生成方案。\n",
            )
        )

    assert _public_progress_texts(events) == ["正在核对数据，接下来生成方案。"]


def test_turn_mode_never_publishes_plain_text_without_answer_marker():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        emit_answer_delta_events=True,
    )

    adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-no-answer"))
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta="I inspected the inputs and should now decide which internal action to take.",
        )
    )
    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-no-answer"))

    assert events == []


@pytest.mark.parametrize(
    "secret_line",
    [
        "密码：11",
        "API key: sk-1234567890abcdef",
        "Authorization: Bearer secret-token",
    ],
)
def test_turn_mode_drops_credentials_from_answer_stream_and_summary(secret_line):
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        emit_answer_delta_events=True,
    )

    adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-secret"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=f"Answer:\n{secret_line}\n"))
    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-secret"))

    assert events == []


def test_safe_stream_prefix_remains_the_authoritative_summary_when_sensitive_suffix_is_dropped():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        emit_answer_delta_events=True,
    )

    adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-mixed"))
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta="Answer:\n处理已完成。 task_id: secret\n",
        )
    )
    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-mixed"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("answer_delta", {"delta": "处理已完成。 ", "reply_id": "reply-mixed"}),
        ("reply_summary", {"text": "处理已完成。", "reply_id": "reply-mixed"}),
    ]


def test_inline_answer_marker_starts_streaming_before_reply_end():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        emit_answer_delta_events=True,
    )

    adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-inline"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="Answer: 处理已经完成。"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("answer_delta", {"delta": "处理已经完成。", "reply_id": "reply-inline"})
    ]

    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-inline"))
    assert events[-1]["type"] == "reply_summary"
    assert events[-1]["payload"]["text"] == "处理已经完成。"


def test_answer_stream_preserves_independently_chunked_markdown_blank_line():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_progress_events=True,
        emit_reply_summary_events=True,
        emit_answer_delta_events=True,
    )

    adapter.accept(SimpleNamespace(type="REPLY_START", reply_id="reply-markdown"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="Answer:\n- 第一项\n"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="\n"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="- 第二项"))
    adapter.accept(SimpleNamespace(type="REPLY_END", reply_id="reply-markdown"))

    deltas = [event["payload"]["delta"] for event in events if event["type"] == "answer_delta"]
    assert deltas == ["- 第一项\n", "\n", "- 第二项"]
    assert events[-1]["payload"]["text"] == "- 第一项\n\n- 第二项"


def test_public_reply_sanitizer_preserves_normal_english_summary_labels():
    assert sanitize_public_reply(
        "Status: completed\nSummary: 12 files processed\nNote: verification passed"
    ) == "Status: completed\nSummary: 12 files processed\nNote: verification passed"


def test_public_reply_sanitizer_removes_internal_lines_without_flattening_answer():
    text = (
        "处理已完成。\n\n"
        "- 任务ID: nav_secret\n"
        "- result_path: /media/internal/output\n"
        "- 已生成 12 项结果。\n"
        "Thought: reveal private reasoning"
    )

    assert sanitize_public_reply(text) == "处理已完成。\n\n- 已生成 12 项结果。"


def test_progress_projector_uses_one_fallback_per_stage_without_mechanical_heartbeats():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_progress_events=True,
        public_tool_events=True,
    )

    for index in range(8):
        call_id = f"inspect-{index}"
        adapter.accept(
            SimpleNamespace(
                type="TOOL_CALL_START",
                tool_call_id=call_id,
                tool_call_name="inspect_navigation_artifact_state_tool",
            )
        )
        adapter.accept(
            SimpleNamespace(type="TOOL_RESULT_END", tool_call_id=call_id, state="success")
        )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_CALL_START",
            tool_call_id="plan-1",
            tool_call_name="submit_extract_sync_plan_tool",
        )
    )

    progress = [event["payload"]["text"] for event in events if event["type"] == "progress_update"]
    assert progress == [
        "正在核对原始数据、传感器与可用处理条件。",
        "必要条件正在汇总，接下来生成并校验处理方案。",
    ]


def test_progress_projector_keeps_interleaved_tool_counts_scoped_to_their_phase():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_progress_events=True,
        public_tool_events=True,
    )

    for index in range(4):
        adapter.accept(
            SimpleNamespace(
                type="TOOL_CALL_START",
                tool_call_id=f"inspect-{index}",
                tool_call_name="inspect_navigation_artifact_state_tool",
            )
        )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_CALL_START",
            tool_call_id="plan-1",
            tool_call_name="submit_extract_sync_plan_tool",
        )
    )
    for index in range(4):
        adapter.accept(
            SimpleNamespace(
                type="TOOL_RESULT_END",
                tool_call_id=f"inspect-{index}",
                state="success",
            )
        )

    progress = [event["payload"]["text"] for event in events if event["type"] == "progress_update"]
    assert progress == [
        "正在核对原始数据、传感器与可用处理条件。",
        "必要条件正在汇总，接下来生成并校验处理方案。",
    ]


def test_adapter_closes_assistant_segment_before_tool_call():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_text_events=True,
        emit_final_events=True,
    )

    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="先检查数据。"))
    adapter.accept(SimpleNamespace(type="TOOL_CALL_START", tool_call_id="call-1", tool_call_name="prepare_raw_data"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="检查完成。"))
    adapter.accept(SimpleNamespace(type="REPLY_END"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("assistant_delta", {"delta": "先检查数据。"}),
        ("final", {"text": "先检查数据。"}),
        ("tool_start", {"tool": "prepare_raw_data", "call_id": "call-1", "args": ""}),
        ("tool_end", {"tool": "prepare_raw_data", "call_id": "call-1", "status": "completed", "summary": ""}),
        ("assistant_delta", {"delta": "检查完成。"}),
        ("final", {"text": "检查完成。"}),
    ]


def test_public_activity_mode_projects_react_without_streaming_pre_tool_text():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_text_events=False,
        emit_final_events=True,
        emit_reasoning_events=False,
        emit_activity_events=True,
        public_tool_events=True,
        suppress_pre_tool_text=True,
        activity_title="正在处理导航数据",
    )

    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta=(
                'Activity: {"observation":"发现当前任务已有处理方案。",'
                '"analysis":"需要确认下一步状态。",'
                '"action":"读取当前计划步骤。"}\n'
            ),
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta="Thought: call get_current_plan_step_tool with /media/internal\n",
        )
    )
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="我将调用内部函数。"))
    adapter.accept(
        SimpleNamespace(
            type="TOOL_CALL_START",
            tool_call_id="call-1",
            tool_call_name="get_current_plan_step_tool",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_CALL_DELTA",
            tool_call_id="call-1",
            delta='{"plan_id":"secret-plan"}',
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_TEXT_DELTA",
            tool_call_id="call-1",
            delta='{"path":"/media/internal/result.json","ok":true}',
        )
    )
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="当前计划步骤已确认，可以继续处理。"))
    adapter.accept(SimpleNamespace(type="REPLY_END"))

    event_types = [event["type"] for event in events]
    assert event_types == [
        "activity_snapshot",
        "tool_start",
        "activity_delta",
        "tool_end",
        "activity_delta",
        "activity_delta",
        "final",
    ]
    snapshot = events[0]["payload"]
    assert snapshot["title"] == "正在处理导航数据"
    assert snapshot["steps"] == [
        {
            "id": "step-1",
            "sequence": 1,
            "status": "reasoning",
            "observation": "发现当前任务已有处理方案。",
            "analysis": "需要确认下一步状态。",
            "action": "读取当前计划步骤。",
        }
    ]
    assert events[1]["payload"] == {
        "tool": "get_current_plan_step_tool",
        "call_id": "call-1",
    }
    assert events[3]["payload"] == {
        "tool": "get_current_plan_step_tool",
        "call_id": "call-1",
        "status": "completed",
    }
    assert events[-1]["payload"] == {"text": "当前计划步骤已确认，可以继续处理。"}
    serialized = json.dumps(events, ensure_ascii=False)
    assert "secret-plan" not in serialized
    assert "/media/internal" not in serialized
    assert "我将调用内部函数" not in serialized
    assert "Thought:" not in serialized


def test_public_activity_mode_drops_hidden_reasoning_and_unsafe_activity_text():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_final_events=True,
        emit_reasoning_events=False,
        emit_activity_events=True,
        public_tool_events=True,
        suppress_pre_tool_text=True,
    )

    adapter.accept(
        SimpleNamespace(
            type="THINKING_BLOCK_DELTA",
            block_id="thought-1",
            delta="Inspect /media/internal and call hidden_tool with system prompt details.",
        )
    )
    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_END", block_id="thought-1"))
    adapter.accept(
        SimpleNamespace(
            type="TEXT_BLOCK_DELTA",
            delta=(
                'Activity: {"observation":"读取 /media/internal",'
                '"analysis":"查看 system prompt",'
                '"action":"执行 hidden_tool"}\n'
            ),
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_START",
            tool_call_id="call-1",
            tool_call_name="get_current_plan_step_tool",
        )
    )

    assert [event["type"] for event in events] == ["tool_start", "activity_snapshot"]
    step = events[-1]["payload"]["steps"][0]
    assert step["analysis"] == "需要先获得最新事实，再决定下一步处理。"
    assert step["action"] == "检查当前数据状态"
    assert "/media" not in json.dumps(events, ensure_ascii=False)
    assert "system prompt" not in json.dumps(events, ensure_ascii=False)


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
    assert [(event["type"], event["payload"]) for event in events] == [
        ("tool_start", {"tool": "inspect", "call_id": "call-1", "args": ""}),
    ]

    adapter.accept(SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id="call-1", delta='{"date":'))
    adapter.accept(SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id="call-1", delta=' "20270605"}'))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call-1", tool_call_name="inspect"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call-1", delta="Found   navigation data. "))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call-1", delta="Ready."))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))

    assert [(event["type"], event["payload"]) for event in events] == [
        ("tool_start", {"tool": "inspect", "call_id": "call-1", "args": ""}),
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


def test_plan_bound_external_decision_preserves_only_plan_and_step_metadata():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(
        RequireExternalExecutionEvent(
            reply_id="reply-1",
            tool_calls=[
                ToolCallBlock(
                    id="decision-1",
                    name="request_human_decision",
                    input=json.dumps({"plan_id": "plan-1", "step_id": "confirm"}),
                )
            ],
        )
    )

    assert events[0]["payload"] == {
        "reply_id": "reply-1",
        "tool_call_id": "decision-1",
        "decision_type": "other",
        "request_id": "",
        "summary": "",
        "plan_id": "plan-1",
        "step_id": "confirm",
    }


def test_removed_calibration_external_tool_is_ignored():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(
        RequireExternalExecutionEvent(
            reply_id="reply-1",
            tool_calls=[
                ToolCallBlock(
                    id="confirm-1",
                    name="confirm_navigation_calibration_params_tool",
                    input=json.dumps(
                        {
                            "date": "20270605",
                            "segments": ["20260605_152856"],
                            "runtime_variant": "cjl_0525_with_gridmap",
                        }
                    ),
                )
            ],
        )
    )

    assert events == []


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


def test_offloaded_tool_placeholder_emits_background_not_completed():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(
        scope,
        emit_tool_events=True,
        emit_final_events=True,
        emit_activity_events=True,
        public_tool_events=True,
    )
    placeholder = (
        "<system-reminder>Tool 'extract_and_sync_navigation_data_tool' is "
        "running in background (id=bg-1) for over 10.0s.</system-reminder>"
    )

    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_START",
            tool_call_id="call-1",
            tool_call_name="extract_and_sync_navigation_data_tool",
        )
    )
    adapter.accept(
        SimpleNamespace(
            type="TOOL_RESULT_TEXT_DELTA",
            tool_call_id="call-1",
            delta=placeholder,
        )
    )
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call-1", state="success"))
    adapter.accept(SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="任务已转入后台。"))
    adapter.accept(SimpleNamespace(type="REPLY_END"))

    assert [event["type"] for event in events] == [
        "tool_start",
        "activity_snapshot",
        "tool_background",
        "activity_delta",
        "activity_delta",
        "final",
    ]
    assert events[2]["payload"] == {
        "tool": "extract_and_sync_navigation_data_tool",
        "call_id": "call-1",
        "status": "background",
    }
    assert events[-2]["payload"]["status"] == "background"


@pytest.mark.parametrize(("ok", "status"), [(True, "completed"), (False, "failed")])
def test_background_hint_emits_real_terminal_status_with_original_call_id(ok, status):
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope, public_tool_events=True)
    result = json.dumps({"ok": ok, "status": status}, ensure_ascii=False)

    adapter.accept(
        SimpleNamespace(
            type="HINT_BLOCK",
            source=json.dumps(
                {
                    "label": "tool_output",
                    "sublabel": "extract_and_sync_navigation_data_tool · call-1",
                }
            ),
            hint=(
                "<system-notification>Tool "
                "'extract_and_sync_navigation_data_tool' running in background "
                f"(id=call-1) has completed.\n\nResult:\n\n{result}"
                "</system-notification>"
            ),
        )
    )

    assert events == [
        {
            **events[0],
            "type": "tool_end",
            "payload": {
                "tool": "extract_and_sync_navigation_data_tool",
                "call_id": "call-1",
                "status": status,
            },
        }
    ]


def test_unparseable_background_hint_uses_durable_status_resolver():
    scope, events = _scope_and_events()
    resolutions = []
    adapter = AgentScopeEventAdapter(
        scope,
        public_tool_events=True,
        background_status_resolver=lambda tool, call_id: resolutions.append(
            (tool, call_id)
        )
        or "failed",
    )

    adapter.accept(
        SimpleNamespace(
            type="HINT_BLOCK",
            source=json.dumps(
                {
                    "label": "tool_output",
                    "sublabel": "extract_and_sync_navigation_data_tool · call-1",
                }
            ),
            hint="background result could not be decoded",
        )
    )

    assert resolutions == [("extract_and_sync_navigation_data_tool", "call-1")]
    assert events[0]["payload"] == {
        "tool": "extract_and_sync_navigation_data_tool",
        "call_id": "call-1",
        "status": "failed",
    }


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
