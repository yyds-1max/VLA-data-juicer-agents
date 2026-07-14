# AgentScope Web Session Migration — Task 11 Report

Status: DONE

Baseline: `26ba0b0`

## TDD / RED-GREEN evidence

The obsolete bridge names were already absent from production at the Task 11
baseline. After adding the backend/frontend dead-reference guard, the first run of
the complete file therefore recorded that part as baseline already clean. The new
README contract assertion failed as intended:

```text
.venv/bin/pytest tests/test_navigation_dead_references.py -v
1 failed, 3 passed
```

The failure listed all seven missing documentation fragments: AgentScope 2.0.4,
Redis, the selected-session stream route, all three public terminal states, and the
raw/sync/clip/finish preservation boundary. After documenting the contract, all four
tests passed.

A later source audit found genuine cursor-only bridge state that had no callers:

- `AgentScopeSessionMapping.event_cursor`;
- the `agentscope_sessions.event_cursor` column;
- cursor preservation in mapping UPSERTs and cursor selection in all mapping reads;
- the unused `save_agentscope_event_cursor` method.

The dead-reference guard was extended before production cleanup. Its focused RED
reported the two expected source matches (`event_cursor` and the save method). After
removing the state, the relevant mapping/runtime regression was GREEN:

```text
.venv/bin/pytest tests/test_navigation_dead_references.py \
  tests/test_web_session_store.py tests/test_web_agentscope_session.py -q
134 passed in 1.24s
```

The Web schema generation remains the planned `agentscope-native-events-v1`. A fresh
database contains no cursor column. An old pre-migration generation is reset through
the existing development-only allowlist reset. If a developer ran an intermediate
build of this branch that already wrote the v1 generation, SQLite may retain a
physically present but completely unreferenced extra column; current reads and writes
do not depend on it. We deliberately did not introduce a second schema-generation
reset or a compatibility migration for development test sessions.

## Changes

- `tests/test_navigation_dead_references.py`
  - scans production Web code plus the AgentScope runtime for the obsolete forwarding,
    subscriber, WebSocket, and cursor-only symbols;
  - scans frontend production sources for `WebSocket` and `openSessionEvents`;
  - does not scan or ban the legacy TUI `AgentScopeEventAdapter`;
  - guards the durable-session runtime documentation contract.
- `src/vla_data_juicer_agents/web/session_store.py`
  - removes the unused AgentScope event cursor field, schema column, SQL handling,
    and save method;
  - leaves the public event sequence (`public_events.sequence`) unchanged; that is
    the authoritative SSE replay cursor.
- `README.md`
  - documents exact AgentScope 2.0.4 and Redis requirements and actual
    `VLA_AGENT_REDIS_URL` plus `scripts/run_web.sh start/restart` commands;
  - documents the development metadata reset and its artifact preservation boundary;
  - documents writable historical sessions and selected-session-only SSE;
  - documents only success/failure/stopped terminals and explicit-stop semantics;
  - removes the obsolete statement that Vite proxies WebSocket traffic and does not
    present `VLA_AGENT_ENABLE_AGENTSCOPE=0` as a production Web mode.

## Verification

Planned focused backend acceptance:

```text
.venv/bin/pytest tests/test_agentscope_204_contract.py \
  tests/test_datapilot_projection.py tests/test_web_sse.py tests/test_web_api.py \
  tests/test_web_session_store.py tests/test_web_agentscope_session.py \
  tests/test_web_agentscope_background_wakeup.py tests/test_cancellation.py \
  tests/test_navigation_plan_execution.py tests/test_navigation_plan_store.py \
  tests/test_navigation_agent_tools.py tests/test_navigation_tool_groups.py -v
395 passed, 1 warning in 5.85s
```

Fresh final acceptance after the cursor cleanup:

```text
.venv/bin/pytest -q
896 passed, 1 warning in 8.91s

npm --prefix frontend test -- src
7 files passed; 171 tests passed in 4.73s

npm --prefix frontend run build
1626 modules transformed; Vite build passed in 829ms

frontend/node_modules/.bin/tsc -p frontend/tsconfig.json --noEmit
exit 0

.venv/bin/python -m compileall -q src tests
exit 0

git diff --check
exit 0
```

The one Python warning is the existing FastAPI/Starlette TestClient warning that
`httpx` support is deprecated in favor of `httpx2`. It is dependency-level and was
not changed in Task 11. Existing npm audit advisories are likewise outside this
tests/docs/dead-code cleanup; no dependency version was changed and the advisory
count was not re-measured in this task.

## Deletion safety audit

The final production scan was:

```text
rg -n "rmtree|unlink|remove\(" \
  src/vla_data_juicer_agents/web \
  src/vla_data_juicer_agents/runtime \
  src/vla_data_juicer_agents/navigation
```

Every result was traced:

- `navigation/services.py` is the only session-delete filesystem path. It requires
  the configured root name to be exactly `navigation-evidence`, rejects a symlinked
  root, validates every task id, verifies each direct child, and deletes only
  `navigation-evidence/<task_id>`.
- `web/session_store.py` deletes SQLite control-plane rows only. AgentScope runtime
  deletion removes mapped AgentScope sessions and Navigation control rows before the
  public control rows.
- `navigation/evidence_store.py` deletes only an owned evidence ref or an atomic-write
  temporary file beneath its dedicated evidence root.
- `navigation/execution_tools.py` contains processing-action cleanup helpers and
  symlink replacement; these are not called by session deletion and were not newly
  added by this migration's delete-session path.
- `plan_validation.py` removes an item from an in-memory set.
- The branch diff's only newly introduced recursive delete is the guarded dedicated
  evidence task directory. The removed `plan_draft` unlink was an obsolete
  control-plane draft file.

`test_delete_session_removes_only_control_state_and_preserves_artifact_bytes` passed
in both focused and full acceptance. It verifies raw data, nested `sync_data`,
`clip_data`, and `finish_data` bytes after deletion. No session-delete code receives
or resolves `raw_data_root`, `sync_data`, `clip_data_root`, `finish_data_root`, the
VLA dataset root, or a processing-output root.

No destructive reset command was executed. The README only documents the automatic
development metadata reset performed by normal server startup/restart.

## Final static audit

- No production match for `forward_events_until_idle`,
  `subscribe_web_session_events`, `web_session_subscription_key`, `@app.websocket`,
  `event_cursor`, or `save_agentscope_event_cursor`.
- No frontend source match for `WebSocket` or `openSessionEvents`.
- No non-test frontend source match for MainRouterAgent/NavigationDataAgent internal
  names, internal agent ids, or `agentscope_session_id`.
- No production match for the obsolete `已转后台` tool status.
- Public status types and SQLite constraints remain exactly running/success/failure/
  stopped; UI labels are 正在调用/成功/失败/已停止.
- Pins are exact: `agentscope==2.0.4` and
  `@agentscope-ai/agentscope` `0.0.13` in both package manifest and lockfile.
- Legacy TUI `AgentScopeEventAdapter` remains in the Navigation workflow adapter and
  was not modified or prohibited.

## Minor triage and remaining concerns

1. `projection_private_identities()` still enumerates persisted mappings for each
   in-memory Web session on every projected event. This preserves the correctness
   boundary for historical/multi-agent identities after restart. A safe cache needs
   complete hydration and invalidation across mapping save/restore/delete and process
   recovery; changing it during final cleanup would risk private-identity leakage.
   It remains a performance-only follow-up.
2. `_tool_outcome_locks` remains an unbounded per-session lock cache. Popping a lock
   during deletion is not mechanically safe: a late background outcome could still
   hold the old lock while a concurrent path creates a new lock, breaking terminal
   outcome serialization. Lifecycle-aware cleanup should be designed with run/task
   quiescence and is deferred as a low-severity resource-growth concern.

No unresolved Task 11 functional or artifact-safety failure was found.
