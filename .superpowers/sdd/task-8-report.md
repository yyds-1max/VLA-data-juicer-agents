# Task 8 Report

## Scope

Added deterministic local end-to-end regression coverage for session-bound,
model-directed navigation attempts. The tests run the real AgentScope `Agent`
reply loop, production router handoff and navigation tool resolver, and durable
SQLite stores with an offline scripted `ChatModelBase`. No production source,
server checkout, server process, service configuration, network model, or real
dataset was changed or exercised.

## Behavior Proved

- A real router Agent tool call hands a raw-only attempt to a real navigation
  Agent loop. The navigation Agent starts without observations, invokes
  artifact/raw/topic/sensor inspections, submits one complete extract-sync Plan,
  and executes the stored canonical arguments. Only the leaf data-processing
  functions are replaced with local recorders where exact arguments are asserted.
- In the same session, completed extract-sync work returns to investigation and
  planning tools. The actual Agent final verifies products and asks whether to
  continue without calling a finish submission tool. After the user's reply, the
  next real Agent cycle records `scene_mode="out"`; a production resolver refresh
  then exposes the new planning-context fence for reinspection and submission.
- A new session receives a distinct task id, no inherited observations, and no
  inherited Plan. Even when its request says sync is complete, artifact inspection
  is the first fact-producing call. Verified sync permits finish planning; missing
  sync after product deletion leads to extract-sync investigation and a new Plan.
- A historical completed attempt does not suppress work after products are
  deleted. Raw-only facts in a fresh attempt authorize a new extract-sync Plan.
- A completed history row produces a truthful `ok:true/started:true` handoff for
  another Web session and never reports a cross-session ownership error. A real
  non-dry-run ledger step claimed as `running` for an overlapping locking action
  produces the bounded `ok:false/started:false/navigation_data_busy` handoff and
  creates no blocked attempt. Its pre-existing router session mapping is unchanged.

## TDD Evidence

- Tests were added before the server acceptance documentation change.
- The first flow run exposed two invalid test assertions against the observation
  wrapper instead of its `snapshot`; these were corrected and were not counted as
  product RED evidence.
- Valid RED: `pytest tests/test_navigation_context_budget.py -q` produced
  `1 failed, 5 passed` because the runbook did not state the `dry_run=False`
  default, explicitly selected `dry_run=True` boundary, or attended GUI/human
  boundary.
- GREEN after the minimal runbook update: `6 passed`.
- The new flow and real-handoff regressions passed against the completed Tasks 1–7
  implementation, so Task 8 required no production-code correction.
- Review remediation first added source guards that failed because the flow still
  created task attempts directly and the budget test still used a hand-built
  transcript and `chars/4`. After replacement, the four real-Agent flow scenarios
  initially produced `4 passed, 1 failed`. The failure exposed the expected
  `RuntimeError("navigation planning context changed")` when a submission closure
  captured before guidance was reused after durable guidance advanced. Splitting
  that evidence at the real resolver cycle boundary produced `5 passed` without
  bypassing activation or changing production behavior.
- The first real-invocation budget run reached every asserted limit and failed only
  because AgentScope represents an unused summary as `""`, not `None`; the corrected
  no-summary assertion then passed.

## Context Instrumentation

The representative investigation, evidence read, invalid Plan, whole-Plan
correction, and dry-run execution run records 15 real AgentScope model invocations.
The offline model returns real `ToolCallBlock`/`TextBlock` values and records the
`DashScopeChatFormatter` output, exposed tool schemas, actual tool results and
`ChatUsage`. Input tokens use the same `ChatModelBase.count_tokens(messages, tools)`
path used by the production model class:

- `max_tool_result_chars = 3_856` (limit `5_500`)
- `validation_failure_chars = 292` (limit `3_000`)
- `peak_model_input_tokens = 9_181` (limit `83_885`)
- `compact_event_count = 0`
- planning schema characters: `15_023` per call
- execution schema characters: `1_031` per call

Compression instrumentation records any real structured-output compression call
as a compact event. The event list is empty and the AgentScope state summary is
also empty.

## Server Acceptance Runbook

Expanded `docs/navigation-plan-server-acceptance.md` to require separate approval
before any server action, production default `dry_run=False`, `dry_run=True` only
for an explicitly selected test, exact commit/config/root checks, bounded evidence
collection, and a hard stop before GUI/human steps unless the user is present and
has explicitly asked to continue. The runbook preserves the rule that real-data
acceptance is a later, separately approved operation.

## Verification

- Review-remediation focused aggregate: `12 passed`.
- Flow plus its dependent direct-CLI integration: `15 passed`.
- Full Python suite: `765 passed`, with one pre-existing Starlette/httpx
  deprecation warning.
- Frontend: `7` files and `136` tests passed.
- Frontend TypeScript/Vite build: passed.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
- All three final dead-code searches returned no matches (expected `rg` exit 1).

## Files Changed

- `tests/test_navigation_model_authored_flow.py`
- `tests/test_navigation_context_budget.py`
- `tests/navigation_agentscope_harness.py`
- `tests/test_web_agentscope_session.py`
- `docs/navigation-plan-server-acceptance.md`
- `.superpowers/sdd/task-8-report.md`

## Final Current-Attempt Fence Remediation

Final whole-branch review found that a planning toolkit captured for Attempt A
could still write after the same Web/AgentScope pair created a newer,
different-target Attempt B. The shared authorization helper verified A's stored
session identity but did not reselect the pair's current Attempt.

TDD regressions now cover captured inspection, guidance, both submission tools,
submission audit/activation, human-decision TOCTOU, controlled handoff recovery,
evidence cleanup, and task-start compensation. Every attempt-bound write now
runs the shared exact-pair/current-task fence under `BEGIN IMMEDIATE`, ordered by
`created_at DESC, rowid DESC`. Stale public tool paths return bounded session
mismatch/False results and leave task, observation, evidence, audit, Plan,
ledger, outbox, and handoff state unchanged. Exact retry of the same current
Attempt and wrong/omitted-identity behavior remain compatible.

Remediation verification: focused navigation modules `219 passed`; full Python
suite `770 passed` with the same pre-existing warning; compileall and diff check
passed; all three final dead-reference searches returned no matches. No frontend
files changed, so the frontend suite/build was not rerun for this remediation.

## Durable Claim Terminalization Follow-up

Final authorization review found one necessary exception to the newest-Attempt
fence: an action may create Attempt B after Attempt A has already durably claimed
its step. Applying the generic fence to A's result staging and recovery stranded
the step in `running`, leaked a secondary session-mismatch exception, and retained
the target writer lock.

TDD regressions execute the real wrapper while the leaf action creates B and cover
success, processing failure, cancellation, oversized output, and result-stage
failure. Post-claim operations now require the exact non-null durable owner pair
plus exact plan/step/action and a durable running/staged/terminalized state. New
claims and all other new mutations remain newest-Attempt fenced. The same narrow
rule applies to an already-staged human handoff's delivery or recovery; initiating
a new handoff remains newest-Attempt fenced. A terminalizes without rerunning the
action, releases its target lock, and never mutates B.

Follow-up verification: focused execution/store/Web handoff modules `215 passed`;
full Python suite `777 passed` with the same pre-existing warning; compileall and
diff check passed; all three dead-reference searches returned no matches.

## Staged Claim Retry Follow-up

A further final review found that an already-staged result could still be stranded
if its first evidence write, outbox attach, or ledger finalize failed after Attempt
B became current. The second wrapper call entered the ordinary newest-Attempt gate
before observing A's durable outbox, so it returned a session mismatch and retained
A's running step and target lock.

The repository now exposes a read-only terminalization snapshot only for an exact
non-null owner pair, plan, step, and action with an active Plan, durable `running`
step, matching result outbox whose expected status includes `running`, and no
unfinished prior step. The wrapper may use that snapshot only to retry evidence
and ledger finalization; it cannot claim pending work or invoke the action again.
TDD covers success and failure results across first-failure evidence/attach/finalize
boundaries, cancellation finalization retry, wrong owner/action, and pending-state
denial. Attempt B remains byte-for-byte unchanged during retry.

Staged-retry verification: focused high-risk modules `285 passed`; full Python
suite `786 passed` with the same pre-existing warning; compileall and diff check
passed; all three dead-reference searches returned no matches.
