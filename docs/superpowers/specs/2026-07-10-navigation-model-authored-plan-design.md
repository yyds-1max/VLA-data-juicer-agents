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
- Recover the current phase, active plan, and current step from durable state even
  if the conversation is compacted or resumed in another web session.
- Delete the superseded draft/finalize implementation in the same change so the
  repository contains one planning design, not parallel old and new paths.

## Non-Goals

- Do not modify AgentScope's compression implementation.
- Do not rotate AgentScope sub-sessions at phase boundaries in this iteration.
- Do not let code select a time-sync reference, localization policy, gridmap source,
  calibration strategy, processing step, or stage variant.
- Do not adopt Datus's Markdown plan format; navigation plans remain strict JSON.
- Do not add a distributed workflow engine.
- Do not automatically convert an ambiguous legacy profile draft into a new plan.
- Do not preserve obsolete planning tool aliases or deprecated implementation stubs.

## Core Ownership Rules

The system has three authoritative state categories:

| State | Meaning | Owner | Mutability |
| --- | --- | --- | --- |
| Observation Store | What was observed | Investigation code | Append by revision |
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

### Entry Reconciliation and Phase Selection

Every navigation processing entry follows the same ordering, including the first
request for a date, a continuation in the same session, a cross-session resume, and
a request made after test artifacts were manually deleted:

1. create or load the durable task for the selected date and segments
2. inspect current raw, intermediate, and final artifacts
3. reconcile persisted task state with that artifact snapshot
4. select the earliest incomplete processing phase or step
5. only then build the phase observation checklist and enter investigation/planning

Artifact reconciliation is therefore an unconditional entry gate, not a special
rerun workflow. The initial snapshot includes at least raw input, prepared raw temp,
per-segment `sync_data`, finish-temp samples, final outputs, final grid maps, and
available validation markers.

Phase selection follows artifact dependencies:

- selected raw data exists but selected `sync_data` is incomplete: enter
  `extract_sync`
- selected `sync_data` is complete but finish-processing outputs are incomplete:
  enter `finish_processing` at the earliest step whose required output is absent or
  invalid
- required final outputs and validation markers are complete: reconcile the task as
  `completed`
- artifacts are partially present or internally inconsistent: record the exact
  artifact facts and enter `needs_reconcile` before planning the affected work

Observed artifacts override stale persisted phase/status. This phase derivation is
not semantic plan generation: code determines only which prerequisite outputs exist
and which dependency boundary is incomplete. The model still decides the plan,
steps, variants, and parameters for the selected phase.

### Observation Store

Add a task-scoped `NavigationObservationStore`. Observations belong to the durable
`NavigationTask`, not to an AgentScope session, so a new session can resume planning.

Each observation revision contains:

- `task_id`
- `revision`
- `phase`
- `required_observation_status`
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
- user-provided task facts such as `scene_mode`

Observation models must not contain policy fields such as
`localization_policy`, `gridmap_policy`, `calibration_policy`, or
`stage_variants`.

The phase checklist determines which observation types must have been attempted. A
completed observation may state that a resource is unavailable; completeness means
the fact is known, not that the desired resource exists. Resource availability is
handled by plan validation or a human decision, not by marking the observation as
permanently missing.

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

`NavigationPlanningContextProjector` produces the only default planning view. It
contains:

- current task and phase identifiers
- the current observation revision
- required observation completion and missing observation kinds
- compact facts needed for the active phase
- relevant user guidance
- active-phase action identifiers
- evidence catalog entries with summaries and refs

It excludes raw evidence payloads, inactive-phase facts, plan schemas, previous plan
candidates, legacy profile drafts, historical validation errors, and execution
results unrelated to planning.

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

### `get_phase_planning_context`

Returns the active phase's compact context, observation completeness, fact summary,
available action ids, evidence catalog, and `planning_context_revision`.

### `list_observation_evidence`

Lists evidence metadata with optional `kind`, `cursor`, and `limit`. It returns only
refs and summaries.

### `read_observation_evidence`

Reads one task-scoped evidence ref with optional selected fields, cursor, and limit.
It never accepts an arbitrary path.

### `describe_processing_action`

Returns the variants, parameter contract, preconditions, and constraints for one
active-phase action. It describes capability but does not recommend an action or
variant.

Existing inspection tools remain read-only, but their return contract changes. Each
tool persists the full result, appends normalized observations, and returns only:

- `ok`
- an observation delta summary
- evidence refs
- the new observation revision
- remaining missing observation kinds

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

The model submits one complete business plan per phase:

- `submit_extract_sync_plan(planning_context_revision, plan)`
- `submit_finish_processing_plan(planning_context_revision, plan)`

The tool runtime adds task-owned metadata after validation:

- `plan_id`
- plan revision
- contract version
- date and selected segments
- scene mode when applicable
- phase
- timestamps
- observation revision

The model must not repeat these known request facts inside the business plan.
`planning_context_revision` is a concurrency token derived from the task request,
user guidance revision, observation revision, and active capability-catalog
revision. It proves which complete planning view the model used.

All plan input models use `extra="forbid"`. Each phase exposes only its own schema.

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

1. task and phase check
2. planning-context revision check
3. Pydantic/JSON structure validation
4. evidence-ref ownership and revision validation
5. action, variant, and argument validation
6. reference validation against observed facts
7. dependency graph and phase-order validation
8. navigation business-rule validation
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

A successful response returns only `plan_id`, revision, step count, status, and next
action. It does not echo the plan the model just submitted.

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
- task/artifact reconciliation has not invalidated the plan
- stored arguments still satisfy the underlying tool contract

Full processing results are persisted externally. Tool responses return status,
short summaries, produced refs, and the next action.

If execution reveals facts that invalidate the plan, the runtime appends a new
observation revision, marks the ledger and task `needs_replan`, invalidates the old
plan without modifying it, and returns to planning. The model then submits a new
complete plan based on the new context revision.

## Tool Exposure by Phase

The active tool set is resolved from durable task state for each agent turn or
toolkit refresh.

- Investigation: task tools, relevant inspection tools, and read-only cognitive
  tools.
- Planning: read-only cognitive tools and the one active-phase plan submission tool.
- Execution: execution overview/current-step tools, the current step's processing
  tool, and required human-decision tools.

Inactive-phase plan schemas and unrelated processing tool schemas are not exposed.
Execution wrappers remain a hard authorization boundary even if a stale client tries
to invoke a hidden tool.

## Prompt Design

The NavigationDataAgent prompt must state:

- investigation records facts only
- the model owns every semantic plan decision
- the model may inspect evidence and capability details on demand
- required observations must be complete before plan submission
- each phase plan is submitted once as complete strict JSON
- validation failure requires complete resubmission, never a patch
- execution follows the stored active plan one step at a time
- durable tools are authoritative for phase and step state after resume or compact

The prompt must not embed generated plan schemas, draft snapshots, repeated state
panels, exhaustive capability catalogs, or long tool-order instructions that the
runtime already enforces.

A compact state anchor may be injected per turn with only task id, phase, task
status, observation revision, active plan id/revision, and current step id. It must
be generated from durable state rather than conversation history.

## Context Budgets

Application-level responses stay below AgentScope's configured result limit:

| Response | Target | Hard maximum |
| --- | ---: | ---: |
| Inspection observation delta | 2,000 chars | 4,000 chars |
| Phase planning context | 4,000 chars | 5,500 chars |
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

On every processing entry, the runtime reconstructs state from:

- `NavigationTaskStore`
- `NavigationObservationStore`
- `NavigationPlanRepository`
- execution ledger
- current artifact reconciliation

Conversation messages may help the model understand user intent, but they never
decide the current phase or completed step. After any compact or new web session, the
agent first reads the compact durable state anchor and then requests details only as
needed.

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

The SQLite migration preserves durable task identity, request fields, scene mode,
artifact snapshot, drift, and completed execution evidence. It removes the obsolete
`data_profile_json` field through a transactional table migration after marking
affected unfinished tasks `needs_replan`. It does not attempt to convert the old
profile into decisions. `needs_replan` is added as an explicit durable task and
execution-ledger status.

Old session-scoped draft files are no longer read. They are not automatically
deleted from deployment storage because that would be an unrelated destructive data
operation; operational cleanup can remove them after rollout verification. No
runtime compatibility code remains for those files.

Existing unfinished tasks retain their task and artifact state but have no active
new-format plan. On continuation they reconcile current artifacts, refresh
observations, and ask the model for a new complete phase plan. Existing completed
tasks remain completed if artifact reconciliation confirms their outputs.

## Testing Strategy

### Observation and Evidence Tests

- typed observations accept measurements and inventories and do not expose policy
  fields
- inspection calls persist complete evidence and return only bounded deltas/refs
- evidence refs are task- and revision-scoped
- arbitrary path reads and cross-task refs are rejected
- field selection, pagination, and hard response limits are enforced
- required observation completion is independent of resource availability

### Plan Contract and Submission Tests

- all nested plan inputs reject extra fields
- the generated active-phase tool schema contains only active-phase fields
- duplicate legacy profile fields do not exist
- valid extract-sync and finish-processing plans succeed in one submission
- missing fields, bad refs, unsupported variants, invalid arguments, cycles, and
  invalid ordering return stable path-addressed errors
- stale planning-context revisions are rejected
- failed submissions do not create or mutate active plans or ledger rows
- successful responses do not echo the complete plan
- full failed candidates and validation details remain available only in submission
  attempt storage

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

- raw-only artifact state selects `extract_sync` before any plan is requested
- complete selected `sync_data` with missing downstream outputs selects
  `finish_processing` without rerunning extract-sync
- complete validated final artifacts reconcile directly to `completed`
- deleting test artifacts after a completed run causes the next ordinary task entry
  to select the earliest incomplete phase from the new snapshot
- complete extract-sync investigation, model-authored plan, and dry-run execution
- scene-mode update followed by complete finish-processing planning and execution
- model selection of sync reference, localization, gridmap, calibration, steps, and
  variants based on provided facts
- invalid complete plan followed by a corrected complete resubmission
- exact regression for the former nested `topic_params` finalize failure, proving no
  partial draft can be created
- resume from a new web session using durable state only
- recovery after a deliberately compacted conversation without phase/step loss
- all active tool lists exclude legacy draft/finalize tool names

### Context Regression Tests

- record every tool response size and reject budget violations
- record exposed tool-schema size per phase
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
2. Code records facts and validates decisions but does not fill semantic plan fields.
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
9. Forced compaction or cross-session resume cannot lose the authoritative phase,
   plan, or current step.
10. AgentScope compression and phase-boundary session handling are unchanged.
11. The old draft/finalize/profile-generation implementation and all dead references
    are deleted in the same optimization change.
12. Every processing request reconciles current intermediate/final artifacts before
    choosing a planning phase, so stale task state cannot force the wrong starting
    point.

## Implementation Sequence

1. Add typed observation/evidence models, persistence, context projection, and
   bounded read-only cognitive tools.
2. Add normalized phase plan inputs, discriminated step schemas, plan repository,
   submission-attempt audit storage, and atomic submission tools.
3. Adapt existing validators to the new facts/decisions boundary and remove profile
   duplication.
4. Bind the execution ledger and execution tool wrappers to immutable plan
   revisions.
5. Resolve the exposed tool set from durable phase state and simplify prompts/state
   anchors.
6. Migrate durable task storage, mark ambiguous unfinished work `needs_replan`, and
   remove the legacy draft/finalize implementation and tests.
7. Run focused unit and integration tests, the complete local suite, schema/context
   size audits, and the dead-reference audit.
8. Deploy the synchronized code to the server, reconcile a real task, and run an
   instrumented real-data acceptance test before considering any additional context
   mechanism changes.
