import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter
from vla_data_juicer_agents.core.tool import ToolContext, get_tool_spec, list_tool_specs
from vla_data_juicer_agents.capabilities.session.orchestrator import VLASessionAgent
from vla_data_juicer_agents.capabilities.session.runtime import SessionState, SessionToolRuntime
from vla_data_juicer_agents.capabilities.session.toolkit import _tool_context, get_session_tool_specs
from vla_data_juicer_agents.tools.vla.run_workflow import _normalize_model, _normalize_segments, run_vla_workflow


def test_tool_registry_exposes_vla_workflow_tool():
    names = [spec.name for spec in list_tool_specs()]

    assert "vla_run_workflow" in names
    assert get_tool_spec("vla_run_workflow").effects == "execute"


def test_session_toolkit_prioritizes_vla_workflow_tool():
    specs = get_session_tool_specs()
    names = [spec.name for spec in specs]

    assert names[0] == "vla_run_workflow"


def test_session_toolkit_exposes_vla_workflow_input_schema():
    agent = VLASessionAgent(use_llm_router=False)
    toolkit = agent._build_toolkit()

    schemas = asyncio.run(toolkit.get_tool_schemas())
    workflow_schema = next(schema for schema in schemas if schema["function"]["name"] == "vla_run_workflow")
    properties = workflow_schema["function"]["parameters"]["properties"]

    assert "date" in properties
    assert "segments" in properties
    assert "dry_run" in properties
    assert "approve" in properties


def test_session_prompt_routes_complex_vla_requests_to_workflow():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "call vla_run_workflow exactly once" in prompt
    assert "Do not use deterministic Python keyword routing" in prompt
    assert "Respond in the same language as the user" in prompt


def test_session_prompt_allows_dry_run_only_when_user_says_dry_run():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "Set dry_run=true only when the user explicitly says dry_run" in prompt
    assert "Default to dry_run=false and perform real data processing" in prompt
    assert "Do not infer dry_run=true" in prompt
    assert "inspection without mutation" not in prompt


def test_session_prompt_keeps_dry_run_requests_approved_for_execution():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "dry_run is still an execution mode" in prompt
    assert "do not set approve=false merely because dry_run=true" in prompt


def test_session_prompt_guides_concise_action_oriented_progress():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "progress or thinking updates to one or two action-oriented sentences" in prompt
    assert "State one established fact and the next action" in prompt
    assert "Do not dump or repeat prompts or raw tool results" in prompt


def test_session_agent_builds_real_agentscope_agent(monkeypatch):
    seen = {}

    class FakeModel:
        pass

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr("vla_data_juicer_agents.capabilities.session.orchestrator.create_qwen_model", lambda model=None: FakeModel())
    monkeypatch.setattr("agentscope.agent.Agent", FakeAgent)

    agent = VLASessionAgent(use_llm_router=False)
    built = agent.build_react_agent()

    assert isinstance(built, FakeAgent)
    assert seen["name"] == "VLASessionAgent"
    assert "vla_run_workflow" in seen["system_prompt"]
    assert seen["model"].__class__ is FakeModel
    assert asyncio.run(seen["toolkit"].get_tool("vla_run_workflow")) is not None


def test_session_handle_message_uses_llm_agent_not_keyword_router(monkeypatch):
    session = VLASessionAgent(use_llm_router=False)

    class FakeStreamingAgent:
        async def reply_stream(self, msg):
            assert "请处理 20270605 的导航 VLA 数据" in str(msg.content)
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="已调用 VLA workflow。")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    session._react_agent = FakeStreamingAgent()

    reply = asyncio.run(session.handle_message_async("请处理 20270605 的导航 VLA 数据"))

    assert reply.text == "已调用 VLA workflow。"
    assert session.state.history == [
        {"role": "user", "content": "请处理 20270605 的导航 VLA 数据"},
        {"role": "assistant", "content": "已调用 VLA workflow。"},
    ]


def test_session_runtime_exposes_active_normalized_turn_context():
    runtime = SessionToolRuntime(state=SessionState())
    scope = runtime.event_emitter.scope("main", run_id="turn-1")
    cancellation = CancellationContext()

    runtime.begin_turn(scope, cancellation)
    ctx = _tool_context(runtime)

    assert runtime.active_scope is scope
    assert runtime.active_cancellation is cancellation
    assert ctx.runtime_values == {
        "session_runtime": runtime,
        "event_emitter": runtime.event_emitter,
        "event_scope": scope,
        "cancellation": cancellation,
    }

    runtime.end_turn()
    assert runtime.active_scope is None
    assert runtime.active_cancellation is None


def test_session_runtime_emits_paired_bounded_tool_events():
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    scope = runtime.event_emitter.scope("main", run_id="turn-1")
    runtime.begin_turn(scope, CancellationContext())

    payload = asyncio.run(
        runtime.invoke_tool(
            "inspect",
            {"query": "x" * 500},
            lambda: {"ok": True, "message": "done " + ("y" * 500)},
        )
    )

    assert payload["ok"] is True
    assert [(event["type"], event["source"], event["run_id"]) for event in events] == [
        ("tool_start", "main", "turn-1"),
        ("tool_end", "main", "turn-1"),
    ]
    assert events[0]["payload"]["call_id"] == events[1]["payload"]["call_id"]
    assert len(events[0]["payload"]["args"]) <= 240
    assert events[1]["payload"]["status"] == "completed"
    assert len(events[1]["payload"]["result"]) <= 240
    assert len(events[1]["payload"]["summary"]) <= 240


def test_session_runtime_emits_failed_tool_end_before_reraising():
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    runtime.begin_turn(runtime.event_emitter.scope("main"), CancellationContext())

    def fail():
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        asyncio.run(runtime.invoke_tool("inspect", {}, fail))

    assert [event["type"] for event in events] == ["tool_start", "tool_end"]
    assert events[-1]["payload"]["status"] == "failed"
    assert events[-1]["payload"]["error_type"] == "ValueError"
    assert events[-1]["payload"]["summary"] == "bad input"


def test_session_agent_reuses_agent_across_turns_and_emits_one_final_each():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class FakeStreamingAgent:
        def __init__(self):
            self.inputs = []

        async def reply_stream(self, msg):
            self.inputs.append(str(msg.content))
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=f"reply-{len(self.inputs)}")
            yield SimpleNamespace(type="REPLY_END", reply_id=f"reply-{len(self.inputs)}")

    fake_agent = FakeStreamingAgent()
    session._react_agent = fake_agent

    reply1 = asyncio.run(session.handle_message_async("记住日期 20270605"))
    reply2 = asyncio.run(session.handle_message_async("刚才的日期是什么？"))

    assert reply1.text == "reply-1"
    assert reply2.text == "reply-2"
    assert len(fake_agent.inputs) == 2
    assert session.state.history[0]["content"] == "记住日期 20270605"
    assert session.state.history[-1]["content"] == reply2.text
    assert [event["type"] for event in events].count("final") == 2
    assert all(event["source"] == "main" for event in events)


@pytest.mark.parametrize("alias", ["exit", "quit", "q", "退出"])
def test_session_exit_aliases_emit_one_final_and_stop(alias):
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    reply = asyncio.run(session.handle_message_async(alias))

    assert reply.stop is True
    assert reply.interrupted is False
    assert [event["type"] for event in events] == ["final"]
    assert events[0]["payload"] == {"text": "Session ended.", "stop": True}
    assert session.state.history[-1] == {"role": "assistant", "content": reply.text}


def test_session_help_and_failure_each_emit_exactly_one_final():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    help_reply = asyncio.run(session.handle_message_async("help"))

    class FailingAgent:
        async def reply_stream(self, msg):
            del msg
            raise RuntimeError("model unavailable")
            yield

    session._react_agent = FailingAgent()
    failed_reply = asyncio.run(session.handle_message_async("do work"))

    assert failed_reply.stop is False
    assert failed_reply.interrupted is False
    assert "model unavailable" in failed_reply.text
    assert [event["type"] for event in events].count("final") == 2
    assert events[-1]["payload"] == {"text": failed_reply.text, "stop": False}
    assert session.state.history[-1] == {"role": "assistant", "content": failed_reply.text}
    assert help_reply.text == session.state.history[1]["content"]


def test_session_turn_setup_failure_releases_ownership_and_allows_next_turn():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class FailingHistory(list):
        def __init__(self):
            super().__init__()
            self.failed = False

        def append(self, value):
            if not self.failed:
                self.failed = True
                raise RuntimeError("history unavailable")
            super().append(value)

    class FakeStreamingAgent:
        async def reply_stream(self, msg):
            del msg
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="recovered")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply")

    session.state.history = FailingHistory()
    session._react_agent = FakeStreamingAgent()

    failed = asyncio.run(session.handle_message_async("first"))

    assert "history unavailable" in failed.text
    assert session.request_interrupt() is False
    assert session._tool_runtime.active_scope is None
    assert session._tool_runtime.active_cancellation is None

    resumed = asyncio.run(session.handle_message_async("second"))

    assert resumed.text == "recovered"
    assert session.request_interrupt() is False
    assert [event["type"] for event in events].count("final") == 2


def test_session_request_interrupt_is_idle_safe_and_active_turn_is_reusable():
    events = []
    started = threading.Event()
    result = {}
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class InterruptibleAgent:
        def __init__(self):
            self.calls = 0

        async def reply_stream(self, msg):
            del msg
            self.calls += 1
            if self.calls == 1:
                started.set()
                await asyncio.Event().wait()
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="still reusable")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply")

    fake_agent = InterruptibleAgent()
    session._react_agent = fake_agent

    assert session.request_interrupt() is False

    def run_turn():
        result["reply"] = asyncio.run(session.handle_message_async("start long task"))

    worker = threading.Thread(target=run_turn)
    worker.start()
    assert started.wait(timeout=2)
    assert session.request_interrupt() is True
    assert session.request_interrupt() is False
    worker.join(timeout=2)

    assert worker.is_alive() is False
    interrupted = result["reply"]
    assert interrupted.stop is False
    assert interrupted.interrupted is True
    assert "中断" in interrupted.text
    assert session.request_interrupt() is False

    resumed = asyncio.run(session.handle_message_async("continue"))
    assert resumed.text == "still reusable"
    assert resumed.interrupted is False
    assert [event["type"] for event in events].count("final") == 2
    assert [event["payload"]["status"] for event in events if event["type"] == "agent_end"] == [
        "interrupted",
        "completed",
    ]


def test_session_outer_tool_events_are_normalized_without_stream_duplicates():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class ToolStreamingAgent:
        async def reply_stream(self, msg):
            del msg
            await session._tool_runtime.invoke_tool(
                "vla_run_workflow",
                {"date": "20270605"},
                lambda: {"ok": True, "status": "completed", "message": "workflow done"},
            )
            yield SimpleNamespace(
                type="TOOL_CALL_START",
                tool_call_id="stream-call",
                tool_call_name="vla_run_workflow",
            )
            yield SimpleNamespace(type="TOOL_CALL_DELTA", tool_call_id="stream-call", delta='{"date":"20270605"}')
            yield SimpleNamespace(
                type="TOOL_RESULT_START",
                tool_call_id="stream-call",
                tool_call_name="vla_run_workflow",
            )
            yield SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="stream-call", delta="workflow done")
            yield SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="stream-call", state="success")
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="done")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply")

    session._react_agent = ToolStreamingAgent()

    reply = asyncio.run(session.handle_message_async("run workflow"))

    assert reply.text == "done"
    tool_events = [event for event in events if event["type"].startswith("tool_")]
    assert [event["type"] for event in tool_events] == ["tool_start", "tool_end"]
    assert all(event["source"] == "main" for event in tool_events)
    assert tool_events[0]["payload"]["call_id"] == tool_events[1]["payload"]["call_id"]
    assert tool_events[1]["payload"]["status"] == "completed"


def test_vla_run_workflow_tool_reuses_plan_and_executor_agents(tmp_path, monkeypatch):
    calls = []
    events = []
    emitter = EventEmitter(CallbackEventSink(events.append))
    parent_scope = emitter.scope("session", run_id="session-run")
    cancellation = CancellationContext()

    plan = SimpleNamespace(
        model_dump=lambda mode="json": {"date": "20270605", "dataset_profile": "go2w_like", "steps": []},
        model_dump_json=lambda: json.dumps({"date": "20270605", "dataset_profile": "go2w_like", "steps": []}),
    )

    async def fake_run_plan_agent(
        agent,
        request,
        run_store=None,
        run_dir=None,
        *,
        event_scope=None,
        cancellation=None,
    ):
        calls.append(
            (
                "plan",
                agent,
                request.date,
                request.segments,
                request.scene_mode,
                request.dry_run,
                bool(run_store),
                bool(run_dir),
                event_scope,
                cancellation,
            )
        )
        return plan

    async def fake_run_executor_agent(
        agent,
        workflow_plan,
        run_store=None,
        run_dir=None,
        *,
        event_scope=None,
        cancellation=None,
    ):
        calls.append(
            (
                "execute",
                agent,
                workflow_plan,
                bool(run_store),
                bool(run_dir),
                event_scope,
                cancellation,
            )
        )
        return "execution summary"

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda model=None, request=None: "plan-agent")
    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.create_executor_agent",
        lambda model=None, dry_run=False, cancellation=None: f"executor-dry-{dry_run}",
    )
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", fake_run_plan_agent)
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_executor_agent", fake_run_executor_agent)

    ctx = ToolContext(
        working_dir=str(tmp_path),
        artifacts_dir=str(tmp_path / ".djx"),
        runtime_values={
            "event_emitter": emitter,
            "event_scope": parent_scope,
            "cancellation": cancellation,
        },
    )
    payload = asyncio.run(
        run_vla_workflow(
            ctx,
            {
                "date": "20270605",
                "segments": ["20260605_152856"],
                "scene_mode": "out",
                "dry_run": True,
                "approve": True,
            },
        )
    )

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["final_output"] == "execution summary"
    assert Path(payload["run_dir"]).is_dir()
    assert calls[0][:8] == (
        "plan",
        "plan-agent",
        "20270605",
        ["20260605_152856"],
        "out",
        True,
        True,
        True,
    )
    assert calls[0][8].source == "navigation.plan"
    assert calls[0][8].parent_run_id == events[0]["run_id"]
    assert calls[0][9] is cancellation
    assert calls[1][:5] == ("execute", "executor-dry-True", plan, True, True)
    assert calls[1][5].source == "navigation.executor"
    assert calls[1][5].parent_run_id == events[0]["run_id"]
    assert calls[1][6] is cancellation
    assert [(event["type"], event["source"], event["parent_run_id"], event["payload"]) for event in events] == [
        ("agent_start", "navigation.workflow", "session-run", {}),
        ("agent_end", "navigation.workflow", "session-run", {"status": "completed"}),
    ]
    persisted = [
        json.loads(line)
        for line in (Path(payload["run_dir"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert persisted == events
    assert all(set(event) == {"type", "source", "run_id", "parent_run_id", "timestamp", "payload"} for event in persisted)


def test_vla_run_workflow_prefers_scope_emitter_over_independent_emitter(tmp_path, monkeypatch):
    scope_events = []
    independent_events = []
    scope_emitter = EventEmitter(CallbackEventSink(scope_events.append))
    independent_emitter = EventEmitter(CallbackEventSink(independent_events.append))
    parent_scope = scope_emitter.scope("session", run_id="session-run")
    plan = SimpleNamespace(
        model_dump=lambda mode="json": {"date": "20270605", "dataset_profile": "go2w_like", "steps": []},
    )

    async def fake_run_plan_agent(*args, **kwargs):
        return plan

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda **kwargs: object())
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", fake_run_plan_agent)
    ctx = ToolContext(
        working_dir=str(tmp_path),
        runtime_values={
            "event_scope": parent_scope,
            "event_emitter": independent_emitter,
        },
    )

    payload = asyncio.run(
        run_vla_workflow(
            ctx,
            {
                "date": "20270605",
                "scene_mode": "out",
                "dry_run": True,
                "approve": False,
            },
        )
    )

    assert [(event["type"], event["parent_run_id"]) for event in scope_events] == [
        ("agent_start", "session-run"),
        ("agent_end", "session-run"),
    ]
    assert independent_events == []
    persisted = [
        json.loads(line)
        for line in (Path(payload["run_dir"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert persisted == scope_events


def test_vla_run_workflow_reraises_cancellation_after_interrupted_report(tmp_path, monkeypatch):
    events = []
    cancellation = CancellationContext()
    emitter = EventEmitter(CallbackEventSink(events.append))

    async def cancelled_plan(*args, **kwargs):
        raise TurnCancelled("stop")

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda **kwargs: object())
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", cancelled_plan)
    ctx = ToolContext(
        working_dir=str(tmp_path),
        runtime_values={"event_emitter": emitter, "cancellation": cancellation},
    )

    with pytest.raises(TurnCancelled, match="stop"):
        asyncio.run(
            run_vla_workflow(
                ctx,
                {"date": "20270605", "scene_mode": "out", "dry_run": True},
            )
        )

    run_dir = next((tmp_path / "runs" / "20270605").iterdir())
    report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "interrupted"
    assert report["ok"] is False
    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("agent_end", {"status": "interrupted"}),
    ]


def test_vla_run_workflow_tool_requires_scene_mode(tmp_path):
    ctx = ToolContext(working_dir=str(tmp_path), artifacts_dir=str(tmp_path / ".djx"))

    payload = asyncio.run(
        run_vla_workflow(
            ctx,
            {
                "date": "20270605",
                "segments": ["20260605_152856"],
                "dry_run": True,
                "approve": True,
            },
        )
    )

    assert payload["ok"] is False
    assert payload["status"] == "needs_user_input"
    assert payload["error_type"] == "missing_scene_mode"
    assert "in" in payload["message"]
    assert "out" in payload["message"]


def test_vla_run_workflow_normalizes_llm_string_arguments():
    assert _normalize_segments('["20260605_152856"]') == ["20260605_152856"]
    assert _normalize_segments("20260605_152856,20260605_153000") == [
        "20260605_152856",
        "20260605_153000",
    ]
    assert _normalize_model(None) is None
    assert _normalize_model("") is None
    assert _normalize_model("None") is None
    assert _normalize_model("null") is None
    assert _normalize_model("qwen-plus") == "qwen-plus"
