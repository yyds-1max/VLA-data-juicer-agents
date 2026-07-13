# Task 5 Report: Narrow Navigation Target Lock and Plan-Ledger Recovery

## Status

Implementation complete. The original implementation passed independent review,
then external audits found four Important follow-up gaps. All four now have
RED/GREEN regression coverage and are fixed at the repository/execution boundary.

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

The external-audit follow-up RED runs then reproduced all four gaps:

```text
current-attempt barrier race: old A wrapper returned ok=True after B became current
gridmap input drift: 4 failed (all variants still returned ok=True)
waiting_user retry drift: retry returned None/allow after sensor source deletion
captured recovery retry: existing recovery_required handoff still returned allow
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
- Closed the post-gate current-attempt TOCTOU by reselecting the newest exact
  Web/AgentScope task inside the same `BEGIN IMMEDIATE` claim transaction and
  comparing it with the candidate task before conflict scan or ledger mutation.
- Added variant-specific gridmap preconditions from the accepted observation
  revision and canonical Plan arguments: exact JSON-bearing gridmap directories,
  exact PCD sources plus the PCD runtime asset, or exact projection-ready JSON
  directories. These checks do not select a variant or infer live phase.
- Re-check concrete human-decision inputs on every `waiting_user` permission retry.
  Drift writes bounded execution evidence and reuses the existing audited
  `recovery_required -> quarantine -> needs_replan` protocol. A recovery-only
  request anchor is created only when no decision handoff exists; no-drift retries
  remain allowed.
- Deny captured human-decision tools immediately after the authoritative gate when
  the snapshot already has a `recovery_required` handoff. This bounded read-only
  denial runs before precondition checks, evidence writes, or `waiting_user` allow.
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
  human-decision recovery regressions;
- a deterministic gate/claim barrier where the same exact session creates a new
  current attempt between precondition verification and claim;
- all canonical gridmap variants, including missing PCD runtime;
- waiting-user permission retry with and without input drift, including controlled
  quarantine and replan;
- a captured human-decision tool retry against an existing recovery handoff,
  including byte-identical database/evidence and logically identical durable state.

## Verification

Focused Task 5 suite after the external-audit fixes:

```text
110 passed in 1.53s
```

Complete Python suite:

```text
774 passed, 1 warning in 9.26s
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

The initial reviewer reported Approved with no Critical, Important, or Minor
findings and independently verified:

- `103 passed` in the Task 5 focused suite;
- `26 passed` in human-decision API/navigation-agent recovery regressions;
- `git diff --check` exit 0.

The external audit subsequently reported 0 Critical and 3 Important findings:
current-attempt claim TOCTOU, incomplete gridmap preconditions, and fail-open
`waiting_user` permission retries. Each finding was independently reproduced
before implementation and is covered by the follow-up tests above. A final
read-only review of those fixes is recorded with the handoff.

A subsequent review found one remaining Important authorization gap: a captured
request tool could allow a no-drift retry even when its authoritative snapshot
already contained a `recovery_required` handoff. It also noted the stale focused
and full-suite counts as one Minor report issue. The authorization gap was
reproduced before the fix, and this report now records the fresh actual counts.

## Concerns

- No Task 5 blocker is known after the external-audit fixes.
- The catalog and task-store preflight declarations were already correct from
  earlier tasks, so production changes were confined to the authoritative Plan
  repository/execution boundary. The task-store preflight remains intentionally
  read-only and non-authoritative.
