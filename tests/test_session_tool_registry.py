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
from vla_data_juicer_agents.navigation.agents import EXECUTOR_AGENT_INSTRUCTIONS
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import create_navigation_execution_tools
from vla_data_juicer_agents.tools.vla.run_workflow import (
    _normalize_model,
    _normalize_segments,
    continue_vla_workflow,
    run_vla_workflow,
)


def test_tool_registry_exposes_vla_workflow_tool():
    names = [spec.name for spec in list_tool_specs()]

    assert "vla_run_workflow" in names
    assert get_tool_spec("vla_run_workflow").effects == "execute"


def test_session_runtime_exposes_pending_workflow_context():
    state = SessionState()
    state.pending_workflow_run_dir = "/tmp/run"
    state.pending_workflow_status = "waiting_for_user_confirmation"
    state.pending_workflow_input_type = "calibration_confirmation"
    runtime = SessionToolRuntime(state=state)

    assert runtime.context_payload()["pending_workflow"] == {
        "run_dir": "/tmp/run",
        "status": "waiting_for_user_confirmation",
        "input_type": "calibration_confirmation",
    }


def test_tool_registry_exposes_continue_workflow_tool():
    names = [spec.name for spec in list_tool_specs()]

    assert "vla_run_workflow" in names
    assert "vla_continue_workflow" in names
    assert get_tool_spec("vla_continue_workflow").effects == "execute"


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
    assert "response_language" in properties


def test_navigation_execution_tools_include_calibration_confirmation_tool():
    tools = create_navigation_execution_tools(settings=NavigationSettings(), dry_run=True)
    names = [tool.name for tool in tools]

    assert "confirm_navigation_calibration_params_tool" in names


def test_executor_instructions_use_generic_step_to_tool_mapping_for_calibration_confirmation():
    assert 'same name plus "_tool"' in EXECUTOR_AGENT_INSTRUCTIONS
    assert "confirm_navigation_calibration_params_tool" not in EXECUTOR_AGENT_INSTRUCTIONS
    assert "When executing confirm_navigation_calibration_params" in EXECUTOR_AGENT_INSTRUCTIONS


def test_session_prompt_routes_complex_vla_requests_to_workflow():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "call vla_run_workflow exactly once" in prompt
    assert "Navigation planning uses sensor bindings and processing_profile" in prompt
    assert "Fixed platform names are hints, not hard execution categories" in prompt
    assert "response_language" in prompt
    assert "Set vla_run_workflow.response_language to the user's language" in prompt
    assert "If session_context.pending_workflow exists" in prompt
    assert "继续上次任务" in prompt
    assert "vla_continue_workflow exactly once" in prompt
    assert "do not call vla_run_workflow" in prompt
    assert "Never scan old run directories" in prompt
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

    assert "Progress: <one or two concise, action-oriented sentences" in prompt
    assert "reasoning summary and the next action" in prompt
    assert "not the full hidden chain-of-thought" in prompt
    assert "Do not reveal draft notes" in prompt


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

    turn = runtime.begin_turn(scope, cancellation)
    ctx = _tool_context(runtime)

    assert runtime.active_scope is scope
    assert runtime.active_cancellation is cancellation
    assert ctx.runtime_values == {
        "session_runtime": runtime,
        "event_emitter": runtime.event_emitter,
        "event_scope": scope,
        "cancellation": cancellation,
        "emit_event": runtime.emit_event,
    }

    runtime.end_turn(turn)
    assert runtime.active_scope is None
    assert runtime.active_cancellation is None


def test_session_runtime_old_turn_end_does_not_clear_new_owner():
    runtime = SessionToolRuntime(state=SessionState())
    first = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="first"), CancellationContext())
    second = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="second"), CancellationContext())

    runtime.end_turn(first)

    assert runtime.active_scope is second.scope
    assert runtime.active_cancellation is second.cancellation

    runtime.end_turn(second)
    assert runtime.active_scope is None


def test_session_runtime_background_task_keeps_originating_turn_context():
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)

    async def scenario():
        first = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="first"), CancellationContext())
        release = asyncio.Event()

        async def invoke_from_first_turn():
            await release.wait()
            ctx = _tool_context(runtime)
            await runtime.invoke_tool("inspect", {}, lambda: {"ok": True, "message": "done"})
            return ctx.runtime_values

        child = asyncio.create_task(invoke_from_first_turn())
        second = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="second"), CancellationContext())
        release.set()
        values = await child
        runtime.end_turn(first)
        assert runtime.active_scope is second.scope
        runtime.end_turn(second)
        return first, values

    first, values = asyncio.run(scenario())

    assert values["event_scope"] is first.scope
    assert values["cancellation"] is first.cancellation
    assert [event["run_id"] for event in events] == ["first", "first"]


def test_session_runtime_closed_background_turn_cannot_emit_or_invoke_into_new_turn():
    events = []
    executed = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)

    async def scenario():
        first = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="first"), CancellationContext())
        release = asyncio.Event()

        async def invoke_after_first_ends():
            await release.wait()
            runtime.emit_event("reasoning", summary="too late")
            with pytest.raises(TurnCancelled, match="ended"):
                await runtime.invoke_tool(
                    "inspect",
                    {},
                    lambda: executed.append("ran") or {"ok": True},
                )

        child = asyncio.create_task(invoke_after_first_ends())
        runtime.end_turn(first)
        second = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="second"), CancellationContext())
        release.set()
        await child
        runtime.end_turn(second)

    asyncio.run(scenario())

    assert executed == []
    assert events == []


def test_session_runtime_idle_inspection_ignores_stale_child_binding():
    runtime = SessionToolRuntime(state=SessionState())

    async def scenario():
        turn = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="first"), CancellationContext())
        release = asyncio.Event()

        async def inspect_after_end():
            await release.wait()
            return runtime.active_scope, runtime.active_cancellation, runtime.turn_context()

        child = asyncio.create_task(inspect_after_end())
        runtime.end_turn(turn)
        release.set()
        return turn, await child

    turn, (active_scope, active_cancellation, bound_context) = asyncio.run(scenario())

    assert active_scope is None
    assert active_cancellation is None
    assert bound_context is None
    assert turn.closed is True


def test_session_runtime_emit_event_uses_normalized_active_scope():
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    turn = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="turn-1"), CancellationContext())

    runtime.emit_event("reasoning", summary="checking")

    runtime.end_turn(turn)
    assert [(event["type"], event["source"], event["run_id"]) for event in events] == [
        ("reasoning", "main", "turn-1"),
    ]
    assert events[0]["payload"] == {"summary": "checking"}


def test_session_runtime_emit_event_recursively_redacts_payload():
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    turn = runtime.begin_turn(runtime.event_emitter.scope("main", run_id="turn-1"), CancellationContext())

    runtime.emit_event(
        "reasoning",
        summary="before authorization=Basic dXNlcjpwYXNz; after",
        details={"nested": [{"token": "hidden"}, "Bearer standalone-secret"]},
    )

    runtime.end_turn(turn)
    assert events[0]["payload"] == {
        "summary": "before authorization=[REDACTED]; after",
        "details": {"nested": [{"token": "[REDACTED]"}, "Bearer [REDACTED]"]},
    }


def test_session_runtime_public_redaction_handles_secret_key_variants_without_false_positives():
    payload = {
        "api_key": "underscore-secret",
        "api-key": "dash-secret",
        "apiKey": "camel-secret",
        "apikey": "compact-secret",
        "authorization_header": "Digest username=visible",
        "client_credentials": "client-secret",
        "aws_secret_access_key": "aws-secret",
        "private_key": "private-secret",
        "privateKey": "camel-private-secret",
        "password_hash": "hash-secret",
        "passwordHash": "camel-hash-secret",
        "nested": [
            {"access_token": "access-secret"},
            {"access-token": "dash-access-secret"},
            {"accessToken": "camel-access-secret"},
        ],
        "token_count": 3,
        "secretary": "unchanged",
    }

    redacted = SessionToolRuntime.redact_payload(payload)

    assert redacted == {
        "api_key": "[REDACTED]",
        "api-key": "[REDACTED]",
        "apiKey": "[REDACTED]",
        "apikey": "[REDACTED]",
        "authorization_header": "[REDACTED]",
        "client_credentials": "[REDACTED]",
        "aws_secret_access_key": "[REDACTED]",
        "private_key": "[REDACTED]",
        "privateKey": "[REDACTED]",
        "password_hash": "[REDACTED]",
        "passwordHash": "[REDACTED]",
        "nested": [
            {"access_token": "[REDACTED]"},
            {"access-token": "[REDACTED]"},
            {"accessToken": "[REDACTED]"},
        ],
        "token_count": 3,
        "secretary": "unchanged",
    }


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


@pytest.mark.parametrize(
    ("message", "redacted"),
    [
        (
            "before authorization=Basic dXNlcjpwYXNz; after",
            "before authorization=[REDACTED]; after",
        ),
        (
            "before Authorization: Digest digest-credential, after",
            "before Authorization: [REDACTED]",
        ),
        (
            "before authorization=Bearer whitespace-credential; after",
            "before authorization=[REDACTED]; after",
        ),
        (
            "before authorization=Bearer-hyphen-credential; after",
            "before authorization=[REDACTED]; after",
        ),
    ],
)
def test_session_runtime_redacts_full_authorization_assignment(message, redacted):
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    runtime.begin_turn(runtime.event_emitter.scope("main"), CancellationContext())

    asyncio.run(
        runtime.invoke_tool(
            "inspect",
            {},
            lambda: {"ok": True, "message": message},
        )
    )

    completed = next(event for event in events if event["type"] == "tool_end")
    assert completed["payload"]["summary"] == redacted


@pytest.mark.parametrize(
    ("message", "redacted"),
    [
        (
            'before authorization=Digest username="u", realm="r", response="secret"; safe after',
            "before authorization=[REDACTED]; safe after",
        ),
        (
            "before authorization: ApiKey secret; safe after",
            "before authorization: [REDACTED]; safe after",
        ),
        (
            "before authorization=Token secret; safe after",
            "before authorization=[REDACTED]; safe after",
        ),
        (
            "before authorization=Negotiate secret; safe after",
            "before authorization=[REDACTED]; safe after",
        ),
    ],
)
def test_session_runtime_redacts_authorization_values_without_scheme_enumeration(message, redacted):
    assert SessionToolRuntime.redact_text(message) == redacted


def test_session_runtime_redacts_authorization_until_unquoted_delimiter():
    message = (
        'Authorization=Digest username="u;admin", realm="secret-realm", '
        'response="secret-response"; safe after\n'
        'authorization: Custom value="escaped \\"; quote"; retained'
    )

    redacted = SessionToolRuntime.redact_text(message)

    assert redacted == (
        "Authorization=[REDACTED]; safe after\n"
        "authorization: [REDACTED]; retained"
    )
    assert "secret-realm" not in redacted
    assert "secret-response" not in redacted


def test_session_agent_redacts_secrets_from_streamed_reasoning_events():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class SecretThinkingAgent:
        async def reply_stream(self, msg):
            del msg
            yield SimpleNamespace(
                type="THINKING_BLOCK_DELTA",
                block_id="thinking-1",
                delta="Inspect password=hunter2 before choosing a tool.",
            )
            yield SimpleNamespace(type="THINKING_BLOCK_END", block_id="thinking-1")
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="done")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply")

    session._react_agent = SecretThinkingAgent()

    reply = asyncio.run(session.handle_message_async("inspect"))

    assert reply.text == "done"
    reasoning = next(event for event in events if event["type"] == "reasoning")
    assert reasoning["payload"]["summary"] == (
        "Inspect password=[REDACTED] before choosing a tool."
    )
    assert "hunter2" not in json.dumps(events)


def test_session_runtime_redacts_secrets_from_tool_previews_and_errors():
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    runtime.begin_turn(runtime.event_emitter.scope("main"), CancellationContext())

    asyncio.run(
        runtime.invoke_tool(
            "inspect",
            {"API_KEY": "args-secret", "nested": [{"Authorization": "Bearer hidden"}]},
            lambda: {
                "ok": True,
                "token": "result-secret",
                "message": (
                    "use safe mode; authorization=Bearer-visible-secret; "
                    "password:password-visible; API-Key:dash-visible; ToKeN=case-visible; "
                    "credential:cred-visible; Bearer standalone-visible; note remains"
                ),
            },
        )
    )

    def fail():
        raise ValueError("password=hunter2")

    with pytest.raises(ValueError, match="hunter2"):
        asyncio.run(runtime.invoke_tool("inspect", {"query": "safe"}, fail))

    def interrupt():
        raise TurnCancelled("token=interrupt-secret")

    with pytest.raises(TurnCancelled, match="interrupt-secret"):
        asyncio.run(runtime.invoke_tool("inspect", {}, interrupt))

    serialized = json.dumps(events, ensure_ascii=False)
    assert "args-secret" not in serialized
    assert "Bearer hidden" not in serialized
    assert "result-secret" not in serialized
    assert "Bearer-visible-secret" not in serialized
    assert "password-visible" not in serialized
    assert "dash-visible" not in serialized
    assert "case-visible" not in serialized
    assert "cred-visible" not in serialized
    assert "standalone-visible" not in serialized
    assert "hunter2" not in serialized
    assert "interrupt-secret" not in serialized
    assert serialized.count("[REDACTED]") >= 3
    completed = next(event for event in events if event["type"] == "tool_end")
    assert "use safe mode" in completed["payload"]["summary"]
    assert "note remains" in completed["payload"]["summary"]
    assert [event["payload"]["summary"] for event in events if event["type"] == "tool_end"][-2:] == [
        "Tool execution failed.",
        "Tool execution interrupted.",
    ]


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
    assert events[-1]["payload"]["summary"] == "Tool execution failed."


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


def test_session_agent_redacts_secrets_from_final_event_reply_and_history():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class SecretEchoingAgent:
        async def reply_stream(self, msg):
            del msg
            yield SimpleNamespace(
                type="TEXT_BLOCK_DELTA",
                delta=(
                    "authorization=Basic basic-secret; "
                    "Bearer bearer-secret; password=hunter2; normal text"
                ),
            )
            yield SimpleNamespace(type="REPLY_END", reply_id="reply")

    session._react_agent = SecretEchoingAgent()

    reply = asyncio.run(session.handle_message_async("show result"))

    expected = (
        "authorization=[REDACTED]; "
        "Bearer [REDACTED]; password=[REDACTED]; normal text"
    )
    assert reply.text == expected
    assert events[-1]["payload"]["text"] == expected
    assert session.state.history[-1] == {"role": "assistant", "content": expected}
    serialized = json.dumps(
        {"reply": reply.text, "events": events, "history": session.state.history},
        ensure_ascii=False,
    )
    assert "basic-secret" not in serialized
    assert "bearer-secret" not in serialized
    assert "hunter2" not in serialized


@pytest.mark.parametrize("alias", ["exit", "quit", "q", "退出"])
def test_session_exit_aliases_emit_one_final_and_stop(alias):
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    reply = asyncio.run(session.handle_message_async(alias))

    assert reply.stop is True
    assert reply.interrupted is False
    assert [event["type"] for event in events] == ["final"]
    assert events[0]["payload"] == {
        "text": "Session ended.",
        "stop": True,
        "interrupted": False,
    }
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
    assert failed_reply.text == "Session turn failed. Please try again."
    assert [event["type"] for event in events].count("final") == 2
    assert events[-1]["payload"] == {
        "text": failed_reply.text,
        "stop": False,
        "interrupted": False,
    }
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

    assert failed.text == "Session turn failed. Please try again."
    assert session.request_interrupt() is False
    assert session._tool_runtime.active_scope is None
    assert session._tool_runtime.active_cancellation is None

    resumed = asyncio.run(session.handle_message_async("second"))

    assert resumed.text == "recovered"
    assert session.request_interrupt() is False
    assert [event["type"] for event in events].count("final") == 2


def test_session_history_failure_cannot_suppress_final_event():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    class BrokenHistory(list):
        def append(self, value):
            del value
            raise RuntimeError("credential=private-value")

    session.state.history = BrokenHistory()

    reply = asyncio.run(session.handle_message_async("help"))

    serialized = json.dumps(events, ensure_ascii=False)
    assert reply.text == "Session turn failed. Please try again."
    assert [event["type"] for event in events] == ["final"]
    assert events[0]["payload"] == {
        "text": reply.text,
        "stop": False,
        "interrupted": False,
    }
    assert "private-value" not in serialized
    assert session.request_interrupt() is False


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
    assert events[-1]["payload"]["interrupted"] is True
    assert session.request_interrupt() is False

    resumed = asyncio.run(session.handle_message_async("continue"))
    assert resumed.text == "still reusable"
    assert resumed.interrupted is False
    assert [event["type"] for event in events].count("final") == 2
    assert [event["payload"]["status"] for event in events if event["type"] == "agent_end"] == [
        "interrupted",
        "completed",
    ]


def test_session_pending_interrupt_is_applied_when_turn_installs_cancellation():
    events = []
    session = VLASessionAgent(use_llm_router=False, event_callback=events.append)

    session.prepare_turn()
    assert session.request_interrupt(allow_pending=True) is True

    reply = asyncio.run(session.handle_message_async("help"))

    assert reply.interrupted is True
    assert "中断" in reply.text
    assert session.request_interrupt() is False
    assert [event["type"] for event in events] == ["final"]
    assert events[0]["payload"]["interrupted"] is True


def test_session_late_pending_interrupt_does_not_leak_into_next_turn():
    session = VLASessionAgent(use_llm_router=False)

    session.prepare_turn()
    first = asyncio.run(session.handle_message_async("help"))
    assert first.interrupted is False

    assert session.request_interrupt(allow_pending=True) is False
    second = asyncio.run(session.handle_message_async("help"))

    assert second.interrupted is False


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
    plan_payload = {
        "date": "20270605",
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "go2w",
        "steps": [],
    }

    plan = SimpleNamespace(
        model_dump=lambda mode="json": plan_payload,
        model_dump_json=lambda: json.dumps(plan_payload),
    )

    async def fake_run_plan_agent(
        agent,
        request,
        run_store=None,
        run_dir=None,
        *,
        event_scope=None,
        cancellation=None,
        response_language=None,
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
                response_language,
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
        response_language=None,
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
                response_language,
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
                "response_language": "Chinese",
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
    assert calls[0][10] == "Chinese"
    assert calls[1][:5] == ("execute", "executor-dry-True", plan, True, True)
    assert calls[1][5].source == "navigation.executor"
    assert calls[1][5].parent_run_id == events[0]["run_id"]
    assert calls[1][6] is cancellation
    assert calls[1][7] == "Chinese"
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


def test_vla_run_workflow_records_calibration_confirmation_checkpoint(tmp_path, monkeypatch):
    runtime = SessionToolRuntime(state=SessionState())
    plan_payload = {
        "date": "20270605",
        "segments": ["20260605_152856"],
        "scene_mode": "out",
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "go2w",
        "steps": [],
    }
    plan = SimpleNamespace(
        model_dump=lambda mode="json": plan_payload,
        model_dump_json=lambda: json.dumps(plan_payload),
    )

    async def fake_run_plan_agent(*args, **kwargs):
        return plan

    async def fake_run_executor_agent(*args, **kwargs):
        return "calibration_params_not_confirmed: user did not confirm"

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda model=None, request=None: "plan-agent")
    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.create_executor_agent",
        lambda model=None, dry_run=False, cancellation=None: "executor-agent",
    )
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", fake_run_plan_agent)
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_executor_agent", fake_run_executor_agent)

    ctx = ToolContext(
        working_dir=str(tmp_path),
        artifacts_dir=str(tmp_path / ".djx"),
        runtime_values={"session_runtime": runtime},
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

    assert payload["ok"] is False
    assert payload["status"] == "waiting_for_user_confirmation"
    assert payload["error_type"] == "calibration_params_not_confirmed"
    assert runtime.state.pending_workflow_run_dir == payload["run_dir"]
    assert runtime.state.pending_workflow_status == "waiting_for_user_confirmation"
    assert runtime.state.pending_workflow_input_type == "calibration_confirmation"
    checkpoint = json.loads((Path(payload["run_dir"]) / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "waiting_for_user_confirmation"
    assert checkpoint["waiting_step_id"] == "confirm_navigation_calibration_params"
    assert checkpoint["dry_run"] is True


def test_vla_run_workflow_detects_calibration_pause_from_executor_event(tmp_path, monkeypatch):
    runtime = SessionToolRuntime(state=SessionState())
    plan_payload = {
        "date": "20270605",
        "segments": ["20260605_152856"],
        "scene_mode": "out",
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "go2w",
        "steps": [],
    }
    plan = SimpleNamespace(
        model_dump=lambda mode="json": plan_payload,
        model_dump_json=lambda: json.dumps(plan_payload),
    )

    async def fake_run_plan_agent(*args, **kwargs):
        return plan

    async def fake_run_executor_agent(*args, event_scope=None, **kwargs):
        event_scope.emit(
            "tool_end",
            tool="confirm_navigation_calibration_params",
            call_id="call-1",
            status="failed",
            summary="calibration_params_not_confirmed",
        )
        return "Execution stopped before processing."

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda model=None, request=None: "plan-agent")
    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.create_executor_agent",
        lambda model=None, dry_run=False, cancellation=None: "executor-agent",
    )
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", fake_run_plan_agent)
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_executor_agent", fake_run_executor_agent)

    ctx = ToolContext(
        working_dir=str(tmp_path),
        artifacts_dir=str(tmp_path / ".djx"),
        runtime_values={"session_runtime": runtime},
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

    assert payload["ok"] is False
    assert payload["status"] == "waiting_for_user_confirmation"
    assert payload["final_output"] == "Execution stopped before processing."
    assert runtime.state.pending_workflow_run_dir == payload["run_dir"]
    assert runtime.state.pending_workflow_status == "waiting_for_user_confirmation"
    checkpoint = json.loads((Path(payload["run_dir"]) / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "waiting_for_user_confirmation"


def test_continue_workflow_requires_current_session_pending_state(tmp_path):
    ctx = ToolContext(working_dir=str(tmp_path), runtime_values={})

    payload = asyncio.run(continue_vla_workflow(ctx, {"user_input": "确认"}))

    assert payload["ok"] is False
    assert payload["status"] == "needs_active_session_workflow"
    assert payload["error_type"] == "no_pending_workflow"


def test_continue_workflow_rejects_non_pending_run_dir(tmp_path):
    runtime = SessionToolRuntime(state=SessionState())
    pending_run = tmp_path / "runs" / "20270605" / "pending"
    other_run = tmp_path / "runs" / "20270605" / "other"
    pending_run.mkdir(parents=True)
    other_run.mkdir(parents=True)
    runtime.state.pending_workflow_run_dir = str(pending_run)
    runtime.state.pending_workflow_status = "waiting_for_user_confirmation"
    runtime.state.pending_workflow_input_type = "calibration_confirmation"
    ctx = ToolContext(working_dir=str(tmp_path), runtime_values={"session_runtime": runtime})

    payload = asyncio.run(
        continue_vla_workflow(
            ctx,
            {"user_input": "确认", "run_dir": str(other_run)},
        )
    )

    assert payload["ok"] is False
    assert payload["status"] == "needs_active_session_workflow"
    assert payload["error_type"] == "run_dir_not_pending"


def test_continue_workflow_marks_paused_on_terminate(tmp_path):
    runtime = SessionToolRuntime(state=SessionState())
    run_dir = tmp_path / "runs" / "20270605" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "date": "20270605",
                "segments": ["20260605_152856"],
                "scene_mode": "out",
                "processing_profile": "parameterized_navigation_v1",
                "platform_hint": "go2w",
                "steps": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "waiting_for_user_confirmation",
                "waiting_step_id": "confirm_navigation_calibration_params",
                "pending_input_type": "calibration_confirmation",
                "date": "20270605",
                "segments": ["20260605_152856"],
                "scene_mode": "out",
            }
        ),
        encoding="utf-8",
    )
    runtime.state.pending_workflow_run_dir = str(run_dir)
    runtime.state.pending_workflow_status = "waiting_for_user_confirmation"
    runtime.state.pending_workflow_input_type = "calibration_confirmation"
    ctx = ToolContext(working_dir=str(tmp_path), runtime_values={"session_runtime": runtime})

    payload = asyncio.run(continue_vla_workflow(ctx, {"user_input": "终止"}))

    assert payload["ok"] is False
    assert payload["status"] == "paused_by_user"
    assert runtime.state.pending_workflow_run_dir == str(run_dir)
    assert runtime.state.pending_workflow_status == "paused_by_user"
    assert runtime.state.pending_workflow_input_type == "calibration_confirmation"
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "paused_by_user"


def test_continue_workflow_confirms_and_executes_existing_plan_without_replanning(tmp_path, monkeypatch):
    runtime = SessionToolRuntime(state=SessionState())
    run_dir = tmp_path / "runs" / "20270605" / "run"
    run_dir.mkdir(parents=True)
    plan_payload = {
        "date": "20270605",
        "segments": ["20260605_152856"],
        "scene_mode": "out",
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "go2w",
        "steps": [
            {
                "step_id": "confirm_navigation_calibration_params",
                "tool_name": "confirm_navigation_calibration_params",
                "arguments": {},
                "preconditions": [],
                "human_blocking": True,
                "failure_behavior": "stop",
                "effects": "read",
            },
            {
                "step_id": "prepare_raw_data",
                "tool_name": "prepare_raw_data",
                "arguments": {"date": "20270605"},
                "preconditions": ["confirm_navigation_calibration_params"],
                "effects": "execute",
            },
        ],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan_payload), encoding="utf-8")
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "waiting_for_user_confirmation",
                "waiting_step_id": "confirm_navigation_calibration_params",
                "pending_input_type": "calibration_confirmation",
                "date": "20270605",
                "segments": ["20260605_152856"],
                "scene_mode": "out",
            }
        ),
        encoding="utf-8",
    )
    runtime.state.pending_workflow_run_dir = str(run_dir)
    runtime.state.pending_workflow_status = "waiting_for_user_confirmation"
    runtime.state.pending_workflow_input_type = "calibration_confirmation"
    captured = {}

    async def fail_replan(*args, **kwargs):
        raise AssertionError("continue_vla_workflow must not call Plan-Agent")

    async def fake_run_executor_agent(agent, workflow_plan, *args, **kwargs):
        captured["agent"] = agent
        captured["plan"] = workflow_plan
        captured["run_dir"] = kwargs["run_dir"]
        captured["response_language"] = kwargs["response_language"]
        return "resumed"

    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda *args, **kwargs: "plan-agent")
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", fail_replan)
    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.create_executor_agent",
        lambda model=None, dry_run=False, cancellation=None: f"executor-{model}-{dry_run}",
    )
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_executor_agent", fake_run_executor_agent)
    ctx = ToolContext(working_dir=str(tmp_path), runtime_values={"session_runtime": runtime})

    payload = asyncio.run(
        continue_vla_workflow(
            ctx,
            {"user_input": "确认", "model": "qwen-test", "response_language": "Chinese"},
        )
    )

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["final_output"] == "resumed"
    assert runtime.state.pending_workflow_run_dir is None
    assert runtime.state.pending_workflow_status is None
    assert runtime.state.pending_workflow_input_type is None
    assert captured["agent"] == "executor-qwen-test-False"
    assert captured["run_dir"] == run_dir
    assert captured["response_language"] == "Chinese"
    assert [step.step_id for step in captured["plan"].steps] == ["prepare_raw_data"]
    assert captured["plan"].steps[0].preconditions == []
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "completed"


def test_continue_workflow_preserves_dry_run_from_checkpoint(tmp_path, monkeypatch):
    runtime = SessionToolRuntime(state=SessionState())
    run_dir = tmp_path / "runs" / "20270605" / "run"
    run_dir.mkdir(parents=True)
    plan_payload = {
        "date": "20270605",
        "segments": ["20260605_152856"],
        "scene_mode": "out",
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "go2w",
        "steps": [
            {
                "step_id": "confirm_navigation_calibration_params",
                "tool_name": "confirm_navigation_calibration_params",
                "arguments": {},
                "preconditions": [],
                "human_blocking": True,
                "failure_behavior": "stop",
                "effects": "read",
            },
            {
                "step_id": "prepare_raw_data",
                "tool_name": "prepare_raw_data",
                "arguments": {"date": "20270605"},
                "preconditions": ["confirm_navigation_calibration_params"],
                "effects": "execute",
            },
        ],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan_payload), encoding="utf-8")
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "waiting_for_user_confirmation",
                "waiting_step_id": "confirm_navigation_calibration_params",
                "pending_input_type": "calibration_confirmation",
                "date": "20270605",
                "segments": ["20260605_152856"],
                "scene_mode": "out",
                "dry_run": True,
            }
        ),
        encoding="utf-8",
    )
    runtime.state.pending_workflow_run_dir = str(run_dir)
    runtime.state.pending_workflow_status = "waiting_for_user_confirmation"
    runtime.state.pending_workflow_input_type = "calibration_confirmation"
    captured = {}

    def fake_create_executor_agent(model=None, dry_run=False, cancellation=None):
        captured["dry_run"] = dry_run
        return "executor-agent"

    async def fake_run_executor_agent(*args, **kwargs):
        return "resumed"

    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.create_executor_agent",
        fake_create_executor_agent,
    )
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_executor_agent", fake_run_executor_agent)
    ctx = ToolContext(working_dir=str(tmp_path), runtime_values={"session_runtime": runtime})

    payload = asyncio.run(continue_vla_workflow(ctx, {"user_input": "继续"}))

    assert payload["ok"] is True
    assert captured["dry_run"] is True
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["dry_run"] is True


def test_vla_run_workflow_prefers_scope_emitter_over_independent_emitter(tmp_path, monkeypatch):
    scope_events = []
    independent_events = []
    scope_emitter = EventEmitter(CallbackEventSink(scope_events.append))
    independent_emitter = EventEmitter(CallbackEventSink(independent_events.append))
    parent_scope = scope_emitter.scope("session", run_id="session-run")
    plan = SimpleNamespace(
        model_dump=lambda mode="json": {
            "date": "20270605",
            "processing_profile": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "steps": [],
        },
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


def test_vla_run_workflow_redacts_child_events_in_callback_and_jsonl(tmp_path, monkeypatch):
    events = []
    runtime = SessionToolRuntime(state=SessionState(), event_callback=events.append)
    parent_scope = runtime.event_emitter.scope("main", run_id="session-run")
    plan = SimpleNamespace(
        model_dump=lambda mode="json": {
            "date": "20270605",
            "processing_profile": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "steps": [],
        },
    )

    async def fake_run_plan_agent(*args, event_scope=None, **kwargs):
        event_scope.emit("reasoning", summary="password=hunter2")
        return plan

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent",
        fake_run_plan_agent,
    )
    ctx = ToolContext(
        working_dir=str(tmp_path),
        runtime_values={
            "event_scope": parent_scope,
            "event_emitter": runtime.event_emitter,
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

    persisted_text = (Path(payload["run_dir"]) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "hunter2" not in json.dumps(events)
    assert "hunter2" not in persisted_text
    assert next(event for event in events if event["type"] == "reasoning")[
        "payload"
    ] == {"summary": "password=[REDACTED]"}
    assert json.loads(persisted_text.splitlines()[1])["payload"] == {
        "summary": "password=[REDACTED]"
    }


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
