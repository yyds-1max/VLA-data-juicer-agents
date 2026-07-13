# Task 7 Report: Remove the Navigation Dataset State Machine

## Status

Complete. Navigation task persistence now represents one explicit attempt and its
session identity; it no longer stores or derives a dataset lifecycle state machine.
Legacy and Task 1 transitional schema generations fail closed with
`NavigationStateResetRequired`. No migration or backfill path was added.

## Delivered

- Bumped the navigation task schema to version 3 and the clean generation marker
  to `navigation-attempts-final-v2`.
- Reduced `navigation_tasks` to attempt identity, request/target inputs, factual
  guidance/state revisions, coarse status, accepted plan stage, session ownership,
  and timestamps.
- Removed task phase, waiting-state, run-resume, artifact-snapshot, drift, global
  date lookup/upsert, unowned mutation, restore, and reconciliation APIs.
- Deleted `navigation/task_reconciliation.py` and its state-machine test suite.
- Preserved exact-attempt session authorization, CAS runtime-start compensation,
  target-writer locks, plan/step ledger recovery, result outbox, human handoff,
  and `needs_replan` behavior.
- Changed direct CLI/tool workflows to create a new explicit attempt with
  `direct:<run_id>` Web identity and the actual AgentScope session identity.
- Made direct planning activity-driven: the model investigates current facts and
  submits the appropriate plan stage without a persisted task-phase gate.
- Added dead-reference tests and fail-closed tests for both older and Task 1
  transitional schema generations, including byte-for-byte non-mutation checks.

## TDD Evidence

Initial RED command:

```text
.venv/bin/pytest tests/test_navigation_dead_references.py \
  tests/test_navigation_task_store.py::test_task_store_exposes_only_attempt_scoped_mutators \
  tests/test_navigation_task_store.py::test_task_store_idempotently_creates_supported_schema_generation \
  tests/test_navigation_task_store.py::test_task_store_creates_and_loads_navigation_task -q
```

Initial result: 3 failed, 2 passed. Failures proved that legacy source references
and APIs still existed and the store still advertised the transitional generation.
After the production cleanup, the same group passed 5/5.

The first full-suite run found three mechanical legacy-test references, not runtime
regressions. Their business coverage was retained: context-budget anchoring now
uses exact session-pair lookup, and guidance tests continue to prove atomic success
and failure without partial persistence.

## Verification

- High-risk plan store/execution/tool group: `160 passed`.
- Requested focused suite: `164 passed`, one pre-existing Starlette warning.
- Schema/dead-reference group: `26 passed`.
- Full suite: `746 passed`, one pre-existing Starlette warning.
- `.venv/bin/python -m compileall -q src tests`: passed.
- Forbidden production symbol audit: no matches.
- `build_navigation_artifact_snapshot` audit in `plan_execution.py`: no matches.
- Removed phase/status/restore/latest-session symbol audit across `src` and
  `tests`: no matches.
- `git diff --check`: passed after removing one trailing blank line.

## Test Coverage Decisions

Only tests whose subject was the deleted state machine itself were removed:
reconciliation, global task upsert/rebind, automatic dataset-phase advancement,
and automatic completed-entry short-circuit behavior. Tests for authorization,
locking, durable observations, plan ledger transitions, staged-result recovery,
outbox/handoff crash consistency, cancellation, controlled human recovery, direct
workflow identity, and `needs_replan` remain.

## Known Warning

The suite still emits the existing FastAPI/Starlette `httpx` deprecation warning.
No new warnings were introduced.
