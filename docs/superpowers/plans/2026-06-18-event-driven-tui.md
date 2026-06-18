# Event-Driven Multi-Agent TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-turn terminal UI that renders normalized events from the main Agent, navigation workflow, Plan-Agent, Executor-Agent, and tools, with cooperative per-turn interruption and a transport-neutral event contract suitable for a future Web UI.

**Architecture:** A small event core publishes a stable JSON envelope to callback, JSONL, and composite sinks. AgentScope stream events are normalized at `_run_agent_stream()`, while `SessionController` runs a persistent `VLASessionAgent` in a worker thread and the TUI main thread drains and renders events. A shared cancellation context interrupts active Agents and process groups without ending the session.

**Tech Stack:** Python 3.11+, AgentScope 2.0.1, Pydantic 2, Rich, threading/queue, asyncio, pytest.

---

## File Structure

- Create `src/vla_data_juicer_agents/core/events.py`: normalized event envelope, scopes, emitters, and sinks.
- Create `src/vla_data_juicer_agents/core/cancellation.py`: per-turn cancellation flag, Agent registration, and context binding.
- Create `src/vla_data_juicer_agents/adapters/agentscope/events.py`: AgentScope event normalization and progress-summary cleanup.
- Create `src/vla_data_juicer_agents/tui/models.py`: renderer-independent transcript state.
- Create `src/vla_data_juicer_agents/tui/event_adapter.py`: pure `apply_event()` state transitions.
- Create `src/vla_data_juicer_agents/tui/controller.py`: worker-thread session ownership and event queue.
- Create `src/vla_data_juicer_agents/tui/app.py`: Rich transcript renderer, spinner, input loop, and key handling.
- Create `src/vla_data_juicer_agents/tui/__init__.py`: TUI package exports.
- Modify `navigation/workflow.py`: use the AgentScope adapter and propagate event/cancellation contexts.
- Modify `tools/vla/run_workflow.py`: create workflow/child scopes and JSONL sink.
- Modify `navigation/agents.py` and `navigation/execution_tools.py`: inject cancellation into Executor tools and request concise progress text.
- Modify `navigation/subprocess_runner.py`: replace blocking `subprocess.run()` with cancellable process-group polling.
- Modify `capabilities/session/runtime.py`, `toolkit.py`, and `orchestrator.py`: persistent turn context, normalized tool/final events, interruption, and exit aliases.
- Modify `session_cli.py`, `pyproject.toml`, and `README.md`: route the conversational entry point through the TUI and document controls.
- Add focused tests under `tests/test_events.py`, `tests/test_cancellation.py`, `tests/test_agentscope_event_adapter.py`, `tests/test_tui_event_adapter.py`, `tests/test_tui_controller.py`, and `tests/test_tui_app.py`; extend existing session, workflow, Agent, and subprocess tests.

## Task 1: Normalized Event Core

**Files:**
- Create: `src/vla_data_juicer_agents/core/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write failing event-envelope and sink tests**

```python
from pathlib import Path

from vla_data_juicer_agents.core.events import (
    CallbackEventSink,
    CompositeEventSink,
    EventEmitter,
    JsonlEventSink,
)


def test_child_scope_emits_serializable_parent_linked_event():
    captured = []
    emitter = EventEmitter([CallbackEventSink(captured.append)])
    workflow = emitter.scope("navigation.workflow", run_id="workflow_1", parent_run_id="turn_1")
    plan = workflow.child("navigation.plan", run_id="plan_1")

    event = plan.emit("reasoning", summary="检查原始数据")

    assert event["type"] == "reasoning"
    assert event["source"] == "navigation.plan"
    assert event["run_id"] == "plan_1"
    assert event["parent_run_id"] == "workflow_1"
    assert event["payload"] == {"summary": "检查原始数据"}
    assert captured == [event]


def test_composite_sink_isolates_a_failing_sink():
    captured = []

    def fail(_event):
        raise RuntimeError("sink unavailable")

    emitter = EventEmitter([CompositeEventSink([CallbackEventSink(fail), CallbackEventSink(captured.append)])])
    event = emitter.scope("main", run_id="turn_1").emit("final", text="done", stop=False)
    assert captured == [event]


def test_jsonl_sink_writes_one_json_object_per_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    emitter = EventEmitter([JsonlEventSink(path)])
    emitter.scope("main", run_id="turn_1").emit("final", text="done", stop=False)
    assert '"source": "main"' in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `.venv/bin/pytest tests/test_events.py -q`

Expected: collection fails with `ModuleNotFoundError: ...core.events`.

- [ ] **Step 3: Implement the event envelope, scopes, and sinks**

Implement `events.py` with these public APIs:

```python
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

_logger = logging.getLogger(__name__)


class EventSink(Protocol):
    def __call__(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class CallbackEventSink:
    callback: Callable[[dict[str, Any]], None]

    def __call__(self, event: dict[str, Any]) -> None:
        self.callback(dict(event))


@dataclass(frozen=True)
class JsonlEventSink:
    path: Path

    def __call__(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class CompositeEventSink:
    sinks: tuple[EventSink, ...]

    def __init__(self, sinks: Iterable[EventSink]):
        object.__setattr__(self, "sinks", tuple(sinks))

    def __call__(self, event: dict[str, Any]) -> None:
        for sink in self.sinks:
            try:
                sink(event)
            except Exception as exc:
                _logger.debug("event sink failed: %s", exc)


class EventEmitter:
    def __init__(self, sinks: Iterable[EventSink] = ()) -> None:
        self._sinks = tuple(sinks)

    def with_sink(self, sink: EventSink) -> "EventEmitter":
        return EventEmitter((*self._sinks, sink))

    def scope(self, source: str, *, run_id: str | None = None, parent_run_id: str | None = None) -> "EventScope":
        return EventScope(self, source, run_id or f"run_{uuid4().hex[:12]}", parent_run_id)

    def publish(self, event: dict[str, Any]) -> None:
        CompositeEventSink(self._sinks)(event)


@dataclass(frozen=True)
class EventScope:
    emitter: EventEmitter
    source: str
    run_id: str
    parent_run_id: str | None = None

    def child(self, source: str, *, run_id: str | None = None) -> "EventScope":
        return self.emitter.scope(source, run_id=run_id, parent_run_id=self.run_id)

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "type": event_type,
            "source": self.source,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "payload": payload,
        }
        self.emitter.publish(event)
        return event
```

- [ ] **Step 4: Run event tests**

Run: `.venv/bin/pytest tests/test_events.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/core/events.py tests/test_events.py
git commit -m "feat: add normalized agent event core"
```

## Task 2: Turn Cancellation and Cancellable Subprocesses

**Files:**
- Create: `src/vla_data_juicer_agents/core/cancellation.py`
- Modify: `src/vla_data_juicer_agents/navigation/subprocess_runner.py`
- Test: `tests/test_cancellation.py`
- Test: `tests/test_navigation_runtime.py`

- [ ] **Step 1: Write failing cancellation tests**

```python
import asyncio
import sys
import threading
import time

import pytest

from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled, bind_cancellation
from vla_data_juicer_agents.navigation.subprocess_runner import run_command


def test_cancel_interrupts_each_registered_agent_once():
    calls = []

    class Agent:
        async def interrupt(self):
            calls.append("interrupt")

    async def scenario():
        cancellation = CancellationContext()
        async with cancellation.track_agent(Agent()):
            assert cancellation.cancel() is True
            assert cancellation.cancel() is False
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert calls == ["interrupt"]


def test_run_command_terminates_process_group_when_turn_is_cancelled():
    cancellation = CancellationContext()
    timer = threading.Timer(0.15, cancellation.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with bind_cancellation(cancellation):
            with pytest.raises(TurnCancelled):
                run_command([sys.executable, "-c", "import time; time.sleep(30)"])
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
```

- [ ] **Step 2: Verify both tests fail because cancellation APIs are absent**

Run: `.venv/bin/pytest tests/test_cancellation.py -q`

Expected: collection fails importing `core.cancellation`.

- [ ] **Step 3: Implement the shared cancellation context**

Create `core/cancellation.py` with a thread-safe flag, an async Agent registration context, and a context variable used by synchronous tools:

```python
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


class TurnCancelled(RuntimeError):
    pass


class CancellationContext:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._agents: dict[int, tuple[Any, asyncio.AbstractEventLoop]] = {}

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TurnCancelled("The current turn was interrupted.")

    def cancel(self) -> bool:
        with self._lock:
            if self.cancelled:
                return False
            self._cancelled.set()
            registrations = tuple(self._agents.values())
        for agent, loop in registrations:
            if not loop.is_closed():
                asyncio.run_coroutine_threadsafe(agent.interrupt(), loop)
        return True

    @asynccontextmanager
    async def track_agent(self, agent: Any):
        key = id(agent)
        with self._lock:
            self._agents[key] = (agent, asyncio.get_running_loop())
        try:
            self.raise_if_cancelled()
            yield
        finally:
            with self._lock:
                self._agents.pop(key, None)


_CURRENT: ContextVar[CancellationContext | None] = ContextVar("vla_turn_cancellation", default=None)


@contextmanager
def bind_cancellation(cancellation: CancellationContext | None) -> Iterator[None]:
    token = _CURRENT.set(cancellation)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_cancellation() -> CancellationContext | None:
    return _CURRENT.get()
```

- [ ] **Step 4: Replace `subprocess.run()` with polling `Popen()`**

Preserve `CommandRecord` behavior. Start commands with `start_new_session=True`, poll through `communicate(timeout=0.1)`, check `current_cancellation()`, and terminate the process group with `SIGTERM` followed by `SIGKILL` after a one-second grace period. Raise `TurnCancelled` after cleanup; preserve `subprocess.TimeoutExpired` for configured timeouts. Dry-run remains an immediate return.

Use this process-group cleanup helper and polling loop:

```python
def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1.0)
    except ProcessLookupError:
        return


process = subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
started = time.monotonic()
while True:
    try:
        stdout, stderr = process.communicate(timeout=0.1)
        break
    except subprocess.TimeoutExpired:
        cancellation = current_cancellation()
        if cancellation is not None and cancellation.cancelled:
            _terminate_process_group(process)
            raise TurnCancelled("The current turn was interrupted.")
        if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
            _terminate_process_group(process)
            raise
```

- [ ] **Step 5: Run focused and existing runtime tests**

Run: `.venv/bin/pytest tests/test_cancellation.py tests/test_navigation_runtime.py tests/test_navigation_execution_tools_dry_run.py -q`

Expected: all tests pass; existing command records and fake `run_command` functions remain compatible because cancellation is context-bound rather than added to every call signature.

- [ ] **Step 6: Commit**

```bash
git add src/vla_data_juicer_agents/core/cancellation.py src/vla_data_juicer_agents/navigation/subprocess_runner.py tests/test_cancellation.py tests/test_navigation_runtime.py
git commit -m "feat: support cooperative turn cancellation"
```

## Task 3: AgentScope Event Adapter

**Files:**
- Create: `src/vla_data_juicer_agents/adapters/agentscope/events.py`
- Modify: `src/vla_data_juicer_agents/adapters/agentscope/__init__.py`
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py:279-379`
- Test: `tests/test_agentscope_event_adapter.py`
- Test: `tests/test_navigation_agents.py`

- [ ] **Step 1: Write failing normalization tests**

Use `SimpleNamespace(type=..., ...)` events to verify:

```python
from types import SimpleNamespace

from vla_data_juicer_agents.adapters.agentscope.events import AgentScopeEventAdapter
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter


def make_adapter(*, source: str):
    captured = []
    emitter = EventEmitter([CallbackEventSink(captured.append)])
    scope = emitter.scope(source, run_id="agent_1", parent_run_id="workflow_1")
    return captured, AgentScopeEventAdapter(scope, emit_tool_events=True)


def test_thinking_block_becomes_one_natural_language_reasoning_event():
    captured, adapter = make_adapter(source="navigation.plan")
    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_START", block_id="b1"))
    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_DELTA", block_id="b1", delta="Thought: 先检查原始数据。"))
    adapter.accept(SimpleNamespace(type="THINKING_BLOCK_END", block_id="b1"))
    assert captured[-1]["type"] == "reasoning"
    assert captured[-1]["payload"]["summary"] == "先检查原始数据。"


def test_tool_result_events_share_call_id_and_bounded_preview():
    captured, adapter = make_adapter(source="navigation.executor")
    adapter.accept(SimpleNamespace(type="TOOL_CALL_START", tool_call_id="call_1", tool_call_name="prepare_raw_data_tool"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_START", tool_call_id="call_1", tool_call_name="prepare_raw_data_tool"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_TEXT_DELTA", tool_call_id="call_1", delta="prepared"))
    adapter.accept(SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call_1", state="success"))
    assert [(e["type"], e["payload"]["call_id"]) for e in captured] == [
        ("tool_start", "call_1"), ("tool_end", "call_1")
    ]
    assert captured[-1]["payload"]["summary"] == "prepared"
```

- [ ] **Step 2: Run tests and verify the adapter import fails**

Run: `.venv/bin/pytest tests/test_agentscope_event_adapter.py -q`

Expected: collection fails importing `adapters.agentscope.events`.

- [ ] **Step 3: Implement `AgentScopeEventAdapter`**

The adapter constructor accepts `EventScope` and `emit_tool_events`. It maintains dictionaries keyed by block/tool call ID. Implement `accept(event)` with these mappings:

- accumulate `THINKING_BLOCK_DELTA`; on `THINKING_BLOCK_END`, strip `Thought:`/`思考：`, collapse whitespace, keep at most two sentences and 240 characters, then emit `reasoning`;
- remember names and argument deltas from `TOOL_CALL_*`;
- emit `tool_start` on `TOOL_RESULT_START` only when `emit_tool_events=True`;
- accumulate tool result text and emit `tool_end` on `TOOL_RESULT_END`, converting state to `completed`, `failed`, or `interrupted`;
- ignore text reply deltas because `_run_agent_stream()` still owns final-text aggregation.

Export `AgentScopeEventAdapter` and `summarize_progress` from `adapters/agentscope/__init__.py`.

- [ ] **Step 4: Extend `_run_agent_stream()` with explicit execution context**

Add keyword-only parameters:

```python
async def _run_agent_stream(
    agent,
    prompt: str,
    run_store: WorkflowRunStore | None = None,
    run_dir: Path | None = None,
    *,
    event_scope: EventScope | None = None,
    cancellation: CancellationContext | None = None,
    emit_tool_events: bool = True,
) -> str:
```

Create a no-sink scope when omitted so existing callers remain valid. Emit `agent_start` once before confirmation rounds and exactly one `agent_end` in success, failure, or interruption paths. Wrap the loop in `async with cancellation.track_agent(agent)` when cancellation exists; call `raise_if_cancelled()` before every confirmation round. Feed every AgentScope event to the adapter and keep the current raw-event JSONL write temporarily for backward compatibility; Task 4 replaces that file sink with normalized JSONL.

- [ ] **Step 5: Run adapter and existing stream tests**

Run: `.venv/bin/pytest tests/test_agentscope_event_adapter.py tests/test_navigation_agents.py -q`

Expected: all tests pass, including ten confirmation rounds and plan parsing.

- [ ] **Step 6: Commit**

```bash
git add src/vla_data_juicer_agents/adapters/agentscope/events.py src/vla_data_juicer_agents/adapters/agentscope/__init__.py src/vla_data_juicer_agents/navigation/workflow.py tests/test_agentscope_event_adapter.py tests/test_navigation_agents.py
git commit -m "feat: normalize AgentScope stream events"
```

## Task 4: Navigation Workflow Event and Cancellation Propagation

**Files:**
- Modify: `src/vla_data_juicer_agents/tools/vla/run_workflow.py`
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py:314-379`
- Modify: `src/vla_data_juicer_agents/navigation/agents.py:37-154`
- Modify: `src/vla_data_juicer_agents/navigation/execution_tools.py:930-1010`
- Test: `tests/test_session_tool_registry.py`
- Test: `tests/test_navigation_agents.py`

- [ ] **Step 1: Write a failing child-event propagation test**

Extend `test_session_tool_registry.py` with a `ToolContext.runtime_values` containing an emitter and cancellation. Fake Plan/Executor runners must assert:

```python
assert event_scope.source == "navigation.plan"
assert event_scope.parent_run_id == workflow_scope.run_id
assert cancellation is shared_cancellation
```

After the workflow returns, assert captured lifecycle sources are ordered as `navigation.workflow`, `navigation.plan`, `navigation.executor`, and that the JSONL artifact contains normalized envelopes with `source` and `parent_run_id`.

- [ ] **Step 2: Verify the test fails on missing keyword arguments**

Run: `.venv/bin/pytest tests/test_session_tool_registry.py::test_vla_workflow_propagates_event_scopes -q`

Expected: failure because `run_vla_workflow()` does not pass event scopes or cancellation.

- [ ] **Step 3: Build workflow and child scopes**

In `run_vla_workflow()`:

```python
base_emitter = ctx.runtime_values.get("event_emitter") or EventEmitter()
cancellation = ctx.runtime_values.get("cancellation") or CancellationContext()
workflow_scope = base_emitter.with_sink(JsonlEventSink(run_dir / "events.jsonl")).scope(
    "navigation.workflow",
    parent_run_id=getattr(ctx.runtime_values.get("event_scope"), "run_id", None),
)
plan_scope = workflow_scope.child("navigation.plan")
executor_scope = workflow_scope.child("navigation.executor")
```

Emit workflow `agent_start`/`agent_end` around orchestration. Pass `plan_scope`, `executor_scope`, and `cancellation` into `run_plan_agent()` and `run_executor_agent()`. Remove raw AgentScope event writes to the same JSONL file so only normalized events remain.

- [ ] **Step 4: Propagate context into Agent runs and Executor tools**

Add `event_scope` and `cancellation` keyword parameters to `run_plan_agent()` and `run_executor_agent()` and pass them to `_run_agent_stream()`.

Change `create_executor_agent(model=None, dry_run=False, cancellation=None)` and `build_execution_tools(dry_run=False, cancellation=None)`. Every bound execution tool must call `cancellation.raise_if_cancelled()` before work and execute its existing function inside `with bind_cancellation(cancellation):`. Use one `_invoke_bound(fn, *args, **kwargs)` helper so all nine tools share this behavior.

- [ ] **Step 5: Add concise progress instructions to all three Agent prompts**

Append this semantic requirement to main, Plan, and Executor instructions:

```text
Keep each thinking/progress block to one or two concise, action-oriented sentences. State what was established and what action comes next. Do not repeat the prompt or dump raw tool output.
```

- [ ] **Step 6: Run workflow and Agent tests**

Run: `.venv/bin/pytest tests/test_session_tool_registry.py tests/test_navigation_agents.py tests/test_navigation_execution_tools_dry_run.py -q`

Expected: all tests pass; existing direct tools still work without a cancellation context.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/tools/vla/run_workflow.py src/vla_data_juicer_agents/navigation/workflow.py src/vla_data_juicer_agents/navigation/agents.py src/vla_data_juicer_agents/navigation/execution_tools.py tests/test_session_tool_registry.py tests/test_navigation_agents.py
git commit -m "feat: stream navigation workflow child events"
```

## Task 5: Persistent Main Session Lifecycle

**Files:**
- Modify: `src/vla_data_juicer_agents/capabilities/session/runtime.py`
- Modify: `src/vla_data_juicer_agents/capabilities/session/toolkit.py`
- Modify: `src/vla_data_juicer_agents/capabilities/session/orchestrator.py`
- Test: `tests/test_session_tool_registry.py`

- [ ] **Step 1: Write failing multi-turn, final-event, and interruption tests**

Add tests proving:

```python
reply1 = asyncio.run(session.handle_message_async("记住日期 20270605"))
reply2 = asyncio.run(session.handle_message_async("刚才的日期是什么？"))
assert fake_agent.inputs == 2
assert session.state.history[0]["content"] == "记住日期 20270605"
assert session.state.history[-1]["content"] == reply2.text
assert [e["type"] for e in captured].count("final") == 2
```

Also test `退出` returns `stop=True`, all four exit aliases work, `request_interrupt()` returns `False` while idle, and a cancelled fake stream returns an interrupted final response with `stop=False`.

- [ ] **Step 2: Run focused tests and verify missing lifecycle behavior**

Run: `.venv/bin/pytest tests/test_session_tool_registry.py -q`

Expected: new assertions fail because events use the old flat shape, `退出` is unsupported, and interruption is absent.

- [ ] **Step 3: Convert `SessionToolRuntime` to normalized event scopes**

Construct one `EventEmitter` from the optional callback. Add `begin_turn(scope, cancellation)`, `end_turn()`, and read-only properties for the active scope/cancellation. `invoke_tool()` emits `tool_start` and `tool_end` from the active main scope, includes `call_id`, bounded argument/result summaries, and guarantees a failed end event on exceptions.

Update `_tool_context()` so `runtime_values` contains:

```python
{
    "session_runtime": runtime,
    "event_emitter": runtime.event_emitter,
    "event_scope": runtime.active_scope,
    "cancellation": runtime.active_cancellation,
}
```

- [ ] **Step 4: Add per-turn scope and cancellation ownership to `VLASessionAgent`**

For each non-empty message, create a main scope and `CancellationContext`, call `runtime.begin_turn()`, and clear it in `finally`. Pass the scope/cancellation to `_run_agent_stream(..., emit_tool_events=False)` because `SessionToolRuntime` owns main-tool events.

Implement `request_interrupt()` under a lock:

```python
def request_interrupt(self) -> bool:
    with self._turn_lock:
        cancellation = self._active_cancellation
    return cancellation.cancel() if cancellation is not None else False
```

Emit exactly one `final` event for ordinary, help, exit, failed, and interrupted replies. Add `退出` to the exit aliases. On `TurnCancelled`, return `SessionReply("当前任务已中断，可以继续输入下一条请求。", stop=False, interrupted=True)` and keep the session reusable. Extend `SessionReply` with `interrupted: bool = False`.

- [ ] **Step 5: Run session tests**

Run: `.venv/bin/pytest tests/test_session_tool_registry.py -q`

Expected: all tests pass and final events appear exactly once per turn.

- [ ] **Step 6: Commit**

```bash
git add src/vla_data_juicer_agents/capabilities/session/runtime.py src/vla_data_juicer_agents/capabilities/session/toolkit.py src/vla_data_juicer_agents/capabilities/session/orchestrator.py tests/test_session_tool_registry.py
git commit -m "feat: add persistent interruptible session turns"
```

## Task 6: TUI State and Pure Event Adapter

**Files:**
- Create: `src/vla_data_juicer_agents/tui/__init__.py`
- Create: `src/vla_data_juicer_agents/tui/models.py`
- Create: `src/vla_data_juicer_agents/tui/event_adapter.py`
- Test: `tests/test_tui_event_adapter.py`

- [ ] **Step 1: Write failing approved-layout state tests**

Cover reasoning, tool timing, lifecycle, final messages, and the removed labels:

```python
from datetime import datetime, timezone

from vla_data_juicer_agents.tui.event_adapter import apply_event
from vla_data_juicer_agents.tui.models import TuiState


def event(event_type: str, source: str, **payload):
    return {
        "type": event_type,
        "source": source,
        "run_id": "run_1",
        "parent_run_id": "parent_1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def test_reasoning_uses_source_and_natural_text_without_round_labels():
    state = TuiState()
    apply_event(state, event("reasoning", "navigation.plan", summary="先检查原始片段。"))
    item = state.timeline[-1]
    assert item.kind == "reasoning"
    assert item.source_label == "Plan"
    assert item.text == "先检查原始片段。"
    assert "思考摘要" not in item.text
    assert "第 1 轮" not in item.text


def test_tool_start_drives_spinner_and_tool_end_adds_timeline_item():
    state = TuiState()
    apply_event(state, event("tool_start", "navigation.executor", call_id="c1", tool="prepare_raw_data_tool"))
    assert state.spinner_text() == "[Executor] running prepare_raw_data_tool"
    assert state.timeline == []
    apply_event(state, event("tool_end", "navigation.executor", call_id="c1", tool="prepare_raw_data_tool", status="completed", summary="prepared"))
    assert state.active_tools == {}
    assert state.timeline[-1].status == "completed"
```

- [ ] **Step 2: Verify imports fail**

Run: `.venv/bin/pytest tests/test_tui_event_adapter.py -q`

Expected: collection fails importing `vla_data_juicer_agents.tui`.

- [ ] **Step 3: Implement renderer-independent models**

Define `TimelineItem`, `ToolCallState`, and `TuiState` dataclasses. State owns `timeline`, `active_agents`, `active_tools`, and tool call order. Implement source labels with this fixed mapping:

```python
SOURCE_LABELS = {
    "main": "Main",
    "navigation.workflow": "Workflow",
    "navigation.plan": "Plan",
    "navigation.executor": "Executor",
}
```

`spinner_text()` selects the oldest active tool, otherwise the deepest active Agent, otherwise returns an empty string.

- [ ] **Step 4: Implement `apply_event(state, event)`**

Read only normalized envelope fields. `reasoning` appends a natural-text item; `tool_start` updates active state without adding a persistent line; `tool_end` closes timing and appends a status item; `agent_start/end` manages hierarchy; `final` appends one assistant item and stores `stop`. Unknown event types add a muted system item rather than raising.

- [ ] **Step 5: Run adapter tests**

Run: `.venv/bin/pytest tests/test_tui_event_adapter.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/vla_data_juicer_agents/tui tests/test_tui_event_adapter.py
git commit -m "feat: add TUI event state adapter"
```

## Task 7: Worker-Thread Session Controller

**Files:**
- Create: `src/vla_data_juicer_agents/tui/controller.py`
- Test: `tests/test_tui_controller.py`

- [ ] **Step 1: Write failing controller tests**

Inject an `agent_factory` so tests need no model credentials. Verify start-once, one active turn, queue draining, result consumption, and interruption:

```python
def test_controller_reuses_one_agent_for_two_turns():
    agent = FakeAgent()
    controller = SessionController(agent_factory=lambda **_: agent)
    controller.start()
    run_turn(controller, "first")
    run_turn(controller, "second")
    assert agent.messages == ["first", "second"]


def test_request_interrupt_targets_running_turn_only():
    agent = BlockingFakeAgent()
    controller = SessionController(agent_factory=lambda **_: agent)
    controller.start()
    controller.submit_turn("run")
    assert controller.request_interrupt() is True
    wait_until_idle(controller)
    assert controller.request_interrupt() is False
```

- [ ] **Step 2: Verify controller import fails**

Run: `.venv/bin/pytest tests/test_tui_controller.py -q`

Expected: collection fails importing `tui.controller`.

- [ ] **Step 3: Implement `SessionController`**

Follow the approved reference pattern: one `queue.Queue`, one persistent Agent, a reentrant lock, and a daemon worker per turn. `_on_agent_event()` copies events into the queue. `submit_turn()` rejects concurrent turns. `consume_turn_result()` joins the finished thread and returns its `SessionReply`; `request_interrupt()` delegates to the persistent Agent.

If an exception escapes the Agent before it emits `final`, the worker must enqueue one normalized `final` event with `source="main"`, `text=f"Session turn failed: {exc}"`, and `stop=False`, then store the matching failed non-stop `SessionReply`. This preserves the rule that the renderer never prints `SessionReply.text` as a second output path and keeps the interactive session usable.

Constructor parameters are `working_dir`, `model`, and optional `agent_factory=VLASessionAgent`; `start()` passes `use_llm_router=True` and `event_callback=self._on_agent_event`.

- [ ] **Step 4: Run controller tests**

Run: `.venv/bin/pytest tests/test_tui_controller.py -q`

Expected: all tests pass with no sleeping longer than 50 ms in polling helpers.

- [ ] **Step 5: Commit**

```bash
git add src/vla_data_juicer_agents/tui/controller.py tests/test_tui_controller.py
git commit -m "feat: add asynchronous TUI session controller"
```

## Task 8: Transcript Renderer and CLI Integration

**Files:**
- Create: `src/vla_data_juicer_agents/tui/app.py`
- Modify: `src/vla_data_juicer_agents/session_cli.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_session_cli.py`
- Test: `tests/test_tui_app.py`

- [ ] **Step 1: Write failing spinner and CLI delegation tests**

Test `_ThinkingSpinner.tick()/clear()` with `io.StringIO`; test timeline rendering with `Console(file=stream, force_terminal=False)`; and change `test_session_cli_one_shot_uses_session_agent` to assert `session_cli.main()` delegates to `run_tui_session(args)` for both one-shot and interactive modes.

Also test the input loop semantics with injected `read_line` and fake controller:

- `EOFError` returns `0`;
- `KeyboardInterrupt` while idle prints the no-running-task hint;
- `KeyboardInterrupt` while a turn runs calls `request_interrupt()` once;
- `exit`, `quit`, `q`, and `退出` end only after the Agent returns `stop=True`.

- [ ] **Step 2: Verify renderer tests fail**

Run: `.venv/bin/pytest tests/test_tui_app.py tests/test_session_cli.py -q`

Expected: collection fails importing `tui.app` and CLI delegation assertions fail.

- [ ] **Step 3: Add Rich and implement the transcript renderer**

Add `"rich>=13.7.0"` to project dependencies.

Implement `_ThinkingSpinner` with `| / - \\` frames, carriage-return updates every 0.35 seconds, and `clear()`. Implement source colors (`Main=magenta`, `Workflow=yellow`, `Plan=blue`, `Executor=bright_red`) and render:

- reasoning as `[Source] natural text` with a colored left marker;
- tool completion as a green/red/yellow bullet plus status, name, elapsed time, and bounded summary;
- Agent start/end with source labels;
- final replies in cyan;
- spinner text from `TuiState.spinner_text()`, adding `(+Ns)` from monotonic active duration.

Implement `run_tui_session(args, *, controller_factory=SessionController, read_line=input, console=None)`. During each turn, poll `controller.drain_events()` every 30 ms, clear the spinner before persistent output, call `apply_event()`, and flush only new timeline items. Drain once more after the worker stops before consuming its result. Do not print `SessionReply.text`; the `final` event already rendered it.

For `--message`, submit exactly one turn, render its events, and exit with `0` unless the result represents an initialization failure. Reject blank one-shot messages before constructing the controller.

- [ ] **Step 4: Reduce `session_cli.py` to argument parsing and delegation**

Keep the existing parser flags and entry point. Replace `_run_one_shot()` and `_run_interactive()` with:

```python
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.message is not None and not str(args.message).strip():
        print("message must not be empty", file=sys.stderr)
        return 2
    return run_tui_session(args)
```

- [ ] **Step 5: Run TUI and CLI tests**

Run: `.venv/bin/pytest tests/test_tui_app.py tests/test_session_cli.py -q`

Expected: all tests pass; captured output contains one final response and no duplicate raw `print()` from Agents or tools.

- [ ] **Step 6: Commit**

```bash
git add src/vla_data_juicer_agents/tui/app.py src/vla_data_juicer_agents/session_cli.py pyproject.toml tests/test_tui_app.py tests/test_session_cli.py
git commit -m "feat: add multi-turn terminal transcript UI"
```

## Task 9: End-to-End Event Chain, Documentation, and Full Verification

**Files:**
- Create: `tests/test_tui_integration.py`
- Modify: `README.md`
- Modify: `src/vla_data_juicer_agents/cli.py` only if test output reveals imports shared with the conversational TUI; do not otherwise change `vla-nav-agent`.

- [ ] **Step 1: Write a fake end-to-end event-chain test**

Build a fake main Agent that emits the approved sequence through the real controller and state adapter:

```text
main reasoning
main tool_start(vla_run_workflow)
navigation.workflow agent_start
navigation.plan agent_start/reasoning/tool_start/tool_end/agent_end
navigation.executor agent_start/reasoning/tool_start/tool_end/agent_end
navigation.workflow agent_end
main tool_end(vla_run_workflow)
main final
```

Assert source ordering, parent IDs, no duplicate `vla_run_workflow` completion, no forbidden summary labels, and exactly one final assistant message. Submit a second turn through the same controller and assert it succeeds.

- [ ] **Step 2: Run the end-to-end integration test**

Run: `.venv/bin/pytest tests/test_tui_integration.py -q`

Expected: pass. Earlier tasks introduced each behavior test-first; this test verifies their assembled event chain without adding a second implementation path.

- [ ] **Step 3: Document usage and controls**

Update the conversational section of `README.md` with:

```text
vla-data-agent

Controls:
- Ctrl+D: exit the session at the input prompt
- exit / quit / q / 退出: end the session normally
- Ctrl+C: interrupt the current turn and keep the session open

The transcript shows grouped Main, Workflow, Plan, and Executor progress summaries and tool events.
`vla-nav-agent plan/run` remains the command-oriented navigation diagnostic entry point.
```

- [ ] **Step 4: Run focused integration and regression tests**

Run: `.venv/bin/pytest tests/test_tui_integration.py tests/test_tui_app.py tests/test_tui_controller.py tests/test_session_tool_registry.py tests/test_navigation_agents.py tests/test_navigation_execution_tools_dry_run.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Run the complete test suite**

Run: `.venv/bin/pytest -q`

Expected: zero failures.

- [ ] **Step 6: Run terminal smoke tests**

Without credentials, verify startup error rendering:

Run: `env -u DASHSCOPE_API_KEY .venv/bin/vla-data-agent --message "hello"`

Expected: exit code `2` and one concise initialization error, without a traceback.

With `DASHSCOPE_API_KEY` and fixture-accessible runtime configuration, run:

```bash
.venv/bin/vla-data-agent --message "请 dry-run 处理 20270605 的室外导航数据"
```

Expected: grouped Main/Workflow/Plan/Executor timeline, transient spinners, natural progress summaries, tool completion events, and one final response. If credentials are unavailable, record that this live smoke test was skipped; automated fake-Agent integration remains mandatory.

- [ ] **Step 7: Commit**

```bash
git add tests/test_tui_integration.py README.md
git commit -m "test: cover multi-agent TUI event flow"
```

## Final Review Checklist

- [ ] Every production behavior was introduced by a failing test observed before implementation.
- [ ] Main, workflow, Plan, Executor, tool, reasoning, interruption, and final events use one normalized envelope.
- [ ] No Agent, workflow, or tool writes directly to the interactive terminal.
- [ ] The TUI does not display `思考摘要`, round numbers, or summary-type labels.
- [ ] The same session handles multiple turns and remains usable after `Ctrl+C`.
- [ ] `Ctrl+D` and all four exit aliases end the session as specified.
- [ ] Active subprocess groups terminate on cancellation.
- [ ] `vla-nav-agent` remains a command-oriented diagnostic CLI.
- [ ] JSONL output and the future Web UI boundary use the same event schema.
- [ ] The complete pytest suite passes with fresh output.
