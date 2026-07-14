# Final branch review fixes report

Date: 2026-07-15
Baseline: `5595a38`

## Process

Applied receiving-code-review, systematic-debugging, and test-driven-development.
Each production change followed a focused reproduction. No product code was changed
before the root-cause checkpoint was sent to the parent agent.

## Critical: synchronous navigation plan tools blocked AgentScope offload

Root cause: `build_plan_bound_execution_tools()` wrapped `_invoke_plan_step()` in a
synchronous function. AgentScope 2.0.4 `FunctionTool.call()` invokes synchronous
functions directly inside its async method, so the official ToolOffload drain task,
timeout, SSE heartbeat, and interrupt scheduling could not run until the body returned.

RED: a production-created extract/sync plan FunctionTool with an 80ms synchronous body
and the official 10ms ToolOffload timeout produced its first event after about 95ms;
the assertion requiring a responsive event loop failed.

GREEN: the plan-bound wrapper is a real async FunctionTool function and calls the
synchronous orchestration through `asyncio.to_thread`. Context variables are copied;
the same thread-safe CancellationContext is passed explicitly; repositories continue
to create per-operation SQLite connections. A second test cancels the async wrapper,
proves the worker thread observes the same cancellation object, exits promptly, and
durably transitions the claimed step to failed.

The initial run and runtime recovery run now both register, bind, track, and release a
CancellationContext. `DataPilotRunBoundaryMiddleware` covers AgentScope's own
WakeupDispatcher path, which calls ChatService directly, and Navigation tool-surface
resolution reads the bound current cancellation at synchronization time.

## Important: background execution remains stoppable in the UI

Root cause: the window treated only reply phase as running. ToolOffload ends the first
reply while its public tool row remains `running`, so the Composer incorrectly showed
Send and allowed a concurrent turn. Interrupting could not be represented from idle.

RED: an idle conversation with a running tool had no accessible Stop button.

GREEN: `hasActiveExecution` combines reply phase with any running tool. Idle background
tools display Stop, block submit while preserving an editable draft, and can enter
interrupting. The last canonical tool terminal returns an ownerless interrupted state
to idle; multiple running tools remain active until all are terminal. A terminal that
races ahead of the stop HTTP response cannot strand local interrupt-pending state.
Restored snapshots with running tools use the same derivation. No backgrounded state
or label was added.

## Important: explicit stop fences late background wakeups

Root cause: first-terminal-wins protected the public tool ledger but did not stop the
official ToolOffload deliverer from pushing a late HintBlock and enqueueing a wakeup.
The prior P0 test explicitly expected a second assistant reply after a cancellation-
resistant late success.

Design and GREEN behavior:

- `session_execution_boundaries` durably stores the current execution generation and
  its stopped generation. It is created in both fresh and existing v1 schemas without
  changing the development schema generation or resetting existing sessions.
- A successful explicit stop marks the current generation stopped in the same SQLite
  transaction that stops open tool rows and appends their terminal events.
- A new user turn increments the generation and clears only the current stop boundary.
- DataPilotToolOutcomeMiddleware checks the persisted public tool status. A late
  ToolResponse or exception for a stopped call becomes `CancelledError`, the official
  ToolOffload no-delivery signal, before its inbox/wakeup deliverer.
- A restarted runtime fail-closes an already queued `input_msg=None` wakeup for a
  stopped generation in DataPilotRunBoundaryMiddleware, acknowledging it with an
  internal empty Msg: no model call and no public reply. A new generation runs normally.

The real ChatService/official ToolOffload stop P0 now asserts one stopped terminal,
one assistant reply, no third model invocation, no wakeup snapshot, and an empty
wakeup queue after a cancellation-resistant late success.

## Important: thinking is not public

Root cause: projection suppressed only TOOL_RESULT events, while MessageList rendered
AgentScope thinking block content verbatim.

RED: real ThinkingBlock start/delta/end events were persisted, and a malicious legacy
snapshot displayed `private chain of thought`.

GREEN: all three AgentScope 2.0.4 thinking event types are suppressed before public
event persistence. The frontend defensively consumes their sequence without reducing
content, and MessageList ignores thinking blocks already present in old/malicious SDK
messages. Legal reply text remains visible.

## Important: public terminal outcome identity sanitization

Root cause: summary and error_type were written into `public_tool_runs` before any
event sanitization, and the event reused those raw values. The identity provider also
omitted the runtime user id and could miss historical mappings after restart.

RED: current/historical agent IDs, AgentScope session IDs, and user ID remained
verbatim in SQLite tool rows and terminal events.

GREEN: one sanitizer runs before ledger mutation and feeds both ledger and terminal
event. Its identity set includes runtime names/IDs, user ID, in-memory bindings, and
all persisted mappings. A private-ID-only error type becomes
`private_runtime_identity`. Identity lookup failure persists only safe generic values
(`Tool execution details unavailable.` / `public_sanitization_failed`). Snapshot and
SSE replay tests scan the serialized public payload and find no original identities;
public tool_call_id remains unchanged.

## Verification

- Focused affected backend groups: 278 passed before the final edge additions.
- Final backend: `905 passed, 1 warning` (`StarletteDeprecationWarning` already known).
- Final frontend: `176 passed`.
- Frontend production build: passed (Vite 1626 modules transformed).
- TypeScript `npx tsc --noEmit`: passed.
- Python `compileall`: passed.
- `git diff --check`: passed.
- Static scans retain no WebSocket/openSessionEvents transport and no public
  backgrounded terminal state.

## Deferred minors

- The complete projection identity set still queries persisted mappings for each
  projected event. Correctness and restart privacy take precedence; cache invalidation
  is a separate performance change.
- `_tool_outcome_locks` remains an unbounded per-runtime session cache, as noted by the
  earlier review. It was not changed in this high-risk correctness/security fix.
