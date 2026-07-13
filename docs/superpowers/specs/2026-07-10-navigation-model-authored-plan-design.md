# Navigation Model-Authored Plan and Bounded Context Design

## Context

A real server-side navigation-processing test exposed two related failures in the
current planning design.

First, `finalize_finish_processing_plan_tool` repeatedly failed even though the
read-only investigation tools had already produced a complete processing profile.
The current flow asks the model to copy selected fields from complete tool results
into a mutable `WorkflowPlanDraftState`, merge partial patches over several turns,
and then ask code to rebuild and validate a plan. The profile model duplicates the
same semantics at several levels. A shallow completeness check can therefore report
no missing fields while nested Pydantic validation still fails. Failed updates also
leave a partially mutated draft and append increasingly large validation results.

Second, the same server session exceeded AgentScope's compact trigger. Repeated
draft snapshots returned all phase schemas, the full accumulated profile, the
finalized plan when present, observation history, and validation errors. Most draft
tool results exceeded the configured `tool_result_limit=6000`. The session retained
roughly 110,000 input tokens before compaction, despite the navigation workflow
itself having only a small number of meaningful decisions and processing steps.
Compaction then lost semantic state: durable task state had already advanced to
`finish_processing`, while the model later described the task as still waiting for
`scene_mode`.

The useful pattern from
[Datus-agent](https://github.com/Datus-ai/Datus-agent) is not deterministic semantic
planning. Datus lets the model author the plan artifact, stores only small control
state in the conversation, exposes overview/detail access separately, and persists
execution progress outside the model context. This design applies that ownership
boundary while retaining strict JSON and domain validation required by navigation
processing.

A later server test exposed a separate task-entry design error. A new Web session
could not delegate `20270623/20260623_145550` to the NavigationDataAgent because an
old `completed` task for the same data was permanently owned by another Web session.
The main router received the ownership error but twice told the user that processing
had started. This happened because the implementation treated one date/segment scope
as a cross-session durable workflow identity. That is not the required behavior.
Continuing from existing data products means inspecting those products again in a new
task attempt; it does not mean restoring an old Web session, AgentScope context,
task, observation history, or plan.

## Goals

- Make the model the sole author of navigation plan semantics.
- Let code record observed facts without choosing policies, steps, variants, or
  business parameters.
- Require the model to submit one complete, phase-specific JSON plan in a single
  tool call.
- Validate and persist a plan atomically; an invalid candidate must never mutate the
  active plan or execution state.
- Remove duplicated profile fields and eliminate the mutable patch/finalize loop.
- Separate observed facts, immutable plan semantics, and mutable execution status.
- Give the model read-only tools to inspect detailed evidence and processing
  capabilities on demand.
- Keep routine tool results bounded and keep the standard navigation workflow below
  AgentScope's compact trigger.
- Let the main router distinguish ordinary conversation from a concrete navigation
  processing request and delegate only the latter to the NavigationDataAgent.
- Require the NavigationDataAgent to investigate current raw, intermediate, and
  final products before it chooses a processing stage or authors a plan, including
  when the user claims that an earlier stage has already completed.
- Keep same-session plan and execution progress durable across compaction, process
  restart, cancellation, and later continuation without making task state an
  authoritative description of current filesystem products.
- Start a fresh task attempt in every new Web session instead of transferring or
  restoring an old task attempt.
- Preserve the two major business stages: after extract/sync, the model asks whether
  the user wants to continue and collects the finish-processing inputs before it
  plans the second stage.
- Delete the superseded draft/finalize implementation in the same change so the
  repository contains one planning design, not parallel old and new paths.

## Non-Goals

- Do not modify AgentScope's compression implementation.
- Do not rotate AgentScope sub-sessions at phase boundaries in this iteration.
- Do not let code select a time-sync reference, localization policy, gridmap source,
  calibration strategy, processing step, or stage variant.
- Do not adopt Datus's Markdown plan format; navigation plans remain strict JSON.
- Do not add a distributed workflow engine.
- Do not implement cross-Web-session task, plan, observation, or AgentScope-context
  recovery.
- Do not let entry code inspect products automatically, choose the current stage, or
  force the model to stop or continue at a stage boundary.
- Do not treat persisted task phase/status or a user's description of prior work as
  proof that an intermediate or final product currently exists.
- Do not automatically convert an ambiguous legacy profile draft into a new plan.
- Do not preserve obsolete planning tool aliases or deprecated implementation stubs.

## Core Ownership Rules

The system has three authoritative state categories:

| State | Meaning | Owner | Mutability |
| --- | --- | --- | --- |
| Observation Store | Facts returned by model-invoked investigation tools | Model chooses calls; tools record facts | Append by revision |
| Plan Repository | What the model decided to do | Model, validated by code | Immutable revisions |
| Execution Ledger | What has executed and what comes next | Execution runtime | Mutable state machine |

The model context is not an authoritative state store.

Code may derive non-semantic metadata such as identifiers, timestamps, schema
versions, output declarations from a selected capability, and step runtime status.
Code must not derive a semantic choice that the model was expected to make.

For example, code may calculate topic frequency, timestamp jitter, time coverage,
and missing-message ratios. It may expose candidate sensor roles with evidence and
confidence. The model chooses the reference sensor and time-sync method. The
validator only checks that the selected sensor exists and that the selected method
and tolerance are supported.

## Architecture

### Router, Session, and Task-Attempt Boundaries

The main router owns conversation triage. It answers ordinary conversation and
capability questions itself. When the user supplies a concrete navigation-processing
target, it calls `start_navigation_data_task` and delegates to the
NavigationDataAgent. It does not inspect navigation products, select a processing
stage, or claim that delegation succeeded unless the tool returns `ok: true` and
`started: true`.

A `NavigationTask` is one task attempt, not the global lifecycle of one dataset
scope. The boundaries are:

- the first delegation in a Web session creates a task attempt bound to that Web and
  AgentScope session;
- later continuation in the same Web/AgentScope session may reuse that attempt's
  plan and execution ledger, but the model re-inspects facts that may have changed;
- delegation from a new Web session creates a new task attempt and does not load,
  transfer, or mutate an earlier session's task, plan, observations, or conversation;
- completed, failed, waiting-user, or planning attempts for the same date/segments
  do not block a new task attempt;
- an actual data-writing action for the same target holds a narrow resource lock.
  A conflicting execution returns `navigation_data_busy`; it does not trigger
  cross-session recovery or ownership transfer.

Historical attempts remain queryable for diagnostics and audit. They are not an
authoritative dataset state machine and are never selected merely because their date
and segments match a new request.

### Model-Directed Investigation and Stage Choice

After delegation succeeds, the NavigationDataAgent decides what to investigate and
calls registered read-only tools to obtain current facts. Entry code does not create
an artifact snapshot or choose a phase on the model's behalf. The prompt and compact
domain guidance teach the model to establish, in dependency order:

1. the requested date and selected segment inventory
2. raw-input availability and structure
3. prepared-raw and per-segment extract/sync products
4. finish-temporary, annotation, tracking, projection, final-output, and validation
   products
5. detailed topic, sensor, calibration, localization, and gridmap facts needed only
   for the stage that remains to be planned

The model must not trust the user's description of prior progress, conversation
memory, or a persisted task status as product evidence. It may use those inputs as
guidance, but it verifies them with tools before planning.

The model chooses the stage by choosing the corresponding complete-plan submission
tool:

- incomplete extract/sync products normally lead to an extract-sync plan;
- complete extract/sync products with incomplete finish products normally lead to a
  finish-processing plan;
- complete validated final products normally lead to a user-facing no-work-needed
  result;
- partial or contradictory products lead to more investigation, an explicit user
  decision when destructive replacement is required, or a plan that handles the
  observed condition.

These are domain recommendations, not code-authored phase transitions. Validators
may reject a plan whose preconditions are contradicted by current evidence, but they
do not select an alternative plan or stage.

### Observation Store

Add a task-attempt-scoped `NavigationObservationStore`. An observation is appended
only when the NavigationDataAgent invokes an investigation tool. A new Web session
starts with a new task attempt and new observations; it does not reuse observations
from an older attempt for the same date/segments.

Each observation revision contains:

- `task_id`
- `revision`
- factual investigation kind/scope
- normalized typed facts
- evidence metadata
- `created_at`

Typed observation models represent inventories, measurements, and externally
visible state. Initial models should cover:

- raw segments and ROS topic inventory
- topic types, counts, frequencies, time ranges, jitter, and missing ratios
- sensor metadata and candidate roles
- calibration inventory
- localization-source inventory
- processing artifact inventory
- gridmap artifact inventory
- runtime tool and asset availability
- user-provided guidance such as `scene_mode`, clearly distinguished from observed
  product facts

Observation models must not contain policy fields such as
`localization_policy`, `gridmap_policy`, `calibration_policy`, or
`stage_variants`.

The guidance recommends the investigation order, while the model decides which
detail tools are necessary. Plan validation may require specific observation kinds
and evidence for the plan being submitted. An observation may state that a resource
is unavailable; completeness means the relevant fact is known, not that the desired
resource exists. Resource availability is handled by plan validation, a different
model-authored choice, or a human decision.

### Evidence Store

Full inspection payloads are persisted outside the conversation. Evidence metadata
is indexed by opaque `evidence_ref` and bound to `task_id` and observation revision.
Large JSON evidence is stored under a task-scoped directory below the
configured navigation runs root; SQLite stores the reference, kind, summary, size,
and provenance. The model never receives arbitrary filesystem access.

Every evidence read is:

- task-scoped
- read-only
- field-selectable
- paginated
- length-bounded

### Planning Context Projector

`NavigationPlanningContextProjector` produces the only default task-attempt view. It
contains:

- current task-attempt identifier and requested target
- the current observation revision
- compact facts already observed
- relevant user guidance
- available stage-specific action identifiers
- evidence catalog entries with summaries and refs

It excludes raw evidence payloads, product claims copied from older task attempts,
plan schemas, previous plan candidates, legacy profile drafts, historical validation
errors, and execution results unrelated to the current attempt. It may report that a
validator-required fact is absent, but it must not recommend a stage, action,
variant, or parameter.

### Plan Repository

Add a task-scoped `NavigationPlanRepository`. A plan is immutable after successful
validation. A later plan creates a new revision and marks the previous active plan
as `superseded`; it never patches the stored JSON.

Persist at least:

- `plan_id`
- `task_id`
- `phase`
- `plan_revision`
- `contract_version`
- `observation_revision`
- canonical plan JSON
- validation summary
- status (`active`, `superseded`, `completed`, `invalidated`)
- timestamps

The repository also persists `PlanSubmissionAttempt` records containing the full
candidate and complete validation report for server diagnostics. Submission attempts
are not returned to the model context and are not active plans.

### Execution Ledger

The execution ledger is created only after an active plan is stored. It references
the immutable `plan_id` and stores:

- current step
- per-step status
- start and finish timestamps
- compact result summaries
- full result refs
- produced-path refs
- retry count
- failure and replan state

The existing durable `navigation_task_steps` table is evolved into this ledger
instead of adding a parallel step table. Its rows are bound to `plan_id` and
`plan_revision` and no longer accept arbitrary arguments that differ from the plan.

## Read-Only Cognitive Tools

Planning needs a small, stable set of tools that extend what the model can know
without processing or mutating source data.

### `get_navigation_task_context`

Returns the current attempt's target, compact observed facts, evidence catalog,
user guidance, available stage identifiers, and `planning_context_revision`. It does
not return an entry-code-selected active phase.

### `list_observation_evidence`

Lists evidence metadata with optional `kind`, `cursor`, and `limit`. It returns only
refs and summaries.

### `read_observation_evidence`

Reads one task-scoped evidence ref with optional selected fields, cursor, and limit.
It never accepts an arbitrary path.

### `describe_processing_action`

Returns the variants, parameter contract, preconditions, and constraints for one
requested action. It describes capability but does not recommend an action or
variant.

Existing inspection tools remain read-only, but their return contract changes. Each
tool persists the full result, appends normalized observations, and returns only:

- `ok`
- an observation delta summary
- evidence refs
- the new observation revision

Inspection tools do not return a code-authored processing stage, next tool, semantic
profile, recommended sensor binding, or recommended plan.

The current semantic `infer_*` tools are replaced rather than retained as aliases:

- `infer_navigation_sensor_bindings_tool` becomes an observation-oriented sensor
  candidate inspection tool; it reports metadata and candidate roles but does not
  select bindings.
- `infer_navigation_topic_params_tool` becomes an observation-oriented topic
  candidate/statistics tool; it does not select the final whitelist, mapping, query
  directory, or sync policy.
- `infer_navigation_processing_profile_tool` is removed. Its factual platform,
  runtime, calibration, localization-source, and artifact checks move into typed
  inspection observations; its policy/profile synthesis is not preserved.

Their previous complete `NavigationProcessingProfile` return is not a plan and is
not copied into the new Observation Store.

## Phase-Specific Complete Plan Contracts

The model submits one complete business plan for the stage it selected:

- `submit_extract_sync_plan(planning_context_revision, plan)`
- `submit_finish_processing_plan(planning_context_revision, plan)`

Both submission tools are available after delegation so that choosing the tool is
the model's stage decision. The schemas stay phase-specific; they are not combined
into one large union or injected as prompt text. The tool runtime adds task-owned
metadata after validation:

- `plan_id`
- plan revision
- contract version
- date and selected segments
- scene mode when applicable
- model-selected phase
- timestamps
- observation revision

The model must not repeat these known request facts inside the business plan.
`planning_context_revision` is a concurrency token derived from the task request,
user guidance revision, observation revision, and active capability-catalog
revision. It proves which complete planning view the model used.

All plan input models use `extra="forbid"`. Each submission call validates only its
own phase schema. Code records the accepted plan's phase on the task attempt for
execution and telemetry; it does not infer that phase from artifacts.

### Extract-Sync Plan Input

The model owns:

- selected sensor bindings
- topic selection and mapping
- time-sync reference, method, tolerance, and rationale
- selected steps, variants, parameters, dependencies, and failure policies
- evidence refs supporting each decision

### Finish-Processing Plan Input

The model owns:

- localization source and conversion
- gridmap source and preparation choice
- calibration mode/profile and whether human confirmation is needed
- selected steps, variants, parameters, dependencies, and failure policies
- evidence refs supporting each decision

Finish processing references the active successful extract-sync plan and artifacts;
it does not duplicate extract-sync decisions.

### Step Schema

Steps use a discriminated union keyed by `action`. Each action type exposes only its
legal variants and argument model. Every nested model forbids extra fields.

The model supplies:

- `step_id`
- `action`
- `variant`
- action-specific arguments
- `depends_on`
- `failure_policy`
- `decision_refs` linking the step to the relevant decision objects

Every semantic decision object requires a concise `reason` of at most 500
characters and at least one evidence ref. A ref may point to user guidance as well
as inspection evidence. Steps do not duplicate those reasons or evidence lists.

Code derives fixed capability metadata such as effects and declared output kinds
after the model has selected the action and variant. It does not generate steps or
variants.

The new canonical plan has one location for each semantic decision. It does not
contain duplicate `processing_profile`, top-level and nested `topic_params`, separate
`stage_variants`, repeated `platform_hint`, or model-authored warning/blocking issue
trees.

## Atomic Submission and Validation

The submission boundary performs, in order:

1. task-attempt and submission-tool check
2. planning-context revision check
3. Pydantic/JSON structure validation
4. evidence-ref ownership and revision validation
5. action, variant, and argument validation
6. reference validation against observed facts
7. dependency graph and phase-order validation
8. navigation business-rule and observed-precondition validation
9. one database transaction that saves the active plan and initializes its ledger

Any failure leaves both the active plan and execution ledger unchanged.

The model receives a compact stable response such as:

```json
{
  "ok": false,
  "error_type": "plan_validation_failed",
  "errors": [
    {
      "path": "plan.decisions.time_sync.reference_sensor",
      "code": "unknown_sensor_role",
      "message": "Referenced sensor role does not exist",
      "allowed_values": ["fisheye_front", "lidar", "odom"]
    }
  ],
  "retry": "resubmit_complete_plan"
}
```

Errors are deduplicated, sorted by path/code, capped in count and length, and never
include the full candidate, full schema, current plan, or previous errors. When the
allowed-value set is large, the response points to an evidence or action-contract
ref rather than embedding the set.

After an invalid submission, the model must resubmit a complete corrected plan. No
patch endpoint exists.

A successful response returns only `ok: true`, `plan_id`, revision, step count,
status, and next action. It does not echo the plan the model just submitted. Both
success and failure responses therefore expose the same top-level `ok` discriminator.
The runtime never replaces a rejected model choice with a code-generated stage or
plan.

## Plan-Bound Execution

After validation, the model reads execution state through:

- `get_plan_execution_overview(plan_id)`: ids, titles, statuses, and counts only
- `get_current_plan_step(plan_id)`: one current step and its concise decision context

The model then invokes the processing tool named by the current plan step with
`plan_id` and `step_id`. A plan-bound wrapper loads canonical business arguments
from the stored plan and passes them to the underlying execution function. The model
does not retype date, segments, variant, or complex arguments.

Before execution, the wrapper verifies:

- the plan is still active
- the requested step belongs to that plan revision
- the invoked tool matches the step action
- all dependencies are complete
- the step is currently executable
- observed input refs and required filesystem preconditions still hold
- stored arguments still satisfy the underlying tool contract
- no other task attempt currently holds the data-writing resource lock for the same
  target

Full processing results are persisted externally. Tool responses return status,
short summaries, produced refs, and the next action.

If execution reveals facts that invalidate the plan, the runtime persists those
facts, marks the ledger `needs_replan`, invalidates the old plan without modifying
it, and returns a compact failure with evidence refs. The model decides what to
inspect next and submits a new complete plan based on the new context revision. The
runtime does not choose the replacement stage or steps.

### Two-Stage User Interaction

Navigation processing has two major business stages, but their conversational
boundary is model-governed rather than a hard-coded workflow transition.

After the accepted extract-sync plan finishes, the NavigationDataAgent follows the
domain guidance to:

1. inspect or validate the produced sync artifacts
2. tell the user what completed and what remains
3. ask whether the user wants to continue with finish processing now
4. if continuing, collect only the finish-processing inputs that are still needed,
   such as scene mode and any required confirmation context
5. investigate finish-specific facts and submit one complete finish-processing plan

If the user stops, the same task attempt and extract-sync ledger remain durable for
same-session continuation. On a later message in that session, the model rechecks
the relevant artifacts before it plans finish processing. If the user instead opens
a new Web session, the router delegates a new task attempt; the new
NavigationDataAgent independently investigates the products and normally discovers
that extract/sync is already complete.

No entry-state machine automatically advances to finish processing, and no code gate
pretends that a user confirmation occurred. Safety validators and plan-bound tool
authorization remain code-enforced, but asking, waiting, interpreting the reply, and
choosing whether to author the finish plan belong to the model under prompt and
guidance instructions.

### Fail-Closed Human-Decision Delivery Recovery

Plan-bound human decisions use a durable handoff record, but lease expiry is not
proof that the previous AgentScope worker stopped. The runtime therefore never
automatically re-delivers a handoff merely because its `delivering` lease expired.

Delivery recovery follows these rules:

- if the persisted AgentScope tool call is no longer `SUBMITTED`, the decision was
  consumed; recovery only acknowledges the durable handoff and never starts a
  second AgentScope continuation;
- if the tool call remains `SUBMITTED` after the delivery lease expires, the
  handoff becomes `recovery_required`; automatic re-delivery is forbidden;
- if the Web mapping or AgentScope session needed to inspect the tool call is
  missing, the handoff also becomes `recovery_required`;
- `recovery_required` handoffs continue to block plan replacement and processing
  recovery until an explicit controlled recovery request is made;
- controlled recovery quarantines the handoff with an operator/user reason,
  preserves its decision and delivery audit, invalidates the affected plan, and
  moves the task to `needs_replan`; it never pretends that the decision was
  delivered;
- only a handoff already marked `recovery_required` may be quarantined. Live
  `pending` or unexpired `delivering` work cannot be force-recovered through this
  path.

The controlled recovery endpoint is
`POST /api/sessions/{session_id}/human-decisions/recovery`. Its request contains
only `action: "quarantine_and_replan"`, `plan_id`, `step_id`, and a bounded
non-empty `reason`. It is an operational safety valve, not a model-facing planning
or processing tool. The repository verifies that the affected task belongs to the
Web session in the URL and that the handoff is already `recovery_required`.

A successful recovery changes the handoff to the terminal audited state
`quarantined`, preserves the original decision, records the reason and recovery
timestamp, invalidates the affected plan, changes unfinished ledger steps and the
task to `needs_replan`, and returns a compact recovery anchor. Quarantined handoffs
do not block a replacement plan. A stale worker that later resumes remains fenced
from processing by the invalidated plan and current-step ledger gates.

The Web event and UI preserve `plan_id`, `step_id`, `recovery_required`,
`submission_disabled`, and `recovery_endpoint`. A recovery-required update for an
already displayed reply/tool-call identity replaces the prior normal decision
state instead of being dropped as a duplicate. The UI disables confirm, stop, and
guide submission, displays a bounded recovery-reason input, and calls only the
controlled recovery endpoint. Normal plan-bound submissions include `plan_id` and
`step_id`; legacy decisions may omit them until the legacy path is removed.

Fully automatic lease reclamation requires a future AgentScope session-transaction
fencing token and is explicitly outside this iteration. AgentScope context
compression remains unchanged.

## Tool Exposure by Workflow Activity

Tool exposure follows the current task attempt's activity, not a phase inferred by
entry code.

- Main router: conversation tools plus `start_navigation_data_task`; it does not
  receive navigation investigation, plan, or processing tools.
- Investigation and planning: task-context, artifact/inventory inspection, bounded
  evidence/action-description tools, and both phase-specific complete-plan
  submission tools. Choosing a submission tool is the model's stage choice.
- Execution: execution overview/current-step tools, the small set of plan-bound
  processing tools referenced by the accepted plan, and required human-decision
  tools. Each wrapper accepts only `plan_id`/`step_id` and authorizes only the
  current executable step.
- Between stages: investigation and planning tools remain available. The prompt and
  guidance tell the model to ask the user before finish processing; code does not
  fabricate that decision or automatically submit a second-stage plan.

Both complete-plan schemas may be exposed during investigation, but no execution
schema, inactive legacy schema, full capability catalog, or generated schema
snapshot is placed in prompt or tool results. If the two strict schemas later exceed
the measured context budget, the model may first select a stage through a tiny
model-authored stage-selection call that only changes tool exposure; code still must
not select the stage. This fallback is allowed only with context measurements and is
not the default design.

Execution wrappers and the target-scoped data-writing lock remain hard authorization
boundaries even if a stale client invokes a hidden tool.

## Prompt and Domain-Guidance Design

Prompt content is split deliberately so that each rule has one home.

### Main Router Prompt

The main router prompt contains only routing policy:

- answer ordinary conversation and capability questions directly
- delegate only a concrete navigation-processing request with a date/path/dataset
  target
- preserve the user's target, optional segments, optional scene context, request,
  and response language in the handoff
- do not inspect navigation products or decide a navigation-processing stage
- report delegation success only when `start_navigation_data_task` returns
  `ok: true` and `started: true`
- report a compact truthful failure for `ok: false`; never claim the task started
  and never use shell/file tools to work around a failed handoff

`start_navigation_data_task` is terminal for the router turn. After its ToolResult
is durable, a router-only AgentScope reply middleware ends the reasoning/acting loop,
so the router cannot call another tool or generate another model response in that
turn. The Web runtime renders the authoritative user-facing outcome from the
structured tool result and defensively ignores any post-tool free text. This
prevents the exact observed failure where the model received an error ToolResult and
then stated that the task had started.

The handoff tool itself returns a stable contract:

```json
{"ok": true, "started": true, "task_id": "nav_..."}
```

or:

```json
{
  "ok": false,
  "started": false,
  "error_type": "navigation_data_busy",
  "message": "This target currently has a running data-processing action."
}
```

`dry_run` is not part of the router/model-facing handoff schema. Production defaults
to real execution (`dry_run=false`); tests and operators may select dry-run only
through trusted runtime or direct-CLI configuration.

### NavigationDataAgent Prompt

The NavigationDataAgent system prompt contains concise, durable invariants:

- investigate before deciding; user statements, conversation memory, older task
  status, and older product snapshots are guidance rather than current facts
- the model decides which investigation tools to call, which stage applies, and all
  plan semantics
- investigation tools record and return facts only
- submit one complete strict JSON plan through the chosen stage tool; never patch
- on validation failure, use bounded errors/evidence and resubmit a complete plan
- execute only the accepted immutable plan through plan-bound tools
- after extract/sync, verify outputs, report completion, ask whether to continue,
  and collect required finish inputs before authoring the finish plan
- same-session durable Plan/ledger state survives compaction or restart, but mutable
  product facts are rechecked before new work
- a new Web session is a new task attempt and never resumes an old attempt

The system prompt does not contain a generated schema, exhaustive action catalog,
long command cookbook, repeated state panel, or a copy of the domain-guidance file.

### `navigation-plan-agent-guidance.md`

The guidance file is a compact domain playbook, not a second system prompt. It is
loaded once per NavigationDataAgent context or exposed as one bounded guidance
resource. It contains only information that materially improves model decisions:

1. **Product dependency map**: raw acquisition data -> prepared/extracted data ->
   per-segment `sync_data` -> finish temporary data -> annotation/tracking/projection
   -> final outputs and validation markers.
2. **Recommended investigation order**: confirm target and segment inventory; inspect
   product existence/completeness; then inspect detailed topics, timestamps, sensors,
   calibration, localization, and gridmap only for the remaining stage.
3. **Common extract-sync work**: prepare raw data, inspect ROS topics and timing,
   choose sensor bindings and sync reference/method, extract, synchronize, and
   validate selected outputs.
4. **Common finish-processing work**: confirm continuation and required user inputs,
   inspect localization/gridmap/calibration facts, prepare finish data, request
   bounded human decisions when necessary, run annotation/tracking/projection, and
   validate final outputs.
5. **Decision ownership**: the model chooses reference sensor, sync policy,
   localization, calibration, gridmap, ordered steps, variants, and parameters; code
   only validates against facts and capabilities.
6. **User-confirmation points**: whether to continue after extract/sync, missing
   finish inputs, destructive overwrite/delete, and plan-declared calibration/GUI
   decisions.
7. **Failure behavior**: inspect before retry, do not repeat destructive actions
   without confirmation, and resubmit a whole plan after validation failure.
8. **Few-shot examples** limited to decision boundaries that are easy to mishandle:
   - user says sync is complete, but inspection finds missing segment outputs ->
     investigate and choose extract-sync rather than trusting the statement;
   - a new session finds complete sync products and missing finish products -> ask
     for/confirm finish inputs, then submit a finish-processing plan without
     restoring the older task;
   - extract/sync just completed -> report and ask whether to continue instead of
     immediately running finish tools;
   - invalid complete plan -> correct the indicated fields and resubmit the complete
     JSON, never a patch.

Few-shots show compact observations, reasoning criteria, and the correct next
action. They do not embed full schemas, large tool payloads, complete transcripts,
or fixed business choices that should depend on the actual data.

Deployment acceptance instructions, Git synchronization, token-metric collection,
legacy draft-directory cleanup, and operator-only recovery runbooks are removed from
`navigation-plan-agent-guidance.md` and kept in separate operator documentation.
They do not help the NavigationDataAgent decide a data plan and must not consume its
context.

The prompt and guidance are reviewed together for duplication. Identity, safety,
tool-result truthfulness, and strict-plan invariants stay in the prompt. Domain
knowledge, investigation method, common stage steps, confirmation points, and
few-shots stay in the guidance file.

A compact state anchor may be injected per turn with only task-attempt id, accepted
plan id/revision when present, current ledger step, observation revision, and actual
execution status. It must not assert that current products exist or choose the next
stage.

## Context Budgets

Application-level responses stay below AgentScope's configured result limit:

| Response | Target | Hard maximum |
| --- | ---: | ---: |
| Inspection observation delta | 2,000 chars | 4,000 chars |
| Task-attempt planning context | 4,000 chars | 5,500 chars |
| Evidence detail page | 4,000 chars | 5,500 chars |
| Plan validation failure | 2,000 chars | 3,000 chars |
| Execution overview/current step | 2,000 chars | 4,000 chars |
| Processing result summary | 2,000 chars | 4,000 chars |

Anything larger is stored externally and returned by ref with pagination. Tool
results never contain a schema snapshot or full state snapshot.

The representative end-to-end transcript must remain below AgentScope's compact
trigger with at least 20% headroom. With the current 104,857-token trigger, peak
model input must stay at or below 83,885 tokens. The stronger desired result is that
the standard test completes without any compact event.

No AgentScope compression behavior or phase-boundary sub-session rotation is changed
in this iteration. If a later optimized real run still reaches the compact trigger,
sub-session rotation can be reconsidered as a separate design.

## Persistence and Recovery

Persistence is attempt-scoped and has two purposes: safe same-session continuation
and auditable side-effect execution. It is not a cross-session dataset state machine.

Persist for one task attempt:

- request target, selected segments, Web/AgentScope session ids, and timestamps
- compact lifecycle status for diagnostics (`active`, `waiting_user`,
  `needs_replan`, `completed`, `failed`, `cancelled`, or `superseded`);
  `needs_replan` is an execution-recovery condition, never product evidence
- observations/evidence produced by tools the model actually called
- immutable accepted Plan revisions and validation attempts
- execution ledger, staged results/outbox, human-decision handoffs, and resource-lock
  ownership needed for exactly-once side effects

An accepted plan's phase may be stored as execution metadata. It is authoritative
for that plan's ledger only; it is not proof of current filesystem products and is
not used to route a new Web session.

After compaction, restart, cancellation, or a later message in the same session, the
agent may recover its accepted Plan and current ledger step from durable stores. It
rechecks mutable product facts before it authors another plan or repeats work.

A new Web session creates a fresh attempt. It reads no older attempt as planning
state and independently invokes investigation tools. Historical attempts remain
available to operators but are excluded from the default model context.

The only cross-attempt coordination is a narrow target-scoped lock around actual
data-writing actions. Completed, failed, waiting-user, and planning attempts do not
block delegation. A genuinely running conflicting action returns a compact busy
result and performs no ownership transfer or automatic recovery.

## Legacy Design Removal and Migration

This change replaces the legacy planning path completely. After new consumers are
connected, delete the obsolete implementation in the same change, including:

- `WorkflowPlanDraftState`
- `NavigationPlanDraftStore` and its JSON implementation
- `get_workflow_plan_draft_tool`
- `update_workflow_plan_draft_tool`
- `finalize_extract_sync_plan_tool`
- `finalize_finish_processing_plan_tool`
- `finalize_workflow_plan_tool`
- `schema_snapshot()` and draft status panels
- `NavigationProcessingProfile`, `NavigationExtractSyncProfile`, and
  `NavigationFinishProcessingProfile` once observation/plan models replace all uses
- deterministic phase plan builders such as `build_deterministic_plan_template`
- legacy prompt instructions, gates, imports, fixtures, and tests that exist only for
  the draft/finalize path

Do not leave deprecated wrappers, compatibility aliases, unreachable branches, or
commented-out old code. Source files that become empty or wholly obsolete are
deleted. The implementation plan must include an `rg`-based dead-reference audit.

This is an explicit development-stage durable-state reset, not a backward-compatible
SQLite migration. The old Task, Observation, Evidence metadata, Plan, ledger,
outbox, and human-handoff records were produced only by test runs under a flawed
task-identity design and have no audit or recovery value. New code detects an
incompatible navigation-state schema and fails closed with
`NavigationStateResetRequired`; it never rewrites, merges, or silently deletes the
old database. The deployment runbook stops the service, backs up the old navigation
state, and creates a fresh database with the new schema.

Remove the invariant and unique index that make `(date, segments)` one global active
task across Web sessions. New delegation creates a new attempt. Same-session lookup
uses the verified Web/AgentScope session pair and task id, never a global
date/segments owner. Current execution authorization remains attempt- and
plan-bound. A separate target lock protects only running data-writing actions.

Old session-scoped draft/evidence files are no longer read. They are not deleted by
application startup. The reset runbook may remove them only after the backed-up
SQLite state has been verified and only within the configured navigation-state
root; raw acquisition data and processing products are outside that cleanup scope.
No runtime compatibility code remains for old files or schemas.

Attempts created after the reset remain historical attempts for diagnostics and
same-session Plan/ledger recovery. A request from a new Web session never attaches
to an attempt from another session, regardless of whether that attempt is completed,
failed, waiting, or unfinished.

## Testing Strategy

### Observation and Evidence Tests

- typed observations accept measurements and inventories and do not expose policy
  fields
- no observation or artifact snapshot is created until the model invokes an
  investigation tool
- inspection calls persist complete evidence and return only bounded deltas/refs
- evidence refs are task- and revision-scoped
- arbitrary path reads and cross-task refs are rejected
- field selection, pagination, and hard response limits are enforced
- a new Web session cannot read an older attempt's observations as its planning
  context

### Plan Contract and Submission Tests

- all nested plan inputs reject extra fields
- each generated phase submission schema contains only that phase's fields
- duplicate legacy profile fields do not exist
- valid extract-sync and finish-processing plans succeed in one submission
- missing fields, bad refs, unsupported variants, invalid arguments, cycles, and
  invalid ordering return stable path-addressed errors
- stale planning-context revisions are rejected
- failed submissions do not create or mutate active plans or ledger rows
- successful responses do not echo the complete plan
- full failed candidates and validation details remain available only in submission
  attempt storage
- choosing `submit_extract_sync_plan` or `submit_finish_processing_plan` records the
  model-selected phase; no artifact-entry function chooses it first

### Execution Tests

- ledger creation is atomic with successful plan activation
- overview and current-step tools return bounded views
- processing arguments are loaded from the active plan, not from model-provided
  copies
- wrong plan, step, action, phase, revision, or dependency state is rejected
- successful/failed tool results update the correct step exactly once
- new invalidating facts transition the task and ledger to `needs_replan`
- replan creates a new immutable revision and supersedes the old plan

### Integration Tests

- the main router delegates a concrete navigation request and does not delegate
  ordinary conversation or a capability question
- a failed handoff returns `ok: false, started: false`, remains on the router, and
  terminates the router turn with an authoritative failure response
- a successful handoff returns `ok: true, started: true` and creates a task attempt
  for the current Web/AgentScope session without a second model-authored success
  message
- a new Web session for the same date/segments creates a new task attempt rather
  than attaching to or being blocked by an older completed/waiting/failed attempt
- a genuinely running data-writing action for the same target returns
  `navigation_data_busy` without mutating either attempt
- raw-only investigation facts lead the model to submit an extract-sync plan
- complete selected `sync_data` plus missing downstream products lead the model to
  ask for required finish inputs and submit a finish-processing plan without
  rerunning extract-sync
- complete validated final-product facts lead the model to report that no processing
  is needed
- deleting test products after an earlier completed attempt causes a new-session
  NavigationDataAgent to investigate again and choose work from current facts
- complete extract-sync investigation, model-authored plan, and dry-run execution
- after extract-sync execution, the model verifies outputs, reports completion, asks
  whether to continue, and does not invoke finish-processing tools before the user
  answers
- same-session continuation rechecks relevant products before finish planning
- new-session continuation creates a fresh attempt, distrusts the user's claim that
  extract/sync completed until tools verify it, and then plans the appropriate stage
- model selection of sync reference, localization, gridmap, calibration, steps, and
  variants based on provided facts
- invalid complete plan followed by a corrected complete resubmission
- exact regression for the former nested `topic_params` finalize failure, proving no
  partial draft can be created
- recovery after a deliberately compacted same-session conversation retains the
  accepted Plan/current ledger step while requiring fresh mutable facts for new work
- all active tool lists exclude legacy draft/finalize tool names

### Prompt and Guidance Tests

- the router prompt defines triage and truthful handoff-result handling but contains
  no navigation artifact investigation or stage-selection policy
- the NavigationDataAgent prompt states model ownership, investigate-before-decide,
  strict complete-plan submission, same-session durability, fresh new-session task
  semantics, and the two-stage user-confirmation boundary
- `navigation-plan-agent-guidance.md` contains the dependency map, recommended
  investigation order, common steps for both stages, confirmation points, failure
  behavior, and the bounded few-shot cases required by this design
- prompt and guidance have no duplicated long blocks, generated schemas, exhaustive
  catalogs, draft snapshots, or obsolete cross-session-resume instructions
- the combined prompt, guidance, exposed tool schemas, and representative compact
  anchor are measured as part of the context regression budget

### Context Regression Tests

- record every tool response size and reject budget violations
- record exposed tool-schema size per workflow activity
- simulate representative investigation plus validation retry plus full execution
- assert no returned payload contains full schemas, full drafts, or accumulated
  validation history
- assert peak model input remains under the defined headroom budget
- run the same instrumentation during the server real-data acceptance test

### Dead-Code Tests

- `rg` finds no references to legacy draft/finalize classes, tools, prompts, or
  deterministic plan builders
- obsolete source and test files are removed rather than skipped
- imports and public exports expose only the new planning architecture

## Acceptance Criteria

1. The model selects all navigation strategies, steps, variants, and business
   parameters from observations, guidance, and capability contracts.
2. The NavigationDataAgent decides which investigation tools to call; code records
   returned facts and validates decisions but does not inspect automatically or fill
   semantic plan fields.
3. Each phase uses one complete strict JSON submission; there is no patch/finalize
   loop.
4. A valid plan succeeds in one call, and an invalid call cannot pollute active
   state.
5. Observation facts, immutable plan revisions, and execution status have separate
   durable stores/interfaces.
6. Detailed evidence is available through bounded read-only tools without entering
   the default context.
7. Execution uses canonical plan arguments and proceeds one stored step at a time.
8. The standard representative workflow does not trigger AgentScope compaction and
   stays within the defined headroom budget.
9. Forced compaction or same-session restart cannot lose the accepted Plan or current
   ledger step; new Web sessions deliberately start fresh task attempts.
10. AgentScope compression and phase-boundary session handling are unchanged.
11. The old draft/finalize/profile-generation implementation and all dead references
    are deleted in the same optimization change.
12. Before choosing a stage, every NavigationDataAgent calls investigation tools for
    current intermediate/final products and does not trust stale task state or user
    claims as product facts.
13. After extract/sync, the model asks whether to continue and collects required
    finish inputs before it authors a finish-processing plan; this behavior comes
    from concise prompt/domain guidance rather than a code-selected phase transition.
14. A new Web session for an existing data target creates a new attempt; only an
    actual conflicting data-writing action may return `navigation_data_busy`.
15. Router handoff results use explicit `ok`/`started` booleans, and a failed tool
    result cannot be presented as a successful delegation.

## Implementation Sequence

1. Redefine `NavigationTask` as a session-bound task attempt, remove global
   date/segments ownership, add the narrow data-writing resource lock, and make the
   handoff contract return explicit `ok`/`started` fields.
2. Add model-invoked typed observation/evidence tools, persistence, context
   projection, and bounded read-only cognitive tools; remove automatic entry
   inspection and code-authored phase selection.
3. Add normalized phase plan inputs, discriminated step schemas, plan repository,
   submission-attempt audit storage, and atomic submission tools.
4. Adapt existing validators to the new facts/decisions boundary and remove profile
   duplication.
5. Bind the execution ledger and execution tool wrappers to immutable plan
   revisions.
6. Resolve tools by task-attempt activity, then rewrite the router prompt,
   NavigationDataAgent prompt, `navigation-plan-agent-guidance.md`, and compact state
   anchor according to the non-duplication rules and few-shot requirements.
7. Enforce the new navigation-state schema generation, document the explicit
   backup/reset procedure for incompatible development databases, and remove the
   legacy draft/finalize implementation and tests.
8. Run focused unit and integration tests, the complete local suite, schema/context
   size audits, and the dead-reference audit.
9. Deploy the synchronized code to the server, run a fresh NavigationDataAgent
   investigation on real data, and run an
   instrumented real-data acceptance test before considering any additional context
   mechanism changes.
