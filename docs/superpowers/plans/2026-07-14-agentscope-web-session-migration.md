# AgentScope 2.0 Web Session Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace DataPilot's turn-scoped custom WebSocket interaction layer with durable AgentScope 2.0.4 session events, resumable public sessions, authoritative tool terminal states, safe interruption/deletion, and bounded failed-step diagnosis.

**Architecture:** Keep the DataPilot visual shell and one public session ID, but persist sanitized AgentScope events before broadcasting them through a per-selected-session SSE stream. Install DataPilot middlewares inside AgentScope's built-in `ToolOffloadMiddleware` so the real `ToolResponse`, including a background completion, is the only source of success/failure; keep a separate public tool ledger for `success`, `failure`, and explicit user `stopped`. Use AgentScope's TypeScript SDK to reduce native reply events into messages while retaining small DataPilot projections for tool cards and Navigation HITL.

**Tech Stack:** Python 3.11, FastAPI, SQLite, Redis, AgentScope 2.0.4, React 19, TypeScript 5.7, Zustand 5, `@agentscope-ai/agentscope` 0.0.13, pytest, Vitest.

## Global Constraints

- Pin Python AgentScope exactly to `agentscope==2.0.4`.
- Pin the frontend SDK to `@agentscope-ai/agentscope@0.0.13` in `package-lock.json`.
- Do not modify AgentScope source code.
- Keep the DataPilot visual identity and never expose MainRouterAgent, NavigationDataAgent, internal agent IDs, or internal AgentScope session IDs to the browser.
- Only the currently selected public session owns an SSE connection.
- Persist public events before live broadcast; browser connectivity must never control AgentScope execution.
- Tool card terminal states are exactly `success`, `failure`, and `stopped`; `stopped` is only produced by an explicit user stop.
- ToolOffload's synthetic success is never a public terminal result.
- Deleting a session must never delete raw data, `sync_data`, finish outputs, or any other processing artifact.
- Existing development Web/AgentScope test sessions do not require compatibility migration and may be reset.
- Never automatically rerun a failed or stopped side-effecting processing step.
- Do not add incremental/partial extract-sync execution in this work.
- Preserve existing system-managed Navigation tool groups and context-compression behavior.
- Every implementation task follows red-green-refactor and ends in a focused commit.

---

## File Map

### New backend files

- `src/vla_data_juicer_agents/runtime/datapilot_projection.py`: AgentScope reply projection and real tool-outcome middlewares.
- `src/vla_data_juicer_agents/web/sse.py`: SSE frame encoding, replay-then-live stream, and heartbeat handling.
- `tests/test_datapilot_projection.py`: middleware ordering, sanitization, and real background outcome contracts.
- `tests/test_web_sse.py`: replay, reconnect, race, heartbeat, and connection-isolation tests.
- `tests/test_agentscope_204_contract.py`: dependency and embedded service API contract.

### New frontend files

- `frontend/src/store/agentConversation.ts`: AgentScope SDK message reducer plus DataPilot tool/HITL projections.
- `frontend/src/store/agentConversation.test.ts`: native event, reconnect, wakeup, and terminal-state reducer tests.

### Major modified backend files

- `pyproject.toml`: AgentScope version pin.
- `src/vla_data_juicer_agents/web/schemas.py`: public session, event envelope, tool run, and interrupt/delete response contracts.
- `src/vla_data_juicer_agents/web/session_store.py`: fresh public-session schema, durable events/tool runs, mappings, deletion.
- `src/vla_data_juicer_agents/web/event_stream.py`: broadcast persisted event records rather than transient dictionaries.
- `src/vla_data_juicer_agents/web/agent_session.py`: thin public-session coordinator; remove turn-scoped forwarding.
- `src/vla_data_juicer_agents/web/app.py`: SSE, delete, fire-and-forget turns, and no AgentScope forwarder tasks.
- `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`: transport binding, 2.0.4 interrupt/delete, projection middleware wiring.
- `src/vla_data_juicer_agents/navigation/plan_execution.py`: conservative side-effect completion state.
- `src/vla_data_juicer_agents/navigation/plan_store.py`: failed-recovery activity and step-result lookup.
- `src/vla_data_juicer_agents/navigation/agent_tools.py`: bounded failed-step result reader and safe failed-recovery surface.
- `src/vla_data_juicer_agents/navigation/tool_groups.py`: `failed_recovery` policy.
- `src/vla_data_juicer_agents/navigation/task_store.py`: delete only Navigation control-state rows owned by a public session.

### Major modified frontend files

- `frontend/package.json`, `frontend/package-lock.json`: AgentScope SDK dependency.
- `frontend/src/api/types.ts`: AgentScope SDK types and public SSE envelopes.
- `frontend/src/api/client.ts`: fetch-based SSE, delete, and new interrupt response.
- `frontend/src/store/datapilotStore.ts`: one writable session mode and SDK conversation state.
- `frontend/src/components/datapilot/DataPilotWindow.tsx`: abortable selected-session stream, restore, delete, and interrupt flow.
- `frontend/src/components/datapilot/SessionHistoryPanel.tsx`: select-and-resume plus explicit delete action.
- `frontend/src/components/datapilot/MessageList.tsx`: render SDK messages and public tool runs.
- `frontend/src/components/datapilot/AgentRunSummary.tsx`: render `success`, `failure`, and `stopped` only.

---

### Task 1: Pin AgentScope 2.0.4 and lock the public integration contract

**Files:**
- Create: `tests/test_agentscope_204_contract.py`
- Modify: `pyproject.toml:11-21`
- Modify: `frontend/package.json:12-27`
- Modify: `frontend/package-lock.json`

**Interfaces:**
- Consumes: AgentScope embedded app created by `agentscope.app.create_app`.
- Produces: exact dependency versions and verified access to `ChatService.interrupt`, `SessionService.delete_session`, `MessageBusKeys.bg_tasks`, and `MessageBusKeys.task_cancel_channel`.

- [ ] **Step 1: Write the failing Python contract test**

```python
from importlib.metadata import version
import inspect

from agentscope.app.message_bus import MessageBusKeys


def test_agentscope_204_embedded_contract():
    assert version("agentscope") == "2.0.4"
    assert callable(MessageBusKeys.bg_tasks)
    assert callable(MessageBusKeys.task_cancel_channel)


def test_embedded_services_expose_interrupt_and_delete(runtime):
    assert inspect.iscoroutinefunction(runtime.app.state.chat_service.interrupt)
    assert inspect.iscoroutinefunction(runtime.app.state.session_service.delete_session)
```

Use the existing fake/fixture construction pattern from `tests/navigation_chat_service_harness.py` so the second test enters the AgentScope lifespan before reading `app.state`.

- [ ] **Step 2: Run the contract test and confirm the version failure**

Run: `pytest tests/test_agentscope_204_contract.py -v`

Expected: FAIL because the installed and declared version is `2.0.1`.

- [ ] **Step 3: Upgrade both dependency locks**

Change Python to:

```toml
"agentscope==2.0.4",
```

Add the frontend dependency:

```json
"@agentscope-ai/agentscope": "0.0.13"
```

Run: `python -m pip install -e '.[dev]'`

Run: `npm install --prefix frontend @agentscope-ai/agentscope@0.0.13 --save-exact`

Expected: `pyproject.toml`, `frontend/package.json`, and `frontend/package-lock.json` all resolve the exact versions.

- [ ] **Step 4: Repair only 2.0.4 compatibility breaks exposed by the existing suite**

Use public 2.0.4 keys where available:

```python
await message_bus.publish(
    MessageBusKeys.session_interrupt_channel(),
    {"session_id": agentscope_session_id},
)
```

Do not change event lifetime or tool-card behavior in this task.

- [ ] **Step 5: Verify dependency and bootstrap compatibility**

Run: `pytest tests/test_agentscope_204_contract.py tests/test_agentscope_bootstrap.py tests/test_navigation_chat_service_tool_groups.py -v`

Run: `npm --prefix frontend run build`

Expected: all selected tests pass and TypeScript resolves the SDK imports.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml frontend/package.json frontend/package-lock.json tests/test_agentscope_204_contract.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py
git commit -m "build: upgrade AgentScope integration to 2.0.4"
```

---

### Task 2: Replace the development Web schema with durable public events and tool runs

**Files:**
- Modify: `src/vla_data_juicer_agents/web/schemas.py`
- Modify: `src/vla_data_juicer_agents/web/session_store.py`
- Modify: `tests/test_web_session_store.py`

**Interfaces:**
- Consumes: sanitized AgentScope event dictionaries and public session IDs.
- Produces:
  - `append_public_event(session_id: str, dedupe_key: str, event: dict[str, Any]) -> PublicEventRecord`
  - `list_public_events(session_id: str, after_sequence: int = 0) -> list[PublicEventRecord]`
  - `start_tool_run(session_id: str, tool_call_id: str, tool_name: str, started_at: str) -> PublicToolRun`
  - `finish_tool_run(..., status: Literal["success", "failure"]) -> PublicToolRun | None`
  - `stop_open_tool_runs(session_id: str) -> list[PublicToolRun]`

- [ ] **Step 1: Replace legacy-status tests with fresh-schema tests**

Add tests that create an old `sessions(status='historical')` schema, initialize `WebSessionStore`, and assert the development schema is reset rather than migrated:

```python
def test_old_web_schema_is_reset_without_touching_neighbor_files(tmp_path):
    db_path = tmp_path / "sessions.sqlite"
    create_legacy_web_schema(db_path)
    artifact = tmp_path / "VLADatasets" / "clip" / "sync_data" / "frame.jpg"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"frame")

    store = WebSessionStore(db_path)

    assert store.list_sessions() == []
    assert artifact.read_bytes() == b"frame"
```

Add atomic sequence/dedupe and first-terminal-wins tests:

```python
first = store.append_public_event(session.id, "as:s1:r1:0", reply_start)
duplicate = store.append_public_event(session.id, "as:s1:r1:0", reply_start)
assert first.id == duplicate.id
assert first.sequence == duplicate.sequence == 1

store.start_tool_run(session.id, "call-1", "extract", now)
assert store.finish_tool_run(session.id, "call-1", status="failure", summary="boom")
assert store.finish_tool_run(session.id, "call-1", status="success", summary="late") is None
```

- [ ] **Step 2: Run the store tests and confirm missing-contract failures**

Run: `pytest tests/test_web_session_store.py -v`

Expected: FAIL because the public event/tool APIs and fresh schema generation do not exist.

- [ ] **Step 3: Define the new public models**

Use these contracts in `web/schemas.py`:

```python
ToolRunStatus = Literal["running", "success", "failure", "stopped"]

class PublicEventRecord(BaseModel):
    id: str
    session_id: str
    sequence: int
    dedupe_key: str  # SHA-256 hex digest; never contains an internal session ID
    event: dict[str, Any]
    created_at: str

class PublicToolRun(BaseModel):
    session_id: str
    tool_call_id: str
    tool_name: str
    status: ToolRunStatus
    summary: str = ""
    error_type: str | None = None
    started_at: str
    finished_at: str | None = None

class SessionRecord(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str

class SessionDetail(SessionRecord):
    messages: list[ChatMessageRecord] = Field(default_factory=list)
    events: list[PublicEventRecord] = Field(default_factory=list)
    tool_runs: list[PublicToolRun] = Field(default_factory=list)
    last_sequence: int = 0
```

- [ ] **Step 4: Implement an explicit development schema generation**

Use a `web_schema(singleton, generation)` table with generation `agentscope-native-events-v1`. On mismatch, drop only this whitelist before recreating it:

```python
WEB_CONTROL_TABLES = (
    "human_decision_consumptions",
    "public_tool_runs",
    "public_events",
    "agentscope_sessions",
    "messages",
    "sessions",
    "web_schema",
)
```

Create `public_events` with `UNIQUE(session_id, sequence)` and `UNIQUE(session_id, dedupe_key)`. Create `public_tool_runs` with primary key `(session_id, tool_call_id)` and a status check over the four public states.

- [ ] **Step 5: Implement transactional event and tool-ledger methods**

Use `BEGIN IMMEDIATE` for sequence allocation and conditional terminal updates:

```python
cursor = connection.execute(
    """UPDATE public_tool_runs
       SET status = ?, summary = ?, error_type = ?, finished_at = ?
       WHERE session_id = ? AND tool_call_id = ? AND status = 'running'""",
    (status, summary, error_type, _now(), session_id, tool_call_id),
)
return record if cursor.rowcount == 1 else None
```

`stop_open_tool_runs` performs the same conditional transition to `stopped` and returns the rows it changed.

- [ ] **Step 6: Run the store tests**

Run: `pytest tests/test_web_session_store.py -v`

Expected: PASS, including reset safety, dedupe, monotonic sequence, and first-terminal-wins.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/web/schemas.py src/vla_data_juicer_agents/web/session_store.py tests/test_web_session_store.py
git commit -m "feat: persist public AgentScope events and tool runs"
```

---

### Task 3: Project AgentScope-native replies and capture real tool outcomes inside ToolOffload

**Files:**
- Create: `src/vla_data_juicer_agents/runtime/datapilot_projection.py`
- Create: `tests/test_datapilot_projection.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py:2422-2497`
- Modify: `tests/test_web_agentscope_session.py`

**Interfaces:**
- Consumes: `AgentEvent` values from `on_reply`, real `ToolResponse` values from `on_acting`, and a runtime sink.
- Produces:
  - `DataPilotReplyProjectionMiddleware(session_id, sink)`
  - `DataPilotToolOutcomeMiddleware(session_id, sink)`
  - sink methods `project_agent_event`, `start_public_tool`, and `finish_public_tool`.

- [ ] **Step 1: Write middleware contract tests**

Cover sanitization and filtering:

```python
async def test_reply_projection_persists_before_yield_and_hides_internal_identity():
    sink = RecordingSink()
    middleware = DataPilotReplyProjectionMiddleware("internal-nav-session", sink)
    yielded = [event async for event in middleware.on_reply(agent, {}, handler)]
    assert yielded == source_events
    assert sink.events[0]["name"] == "DataPilot"
    assert "navigation-data-agent" not in json.dumps(sink.events)
```

Cover real background failure versus synthetic success by composing AgentScope's actual `ToolOffloadMiddleware` outside the DataPilot middleware:

```python
chain = [tool_offload_middleware, datapilot_tool_outcome_middleware]
items = [item async for item in run_acting_chain(chain, delayed_failed_tool)]
assert items[-1].state is ToolResultState.SUCCESS  # AgentScope placeholder
assert sink.terminals == []
await background_completion
assert sink.terminals == [("call-1", "failure")]
```

Also test normal success, normal `ok=false`, exception, and that a later terminal write is ignored by the store.

- [ ] **Step 2: Run tests and confirm middleware classes are missing**

Run: `pytest tests/test_datapilot_projection.py -v`

Expected: FAIL on import of `datapilot_projection`.

- [ ] **Step 3: Implement reply projection**

The middleware must persist before yielding and suppress AgentScope `TOOL_RESULT_*` events, because public tool terminal events come from the inner outcome middleware:

```python
SUPPRESSED_TOOL_RESULT_EVENTS = {
    "TOOL_RESULT_START",
    "TOOL_RESULT_TEXT_DELTA",
    "TOOL_RESULT_END",
}

async def on_reply(self, agent, input_kwargs, next_handler):
    reply_id = ""
    ordinal = 0
    async for event in next_handler(**input_kwargs):
        raw = event.model_dump(mode="json")
        event_type = _event_type(raw)
        if event_type == "REPLY_START":
            reply_id = str(raw.get("reply_id", ""))
            ordinal = 0
        if event_type not in SUPPRESSED_TOOL_RESULT_EVENTS:
            public = sanitize_agent_event(raw, public_name="DataPilot")
            identity = f"{self._session_id}:{reply_id}:{ordinal}"
            await self._sink.project_agent_event(
                self._session_id,
                dedupe_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                event=public,
            )
            ordinal += 1
        yield event
```

Sanitization removes internal session/agent fields and replaces all event `name` values with `DataPilot`; it preserves AgentScope event `type`, reply/block/tool-call IDs, deltas, states, and timestamps required by `appendEvent`. The public `dedupe_key` is a one-way SHA-256 digest, not the unhashed identity string.

- [ ] **Step 4: Implement the real outcome middleware**

Because AgentScope installs built-ins before extras, this middleware is inside `ToolOffloadMiddleware` and sees the real response drained in the background:

```python
async def on_acting(self, agent, input_kwargs, next_handler):
    tool_call = input_kwargs["tool_call"]
    await self._sink.start_public_tool(
        self._session_id,
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
    )
    async for item in next_handler(**input_kwargs):
        if isinstance(item, ToolResponse):
            status, summary, error_type = classify_real_tool_response(item)
            await self._sink.finish_public_tool(
                self._session_id,
                tool_call_id=tool_call.id,
                status=status,
                summary=summary,
                error_type=error_type,
            )
        yield item
```

`classify_real_tool_response` returns success only when AgentScope state is success and decoded structured content does not contain `ok: false`; all other real outcomes are failure. It never returns `stopped`.

- [ ] **Step 5: Wire both middlewares for router and Navigation runs**

Return the reply and outcome middleware for every agent, then append `NavigationToolSurfaceMiddleware` only for the Navigation agent:

```python
middlewares = [
    DataPilotReplyProjectionMiddleware(session_id, runtime),
    DataPilotToolOutcomeMiddleware(session_id, runtime),
]
if agent_id == config.navigation_agent_id:
    middlewares.append(NavigationToolSurfaceMiddleware(...))
return middlewares
```

Bind the `WebSessionStore` and broadcaster through `runtime.set_web_transport(store, publish_callback)` during Web app construction.

- [ ] **Step 6: Verify middleware behavior and existing system-managed groups**

Run: `pytest tests/test_datapilot_projection.py tests/test_navigation_tool_surface_middleware.py tests/test_navigation_chat_service_tool_groups.py -v`

Expected: PASS; the test must prove actual background `ok=false` wins over the synthetic success.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/runtime/datapilot_projection.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_datapilot_projection.py tests/test_web_agentscope_session.py
git commit -m "feat: project AgentScope replies and real tool outcomes"
```

---

### Task 4: Replace turn-scoped forwarding and WebSocket with replayable SSE

**Files:**
- Create: `src/vla_data_juicer_agents/web/sse.py`
- Create: `tests/test_web_sse.py`
- Modify: `src/vla_data_juicer_agents/web/event_stream.py`
- Modify: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `src/vla_data_juicer_agents/web/app.py:40-250`
- Modify: `tests/test_web_api.py`
- Modify: `tests/test_web_event_stream.py`
- Modify: `tests/test_web_agentscope_session.py`

**Interfaces:**
- Consumes: `WebSessionStore.list_public_events(after_sequence)` and persisted records broadcast by `SessionEventBus`.
- Produces: `GET /api/sessions/{session_id}/stream?after_sequence=N` as `text/event-stream`.

- [ ] **Step 1: Write SSE replay/live race tests**

Test that subscription is established before replay so an event committed during replay is not lost:

```python
async with stream_session_events(store, bus, session.id, after_sequence=0) as events:
    first = await anext(events)
    await persist_and_publish(second_event)
    second = await anext(events)
assert [first.sequence, second.sequence] == [1, 2]
```

Add API tests for `Content-Type: text/event-stream`, `after_sequence`, heartbeat comments, 404, and two sessions not receiving each other's events.

- [ ] **Step 2: Run the SSE tests and confirm the endpoint is absent**

Run: `pytest tests/test_web_sse.py tests/test_web_api.py -v`

Expected: FAIL because the route is still a WebSocket and no replay cursor exists.

- [ ] **Step 3: Implement replay-then-live streaming**

In `web/sse.py`, subscribe first, replay committed rows, and discard live duplicates by sequence:

```python
async def iter_sse(store, bus, session_id, after_sequence):
    last = after_sequence
    async with bus.subscribe(session_id) as queue:
        for record in store.list_public_events(session_id, after_sequence=last):
            last = record.sequence
            yield encode_data(record.model_dump(mode="json"))
        while True:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=15.0)
            except TimeoutError:
                yield b": heartbeat\n\n"
                continue
            if record.sequence > last:
                last = record.sequence
                yield encode_data(record.model_dump(mode="json"))
```

- [ ] **Step 4: Remove all AgentScope per-turn forwarding calls**

Delete `forward_events_until_idle`, its locks, `_runtime_subscription_key`, calls from turn/HITL routes, and the WebSocket-connect forwarder. `submit_turn` remains fire-and-forget and returns the accepted turn ID.

- [ ] **Step 5: Add the SSE route**

```python
@app.get("/api/sessions/{session_id}/stream")
async def session_stream(session_id: str, after_sequence: int = 0):
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return StreamingResponse(
        iter_sse(store, bus, session_id, after_sequence),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 6: Verify no turn-scoped AgentScope subscriber remains**

Run: `rg -n "forward_events_until_idle|subscribe_web_session_events|@app.websocket" src/vla_data_juicer_agents/web src/vla_data_juicer_agents/runtime`

Expected: no production matches. The old runtime subscriber and cursor helpers can be removed after their remaining tests are replaced by projection tests.

Run: `pytest tests/test_web_sse.py tests/test_web_api.py tests/test_web_event_stream.py tests/test_web_agentscope_session.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/web src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_web_sse.py tests/test_web_api.py tests/test_web_event_stream.py tests/test_web_agentscope_session.py
git commit -m "feat: stream durable session events over SSE"
```

---

### Task 5: Make every saved session resumable and implement artifact-safe deletion

**Files:**
- Modify: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `src/vla_data_juicer_agents/web/app.py`
- Modify: `src/vla_data_juicer_agents/web/session_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/services.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `tests/test_web_api.py`
- Modify: `tests/test_web_session_store.py`
- Modify: `tests/test_navigation_task_store.py`

**Interfaces:**
- Consumes: all internal AgentScope mappings for one public session.
- Produces:
  - `AgentScopeRuntime.delete_web_session(web_session_id: str) -> bool`
  - `NavigationServices.delete_control_state_for_web_session(web_session_id: str) -> list[str]`
  - `DELETE /api/sessions/{session_id}` returning 204.

- [ ] **Step 1: Write resume and deletion safety tests**

Resume test:

```python
session = await manager.create_session("first")
await manager.submit_turn(session.id, "first")
recreated_manager = AgentScopeWebSessionManager(store=WebSessionStore(db), runtime=restarted_runtime)
await recreated_manager.submit_turn(session.id, "continue")
assert restarted_runtime.calls[-1].web_session_id == session.id
```

Deletion test creates raw/sync/finish artifacts plus Navigation control state, deletes the session, and asserts only the control state is gone:

```python
assert client.delete(f"/api/sessions/{session_id}").status_code == 204
assert store.get_session(session_id) is None
assert navigation_store.find_by_web_session(session_id) == []
assert raw_artifact.read_bytes() == b"raw"
assert sync_artifact.read_bytes() == b"sync"
assert finish_artifact.read_bytes() == b"finish"
```

- [ ] **Step 2: Run tests and confirm delete/resume gaps**

Run: `pytest tests/test_web_api.py tests/test_navigation_task_store.py -v`

Expected: FAIL because delete is not exposed and Navigation control-state deletion is absent.

- [ ] **Step 3: Remove historical/read-only session behavior**

Delete `mark_historical` and all `status` branching. `submit_turn` checks only that the public session exists; mapping restoration continues to use the active internal mapping persisted for that public ID.

- [ ] **Step 4: Add explicit Navigation control-state deletion**

Delete rows in foreign-key-safe order for task IDs owned by `created_by_web_session_id`, then delete only their dedicated evidence directories:

```python
CONTROL_CHILD_TABLES = (
    "navigation_step_result_outbox",
    "navigation_human_decision_handoffs",
    "navigation_evidence",
    "navigation_plan_submission_attempts",
    "navigation_task_steps",
    "navigation_plans",
    "navigation_observation_revisions",
)
```

The service may remove `workspace_root/navigation-evidence/<task_id>` but must never receive or derive a path under `NavigationSettings.raw_data_root`, `clip_data_root`, or `finish_data_root`.

- [ ] **Step 5: Delete every mapped AgentScope session through 2.0.4 SessionService**

```python
for mapping in store.list_agentscope_session_mappings(web_session_id):
    await self.app.state.session_service.delete_session(
        self.config.user_id,
        mapping.agent_id,
        mapping.agentscope_session_id,
    )
self._navigation_services().delete_control_state_for_web_session(web_session_id)
store.delete_session(web_session_id)
```

The repository deletion of public rows runs only after AgentScope cancellation/deletion succeeds; repeated DELETE is a 404.

- [ ] **Step 6: Verify deletion safety and session resume**

Run: `pytest tests/test_web_api.py tests/test_web_session_store.py tests/test_navigation_task_store.py tests/test_web_agentscope_session.py -v`

Expected: PASS and artifact bytes remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/web src/vla_data_juicer_agents/navigation/task_store.py src/vla_data_juicer_agents/navigation/services.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_web_api.py tests/test_web_session_store.py tests/test_navigation_task_store.py tests/test_web_agentscope_session.py
git commit -m "feat: resume and safely delete DataPilot sessions"
```

---

### Task 6: Implement explicit stop across chat runs, HITL, background tools, and public cards

**Files:**
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py:322-356`
- Modify: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `src/vla_data_juicer_agents/web/schemas.py`
- Modify: `tests/test_web_agentscope_session.py`
- Modify: `tests/test_web_api.py`
- Modify: `tests/test_cancellation.py`

**Interfaces:**
- Consumes: AgentScope 2.0.4 `ChatService.interrupt`, background registry keys, task cancel channel, and local `CancellationContext`.
- Produces: `InterruptResponse(interrupted: bool, stopped_tool_call_ids: list[str])` and durable `stopped` tool terminal events.

- [ ] **Step 1: Write interruption tests**

Cover active chat, HITL-parked chat, offloaded background tool, idempotent idle interrupt, and same-session continuation. Assert an explicit user stop produces `stopped`, while an unrelated cancellation produces `failure`.

```python
result = await runtime.interrupt_web_session(web_session_id=web_id)
assert result.stopped_tool_call_ids == ["call-1"]
assert chat_service.interrupt_calls == [(user_id, nav_sid, nav_agent_id)]
assert published_task_cancels == ["bg-task-1"]
assert store.get_tool_run(web_id, "call-1").status == "stopped"
```

- [ ] **Step 2: Run tests and confirm the old hard-cancel behavior fails**

Run: `pytest tests/test_web_agentscope_session.py tests/test_web_api.py tests/test_cancellation.py -v`

Expected: FAIL because current code publishes a session cancel directly, does not enumerate background tasks, and has no stopped tool terminal.

- [ ] **Step 3: Use official 2.0.4 chat interruption for every mapped session**

```python
for mapping in mappings:
    await self.app.state.chat_service.interrupt(
        self.config.user_id,
        mapping.agentscope_session_id,
        mapping.agent_id,
    )
```

Cancel the local `CancellationContext` first so subprocess polling reacts immediately.

- [ ] **Step 4: Cancel registered background tools cross-process**

```python
tasks = await self.message_bus.registry_getall(
    MessageBusKeys.bg_tasks(mapping.agentscope_session_id),
)
for task_id in tasks:
    await self.message_bus.publish(
        MessageBusKeys.task_cancel_channel(),
        {"task_id": task_id},
    )
```

- [ ] **Step 5: Terminalize open public tools as stopped**

Call `store.stop_open_tool_runs(web_session_id)` only in this explicit user-stop path. For each changed row, append and broadcast one AgentScope `CustomEvent`:

```python
CustomEvent(
    name="datapilot_tool_terminal",
    value={"tool_call_id": row.tool_call_id, "status": "stopped", "summary": "已由用户停止"},
)
```

Late real outcomes are ignored by the conditional tool-ledger update.

- [ ] **Step 6: Verify interrupt and continuation behavior**

Run: `pytest tests/test_web_agentscope_session.py tests/test_web_api.py tests/test_cancellation.py -v`

Expected: PASS; no open tool card remains running, and a subsequent turn on the same public session is accepted after `REPLY_END`.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/web tests/test_web_agentscope_session.py tests/test_web_api.py tests/test_cancellation.py
git commit -m "feat: stop AgentScope runs and background tools"
```

---

### Task 7: Preserve failed execution state, expose bounded result details, and block unsafe replans

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/plan_execution.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/plan_models.py`
- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/tool_groups.py`
- Modify: `tests/test_navigation_plan_execution.py`
- Modify: `tests/test_navigation_plan_store.py`
- Modify: `tests/test_navigation_agent_tools.py`
- Modify: `tests/test_navigation_tool_groups.py`

**Interfaces:**
- Consumes: the current failed step's durable `result_ref` and full execution evidence.
- Produces:
  - `SideEffectState = Literal["not_started", "completed", "partial_or_unknown"]`
  - activity `failed_recovery`
  - `read_navigation_step_result_tool(plan_id, step_id, fields=None, cursor=0, limit=50)`.

- [ ] **Step 1: Write failed-recovery classification tests**

```python
snapshot = repo.read_execution_snapshot(task_id=task.task_id, ...)
assert snapshot.activity == "failed_recovery"
assert snapshot.current["step"]["result_summary"]["side_effect_state"] == "partial_or_unknown"
```

Add tests proving a precondition rejection is `not_started`, a real successful tool is `completed`, and any invoked failed/stopped extract-sync is `partial_or_unknown` even when `sync_data` exists.

- [ ] **Step 2: Write result-reader authorization and pagination tests**

```python
result = call_tool(
    tools["read_navigation_step_result_tool"],
    plan_id=plan.plan_id,
    step_id="sync",
    fields=["commands", "details"],
    cursor=0,
    limit=1,
)
assert result["data"]["commands"][0]["return_code"] == 1
assert "stderr" in result["data"]["commands"][0]
```

Wrong task/session/plan/step must return a bounded authorization error without exposing a `result_ref`.

- [ ] **Step 3: Run tests and confirm current planning fallback**

Run: `pytest tests/test_navigation_plan_execution.py tests/test_navigation_plan_store.py tests/test_navigation_agent_tools.py tests/test_navigation_tool_groups.py -v`

Expected: FAIL because failed steps currently resolve to `planning` and there is no plan-bound result reader.

- [ ] **Step 4: Persist conservative side-effect state in result summaries**

Use this rule in `plan_execution.py`:

```python
def _side_effect_state(*, invoked: bool, ok: bool) -> str:
    if not invoked:
        return "not_started"
    return "completed" if ok else "partial_or_unknown"
```

Gate/precondition failures record `invoked=False`. Once the processing function has been entered, every non-success result is conservatively `partial_or_unknown`; directory existence never upgrades it.

- [ ] **Step 5: Return `failed_recovery` from the atomic execution snapshot**

```python
if handoff is not None and handoff.status == "recovery_required":
    activity = "recovery_required"
elif current_status in {"failed", "needs_replan"}:
    activity = "failed_recovery"
elif current_status in {"pending", "running", "waiting_user"}:
    activity = "execution"
else:
    activity = "planning"
```

- [ ] **Step 6: Add the plan-bound result reader**

Authorize through a fresh `read_execution_snapshot`, require the requested plan and step to be its current failed step, resolve `result_ref` server-side, and call the existing evidence store:

```python
return services.evidence_store.read(
    snapshot.task.task_id,
    current_step.result_ref,
    fields=fields,
    cursor=cursor,
    limit=limit,
)
```

Classify this tool in `NAVIGATION_DIAGNOSTICS`.

- [ ] **Step 7: Expose a safe failed-recovery surface**

Always expose evidence read, artifact checks, execution state, and diagnostics. Build plan-authoring tools only when the current summary says `not_started`; for `partial_or_unknown`, create an empty plan-authoring group and expose no execution actions. Assert the exact tool-name set in tests so prompt behavior cannot re-enable the side effect.

- [ ] **Step 8: Verify no failed extract-sync can be automatically re-invoked**

Run: `pytest tests/test_navigation_plan_execution.py::test_failed_step_is_recorded_exactly_once_and_duplicate_does_not_reinvoke tests/test_navigation_plan_execution.py::test_failed_step_does_not_infer_artifact_state_and_exposes_no_fresh_execution_tools tests/test_navigation_agent_tools.py tests/test_navigation_tool_groups.py tests/test_navigation_plan_store.py -v`

Expected: PASS; failure details are readable while action tools remain absent for `partial_or_unknown`.

- [ ] **Step 9: Commit**

```bash
git add src/vla_data_juicer_agents/navigation tests/test_navigation_plan_execution.py tests/test_navigation_plan_store.py tests/test_navigation_agent_tools.py tests/test_navigation_tool_groups.py
git commit -m "feat: add bounded failed-step recovery state"
```

---

### Task 8: Replace the custom frontend event reducer with AgentScope SDK messages

**Files:**
- Create: `frontend/src/store/agentConversation.ts`
- Create: `frontend/src/store/agentConversation.test.ts`
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/store/datapilotStore.ts`
- Delete after migration: `frontend/src/store/eventReducer.ts`
- Delete after migration: `frontend/src/store/eventReducer.test.ts`

**Interfaces:**
- Consumes: `PublicEventEnvelope { sequence, event: AgentEvent }`, stored user messages, and public tool runs.
- Produces: `AgentConversationState` with SDK `Msg[]`, `ReplyPhase`, `lastSequence`, tool-run map, and pending DataPilot HITL.

- [ ] **Step 1: Write SDK reducer tests**

Cover `REPLY_START -> TEXT_BLOCK_* -> REPLY_END`, duplicate sequence rejection, reconnect continuation, a second wakeup reply after the first `REPLY_END`, tool success/failure/stopped custom events, and interrupt cleanup.

```typescript
const state = createAgentConversation();
applyPublicEvent(state, envelope(1, replyStart("reply-1")));
applyPublicEvent(state, envelope(2, textStart("reply-1", "block-1")));
applyPublicEvent(state, envelope(3, textDelta("reply-1", "block-1", "完成")));
applyPublicEvent(state, envelope(4, replyEnd("reply-1")));
expect(state.messages[0].content[0]).toMatchObject({ type: "text", text: "完成" });
expect(state.phase).toBe("idle");
```

- [ ] **Step 2: Run Vitest and confirm the new state module is absent**

Run: `npm --prefix frontend test -- src/store/agentConversation.test.ts`

Expected: FAIL on missing module.

- [ ] **Step 3: Define SDK-backed API types**

```typescript
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { Msg } from "@agentscope-ai/agentscope/message";

export interface PublicEventEnvelope {
  id: string;
  session_id: string;
  sequence: number;
  dedupe_key: string; // opaque SHA-256 digest
  event: AgentEvent;
  created_at: string;
}

export type PublicToolStatus = "running" | "success" | "failure" | "stopped";
```

- [ ] **Step 4: Implement the AgentScope message reducer**

Use SDK constructors and `appendEvent`:

```typescript
if (event.type === EventType.REPLY_START) {
  const start = event as ReplyStartEvent;
  const msg = AssistantMsg({ id: start.reply_id, name: "DataPilot", content: [] });
  state.messages.push(msg);
  state.currentReplyId = start.reply_id;
  state.phase = "streaming";
} else if (event.type === EventType.REPLY_END) {
  const current = currentReply(state);
  if (current) appendEvent(current, event);
  state.currentReplyId = null;
  state.phase = "idle";
} else if (event.type === EventType.CUSTOM) {
  applyDataPilotProjection(state, event as CustomEvent);
} else {
  const current = currentReply(state);
  if (current) appendEvent(current, event);
}
state.lastSequence = envelope.sequence;
```

Use `UserMsg` to convert persisted public user messages into SDK messages. Custom projections update only `toolRuns` and `pendingHumanDecision`; they do not replace native reply reduction.

- [ ] **Step 5: Move Zustand to one writable session mode**

Use `"draft_new_session" | "active_session"`; remove `history_session`, `restoreHistory`, `activeAgents`, `activeTools`, and custom timeline dedupe state. `restoreSession` rebuilds from snapshot messages/events/tool runs, then accepts live envelopes with larger sequences.

- [ ] **Step 6: Verify reducer and store tests**

Run: `npm --prefix frontend test -- src/store/agentConversation.test.ts src/app/App.test.tsx`

Expected: PASS.

- [ ] **Step 7: Delete the old reducer and commit**

```bash
git add frontend/src/api/types.ts frontend/src/store/agentConversation.ts frontend/src/store/agentConversation.test.ts frontend/src/store/datapilotStore.ts
git rm frontend/src/store/eventReducer.ts frontend/src/store/eventReducer.test.ts
git commit -m "feat: reduce DataPilot replies with AgentScope SDK"
```

---

### Task 9: Add the selected-session SSE client, resume/delete UI, and three tool terminal states

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/api/client.test.ts`
- Modify: `frontend/src/components/datapilot/DataPilotWindow.tsx`
- Modify: `frontend/src/components/datapilot/SessionHistoryPanel.tsx`
- Modify: `frontend/src/components/datapilot/MessageList.tsx`
- Modify: `frontend/src/components/datapilot/AgentRunSummary.tsx`
- Modify: `frontend/src/components/datapilot/Composer.tsx`
- Modify: `frontend/src/app/App.test.tsx`

**Interfaces:**
- Consumes: snapshot `last_sequence` and `GET /stream?after_sequence=`.
- Produces: one abortable stream for the selected session, writable restored sessions, delete action, and correct tool-card labels.

- [ ] **Step 1: Write fetch-SSE and selected-session tests**

Mock `fetch` with chunked SSE frames and assert parsing across chunk boundaries, heartbeat skipping, and abort. In component tests, switch from session A to B and assert A's `AbortController` is aborted before B opens.

```typescript
for await (const envelope of streamSessionEvents("session-b", 12, signal)) {
  received.push(envelope);
}
expect(requestedUrl).toContain("after_sequence=12");
```

Add history tests: every listed session calls the same `restoreSession`; delete requires explicit click and removes only the list entry after a successful 204.

- [ ] **Step 2: Run the frontend tests and confirm WebSocket expectations fail**

Run: `npm --prefix frontend test -- src/api/client.test.ts src/app/App.test.tsx`

Expected: FAIL because `openSessionEvents` still constructs `WebSocket` and history can be read-only.

- [ ] **Step 3: Implement fetch-based SSE**

```typescript
export async function* streamSessionEvents(
  sessionId: string,
  afterSequence: number,
  signal: AbortSignal,
): AsyncGenerator<PublicEventEnvelope> {
  const response = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/stream?after_sequence=${afterSequence}`,
    { signal, headers: { Accept: "text/event-stream" } },
  );
  if (!response.ok || !response.body) throw await apiError(response);
  yield* parseSse(response.body, signal);
}
```

Add `deleteSession(sessionId)` and keep turn submission fire-and-forget.

- [ ] **Step 4: Hold exactly one selected-session stream**

Replace `socketRef` with `{ sessionId, controller }`. On close, draft, session switch, component unmount, or delete, abort the controller. Reconnect from `datapilotStore.getState().conversation.lastSequence`, not from zero.

- [ ] **Step 5: Make history selection writable and add deletion**

`handleSelectHistory` always calls `restoreSession(detail)` and enters `active_session`. Add a trash button with `aria-label="Delete session <title>"`; stop propagation so deletion does not also select the row.

- [ ] **Step 6: Render SDK messages and public tool states without internal identities**

Render message names as `You` or `DataPilot`, regardless of the internal SDK `name`. Use these exact labels:

```typescript
const toolStatusText = {
  running: "正在调用",
  success: "成功",
  failure: "失败",
  stopped: "已停止",
} as const;
```

Do not add an `已转后台` label or state.

- [ ] **Step 7: Preserve draft text while stopping**

Keep the input editable during `streaming` and `interrupting`; only the submit/stop button is disabled while the stop request is awaiting terminating `REPLY_END`. After idle, the same session accepts the draft.

- [ ] **Step 8: Verify frontend behavior and build**

Run: `npm --prefix frontend test -- src`

Run: `npm --prefix frontend run build`

Expected: all Vitest tests pass and TypeScript/Vite build succeeds.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/api frontend/src/components/datapilot frontend/src/app/App.test.tsx
git commit -m "feat: resume DataPilot sessions over selected SSE"
```

---

### Task 10: Add the P0 background-wakeup integration regression

**Files:**
- Create or extend: `tests/test_web_agentscope_background_wakeup.py`
- Modify: `tests/navigation_chat_service_harness.py`
- Modify: `tests/test_web_agentscope_session.py`

**Interfaces:**
- Consumes: actual AgentScope 2.0.4 ToolOffload middleware, fake Redis/message bus harness, public event projection, and SSE replay.
- Produces: an end-to-end regression proving browser-independent background completion.

- [ ] **Step 1: Write the failing end-to-end scenario**

The fake tool sleeps beyond a shortened ToolOffload threshold and returns `{"ok": False, "error_type": "extract_sync_failed"}`. Execute the first run with no SSE subscriber, wait for the wakeup run, then open the public stream from sequence zero.

Assert in order:

```python
assert tool_runs == [
    {"tool_call_id": call_id, "status": "failure", "error_type": "extract_sync_failed"},
]
assert assistant_reply_ids == [first_reply_id, wakeup_reply_id]
assert first_reply_id != wakeup_reply_id
assert "completed" not in public_tool_terminal_statuses
```

Also add the success variant and explicit-stop variant.

- [ ] **Step 2: Run the regression and confirm the old symptom**

Run: `pytest tests/test_web_agentscope_background_wakeup.py -v`

Expected before final wiring: FAIL because either the wakeup reply is absent from public replay or the placeholder success appears as terminal.

- [ ] **Step 3: Make only integration-level wiring corrections**

Corrections are limited to middleware registration, sink binding, event dedupe, and test harness lifecycle. Do not add sleeps/grace periods to production code and do not change AgentScope source.

- [ ] **Step 4: Verify all three terminal paths**

Run: `pytest tests/test_web_agentscope_background_wakeup.py -v`

Expected: PASS for real success, real failure, and explicit user stopped; every case includes the wakeup reply when applicable.

- [ ] **Step 5: Commit**

```bash
git add tests/test_web_agentscope_background_wakeup.py tests/navigation_chat_service_harness.py tests/test_web_agentscope_session.py src/vla_data_juicer_agents/runtime/datapilot_projection.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/web/session_store.py src/vla_data_juicer_agents/web/sse.py
git commit -m "test: cover AgentScope background wakeup delivery"
```

---

### Task 11: Remove obsolete bridge code and run the complete acceptance suite

**Files:**
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `src/vla_data_juicer_agents/web/app.py`
- Modify: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `README.md` or the repository's existing Web run documentation
- Modify: affected tests that name the removed WebSocket bridge

**Interfaces:**
- Consumes: all completed tasks.
- Produces: no production path using the custom AgentScope event subscriber/WebSocket bridge and documented reset/operation commands.

- [ ] **Step 1: Add dead-reference assertions**

Extend `tests/test_navigation_dead_references.py` or add an equivalent Web test:

```python
FORBIDDEN_WEB_REFERENCES = {
    "forward_events_until_idle",
    "subscribe_web_session_events",
    "web_session_subscription_key",
    "@app.websocket",
}
```

The scan covers production `src/vla_data_juicer_agents/web` and the obsolete runtime subscriber region, but does not ban legacy TUI's `AgentScopeEventAdapter`, which remains outside this scope.

- [ ] **Step 2: Run the dead-reference test and remove each remaining bridge reference**

Run: `pytest tests/test_navigation_dead_references.py -v`

Expected initially: FAIL with exact obsolete reference locations; remove those helpers and their cursor-only state without changing Navigation workflow adapters.

- [ ] **Step 3: Document development reset and runtime requirements**

Document:

```text
AgentScope 2.0.4 and Redis are required for the DataPilot Web runtime.
The first run after this change resets development Web session metadata.
The reset never removes VLA dataset roots or processing outputs.
Only the selected session opens /api/sessions/{id}/stream.
```

Do not document `VLA_AGENT_ENABLE_AGENTSCOPE=0` as a supported production Web mode; tests may retain isolated controller fixtures until the separate CLI/TUI cleanup project.

- [ ] **Step 4: Run focused backend acceptance**

Run: `pytest tests/test_agentscope_204_contract.py tests/test_datapilot_projection.py tests/test_web_sse.py tests/test_web_api.py tests/test_web_session_store.py tests/test_web_agentscope_session.py tests/test_web_agentscope_background_wakeup.py tests/test_cancellation.py tests/test_navigation_plan_execution.py tests/test_navigation_plan_store.py tests/test_navigation_agent_tools.py tests/test_navigation_tool_groups.py -v`

Expected: all selected tests pass with zero failures.

- [ ] **Step 5: Run the complete backend and frontend suites**

Run: `pytest -q`

Run: `npm --prefix frontend test -- src`

Run: `npm --prefix frontend run build`

Expected: all commands exit 0; pytest and Vitest report zero failures, and Vite emits the production bundle.

- [ ] **Step 6: Inspect the final diff for safety boundaries**

Run: `git diff --check`

Run: `rg -n "rmtree|unlink|remove\(" src/vla_data_juicer_agents/web src/vla_data_juicer_agents/runtime src/vla_data_juicer_agents/navigation`

Expected: any new deletion is confined to Web/control-plane tables and `navigation-evidence/<task_id>`; no new deletion call targets `raw_data_root`, `clip_data_root`, `finish_data_root`, `sync_data`, or dataset roots.

- [ ] **Step 7: Commit the cleanup and documentation**

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/web/app.py src/vla_data_juicer_agents/web/agent_session.py tests/test_navigation_dead_references.py README.md
git commit -m "refactor: remove turn-scoped DataPilot event bridge"
```

---

## Final Acceptance Checklist

- [ ] A background tool completion is persisted and replayable with no browser connected.
- [ ] A wakeup run after the first `REPLY_END` appears in the same public conversation.
- [ ] ToolOffload placeholder success never terminates a public tool card.
- [ ] Real tool `ok=true`, `ok=false`, and explicit user stop render success, failure, and stopped respectively.
- [ ] Every saved session is writable when selected; only that session owns an SSE connection.
- [ ] Deleting a session removes AgentScope and control-plane state but preserves all processing artifacts byte-for-byte.
- [ ] Interrupt ends chat/HITL/background work and the same session accepts another message afterward.
- [ ] Failed-step state and bounded command stderr are readable without carrying an opaque `result_ref`.
- [ ] A partial/unknown extract-sync result exposes no automatic action or unsafe replacement Plan path.
- [ ] Browser payloads and labels expose DataPilot only, never internal agent/session identities.
- [ ] Full pytest, Vitest, and frontend build commands pass.
