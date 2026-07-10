import json
from collections.abc import Sequence
from typing import Any, Literal, cast
from uuid import uuid4

from agentscope.tool import FunctionTool
from pydantic import BaseModel, ValidationError

from vla_data_juicer_agents.navigation.catalog import (
    CAPABILITY_CATALOG_REVISION,
    ToolCapability,
)
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    NavigationObservationRevision,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    PlanSubmissionAttempt,
    PlanValidationIssue,
    PlanValidationReport,
)
from vla_data_juicer_agents.navigation.plan_store import (
    SqliteNavigationPlanRepository,
)
from vla_data_juicer_agents.navigation.plan_validation import (
    MAX_PUBLIC_PLAN_VALIDATION_ISSUES,
    validate_navigation_plan,
)
from vla_data_juicer_agents.navigation.planning_context import (
    compute_planning_context_revision,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask, utc_now


MAX_PLAN_VALIDATION_FAILURE_CHARS = 3_000
MAX_PLAN_SUBMISSION_SUCCESS_CHARS = 4_000
PlanPhase = Literal["extract_sync", "finish_processing"]


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_report(errors: Sequence[PlanValidationIssue]) -> PlanValidationReport:
    indexed: dict[tuple[str, str], PlanValidationIssue] = {}
    for issue in errors:
        indexed.setdefault((issue.path, issue.code), issue)
    public = [
        indexed[key]
        for key in sorted(indexed)[:MAX_PUBLIC_PLAN_VALIDATION_ISSUES]
    ]
    return PlanValidationReport(ok=not public, errors=public, warnings=[])


def _structure_report(error: ValidationError) -> PlanValidationReport:
    issues: list[PlanValidationIssue] = []
    code_by_type = {
        "missing": "missing_required_field",
        "extra_forbidden": "extra_field_forbidden",
        "union_tag_invalid": "unknown_action",
        "union_tag_not_found": "missing_required_field",
        "literal_error": "invalid_literal",
    }
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        path = ".".join(["plan", *(str(part) for part in item["loc"])])
        issues.append(
            PlanValidationIssue(
                path=path,
                code=code_by_type.get(item["type"], "invalid_plan_structure"),
                message=item["msg"],
            )
        )
    return _stable_report(issues)


def _context_report(
    *,
    task: NavigationTask,
    phase: PlanPhase,
    observation: NavigationObservationRevision | None,
    submitted_revision: str,
    capability_revision: str,
) -> PlanValidationReport:
    issues: list[PlanValidationIssue] = []
    if task.phase.value != phase:
        issues.append(
            PlanValidationIssue(
                path="task.phase",
                code="task_phase_mismatch",
                message="Submission tool does not match the active task phase",
                allowed_values=[task.phase.value],
            )
        )
    if observation is None:
        issues.append(
            PlanValidationIssue(
                path="planning_context_revision",
                code="no_current_observation",
                message="No current observation exists for plan submission",
            )
        )
    else:
        expected_revision = compute_planning_context_revision(
            task=task,
            observation_revision=observation.revision,
            capability_revision=capability_revision,
        )
        if submitted_revision != expected_revision:
            issues.append(
                PlanValidationIssue(
                    path="planning_context_revision",
                    code="stale_planning_context_revision",
                    message="Planning context revision is stale",
                )
            )
    return _stable_report(issues)


def _candidate_dict(plan: Any) -> dict[str, Any]:
    if isinstance(plan, BaseModel):
        return cast(dict[str, Any], plan.model_dump(mode="json"))
    if isinstance(plan, dict):
        return cast(dict[str, Any], plan)
    return {"invalid_candidate": plan}


def _compact_failure(
    *,
    error_type: str,
    report: PlanValidationReport,
) -> dict[str, Any]:
    errors = [issue.model_dump(mode="json") for issue in report.errors]
    result = {
        "ok": False,
        "error_type": error_type,
        "errors": errors,
        "retry": "resubmit_complete_plan",
    }
    if len(_canonical_json(result)) <= MAX_PLAN_VALIDATION_FAILURE_CHARS:
        return result

    # Large observed-value sets are useful in audit storage but not in the public
    # retry response.  Preserve paths/codes/messages first, then the largest stable
    # prefix of issues that fits the tool result budget.
    for issue in errors:
        issue["allowed_values"] = []
    while errors and len(_canonical_json(result)) > MAX_PLAN_VALIDATION_FAILURE_CHARS:
        errors.pop()
    if not errors:
        result["errors"] = [
            {
                "path": "plan",
                "code": "validation_details_too_large",
                "message": "Plan validation failed; inspect evidence and resubmit the complete plan",
                "allowed_values": [],
            }
        ]
    return result


def _internal_failure(error_type: str, code: str, message: str) -> dict[str, Any]:
    return _compact_failure(
        error_type=error_type,
        report=PlanValidationReport(
            ok=False,
            errors=[
                PlanValidationIssue(
                    path="submission",
                    code=code,
                    message=message,
                )
            ],
            warnings=[],
        ),
    )


def _capability_revision(
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> str:
    if isinstance(capabilities, dict):
        return str(capabilities.get("revision", CAPABILITY_CATALOG_REVISION))
    return CAPABILITY_CATALOG_REVISION


def build_navigation_plan_submission_tools(
    *,
    task: NavigationTask,
    observation_store: SqliteNavigationObservationStore,
    evidence_store: FileNavigationEvidenceStore,
    plan_store: SqliteNavigationPlanRepository,
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> list[FunctionTool]:
    """Build the single complete-plan submission tool for the bound task phase."""
    phase_value = task.phase.value
    if phase_value not in {"extract_sync", "finish_processing"}:
        raise ValueError(
            f"task phase does not support plan submission: {phase_value}"
        )
    active_phase = cast(PlanPhase, phase_value)
    capability_revision = _capability_revision(capabilities)

    # Evidence payloads remain external. Submission validates refs against the
    # observation metadata index; the bound payload store is retained in the
    # builder contract for the same task-scoped service bundle.
    _ = evidence_store

    def _submit_complete_plan(
        *,
        phase: PlanPhase,
        planning_context_revision: str,
        plan: ExtractSyncPlanInput | FinishProcessingPlanInput | dict[str, Any],
    ) -> dict[str, Any]:
        candidate = _candidate_dict(plan)
        observation = observation_store.latest(task.task_id)
        context_report = _context_report(
            task=task,
            phase=phase,
            observation=observation,
            submitted_revision=planning_context_revision,
            capability_revision=capability_revision,
        )
        error_type = "planning_context_mismatch"
        validation = context_report
        canonical_plan: ExtractSyncPlanInput | FinishProcessingPlanInput | None = None

        if context_report.ok:
            model = ExtractSyncPlanInput if phase == "extract_sync" else FinishProcessingPlanInput
            try:
                canonical_plan = model.model_validate(candidate)
            except ValidationError as error:
                validation = _structure_report(error)
                error_type = "plan_validation_failed"
            else:
                if observation is None:  # Kept explicit for type narrowing.
                    raise RuntimeError("context validation accepted missing observation")
                evidence = observation_store.list_evidence(
                    task.task_id,
                    limit=100_000,
                )
                validation = validate_navigation_plan(
                    task=task,
                    observation=observation,
                    plan=canonical_plan,
                    evidence=evidence,
                    capabilities=capabilities,
                )
                error_type = "plan_validation_failed"

        attempt = PlanSubmissionAttempt(
            attempt_id=f"nav_plan_attempt_{uuid4().hex}",
            task_id=task.task_id,
            phase=phase,
            planning_context_revision=planning_context_revision,
            candidate=candidate,
            validation=validation,
            created_at=utc_now(),
        )
        try:
            plan_store.record_attempt(attempt)
        except Exception:
            return _internal_failure(
                "submission_audit_failed",
                "audit_persistence_failed",
                "Submission audit could not be persisted; no plan was activated",
            )

        if not validation.ok or canonical_plan is None:
            return _compact_failure(error_type=error_type, report=validation)

        try:
            record = plan_store.activate(
                task,
                phase,
                observation.revision,
                canonical_plan,
            )
        except Exception:
            return _internal_failure(
                "plan_activation_failed",
                "activation_transaction_failed",
                "Plan activation failed; the candidate was not exposed as active",
            )

        result = {
            "ok": True,
            "plan_id": record.plan_id,
            "plan_revision": record.plan_revision,
            "step_count": len(record.plan.steps),
            "status": record.status,
            "next_action": record.plan.steps[0].action,
        }
        if len(_canonical_json(result)) > MAX_PLAN_SUBMISSION_SUCCESS_CHARS:
            raise ValueError(
                f"plan submission success exceeds {MAX_PLAN_SUBMISSION_SUCCESS_CHARS} characters"
            )
        return result

    def submit_extract_sync_plan_tool(
        planning_context_revision: str,
        plan: ExtractSyncPlanInput,
    ) -> dict[str, Any]:
        """Validate and atomically activate one complete extract-sync plan."""
        return _submit_complete_plan(
            phase="extract_sync",
            planning_context_revision=planning_context_revision,
            plan=plan,
        )

    def submit_finish_processing_plan_tool(
        planning_context_revision: str,
        plan: FinishProcessingPlanInput,
    ) -> dict[str, Any]:
        """Validate and atomically activate one complete finish-processing plan."""
        return _submit_complete_plan(
            phase="finish_processing",
            planning_context_revision=planning_context_revision,
            plan=plan,
        )

    if active_phase == "extract_sync":
        return [
            FunctionTool(
                submit_extract_sync_plan_tool,
                name="submit_extract_sync_plan_tool",
                is_concurrency_safe=False,
            )
        ]
    return [
        FunctionTool(
            submit_finish_processing_plan_tool,
            name="submit_finish_processing_plan_tool",
            is_concurrency_safe=False,
        )
    ]
