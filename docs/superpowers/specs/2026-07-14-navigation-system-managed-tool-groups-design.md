# Navigation System-Managed Tool Groups Design

## Context

The NavigationDataAgent currently receives its domain tools through AgentScope
App's `extra_agent_tools` callback. `ChatService` invokes that callback once when it
assembles an Agent for a chat run and adds the returned tools to the always-active
`basic` group. `resolve_navigation_agent_tools()` correctly returns planning tools
before an accepted Plan and execution tools after activation, but it is not called
again inside the same AgentScope ReAct reply.

A server test exposed the resulting gap. The model successfully submitted an
extract-sync Plan, but the Toolkit created at the start of the reply still contained
only planning tools. The model could not see `prepare_raw_data_tool`; it instead
called `describe_processing_action_tool` and inferred that Plan submission might
start execution automatically. The backend does not and must not execute Plan steps
automatically. An accepted Plan must be executed only when the model calls the
corresponding plan-bound tool.

AgentScope 2.0.1 already supports changing tool visibility between adjacent
reasoning iterations. `Agent._prepare_model_input()` rebuilds tool schemas from
`AgentState.tool_context.activated_groups` for every model call, and Toolkit checks
the active groups again before executing a call. The missing piece is an
application-level integration that refreshes NavigationDataAgent's groups from the
durable navigation state instead of relying on the once-per-chat
`extra_agent_tools` assembly.

## Goals

- Let a successfully accepted Plan switch the NavigationDataAgent from planning
  tools to execution tools inside the same AgentScope reply.
- Keep the model responsible for invoking every processing action; switching tool
  groups must never execute data processing automatically.
- Make SQLite navigation state, not the model or AgentScope's persisted activated
  group list, authoritative for tool availability.
- Classify navigation tools by capability and activate the necessary combination of
  groups for the current activity.
- Remove AgentScope's generic Bash, file, Task, Schedule, Team, skill, and MCP tools
  from NavigationDataAgent while leaving MainRouterAgent unchanged.
- Prevent the model from changing navigation tool authorization through
  AgentScope's `reset_tools` meta tool.
- Preserve direct CLI behavior through a flat adapter over the same grouped tool
  catalog.
- Keep current fail-closed Plan activation, immutable Plan, execution-ledger, and
  session-ownership contracts.
- Keep planning, execution, and recovery tool schemas bounded to reduce context
  pressure.

## Non-Goals

- Do not modify, fork, monkey-patch, or vendor AgentScope.
- Do not change AgentScope context compression.
- Do not add a second model session or require an extra user message after Plan
  acceptance.
- Do not put all navigation tools into the always-active `basic` group.
- Do not let code choose Plan steps, variants, business parameters, or whether a
  user wants to continue to finish processing.
- Do not implement new diagnostic tools in this change.
- Do not implement diagnostic metrics in this change.
- Do not add a special diagnostics-only workflow state.
- Do not redesign terminal execution failures into automatically rerunnable ledger
  steps. Controlled retry of an already executed and terminally failed action is a
  separate safety design.
- Do not change MainRouterAgent's generic tool behavior except for wiring the new
  NavigationDataAgent-only middleware factory.

## Authority and State Rules

The tool surface is a projection of existing durable state, not a new state
machine.

| Concern | Authority |
| --- | --- |
| Current task and session ownership | Navigation task store |
| Current observations and evidence | Observation store |
| Accepted immutable Plan | Plan repository |
| Current step and execution status | Execution ledger |
| Navigation activity | `NavigationExecutionSnapshot` |
| Model-visible navigation tools | Derived tool-surface policy |
| Cached activated group names | AgentScope `AgentState`, overwritten by policy |

Every reasoning iteration recomputes the surface from a fresh, session-authorized
snapshot. A value restored in `AgentState.tool_context.activated_groups` is only a
cache and must never override SQLite.

## Tool Classification

Each navigation tool belongs to exactly one capability group. Classification tests
must fail if a tool is missing, duplicated, or assigned to an incompatible group.

### `navigation_evidence_read`

Pure cognitive reads over bounded, task-scoped evidence:

- `list_observation_evidence_tool`
- `read_observation_evidence_tool`

### `navigation_investigation`

Tools that inspect source/runtime facts and append factual observations, but do not
choose Plan semantics or process source data:

- `inspect_navigation_raw_metadata_tool`
- `inspect_navigation_sensor_candidates_tool`
- `inspect_navigation_topic_candidates_tool`
- `inspect_navigation_runtime_assets_tool`
- `inspect_navigation_calibration_inventory_tool`
- `inspect_navigation_localization_sources_tool`

These tools are observational from the dataset perspective but write an observation
revision. They must not be conflated with pure reads merely because their
`is_read_only` flag is true.

### `navigation_artifact_checks`

Current intermediate/final product checks:

- `inspect_navigation_artifact_state_tool`
- `inspect_navigation_gridmap_artifacts_tool`

They are available during planning and execution so the model can verify product
state without receiving the full investigation surface.

### `navigation_plan_authoring`

Planning context, capability contracts, user guidance, and complete Plan
submission:

- `get_navigation_task_context_tool`
- `describe_processing_action_tool`
- `record_navigation_user_guidance_tool`
- `submit_extract_sync_plan_tool`
- `submit_finish_processing_plan_tool`

This group is never visible during execution. In particular,
`describe_processing_action_tool` cannot distract the model after an accepted Plan.

### `navigation_execution_state`

Compact reads bound to the active immutable Plan:

- `get_plan_execution_overview_tool`
- `get_current_plan_step_tool`

### `navigation_execution_actions`

Only the distinct remaining actions from the active immutable Plan, including the
plan-bound human-decision tool when it is the current action. The existing execution
builder continues to derive canonical arguments from the stored Plan. The model
supplies only `plan_id` and `step_id`.

### `navigation_diagnostics`

An initially empty extension group. It is included in every navigation surface so
future diagnostic tools become available during planning and execution without a
new phase-policy redesign. This change does not add tools, special status,
instructions, or metrics to the group.

## Surface Policy

`NavigationToolSurfacePolicy` accepts one authorized
`NavigationExecutionSnapshot` and returns immutable group definitions plus the
active group names.

### Planning

Active groups:

- `navigation_evidence_read`
- `navigation_investigation`
- `navigation_artifact_checks`
- `navigation_plan_authoring`
- `navigation_diagnostics`

Execution state and execution actions are absent, not merely inactive.

### Execution

Active groups:

- `navigation_evidence_read`
- `navigation_artifact_checks`
- `navigation_execution_state`
- `navigation_execution_actions`
- `navigation_diagnostics`

Investigation and Plan-authoring tools are absent. The execution-action group is
rebuilt from the latest ledger so completed actions disappear and the current or
remaining plan-bound actions stay visible.

### Recovery Required

Active groups:

- `navigation_evidence_read`
- `navigation_artifact_checks`
- `navigation_execution_state`
- `navigation_diagnostics`

The existing recovery contract remains fail-closed. No new recovery or diagnostic
tool is introduced here.

### Completed Plan and Reverse Transition

The transition is bidirectional. After the last ledger step completes, the Plan
repository atomically marks the accepted Plan `completed`. A completed Plan is no
longer returned as `active_plan`, so the next authorized execution snapshot has
`activity="planning"`. The middleware's post-tool synchronization must therefore:

1. remove `navigation_execution_actions` and `navigation_execution_state`;
2. restore the planning, investigation, evidence-read, artifact-check, and empty
   diagnostics extension groups; and
3. let the model inspect the newly produced artifacts and decide the next
   conversational action from the domain guidance.

Completing an extract-sync Plan does not complete the whole task attempt. The model
uses the restored planning surface to verify extract/sync products, report what
finished, ask whether the user wants to continue, and collect any missing
finish-processing inputs. Code does not automatically submit or execute a
finish-processing Plan and does not force the conversation to stop.

While the model is waiting for the user there is no active ReAct execution. When the
user later provides parameters or asks to continue in the same Web session, the
first reasoning iteration again derives the planning surface from SQLite. The model
must re-inspect mutable products as required, record user guidance, and submit one
complete finish-processing Plan. Successful acceptance switches the surface back to
execution by the same forward transition.

A completed finish-processing Plan also closes the execution groups. When its final
validation step completes, the task attempt becomes `completed`; the restored
planning/read surface lets the model report the final result without leaving stale
processing actions available.

### Retry Boundary

Tool availability follows the ledger rather than the presence of an error-shaped
tool result.

- A pre-claim error, such as a wrong `plan_id`, wrong `step_id`, unmet gate, or
  temporary busy result, does not terminalize the step. If the snapshot remains in
  execution, the same plan-bound action remains visible and the model may correct
  the call.
- An underlying action that ran and was durably finalized with `status=failed` is a
  terminal ledger step under the current contract. Recalling the wrapper must not
  execute the underlying action again; the current re-investigation and complete
  Plan replacement path remains in force.
- Retrying only staged-result finalization remains supported by the existing
  execution code and must not rerun the underlying action.

## Application Components

### Grouped Resolver

Refactor the current flat resolver into three layers:

1. `build_navigation_tool_groups(...)` builds the categorized tools for one
   authorized snapshot.
2. `resolve_navigation_tool_surface(...)` applies the surface policy and returns
   group names, definitions, and a compact mode.
3. `resolve_navigation_agent_tools(...)` remains as a compatibility adapter that
   flattens only the active groups for the direct CLI path.

There must be one tool-building implementation. Web and CLI adapters must not grow
independent tool lists or phase rules.

### `NavigationToolSurfaceMiddleware`

Add an AgentScope middleware used only for NavigationDataAgent. It owns no durable
business state and performs four functions.

#### 1. Synchronize Before Reasoning

In `on_reasoning`, before calling `next_handler`:

1. Read a fresh session-authorized execution snapshot.
2. Resolve the current tool surface.
3. Remove the Toolkit's generic AgentScope tools, skills, MCPs, and non-navigation
   groups for NavigationDataAgent.
4. Mutate the Toolkit's `tool_groups` list in place to contain an empty `basic`
   group plus the current navigation groups.
5. Replace `agent.state.tool_context.activated_groups` with the policy result.

The list is mutated in place because AgentScope's built-in meta tool retains a
reference to the list created by Toolkit. The middleware must not replace the Agent
or AgentScope session.

#### 2. Synchronize After Domain Tool Execution

In `on_acting`, forward a permitted navigation call to `next_handler`. When its
terminal `ToolResponse` is received, re-read SQLite and synchronize the surface
before yielding the terminal response.

This creates an immediate authorization barrier inside one model response. A
successful, non-concurrency-safe Plan submission activates the Plan; any later tool
call in the same response is checked against the execution surface rather than the
old planning surface. Calls that occurred before the submission retain normal
planning semantics.

The middleware never decides that a Plan succeeded by parsing model text or a
tool-result summary. It trusts only the post-call SQLite snapshot.

#### 3. Remove Model Control of Groups

AgentScope exposes `reset_tools` whenever non-basic groups exist. Navigation group
authorization is system-managed, so the middleware must:

- remove `reset_tools` from the schemas passed through `on_model_call`; and
- reject a fabricated `reset_tools` call in `on_acting` without mutating AgentState.

The model can choose among the tools the current surface exposes, but it cannot
activate a different surface.

#### 4. Fail Closed

If ownership validation, snapshot reading, group construction, or synchronization
fails, clear all NavigationDataAgent domain groups and propagate a bounded runtime
failure through the existing Web/AgentScope error path. Never retain the previously
active execution group after an uncertain refresh.

No separate diagnostic metric or fallback tool is added in this change.

### ChatService Wiring

Keep MainRouterAgent's `extra_agent_tools` behavior unchanged so it receives
`start_navigation_data_task`.

For NavigationDataAgent:

- `extra_agent_tools` returns no navigation domain tools, preventing them from being
  placed in `basic`;
- a new `extra_agent_middlewares` factory returns
  `NavigationToolSurfaceMiddleware` bound to the runtime services, Web session, and
  AgentScope session;
- `agentscope.app.create_app(...)` receives both factories.

The middleware factory returns nothing for MainRouterAgent, so router tools and
generic framework behavior remain unchanged.

## End-to-End Data Flow

```text
ChatService assembles NavigationDataAgent
  -> generic Toolkit exists temporarily
  -> first on_reasoning reads SQLite activity=planning
  -> middleware installs and activates planning groups
  -> model investigates and submits one complete JSON Plan
  -> submission validates and atomically activates the Plan
  -> on_acting re-reads SQLite activity=execution
  -> middleware replaces planning groups with execution groups
  -> AgentScope enters its next reasoning iteration
  -> _prepare_model_input obtains execution schemas
  -> model reads current Plan step
  -> model calls the matching plan-bound processing tool
  -> the last extract-sync step completes and the Plan becomes completed
  -> on_acting re-reads SQLite activity=planning
  -> middleware removes execution groups and restores planning/read groups
  -> model verifies products and asks whether the user wants to continue
  -> a later user answer starts from the planning surface
  -> a complete finish-processing Plan is authored and accepted
  -> the same forward transition activates its execution groups
```

Only the model's final plan-bound call executes processing. The group transition
does not call `prepare_raw_data`, any other processing action, or a deterministic
workflow runner.

## Prompt Changes

Keep the compact domain guidance and reinforce only the control contract:

- after a successful complete-Plan submission, continue the same reply;
- read the accepted Plan's compact execution state;
- invoke the matching plan-bound tool;
- never assume Plan submission starts processing;
- treat unavailable tools as a phase boundary rather than searching for a generic
  shell/file workaround.

Do not add tool inventories to the prompt. The active Toolkit schemas are the
inventory.

## Verification

### Classification and Policy Tests

- Every navigation tool belongs to exactly one group.
- Planning, execution, recovery, and diagnostics extension surfaces have exact
  expected group and tool-name sets.
- `navigation_diagnostics` exists and is empty.
- NavigationDataAgent surfaces contain no Bash, Read/Write, Task, Schedule, Team,
  skill, MCP, or `reset_tools` schema.
- MainRouterAgent remains unchanged.

### Middleware Tests

- Every reasoning iteration reads a fresh authorized snapshot.
- Persisted stale activated groups are overwritten.
- Toolkit group mutation preserves AgentScope's expected list identity.
- A successful Plan submission switches the surface before a later tool call.
- Rejected, stale-revision, or activation-failed submissions remain in planning.
- A fabricated `reset_tools` call is rejected.
- A synchronization error leaves no navigation execution action available.
- Non-terminal execution call errors keep the ledger-authorized action visible.
- Terminally failed actions are not made rerunnable by the surface policy.
- Completing the final extract-sync step switches back to the planning surface in
  the same reply.
- A later same-session user answer begins with planning and can submit a complete
  finish-processing Plan, which switches back to execution.
- Completing the final finish-processing step removes all execution actions.

### Real ChatService Regression

Use AgentScope 2.0.1's real `ChatService`, Agent, Toolkit, and ReAct loop with a
scripted model. One user message must produce:

1. a planning model call whose schemas contain both complete-Plan submission tools
   and exclude processing actions;
2. a successful complete Plan submission;
3. a second model call in the same `reply_stream` whose schemas contain
   `get_current_plan_step_tool` and the correct plan-bound processing action, and
   exclude `describe_processing_action_tool` and both submission tools;
4. a model-authored call to that processing action.

Assert that:

- no user continuation message or manually invoked test `refresh_tools()` occurs;
- code does not call the processing action before the model tool call;
- the processing action executes exactly once;
- a failed submission never exposes execution tools;
- a process restart or stale AgentScope activated-group cache still reconstructs
  the surface from SQLite.
- the final extract-sync action causes a second reverse transition in the same
  reply, after which the model sees artifact checks and Plan-authoring tools but no
  execution action;
- a later user message with finish-processing parameters starts from that planning
  surface and can activate a new finish-processing Plan.

The existing direct model-flow test may continue to flatten and refresh tools for
the direct CLI adapter, but it is not sufficient evidence for the Web/ChatService
path.

### Context-Budget Regression

Measure the planning, execution, and recovery schema sets using the existing
context-budget tests. Assert that NavigationDataAgent receives no generic AgentScope
tool schemas and that execution excludes investigation and Plan-authoring schemas.
Keep the existing real-server acceptance target of completing the routine flow
without AgentScope context compaction.

No new diagnostic metrics are implemented by this design.

## Expected Code Changes

- Add a grouped navigation tool catalog and surface-policy module under
  `src/vla_data_juicer_agents/navigation/`.
- Add `NavigationToolSurfaceMiddleware` under
  `src/vla_data_juicer_agents/runtime/`.
- Refactor `navigation/agent_tools.py` to consume the grouped catalog while keeping
  the flat direct adapter.
- Update `runtime/agentscope_runtime.py` to wire both extra-tool and
  extra-middleware factories.
- Update the compact NavigationDataAgent prompt/guidance for same-reply execution
  after Plan acceptance.
- Add policy, middleware, production-real ChatService, restart, router-isolation,
  and context-budget tests.

No AgentScope package files, SQLite schema, diagnostic tool, diagnostic metric, or
processing implementation is changed.
