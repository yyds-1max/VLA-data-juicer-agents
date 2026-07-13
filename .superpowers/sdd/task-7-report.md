# Task 7 Report: Final Navigation Attempt Contract

## Status

Complete. Navigation persistence now has one reset-only schema contract and one
explicit, non-null Web/AgentScope owner pair per attempt. Direct planning and
execution resolve tools only through that exact durable pair. Legacy, missing,
or drifted navigation child objects fail closed before mutation.

## Delivered

- Centralized all nine tables, nine indexes, and twenty-one aggregate-revision
  triggers in `navigation/schema.py`; every navigation repository uses that
  initializer.
- Fresh schema creation is one `BEGIN IMMEDIATE` transaction and writes the
  `navigation_state_schema` marker last.
- Existing databases are inspected read-only against exact columns, constraints,
  foreign keys, indexes, partial-index SQL, triggers, and generation marker.
- Deleted plan-repository migrations, nullable outbox repair, table rebuild/copy,
  `ALTER TABLE`, and lazy backfill paths.
- Made both task session columns SQL `NOT NULL`, made both Pydantic fields required
  non-empty strings, rejected `None`, empty, and whitespace-only creation input,
  and authorized writes only for an exact non-null pair.
- Removed task phase, waiting-state, run-resume, artifact-snapshot, drift, global
  date lookup/upsert, unowned mutation, restore, and reconciliation APIs.
- Preserved exact-attempt authorization, CAS compensation, target-writer locks,
  plan/step recovery, result outbox, human handoff, cancellation, dry-run behavior,
  and `needs_replan` behavior.
- Changed direct CLI/tool workflows to carry durable Web and AgentScope identities
  through planning and execution. The resolver no longer infers Web ownership from
  an AgentScope ID string shape.
- Covered the real resolver, real direct planning loop, real CLI `async_main`, and
  the VLA workflow runner; the pre-execution toolkit includes factual inspections
  and both plan-submission tools.
- Expanded dead-reference tests to scan both `src` and `tests`; legacy fixture
  identifiers are assembled dynamically so the repository has true zero matches.

## TDD Evidence

The direct-resolution RED group failed because a valid durable pair whose IDs did
not share the legacy string shape received no tools, and the real planning loop had
no factual/submission tools. After removing the heuristic and propagating the Web
identity, the group passed.

The schema RED group failed because fresh initialization omitted plan, outbox,
handoff, and submission-attempt tables and because all three repository entry
points accepted a drifted child contract. The identity RED group accepted missing
or blank model fields and empty creation inputs. Those groups now pass, including
read-only byte/logical non-mutation checks for missing and drifted tables, indexes,
triggers, and the legacy nullable outbox.

## Verification

- Direct resolver/runner integration group: passed.
- Six-module high-risk group: `193 passed`.
- Plan execution: `43 passed`.
- Full suite: `761 passed`, one pre-existing Starlette warning.
- `.venv/bin/python -m compileall -q src tests`: passed.
- Forbidden source-and-test symbol audit: no matches.
- Removed state-machine/session-compatibility literal audit across `src` and
  `tests`: no matches.
- `git diff --check`: passed.

## Test Coverage Decisions

Authorization, lock ownership, durable observations, plan-ledger transitions,
staged-result recovery, outbox/handoff crash consistency, cancellation, controlled
human recovery, dry-run completion, and `needs_replan` coverage remain. Tests now
construct every navigation task with an explicit owner pair; fail-closed tests use
an explicit wrong pair rather than the removed unowned compatibility path.

## Known Warning

The suite still emits the existing FastAPI/Starlette `httpx` deprecation warning.
No new warnings were introduced.
