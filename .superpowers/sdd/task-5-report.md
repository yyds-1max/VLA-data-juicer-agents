# Task 5 Report: Narrow Navigation Target Lock and Plan-Ledger Recovery

## Status

Implementation complete. Independent review approved the change with no Critical,
Important, or Minor findings.

## RED evidence

The first collection run failed because the required claim outcome interface did
not exist:

```text
ImportError: cannot import name 'StepClaimOutcome'
2 errors during collection
```

After adding only the enum surface, the behavioral RED run exposed the missing
implementation:

```text
15 failed, 16 passed, 63 deselected
```

The failures showed that `claim_step` still returned a boolean, both concurrent
overlapping writers could claim, target overlap/dry-run rules were unenforced,
input drift still invoked processing, busy had no structured execution response,
and finish-plan completion did not complete the attempt.

The human-decision precondition test then failed independently:

```text
test_human_decision_permission_fails_closed_when_sensor_input_disappears
AssertionError: assert 'allow' == 'deny'
```

## Implemented behavior

- Added `StepClaimOutcome` with `claimed`, `not_claimable`, and
  `navigation_data_busy` outcomes.
- Made `claim_step` start with `BEGIN IMMEDIATE`, authorize the exact durable
  session, verify the canonical active Plan step, inspect overlapping running
  writers, and transition `pending -> running` in the same transaction.
- Treated the running ledger row as the only lock source. There is no lock table,
  lease, expiry, ownership transfer, or automatic stale-work reclamation.
- Applied the lock only when both sides are real executions, the catalog declares
  `locks_navigation_target=True`, dates match, and either side selects all segments
  or explicit segment sets intersect.
- Kept dry-run steps non-locking and non-conflicting in both directions.
- Preserved terminal and controlled-recovery release through the existing atomic
  ledger state transitions; stale running work remains fail-closed until recovery.
- Added compact plan-bound busy output with the exact `ok:false`,
  `navigation_data_busy`, and `wait_and_reinspect` contract and no foreign session
  or attempt identity.
- Added `verify_plan_step_preconditions`, which resolves canonical Plan arguments
  and checks only concrete filesystem inputs for the accepted action. Changed
  inputs write bounded execution evidence, invalidate the Plan and unfinished
  ledger to `needs_replan`, and do not append investigation observations or infer
  artifact state/phase.
- Applied the same fail-closed input check to plan-bound human-decision permission
  without changing the durable delivery/quarantine recovery protocol.
- Kept extract-sync completion on an active attempt. Finish-processing completes
  the attempt only after every ledger step is complete and the terminal completed
  step is `validate_navigation_outputs`.
- Strengthened the catalog invariant so every executable write/execute/external
  mutation must be target-locking while reads, validation, and human-decision
  requests remain non-locking.

## Coverage

Tests cover:

- all-segment and explicit-segment overlap;
- intersecting/disjoint segment sets and disjoint dates;
- planning, waiting, completed, and failed non-running attempts;
- a real running non-locking validation step;
- dry-run acquire/conflict exclusion in both directions;
- two-thread simultaneous claim arbitration;
- completed, failed, and controlled-recovery lock release;
- stale running fail-closed behavior;
- exact compact busy response and no underlying invocation;
- execution and human-decision input drift evidence;
- absence of execution-path artifact reconciliation/snapshot calls;
- extract and finish lifecycle semantics;
- existing canonical argument, exactly-once, cancellation, staged outbox, and
  human-decision recovery regressions.

## Verification

Focused Task 5 suite:

```text
103 passed in 1.43s
```

Complete Python suite:

```text
766 passed, 1 warning in 9.10s
```

The warning is the pre-existing Starlette `httpx` deprecation warning.

Additional verification:

```text
python -m compileall -q src tests   # exit 0
git diff --check                    # exit 0, no output
```

The execution-path audit found no `reconcile_navigation_task`,
`build_navigation_artifact_snapshot`, `artifact_snapshot`, `live.phase`, or
`task.phase` reference in `plan_execution.py`.

## Independent review

The reviewer reported Approved with no Critical, Important, or Minor findings and
independently verified:

- `103 passed` in the Task 5 focused suite;
- `26 passed` in human-decision API/navigation-agent recovery regressions;
- `git diff --check` exit 0.

## Concerns

- No Task 5 blocker is known.
- The catalog and task-store preflight declarations were already correct from
  earlier tasks, so production changes were confined to the authoritative Plan
  repository/execution boundary. The task-store preflight remains intentionally
  read-only and non-authoritative.
