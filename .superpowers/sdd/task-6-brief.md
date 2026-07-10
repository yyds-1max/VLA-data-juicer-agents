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

Implement `_submit_complete_plan` once. It validates the bound task/current context, calls `validate_navigation_plan`, records a submission attempt, returns `ok`/`error_type`/compact `errors`/`retry` on failure with the 3,000-character limit, or calls `plan_store.activate` and returns only `ok: true`, `plan_id`, `plan_revision`, `step_count`, `status`, and `next_action` on success.

- [ ] **Step 6: Run focused tests**

Run: `pytest tests/test_navigation_plan_validation.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_store.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/vla_data_juicer_agents/navigation/plan_validation.py src/vla_data_juicer_agents/navigation/plan_submission_tools.py tests/test_navigation_plan_validation.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_store.py
git commit -m "feat: submit complete navigation plans atomically"
```

---
