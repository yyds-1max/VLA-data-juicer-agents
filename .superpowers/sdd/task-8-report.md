# Task 8 Report

## Scope

Added local end-to-end regression coverage for session-bound, model-directed
navigation attempts. No production source, server checkout, server process,
service configuration, or real dataset was changed or exercised.

## Behavior Proved

- A raw-only attempt starts without observations, records only model-invoked
  artifact/raw/topic/sensor inspections, submits one complete extract-sync Plan,
  and executes the stored canonical arguments. Extract completion leaves the
  attempt active and does not recreate a phase state machine.
- In the same session, completed extract-sync work returns to investigation and
  planning tools. The simulated agent turn verifies products, reports completion,
  and asks whether to continue without submitting or executing finish work. Only
  after user guidance records `scene_mode="out"` does the attempt re-inspect
  current finish facts and submit a finish-processing Plan.
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
  creates no blocked attempt.

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

## Context Instrumentation

The representative entry, investigation, invalid Plan, whole-Plan correction,
and dry-run execution transcript records every tool-result size, every activity's
exposed schema size, and every simulated model input:

- `max_tool_result_chars = 5_494` (limit `5_500`)
- `validation_failure_chars = 276` (limit `3_000`)
- `peak_model_input_tokens = 11_264` (limit `83_885`)
- `compact_event_count = 0`
- exposed schema characters: entry `14_588`, inspection `14_588`, planning
  `14_588`, execution `915`

No schema snapshot, draft snapshot, or accumulated validation history enters a
tool result or retained transcript.

## Server Acceptance Runbook

Expanded `docs/navigation-plan-server-acceptance.md` to require separate approval
before any server action, production default `dry_run=False`, `dry_run=True` only
for an explicitly selected test, exact commit/config/root checks, bounded evidence
collection, and a hard stop before GUI/human steps unless the user is present and
has explicitly asked to continue. The runbook preserves the rule that real-data
acceptance is a later, separately approved operation.

## Verification

- Focused Task 8 files: `125 passed`.
- Full Python suite: `767 passed`, with one pre-existing Starlette/httpx
  deprecation warning.
- Frontend: `7` files and `136` tests passed.
- Frontend TypeScript/Vite build: passed.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
- All three Task 8 dead-code searches returned no matches (expected `rg` exit 1).

## Files Changed

- `tests/test_navigation_model_authored_flow.py`
- `tests/test_navigation_context_budget.py`
- `tests/test_web_agentscope_session.py`
- `docs/navigation-plan-server-acceptance.md`
- `.superpowers/sdd/task-8-report.md`
