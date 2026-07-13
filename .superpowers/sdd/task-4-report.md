# Task 4 Report: Let the Model Select the Navigation Activity

## Outcome

Implemented the Task 4 model-authored Plan flow. A bound navigation attempt now
starts with observation, evidence, context, user-guidance, and both complete Plan
submission tools. The model chooses exactly one strict whole-Plan contract:
`extract_sync` or `finish_processing`. Successful submission validates, persists,
activates, records the accepted phase, and creates its execution ledger atomically.

No draft, patch, finalize, or code-selected lifecycle is exposed to the model. An
accepted Plan switches the attempt to Plan-bound execution tools; a missing or
completed active Plan returns it to planning tools. No terminal/runtime suppression
was added.

## Implementation

- Replaced lifecycle-driven tool resolution with activity-driven resolution.
  Unbound attempts fail closed; exact Web/AgentScope ownership is required.
- Exposed all observation/cognitive tools, bounded guidance, and both submission
  tools while planning. Both submission schemas are strict and reject extra keys.
- Made Plan validation derive required evidence and phase from the submitted Plan
  type rather than from task phase.
- Persisted `accepted_plan_phase`, Plan record, execution ledger, and task revision
  in the same SQLite transaction. Failed validation or activation leaves no
  accepted phase and does not mutate the active Plan.
- Made activation task-wide: accepting a Plan supersedes any other active Plan,
  including a different phase. Running/waiting work, staged results, or an
  unacknowledged human handoff from any Plan still blocks replacement.
- Made execution authorization resolve the durable active Plan selected by the
  task's accepted phase, so stale in-memory task objects cannot change authority.
- Reworked user guidance into one bounded model-facing tool. It records guidance
  as an observation/evidence fact and does not choose a phase or inspect artifacts.
  The guidance revision, optional scene mode, observation, and evidence metadata
  are committed in one session-fenced SQLite transaction.
- Made processing-action description stage-neutral so the model can investigate
  either activity before choosing a Plan. It exposes only factual variants,
  parameter contracts, preconditions, and constraints—not recommendation notes.
- Defined execution activity from the ledger, not only the Plan row: only a
  current `pending`, `running`, or `waiting_user` step exposes execution tools;
  failed, `needs_replan`, or exhausted ledgers return to planning.

## TDD Evidence

RED was established with eight focused failures covering the old lifecycle
resolver, unbound tool exposure, intake-only Plan rejection, missing guidance
arguments, single submission schema exposure, task-phase validation, and missing
accepted phase persistence.

GREEN added or updated focused tests for:

- strict dual complete-Plan schemas and bounded correctable errors;
- phase-neutral validation and observation requirements;
- atomic accepted-phase/Plan/ledger activation and rollback;
- cross-phase active Plan replacement and in-flight-work protection;
- activity-driven planning/execution tool exposure and exact session fencing;
- atomic guidance/observation recording and append-failure rollback;
- stage-neutral action descriptions and durable execution authorization.

During full-suite verification, an existing model-flow test looped because it
assumed inspection tools disappear after one observation pass. Repeated resolver
sampling showed observation revisions increasing while the same eight inspection
tools correctly remained available. The helper was corrected to perform one
investigation pass. A later full-suite failure showed that the new task-wide
replacement query had accidentally limited in-flight checks to active Plans; the
original protection for completed Plans with unacknowledged final handoffs was
restored.

## Verification

Final commands and results:

```text
.venv/bin/python -m pytest tests/test_navigation_agent_tools.py tests/test_navigation_task_tools.py tests/test_session_tool_registry.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_validation.py tests/test_navigation_plan_store.py tests/test_navigation_plan_contracts.py tests/test_navigation_plan_execution.py tests/test_navigation_observation_tools.py tests/test_navigation_observation_store.py tests/test_navigation_context_budget.py tests/test_navigation_model_authored_flow.py tests/test_web_agentscope_session.py -q
372 passed in 9.92s

.venv/bin/python -m pytest -q
746 passed, 1 warning in 16.27s

.venv/bin/python -m compileall -q src tests
success

git diff --check
success
```

The remaining warning is the existing Starlette/httpx deprecation warning.

## Review Notes

The specification review found two Important issues and no Critical issues. Both
were fixed and re-reviewed with no remaining findings: recommendation-bearing
variant notes were removed from action descriptions, and terminal ledger states
now return the attempt to planning instead of exposing rejected execution calls.

The code-quality review found two Important concurrency issues and no Critical
issues. Both were fixed and re-reviewed with no remaining blocking findings:
guidance no longer relies on cross-transaction compensation, and resolver
authority now uses exact Web/AgentScope lookup plus the durable accepted-Plan join.
The reviewer noted only a non-blocking opportunity to extract shared private
observation assembly code in a later cleanup.

An external independent follow-up review found three additional Important
authorization/snapshot issues. They were fixed with explicit RED-to-GREEN
regressions:

- A toolkit captured from a superseded Plan now returns bounded
  `plan_not_active` before any artifact inspection or durable mutation. The old
  execution-time artifact reconciliation and live-phase derivation path was
  removed completely.
- `read_execution_snapshot` now reads the exact Web/AgentScope task, durable
  accepted active Plan, overview/current ledger state, dependency statuses,
  staged outbox result, and human handoff in one SQLite read transaction.
  Resolver and execution-tool construction consume that same snapshot.
- Overview/current-step tools reauthorize from a fresh exact snapshot at call
  time. Superseded Plans, session rebinds, and accepted-phase changes fail closed
  with bounded `inactive_navigation_plan` results.

The follow-up implementation review found no remaining Critical or Important
issues.

A subsequent review found one more Important current-attempt fencing gap: after
the same Web/AgentScope session created a different target attempt, a toolkit
captured from the previous attempt could still authorize by explicitly passing
its old `task_id`. The snapshot transaction now first selects the exact-session
current attempt using the same `created_at DESC, rowid DESC` ordering as
`find_by_session`, then treats a supplied `task_id` only as an assertion. A
different-target successor revokes the old overview, current-step, and processing
tools before claim or mutation, while an exact same-target retry remains bound to
the same task. The independent current-attempt follow-up review found no Critical
or Important blockers.

Task 7 remains responsible for final deletion of any dead compatibility code not
reachable through the Task 4 model-facing resolver.
