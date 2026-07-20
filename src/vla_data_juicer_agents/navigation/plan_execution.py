from __future__ import annotations

import asyncio
import json
import hashlib
import threading
from contextlib import nullcontext
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
    GridmapArtifactsObservation,
    RuntimeAssetsObservation,
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
    NavigationExecutionSnapshot,
    SqliteNavigationPlanRepository,
    StepClaimOutcome,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


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
_GLOBAL_HEAVY_WRITER_CAPACITY = threading.BoundedSemaphore(1)
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


def _session_mismatch_error() -> dict[str, Any]:
    return _compact_error(
        "navigation_task_session_mismatch",
        "The bound navigation task is no longer the current attempt for this session.",
        next_action="inspect_current_navigation_task",
    )


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
            "localization_source": plan.decisions.localization.source,
            **common,
        }
    if action == "validate_navigation_outputs":
        return {"date": task.date, "segments": task.segments, **common}
    raise ValueError(f"unsupported navigation plan action: {action}")


def verify_plan_step_preconditions(
    *,
    task: NavigationTask,
    plan: NavigationPlanRecord,
    step: Any,
    settings: NavigationSettings,
    plan_store: SqliteNavigationPlanRepository,
) -> dict[str, Any] | None:
    """Check only concrete inputs required by canonical arguments for one step."""
    arguments = resolve_step_arguments(
        task=task,
        plan=plan.plan,
        step=step,
        settings=settings,
    )
    missing: list[Path] = []

    def require(path: Path) -> None:
        if not path.exists():
            missing.append(path)

    def require_segments(root: Path, *, suffix: tuple[str, ...] = ()) -> None:
        if task.segments is None:
            require(root)
            return
        for segment in task.segments:
            require(root.joinpath(segment, *suffix))

    def require_gridmap_json_dir(path: Path) -> None:
        if not path.is_dir() or not any(child.is_file() for child in path.glob("*.json")):
            missing.append(path)

    action = step.action
    if action == "prepare_raw_data":
        require_segments(settings.raw_data_root / task.date)
    elif action == "extract_and_sync_navigation_data":
        raw_temp = settings.raw_data_root / f"{task.date}_temp"
        input_root = (
            raw_temp
            if raw_temp.exists() or not task.dry_run
            else settings.raw_data_root / task.date
        )
        require_segments(input_root)
    elif action in {_EXTERNAL_ACTION, "assemble_finish_temp"}:
        sensor_source = Path(arguments["selected_sensor_source"])
        require(sensor_source)
        if action == "assemble_finish_temp":
            require(sensor_source / "fisheye_front.json")
            require(sensor_source / "r32_rslidar_points.json")
            clip_date_root = settings.clip_data_root / task.date
            require_segments(clip_date_root, suffix=("sync_data",))
            segment_names = task.segments or (
                sorted(path.name for path in clip_date_root.iterdir() if path.is_dir())
                if clip_date_root.is_dir()
                else []
            )
            for segment_name in segment_names:
                sync_root = clip_date_root / segment_name / "sync_data"
                if not sync_root.is_dir():
                    continue
                if not any(
                    all(
                        child.is_dir()
                        and any(path.is_file() for path in child.iterdir())
                        for child in (
                            sequence / "fisheye_front",
                            sequence / "r32_rslidar_points",
                        )
                    )
                    for sequence in sync_root.iterdir()
                    if sequence.is_dir()
                ):
                    missing.append(sync_root)
    elif action == "prepare_gridmap_for_projection":
        observation = SqliteNavigationObservationStore(
            plan_store.db_path,
            initialize=False,
        ).get(task.task_id, plan.observation_revision)
        payloads = observation.payloads if observation is not None else []
        gridmap = next(
            (
                payload
                for payload in payloads
                if isinstance(payload, GridmapArtifactsObservation)
            ),
            None,
        )
        runtime = next(
            (
                payload
                for payload in payloads
                if isinstance(payload, RuntimeAssetsObservation)
            ),
            None,
        )
        source = plan.plan.decisions.gridmap.source
        expected_variant = {
            "existing_gridmap": "copy_existing_gridmap",
            "generated_from_pcd": "generate_from_pcd",
            "projection_ready": "skip_if_projection_ready",
        }[source]
        canonical_variant = arguments["gridmap_variant"]
        if canonical_variant != expected_variant or gridmap is None:
            missing.append(Path(f"accepted_observation:{source}:{canonical_variant}"))
        elif canonical_variant == "copy_existing_gridmap":
            if not gridmap.existing_gridmap_paths:
                missing.append(Path("accepted_observation:existing_gridmap_paths"))
            for observed_path in gridmap.existing_gridmap_paths:
                require_gridmap_json_dir(Path(observed_path))
        elif canonical_variant == "generate_from_pcd":
            if not gridmap.pcd_sources:
                missing.append(Path("accepted_observation:pcd_sources"))
            for observed_path in gridmap.pcd_sources:
                if not Path(observed_path).is_file():
                    missing.append(Path(observed_path))
            if runtime is None or not runtime.pcd_gridmap_tool_available:
                missing.append(settings.pcd_to_grid_script)
            elif not settings.pcd_to_grid_script.is_file():
                missing.append(settings.pcd_to_grid_script)
        elif canonical_variant == "skip_if_projection_ready":
            if not gridmap.projection_ready:
                missing.append(Path("accepted_observation:projection_ready"))
            if not gridmap.existing_gridmap_paths:
                missing.append(Path("accepted_observation:projection_ready_paths"))
            for observed_path in gridmap.existing_gridmap_paths:
                require_gridmap_json_dir(Path(observed_path))
    elif action in {
        "run_noobscene_preprocessing",
        "run_initial_annotation_gui",
        "run_tracking",
        "run_projection_and_trajectory",
    }:
        if not task.dry_run:
            require(Path(arguments["finish_temp_path"]))
        if action == "run_noobscene_preprocessing":
            noobscene_root = settings.processing_root / "NoobScenes"
            localization_source = arguments["localization_source"]
            require(
                noobscene_root
                / ("main_smart.py" if localization_source == "ins" else "main_smart_odom.py")
            )
            if localization_source == "odom":
                require(noobscene_root / "include" / "1_odom_convert.py")
                require(noobscene_root / "include" / "2_resize.py")
        elif action == "run_projection_and_trajectory":
            pt_project = settings.processing_root / "2_pt_project"
            localization_source = arguments["localization_source"]
            require(
                pt_project
                / (
                    "4_speed_direction_Ins.py"
                    if localization_source == "ins"
                    else "4_speed_direction_odom.py"
                )
            )
            require(
                pt_project
                / (
                    "2_othermethod_cjl.py"
                    if localization_source == "ins"
                    else "2_othermethod_cjl_0525.py"
                )
            )

    if not missing:
        return None
    return {
        "action": action,
        "canonical_argument_keys": sorted(arguments),
        "missing_inputs": [str(path)[:800] for path in missing[:20]],
        "missing_input_count": len(missing),
    }


def _record_changed_preconditions(
    *,
    task: NavigationTask,
    plan: NavigationPlanRecord,
    step: Any,
    failure: dict[str, Any],
    plan_store: SqliteNavigationPlanRepository,
    evidence_store: FileNavigationEvidenceStore,
    expected_web_session_id: str | None,
    expected_agentscope_session_id: str | None,
) -> dict[str, Any]:
    descriptor = evidence_store.write(
        task.task_id,
        plan.observation_revision,
        "execution_precondition",
        step.action,
        failure,
        f"Input precondition changed before {step.step_id}",
    )
    try:
        transitioned = plan_store.mark_needs_replan(
            plan.plan_id,
            "input_precondition_changed",
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    except PermissionError:
        evidence_store.delete(task.task_id, descriptor.ref)
        return _session_mismatch_error()
    except Exception:
        evidence_store.delete(task.task_id, descriptor.ref)
        raise
    if not transitioned:
        evidence_store.delete(task.task_id, descriptor.ref)
        return _terminal_error(plan_store, plan.plan_id)
    return _compact_error(
        "input_precondition_changed",
        "A concrete input required by the accepted plan changed before execution.",
        result_ref=descriptor.ref,
        missing_input_count=failure["missing_input_count"],
        next_action="submit_complete_plan",
    )


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
) -> tuple[
    NavigationTask | None,
    NavigationPlanRecord | None,
    Any | None,
    NavigationExecutionSnapshot | None,
    dict[str, Any] | None,
]:
    snapshot = plan_store.read_execution_snapshot(
        web_session_id=expected_web_session_id,
        agentscope_session_id=expected_agentscope_session_id,
        task_id=bound_task.task_id,
    )
    if snapshot is None:
        snapshot = plan_store.read_claim_terminalization_snapshot(
            plan_id=requested_plan_id,
            step_id=requested_step_id,
            action=expected_action,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
        if snapshot is not None and snapshot.task.task_id != bound_task.task_id:
            snapshot = None
    if snapshot is None:
        return bound_task, None, None, None, _compact_error(
            "navigation_task_session_mismatch",
            "The plan-bound task is no longer owned by this AgentScope session.",
        )
    task = snapshot.task
    plan = snapshot.active_plan
    if (
        plan is None
        or plan.plan_id != requested_plan_id
        or plan.task_id != task.task_id
        or plan.status != "active"
    ):
        return task, plan, None, snapshot, _compact_error(
            "plan_not_active",
            "The requested plan is missing, inactive, or superseded.",
            next_action="load_active_plan",
        )
    step = _plan_step(plan, requested_step_id)
    if step is None:
        return task, plan, None, snapshot, _compact_error(
            "step_not_found",
            "The requested step is not part of the immutable plan.",
            next_action="get_current_step",
        )
    if step.action != expected_action:
        return task, plan, step, snapshot, _compact_error(
            "step_action_mismatch",
            "The requested wrapper does not match the stored step action.",
            next_action=step.action,
        )
    current = snapshot.current
    if current is None or current["step"]["step_id"] != step.step_id:
        return task, plan, step, snapshot, _compact_error(
            "step_not_current",
            "Only the current executable ledger step may run.",
            next_action=(current or {}).get("step", {}).get("action"),
        )
    dependencies = snapshot.dependency_statuses
    unmet = [
        dependency
        for dependency in step.depends_on
        if dependencies.get(dependency) != "completed"
    ]
    if unmet:
        return task, plan, step, snapshot, _compact_error(
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
            return task, plan, step, snapshot, calibration_error
    return task, plan, step, snapshot, None


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
    expected_web_session_id: str | None,
    expected_agentscope_session_id: str | None,
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
    wrote_evidence = False
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
            wrote_evidence = True
        if not plan_store.attach_staged_result_evidence(
            plan.plan_id, step.step_id, result_ref,
            expected_action=step.action,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        ):
            if wrote_evidence:
                evidence_store.delete(task.task_id, result_ref)
            return _result_finalize_retry_error()
    except PermissionError:
        if wrote_evidence:
            evidence_store.delete(task.task_id, result_ref)
        return _session_mismatch_error()
    except Exception:
        if wrote_evidence:
            evidence_store.delete(task.task_id, result_ref)
        return _result_finalize_retry_error()
    try:
        finalized = plan_store.finalize_staged_step(
            plan.plan_id, step.step_id,
            expected_action=step.action,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    except Exception:
        return _result_finalize_retry_error()
    if not finalized:
        return _result_finalize_retry_error()
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
    task, plan, step, snapshot, gate_error = _gate_step(
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
    assert snapshot is not None
    staged = snapshot.staged_result
    if staged is not None:
        return _finalize_staged_result(
            task=task,
            plan=plan,
            step=step,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    current = snapshot.current
    current_status = (current or {}).get("step", {}).get("status")
    if current_status == "running":
        try:
            recovered = plan_store.recover_running_step_without_result(
                plan.plan_id,
                step.step_id,
                "running step has no staged result after process interruption",
                expected_action=step.action,
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
    precondition_failure = verify_plan_step_preconditions(
        task=task,
        plan=plan,
        step=step,
        settings=settings,
        plan_store=plan_store,
    )
    if precondition_failure is not None:
        return _record_changed_preconditions(
            task=task,
            plan=plan,
            step=step,
            failure=precondition_failure,
            plan_store=plan_store,
            evidence_store=evidence_store,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    claim_outcome = plan_store.claim_step(
        plan.plan_id,
        step.step_id,
        step.action,
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    if claim_outcome is StepClaimOutcome.NAVIGATION_DATA_BUSY:
        return _compact_error(
            "navigation_data_busy",
            "An overlapping navigation data write is already running.",
            retry="wait_and_reinspect",
        )
    if claim_outcome is not StepClaimOutcome.CLAIMED:
        if plan_store.read_execution_snapshot(
            web_session_id=expected_web_session_id,
            agentscope_session_id=expected_agentscope_session_id,
            task_id=task.task_id,
        ) is None:
            return _session_mismatch_error()
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
                expected_action=step.action,
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
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
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
                expected_action=step.action,
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
            expected_action=step.action,
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
            expected_action=step.action,
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
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    if oversized and response.get("error_type") != "result_finalize_retry_required":
        plan_store.mark_terminalized_claim_needs_replan(
            plan.plan_id,
            step.step_id,
            "processing result exceeded the durable outbox payload policy",
            expected_action=step.action,
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
    evidence_store: FileNavigationEvidenceStore,
    settings: NavigationSettings,
    plan_id: str,
    step_id: str,
    expected_web_session_id: str | None = None,
    expected_agentscope_session_id: str | None = None,
) -> dict[str, Any] | None:
    durable_task, plan, step, snapshot, gate_error = _gate_step(
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
    assert durable_task is not None and plan is not None and step is not None
    assert snapshot is not None
    if (
        snapshot.handoff is not None
        and snapshot.handoff.status == "recovery_required"
    ):
        return _compact_error(
            "human_handoff_recovery_required",
            "The human-decision handoff requires controlled recovery before "
            "another request may be authorized.",
            next_action="quarantine_human_decision_handoff",
        )
    current = snapshot.current
    waiting_user = current is not None and current["step"]["status"] == "waiting_user"
    precondition_failure = verify_plan_step_preconditions(
        task=durable_task,
        plan=plan,
        step=step,
        settings=settings,
        plan_store=plan_store,
    )
    if precondition_failure is not None:
        if waiting_user:
            descriptor = evidence_store.write(
                durable_task.task_id,
                plan.observation_revision,
                "execution_precondition",
                step.action,
                precondition_failure,
                f"Input precondition changed before {step.step_id} retry",
            )
            try:
                anchored = plan_store.mark_human_decision_recovery_required(
                    plan.plan_id,
                    step.step_id,
                    reason_code="input_precondition_changed",
                    request_anchor={
                        "plan_id": plan.plan_id,
                        "request_state": "waiting_user",
                        "step_id": step.step_id,
                    },
                    expected_web_session_id=expected_web_session_id,
                    expected_agentscope_session_id=expected_agentscope_session_id,
                )
            except PermissionError:
                evidence_store.delete(durable_task.task_id, descriptor.ref)
                return _session_mismatch_error()
            except Exception:
                evidence_store.delete(durable_task.task_id, descriptor.ref)
                raise
            if not anchored:
                evidence_store.delete(durable_task.task_id, descriptor.ref)
                return _terminal_error(plan_store, plan.plan_id)
            return _compact_error(
                "input_precondition_changed",
                "A concrete input changed while the human decision request was waiting; audited recovery is required before replanning.",
                result_ref=descriptor.ref,
                missing_input_count=precondition_failure["missing_input_count"],
                recovery_required=True,
                next_action="quarantine_human_decision_handoff",
            )
        return _record_changed_preconditions(
            task=durable_task,
            plan=plan,
            step=step,
            failure=precondition_failure,
            plan_store=plan_store,
            evidence_store=evidence_store,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    if waiting_user:
        return None
    try:
        marked_waiting = plan_store.mark_waiting_user(
            plan.plan_id,
            step.step_id,
            step.action,
            expected_web_session_id=expected_web_session_id,
            expected_agentscope_session_id=expected_agentscope_session_id,
        )
    except PermissionError:
        return _session_mismatch_error()
    if not marked_waiting:
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
    try:
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
    except PermissionError:
        return False
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
        expected_web_session_id=expected_web_session_id,
        expected_agentscope_session_id=expected_agentscope_session_id,
    )
    return result.get("status") in {"completed", "failed"}


def human_decision_key(decision: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(decision).encode("utf-8")).hexdigest()


def build_plan_bound_execution_tools(
    *,
    task: NavigationTask,
    snapshot: NavigationExecutionSnapshot | None = None,
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
    snapshot = snapshot or plan_store.read_execution_snapshot(
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
        task_id=task.task_id,
    )
    if (
        snapshot is None
        or snapshot.active_plan is None
        or snapshot.overview is None
        or snapshot.activity != "execution"
    ):
        return []
    task = snapshot.task
    overview = snapshot.overview
    remaining_actions: list[str] = []
    for ledger_step in overview.steps:
        if ledger_step.status == "completed" or ledger_step.action in remaining_actions:
            continue
        remaining_actions.append(ledger_step.action)

    tools: list[ToolBase] = []

    def make_invoke(action: str, function: Callable[..., Any]):
        async def invoke(plan_id: str, step_id: str) -> dict[str, Any]:
            active_cancellation = cancellation or current_cancellation()
            background_token = (
                active_cancellation.begin_background_operation()
                if active_cancellation is not None
                else None
            )
            def invoke_in_capacity() -> dict[str, Any]:
                capacity = (
                    _GLOBAL_HEAVY_WRITER_CAPACITY
                    if not task.dry_run
                    else nullcontext()
                )
                with capacity:
                    return _invoke_plan_step(
                        bound_task=task,
                        plan_id=plan_id,
                        step_id=step_id,
                        action=action,
                        function=function,
                        plan_store=plan_store,
                        evidence_store=evidence_store,
                        settings=settings,
                        cancellation=active_cancellation,
                        expected_web_session_id=web_session_id,
                        expected_agentscope_session_id=agentscope_session_id,
                    )

            thread_task = asyncio.create_task(asyncio.to_thread(invoke_in_capacity))
            if active_cancellation is not None and background_token is not None:
                thread_task.add_done_callback(
                    lambda _task: active_cancellation.end_background_operation(
                        background_token,
                    ),
                )
            return await asyncio.shield(thread_task)

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
                        evidence_store=evidence_store,
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
