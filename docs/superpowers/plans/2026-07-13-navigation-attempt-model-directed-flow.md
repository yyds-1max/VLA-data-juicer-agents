# Navigation Task Attempts and Model-Directed Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the current model-authored navigation implementation so every new Web session creates an independent task attempt, the NavigationDataAgent investigates current products and selects the stage itself, router handoff results are truthful, and only a genuinely running data-writing action can block another attempt.

**Architecture:** Keep the implemented Observation/Evidence stores, immutable Plan repository, execution ledger, bounded responses, and fail-closed human-decision recovery. Replace the global `(date, segments)` task identity, entry-time artifact reconciliation, phase-driven tool exposure, and automatic phase transitions with a Web/AgentScope-session-bound task attempt and activity-driven tools. Facts enter durable state only through model-invoked inspection tools; the model selects one of the two complete Plan submission tools; code validates, persists, locks, and executes the accepted Plan.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, AgentScope 2.0.1, FastAPI, Redis, pytest, React/TypeScript/Vitest.

## Global Constraints

- Implement on branch `codex/navigation-model-authored-plan-implementation` from design commit `dc53a76`; do not reset or reimplement the completed foundation.
- Treat `NavigationTask` as one attempt bound to one Web/AgentScope session, never as the global lifecycle of a date/segment scope.
- A successful delegation from a new Web session always creates a new attempt. It never attaches to, transfers, restores, or mutates an older attempt merely because the target matches; only a genuine pre-existing writer may reject delegation before attempt creation.
- Same-session continuation may reuse the accepted Plan and ledger, but mutable artifacts must be re-inspected before the model authors new work.
- Entry/runtime code may parse the handoff and create the attempt. It must not inspect products, append artifact observations, select a stage, or manufacture a user decision.
- The NavigationDataAgent chooses inspection calls, stage, strategies, steps, variants, and business parameters. Code records facts and validates choices.
- Both `submit_extract_sync_plan` and `submit_finish_processing_plan` remain available while planning. The selected tool is the model's stage decision.
- Only an actual running step whose capability declares `locks_navigation_target=True` may block an overlapping target with `navigation_data_busy`.
- Preserve plan-bound argument loading, exactly-once step transitions, cancellation, staged-result/outbox recovery, and fail-closed human-decision recovery.
- Preserve `dry_run=False` as the public/default behavior. Remove `dry_run` from the router/model-facing tool schema; tests or operators may inject `dry_run=True` only through trusted runtime/direct-CLI configuration.
- Do not migrate or normalize pre-redesign navigation durable state. An incompatible SQLite schema raises `NavigationStateResetRequired` without mutating the file; operators stop the service, back it up, and create a fresh navigation-state database.
- Keep AgentScope compression and phase-boundary session handling unchanged.
- Keep routine response limits: planning context/evidence at most 5,500 serialized characters, validation errors at most 3,000, and other tool results at most 4,000.
- Remove superseded functions, fields, migrations, prompts, fixtures, and tests in this change. Do not leave compatibility aliases, deprecated wrappers, or unreachable branches after Task 7.
- Use TDD. For every task: implementer runs the focused tests, a spec-review subagent checks behavior against the design, a code-quality subagent checks the diff, and only then commit.
- Do not run a real server processing job as part of local implementation. Server synchronization and real-data acceptance require a separate user-approved step after local verification.

## Baseline Decision Map

| Area | Preserve | Replace or delete |
| --- | --- | --- |
| Facts | Typed observation payloads, evidence refs, bounded reads | Observation `phase`, automatic entry observation, code-authored missing/next-stage guidance |
| Planning | Strict phase-specific JSON inputs, validation attempts, atomic Plan activation | Task-phase prerequisite, single phase-selected submission tool, deterministic phase routing |
| Execution | Immutable Plan, canonical arguments, ledger, outbox, recovery | Artifact reconciliation after each step, task phase as product truth |
| Sessions | Web-to-AgentScope mapping, same-session durable Plan/ledger | Cross-session ownership transfer and global active target identity |
| Router | Concrete-task triage and structured handoff tool | Global target ownership and ambiguous handoff results |
| Prompts | Short durable invariants | Repeated schemas, operator runbooks, automatic phase/reconciliation instructions |

---

### Task 1: Add Session-Bound Task Attempts Without Global Target Ownership

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/task_state.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/aggregate_revision.py`
- Modify: `src/vla_data_juicer_agents/navigation/catalog.py`
- Modify: `tests/test_navigation_task_store.py`
- Modify: `tests/test_navigation_catalog.py`

**Final interfaces after Task 7:** Task 1 creates a fresh transitional schema with the new attempt fields plus temporary legacy columns needed by unchanged consumers. It never upgrades an old deployment database. Task 7 removes the consumers and creates only the final clean schema shown here.

```python
class NavigationTaskStatus(str, Enum):
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    NEEDS_REPLAN = "needs_replan"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class NavigationTaskPhase(str, Enum):
    EXTRACT_SYNC = "extract_sync"
    FINISH_PROCESSING = "finish_processing"


class NavigationTask(BaseModel):
    task_id: str
    request: str
    target: str
    date: str
    segments: list[str] | None
    scene_mode: Literal["in", "out"] | None
    dry_run: bool = False
    status: NavigationTaskStatus = NavigationTaskStatus.ACTIVE
    accepted_plan_phase: NavigationTaskPhase | None = None
    guidance_revision: int = 0
    state_revision: int = 0
    created_by_web_session_id: str
    agentscope_session_id: str
    schema_version: int = TASK_SCHEMA_VERSION
    created_at: str
    updated_at: str
```

```text
def create_task_attempt(
    self,
    *,
    request: str,
    target: str,
    date: str,
    segments: list[str] | None,
    scene_mode: str | None,
    dry_run: bool,
    web_session_id: str,
    agentscope_session_id: str,
) -> TaskAttemptCreation

def find_by_session(
    self,
    *,
    web_session_id: str,
    agentscope_session_id: str,
) -> NavigationTask | None

find_running_target_writer(
    *, date: str, segments: list[str] | None
) -> NavigationRunningWriter | None
```

`TaskAttemptCreation` contains `task` and `created: bool`. Replaying the same exact session/date/segments/target returns `created=False`; the same session may own a later attempt for a different target.

- [ ] **Step 1: Replace global-owner and legacy-migration tests with clean-cutover tests**

Add tests proving:

```python
def test_new_web_sessions_create_distinct_attempts_for_same_target(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    first = store.create_task_attempt(
        request="处理数据", target="20270623", date="20270623",
        segments=["20260623_145550"], scene_mode=None, dry_run=False,
        web_session_id="web-a", agentscope_session_id="as-a",
    )
    second = store.create_task_attempt(
        request="继续处理", target="20270623", date="20270623",
        segments=["20260623_145550"], scene_mode=None, dry_run=False,
        web_session_id="web-b", agentscope_session_id="as-b",
    )

    assert first.task_id != second.task_id
    assert store.find_by_session(
        web_session_id="web-b", agentscope_session_id="as-b"
    ).task_id == second.task_id


def test_foreign_session_cannot_mutate_an_attempt(tmp_path: Path):
    store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task = store.create_task_attempt(
        request="处理数据", target="20270623", date="20270623",
        segments=None, scene_mode=None, dry_run=False,
        web_session_id="web-a", agentscope_session_id="as-a",
    )

    with pytest.raises(NavigationTaskOwnershipError):
        store.update_task_for_session(
            task.task_id,
            web_session_id="web-b",
            agentscope_session_id="as-b",
            status="completed",
        )
```

Add a two-thread barrier test proving concurrent calls with the same Web/AgentScope/target tuple create one row and both return the same task id. Add a test proving the same session can create a distinct later attempt for a different target.

Add an old-schema fixture proving initialization raises `NavigationStateResetRequired`, includes the configured database path and reset instruction in its bounded message, and leaves the database byte-for-byte/logically unchanged. Do not assert conversion, backfill, superseding, or preservation of old rows in the new store.

- [ ] **Step 2: Run the focused tests and confirm the old invariant fails**

Run: `pytest tests/test_navigation_task_store.py -q`

Expected: the distinct-attempt test fails because the current store reuses or rejects the matching global target.

- [ ] **Step 3: Create the new schema generation and fail closed on old state**

Define a navigation-state schema-generation marker and `NavigationStateResetRequired` in the task-store boundary. Initialization follows exactly two paths:

1. An empty/new database creates the Task 1 transitional schema, including `request`, `target`, `accepted_plan_phase`, non-unique target-history/session indexes, the unique replay index, and no global `(date, segments)` uniqueness.
2. A database containing navigation tables without the exact supported generation/required columns/index contract raises `NavigationStateResetRequired` before executing any `ALTER`, `UPDATE`, `DROP`, or row copy.

Remove task/profile/segment/attempt backfill and table-rebuild migrations that exist only for pre-redesign state. Do not add synthetic legacy identities or duplicate-history normalization. Keep temporary legacy columns and enum values only in the fresh transitional `CREATE TABLE` statement so Tasks 2–6 remain runnable; they are not a compatibility promise to old files.

- [ ] **Step 4: Implement `create_task_attempt` and immutable session identity**

`create_task_attempt` uses one `BEGIN IMMEDIATE` transaction: select the exact Web/AgentScope/date/segments/target tuple and return it as `created=False`, otherwise insert. The composite unique replay index closes concurrent handoff races without forbidding a different later target in the same session. `find_by_session` returns the newest attempt for the exact verified pair. Every new attempt-bound mutation and execution claim runs under `BEGIN IMMEDIATE`; `authorize_navigation_task_write` verifies the exact `created_by_web_session_id`/`agentscope_session_id` pair and reselects that pair's newest row by `created_at DESC, rowid DESC`, rejecting a captured older Attempt before any new work. Once an action or human handoff is durably in flight, only its exact non-null owner and exact durable plan/step/action or handoff identity may terminalize that already-started work; creating a newer Attempt must not strand a running ledger row or target lock. There is no `latest_web_session_id` and no rebind operation.

- [ ] **Step 5: Introduce the narrow running-writer query**

Add `locks_navigation_target: bool = False` to `ToolCapability` and mark every currently known data-mutating processing action explicitly. `find_running_target_writer` joins running ledger steps to their Task and Plan action, then uses the catalog flag and the shared target-overlap rule: dates must match, and either side selecting all segments or explicit segment intersection means overlap. Rows whose owning task has `dry_run=True` are excluded because they do not genuinely write data. It is a read-only preflight for handoff; Task 5 adds the atomic claim-time enforcement that closes the race.

- [ ] **Step 6: Keep old consumers compiling temporarily**

Until Tasks 2–6 switch all consumers, legacy read helpers may remain private in `task_store.py`, but no new code may call `create_or_update_task`, `find_latest_by_date`, or any ownership-transfer path. They operate only on databases freshly created by this branch. Task 7 deletes them.

- [ ] **Step 7: Verify and commit**

Run: `pytest tests/test_navigation_task_store.py tests/test_navigation_catalog.py tests/test_navigation_plan_store.py -q`

Expected: pass.

Commit: `git commit -m "refactor: make navigation tasks session-bound attempts"`

---

### Task 2: Make Router Handoff Create a Fresh Attempt and Return an Authoritative Result

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/task_entry.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `tests/test_web_agentscope_session.py`
- Modify: `tests/test_navigation_agent_tools.py`

**Final contracts:**

```python
class NavigationTaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str
    target: str
    date: str = Field(pattern=r"^[0-9]{8}$")
    segments: list[str] | None = None
    scene_mode: Literal["in", "out"] | None = None
    response_language: str
```

`NavigationTaskEntry` represents model-supplied request identity only. Execution mode is a trusted runtime argument and is not part of the router tool schema or structured handoff JSON.

```json
{"ok":true,"started":true,"task_id":"nav_123","message":"导航数据任务已启动。"}
```

```json
{"ok":false,"started":false,"error_type":"navigation_data_busy","message":"该目标当前有正在运行的数据写入操作。"}
```

- [ ] **Step 1: Write failure-first runtime tests**

Cover all of these cases:

- successful handoff creates one attempt and zero observations;
- a second Web session with the same date/segments creates a different attempt;
- a completed/waiting/failed older attempt does not block;
- invalid handoff returns `ToolResultState.ERROR` with `ok:false, started:false`;
- a known busy conflict returns `error_type=navigation_data_busy` without changing Web mapping;
- success includes `ok:true`, `started:true`, and the created `task_id`;
- `start_navigation_data_task.input_schema` has no `dry_run` property;
- the production/runtime default creates `dry_run=False` attempts;
- a test-only/operator runtime configuration can create `dry_run=True` attempts without exposing that switch to the router model.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_web_agentscope_session.py tests/test_navigation_agent_tools.py -q -k 'handoff or start_navigation or cross_web'`

Expected: old cross-session ownership tests fail and success metadata lacks `ok`/`task_id`.

- [ ] **Step 3: Extract pure handoff parsing**

Move structured-message parsing from `task_reconciliation.py` to `task_entry.py`. Parsing validates only request identity and user-supplied guidance. It performs no filesystem access and returns `NavigationTaskEntry`.

- [ ] **Step 4: Replace `prepare_navigation_task_entry` in the runtime**

`AgentScopeRuntime.start_navigation_agent_task` receives trusted `dry_run` from runtime configuration, not from the model, and must:

1. parse the handoff;
2. call the read-only `find_running_target_writer` preflight and return `navigation_data_busy` only when it finds a genuine overlapping non-dry-run writer, before changing the Web mapping;
3. establish the NavigationDataAgent AgentScope session;
4. call `create_task_attempt` for a new Web session, or retrieve the exact same bound attempt when the same runtime call is retried idempotently;
5. start the NavigationDataAgent with the structured request and compact attempt anchor;
6. return a value object containing `task_id` and `agentscope_session_id`.

It must not call artifact inspection, reconciliation, observation append, phase selection, or task ownership transfer. A pre-existing writer makes the entry preflight return busy before creating an attempt; if two starts race before either writes, both attempts may investigate and the atomic step claim in Task 5 chooses the sole writer.

- [ ] **Step 5: Add fail-closed start compensation**

If task creation or `_start_agent_run` scheduling fails, restore the prior Web mapping. When this call created the attempt and no child observation/Plan/ledger row exists, delete it with the existing CAS-style compensation; when it reused an idempotent attempt, leave it intact. Add injected-failure tests for each boundary and assert there is no active orphan attempt or navigation mapping after `started:false`.

- [ ] **Step 6: Return JSON in every handoff `ToolChunk`**

`NavigationHandoffTool.__call__` catches only declared entry/busy errors and encodes the same payload in `TextBlock.text` and `metadata`. Unknown exceptions are logged with their traceback and correlation id, then converted to a bounded `navigation_start_failed` response before reaching the model. Both success and failure include boolean `ok` and `started`.

- [ ] **Step 7: Verify and commit**

Run: `pytest tests/test_web_agentscope_session.py tests/test_navigation_agent_tools.py -q -k 'handoff or start_navigation or cross_web'`

Expected: pass.

Commit: `git commit -m "fix: make navigation handoff fresh and authoritative"`

---

### Task 3: Remove Phase From Observed Facts and Build a Stage-Neutral Planning Context

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/artifact_inspection.py`
- Modify: `src/vla_data_juicer_agents/navigation/observation_models.py`
- Modify: `src/vla_data_juicer_agents/navigation/observation_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/planning_context.py`
- Modify: `src/vla_data_juicer_agents/navigation/observation_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_reconciliation.py`
- Modify: `src/vla_data_juicer_agents/navigation/services.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_validation.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_submission_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py`
- Modify: `tests/test_navigation_observation_models.py`
- Modify: `tests/test_navigation_observation_store.py`
- Modify: `tests/test_navigation_observation_tools.py`
- Modify: `tests/test_navigation_planning_context.py`
- Create: `tests/test_navigation_artifact_inspection.py`
- Modify: `tests/test_navigation_agent_tools.py`

**Final context shape:**

```python
class NavigationTaskContext(StrictModel):
    task_id: str
    request: str
    target: str
    date: str
    segments: list[str] | None
    scene_mode: Literal["in", "out"] | None
    planning_context_revision: str
    observation_revision: int
    observed_kinds: list[ObservationKind]
    fact_summary: dict[str, Any]
    available_stage_ids: list[Literal["extract_sync", "finish_processing"]]
    evidence_catalog: list[EvidenceDescriptor]
    evidence_next_cursor: int | None = None
```

- [ ] **Step 1: Write phase-neutral observation tests**

Assert that `NavigationObservationRevision.model_json_schema()` has no `phase`, appending facts does not require task phase, context exposes both stage ids, and context contains no `required`, `missing`, `recommended`, or `next_tool` field.

Also assert that a fresh attempt with no observation builds a valid revision-0 context containing request facts, both stage ids, and an empty evidence catalog.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_navigation_observation_models.py tests/test_navigation_observation_store.py tests/test_navigation_planning_context.py -q`

Expected: current revision/context require a phase and return a phase-specific checklist.

- [ ] **Step 3: Create phase-neutral observation storage without legacy conversion**

Create `navigation_observation_revisions` without a `phase` column in the supported fresh schema. Do not rebuild or copy an existing phase-bearing table. Such a table belongs to an incompatible navigation-state generation and is rejected by the Task 1 store boundary before any observation/evidence service mutates it.

Change the append interface to:

```text
def append(
    self,
    task_id: str,
    completed_kind: ObservationKind,
    payloads: list[ObservationPayload],
    evidence_writes: list[EvidenceWrite],
    evidence_store: NavigationEvidenceWriter,
    *,
    expected_web_session_id: str,
    expected_agentscope_session_id: str,
) -> NavigationObservationRevision
```

Update the temporary legacy reconciliation caller to the phase-neutral append signature in the same commit. It remains unreachable from the Web entry after Task 2 and is deleted in Task 7; this prevents an intermediate broken import/test state without preserving it as a final compatibility path.

Delete `services._migrate_legacy_observations`, its migration marker, and conversion/conflict fixtures. Replace them with tests that a fresh unified service initializes phase-neutral observation/evidence tables repeatedly and that an incompatible legacy observation schema produces `NavigationStateResetRequired` without copying evidence or changing either database.

Mechanically update every current consumer of `observation.phase` and `PhasePlanningContext` in `plan_validation.py`, `plan_submission_tools.py`, and `agent_tools.py`. At this intermediate commit they may still use the legacy task phase to choose the one exposed submission tool; Task 4 removes that final phase prerequisite. No consumer may read a removed observation field.

- [ ] **Step 4: Isolate factual artifact inspection**

Move `build_navigation_artifact_snapshot` from `task_reconciliation.py` to `artifact_inspection.py`. The function reports path/existence/completeness facts only. It does not set task status/phase or append an observation until the model calls `inspect_navigation_artifacts_tool`.

- [ ] **Step 5: Replace phase planning projection**

Rename `PhasePlanningContext` to `NavigationTaskContext`, `build_phase_planning_context` to `build_navigation_task_context`, and `get_phase_planning_context_tool` to `get_navigation_task_context_tool`.

`build_navigation_task_context` accepts `observation=None` for a new attempt and projects revision `0`. `compute_planning_context_revision` hashes task id, immutable request/target/date/segments, scene guidance revision, observation revision, and capability catalog revision. It does not hash a code-selected task phase.

- [ ] **Step 6: Bound inspection returns to factual deltas**

Every successful inspection tool returns exactly these top-level keys:

```python
{"ok", "observation_revision", "observed_kind", "summary", "evidence_refs"}
```

No inspection response includes full evidence, stage, required/missing checklist, recommended binding, next tool, schema, accumulated history, or complete profile.

Failures use the common bounded `ok:false/error_type/message` contract and append no partial observation revision.

- [ ] **Step 7: Verify and commit**

Run: `pytest tests/test_navigation_observation_models.py tests/test_navigation_observation_store.py tests/test_navigation_observation_tools.py tests/test_navigation_planning_context.py tests/test_navigation_artifact_inspection.py tests/test_navigation_agent_tools.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_validation.py -q`

Expected: pass, and response-size assertions remain within their hard limits.

Commit: `git commit -m "refactor: make navigation observations stage neutral"`

---

### Task 4: Let the Model Select the Stage and Resolve Tools by Activity

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/services.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_submission_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_validation.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/planning_context.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `tests/test_navigation_agent_tools.py`
- Modify: `tests/test_navigation_task_tools.py`
- Modify: `tests/test_navigation_plan_submission_tools.py`
- Modify: `tests/test_navigation_plan_validation.py`
- Modify: `tests/test_navigation_plan_store.py`

**Resolver rules:**

```text
no bound attempt                  -> fail closed; no navigation mutation tools
active executable Plan/ledger     -> overview + current step + plan-bound actions
no active executable Plan         -> all factual inspections + cognitive reads + both submission tools
completed extract-sync Plan       -> investigation/planning tools remain available
recovery-required human handoff   -> recovery-safe reads only; existing fail-closed behavior
```

- [ ] **Step 1: Write model-selected-stage and activity-driven tool tests**

Assert that a fresh attempt receives all inspection tools, `get_navigation_task_context`, evidence tools, action description, and both submission tools. Assert that no resolver call invokes artifact inspection or mutates task phase. Assert that a completed extract-sync Plan returns to planning tools instead of automatically becoming finish-processing.

For the same fresh phase-neutral attempt, assert that a valid extract-sync Plan records `accepted_plan_phase=extract_sync`, a valid finish Plan records `accepted_plan_phase=finish_processing`, and neither requires a prior task phase. Invalid submission leaves accepted phase, active Plan, and ledger unchanged. Success returns `ok:true` without echoing the Plan; failure returns `ok:false` with bounded errors and no schema/candidate echo.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_navigation_agent_tools.py tests/test_navigation_task_tools.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_validation.py tests/test_navigation_plan_store.py -q`

Expected: current resolver calls reconciliation, exposes one phase-selected subset, and submission rejects a phase-neutral attempt.

- [ ] **Step 3: Move required-fact policy to submitted Plan validation**

Rename `PHASE_REQUIRED_OBSERVATIONS` to `PLAN_REQUIRED_OBSERVATIONS`. Use only the submitted Plan type to select required factual kinds. Missing facts yield `plan_validation_failed`; they never cause code to choose the other stage.

`build_navigation_plan_submission_tools` always returns both `submit_extract_sync_plan_tool` and `submit_finish_processing_plan_tool`. Each input remains phase-specific and `extra="forbid"`; no combined schema is injected into the prompt.

- [ ] **Step 4: Record the selected phase atomically**

In the same transaction that writes the immutable Plan and initializes ledger rows, set `navigation_tasks.accepted_plan_phase` to the submitted phase and increment aggregate revision. On any validation or transaction failure, none of those writes is visible. Invalid candidates remain only in `PlanSubmissionAttempt` and correction always resubmits a whole Plan.

- [ ] **Step 5: Rewrite `resolve_navigation_agent_tools`**

Resolve the bound attempt only with the exact Web/AgentScope pair. Determine execution activity from active Plan plus ledger status, not from artifacts or task phase. Remove the call to `reconcile_navigation_task`.

- [ ] **Step 6: Remove model-facing task lifecycle tools**

Delete `get_or_create_navigation_task_tool`, `reconcile_navigation_task_tool`, and cross-session resumable-task listing. The runtime, not the model, creates the attempt.

Retain one bound guidance tool:

```python
record_navigation_user_guidance_tool(
    text: str,
    scene_mode: Literal["in", "out"] | None = None,
) -> {"ok": True, "guidance_revision": int, "observation_revision": int}
```

It appends `UserGuidanceObservation` and updates scene guidance. It does not inspect artifacts, select a stage, or advance a phase.

- [ ] **Step 7: Make action description stage-neutral**

`describe_processing_action` accepts one requested action id and returns only that action's variants, parameter contract, preconditions, and constraints. It does not filter by task phase or recommend an action/variant.

- [ ] **Step 8: Keep hard authorization independent of exposure**

Calling a hidden/stale plan-bound tool must still verify session ownership, active Plan, current step, dependencies, and canonical action. Tool-list hiding is not an authorization boundary.

- [ ] **Step 9: Verify and commit**

Run: `pytest tests/test_navigation_agent_tools.py tests/test_navigation_task_tools.py tests/test_session_tool_registry.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_validation.py tests/test_navigation_plan_store.py tests/test_navigation_plan_contracts.py -q`

Expected: pass.

Commit: `git commit -m "refactor: let model select navigation activity"`

---

### Task 5: Replace Automatic Reconciliation With Plan-Ledger Facts and Enforce the Narrow Target Lock

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/catalog.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_execution.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py`
- Modify: `tests/test_navigation_catalog.py`
- Modify: `tests/test_navigation_plan_store.py`
- Modify: `tests/test_navigation_plan_execution.py`
- Modify: `tests/test_navigation_model_authored_flow.py`

**Lock semantics:**

```python
class StepClaimOutcome(str, Enum):
    CLAIMED = "claimed"
    NOT_CLAIMABLE = "not_claimable"
    NAVIGATION_DATA_BUSY = "navigation_data_busy"
```

- [ ] **Step 1: Write target-lock tests**

Cover:

- completed, failed, waiting, and planning attempts never block;
- a running non-locking read/validation step never blocks;
- a running locking step blocks the same date when either segment list means all segments;
- explicit segment sets block only when they intersect;
- disjoint dates and disjoint explicit segments can run concurrently;
- two simultaneous claims for an overlapping target produce one `CLAIMED` and one `NAVIGATION_DATA_BUSY`;
- a terminal/recovered step releases the lock because no separate lease remains;
- stale running work remains fail-closed until the existing controlled recovery resolves it.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_navigation_catalog.py tests/test_navigation_plan_store.py tests/test_navigation_plan_execution.py -q -k 'claim or lock or busy or reconcile or finaliz'`

Expected: no target lock exists and execution finalization calls task reconciliation.

- [ ] **Step 3: Audit the catalog lock declarations introduced in Task 1**

Confirm `locks_navigation_target=True` only on actions that create, overwrite, or delete navigation data. Keep artifact inspection, evidence reads, validation views, and human-decision requests false. The catalog invariant test must fail whenever a new executable data mutation omits the flag.

- [ ] **Step 4: Claim the target in the ledger transaction**

Within the existing `BEGIN IMMEDIATE` step-claim transaction, join running steps to their tasks. Targets overlap when dates match and either side selects all segments, or when explicit segment sets intersect. A dry-run task neither acquires nor conflicts with this lock. Return `NAVIGATION_DATA_BUSY` without changing either ledger when a conflicting real writer exists. This claim-time check is authoritative; the entry preflight from Task 2 is only an early user-facing rejection when a writer was already running.

Do not create a second lock table or an expiring lease. The running ledger row is the lock source of truth, so normal success/failure/recovery transitions release it atomically.

- [ ] **Step 5: Remove artifact-driven execution finalization**

Delete every execution-path call to `reconcile_navigation_task` and `build_navigation_artifact_snapshot`, including pre-execution authorization, staged-result finalization, and failure handling. Step results update the ledger/outbox and persist factual produced refs only.

Replace the broad snapshot/phase check with `verify_plan_step_preconditions(plan, step)`, which checks only concrete filesystem inputs declared by the accepted action contract and canonical Plan arguments. A changed precondition returns bounded `input_precondition_changed`, stores execution evidence, and invalidates the immutable Plan/ledger for replan; it does not append a model investigation observation, select another stage, or update task product state.

Add tests that monkeypatch `build_navigation_artifact_snapshot` to raise if any plan execution or authorization path calls it.

- [ ] **Step 6: Record lifecycle without choosing the next business stage**

- completing an extract-sync Plan marks that Plan completed and leaves the attempt active;
- completing a finish-processing Plan marks the attempt completed only when every ledger step, including the validator-required terminal `validate_navigation_outputs` step, is `completed`;
- failure/replan state belongs to Plan/ledger recovery and does not assert filesystem product state;
- no code automatically sets finish-processing as the next stage or asks/answers for the user.

- [ ] **Step 7: Return compact busy failures**

Plan-bound execution returns:

```json
{"ok":false,"error_type":"navigation_data_busy","message":"An overlapping navigation data write is already running.","retry":"wait_and_reinspect"}
```

It does not transfer ownership, reuse the other attempt, or expose the other session id.

- [ ] **Step 8: Verify and commit**

Run: `pytest tests/test_navigation_catalog.py tests/test_navigation_plan_store.py tests/test_navigation_plan_execution.py tests/test_navigation_model_authored_flow.py -q`

Expected: pass.

Commit: `git commit -m "fix: lock only running navigation data writes"`

---

### Task 6: Rewrite Router and Navigation Guidance Around Model-Directed Decisions

**Files:**
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_prompts.py`
- Modify: `src/vla_data_juicer_agents/navigation/agents.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Rewrite: `docs/navigation-plan-agent-guidance.md`
- Create: `docs/navigation-plan-server-acceptance.md`
- Modify: `tests/test_agentscope_bootstrap.py`
- Modify: `tests/test_navigation_agents.py`
- Modify: `tests/test_navigation_context_budget.py`

- [ ] **Step 1: Write prompt/guidance contract tests**

Router prompt must contain only triage, exact handoff preservation, and truthful `ok/started` handling. It must not contain artifact-stage rules.

Router tool-schema tests also assert `dry_run` is absent; production execution mode comes from trusted runtime configuration with default false.

Navigation prompt must contain:

- investigate before deciding;
- do not trust user claims, memory, or task status as current product facts;
- model chooses inspection tools, stage, decisions, steps, variants, and parameters;
- submit a complete JSON Plan and replace the whole Plan after validation errors;
- execute only the accepted Plan;
- after extract/sync, verify outputs, report, ask whether to continue, and collect finish inputs before finish planning;
- same-session Plan/ledger durability and new-session fresh-attempt semantics.

- [ ] **Step 2: Run prompt tests and confirm the old instructions fail**

Run: `pytest tests/test_agentscope_bootstrap.py tests/test_navigation_agents.py tests/test_navigation_context_budget.py -q -k 'prompt or guidance or schema or budget'`

Expected: current text still asserts automatic entry reconciliation/durable phase and includes operator-only content.

- [ ] **Step 3: Rewrite the main router prompt**

Keep routing rules only. The router answers ordinary conversation/capability questions itself and delegates a concrete target. It never decides product stage. After calling `start_navigation_data_task`, it bases its user-facing response on the structured result and reports success only for `ok:true, started:true`.

- [ ] **Step 4: Rewrite the NavigationDataAgent prompt**

Keep durable invariants in the system prompt, not duplicated cookbook content. The compact per-turn anchor may include only attempt id, observation revision, accepted Plan id/revision, current ledger step, and execution status. It must not include artifact snapshots, a selected next stage, full Plan JSON, schemas, or historical errors.

- [ ] **Step 5: Rewrite the domain guidance as a compact playbook**

`docs/navigation-plan-agent-guidance.md` must contain exactly these sections:

1. product dependency map;
2. recommended investigation order;
3. common extract-sync work;
4. common finish-processing work;
5. model/code decision ownership;
6. user-confirmation points;
7. failure/retry behavior;
8. four bounded few-shots.

Few-shots cover: user claims sync complete but products are missing; new session finds sync complete and finish missing; extract/sync just completed and the agent asks before continuing; invalid complete Plan is corrected by whole-Plan resubmission.

- [ ] **Step 6: Move operator instructions out of model context**

Move deployment synchronization, Git checks, token measurement, server log queries, real-data acceptance, and legacy storage cleanup instructions to `docs/navigation-plan-server-acceptance.md`. The agent guidance must contain none of them.

- [ ] **Step 7: Measure static and representative context**

Assert that prompt + guidance + both submission schemas + compact anchor fit the existing budget test and that the representative transcript peak stays at or below 83,885 input tokens. Do not change `ContextConfig(tool_result_limit=6000)` or AgentScope compression.

- [ ] **Step 8: Verify and commit**

Run: `pytest tests/test_agentscope_bootstrap.py tests/test_navigation_agents.py tests/test_navigation_context_budget.py -q`

Expected: pass.

Commit: `git commit -m "docs: teach model-directed navigation investigation"`

---

### Task 7: Delete Superseded Reconciliation, Task-State, and Phase-Routing Code

**Files:**
- Delete: `src/vla_data_juicer_agents/navigation/task_reconciliation.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_state.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py`
- Modify: `src/vla_data_juicer_agents/navigation/services.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_execution.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Delete or rewrite: `tests/test_navigation_task_reconciliation.py`
- Modify: all tests importing removed symbols

- [ ] **Step 1: Add a dead-reference test/scripted audit expectation**

The following search must return no source/test references after cleanup:

```bash
rg -n "prepare_navigation_task_entry|reconcile_navigation_task|create_or_update_task|find_latest_by_date|latest_web_session_id|artifact_snapshot_json|waiting_scene_mode|get_or_create_navigation_task_tool|reconcile_navigation_task_tool|get_phase_planning_context_tool|PhasePlanningContext|PHASE_REQUIRED_OBSERVATIONS|NavigationTaskDrift" src
rg -n "build_navigation_artifact_snapshot" src/vla_data_juicer_agents/navigation/plan_execution.py
```

Expected final exit status for each command: `1` because no matches are found. `build_navigation_artifact_snapshot` remains valid only in the model-invoked artifact inspection implementation.

- [ ] **Step 2: Delete the reconciliation module**

The factual snapshot builder already lives in `artifact_inspection.py`; handoff parsing already lives in `task_entry.py`. Delete the remaining automatic phase/status/recovery logic and its obsolete tests instead of leaving wrappers.

- [ ] **Step 3: Delete legacy task APIs and fields**

Remove global date/segments claim/update, cross-session rebind, latest-session ownership, automatic phase/status mutators, artifact/drift snapshots on Task, and dataset-state transition helpers.

Remove `latest_web_session_id`, legacy `phase`, `artifact_snapshot_json`, `drift_json`, `last_completed_step`, `latest_run_id`, `waiting_reason`, and `next_required_input` from the supported schema and model. Bump the schema generation. The final code creates this clean table only for a new database and raises `NavigationStateResetRequired` for the Task 1 transitional generation or any older schema; it does not rebuild/copy the parent table or map old statuses.

Keep `needs_replan` in the final status enum because it is operational recovery state, not product evidence. Add tests for fresh final-schema creation and fail-fast incompatible-schema detection, not pre-migration row preservation.

- [ ] **Step 4: Update direct workflow/CLI entry**

Direct local workflow entry must create its own explicit attempt identity (`web_session_id=f"direct:{run_id}"` and the actual direct AgentScope session id), call the same model-directed/factual APIs, and preserve cancellation/dry-run behavior. It must not resurrect entry reconciliation or deterministic stage selection.

- [ ] **Step 5: Preserve fail-closed recovery while relocating `needs_replan`**

Human-decision controlled recovery continues to quarantine the handoff, invalidate the Plan, and mark the task plus unfinished ledger rows `needs_replan`. This status authorizes controlled replanning for the same attempt; it is never treated as proof about filesystem products.

- [ ] **Step 6: Run focused schema and integration tests**

Run:

```bash
pytest tests/test_navigation_task_store.py tests/test_navigation_observation_store.py tests/test_navigation_model_authored_flow.py tests/test_navigation_cli.py tests/test_web_agentscope_session.py tests/test_web_human_decision_api.py -q
```

Expected: pass, including repeat initialization of the exact supported schema and fail-fast rejection of an incompatible schema without mutation.

- [ ] **Step 7: Run the dead-reference audit and commit**

Run the `rg` command from Step 1.

Expected: no output, exit status 1.

Commit: `git commit -m "refactor: remove navigation dataset state machine"`

---

### Task 8: Prove the Corrected End-to-End Behavior and Context Budget

**Files:**
- Modify: `tests/test_navigation_model_authored_flow.py`
- Modify: `tests/test_navigation_context_budget.py`
- Modify: `tests/test_web_agentscope_session.py`
- Modify: `docs/navigation-plan-server-acceptance.md`

- [ ] **Step 1: Add the raw-only new-session flow**

Test: router handoff creates an empty-observation attempt; NavigationDataAgent calls artifact/raw/topic/sensor inspections; model submits one complete extract-sync Plan; Plan executes with canonical arguments; no entry code observation or phase transition occurs.

- [ ] **Step 2: Add the same-session two-stage boundary**

Test: after extract-sync Plan completion, tools remain in investigation/planning mode; the agent verifies products, reports completion, and asks whether to continue; no finish Plan or finish tool call occurs before a user reply. After the user continues and supplies scene mode, the agent records guidance, re-inspects relevant facts, and may submit a complete finish Plan.

- [ ] **Step 3: Add the new-session continuation flow**

Test: an old session has a completed extract-sync Plan. A new session for the same data gets a new task id and no inherited observations/Plan. Even when the user says “同步已完成”, the new NavigationDataAgent first calls artifact inspection. Verified complete sync plus missing final products permits a finish Plan; missing sync products leads to extract-sync investigation instead.

- [ ] **Step 4: Add deletion-and-rerun behavior**

Test: after a historical completed attempt, simulated products are removed while raw data remains. A new attempt sees raw-only facts and can produce a new extract-sync Plan. No historical task state suppresses work.

- [ ] **Step 5: Add exact handoff and busy regressions**

Test the server failure shape: a completed old task never causes “belongs to another Web session”. A concurrently running overlapping write yields a truthful bounded `ok:false/started:false/navigation_data_busy` result.

- [ ] **Step 6: Run context instrumentation**

Record every tool-result size, exposed tool-schema size, and per-turn model input for a representative investigation + invalid Plan + corrected Plan + dry-run execution transcript.

Assertions:

```python
assert max_tool_result_chars <= 5_500
assert validation_failure_chars <= 3_000
assert peak_model_input_tokens <= 83_885
assert compact_event_count == 0
```

- [ ] **Step 7: Run the complete local verification**

Run:

```bash
pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
python -m compileall -q src tests
git diff --check
git status --short
```

Expected: all Python tests pass; all frontend tests pass; frontend build and compileall succeed; `git diff --check` prints nothing; only the intended implementation changes are present before commit.

- [ ] **Step 8: Run final dead-code searches**

Run:

```bash
rg -n "WorkflowPlanDraftState|NavigationPlanDraftStore|get_workflow_plan_draft_tool|update_workflow_plan_draft_tool|finalize_extract_sync_plan_tool|finalize_finish_processing_plan_tool|finalize_workflow_plan_tool|schema_snapshot|build_deterministic_plan_template|prepare_navigation_task_entry|reconcile_navigation_task" src
rg -n "full.*schema|draft snapshot|cross-session resume|automatic.*phase" src/vla_data_juicer_agents/runtime docs/navigation-plan-agent-guidance.md
rg -n "build_navigation_artifact_snapshot" src/vla_data_juicer_agents/navigation/plan_execution.py
```

Expected: no obsolete implementation references. A documentation mention is permitted only in the design/spec migration history, not runtime prompts or model guidance.

- [ ] **Step 9: Request final review and commit**

Use `superpowers:requesting-code-review` with the design spec and this plan. Resolve all blocking findings, rerun Step 7, then commit:

`git commit -m "test: verify model-directed navigation attempts"`

---

## Server Acceptance Handoff (After Local Completion and Separate Approval)

The local implementation is complete before this section begins. Do not modify server code or data while implementing Tasks 1–8.

After the user approves deployment testing:

1. verify local/server commit identity and service configuration;
2. synchronize only the approved code;
3. start the service with default `dry_run=False`, using `dry_run=True` only for an explicitly selected test run;
4. create a new Web session for `20270623 / 20260623_145550`;
5. verify a fresh attempt is created even though historical attempts exist;
6. verify the NavigationDataAgent itself invokes artifact inspection before stage choice;
7. collect handoff JSON, task/Plan/ledger rows, tool-result sizes, per-turn tokens, compact events, and bounded logs;
8. stop before GUI/human steps unless the user is present and has explicitly asked to continue;
9. do not delete or alter unrelated server files, code, logs, or datasets.

Only if the optimized real-data run still reaches the compact trigger should phase-boundary AgentScope sub-session rotation be reconsidered as a separate change.
