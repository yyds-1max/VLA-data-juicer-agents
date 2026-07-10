# Navigation Model-Authored Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace mutable profile drafting and deterministic plan generation with task-scoped observations, one-shot model-authored JSON plans, atomic validation/persistence, and plan-bound execution with bounded context.

**Architecture:** Persist facts, immutable plan revisions, and execution state behind separate interfaces that share the existing navigation SQLite database. Every task entry reconciles artifacts before phase selection; the model receives a bounded phase context, submits one complete typed plan, and then invokes wrappers that load canonical arguments from that stored plan.

**Tech Stack:** Python 3.11+, Pydantic 2.7+, SQLite, AgentScope 2.0.1, pytest 8.2+, existing FastAPI/Redis web runtime.

## Global Constraints

- The model is the sole author of time-sync, sensor-binding, localization, calibration, gridmap, step, variant, and business-parameter decisions.
- Code may record measurements, candidates, identifiers, timestamps, revisions, fixed capability metadata, and execution status; it may not fill semantic plan decisions.
- Every Pydantic input model added here uses `ConfigDict(extra="forbid")`.
- Each phase uses one complete JSON submission. Invalid submissions never mutate the active plan or execution ledger and are retried with a complete replacement, never a patch.
- Every task entry reconciles raw, intermediate, and final artifacts before choosing the phase or step from which to continue.
- Keep `ContextConfig(tool_result_limit=6000)` unchanged. Do not modify AgentScope compression and do not rotate sub-sessions.
- Enforce response maxima from the design: planning context/evidence at most 5,500 characters, validation failure at most 3,000, all other routine tool summaries at most 4,000.
- Do not retain compatibility aliases, deprecated wrappers, commented legacy code, or unused legacy tests after the new path is connected.
- During Tasks 1–8, unchanged legacy consumers may keep using their existing symbols so every intermediate commit keeps the full suite green; new runtime code must not depend on those symbols, and Task 9 deletes them all.
- Preserve current cancellation, human-decision, dry-run, task reconciliation, Web/AgentScope handoff, and user-language behavior.
- Use TDD for every task and make one focused commit after its focused tests pass.

## Final File Structure

New focused modules:

- `src/vla_data_juicer_agents/navigation/observation_models.py`: typed factual observations and public evidence descriptors.
- `src/vla_data_juicer_agents/navigation/evidence_store.py`: task-scoped JSON evidence files, bounded field selection, and pagination.
- `src/vla_data_juicer_agents/navigation/observation_store.py`: SQLite observation revisions and evidence metadata.
- `src/vla_data_juicer_agents/navigation/planning_context.py`: required-observation checklist, compact projection, and context revision hash.
- `src/vla_data_juicer_agents/navigation/observation_tools.py`: investigation adapters and read-only cognitive AgentScope tools.
- `src/vla_data_juicer_agents/navigation/plan_models.py`: normalized decisions, discriminated steps, plan records, attempts, and validation issues.
- `src/vla_data_juicer_agents/navigation/plan_store.py`: immutable plans, submission audit, and atomic ledger initialization.
- `src/vla_data_juicer_agents/navigation/plan_submission_tools.py`: phase-specific typed submission tools and compact responses.
- `src/vla_data_juicer_agents/navigation/plan_execution.py`: plan argument resolution, ledger views, and plan-bound wrappers.
- `src/vla_data_juicer_agents/navigation/services.py`: construction of task/observation/evidence/plan services for Web and direct workflow entry points.
- `src/vla_data_juicer_agents/navigation/context_budget.py`: serialized response-size enforcement.

Existing modules retained and narrowed:

- `task_state.py`, `task_store.py`, `task_reconciliation.py`, `task_tools.py`: durable task entry/reconciliation and execution ledger.
- `inspection.py`: raw factual inspection functions only.
- `catalog.py`: versioned capability/action contracts.
- `plan_validation.py`: validation of the new plan contracts only.
- `execution_tools.py`: underlying processing functions; no planning decisions.
- `agent_tools.py`: phase-aware tool resolver and hard execution gates.
- `agents.py`, `workflow.py`, `runtime/agentscope_runtime.py`, `runtime/agentscope_prompts.py`: thin integrations with durable state.

Deleted after migration:

- `src/vla_data_juicer_agents/navigation/plan_draft.py`
- `src/vla_data_juicer_agents/navigation/plan_draft_store.py`
- `src/vla_data_juicer_agents/navigation/session_plan_draft_tools.py`
- legacy draft/profile tests that have no new-contract behavior to preserve

---

### Task 1: Define Factual Observation and Complete Plan Contracts

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/observation_models.py`
- Create: `src/vla_data_juicer_agents/navigation/plan_models.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_state.py:19-90`
- Modify: `src/vla_data_juicer_agents/navigation/catalog.py:1-170`
- Create: `tests/test_navigation_observation_models.py`
- Create: `tests/test_navigation_plan_contracts.py`

**Interfaces:**
- Produces: `ObservationPayload`, `NavigationObservationRevision`, `EvidenceWrite`, `EvidenceDescriptor`, `ExtractSyncPlanInput`, `FinishProcessingPlanInput`, `NavigationPlanRecord`, `PlanSubmissionAttempt`, `PlanValidationIssue`, and `CAPABILITY_CATALOG_REVISION`.
- Consumes: `NavigationArtifactSnapshot`, existing tool names/variants, and Pydantic 2 discriminated unions.

- [ ] **Step 1: Write failing strict-observation tests**

```python
def test_observation_payloads_forbid_policy_fields():
    with pytest.raises(ValidationError):
        ArtifactStateObservation.model_validate({
            "kind": "artifact_state",
            "snapshot": _snapshot(),
            "localization_policy": {"source": "odom"},
        })


def test_unavailable_resource_still_completes_observation_kind():
    revision = NavigationObservationRevision(
        task_id="nav-1",
        revision=1,
        phase="finish_processing",
        completed_kinds=["runtime_assets"],
        payloads=[RuntimeAssetsObservation(
            pcd_gridmap_tool_available=False,
            manual_annotation_gui_available=False,
            projection_variants={},
        )],
    )
    assert revision.completed_kinds == ["runtime_assets"]
```

- [ ] **Step 2: Write failing complete-plan contract tests**

```python
def test_extract_plan_has_one_source_for_topic_and_sync_decisions():
    schema = ExtractSyncPlanInput.model_json_schema()
    text = json.dumps(schema)
    assert "processing_profile" not in text
    assert "stage_variants" not in text
    assert "blocking_issues" not in text


def test_nested_plan_models_forbid_extra_fields():
    payload = valid_extract_plan_payload()
    payload["decisions"]["time_sync"]["invented"] = True
    with pytest.raises(ValidationError):
        ExtractSyncPlanInput.model_validate(payload)
```

- [ ] **Step 3: Run the contract tests and confirm they fail**

Run: `pytest tests/test_navigation_observation_models.py tests/test_navigation_plan_contracts.py -q`

Expected: collection fails because the new modules do not exist.

- [ ] **Step 4: Implement strict observation models**

Add the following concrete shape to `observation_models.py`:

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ObservationKind = Literal[
    "raw_metadata", "sensor_candidates", "topic_candidates",
    "artifact_state", "gridmap_artifacts", "runtime_assets",
    "calibration_inventory", "localization_sources", "user_guidance",
]


class TopicMeasurement(StrictModel):
    topic: str
    message_type: str | None = None
    message_count: int = 0
    frequency_hz: float | None = None
    time_range: tuple[float, float] | None = None
    timestamp_jitter_ms: float | None = None
    missing_ratio: float | None = None


class SensorRoleCandidate(StrictModel):
    role: Literal["fisheye_front", "lidar", "odom", "ins", "localization"]
    topic: str
    message_type: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class RawMetadataObservation(StrictModel):
    kind: Literal["raw_metadata"] = "raw_metadata"
    segments: list[str]
    topics: list[TopicMeasurement]


class SensorCandidatesObservation(StrictModel):
    kind: Literal["sensor_candidates"] = "sensor_candidates"
    candidates: list[SensorRoleCandidate]


class TopicCandidatesObservation(StrictModel):
    kind: Literal["topic_candidates"] = "topic_candidates"
    available_topics: list[str]
    suggested_role_names: dict[str, list[str]]


class ArtifactStateObservation(StrictModel):
    kind: Literal["artifact_state"] = "artifact_state"
    snapshot: NavigationArtifactSnapshot


class GridmapArtifactsObservation(StrictModel):
    kind: Literal["gridmap_artifacts"] = "gridmap_artifacts"
    existing_gridmap_paths: list[str] = Field(default_factory=list)
    pcd_sources: list[str] = Field(default_factory=list)
    projection_ready: bool = False


class RuntimeAssetsObservation(StrictModel):
    kind: Literal["runtime_assets"] = "runtime_assets"
    pcd_gridmap_tool_available: bool
    manual_annotation_gui_available: bool
    projection_variants: dict[str, bool]


class CalibrationInventoryObservation(StrictModel):
    kind: Literal["calibration_inventory"] = "calibration_inventory"
    sensor_sources: list[str]


class LocalizationSourcesObservation(StrictModel):
    kind: Literal["localization_sources"] = "localization_sources"
    available_sources: list[Literal["odom", "ins"]]
    conversion_available: bool


class UserGuidanceObservation(StrictModel):
    kind: Literal["user_guidance"] = "user_guidance"
    guidance_revision: int
    text: str


ObservationPayload = Annotated[
    RawMetadataObservation | SensorCandidatesObservation |
    TopicCandidatesObservation | ArtifactStateObservation |
    GridmapArtifactsObservation | RuntimeAssetsObservation |
    CalibrationInventoryObservation | LocalizationSourcesObservation |
    UserGuidanceObservation,
    Field(discriminator="kind"),
]


class EvidenceWrite(StrictModel):
    kind: str
    source_tool: str
    payload: dict[str, Any] | list[Any]
    summary: str = Field(max_length=500)
```

Define the public evidence and revision types without a filesystem path:

```python
class EvidenceDescriptor(StrictModel):
    ref: str
    task_id: str
    observation_revision: int
    kind: str
    summary: str
    byte_size: int = Field(ge=0)
    source_tool: str
    created_at: str


class NavigationObservationRevision(StrictModel):
    task_id: str
    revision: int = Field(ge=1)
    phase: NavigationTaskPhase
    completed_kinds: list[ObservationKind]
    payloads: list[ObservationPayload]
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
```

In `task_state.py`, add `NEEDS_REPLAN = "needs_replan"`, `dry_run: bool = False`, and `guidance_revision: int = 0` to the domain model now so Task 2 can hash a stable typed planning context. Task 4 adds their SQLite persistence and reconciliation behavior.

- [ ] **Step 5: Implement normalized decisions and discriminated steps**

Use a common strict base and the following canonical fields in `plan_models.py`:

```python
class DecisionBase(StrictModel):
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1)


class SensorBindingDecision(DecisionBase):
    bindings: dict[Literal["fisheye_front", "lidar", "odom", "ins", "localization"], str]


class TopicSelectionDecision(DecisionBase):
    topic_whitelist: list[str] = Field(min_length=1)
    topic_map: dict[str, str] = Field(min_length=1)
    query_dir: str


class TimeSyncDecision(DecisionBase):
    reference_sensor: str
    method: Literal["nearest_timestamp"]
    tolerance_ms: int = Field(gt=0, le=1000)


class LocalizationDecision(DecisionBase):
    source: Literal["odom", "ins"]
    conversion: Literal["odom_to_ins", "none"]


class GridmapDecision(DecisionBase):
    source: Literal["existing_gridmap", "generated_from_pcd", "projection_ready"]


class CalibrationDecision(DecisionBase):
    mode: Literal["hardcoded_with_user_confirmation", "selected_profile"]
    selected_sensor_source: str
    requires_user_confirmation: bool


class EmptyArguments(StrictModel):
    pass


class ExtractSyncArguments(StrictModel):
    processes_num: int = Field(default=4, ge=1, le=64)


class StepBase(StrictModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    depends_on: list[str] = Field(default_factory=list)
    failure_policy: Literal["stop"] = "stop"
    decision_refs: list[str] = Field(default_factory=list)


class PrepareRawStep(StepBase):
    action: Literal["prepare_raw_data"] = "prepare_raw_data"
    variant: Literal["default"] = "default"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class ExtractSyncStep(StepBase):
    action: Literal["extract_and_sync_navigation_data"] = "extract_and_sync_navigation_data"
    variant: Literal["explicit_topic_params"] = "explicit_topic_params"
    arguments: ExtractSyncArguments = Field(default_factory=ExtractSyncArguments)


class ConfirmCalibrationStep(StepBase):
    action: Literal["confirm_navigation_calibration_params"] = "confirm_navigation_calibration_params"
    variant: Literal["default"] = "default"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class AssembleFinishTempStep(StepBase):
    action: Literal["assemble_finish_temp"] = "assemble_finish_temp"
    variant: Literal["default"] = "default"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class NoobscenePreprocessingStep(StepBase):
    action: Literal["run_noobscene_preprocessing"] = "run_noobscene_preprocessing"
    variant: Literal["default"] = "default"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class InitialAnnotationStep(StepBase):
    action: Literal["run_initial_annotation_gui"] = "run_initial_annotation_gui"
    variant: Literal["human_gui"] = "human_gui"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class TrackingStep(StepBase):
    action: Literal["run_tracking"] = "run_tracking"
    variant: Literal["default"] = "default"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class PrepareGridmapStep(StepBase):
    action: Literal["prepare_gridmap_for_projection"] = "prepare_gridmap_for_projection"
    variant: Literal["copy_existing_gridmap", "generate_from_pcd", "skip_if_projection_ready"]
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class ProjectionStep(StepBase):
    action: Literal["run_projection_and_trajectory"] = "run_projection_and_trajectory"
    variant: Literal["cjl_with_gridmap", "cjl_0525_with_gridmap"]
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


class ValidateOutputsStep(StepBase):
    action: Literal["validate_navigation_outputs"] = "validate_navigation_outputs"
    variant: Literal["expect_gridmap"] = "expect_gridmap"
    arguments: EmptyArguments = Field(default_factory=EmptyArguments)


ExtractSyncStepInput = Annotated[
    PrepareRawStep | ExtractSyncStep,
    Field(discriminator="action"),
]
FinishProcessingStepInput = Annotated[
    ConfirmCalibrationStep | AssembleFinishTempStep |
    NoobscenePreprocessingStep | InitialAnnotationStep | TrackingStep |
    PrepareGridmapStep | ProjectionStep | ValidateOutputsStep,
    Field(discriminator="action"),
]
```

Define the phase inputs exactly once, without request metadata or legacy profile copies:

```python
class ExtractSyncDecisions(StrictModel):
    sensor_bindings: SensorBindingDecision
    topic_selection: TopicSelectionDecision
    time_sync: TimeSyncDecision


class FinishProcessingDecisions(StrictModel):
    localization: LocalizationDecision
    gridmap: GridmapDecision
    calibration: CalibrationDecision


class ExtractSyncPlanInput(StrictModel):
    decisions: ExtractSyncDecisions
    steps: list[ExtractSyncStepInput] = Field(min_length=1)


class FinishProcessingPlanInput(StrictModel):
    decisions: FinishProcessingDecisions
    steps: list[FinishProcessingStepInput] = Field(min_length=1)
```

Do not include date, segments, scene mode, platform hint, warnings, blocking issues, or a processing-profile copy.

Add these persistence/audit types in the same module:

```python
class PlanValidationIssue(StrictModel):
    path: str
    code: str
    message: str
    allowed_values: list[str] = Field(default_factory=list)


class PlanValidationReport(StrictModel):
    ok: bool
    errors: list[PlanValidationIssue] = Field(default_factory=list)
    warnings: list[PlanValidationIssue] = Field(default_factory=list)


class NavigationPlanRecord(StrictModel):
    plan_id: str
    task_id: str
    phase: Literal["extract_sync", "finish_processing"]
    plan_revision: int
    contract_version: str
    observation_revision: int
    status: Literal["active", "superseded", "completed", "invalidated"]
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput
    created_at: str


class PlanSubmissionAttempt(StrictModel):
    attempt_id: str
    task_id: str
    phase: Literal["extract_sync", "finish_processing"]
    planning_context_revision: str
    candidate: dict[str, Any]
    validation: PlanValidationReport
    created_at: str


class ExecutionStepRecord(StrictModel):
    id: str
    plan_id: str
    plan_revision: int
    sequence: int
    step_id: str
    action: str
    status: Literal["pending", "running", "waiting_user", "completed", "failed", "needs_replan"]
    result_summary: dict[str, Any] | None = None
    result_ref: str | None = None
    retry_count: int = 0


class PlanExecutionOverview(StrictModel):
    plan_id: str
    plan_revision: int
    status: str
    total_steps: int
    completed_steps: int
    current_step_id: str | None
    steps: list[ExecutionStepRecord]
```

- [ ] **Step 6: Version and enrich the capability catalog**

Set `CAPABILITY_CATALOG_REVISION = "navigation-capabilities-v2"`. Add `phase`, `argument_model`, and `declared_output_kinds` to `ToolCapability`. Add factual `inspect_navigation_sensor_candidates` and `inspect_navigation_topic_candidates` entries for the new path. Keep existing semantic entries only while unchanged legacy consumers still require them; Task 9 removes them. Exclude `run_tracking_and_projection` from the v2 planning view so the model uses explicit tracking, gridmap, and projection steps.

- [ ] **Step 7: Run focused tests**

Run: `pytest tests/test_navigation_observation_models.py tests/test_navigation_plan_contracts.py tests/test_navigation_catalog.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/observation_models.py src/vla_data_juicer_agents/navigation/plan_models.py src/vla_data_juicer_agents/navigation/task_state.py src/vla_data_juicer_agents/navigation/catalog.py tests/test_navigation_observation_models.py tests/test_navigation_plan_contracts.py tests/test_navigation_catalog.py
git commit -m "feat: define navigation observation and plan contracts"
```

---

### Task 2: Persist Evidence and Observation Revisions and Project Bounded Context

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/context_budget.py`
- Create: `src/vla_data_juicer_agents/navigation/evidence_store.py`
- Create: `src/vla_data_juicer_agents/navigation/observation_store.py`
- Create: `src/vla_data_juicer_agents/navigation/planning_context.py`
- Create: `tests/test_navigation_evidence_store.py`
- Create: `tests/test_navigation_observation_store.py`
- Create: `tests/test_navigation_planning_context.py`

**Interfaces:**
- Consumes: Task ids/phases and Task 1 observation models.
- Produces: `FileNavigationEvidenceStore`, `SqliteNavigationObservationStore`, `PhasePlanningContext`, `build_phase_planning_context(*, task, observation, evidence, capabilities) -> PhasePlanningContext`, `compute_planning_context_revision(*, task, observation_revision, capability_revision) -> str`, and `ensure_payload_within_limit(payload, *, max_chars, label) -> dict`.

- [ ] **Step 1: Write failing persistence and isolation tests**

```python
def test_evidence_read_is_task_scoped_and_paginated(tmp_path):
    store = FileNavigationEvidenceStore(tmp_path / "evidence")
    descriptor = store.write("nav-1", 1, "topics", "inspect_raw_date_tool", {"rows": list(range(20))}, "20 rows")
    page = store.read("nav-1", descriptor.ref, fields=["rows"], cursor=5, limit=3)
    assert page["data"] == {"rows": [5, 6, 7]}
    assert page["next_cursor"] == 8
    with pytest.raises(PermissionError):
        store.read("nav-2", descriptor.ref)


def test_observation_revision_is_monotonic(tmp_path):
    store = SqliteNavigationObservationStore(tmp_path / "state.sqlite")
    evidence = FileNavigationEvidenceStore(tmp_path / "evidence")
    first = store.append("nav-1", "extract_sync", "raw_metadata", [_raw_observation()], [], evidence)
    second = store.append("nav-1", "extract_sync", "sensor_candidates", [_sensor_observation()], [], evidence)
    assert (first.revision, second.revision) == (1, 2)
```

- [ ] **Step 2: Write failing bounded-context tests**

```python
def test_planning_context_excludes_raw_evidence_and_schema(tmp_path):
    context = build_phase_planning_context(task=_task(), observation=_revision(), capabilities=_caps())
    payload = context.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)
    assert "phase_profile_schema" not in text
    assert "data_profile_draft" not in text
    assert "raw_payload" not in text
    assert len(text) <= 5_500
```

- [ ] **Step 3: Run tests and verify failure**

Run: `pytest tests/test_navigation_evidence_store.py tests/test_navigation_observation_store.py tests/test_navigation_planning_context.py -q`

Expected: imports fail for the new stores/context projector.

- [ ] **Step 4: Implement response-size enforcement**

```python
def serialized_chars(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def ensure_payload_within_limit(payload: dict[str, Any], *, max_chars: int, label: str) -> dict[str, Any]:
    size = serialized_chars(payload)
    if size > max_chars:
        raise ValueError(f"{label} exceeds {max_chars} characters: {size}")
    return payload
```

- [ ] **Step 5: Implement the file evidence store**

Store canonical JSON at `<root>/<task_id>/<revision>/<evidence_id>.json`, reject refs whose indexed task differs, expose no path in `EvidenceDescriptor`, select only top-level requested fields, paginate list-valued selected fields, and enforce 5,500 characters on public reads. Use atomic temp-file replacement for writes.

- [ ] **Step 6: Implement the SQLite observation store**

Create `navigation_observation_revisions` and `navigation_evidence` tables. `append(task_id, phase, completed_kind, payloads, evidence_writes, evidence_store)` opens `BEGIN IMMEDIATE`, obtains `MAX(revision)+1` for the task, writes each `EvidenceWrite(kind, source_tool, payload, summary)` using that allocated revision, carries forward prior `completed_kinds`, inserts the full typed revision JSON and evidence metadata, then commits. On rollback, delete files written for the failed revision. Implement `latest(task_id)`, `get(task_id, revision)`, and filtered evidence metadata listing.

- [ ] **Step 7: Implement planning-context projection and revision hashing**

```python
class ObservationStatus(StrictModel):
    complete: bool
    required: list[str]
    completed: list[str]
    missing: list[str]


class PhasePlanningContext(StrictModel):
    task_id: str
    phase: Literal["extract_sync", "finish_processing"]
    planning_context_revision: str
    observation_status: ObservationStatus
    fact_summary: dict[str, Any]
    available_action_ids: list[str]
    evidence_catalog: list[EvidenceDescriptor]


PHASE_REQUIRED_OBSERVATIONS: dict[str, tuple[ObservationKind, ...]] = {
    "extract_sync": (
        "artifact_state", "raw_metadata", "sensor_candidates", "topic_candidates",
    ),
    "finish_processing": (
        "artifact_state", "gridmap_artifacts", "runtime_assets",
        "calibration_inventory", "localization_sources",
    ),
}


def compute_planning_context_revision(*, task: NavigationTask, observation_revision: int, capability_revision: str) -> str:
    payload = {
        "task_id": task.task_id,
        "date": task.date,
        "segments": task.segments,
        "scene_mode": task.scene_mode,
        "phase": task.phase.value,
        "guidance_revision": task.guidance_revision,
        "observation_revision": observation_revision,
        "capability_revision": capability_revision,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
```

`build_phase_planning_context` includes only active-phase compact facts, required/completed/missing observation kinds, action ids, evidence summaries, and the hash. Before returning, it calls `ensure_payload_within_limit(context.model_dump(mode="json"), max_chars=5_500, label="planning_context")`.

- [ ] **Step 8: Run focused tests**

Run: `pytest tests/test_navigation_evidence_store.py tests/test_navigation_observation_store.py tests/test_navigation_planning_context.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/context_budget.py src/vla_data_juicer_agents/navigation/evidence_store.py src/vla_data_juicer_agents/navigation/observation_store.py src/vla_data_juicer_agents/navigation/planning_context.py tests/test_navigation_evidence_store.py tests/test_navigation_observation_store.py tests/test_navigation_planning_context.py
git commit -m "feat: persist navigation observations and bounded evidence"
```

---

### Task 3: Replace Semantic Inference with Factual Inspection and Cognitive Tools

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/observation_tools.py`
- Modify: `src/vla_data_juicer_agents/navigation/inspection.py:140-625`
- Modify: `src/vla_data_juicer_agents/navigation/catalog.py`
- Modify: `tests/test_navigation_inspection.py`
- Create: `tests/test_navigation_observation_tools.py`

**Interfaces:**
- Consumes: Task 2 stores/projector and existing raw/artifact inspection functions.
- Produces: `build_navigation_observation_tools(*, task, observation_store, evidence_store, settings) -> list[FunctionTool]` with compact inspection and cognitive tools.

- [ ] **Step 1: Write failing tests that separate facts from choices**

```python
def test_sensor_candidate_inspection_does_not_select_binding(settings):
    result = inspect_navigation_sensor_candidates("20270605", settings=settings)
    assert result.candidates
    assert not hasattr(result, "sensor_bindings")


def test_topic_candidate_inspection_does_not_select_final_params(settings):
    result = inspect_navigation_topic_candidates("20270605", settings=settings)
    payload = result.model_dump(mode="json")
    assert "topic_whitelist" not in payload
    assert "topic_map" not in payload
    assert "query_dir" not in payload
```

- [ ] **Step 2: Write failing compact-tool tests**

Call the built tool functions through `tool.wrapped_func` or the existing test helper and assert an inspection result contains only `ok`, `observation_delta`, `evidence_refs`, `observation_revision`, and `remaining_missing_observations`; assert serialized size is at most 4,000 characters.

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `pytest tests/test_navigation_inspection.py tests/test_navigation_observation_tools.py -q`

Expected: the factual inspection names and tool builder are missing.

- [ ] **Step 4: Refactor inspection functions**

Add/refactor the new factual functions:

```python
inspect_navigation_sensor_candidates
inspect_navigation_topic_candidates
```

Return `SensorCandidatesObservation` and `TopicCandidatesObservation`. Extract measurable platform-topic, calibration-file, localization-source, and runtime-asset facts into raw metadata, runtime assets, or artifact observation payloads. Keep the old `infer_*` functions unchanged only for legacy callers until Task 9 deletes them; no new tool builder or runtime path may expose them.

- [ ] **Step 5: Implement observation tool adapters**

`build_navigation_observation_tools(task, observation_store, evidence_store, settings)` returns closures for raw metadata, sensor candidates, topic candidates, artifact state, gridmap artifacts, runtime assets, calibration inventory, and localization-source inventory. Each closure:

1. calls the read-only inspector;
2. writes the full raw payload to evidence;
3. appends one typed observation revision;
4. projects only a small delta;
5. enforces the 4,000-character inspection limit.

- [ ] **Step 6: Implement cognitive tools**

Add closures named `get_phase_planning_context_tool`, `list_observation_evidence_tool`, `read_observation_evidence_tool`, and `describe_processing_action_tool`. Bind the active task in the closure so the model cannot supply another task id. Evidence list/read must enforce task ownership and pagination; action description returns only the requested active-phase capability.

- [ ] **Step 7: Run focused tests**

Run: `pytest tests/test_navigation_inspection.py tests/test_navigation_observation_tools.py tests/test_navigation_catalog.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/inspection.py src/vla_data_juicer_agents/navigation/observation_tools.py src/vla_data_juicer_agents/navigation/catalog.py tests/test_navigation_inspection.py tests/test_navigation_observation_tools.py tests/test_navigation_catalog.py
git commit -m "feat: expose factual navigation inspection tools"
```

---

### Task 4: Make Artifact Reconciliation the Unconditional Task Entry Gate

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/task_state.py:1-110`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py:70-530`
- Modify: `src/vla_data_juicer_agents/navigation/task_reconciliation.py:20-230`
- Modify: `src/vla_data_juicer_agents/navigation/task_tools.py:20-220`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py:118-320`
- Modify: `tests/test_navigation_task_reconciliation.py`
- Modify: `tests/test_navigation_task_store.py`
- Modify: `tests/test_navigation_task_tools.py`
- Modify: `tests/test_web_agentscope_session.py`

**Interfaces:**
- Produces: `prepare_navigation_task_entry(*, task_store, observation_store, evidence_store, message, web_session_id, agentscope_session_id, settings) -> NavigationTask`, `find_latest_by_agentscope_session(session_id) -> NavigationTask | None`, persisted task fields `dry_run`/`guidance_revision`, and status `needs_replan`.
- Consumes: existing artifact snapshot builder and structured handoff parser.

- [ ] **Step 1: Write failing phase-selection regression tests**

```python
def test_raw_only_entry_selects_extract_sync(tmp_path):
    _make_raw(tmp_path, "20270623", "segment_a")
    task = _task(phase="completed", status="completed")
    reconciled = reconcile_navigation_task(task, settings=_settings(tmp_path))
    assert reconciled.phase == NavigationTaskPhase.EXTRACT_SYNC
    assert reconciled.status == NavigationTaskStatus.NEEDS_RERUN


def test_existing_sync_selects_finish_processing_when_scene_known(tmp_path):
    _make_raw_and_sync(tmp_path, "20270623", "segment_a")
    task = _task(scene_mode="out")
    reconciled = reconcile_navigation_task(task, settings=_settings(tmp_path))
    assert reconciled.phase == NavigationTaskPhase.FINISH_PROCESSING


def test_valid_final_outputs_select_completed(tmp_path):
    _make_valid_final(tmp_path, "20270623")
    reconciled = reconcile_navigation_task(_task(), settings=_settings(tmp_path))
    assert reconciled.phase == NavigationTaskPhase.COMPLETED
```

- [ ] **Step 2: Run reconciliation tests and confirm failure**

Run: `pytest tests/test_navigation_task_reconciliation.py -q`

Expected: current phase-specific reconciler does not apply these rules from every starting phase.

- [ ] **Step 3: Persist the new task state fields**

Add SQLite columns for the Task 1 `dry_run` and `guidance_revision` fields with idempotent `PRAGMA table_info` migrations and implement `find_latest_by_agentscope_session(session_id)`. Include `NEEDS_REPLAN` in resumable statuses and round-trip all three Task 1 additions through `_task_values`/`_task_from_row`.

- [ ] **Step 4: Rewrite reconciliation as snapshot-first rules**

Apply rules in artifact-dependency order, independent of persisted phase: valid final markers -> completed; incomplete selected sync -> extract-sync/needs-rerun or partial/needs-reconcile; complete sync plus missing scene mode -> waiting-scene-mode; complete sync plus scene mode -> finish-processing; missing raw input -> needs-reconcile with explicit evidence. Persist the new artifact snapshot every time.

- [ ] **Step 5: Replace draft precreation with task entry preparation**

```python
def prepare_navigation_task_entry(
    *, task_store, observation_store, evidence_store,
    message, web_session_id, agentscope_session_id, settings,
):
    handoff = _structured_handoff_payload_from_message(message)
    task = task_store.create_or_update_task(
        date=handoff["date"],
        segments=handoff.get("segments"),
        scene_mode=_navigation_scene_mode_for_request(handoff.get("scene_mode")),
        dry_run=bool(handoff.get("dry_run", False)),
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    )
    reconciled = reconcile_navigation_task(task, settings=settings)
    saved = task_store.update_task(task.task_id, **_changes_without_id(reconciled))
    observation_store.append(
        saved.task_id, saved.phase.value, "artifact_state",
        [ArtifactStateObservation(snapshot=saved.artifact_snapshot)],
        [EvidenceWrite(
            kind="artifact_state",
            source_tool="task_entry_reconciliation",
            payload=saved.artifact_snapshot.model_dump(mode="json"),
            summary="task-entry artifact snapshot",
        )],
        evidence_store,
    )
    return saved
```

When the handoff contains non-empty user guidance, increment `guidance_revision`, write it as `UserGuidanceObservation`, and include its evidence ref in the same entry sequence. Call the helper synchronously in `start_navigation_agent_task` before `_start_agent_run`. Keep draft precreation temporarily for unchanged legacy consumers; Task 8 removes Web/AgentScope draft wiring and Task 9 removes the remaining direct-workflow draft code.

- [ ] **Step 6: Make task tools compact and reconciliation-first**

`get_or_create_navigation_task_tool` must reconcile before returning and return a compact state anchor, not the full task model. `reconcile_navigation_task_tool` uses the same helper. Remove `_sync_finish_processing_draft`.

- [ ] **Step 7: Run focused tests**

Run: `pytest tests/test_navigation_task_reconciliation.py tests/test_navigation_task_store.py tests/test_navigation_task_tools.py tests/test_web_agentscope_session.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/task_state.py src/vla_data_juicer_agents/navigation/task_store.py src/vla_data_juicer_agents/navigation/task_reconciliation.py src/vla_data_juicer_agents/navigation/task_tools.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_navigation_task_reconciliation.py tests/test_navigation_task_store.py tests/test_navigation_task_tools.py tests/test_web_agentscope_session.py
git commit -m "feat: reconcile navigation artifacts at task entry"
```

---

### Task 5: Add Immutable Plan Persistence, Attempt Audit, and Atomic Ledger Creation

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/plan_store.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py`
- Create: `tests/test_navigation_plan_store.py`
- Modify: `tests/test_navigation_task_store.py`

**Interfaces:**
- Produces: `SqliteNavigationPlanRepository.record_attempt(attempt)`, `activate(task, phase, observation_revision, plan)`, `get_active(task_id, phase)`, `get(plan_id)`, `invalidate(plan_id, reason)`, `get_execution_overview(plan_id)`, and `get_current_step(plan_id)`.
- Consumes: Task 1 plan models and the existing `navigation_tasks`/`navigation_task_steps` database.

- [ ] **Step 1: Write failing atomicity tests**

```python
def test_activate_plan_and_ledger_is_atomic(tmp_path):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 3, valid_extract_plan())
    assert repo.get_active(task.task_id, "extract_sync").plan_id == record.plan_id
    assert [step.step_id for step in repo.get_execution_overview(record.plan_id).steps] == ["prepare", "sync"]


def test_failed_activation_does_not_supersede_active_plan(tmp_path, monkeypatch):
    repo, task = stores_with_task(tmp_path)
    first = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    monkeypatch.setattr(repo, "_insert_ledger_rows", lambda *args: (_ for _ in ()).throw(sqlite3.IntegrityError()))
    with pytest.raises(sqlite3.IntegrityError):
        repo.activate(task, "extract_sync", 2, valid_extract_plan())
    assert repo.get_active(task.task_id, "extract_sync").plan_id == first.plan_id
```

- [ ] **Step 2: Write failing submission-audit test**

Assert `record_attempt` stores full candidate/errors and that no public plan lookup returns an invalid candidate.

- [ ] **Step 3: Run tests and confirm failure**

Run: `pytest tests/test_navigation_plan_store.py -q`

Expected: plan repository is missing.

- [ ] **Step 4: Create plan and attempt tables**

Create `navigation_plans` with unique active `(task_id, phase)` semantics and `navigation_plan_submission_attempts`. Add `plan_id`, `plan_revision`, `sequence`, `result_summary_json`, `result_ref`, and `retry_count` columns to `navigation_task_steps`; leave old nullable columns readable until Task 9 removes the legacy writers.

Change the new ledger read/write path to use `ExecutionStepRecord.status` rather than overloading `NavigationTaskStatus`. Task 9 removes the old task-step writer that still uses task statuses.

- [ ] **Step 5: Implement activation in one transaction**

`activate(task, phase, observation_revision, plan)` uses `BEGIN IMMEDIATE`, verifies the task exists, computes the next plan revision, marks the previous active plan superseded, inserts canonical JSON, inserts ordered pending ledger rows, and commits. Roll back all mutations on any exception.

- [ ] **Step 6: Implement compact ledger reads**

Overview returns only plan id/revision, counts, and `{step_id, action, status}`. Current-step read returns one stored step and its plan decision refs. Both enforce the 4,000-character maximum.

- [ ] **Step 7: Run focused tests**

Run: `pytest tests/test_navigation_plan_store.py tests/test_navigation_task_store.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/plan_store.py src/vla_data_juicer_agents/navigation/task_store.py tests/test_navigation_plan_store.py tests/test_navigation_task_store.py
git commit -m "feat: persist immutable navigation plans"
```

---

### Task 6: Validate and Submit Complete Plans Atomically

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/plan_validation.py`
- Create: `src/vla_data_juicer_agents/navigation/plan_submission_tools.py`
- Rewrite: `tests/test_navigation_plan_validation.py`
- Create: `tests/test_navigation_plan_submission_tools.py`

**Interfaces:**
- Produces: `validate_navigation_plan(*, task, observation, plan, evidence, capabilities) -> PlanValidationReport` and `build_navigation_plan_submission_tools(*, task, observation_store, evidence_store, plan_store, capabilities) -> list[FunctionTool]`.
- Consumes: task, current observation/context hash, evidence metadata, capability catalog, plan repository, and complete phase input.

- [ ] **Step 1: Write failing validator tests**

Cover unknown evidence refs, refs from another revision/task, selected topics absent from observations, invalid localization conversion pairs, gridmap source/capability mismatch, duplicate step ids, unknown dependencies, cycles, missing calibration confirmation, gridmap before tracking, projection before gridmap, and validation not last.

Use exact assertions such as:

```python
assert report.errors[0] == PlanValidationIssue(
    path="plan.decisions.time_sync.reference_sensor",
    code="unknown_sensor_role",
    message="Referenced sensor role does not exist",
    allowed_values=["fisheye_front", "lidar", "odom"],
)
```

- [ ] **Step 2: Write failing submission tests including the server regression**

```python
def test_invalid_complete_submission_never_creates_partial_state(services):
    payload = valid_finish_plan_payload()
    del payload["decisions"]["localization"]
    result = call_submit_finish(services, payload)
    assert result["ok"] is False
    assert result["retry"] == "resubmit_complete_plan"
    assert "draft" not in result and "schema" not in result
    assert services.plan_store.get_active(services.task.task_id, "finish_processing") is None


def test_valid_finish_plan_does_not_require_nested_topic_params_copy(services):
    result = call_submit_finish(services, valid_finish_plan_payload())
    assert result["ok"] is True
    assert "workflow_plan_json" not in result
```

- [ ] **Step 3: Run tests and confirm failure**

Run: `pytest tests/test_navigation_plan_validation.py tests/test_navigation_plan_submission_tools.py -q`

Expected: new validation/submission interfaces are missing.

- [ ] **Step 4: Implement validation in stable stages**

Return `PlanValidationReport(ok, errors, warnings)` with errors deduplicated by `(path, code)`, sorted, and capped at eight public issues. Validate phase/task/context first, then evidence ownership, observed references, action/variant arguments, dependencies, and business order. The validator never mutates stores.

- [ ] **Step 5: Implement typed submission closures**

Create separate functions whose signatures contain the complete Pydantic model:

```python
def submit_extract_sync_plan_tool(
    planning_context_revision: str,
    plan: ExtractSyncPlanInput,
) -> dict[str, Any]:
    return _submit_complete_plan(
        phase="extract_sync",
        planning_context_revision=planning_context_revision,
        plan=plan,
    )


def submit_finish_processing_plan_tool(
    planning_context_revision: str,
    plan: FinishProcessingPlanInput,
) -> dict[str, Any]:
    return _submit_complete_plan(
        phase="finish_processing",
        planning_context_revision=planning_context_revision,
        plan=plan,
    )
```

Implement `_submit_complete_plan` once. It validates the bound task/current context, calls `validate_navigation_plan`, records a submission attempt, returns `ok`/`error_type`/compact `errors`/`retry` on failure with the 3,000-character limit, or calls `plan_store.activate` and returns only `plan_id`, `plan_revision`, `step_count`, `status`, and `next_action` on success.

- [ ] **Step 6: Run focused tests**

Run: `pytest tests/test_navigation_plan_validation.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_store.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/plan_validation.py src/vla_data_juicer_agents/navigation/plan_submission_tools.py tests/test_navigation_plan_validation.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_store.py
git commit -m "feat: submit complete navigation plans atomically"
```

---

### Task 7: Bind Processing Execution to the Stored Plan and Ledger

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/plan_execution.py`
- Modify: `src/vla_data_juicer_agents/navigation/execution_tools.py:346-1390`
- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py:48-425`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py:300-430`
- Create: `tests/test_navigation_plan_execution.py`
- Modify: `tests/test_navigation_execution_tools_dry_run.py`
- Modify: `tests/test_navigation_agent_tools.py`
- Modify: `tests/test_web_human_decision_api.py`

**Interfaces:**
- Produces: `resolve_step_arguments(*, task, plan, step, settings) -> dict[str, Any]`, `build_plan_bound_execution_tools(*, task, plan_store, evidence_store, settings, dry_run, cancellation) -> list[ToolBase]`, and ledger transition methods.
- Consumes: active plan/step, task metadata, underlying processing functions, cancellation, dry-run, and human-decision delivery.

- [ ] **Step 1: Write failing canonical-argument tests**

```python
def test_extract_wrapper_loads_topics_from_plan(monkeypatch, services):
    captured = {}
    monkeypatch.setattr(plan_execution, "extract_and_sync_navigation_data", lambda **kwargs: captured.update(kwargs) or ok_result())
    tool = tool_named(build_plan_bound_execution_tools(services), "extract_and_sync_navigation_data_tool")
    call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")
    assert captured["date"] == services.task.date
    assert captured["segments"] == services.task.segments
    assert captured["topic_whitelist"] == services.plan.plan.decisions.topic_selection.topic_whitelist
```

- [ ] **Step 2: Write failing gate/ledger tests**

Assert wrong plan, non-current step, unmet dependency, action/tool mismatch, inactive revision, and reconciled artifact invalidation return compact errors without invoking the underlying function. Assert success/failure transitions are idempotent and persist full result by ref but only a compact summary in the ledger.

- [ ] **Step 3: Write failing external calibration test**

Assert the current calibration step exposes `request_human_decision` with only `plan_id`/`step_id`; the server derives the summary from the stored calibration decision, and `submit_human_decision` completes/fails that ledger step exactly once.

- [ ] **Step 4: Run tests and confirm failure**

Run: `pytest tests/test_navigation_plan_execution.py tests/test_navigation_agent_tools.py tests/test_web_human_decision_api.py -q`

Expected: plan-bound wrappers and transitions are missing.

- [ ] **Step 5: Implement canonical argument resolution**

Map action to arguments without model re-copying:

- task supplies date, segments, dry-run, and configured root paths;
- extract-sync decision supplies whitelist/map/query dir and step supplies `processes_num`;
- localization decision supplies NoobScenes source/conversion;
- calibration decision supplies selected sensor source;
- gridmap/projection steps supply their selected variants;
- code derives finish temp/final paths under `NavigationSettings`.

Add the new canonical execution arguments and change the v2 calibration/assembly path to accept the validated selected sensor source rather than infer it from platform hint. Keep legacy optional `processing_profile`/`platform_hint` parameters only until unchanged direct workflows migrate in Task 9; remove them in Task 9.

Reject a calibration source unless it exactly matches a `CalibrationInventoryObservation` entry for the plan's observation revision and resolves under the configured processing root. Never accept an arbitrary model-supplied filesystem path.

- [ ] **Step 6: Implement plan-bound wrappers and compact results**

Each named wrapper exposes only `plan_id` and `step_id`, gates against the active/current ledger step, marks running, invokes the underlying function with resolved args, stores the full result in task-scoped result evidence, marks completed/failed, and returns at most 4,000 characters with `next_action`.

Expose wrappers for distinct actions remaining in the active plan so a single AgentScope ReAct turn can proceed through multiple steps; every wrapper still rejects any call except the current executable step.

- [ ] **Step 7: Integrate external human decisions with the ledger**

Replace the free-form summary input with a plan-bound request. Include `plan_id` and `step_id` in external tool metadata and use them in `submit_human_decision` to transition the current ledger step before AgentScope resumes.

- [ ] **Step 8: Run focused tests**

Run: `pytest tests/test_navigation_plan_execution.py tests/test_navigation_execution_tools_dry_run.py tests/test_navigation_agent_tools.py tests/test_web_human_decision_api.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/plan_execution.py src/vla_data_juicer_agents/navigation/execution_tools.py src/vla_data_juicer_agents/navigation/agent_tools.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_navigation_plan_execution.py tests/test_navigation_execution_tools_dry_run.py tests/test_navigation_agent_tools.py tests/test_web_human_decision_api.py
git commit -m "feat: execute navigation steps from stored plans"
```

---

### Task 8: Resolve Phase Tools Dynamically and Simplify AgentScope Prompts

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/services.py`
- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py:429-490`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py:280-330,1616-1705`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_prompts.py:1-285`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_bootstrap.py`
- Modify: `tests/test_navigation_agent_tools.py`
- Modify: `tests/test_agentscope_bootstrap.py`
- Modify: `tests/test_web_agentscope_session.py`

**Interfaces:**
- Produces: `NavigationServices`, `build_navigation_services(workspace_root, settings) -> NavigationServices`, and `resolve_navigation_agent_tools(*, services, agentscope_session_id, cancellation) -> list[ToolBase]`.
- Consumes: all durable stores and Task 3/6/7 tool builders.

- [ ] **Step 1: Write failing phase-tool tests**

Assert investigation exposes task/inspection/cognitive tools but no execution/submission schema; planning exposes cognitive tools and exactly one phase submit tool; execution exposes overview/current-step, plan-bound human decision when required, and only actions remaining in the active plan. Assert no tool list contains `get_workflow_plan_draft_tool`, `update_workflow_plan_draft_tool`, or any `finalize_*_plan_tool`.

- [ ] **Step 2: Write failing prompt tests**

Assert the prompt includes “observations are facts”, “model owns semantic decisions”, “submit one complete JSON plan”, “resubmit complete plan”, and “durable state is authoritative”. Assert it excludes `phase_profile_schema`, `data_profile_draft`, `data_profile_patch`, old infer tool names, and old finalize tool names.

- [ ] **Step 3: Run tests and confirm failure**

Run: `pytest tests/test_navigation_agent_tools.py tests/test_agentscope_bootstrap.py tests/test_web_agentscope_session.py -q`

Expected: current runtime registers all tools and prompts the draft/finalize loop.

- [ ] **Step 4: Implement service construction**

`build_navigation_services(workspace_root, settings)` creates one SQLite path, evidence/result roots, and task/observation/plan repositories. Use this builder from Web and direct workflows; do not instantiate draft stores.

```python
@dataclass(frozen=True)
class NavigationServices:
    settings: NavigationSettings
    task_store: SqliteNavigationTaskStore
    observation_store: SqliteNavigationObservationStore
    evidence_store: FileNavigationEvidenceStore
    plan_store: SqliteNavigationPlanRepository


def build_navigation_services(
    workspace_root: Path,
    settings: NavigationSettings | None = None,
) -> NavigationServices:
    settings = settings or NavigationSettings()
    # Reuse the deployed database path so the v2 migration preserves existing tasks.
    db_path = workspace_root / "navigation-tasks.sqlite"
    return NavigationServices(
        settings=settings,
        task_store=SqliteNavigationTaskStore(db_path),
        observation_store=SqliteNavigationObservationStore(db_path),
        evidence_store=FileNavigationEvidenceStore(workspace_root / "navigation-evidence"),
        plan_store=SqliteNavigationPlanRepository(db_path),
    )
```

- [ ] **Step 5: Implement phase-aware tool resolution**

Resolve the active task by AgentScope session, then use reconciled durable state:

- no active task: compact task-entry tool only;
- incomplete observations: relevant inspection plus cognitive tools;
- observations complete/no active plan: cognitive tools plus current phase submit tool;
- active plan: execution overview/current-step plus remaining plan-bound actions;
- completed: compact state/evidence tools only.

- [ ] **Step 6: Replace AgentScope runtime draft wiring**

Remove all `JsonNavigationPlanDraftStore` construction, `_navigation_dry_run_for_session`, draft precreation, and draft-based gates. The extra-agent-tools factory builds services and calls the phase resolver on every AgentScope turn.

- [ ] **Step 7: Replace the navigation prompt with the compact contract**

Keep public progress, language, human-decision, cancellation, two-phase, and artifact-reconciliation instructions. Remove embedded schemas, fixed observation order, profile patching, and exhaustive catalog duplication. Inject only the compact durable state anchor generated from stores.

- [ ] **Step 8: Run focused tests**

Run: `pytest tests/test_navigation_agent_tools.py tests/test_agentscope_bootstrap.py tests/test_web_agentscope_session.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/services.py src/vla_data_juicer_agents/navigation/agent_tools.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/runtime/agentscope_prompts.py src/vla_data_juicer_agents/runtime/agentscope_bootstrap.py tests/test_navigation_agent_tools.py tests/test_agentscope_bootstrap.py tests/test_web_agentscope_session.py
git commit -m "feat: expose navigation tools by durable phase"
```

---

### Task 9: Migrate Direct Workflows and Delete the Legacy Draft/Profile Path

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/agents.py`
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py`
- Modify: `src/vla_data_juicer_agents/cli.py`
- Modify: `src/vla_data_juicer_agents/tools/vla/run_workflow.py`
- Modify: `src/vla_data_juicer_agents/navigation/models.py`
- Modify: `src/vla_data_juicer_agents/navigation/task_store.py`
- Modify: `docs/navigation-plan-agent-guidance.md`
- Delete: `src/vla_data_juicer_agents/navigation/plan_draft.py`
- Delete: `src/vla_data_juicer_agents/navigation/plan_draft_store.py`
- Delete: `src/vla_data_juicer_agents/navigation/session_plan_draft_tools.py`
- Delete: `tests/test_navigation_plan_draft_store.py`
- Delete: `tests/test_navigation_plan_profile.py`
- Delete: `tests/test_navigation_phase_profiles.py`
- Delete: `tests/test_navigation_session_plan_draft_tools.py`
- Rewrite: `tests/test_navigation_agents.py`
- Rewrite: `tests/test_navigation_workflow_models.py`
- Modify: `tests/test_navigation_cli.py`
- Modify: `tests/test_session_tool_registry.py`

**Interfaces:**
- Produces: direct CLI/session workflows backed by the same durable services and complete-plan tools as Web.
- Consumes: Task 8 services/tool resolver and Task 7 execution wrappers.

- [ ] **Step 1: Write failing direct-workflow tests**

Assert CLI/session workflow creates services under its run directory, reconciles before planning, receives its active plan from `NavigationPlanRepository` rather than parsing assistant text, and executes with plan-bound tools. Assert no prompt injects a complete plan or draft snapshot.

- [ ] **Step 2: Run direct-workflow tests and confirm failure**

Run: `pytest tests/test_navigation_agents.py tests/test_navigation_workflow_models.py tests/test_navigation_cli.py tests/test_session_tool_registry.py -q`

Expected: current paths depend on `WorkflowPlanDraftState` and deterministic builders.

- [ ] **Step 3: Migrate plan/executor agent construction**

Change `create_plan_agent`/`create_executor_agent` to receive resolved tool lists and compact instructions. Change `run_plan_agent` to run the agent then load the active plan from `plan_store.get_active(task_id, phase)`; raise a precise error if the model did not submit a valid plan. Change `run_executor_agent` to read overview/current-step state instead of embedding full `WorkflowPlan JSON` in its prompt.

- [ ] **Step 4: Migrate CLI and session workflow entry points**

Build services under the existing run/workspace directory, call `prepare_navigation_task_entry`, resolve planning tools, run planning, resolve execution tools, and run execution. Keep existing event streaming, cancellation, run-state persistence, dry-run, and response-language arguments.

- [ ] **Step 5: Rebuild the task table without legacy profile state**

Increment `TASK_SCHEMA_VERSION`. On the migration connection, set `PRAGMA foreign_keys=OFF` before `BEGIN IMMEDIATE`, create the v2 `navigation_tasks_new` table without `data_profile_json`, copy preserved columns, map unfinished rows with legacy profile data to `needs_replan`, drop the old table, rename the new table, recreate indexes, and commit. Re-enable foreign keys and require `PRAGMA foreign_key_check` to return no rows. Remove `NavigationTask.data_profile` and all reads/writes.

- [ ] **Step 6: Delete legacy files, types, and deterministic builders**

Delete the three legacy modules and tests listed above. Remove `NavigationProcessingProfile`, `NavigationExtractSyncProfile`, `NavigationFinishProcessingProfile`, `StageVariantDecision`, old profile-only issue models, `build_deterministic_plan_template`, draft-aware workflow fallbacks, old imports, and obsolete guide content. Retain only generic request/result/runtime models still used.

Remove `record_navigation_task_step_tool` and its arbitrary model-supplied arguments/results path; execution wrappers are now the only ledger writers. Stop exposing `update_navigation_task_state_tool` to the model and keep phase/status transitions inside reconciliation, plan activation, execution, and human-decision services.

Do not delete deployed `navigation-plan-drafts/` data from runtime storage in this code change. Remove every reader/writer and document that the now-unreferenced directory may be cleaned operationally after rollout verification.

- [ ] **Step 7: Run a dead-reference audit before tests**

Run:

```bash
rg -n "WorkflowPlanDraftState|NavigationPlanDraftStore|get_workflow_plan_draft_tool|update_workflow_plan_draft_tool|finalize_extract_sync_plan_tool|finalize_finish_processing_plan_tool|finalize_workflow_plan_tool|record_navigation_task_step_tool|schema_snapshot\(|build_deterministic_plan_template|NavigationProcessingProfile|NavigationExtractSyncProfile|NavigationFinishProcessingProfile" src tests docs/navigation-plan-agent-guidance.md
```

Expected: no output and exit status 1.

- [ ] **Step 8: Run migrated workflow tests**

Run: `pytest tests/test_navigation_agents.py tests/test_navigation_workflow_models.py tests/test_navigation_cli.py tests/test_session_tool_registry.py tests/test_web_agentscope_session.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add -A src/vla_data_juicer_agents/navigation src/vla_data_juicer_agents/runtime src/vla_data_juicer_agents/tools/vla/run_workflow.py src/vla_data_juicer_agents/cli.py tests docs/navigation-plan-agent-guidance.md
git commit -m "refactor: remove legacy navigation plan drafts"
```

---

### Task 10: Enforce Context Budgets and Run End-to-End Acceptance

**Files:**
- Create: `tests/test_navigation_context_budget.py`
- Create: `tests/test_navigation_model_authored_flow.py`
- Modify: `tests/test_web_agentscope_session.py`
- Modify: `docs/navigation-plan-agent-guidance.md`

**Interfaces:**
- Consumes: final tool resolver, stores, prompts, and dry-run processing tools.
- Produces: regression evidence for bounded context, compact recovery, artifact-driven phase selection, and server acceptance instructions.

- [ ] **Step 1: Write the transcript/context regression test**

Build a deterministic fake AgentScope transcript covering task entry, all required inspections, one invalid full submission, one valid full submission, and all execution steps. Assert each tool result limit from the design, no payload contains `schema_snapshot`, `data_profile_draft`, or accumulated errors, total application-provided content is below 83,885 estimated tokens, and no simulated compact event is required.

- [ ] **Step 2: Write end-to-end state recovery tests**

Cover:

- raw-only -> extract-sync plan -> dry-run execution;
- existing sync plus scene mode -> finish-processing plan without extract-sync;
- final outputs -> completed with no processing tools;
- completed task whose outputs were deleted -> next task entry chooses the earliest incomplete phase;
- cleared/compacted conversation -> state anchor recovers task phase, active plan, and current step from SQLite.

- [ ] **Step 3: Run the new acceptance tests and fix only integration defects**

Run: `pytest tests/test_navigation_context_budget.py tests/test_navigation_model_authored_flow.py tests/test_web_agentscope_session.py -q`

Expected: all tests pass.

- [ ] **Step 4: Run all navigation tests**

Run: `pytest tests/test_navigation_*.py tests/test_agentscope_bootstrap.py tests/test_web_agentscope_session.py tests/test_web_human_decision_api.py -q`

Expected: all tests pass with zero failures.

- [ ] **Step 5: Run the complete local suite**

Run: `pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 6: Run final static and dead-code checks**

Run:

```bash
git diff --check
rg -n "WorkflowPlanDraftState|NavigationPlanDraftStore|get_workflow_plan_draft_tool|update_workflow_plan_draft_tool|finalize_.*_plan_tool|record_navigation_task_step_tool|schema_snapshot\(|build_deterministic_plan_template|NavigationProcessingProfile|NavigationExtractSyncProfile|NavigationFinishProcessingProfile" src tests
```

Expected: `git diff --check` exits 0; `rg` prints nothing and exits 1.

- [ ] **Step 7: Commit local acceptance tests**

```bash
git add tests/test_navigation_context_budget.py tests/test_navigation_model_authored_flow.py tests/test_web_agentscope_session.py docs/navigation-plan-agent-guidance.md
git commit -m "test: verify bounded navigation planning context"
```

- [ ] **Step 8: Perform server acceptance after synchronized deployment**

On the server, first run read-only checks against the synchronized revision and one known test date: inspect service logs, task SQLite state, artifact snapshot, active observation/plan/ledger rows, and exposed tool names. Then run a dry-run task followed by the authorized real-data test. Record model input tokens per turn, compact events, plan submission attempts, active plan revision, and ledger transitions.

Expected:

- artifact reconciliation runs before planning;
- one valid complete plan submission succeeds without a draft/finalize loop;
- tool results remain within limits;
- peak input stays below 83,885 tokens and the standard run emits no compact event;
- if compaction is forced, durable phase/plan/current-step recovery is correct;
- deleting test outputs before another task entry selects the correct earliest incomplete phase.

Do not add sub-session rotation or change AgentScope compression based on this run. If context still exceeds the target after all application-level limits pass, capture the transcript metrics and open a separate design task.
