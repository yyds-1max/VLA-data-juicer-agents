from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Callable

from agentscope.tool import FunctionTool, ToolBase

from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    TurnCancelled,
    bind_cancellation,
    current_cancellation,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.execution_tools import (
    assemble_finish_temp,
    extract_and_sync_navigation_data,
    prepare_gridmap_for_projection,
    prepare_raw_data,
    run_initial_annotation_gui,
    run_noobscene_preprocessing,
    run_projection_and_trajectory,
    run_tracking,
    validate_navigation_outputs,
)
from vla_data_juicer_agents.navigation.observation_models import (
    CalibrationInventoryObservation,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    NavigationPlanRecord,
)
from vla_data_juicer_agents.navigation.plan_store import (
    ActivePlanExecutionConflict,
    MAX_EXECUTION_READ_CHARS,
    MAX_RESULT_OUTBOX_CHARS,
    SqliteNavigationPlanRepository,
)
from vla_data_juicer_agents.navigation.task_reconciliation import (
    build_navigation_artifact_snapshot,
    reconcile_navigation_task,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskOwnershipError,
    NavigationTaskStateRevisionError,
    SqliteNavigationTaskStore,
)


_PROCESSING_ACTIONS = {
    "prepare_raw_data",
    "extract_and_sync_navigation_data",
    "assemble_finish_temp",
    "run_noobscene_preprocessing",
    "run_initial_annotation_gui",
    "run_tracking",
    "prepare_gridmap_for_projection",
    "run_projection_and_trajectory",
    "validate_navigation_outputs",
}
_EXTERNAL_ACTION = "confirm_navigation_calibration_params"
_SENSITIVE_KEYS = {
    "password", "token", "secret", "authorization", "api_key", "cookie"
}


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _redact_sensitive(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact_sensitive(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_sensitive(value) for value in payload]
    if isinstance(payload, tuple):
        return [_redact_sensitive(value) for value in payload]
    return payload


def _bounded_result_payload(payload: dict[str, Any], *, action: str) -> tuple[dict[str, Any], bool]:
    redacted = _redact_sensitive(payload)
    canonical = _canonical_json(redacted)
    if len(canonical.encode("utf-8")) <= MAX_RESULT_OUTBOX_CHARS:
        return redacted, False
    return {
        "ok": False,
        "tool_name": action[:200],
        "message": "Processing returned an oversized result; the bounded digest is retained and replanning is required.",
        "details": {
            "error_type": "processing_result_oversized",
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "original_bytes": len(canonical.encode("utf-8")),
            "limit_bytes": MAX_RESULT_OUTBOX_CHARS,
        },
    }, True


def _compact_error(error_type: str, message: str, **details: Any) -> dict[str, Any]:
    result = {
        "ok": False,
        "error_type": error_type,
        "message": message[:800],
        **details,
    }
    if len(_canonical_json(result)) > MAX_EXECUTION_READ_CHARS:
        result = {
            "ok": False,
            "error_type": error_type,
            "message": message[:400],
        }
    return result


def _calibration_source_path(
    selected_sensor_source: str,
    settings: NavigationSettings,
) -> Path:
    source = Path(selected_sensor_source)
    resolved = (
        source.resolve(strict=False)
        if source.is_absolute()
        else (settings.processing_root / source).resolve(strict=False)
    )
    processing_root = settings.processing_root.resolve(strict=False)
    if not resolved.is_relative_to(processing_root):
        raise ValueError("selected calibration source must resolve under processing_root")
    return resolved


def resolve_step_arguments(
    *,
    task: NavigationTask,
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput,
    step: Any,
    settings: NavigationSettings,
) -> dict[str, Any]:
    """Resolve canonical processing arguments without accepting model copies."""
    common = {"settings": settings, "dry_run": task.dry_run}
    date_args = {"date": task.date, "segments": task.segments, **common}
    finish_temp = settings.finish_data_root / f"{task.date}_temp"
    finish_path = settings.finish_data_root / task.date
    action = step.action

    if action == "prepare_raw_data":
        return date_args
    if action == "extract_and_sync_navigation_data":
        if not isinstance(plan, ExtractSyncPlanInput):
            raise ValueError("extract-sync action requires an extract-sync plan")
        topics = plan.decisions.topic_selection
        return {
            **date_args,
            "processes_num": step.arguments.processes_num,
            "topic_whitelist": list(topics.topic_whitelist),
            "topic_map": dict(topics.topic_map),
            "query_dir": topics.query_dir,
        }
    if not isinstance(plan, FinishProcessingPlanInput):
        raise ValueError(f"finish-processing action requires a finish plan: {action}")
    if action == _EXTERNAL_ACTION:
        calibration_source = _calibration_source_path(
            plan.decisions.calibration.selected_sensor_source,
            settings,
        )
        return {
            **date_args,
            "selected_sensor_source": calibration_source,
        }
    if action == "assemble_finish_temp":
        calibration_source = _calibration_source_path(
            plan.decisions.calibration.selected_sensor_source,
            settings,
        )
        return {
            **date_args,
            "selected_sensor_source": calibration_source,
        }
    if action == "run_noobscene_preprocessing":
        localization = plan.decisions.localization
        return {
            "finish_temp_path": finish_temp,
            "localization_source": localization.source,
            "localization_conversion": localization.conversion,
            **common,
        }
    if action == "run_initial_annotation_gui":
        return {"finish_temp_path": finish_temp, **common}
    if action == "run_tracking":
        return {"finish_temp_path": finish_temp, **common}
    if action == "prepare_gridmap_for_projection":
        return {
            **date_args,
            "finish_temp_path": finish_temp,
            "gridmap_variant": step.variant,
        }
    if action == "run_projection_and_trajectory":
        return {
            "finish_temp_path": finish_temp,
            "finish_path": finish_path,
            "projection_variant": step.variant,
            **common,
        }
    if action == "validate_navigation_outputs":
        return {"date": task.date, **common}
    raise ValueError(f"unsupported navigation plan action: {action}")


def _task_changes(task: NavigationTask) -> dict[str, Any]:
    changes = task.model_dump(mode="json")
    for field in {
        "task_id",
        "created_by_web_session_id",
        "latest_web_session_id",
        "agentscope_session_id",
        "created_at",
        "updated_at",
        "state_revision",
    }:
        changes.pop(field, None)
    return changes


def _reconcile_execution_entry(
    *,
    task: NavigationTask,
    plan_store: SqliteNavigationPlanRepository,
    settings: NavigationSettings,
    requested_plan_id: str,
    expected_action: str,
    expected_web_session_id: str | None = None,
    expected_agentscope_session_id: str | None = None,
) -> tuple[NavigationTask, dict[str, Any] | None]:
    task_store = SqliteNavigationTaskStore(plan_store.db_path)
    stored = task_store.get_task(task.task_id)
    if stored is None:
        return task, _compact_error(
            "task_not_found",
            "The plan-bound navigation task no longer exists.",
        )
    if expected_agentscope_session_id is not None and (
        stored.created_by_web_session_id != expected_web_session_id
        or stored.latest_web_session_id != expected_web_session_id
        or stored.agentscope_session_id != expected_agentscope_session_id
    ):
        return stored, _compact_error(
            "navigation_task_session_mismatch",
            "The plan-bound task is no longer owned by this AgentScope session.",
        )
    active_plans = [
        active
        for phase in ("extract_sync", "finish_processing")
        if (active := plan_store.get_active(stored.task_id, phase)) is not None
    ]
    terminal_recovery = any(
        (
            (plan_store.get_current_step(active_plan.plan_id) or {}).get("step")
            or {}
        ).get("status")
        in {"failed", "needs_replan"}
        for active_plan in active_plans
    )
    if terminal_recovery:
        snapshot = build_navigation_artifact_snapshot(
            stored.date, stored.segments, settings=settings
        )
        changes = {"artifact_snapshot": snapshot.model_dump(mode="json")}
        try:
            updated = task_store.update_task_for_session(
                stored.task_id,
                web_session_id=expected_web_session_id,
                agentscope_session_id=expected_agentscope_session_id,
                expected_state_revision=stored.state_revision,
                **changes,
            )
        except (NavigationTaskOwnershipError, NavigationTaskStateRevisionError):
            current = task_store.get_task(stored.task_id) or stored
            return current, _compact_error(
                "navigation_task_state_stale",
                "The task changed while execution artifacts were reconciled.",
                next_action="reload_active_plan",
            )
        return updated, None
    live = reconcile_navigation_task(stored, settings=settings)
    invalid_plans: list[NavigationPlanRecord] = []
    for active_plan in active_plans:
        current = plan_store.get_current_step(active_plan.plan_id)
        current_step_id = (current or {}).get("step", {}).get("step_id")
        has_active_staged_result = (
            isinstance(current_step_id, str)
            and plan_store.get_staged_step_result(active_plan.plan_id, current_step_id)
            is not None
        )
        if has_active_staged_result:
            continue
        phase_compatible = live.phase.value == active_plan.phase
        if (
            active_plan.phase == "finish_processing"
            and live.phase.value == "completed"
            and active_plan.plan_id == requested_plan_id
            and expected_action == "validate_navigation_outputs"
        ):
            phase_compatible = True
        if live.status.value == "needs_reconcile" or not phase_compatible:
            invalid_plans.append(active_plan)
    if invalid_plans:
        for active_plan in invalid_plans:
            if active_plan.status == "active":
                try:
                    plan_store.mark_needs_replan(
                        active_plan.plan_id,
                        "artifact reconciliation invalidated the stored execution plan",
                        expected_web_session_id=expected_web_session_id,
                        expected_agentscope_session_id=expected_agentscope_session_id,
                    )
                except ActivePlanExecutionConflict:
                    return stored, _compact_error(
                        "plan_mutation_blocked_by_execution",
                        "Artifact reconciliation cannot invalidate a plan while its result or handoff is in flight. Retry the current step recovery first.",
                        next_action="retry_same_plan_step",
                    )
        return live, _compact_error(
            "plan_invalidated_by_artifacts",
            "Artifact reconciliation produced facts incompatible with the active plan; replan before execution.",
            next_action="submit_complete_plan",
        )
    has_any_staged_result = any(
        plan_store.get_staged_step_result(
            active_plan.plan_id,
            ((plan_store.get_current_step(active_plan.plan_id) or {}).get("step") or {}).get("step_id", ""),
        ) is not None
        for active_plan in active_plans
    )
    if has_any_staged_result:
        return stored, None
    try:
        updated = task_store.update_task_for_session(
            stored.task_id,
            web_session_id=expected_web_session_id,
            agentscope_session_id=expected_agentscope_session_id,
            expected_state_revision=stored.state_revision,
            **_task_changes(live),
        )
    except (NavigationTaskOwnershipError, NavigationTaskStateRevisionError):
        current = task_store.get_task(stored.task_id) or stored
        return current, _compact_error(
            "navigation_task_state_stale",
            "The task changed while execution artifacts were reconciled.",
            next_action="reload_active_plan",
        )
    return updated, None


def _plan_step(plan: NavigationPlanRecord, step_id: str) -> Any | None:
    return next((step for step in plan.plan.steps if step.step_id == step_id), None)


def _validate_calibration_inventory(
    *,
    task: NavigationTask,
    plan: NavigationPlanRecord,
    settings: NavigationSettings,
    plan_store: SqliteNavigationPlanRepository,
) -> dict[str, Any] | None:
    if not isinstance(plan.plan, FinishProcessingPlanInput):
        return None
    selected = plan.plan.decisions.calibration.selected_sensor_source
    observation = SqliteNavigationObservationStore(plan_store.db_path).get(
        task.task_id,
        plan.observation_revision,
    )
    observed = {
        source
        for payload in (observation.payloads if observation is not None else [])
        if isinstance(payload, CalibrationInventoryObservation)
        for source in payload.sensor_sources
    }
    try:
        _calibration_source_path(selected, settings)
    except ValueError:
        valid = False
    else:
        valid = selected in observed
    if valid:
        return None
    return _compact_error(
        "calibration_source_invalid",
        "The stored calibration source does not exactly match the plan observation revision or escapes processing_root.",
        next_action="submit_complete_plan",
    )


def _gate_step(
    *,
    bound_task: NavigationTask,
    requested_plan_id: str,
    requested_step_id: str,
    expected_action: str,
    plan_store: SqliteNavigationPlanRepository,
    settings: NavigationSettings,
    expected_web_session_id: str | None = None,
    expected_agentscope_session_id: str | None = None,
) -> tuple[NavigationTask | None, NavigationPlanRecord | None, Any | None, dict[str, Any] | None]:
    task, reconcile_error = _reconcile_execution_entry(
        task=bound_task,
        plan_store=plan_store,
        settings=settings,
        requested_plan_id=requested_plan_id,
        expected_action=expected_action,
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    if reconcile_error is not None:
        return task, None, None, reconcile_error
    plan = plan_store.get(requested_plan_id)
    active = plan_store.get_active(task.task_id, plan.phase) if plan is not None else None
    if (
        plan is None
        or plan.task_id != task.task_id
        or plan.status != "active"
        or active is None
        or active.plan_id != plan.plan_id
    ):
        return task, plan, None, _compact_error(
            "plan_not_active",
            "The requested plan is missing, inactive, or superseded.",
            next_action="load_active_plan",
        )
    step = _plan_step(plan, requested_step_id)
    if step is None:
        return task, plan, None, _compact_error(
            "step_not_found",
            "The requested step is not part of the immutable plan.",
            next_action="get_current_step",
        )
    if step.action != expected_action:
        return task, plan, step, _compact_error(
            "step_action_mismatch",
            "The requested wrapper does not match the stored step action.",
            next_action=step.action,
        )
    current = plan_store.get_current_step(plan.plan_id)
    if current is None or current["step"]["step_id"] != step.step_id:
        return task, plan, step, _compact_error(
            "step_not_current",
            "Only the current executable ledger step may run.",
            next_action=(current or {}).get("step", {}).get("action"),
        )
    dependencies = plan_store.dependency_statuses(plan.plan_id, list(step.depends_on))
    unmet = [
        dependency
        for dependency in step.depends_on
        if dependencies.get(dependency) != "completed"
    ]
    if unmet:
        return task, plan, step, _compact_error(
            "step_dependencies_unmet",
            "The current step has unfinished plan dependencies.",
            unmet_dependencies=unmet,
            next_action="get_current_step",
        )
    if step.action in {_EXTERNAL_ACTION, "assemble_finish_temp"}:
        calibration_error = _validate_calibration_inventory(
            task=task,
            plan=plan,
            settings=settings,
            plan_store=plan_store,
        )
        if calibration_error is not None:
            return task, plan, step, calibration_error
    return task, plan, step, None


def _result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    raise TypeError(f"navigation processing action returned unsupported result: {type(result)!r}")


def _result_summary(payload: dict[str, Any]) -> dict[str, Any]:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    error_type = details.get("error_type") or payload.get("error_type")
    return {
        "ok": bool(payload.get("ok")),
        "tool_name": str(payload.get("tool_name", ""))[:200],
        "message": str(payload.get("message", ""))[:800],
        "error_type": str(error_type)[:200] if error_type else None,
        "produced_path_count": len(payload.get("produced_paths") or []),
    }


def _next_action(plan_store: SqliteNavigationPlanRepository, plan_id: str) -> str | None:
    current = plan_store.get_current_step(plan_id)
    return current["step"]["action"] if current is not None else None


def _terminal_error(
    plan_store: SqliteNavigationPlanRepository,
    plan_id: str,
) -> dict[str, Any]:
    current = plan_store.get_current_step(plan_id)
    status = (current or {}).get("step", {}).get("status")
    next_action = (
        "submit_complete_plan"
        if status in {"failed", "needs_replan"}
        else "manual_recovery"
        if status == "running"
        else _next_action(plan_store, plan_id)
    )
    return _compact_error(
        "step_already_terminal",
        "The ledger step was already claimed or finished; the processing function was not invoked again.",
        next_action=next_action,
    )


def _result_finalize_retry_error() -> dict[str, Any]:
    return _compact_error(
        "result_finalize_retry_required",
        "The processing result is durably staged but evidence or ledger finalization is temporarily incomplete. Retry the same plan_id and step_id; the underlying action will not run again.",
        next_action="retry_same_plan_step",
    )


def _finalize_staged_result(
    *,
    task: NavigationTask,
    plan: NavigationPlanRecord,
    step: Any,
    plan_store: SqliteNavigationPlanRepository,
    evidence_store: FileNavigationEvidenceStore,
    settings: NavigationSettings | None,
) -> dict[str, Any]:
    staged = plan_store.get_staged_step_result(plan.plan_id, step.step_id)
    if staged is None:
        return _compact_error(
            "step_recovery_requires_replan",
            "The running step has no durable staged result. Replan or perform manual recovery; the underlying action will not be rerun automatically.",
            next_action="submit_complete_plan",
        )
    result_ref = staged.result_ref
    if result_ref is None:
        return _result_finalize_retry_error()
    try:
        if not evidence_store.exists(task.task_id, result_ref):
            evidence_store.write(
                task.task_id,
                plan.observation_revision,
                "execution_result",
                step.action,
                staged.full_result,
                f"Execution result for {step.step_id}",
                ref=result_ref,
            )
        if not plan_store.attach_staged_result_evidence(
            plan.plan_id, step.step_id, result_ref,
            expected_web_session_id=task.latest_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        ):
            return _result_finalize_retry_error()
    except Exception:
        return _result_finalize_retry_error()
    try:
        finalized = plan_store.finalize_staged_step(
            plan.plan_id, step.step_id,
            expected_web_session_id=task.latest_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
    except Exception:
        return _result_finalize_retry_error()
    if not finalized:
        return _result_finalize_retry_error()
    stored_task = SqliteNavigationTaskStore(plan_store.db_path).get_task(task.task_id)
    if stored_task is not None and settings is not None:
        try:
            task_store = SqliteNavigationTaskStore(plan_store.db_path)
            if staged.target_status == "completed":
                reconciled = reconcile_navigation_task(stored_task, settings=settings)
                task_store.update_task_for_session(
                    stored_task.task_id,
                    web_session_id=task.latest_web_session_id,
                    agentscope_session_id=task.agentscope_session_id,
                    expected_state_revision=stored_task.state_revision,
                    **_task_changes(reconciled),
                )
            else:
                snapshot = build_navigation_artifact_snapshot(
                    stored_task.date, stored_task.segments, settings=settings
                )
                task_store.update_task_for_session(
                    stored_task.task_id,
                    web_session_id=task.latest_web_session_id,
                    agentscope_session_id=task.agentscope_session_id,
                    expected_state_revision=stored_task.state_revision,
                    artifact_snapshot=snapshot.model_dump(mode="json"),
                    status="failed",
                )
        except Exception:
            pass
    response = {
        **staged.result_summary,
        "plan_id": plan.plan_id,
        "step_id": step.step_id,
        "status": staged.target_status,
        "result_ref": result_ref,
        "next_action": (
            _next_action(plan_store, plan.plan_id)
            if staged.target_status == "completed"
            else "submit_complete_plan"
        ),
    }
    if len(_canonical_json(response)) > MAX_EXECUTION_READ_CHARS:
        raise ValueError("plan execution response exceeds 4000 characters")
    return response


def _invoke_plan_step(
    *,
    bound_task: NavigationTask,
    plan_id: str,
    step_id: str,
    action: str,
    function: Callable[..., Any],
    plan_store: SqliteNavigationPlanRepository,
    evidence_store: FileNavigationEvidenceStore,
    settings: NavigationSettings,
    cancellation: CancellationContext | None,
    expected_web_session_id: str | None = None,
    expected_agentscope_session_id: str | None = None,
) -> dict[str, Any]:
    active_cancellation = cancellation or current_cancellation()
    if active_cancellation is not None:
        active_cancellation.raise_if_cancelled()
    task, plan, step, gate_error = _gate_step(
        bound_task=bound_task,
        requested_plan_id=plan_id,
        requested_step_id=step_id,
        expected_action=action,
        plan_store=plan_store,
        settings=settings,
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    if gate_error is not None:
        return gate_error
    assert task is not None and plan is not None and step is not None
    staged = plan_store.get_staged_step_result(plan.plan_id, step.step_id)
    if staged is not None:
        return _finalize_staged_result(
            task=task,
            plan=plan,
            step=step,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
        )
    current = plan_store.get_current_step(plan.plan_id)
    current_status = (current or {}).get("step", {}).get("status")
    if current_status == "running":
        try:
            recovered = plan_store.recover_running_step_without_result(
                plan.plan_id,
                step.step_id,
                "running step has no staged result after process interruption",
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
        except ActivePlanExecutionConflict:
            return _compact_error(
                "human_handoff_recovery_required",
                "Force recovery is blocked by an unacknowledged human-decision handoff for this task and phase. Recover or acknowledge that handoff before invalidating the running step.",
                next_action="retry_human_decision_handoff",
            )
        if recovered:
            return _compact_error(
                "step_recovery_requires_replan",
                "The running step has no durable staged result. It was moved to needs_replan and will not be rerun automatically.",
                next_action="submit_complete_plan",
            )
        return _terminal_error(plan_store, plan.plan_id)
    if not plan_store.claim_step(
        plan.plan_id,
        step.step_id,
        step.action,
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    ):
        return _terminal_error(plan_store, plan.plan_id)

    try:
        arguments = resolve_step_arguments(
            task=task,
            plan=plan.plan,
            step=step,
            settings=settings,
        )
        with bind_cancellation(active_cancellation):
            if active_cancellation is not None:
                active_cancellation.raise_if_cancelled()
            payload = _result_payload(function(**arguments))
    except TurnCancelled:
        payload = {
            "ok": False,
            "tool_name": action,
            "message": "The current turn was interrupted.",
            "details": {"error_type": "turn_cancelled"},
        }
        try:
            plan_store.stage_step_result(
                plan.plan_id,
                step.step_id,
                target_status="failed",
                full_result=payload,
                result_summary=_result_summary(payload),
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            transition = _finalize_staged_result(
                task=task,
                plan=plan,
                step=step,
                plan_store=plan_store,
                evidence_store=evidence_store,
                settings=settings,
            )
            if transition.get("error_type") == "result_finalize_retry_required":
                error = TurnCancelled("The current turn was interrupted.")
                error.add_note("Cancellation result is staged and requires finalization retry.")
                raise error
        except TurnCancelled:
            raise
        except Exception as stage_error:
            plan_store.recover_running_step_without_result(
                plan.plan_id,
                step.step_id,
                "cancellation result could not be staged",
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            error = TurnCancelled("The current turn was interrupted.")
            error.add_note(f"Cancellation recovery requires replan: {stage_error!r}")
            raise error from stage_error
        raise
    except Exception as error:
        payload = {
            "ok": False,
            "tool_name": action,
            "message": str(error),
            "details": {"error_type": "processing_exception"},
        }

    payload, oversized = _bounded_result_payload(payload, action=action)
    summary = _result_summary(payload)
    terminal_status = "completed" if summary["ok"] else "failed"
    try:
        plan_store.stage_step_result(
            plan.plan_id,
            step.step_id,
            target_status=terminal_status,
            full_result=payload,
            result_summary=summary,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    except Exception:
        plan_store.recover_running_step_without_result(
            plan.plan_id,
            step.step_id,
            "processing result could not be staged after underlying execution",
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
        return _compact_error(
            "step_recovery_requires_replan",
            "The underlying action finished but its result could not be staged. The step was moved to needs_replan and will not be rerun automatically.",
            next_action="submit_complete_plan",
        )
    response = _finalize_staged_result(
        task=task,
        plan=plan,
        step=step,
        plan_store=plan_store,
        evidence_store=evidence_store,
        settings=settings,
    )
    if oversized and response.get("error_type") != "result_finalize_retry_required":
        plan_store.mark_needs_replan(
            plan.plan_id,
            "processing result exceeded the durable outbox payload policy",
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
        response["status"] = "needs_replan"
        response["next_action"] = "submit_complete_plan"
    return response


def prepare_plan_human_decision(
    *,
    task: NavigationTask,
    plan_store: SqliteNavigationPlanRepository,
    settings: NavigationSettings,
    plan_id: str,
    step_id: str,
    expected_web_session_id: str | None = None,
    expected_agentscope_session_id: str | None = None,
) -> dict[str, Any] | None:
    _task, plan, step, gate_error = _gate_step(
        bound_task=task,
        requested_plan_id=plan_id,
        requested_step_id=step_id,
        expected_action=_EXTERNAL_ACTION,
        plan_store=plan_store,
        settings=settings,
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    if gate_error is not None:
        return gate_error
    assert plan is not None and step is not None
    current = plan_store.get_current_step(plan.plan_id)
    if current is not None and current["step"]["status"] == "waiting_user":
        return None
    if not plan_store.mark_waiting_user(
        plan.plan_id,
        step.step_id,
        step.action,
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    ):
        return _terminal_error(plan_store, plan.plan_id)
    return None


def submit_plan_human_decision(
    *,
    plan_store: SqliteNavigationPlanRepository,
    evidence_store: FileNavigationEvidenceStore,
    plan_id: str,
    step_id: str,
    decision: dict[str, Any],
    expected_web_session_id: str | None = None,
    expected_agentscope_session_id: str | None = None,
) -> bool:
    plan = plan_store.get(plan_id)
    if plan is None:
        return False
    step = _plan_step(plan, step_id)
    if step is None or step.action != _EXTERNAL_ACTION:
        return False
    action = decision.get("action")
    error_type = (
        None
        if action == "confirm"
        else "human_guidance_required"
        if action == "guide"
        else "calibration_params_not_confirmed"
    )
    payload = {
        "ok": action == "confirm",
        "tool_name": _EXTERNAL_ACTION,
        "message": (
            "Camera parameters confirmed by user."
            if action == "confirm"
            else "User provided guidance before calibration confirmation."
            if action == "guide"
            else "Navigation calibration was not confirmed by the user."
        ),
        "details": {
            "action": action,
            "text": decision.get("text"),
            "error_type": error_type,
        },
    }
    normalized_decision = _redact_sensitive({
        "action": action,
        "text": decision.get("text"),
        "request_id": decision.get("request_id"),
        "plan_id": plan_id,
        "step_id": step_id,
    })
    decision_key = human_decision_key(normalized_decision)
    existing_handoff = plan_store.get_human_decision_handoff(plan_id, step_id)
    if existing_handoff is None:
        if plan.status != "active":
            return False
        current = plan_store.get_current_step(plan_id)
        if (
            current is None
            or current["step"]["step_id"] != step_id
            or current["step"]["status"] not in {"pending", "waiting_user"}
        ):
            return False
        dependencies = plan_store.dependency_statuses(plan_id, list(step.depends_on))
        if any(dependencies.get(dependency) != "completed" for dependency in step.depends_on):
            return False
    outcome = plan_store.stage_human_decision_handoff(
        plan_id,
        step_id,
        decision_key=decision_key,
        decision=normalized_decision,
        target_status="completed" if payload["ok"] else "failed",
        full_result=payload,
        result_summary=_result_summary(payload),
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    if outcome == "conflict":
        return False
    if plan_store.get_staged_step_result(plan_id, step_id) is None:
        return True
    task = SqliteNavigationTaskStore(plan_store.db_path).get_task(plan.task_id)
    if task is None:
        return False
    result = _finalize_staged_result(
        task=task,
        plan=plan,
        step=step,
        plan_store=plan_store,
        evidence_store=evidence_store,
        settings=None,
    )
    return result.get("status") in {"completed", "failed"}


def human_decision_key(decision: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(decision).encode("utf-8")).hexdigest()


def build_plan_bound_execution_tools(
    *,
    task: NavigationTask,
    plan_store: SqliteNavigationPlanRepository,
    evidence_store: FileNavigationEvidenceStore,
    settings: NavigationSettings,
    dry_run: bool,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
    agentscope_session_id: str | None = None,
) -> list[ToolBase]:
    """Expose only distinct actions remaining in the task's active immutable plan."""
    _ = dry_run  # The durable task is the canonical dry-run authority.
    phase = task.phase.value
    if phase not in {"extract_sync", "finish_processing"}:
        return []
    active = plan_store.get_active(task.task_id, phase)
    if active is None:
        return []
    overview = plan_store.get_execution_overview(active.plan_id)
    remaining_actions: list[str] = []
    for ledger_step in overview.steps:
        if ledger_step.status == "completed" or ledger_step.action in remaining_actions:
            continue
        remaining_actions.append(ledger_step.action)

    tools: list[ToolBase] = []

    def make_invoke(action: str, function: Callable[..., Any]):
        def invoke(plan_id: str, step_id: str) -> dict[str, Any]:
            return _invoke_plan_step(
                bound_task=task,
                plan_id=plan_id,
                step_id=step_id,
                action=action,
                function=function,
                plan_store=plan_store,
                evidence_store=evidence_store,
                settings=settings,
                cancellation=cancellation,
                expected_web_session_id=web_session_id,
                expected_agentscope_session_id=agentscope_session_id,
            )

        invoke.__name__ = f"{action}_tool"
        invoke.__doc__ = (
            "Execute the matching current step from the stored immutable navigation plan."
        )
        return invoke

    for action in remaining_actions:
        if action == _EXTERNAL_ACTION:
            from vla_data_juicer_agents.navigation.agent_tools import (
                PlanBoundHumanDecisionTool,
            )

            tools.append(
                PlanBoundHumanDecisionTool(
                    gate=lambda tool_input: prepare_plan_human_decision(
                        task=task,
                        plan_store=plan_store,
                        settings=settings,
                        plan_id=tool_input.get("plan_id", ""),
                        step_id=tool_input.get("step_id", ""),
                        expected_web_session_id=web_session_id,
                        expected_agentscope_session_id=agentscope_session_id,
                    )
                )
            )
            continue
        if action not in _PROCESSING_ACTIONS:
            continue
        function = globals()[action]
        invoke = make_invoke(action, function)
        tool = FunctionTool(
            invoke,
            name=f"{action}_tool",
            is_concurrency_safe=False,
            is_read_only=task.dry_run,
        )
        tool.input_schema["additionalProperties"] = False
        tools.append(tool)
    return tools
