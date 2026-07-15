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

## Round 4 whole-branch review fixes

Baseline: `958005d`

Five findings were reproduced before production changes. The root-cause checkpoint
included the exact AgentScope 2.0.4 cancellation chain and was approved before the
larger cross-process stop protocol was implemented.

### Rejected turn admission cannot clear a stopped generation

Root cause: `_start_agent_run()` advanced the public execution generation before
`ChatRunRegistry.spawn()`. A duplicate-active rejection therefore cleared the durable
stop fence even though no new user turn was admitted. A later `inputs=None` wakeup
could call the model and publish a reply.

GREEN: generation admission moved into `DataPilotRunBoundaryMiddleware`, which runs
after both local registry spawn and AgentScope's distributed session-run lock. Only a
fresh user `Msg` (or non-empty all-user list) advances the generation. Idle wakeups do
not. The generation is attached to the already registered cancellation lease and is
idempotent for middleware re-entry. Tests cover rejection, retry, restart, accepted
exactly once, and wakeup suppression.

### Native AgentScope correlation fields remain byte-identical

Root cause: recursive identity sanitization rewrote every string, including native
`id`, `reply_id`, `block_id`, and `tool_call_id`. Tool native events could therefore
use a different ID from the canonical public ledger terminal.

GREEN: sanitization is field-aware. The four public correlation fields are preserved
byte-for-byte; `session_id` remains private; agent names and semantic content such as
tool name and delta text remain sanitized. Native start/delta/end events with an
adversarial private-identity substring now reduce with the terminal snapshot into one
tool row rather than an orphan.

### Cross-process Stop waits for owner worker quiescence

Official AgentScope evidence: `ChatService.interrupt()` publishes and returns;
`CancelDispatcher` cancels a process-local chat task and explicitly does not cancel
background work; background task cancellation calls `asyncio_task.cancel()` and also
returns without awaiting completion. Redis publish confirms only the PUBLISH command.
The official session-run lock cannot prove background completion because background
tasks do not hold it.

This was unsafe for production plan tools: their body runs in `asyncio.to_thread`, and
their subprocess exits cooperatively through the project `CancellationContext`. A Stop
handled by runtime A could not see runtime B's in-memory token, yet the old code wrote
`stopped` immediately after fire-and-forget AgentScope cancellation.

GREEN adds a project-owned `StopCoordinator` without patching AgentScope:

- every Web worker subscribes after the AgentScope message bus is live and removes its
  owner records during lifespan shutdown;
- generation-aware owner records use AgentScope's generic Redis registry API and
  heartbeat timestamps; ACK identity binds runtime, AgentScope session, and generation;
- a durable SQLite pending request gives each Web session/generation a stable request
  ID, blocks a new generation, and prevents a racing success/failure terminal;
- Pub/Sub requests are idempotently resent for missing ACKs; all owners across every
  mapped session must ACK; timeout fails closed and leaves the public ledger running;
- `CancellationContext.reserve_worker()` registers the worker before `to_thread` is
  submitted. Only the actual synchronous worker's `finally` calls `finish_worker()`.
  An async wrapper `CancelledError` cannot create a false quiescence signal;
- an owner writes an applied ACK only after setting its cooperative token and every
  tracked worker has actually returned. The requester then performs stopped ledger
  rows, canonical terminal events, stopped generation, and request completion in one
  `BEGIN IMMEDIATE` transaction;
- official AgentScope interrupt/task cancellation remains defense-in-depth after the
  project ACK, but is not treated as proof of completion.

Deterministic shared-bus tests use separate requester and owner runtimes. Before the
owner worker exits, the HTTP task remains pending and SQLite stays `running` with zero
terminal events. A non-cooperative worker times out without a false stopped result;
after the worker exits, retry reuses the same request ID and commits exactly one
`stopped` terminal. Separate tests require the complete multi-mapping owner ACK union.

Public tool cards continue to use only `running`, `success`, `failure`, and the
user-requested `stopped` state. No background-transfer status was introduced.

### Submit admission is synchronously latched without losing later draft edits

Root cause: the Composer cleared its local input, but DataPilotWindow had no synchronous
request-admission latch before the SSE-derived running state. A second edited message
could be submitted while the first HTTP promise was pending. Draft mode created an
orphan persisted session; active mode sent a request the backend later rejected.

GREEN: draft creation and active turns share one synchronous ref latch plus explicit
submitting UI state. The input remains editable. The submitted text is cleared only
when it still owns the draft, later edits are preserved, and failure restores the
original only when the user has not replaced it. HTTP acceptance remains latched
through the HTTP-to-`REPLY_START` gap, including the reverse event-before-response
race. Close, new/select/delete session, and unmount invalidate stale ownership. The
two old tests that explicitly permitted concurrent draft/active submits now enforce a
single request.

### Production Web startup cannot fall back to the legacy controller

Root cause: `VLA_AGENT_ENABLE_AGENTSCOPE=0|false` returned `None`, and `create_app()`
silently built `WebSessionManager(SessionController)`, bypassing the SDK projection and
privacy boundaries described by the production README.

GREEN: the CLI rejects the obsolete disable setting and always constructs AgentScope.
The production app factory fails closed when neither an AgentScope runtime nor an
explicit test controller adapter is supplied. Fake-controller Web API tests and the
independent TUI remain supported; the production default has no legacy controller.

### Round 4 verification

- Full backend: `942 passed, 1 warning` (the existing Starlette deprecation warning).
- Full frontend: `181 passed`.
- Frontend production build: passed (Vite 1626 modules transformed).
- Python `compileall`: passed.
- `git diff --check`: passed.
- Final Web/AgentScope/Stop focused verification: `234 passed, 1 warning`.

### Round 4 pre-commit independent review fixes

The first independent pre-commit review found five paths not covered by the initial
GREEN tests. Each was reproduced and fixed before commit.

1. A Stop ACK originally waited for worker threads but not the tracked AgentScope task
   or lease cleanup. `CancellationContext` now exposes separate agent and worker
   quiescence, while the runtime lease exposes foreground/tool-reference quiescence.
   The owner captures the target lease, sets its token, and waits for all three before
   ACK. A no-worker task with deliberately gated cleanup keeps the requester pending.
2. A same-process owner could disappear before the requester refreshed Redis. The
   requester now freezes the complete expected owner snapshot before any cancellation;
   the coordinator handles local and remote owners through the same loopback protocol.
   Pending requests retain a quiescent lease tombstone until completion, allowing a
   timed-out request to converge on retry with the same request ID.
3. A pending stop initially blocked new user generations and tool outcomes but not an
   already queued wakeup. `should_suppress_wakeup()` now treats both pending and
   completed current-generation stops as fenced. Pending ACK and timeout tests prove
   `inputs=None` performs zero model calls.
4. An HTTP-accepted turn that failed before `REPLY_START` could leave the frontend
   latch set forever. Every accepted user turn now persists and publishes a sanitized
   `datapilot_run_terminal` custom event after ChatRunRegistry cleanup, with exact
   `turn_id` and `success|failure|stopped`. The frontend releases only for its matching
   session/turn and supports terminal-before-HTTP ordering; unrelated terminals do not
   release admission.
5. Although the production default failed closed, the first patch still kept the
   legacy controller adapter inside the production factory. Production `create_app()`
   now unconditionally requires AgentScope and contains no controller factory, legacy
   manager, or drain path. The legacy manager and drain live only in
   `tests/web_legacy_app.py`; the production `web/session_manager.py` was deleted. The
   independent TUI remains unchanged.

Review-driven quiescence initially exposed a lock cycle: Stop held the tool-outcome
lock while waiting for a lease whose cancellation path needed that lock to observe the
pending barrier and release. The corrected sequence durably creates the pending stop
and freezes owners first, then waits for ACK without the outcome lock. Racing outcomes
enter normally, are rejected by the pending barrier, and release their lease. Stop
uses a separate per-session lock to serialize concurrent requests and takes the
outcome lock only for the final short atomic stopped transaction. The former hanging
race test and its success/remote-failure variants now finish in under one second.

A second independent pass found one executor-queue edge. The first worker-lifecycle
patch reserved its token before `asyncio.to_thread`, but cancellation could cancel a
queued executor future before the thread ever started, leaking the token forever.
The production wrapper now creates a strongly referenced worker task and awaits it
through `asyncio.shield`. Cancellation still returns immediately to AgentScope, while
the queued job remains alive, eventually observes the already-cancelled context, and
releases its token in the real worker `finally`. Detached results are explicitly
retrieved. Quiescence-event waits now poll their thread-safe Events asynchronously
rather than consuming another slot in the same potentially saturated executor. A
single-thread saturated-executor test proves cancel-before-start first remains
non-quiescent, then converges after the blocker releases; existing normal and
cancel-after-start plan tests remain green.

The final independent re-review reported no actionable findings and ran an additional
`82 passed` targeted verification over the queued-worker lifecycle, stop lock,
pending fence, and ACK protocol.

## Round 5: admission, official cancellation, delete quiescence, and UI ordering

Baseline: `da7ca44`

### HTTP acceptance now proves durable AgentScope run admission

The local ChatRunRegistry spawn was not a sufficient admission acknowledgement. A
request could return HTTP 200 before the run-boundary middleware acquired AgentScope's
distributed session-run lock, while its cancellation lease still had no generation or
owner record. Stop in that interval could miss the run, complete, and then have the
old run advance the generation and clear the fence.

GREEN uses a durable two-stage admission ticket. The store claims it before any
AgentScope session ensure/upsert, atomically snapshots the
`(generation, stopped_generation)` boundary, and records runtime ownership plus an
expiring lease. Before spawn, the runtime attaches that baseline generation to its
cancellation lease and synchronously refreshes the coordinator owner registry.
Failure to publish the owner rejects without spawning. At the real user-input run
boundary, the store atomically compares the baseline and advances the generation
exactly once. The runtime immediately republishes the admitted generation, rechecks
the durable fence, and only then resolves the admission future awaited by
`_start_agent_run()` and the HTTP endpoint. A heartbeat renews slow pre-admission
work; loss of that lease before admission cancels and rejects the run. Stop-before-
admission cancels the tracked run and leaves the stopped boundary intact; a stale
boundary CAS cannot clear it. Tests cover runtime and HTTP waiting, pre-heartbeat
owner visibility, Stop-before-admission, owner publication failure, slow upsert,
heartbeat failure, crash expiry/reaping, and middleware re-entry.

### Official AgentScope cancellation precedes the owner ACK wait

Round 4 correctly treated project ACK as the quiescence proof, but issued AgentScope's
official background-task cancellation only after that proof. A pure-async offloaded
tool can release its retained tool lease only from its official task's `finally`, so
that ordering deadlocked until the requester timed out.

GREEN freezes the expected owner set first, applies all local cooperative tokens, then
attempts every ChatService interrupt and deduplicated AgentScope background-task cancel
before waiting for the frozen ACK set. Official cancellation is still not itself an
ACK; the owner must observe the lease's real cleanup and write the applied ACK. Any
local or official cancellation transport failure remains fail-closed and retryable. A
deterministic pure-async task test proves the official cancel occurs before owner ACK
and the stopped ledger commits only after task cleanup.

### Delete reuses the distributed quiescence barrier

Deletion previously cancelled only leases visible in the HTTP process and could call
AgentScope SessionService plus navigation cleanup while another worker process was
still executing. Delete now holds the same per-Web-session stop lock and, whenever the
coordinator is live, completes the durable Stop/owner-ACK barrier before its first
destructive operation. A durable deletion marker blocks new admission claims, waits
for live pre-admission tickets, reaps only expired crash tickets, and then re-reads all
AgentScope mappings before deletion. This closes the late-upsert orphan race. ACK
timeout or a partial destructive failure preserves the marker and makes retry
idempotent; the public session is removed only after remote quiescence and mapped
session cleanup complete. A completed durable stop is not needlessly re-coordinated
on retry. Tests cover multiple remote owners, gated upsert, mapping re-read, partial
failure, and restart reaping. Dataset and processing artifacts remain outside session
deletion and are unchanged.

### Active-turn user messages are optimistic but ownership-safe

The frontend used to append the local user row only after HTTP success. An AgentScope
`REPLY_START` SSE event could therefore render an assistant row first. Active submit
now inserts its uniquely identified optimistic user row synchronously before calling
the HTTP API; success performs no second insertion. Failure removes only that exact
`{session_id, message_id}` row. Draft restoration is allowed only while the same
request still owns the session lifecycle and admission, so a late rejection from a
previous session cannot remove a restored message or overwrite the new session's
draft. Five deterministic tests cover reverse SSE/HTTP order, exact rollback among
identical text, edited-draft preservation, success dedupe, and cross-session rejection.

### Round 5 independent review fixes

Independent review found and test-first closed additional race windows:

1. A Stop request frozen at an older generation could ACK while a same-session
   successor owner was already active. Owner handling now cancels and waits for the
   complete frozen-through-target generation union, including an exact tombstone plus
   newer active lease, before ACK. Owner refresh is serialized, and a Redis write that
   raises after mutation removes its possible ghost record best-effort.
2. Admission-ticket release and heartbeat completion could race an already successful
   admission. The admission result is authoritative when both complete in the same
   event-loop turn. A permanent post-admission release error is logged and leaves the
   conservative expiring ticket for later reaping rather than reversing the accepted
   turn or causing a second model run.
3. Delete and session upsert are serialized by the durable admission/deletion state,
   not process-local timing. A delete waits for renewed live tickets and reads mappings
   again only after they drain, so a session created by an in-flight submit cannot be
   orphaned.

The final independent re-review reported no remaining actionable findings and made no
code changes.

### Round 5 verification

- Affected backend suites: `235 passed, 1 warning`.
- Full backend: `965 passed, 1 warning` (the existing Starlette deprecation warning).
- Full frontend: `182 passed`.
- Frontend production build: passed (Vite 1626 modules transformed).
- Python `compileall`: passed.
- `git diff --check`: passed.

## Round 6: draft ordering, deletion mapping fence, and owner cleanup

Baseline: `7a4c469`

### Draft creation is optimistic before SSE and HTTP

The active-session path inserted a local user row before its request, but the first
message of a draft-created session still started SSE and awaited HTTP before appending
the user row. A fast `REPLY_START` could therefore place the assistant first.

The draft path now appends one uniquely identified optimistic user message immediately
after activating the created session and before opening SSE or calling HTTP. Success
does not append it again. Failure removes only the exact session/message pair, and the
existing lifecycle, request ownership, and draft-revision guards prevent a stale
failure from touching another session or overwriting a newer edit. Deterministic tests
cover SSE-before-HTTP ordering and rollback while preserving a newer draft.

### Deletion fences mapping publication and re-reads after quiescence

Delete initially read AgentScope mappings before distributed Stop. A navigation
handoff could publish another mapping during cancellation, after which the destructive
phase reused the stale list and left an internal orphan.

`save_agentscope_session_mapping()` now takes a SQLite `BEGIN IMMEDIATE` lock and
atomically rejects a deletion marker. The only exception is a matching, unexpired
admission ticket owned by the same runtime: that submit started before deletion, so
delete is already waiting for its ticket and must allow it to finish publishing the
mapping. Expired tickets, mismatched runtime identities, and missing tickets are all
rejected. The ticket and runtime identity are carried from HTTP admission through
AgentScope ensure/upsert. If an unadmitted mapping publication fails after upsert, the
runtime restores its in-memory mapping. It deliberately does not delete the internal
AgentScope session because ownership of a deterministic pre-existing ID is not proven.

After admission tickets drain and remote owners quiesce, delete takes a fresh mapping
snapshot immediately before SessionService deletion. This is defense-in-depth for an
older or external writer that bypassed the supported store API. Tests cover direct
rejection, an actual SQLite write-lock race, a gated pre-delete upsert, and a mapping
injected during remote quiescence. Session deletion still does not touch datasets or
processing artifacts.

### Acknowledged remote tombstones stop heartbeating

A remote owner retained its pending-stop cancellation lease after writing its ACK.
The heartbeat therefore kept publishing an obsolete generation owner until TTL and
could accumulate unnecessary work across repeated stops.

Owner leases now expose a post-ACK callback. Only after the applied ACK is durably
written does the runtime remove that exact generation/lease object when it has no
foreground or tool references, then refresh the registry immediately. An active
successor lease is identity-distinct and remains registered. The deterministic shared-
bus test proves the stopped owner field disappears, does not return on heartbeat, and
the successor remains uncancelled.

### Exact turn identity, durable replay, and multi-client execution fencing

Every turn now carries a validated client-generated `local-*` message ID. The durable
admission row binds that ID to the session, content, deterministic turn ID, runtime,
pending/admitted/terminal state, and lease. Same-ID replay never starts a second model
run; different content conflicts; a different ID cannot enter while the session has an
active turn. Terminal status and the public run-terminal event commit atomically.

The browser keeps an exact retry record in tab-scoped `sessionStorage`, validates its
shape/content and 24-hour lifetime, and removes it only when the expected message ID
still owns the entry. Reload, hide/reopen, and session switching restore the same
optimistic row and draft ownership. Authoritative snapshots/terminals retire the exact
entry without treating identical text as identity. Malformed 2xx responses remain
ambiguous and retain the retry ID. Session creation similarly uses a persisted
`local-create-*` key and a deterministic backend session identity, so a lost creation
response replays the existing session instead of creating an orphan.

### Real worker quiescence remains part of the active-turn fence

Foreground ChatService completion is no longer sufficient to terminate a user turn.
The terminal callback retains the exact cancellation lease and waits for AgentScope
owners, public ToolOffload references, and registered real worker threads to become
quiescent. Execution-heartbeat failure first cancels the cooperative
`CancellationContext`, then cancels the asyncio task. A durable running public tool is
also included in the store's admission busy check, preventing a second client from
bypassing the frontend while background side effects remain active.

Lease expiry is intentionally fail-closed: a missed heartbeat does not prove that a
paused process or detached worker stopped, so GET/stream/retry no longer writes a false
failure terminal or releases the fence. An explicit Stop freezes the generation owner
set, obtains the distributed quiescence barrier where owners exist, and writes a
`stopped` user-turn terminal. The dead-owner path is also deterministic: after the
owner registry has expired, explicit Stop closes the admitted row and permits a
successor; passive TTL recovery never does.

The frontend no longer lets a late terminal for an older user turn clear a newer
wakeup reply. Successful run terminals do not mutate active reply ownership, and
failure/stopped recovery only converges an ownerless reply state.

### Final Round 6 verification

- Affected backend suites: `272 passed, 1 warning`.
- Full backend: `994 passed, 1 warning` (the existing Starlette deprecation warning).
- Full frontend: `214 passed` across 8 files.
- Frontend production build and TypeScript compilation: passed (Vite 1627 modules).
- Python `compileall`: passed.
- `git diff --check`: passed.
