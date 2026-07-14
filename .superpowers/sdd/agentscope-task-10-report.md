# Task 10 implementer report

Status: DONE

## Scope and real AgentScope path

- Added `tests/test_web_agentscope_background_wakeup.py` with three end-to-end paths over installed `agentscope==2.0.4`.
- Extended `tests/navigation_chat_service_harness.py` with an in-memory Redis/message-bus contract and a recording subclass of AgentScope's real `ChatRunRegistry`.
- The tests instantiate the real AgentScope types `ChatService`, `BackgroundTaskManager`, `ToolOffloadMiddleware`, `WakeupDispatcher`, `CancelDispatcher`, and `ChatRunRegistry`.
- The ChatService assembly order exercised by the tests is the framework order: `InboxMiddleware -> StateChangeMiddleware -> ToolOffloadMiddleware -> DataPilotReplyProjectionMiddleware -> DataPilotToolOutcomeMiddleware`.
- ToolOffload's timeout is shortened only at the ChatService test assembly seam. The replacement is a factory that returns an exact installed `ToolOffloadMiddleware` instance with the official `timeout_secs=0.01` constructor parameter. AgentScope source and production timing are unchanged.
- On timeout, the real middleware keeps the inner drain task alive. The inner DataPilot outcome middleware persists the real tool terminal to SQLite. ToolOffload then pushes a `HintBlock` into the fake Redis inbox and calls the real `enqueue_run_trigger`; the real WakeupDispatcher starts a second real ChatService run with `input_msg=None`.

## TDD / RED evidence

Initial command:

```text
.venv/bin/pytest tests/test_web_agentscope_background_wakeup.py -v
```

Initial result: `3 failed`. The actual first ChatService run stopped at `REQUIRE_USER_CONFIRM`, so the controlled tool was never invoked. The event log showed one real model invocation followed by the AgentScope confirmation event. This exposed a harness gap: AgentScope 2.0.4 intentionally returns `ASK` for a plain `FunctionTool`, even when read-only. The harness tool was then given an explicit always-allow permission decision. No production behavior was changed.

After that harness correction, the new regression passed against the existing Task 3/4/6 production wiring. The planned older symptoms (missing wakeup reply or placeholder terminal success) were not present in baseline `8a7d7c9`, so no production correction was necessary.

## Scenario assertions

Failure path:

- Controlled tool remains active beyond the real shortened ToolOffload threshold and returns `{"ok": false, "message": "extract failed", "error_type": "extract_sync_failed"}`.
- Exactly one SQLite tool row exists for `call-1-delayed_extract`: `failure / extract_sync_failed`.
- Exactly one public terminal event exists and its status is `failure`; no `completed` or synthetic success terminal exists.
- Public replay contains exactly two distinct real AgentScope `REPLY_START.reply_id` values: initial offload reply and wakeup reply.

Success path:

- Exactly one SQLite/public `success` terminal exists, with no failure/stopped/placeholder duplicate.
- A distinct second real AgentScope reply is projected by the wakeup run.

Explicit-stop path:

- Stop is submitted through `AgentScopeWebSessionManager.interrupt`, then `AgentScopeRuntime.interrupt_web_session`, AgentScope `ChatService.interrupt`, the fake bus, and the real CancelDispatcher/BackgroundTaskManager.
- The cancellation-resistant controlled tool deliberately returns a late success through the still-running real middleware chain after the explicit stop.
- SQLite and public replay remain exactly `stopped`; the late success cannot overwrite the first terminal.
- The durable `datapilot_human_decision_resolved` event remains `{reason: stopped, all: true}`.
- Because this special cancellation-resistant test tool completes normally after cancellation, AgentScope naturally enqueues a wakeup and the test consumes its actual reply. No wakeup is fabricated.

## Browser-independent delivery evidence

- `SessionEventBus._subscribers` is asserted empty while the initial run is active, when the background task is registered, and synchronously when the background wakeup is enqueued.
- At wakeup enqueue time, a synchronous SQLite snapshot already contains the real terminal tool row and public terminal event, proving outcome persistence precedes wakeup delivery.
- Only after background completion and the wakeup run finish does the test open `stream_session_events(..., after_sequence=0)`. It observes one subscriber during replay and zero after cleanup.
- Replayed sequences are contiguous from 1, and public payload JSON is asserted not to contain the private AgentScope session id, agent record id, user id, or internal display name.

## Verification

```text
.venv/bin/pytest tests/test_web_agentscope_background_wakeup.py -v
3 passed in 0.45s

.venv/bin/pytest -q tests/test_web_agentscope_background_wakeup.py tests/test_datapilot_projection.py tests/test_web_sse.py tests/test_web_agentscope_session.py
133 passed in 1.10s

.venv/bin/pytest -q
894 passed, 1 known Starlette/httpx deprecation warning in 8.48s

.venv/bin/python -m compileall -q src tests/navigation_chat_service_harness.py tests/test_web_agentscope_background_wakeup.py
passed

git diff --check
passed
```

## Production changes

None. The regression validates the integration wiring delivered by earlier tasks without adding sleeps, grace periods, browser-controlled execution, automatic reruns, or AgentScope source modifications.

## Reviewer-oracle fix

Follow-up status: DONE

The three scenarios now share one complete public replay contract. For every
replay opened from sequence zero it asserts:

- sequences are exactly `1..N` with no gap;
- no native AgentScope `TOOL_RESULT_START`, `TOOL_RESULT_TEXT_DELTA`,
  `TOOL_RESULT_DATA_DELTA`, or `TOOL_RESULT_END` event is public;
- exactly one canonical `datapilot_tool_terminal` exists;
- the terminal status and error type match the scenario (`failure /
  extract_sync_failed`, `success / None`, or `stopped / None`);
- exactly two ordered, distinct, runtime-produced `REPLY_START.reply_id`
  values exist, including the cancellation-resistant stop case's natural
  wakeup reply;
- private identity cleanup still holds.

### Mutation / RED proof

After writing the stronger assertions, the production
`SUPPRESSED_TOOL_RESULT_EVENTS` set was temporarily replaced with an empty
set and the failure scenario was run. This mutation was not committed.

```text
.venv/bin/pytest tests/test_web_agentscope_background_wakeup.py::test_real_agentscope_background_failure_replays_after_browser_connects -q
1 failed

AssertionError at:
assert event_types.isdisjoint(FORBIDDEN_PUBLIC_TOOL_RESULT_EVENTS)
```

The captured real AgentScope log also showed
`Synthetic ToolResponse yielded for offloaded tool`, proving the new oracle
failed on the intended outer ToolOffload placeholder event stream. The
suppression set was restored byte-for-byte; `git diff` for
`datapilot_projection.py` is empty.

After restoration:

```text
.venv/bin/pytest tests/test_web_agentscope_background_wakeup.py -v
3 passed in 0.46s

.venv/bin/pytest -q tests/test_web_agentscope_background_wakeup.py tests/test_datapilot_projection.py tests/test_web_sse.py tests/test_web_agentscope_session.py
133 passed in 1.10s

.venv/bin/pytest -q
894 passed, 1 known Starlette/httpx deprecation warning in 8.58s

.venv/bin/python -m compileall -q src tests/navigation_chat_service_harness.py tests/test_web_agentscope_background_wakeup.py
passed

git diff --check
passed
```
