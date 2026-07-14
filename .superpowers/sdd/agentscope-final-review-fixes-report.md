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

## Round 2 final re-review fixes

Baseline: `041dae4`

The second review was handled with the same review-verification, systematic-debugging,
and test-first process. Six independent RED reproductions were recorded before any
round-2 product code changed, and a root-cause checkpoint was sent to the parent agent.

### Critical: cancellation lease survives ToolOffload foreground cleanup

Root cause: `_start_agent_run()` removed the session's only CancellationContext when
the first ChatService reply ended. Official ToolOffload had already returned its
placeholder, but its production plan FunctionTool was still awaiting an
`asyncio.to_thread` worker. A later Stop could cancel the AgentScope drain task, not
the worker thread or its child process.

RED: an actual production-created plan FunctionTool, official ToolOffload timeout, and
a cooperative blocking worker reached foreground cleanup. Stop canceled the drain,
but the worker did not exit within 500ms and stopped only when the discarded token was
manually canceled.

GREEN: runtime cancellation state is now a per-session collection of generation-aware
leases. Each lease counts foreground users and retains every active tool-call ID.
ToolOutcome start retains the currently bound token; real terminal/exception releases
that tool; reply cleanup only releases its foreground reference. Stop and delete cancel
all retained leases before AgentScope task cancellation, and a completed Stop discards
them. Multiple tools and a newer generation do not overwrite an older running lease.

Self-review found and test-first fixed one additional edge: a direct wakeup can reuse
an old tool lease. RunBoundary now acquires/releases a foreground reference even when
the token already exists, so the last background terminal cannot prune the token while
the wakeup reasoning run is still stoppable.

### Important: normal tool terminal is one SQLite transaction

Root cause: runtime first committed `finish_tool_run()` and then opened a separate
transaction for `append_public_event()`. An injected append failure left a successful
ledger row with no canonical terminal event.

GREEN: `finish_tool_run_with_terminal_event()` performs the conditional first-terminal
transition, canonical event construction and validation, dedupe/sequence allocation,
event insert, and session touch in one `BEGIN IMMEDIATE`. Encoding or insert failure
rolls the ledger back to running. Concurrent success/failure has exactly one winner;
late outcomes return `None` and do not invoke the event factory. Runtime publishes only
after the transaction commits.

### Important: resumed HITL continuations have stable distinct identities

Root cause: AgentScope's awaiting continuation path does not emit REPLY_START, and each
ChatService run rebuilds projection middleware. Each continuation therefore started at
`reply_id=''`, ordinal zero, producing the same `hash(session::0)`.

GREEN: projection seeds continuation identity from the input event's durable reply ID
and idempotency key (falling back to its stable event ID), plus the event type and
ordinal. A normal REPLY_START switches back to the established reply identity format.
Two sequential continuations in one reply are distinct; replay/restart of the same
input is stable. Non-plan human continuation IDs are now canonical SHA-256 values
rather than random UUIDs and carry the same idempotency key in metadata.

### Important: stopped ToolOffload hints cannot cross into a new generation

Root cause: suppressing an `inputs=None` wakeup left its already queued HintBlock in
the inbox. A new turn cleared the current stop boundary, then framework InboxMiddleware
drained and injected the old result before any extra DataPilot reasoning middleware.

GREEN: `tool_execution_provenance` durably records tool, generation, and delivery
suppression. Explicit Stop marks delivery suppression in the same transaction as the
stop boundary and terminal events; the tombstone survives later generations and is
removed with the session. Stop suppresses older-generation tools too, covering a
retained worker after a newer turn. Fresh and existing v1 databases create the table
without resetting sessions.

The production AgentScope app receives a DataPilot-owned message-bus wrapper. It keeps
the official single inbox drain but filters after drain and before InboxMiddleware
injection, using an exact persisted `tool_name · tool_call_id` comparison. It does not
patch AgentScope. A real deterministic bus/official Inbox test proves an old stopped
hint is dropped while a normal new-generation success hint in the same batch is kept.
The wrapper also explicitly delegates AgentScope's asynchronous context-manager
lifecycle; a focused RED caught that Python special methods are not forwarded through
`__getattr__`, and the lifecycle regression now passes.

### Important: ownerless thinking advances the frontend cursor

Root cause: the reply-owner guard returned before the defensive thinking guard. An
ownerless thinking event did not advance sequence, so the following valid REPLY_START
looked like a gap and was rejected too.

GREEN: the exact three AgentScope thinking event types are consumed before ownership
checks and never reduced into message content. The ownerless-thinking-then-valid-reply
RED now reaches sequence four and renders only the public answer.

### Important: correlation IDs are never sanitized

Root cause: explicit Stop sanitized the entire terminal event. A valid public
tool_call_id containing a private identity substring was rewritten in the event while
the ledger retained the original ID.

GREEN: Stop constructs its fixed public summary directly and never sends correlation,
status, or dedupe fields through identity sanitization. Success/failure continue to
sanitize only summary and error_type before the atomic ledger/event write. The
adversarial tool_call_id is byte-identical in response, ledger, and event.

### Round 2 verification

- Full backend: `917 passed, 1 warning` (the existing Starlette deprecation warning).
- Full frontend: `177 passed`.
- Frontend production build: passed (Vite 1626 modules transformed).
- TypeScript `npx tsc --noEmit`: passed.
- Python `compileall`: passed.
- `git diff --check`: passed.
- AgentScope runtime assembly smoke test with the owned message-bus wrapper: passed.

## Round 3 patch-review fixes

Baseline: `cfb9f51`

The final patch review found one delete-ordering safety issue, one inbox-validation
failure mode, and one MessageBus signature mismatch. Tests were added and observed
failing before the small production changes.

### Delete cancels every retained lease before destructive session deletion

Root cause: `delete_web_session()` interleaved one lease cancellation with one
AgentScope SessionService deletion. With two mappings, the first deletion began while
the second mapping's production worker token was still live.

RED: two retained tokens and a SessionService callback asserted that every token was
canceled at the first delete call. The endpoint returned 409 because the second token
was not canceled yet.

GREEN: deletion now has three explicit phases. It first materializes all mappings and
attempts cancellation for every retained lease. Cancellation exceptions are collected
without skipping later leases in the same session or later mappings; any failure aborts before the first destructive
AgentScope delete and reaches the existing generic 409 boundary without exposing a
private identity. Only a successful cancel-all phase may delete mapped AgentScope
sessions. Lease discard happens only after every mapped deletion succeeds, preserving
the existing idempotent retry semantics when a later mapped deletion fails. Control
rows and navigation control state continue through the established finalization path.

### Inbox filtering preserves AgentScope validation and fails open after drain

Root cause: the stop-aware bus parsed the raw `source` field before AgentScope's
official InboxMiddleware validation. A malformed/non-HintBlock payload could forge an
exact stopped sublabel and be silently dropped. A tombstone SQLite exception occurred
after destructive queue drain and propagated, permanently losing the batch.

RED: a mixed batch containing normal, malformed exact-stopped, valid stopped, and
unrelated payloads lost both malformed entries; an injected SQLite lookup error raised
after drain. The original wrapper also failed the default and positional forms of the
AgentScope MessageBus API.

GREEN: each entry is classified with AgentScope 2.0.4 `HintBlock.model_validate`.
Validation failures are retained byte-for-byte so official InboxMiddleware still
raises its canonical ValidationError. Only a valid HintBlock with the complete official
two-field source and an exact persisted sublabel match is removed. Filtering exceptions
after drain log a warning and return the complete original `(entry_id, payload)` batch
in original order; this deliberately fails open rather than losing acknowledged
messages. A real InboxMiddleware integration verifies malformed input remains visible
to official validation.

The wrapper now exposes `queue_drain(key, max_count=100)`, matching AgentScope 2.0.4.
Default, positional, and keyword max-count calls all delegate correctly.

### Round 3 verification

- Focused delete/inbox/store/runtime groups: `208 passed, 1 warning`.
- Full backend: `924 passed, 1 warning` (the existing Starlette deprecation warning).
- Python `compileall`: passed.
- `git diff --check`: passed.
- No frontend files changed in round 3.
