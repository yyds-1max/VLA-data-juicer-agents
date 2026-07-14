# Task 5 report: resumable sessions and artifact-safe deletion

## Status

Implemented Task 5 on base `a132ce0` with test-first RED/GREEN evidence. Every persisted AgentScope-backed public session remains writable after manager/runtime reconstruction, and `DELETE /api/sessions/{session_id}` deletes session control state without deleting navigation data products.

## RED / GREEN

- RED command: `.venv/bin/pytest tests/test_web_api.py tests/test_navigation_task_store.py -v`
- RED result: `70 passed, 4 failed`. Expected failures only: DELETE returned 405 and `NavigationServices.delete_control_state_for_web_session` did not exist.
- First GREEN for the same command: `74 passed`.
- Retry RED/GREEN: a multi-mapping retry test first failed because an already deleted AgentScope mapping returned `False`; after treating absence as the idempotent deletion postcondition, it passed. An evidence-removal failure test first proved the DB was deleted too early; after reordering filesystem control cleanup before the DB transaction, it passed with the DB row intact.
- Brief verification group: `.venv/bin/pytest tests/test_web_api.py tests/test_web_session_store.py tests/test_navigation_task_store.py tests/test_web_agentscope_session.py -v` -> `189 passed`.
- Full backend verification before finalization: `.venv/bin/pytest -q` -> `841 passed, 1 pre-existing Starlette deprecation warning`.

## Deletion boundary

- `AgentScopeRuntime.delete_web_session` enumerates every persisted mapping, including inactive mappings, and calls AgentScope 2.0.4 `SessionService.delete_session(user_id, agent_id, agentscope_session_id)` for each mapping.
- An AgentScope exception stops before Navigation or public Web control rows are deleted. AgentScope 2.0.4 returns `False` when the session record is already absent; this is accepted as an idempotent success because the deletion postcondition is already satisfied.
- Navigation deletion selects tasks only by `created_by_web_session_id`, validates every `task_id`, and deletes rows in FK-safe order: outbox, human-decision handoffs, evidence metadata, plan-submission attempts, task steps, plans, observation revisions, then tasks.
- Filesystem deletion is limited to `<workspace_root>/navigation-evidence/<task_id>`. Unsafe task IDs and a symlinked evidence root are rejected; a symlink at the task directory is unlinked without following its target.
- The deletion path neither accepts nor references `raw_data_root`, `clip_data_root`, `finish_data_root`, `vladatasets_root`, or processing roots.
- Public messages/events/tool runs/AgentScope mappings/human-decision consumptions and the public session row are deleted only after AgentScope and Navigation deletion return successfully.
- Repeated DELETE returns 404.
- Tests preserve and compare exact bytes for raw, nested `sync_data`, clip, and finish artifacts.

## Files changed

- `src/vla_data_juicer_agents/web/agent_session.py`
- `src/vla_data_juicer_agents/web/app.py`
- `src/vla_data_juicer_agents/web/session_manager.py`
- `src/vla_data_juicer_agents/web/session_store.py`
- `src/vla_data_juicer_agents/navigation/task_store.py`
- `src/vla_data_juicer_agents/navigation/services.py`
- `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- `tests/test_web_api.py`
- `tests/test_web_session_store.py`
- `tests/test_web_session_manager.py`
- `tests/test_navigation_task_store.py`
- `tests/test_web_agentscope_session.py`

## Schema evidence and self-review

The brief's seven child tables are complete for the current Navigation schema. `navigation_evidence` references observation revisions without cascade; `navigation_task_steps` references tasks without cascade; plans and plan-submission attempts cascade from tasks; outbox and handoffs cascade from plans/tasks. Explicit deletion nevertheless covers every child table in dependency order so it does not rely on partial cascade behavior.

Self-review confirmed:

- no AgentScope source changes;
- no Task 6 stop/interrupt semantics and no Task 9 UI work;
- no changes to the tracked legacy `.superpowers/sdd/task-5-report.md`;
- `git diff --check` clean before final verification;
- deletion source scan has no data-root references;
- artifact and foreign-session Navigation state remain untouched in tests.

## Concerns

- AgentScope deletion is an external multi-session cascade. If deletion of a later mapping raises, AgentScope may already have deleted earlier mappings; public and Navigation control rows remain intact. A retry calls every mapping again, accepts `False` for the already absent earlier mapping, and continues through the remaining mappings.
- Navigation control deletion has three explicit boundaries: evidence-path validation/removal runs first, so a filesystem failure leaves the Navigation DB and public row intact; the FK-ordered DB deletion is one transaction, so a DB failure rolls back all DB rows and leaves the public row intact (dedicated evidence may already be gone); public Web rows are deleted last. If the final public DB deletion fails, retry is safe because already absent AgentScope mappings return `False` and zero remaining Navigation tasks is a successful no-op.
- The test suite emits the existing Starlette `TestClient`/httpx deprecation warning; no Task 5 failure is associated with it.

## Review follow-up fix

The Task 5 review identified one response-confidentiality issue and one validation-order issue. Both were addressed in a separate TDD cycle:

- RED: nine selected cases failed. AgentScope `RuntimeError`/Navigation `ValueError` text reached HTTP `detail`; `OSError` and `sqlite3.Error` escaped the endpoint; `bad id` evidence was removed before the task store rejected the invalid ID; the three injected phase failures could not produce the required stable response/retry behavior.
- GREEN: the focused failure-injection set passed, followed by the four-file brief group with `199 passed` and the full backend suite with `851 passed` (the same pre-existing Starlette deprecation warning only).
- The DELETE endpoint now preflights public-session existence, catches ordinary `Exception`, logs the full server-side traceback with `logger.exception`, and returns only HTTP 409 with stable code `session_delete_failed` and fixed DataPilot wording. `asyncio.CancelledError` and `KeyboardInterrupt` remain uncaught and were tested directly.
- The confidentiality regression makes AgentScope raise `delete failed for internal-agent / internal-as-session` and verifies neither identity reaches the response. The same stable boundary is parametrically verified for `OSError`, `sqlite3.OperationalError`, `RuntimeError`, and `ValueError`.
- `validate_navigation_task_id` is now the single task-store validator used by both the SQLite deletion transaction and `NavigationServices`. The service validates the complete task-ID batch before any filesystem or DB mutation. A non-traversal invalid ID (`bad id`) leaves both its evidence bytes and DB row intact.
- A symlinked `navigation-evidence` root is rejected without following it; raw bytes, Navigation rows, and the public row remain intact.
- The Navigation DB failure test now injects `sqlite3.OperationalError` at the second child-table DELETE inside the real transaction. Evidence has already been removed, the first child DELETE rolls back, public state remains, and retry completes with missing evidence treated idempotently.
- The final public-store failure test runs AgentScope and Navigation deletion successfully, injects a public SQLite failure containing internal identities, verifies the stable non-sensitive response, then retries through AgentScope `False` and Navigation no-op to 204.

The three-stage retry boundary remains intentional: evidence failure occurs before the DB transaction and preserves DB/public state; an in-transaction SQLite failure rolls all Navigation rows back while public state remains; a final public-store failure leaves the public row for retry after AgentScope/Navigation have already reached their deletion postconditions.

## Second review follow-up

- RED: injecting `sqlite3.OperationalError("preflight failed for internal-agent / internal-as-session")` from the DELETE route's public-session `get_session` preflight escaped as an unlogged server error.
- GREEN: the preflight now runs inside the same ordinary-`Exception` boundary as deletion. It logs via `logger.exception` and returns the stable 409 `session_delete_failed` DataPilot response without either internal identity. A genuine `None` preflight still returns 404, while `asyncio.CancelledError` and `KeyboardInterrupt` remain uncaught.
- Added an end-to-end DELETE regression with `task_id="bad id"`; the stable 409 response leaves exact evidence bytes, the Navigation task row, and the public session row intact.
- Focused regressions passed, followed by the four-file brief group with `201 passed`; `compileall`, diff-check, tracked-report guard, and data-root scan also passed.
