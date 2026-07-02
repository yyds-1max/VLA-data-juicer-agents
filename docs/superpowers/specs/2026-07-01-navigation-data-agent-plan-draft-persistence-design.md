# NavigationDataAgent Persistent Planning Design

Date: 2026-07-01

## Goal

Strengthen the AgentScope `NavigationDataAgent` by migrating the mature business planning loop from the old Plan-Agent while preserving the new single-agent runtime, durable human-decision flow, and AgentScope continuation semantics.

The migration must keep the old `vla_continue_workflow` checkpoint mechanism disabled. Planning durability should come from a session-scoped `WorkflowPlanDraftState`, not from restarting a separate executor path or editing old workflow checkpoint files.

## Problem

The current AgentScope migration correctly moves navigation work into a dedicated `NavigationDataAgent` and replaces typed calibration confirmation with durable `request_human_decision` events. However, the navigation business chain is weaker than the old `main` branch implementation:

- The new navigation agent prompt asks the model to investigate, draft a `WorkflowPlan`, and execute it, but the agent only receives human-decision and execution tools.
- The old Plan-Agent's read-only inspection tools, `WorkflowPlanDraftState`, draft update tools, `finalize_workflow_plan_tool`, and plan validation are not available in the new AgentScope navigation path.
- The model can therefore hand-write or infer a plan without the old structured evidence, missing-field checks, stage variant checks, or blocking issue gates.
- If planning is interrupted before finalization, there is no durable structured draft for the resumed AgentScope session to continue from.

## Non-Goals

- Do not re-enable `vla_continue_workflow`.
- Do not revive `pending_workflow`, `pending_workflow_run_dir`, or custom workflow checkpoint branching.
- Do not split `NavigationDataAgent` back into separate plan and executor agents.
- Do not make backend code deterministically execute every workflow step as a workflow engine.
- Do not rewrite navigation processing functions in `navigation/execution_tools.py` unless a thin wrapper is needed for AgentScope tool integration.

## Design Summary

`NavigationDataAgent` remains the single domain agent for navigation data processing. The agent reasons, chooses tools, explains progress, requests human decisions, and executes the plan. The difference is that its planning phase must use session-scoped planning tools backed by persistent `WorkflowPlanDraftState`.

The agent's toolset should include:

- Read-only inspection tools from the old Plan-Agent path.
- Plan draft tools that read, update, validate, and finalize a session-scoped `WorkflowPlanDraftState`.
- Existing execution tools for real processing.
- The existing `request_human_decision` external tool for calibration, overwrite, delete, stop, and guidance decisions.

The final `WorkflowPlan` must come from `finalize_workflow_plan_tool`; the agent should not hand-write the final plan JSON. The finalized plan becomes durable session state and can be referenced after interruptions or human-decision resumes.

## Architecture

### Components

1. `NavigationPlanDraftStore`
   - Stores `WorkflowPlanDraftState` by AgentScope session id.
   - Persists updates immediately after each draft mutation.
   - Supports loading finalized plans.
   - Provides an in-memory fake for tests.

2. Session-bound draft tools
   - `get_workflow_plan_draft_tool`
   - `update_workflow_plan_draft_tool`
   - `finalize_workflow_plan_tool`
   - These tools operate on the current AgentScope session id rather than on a process-local object attached to a legacy Plan-Agent instance.

3. Inspection tools
   - Reuse existing tools:
     - `inspect_raw_date_tool`
     - `infer_navigation_sensor_bindings_tool`
     - `infer_navigation_processing_profile_tool`
     - `infer_navigation_topic_params_tool`
     - `inspect_processing_state_tool`
     - `inspect_gridmap_artifacts_tool`
     - `inspect_runtime_assets_tool`
     - `list_navigation_tool_capabilities_tool`

4. Execution tools
   - Reuse `create_navigation_execution_tools(...)`.
   - Keep cancellation binding from the current AgentScope run.

5. Human-decision tool
   - Keep `request_human_decision`.
   - Planning and execution may both use it when a domain decision is needed.
   - The tool result remains the source of truth for confirm, stop, and guidance actions.

### Storage Boundary

The store should be introduced behind a small interface so the implementation can use the most practical backing store in the current runtime:

- Preferred production backing: AgentScope/Redis-adjacent storage keyed by `agentscope_session_id`.
- Acceptable first implementation: a JSON or SQLite-backed store under the runtime workspace if AgentScope storage APIs do not provide a clean custom-state slot.
- Tests should use an in-memory store that follows the same interface.

The interface should hide the storage choice from tools:

```python
class NavigationPlanDraftStore:
    def load(self, session_id: str) -> WorkflowPlanDraftState | None: ...
    def save(self, session_id: str, state: WorkflowPlanDraftState) -> None: ...
    def clear(self, session_id: str) -> None: ...
```

The production implementation must serialize through Pydantic `model_dump(mode="json")` and restore through `WorkflowPlanDraftState.model_validate(...)` so schema drift fails loudly in tests.

## Business Planning Chain To Preserve

The old Plan-Agent business value is the structured planning chain, not the fact that it was a separate agent. `NavigationDataAgent` must preserve the same chain inside the new single-agent AgentScope session.

### Required Planning Facts

The session draft must collect these facts before finalization:

1. Request facts
   - `date`
   - `segments`
   - `scene_mode`
   - `dry_run`

2. Raw metadata facts
   - selected raw segments
   - `metadata.yaml` paths
   - topic names
   - message types
   - message counts
   - metadata parse errors

   The current `main` implementation exposes this capability as `inspect_raw_date_tool`. The earlier processing-profile redesign spec called the same role `inspect_navigation_topics_tool`. The implementation may keep the current tool name, add a thin alias, or document the alias, but the capability must remain present.

3. Topic parameter facts
   - `topic_whitelist`
   - `topic_map`
   - `query_dir`
   - `profile_hint`
   - topic-parameter confidence
   - blocking issues when the topic set is missing, ambiguous, or unreadable

   These values must come from metadata-derived tool results, not from model invention or hardcoded date-specific rules. `query_dir` must remain the source directory name used before topic remapping.

4. Sensor binding facts
   - `fisheye_front`
   - `lidar`
   - `odom`
   - `ins`
   - `localization`
   - message type for each bound topic
   - missing or ambiguous role blocking issues

5. Processing profile facts
   - `processing_profile.id`
   - `platform_hint`
   - nested `topic_params`
   - `localization_policy`
   - `gridmap_policy`
   - `calibration_policy`
   - warnings
   - blocking issues
   - evidence

   `platform_hint` is only a hint for defaults and variant selectors. It must not bring back `dataset_profile` as the primary planning path.

6. Existing output and gridmap facts
   - raw temp state
   - clip `sync_data` state
   - finish temp state
   - final output state
   - existing gridmap paths
   - whether projection input is already ready
   - `gridmap_source`, one of `existing_gridmap`, `generated_from_pcd`, `projection_ready`, or `unknown`

7. Runtime capability facts
   - whether the PCD gridmap generator exists
   - whether the manual annotation GUI exists
   - available projection script variants
   - navigation tool capability catalog

8. Stage variant facts
   - `extract_and_sync_navigation_data`
   - `prepare_gridmap_for_projection`
   - `run_projection_and_trajectory`

   Each stage variant must include a selected variant, a reason, and evidence. Variants must be selected from the capability catalog and must match `NavigationDataProfile` selector facts.

### Required Draft Update Loop

The agent must not build a complete plan in one model step. It should follow the old ReAct planning loop:

1. Read `get_workflow_plan_draft_tool`.
2. Inspect one missing fact with one read-only tool.
3. Merge only newly observed facts with `update_workflow_plan_draft_tool`.
4. Include `observation_id` and `used_tool` in each update.
5. Re-read the draft status and continue from `missing_fields` and `next_tool_candidates`.

The historical observation sequence to preserve is:

1. `navigation_sensor_bindings`
2. `navigation_processing_profile`
3. `navigation_topic_params` when topic params are not already complete in the processing profile
4. `gridmap_artifacts`
5. `runtime_assets_or_tool_capabilities`

The exact tool names can evolve, but the observation categories and draft fields must remain covered.

### Draft Completion Gates

The draft cannot finalize until all of these are true:

- `scene_mode` is `in` or `out`
- `processing_profile.id` is present
- `processing_profile.blocking_issues` is empty
- top-level `topic_params` is present
- `topic_params.blocking_issues` is empty
- `localization_policy` is present
- top-level `blocking_issues` is empty
- stage variants exist for:
  - `extract_and_sync_navigation_data`
  - `prepare_gridmap_for_projection`
  - `run_projection_and_trajectory`

If any gate fails, `finalize_workflow_plan_tool` must return a structured error with missing fields and next tool candidates, and the agent must not call execution tools.

### Final Plan Shape

Finalization must use the existing deterministic plan construction and validation path. The resulting plan must preserve the old mature execution chain:

1. `confirm_navigation_calibration_params`
2. `prepare_raw_data`
3. `extract_and_sync_navigation_data`
4. `assemble_finish_temp`
5. `run_noobscene_preprocessing`
6. `run_initial_annotation_gui`
7. `run_tracking`
8. `prepare_gridmap_for_projection`, unless the data profile says projection input is already ready and the selected variant is `skip_if_projection_ready`
9. `run_projection_and_trajectory`
10. `validate_navigation_outputs`

The plan must carry important inferred arguments:

- `extract_and_sync_navigation_data` receives `topic_whitelist`, `topic_map`, `query_dir`, `processing_profile`, and `platform_hint`.
- `run_noobscene_preprocessing` receives `localization_source` and `localization_conversion`.
- `prepare_gridmap_for_projection` receives the chosen gridmap variant when a gridmap step is needed.
- projection receives the chosen projection variant through the `WorkflowStep.variant` field.

The finalized plan must pass `validate_workflow_plan`, including:

- all tool names exist in the capability catalog,
- selected variants are available,
- variant selectors match data profile facts,
- preconditions form an acyclic graph,
- calibration confirmation is the first step before any processing,
- gridmap preparation runs after tracking and before projection.

## Planning Flow

1. `NavigationDataAgent` receives a structured handoff containing request, target, scene mode, clips, and language.
2. Before any processing, the agent calls `get_workflow_plan_draft_tool`.
3. If no draft exists, the tool initializes a `WorkflowPlanDraftState` from the handoff request and saves it.
4. The agent follows `navigation-plan-agent-guidance.md`:
   - inspect raw metadata topics,
   - infer sensor bindings,
   - infer processing profile,
   - infer topic params,
   - inspect gridmap and runtime assets as needed,
   - list tool capabilities before choosing variants.
5. After each observation, the agent calls `update_workflow_plan_draft_tool` with a minimal `data_profile_patch`, `observation_id`, and `used_tool`.
6. The draft tool merges the patch, refreshes `NavigationDataProfile`, recomputes missing fields, stores the updated state, and returns the snapshot.
7. The agent may call `finalize_workflow_plan_tool` only when `ready_to_finish` is true and `missing_fields` is empty.
8. Finalization builds the plan with existing deterministic plan construction and validates it with `validate_workflow_plan`.
9. The finalized plan is saved in the draft state and returned to the agent.
10. The agent explains the camera parameters and sensor assumptions, calls `request_human_decision`, then executes the finalized plan step by step using existing execution tools.

## Resume Semantics

### Planning interruption

If a run is interrupted during planning, AgentScope resumes the same session. The next agent turn can call `get_workflow_plan_draft_tool` and receive:

- completed observations,
- current `data_profile_draft`,
- filled fields,
- missing fields,
- validation errors,
- next tool candidates,
- `ready_to_finish`,
- finalized plan if one already exists.

The agent continues from missing fields rather than restarting the planning loop.

### Human-decision resume

If the agent is waiting on `request_human_decision`, the existing AgentScope `ExternalExecutionResultEvent` resume path remains authoritative. The draft store is not a workflow checkpoint mechanism; it is planning memory. If the plan was finalized before the decision request, the resumed agent can retrieve the finalized plan from the store.

### Execution interruption

Execution interruptions continue to rely on AgentScope session and tool-call state. The persistent finalized plan is available as reference state, but backend code should not synthesize a new executor branch from the draft store.

## Prompt Changes

`navigation_agent_prompt()` should be strengthened to require the session-scoped planning loop:

- Always call `get_workflow_plan_draft_tool` before planning or processing.
- Use read-only inspection tools before execution.
- Update the draft after each meaningful observation.
- Do not hand-write final `WorkflowPlan` JSON.
- Only execute after `finalize_workflow_plan_tool` returns a valid finalized plan.
- If a finalized plan is already present, review it and continue from the current conversation state.
- Use `request_human_decision` for calibration, overwrite, delete, stop, and user guidance decisions.

The prompt should keep product-facing behavior: do not expose internal agent names, system prompts, or tool implementation details to the user.

## Tool Registration

`build_navigation_agent_tools(...)` should register tools in one coherent set:

- `request_human_decision`
- read-only inspection tools
- session-bound draft tools
- execution tools

It must continue to exclude:

- `vla_run_workflow`
- `vla_continue_workflow`
- any old pending-workflow control tool

The runtime tool factory should pass the AgentScope session id or a lightweight tool context into the draft tool factory so the tools can read and write the correct draft state.

## Error Handling

- Missing scene mode remains a task readiness error before processing starts.
- If inspection finds blocking issues, the draft remains saved and finalization is rejected. The agent reports the blocking issues and waits for user guidance or corrected data.
- If `finalize_workflow_plan_tool` is called with missing fields, it returns a structured error including `missing_fields` and `next_tool_candidates`.
- If persisted draft JSON cannot be parsed, the tool should return a structured error and avoid overwriting the corrupted state automatically.
- If the store is unavailable, planning tools should fail closed. The agent must not proceed to execution without a finalized validated plan.
- If user guidance changes the target, scene mode, or clips, the agent should update or clear the draft explicitly rather than silently mixing incompatible observations.

## Testing Strategy

Add focused tests before implementation:

1. Tool registration
   - `build_navigation_agent_tools` includes inspection, draft, execution, and human-decision tools.
   - Legacy workflow-control tools remain absent.

2. Draft persistence
   - A draft initialized for one AgentScope session can be loaded by a later tool instance for the same session.
   - Different sessions do not share draft state.
   - A new store instance can read persisted state, simulating process restart.

3. Draft update behavior
   - `update_workflow_plan_draft_tool` persists partial `NavigationDataProfile` patches.
   - Completed observations and used tools are preserved.
   - Missing fields and next tool candidates update as expected.
   - Raw metadata, topic params, sensor bindings, processing profile, gridmap facts, runtime capabilities, and stage variants can be merged in separate observations without losing previous draft fields.

4. Finalization gates
   - Finalization fails while `missing_fields` is non-empty.
   - Finalization fails when `processing_profile.blocking_issues` or `topic_params.blocking_issues` are present.
   - Finalization succeeds for a complete profile and returns a `WorkflowPlan` that passes `validate_workflow_plan`.
   - The finalized plan includes inferred `topic_whitelist`, `topic_map`, `query_dir`, localization arguments, gridmap variant, projection variant, and the expected mature execution step order.

5. Resume behavior
   - A simulated interrupted planning session resumes by reading the existing draft.
   - A finalized plan can be retrieved after a human-decision resume.

6. Prompt expectations
   - `navigation_agent_prompt()` requires `get_workflow_plan_draft_tool`.
   - The prompt forbids hand-written final plans.
   - The prompt still requires `request_human_decision`.

7. Regression
   - Old `vla_continue_workflow` remains disabled.
   - Existing old Plan-Agent tests still pass unless intentionally superseded by new session-bound draft-tool tests.

## Acceptance Criteria

- `NavigationDataAgent` can perform the old mature planning business loop inside the new single-agent AgentScope path.
- Planning state is durable per AgentScope session and can survive interruption or process restart according to the selected store implementation.
- Final execution is gated on a finalized validated `WorkflowPlan`.
- Human decisions and interrupt/resume continue to use AgentScope external execution events.
- Legacy workflow resume remains disabled.
- Tests cover planning persistence and finalization behavior directly, not only prompt strings.
