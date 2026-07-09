from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from vla_data_juicer_agents.navigation.catalog import (
    ToolCapability,
    ToolVariantCapability,
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.models import (
    NavigationExtractSyncProfile,
    NavigationFinishProcessingProfile,
    WorkflowPlan,
    WorkflowStep,
)


def _issue(issue_type: str, message: str, **details: Any) -> dict[str, Any]:
    issue = {"type": issue_type, "message": message}
    if details:
        issue["details"] = details
    return issue


def _catalog_by_tool(catalog: Iterable[ToolCapability]) -> dict[str, ToolCapability]:
    return {capability.tool_name: capability for capability in catalog}


def _step_positions(steps: list[WorkflowStep]) -> dict[str, int]:
    return {step.tool_name: index for index, step in enumerate(steps)}


_CALIBRATION_CONFIRMATION_STEP_ID = "confirm_navigation_calibration_params"
_PROCESSING_STEP_IDS = {
    "extract_and_sync_navigation_data",
    "assemble_finish_temp",
    "run_noobscene_preprocessing",
    "run_initial_annotation_gui",
    "run_tracking",
    "prepare_gridmap_for_projection",
    "run_projection_and_trajectory",
    "validate_navigation_outputs",
}


def _calibration_confirmation_validation_errors(steps: list[WorkflowStep]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    positions = {step.step_id: index for index, step in enumerate(steps)}
    confirmation_position = positions.get(_CALIBRATION_CONFIRMATION_STEP_ID)
    if confirmation_position is None:
        return [
            _issue(
                "missing_calibration_confirmation",
                "WorkflowPlan must include confirm_navigation_calibration_params before any processing",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
            )
        ]

    confirmation_step = steps[confirmation_position]
    if confirmation_step.tool_name != _CALIBRATION_CONFIRMATION_STEP_ID:
        errors.append(
            _issue(
                "invalid_calibration_confirmation_tool",
                "confirm_navigation_calibration_params step must use confirm_navigation_calibration_params tool",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
                tool_name=confirmation_step.tool_name,
            )
        )

    prepare_position = positions.get("prepare_raw_data")
    if confirmation_position != 0:
        errors.append(
            _issue(
                "invalid_calibration_confirmation_order",
                "confirm_navigation_calibration_params must be the first step before any processing",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
            )
        )
    if prepare_position is not None and confirmation_position >= prepare_position:
        errors.append(
            _issue(
                "invalid_calibration_confirmation_order",
                "confirm_navigation_calibration_params must run before prepare_raw_data",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
            )
        )
    for step_id in _PROCESSING_STEP_IDS:
        step_position = positions.get(step_id)
        if step_position is not None and confirmation_position > step_position:
            errors.append(
                _issue(
                    "invalid_calibration_confirmation_order",
                    f"confirm_navigation_calibration_params must run before processing step {step_id}",
                    step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
                    processing_step_id=step_id,
                )
            )

    if confirmation_step.human_blocking is not True:
        errors.append(
            _issue(
                "invalid_calibration_confirmation_flags",
                "confirm_navigation_calibration_params must be human_blocking",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
                field="human_blocking",
            )
        )
    if confirmation_step.failure_behavior != "stop":
        errors.append(
            _issue(
                "invalid_calibration_confirmation_flags",
                "confirm_navigation_calibration_params failure_behavior must be stop",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
                field="failure_behavior",
            )
        )
    if confirmation_step.effects != "read":
        errors.append(
            _issue(
                "invalid_calibration_confirmation_flags",
                "confirm_navigation_calibration_params effects must be read",
                step_id=_CALIBRATION_CONFIRMATION_STEP_ID,
                field="effects",
            )
        )

    return errors


def _precondition_validation_errors(steps: list[WorkflowStep]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    step_ids = {step.step_id for step in steps}
    graph: dict[str, list[str]] = {step.step_id: list(step.preconditions) for step in steps}

    for step in steps:
        for precondition in step.preconditions:
            if precondition not in step_ids:
                errors.append(
                    _issue(
                        "unknown_precondition",
                        "step precondition does not reference a known WorkflowPlan step_id",
                        step_id=step.step_id,
                        precondition=precondition,
                    )
                )

    if errors:
        return errors

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(step_id: str) -> list[str] | None:
        if step_id in visiting:
            return stack[stack.index(step_id) :] + [step_id]
        if step_id in visited:
            return None

        visiting.add(step_id)
        stack.append(step_id)
        for precondition in graph[step_id]:
            cycle = visit(precondition)
            if cycle is not None:
                return cycle
        stack.pop()
        visiting.remove(step_id)
        visited.add(step_id)
        return None

    for step_id in graph:
        cycle = visit(step_id)
        if cycle is not None:
            errors.append(
                _issue(
                    "cyclic_precondition",
                    "WorkflowPlan preconditions must form an acyclic graph",
                    cycle=cycle,
                )
            )
            break

    return errors


def _find_variant(
    capability: ToolCapability,
    variant_id: str,
) -> ToolVariantCapability | None:
    for variant in capability.variants:
        if variant.id == variant_id:
            return variant
    return None


PhaseProfile = NavigationExtractSyncProfile | NavigationFinishProcessingProfile


def _selector_facts(plan: WorkflowPlan, phase_profile: PhaseProfile | None) -> dict[str, str]:
    facts = {
        "processing_profile": plan.processing_profile,
        "platform_hint": plan.platform_hint,
    }
    if phase_profile is not None:
        facts.update(
            {
                "platform_hint": phase_profile.platform_hint,
            }
        )
        if isinstance(phase_profile, NavigationFinishProcessingProfile):
            facts.update(
                {
                    "gridmap_source": phase_profile.gridmap_source,
                    "pcd_gridmap_tool_available": str(phase_profile.pcd_gridmap_tool_available).lower(),
                    "projection_input_ready": str(phase_profile.projection_input_ready).lower(),
                    "localization_source": phase_profile.localization_policy.source,
                    "localization_conversion": phase_profile.localization_policy.conversion,
                }
            )
        if phase_profile.topic_params.profile_hint is not None:
            facts["topic_profile_hint"] = phase_profile.topic_params.profile_hint
        if phase_profile.topic_params.query_dir is not None:
            facts["query_dir"] = phase_profile.topic_params.query_dir
        if isinstance(phase_profile, NavigationFinishProcessingProfile):
            processing_profile = phase_profile.processing_profile
            facts.update(
                {
                    "processing_profile": processing_profile.id,
                    "processing_profile_platform_hint": processing_profile.platform_hint,
                    "gridmap_policy_source": processing_profile.gridmap_policy.source,
                    "calibration_policy_mode": processing_profile.calibration_policy.mode,
                    "calibration_requires_user_confirmation": str(
                        processing_profile.calibration_policy.requires_user_confirmation
                    ).lower(),
                }
            )
            if facts.get("platform_hint") == "unknown" and processing_profile.platform_hint != "unknown":
                facts["platform_hint"] = processing_profile.platform_hint
    return facts


def validate_workflow_plan(
    plan: WorkflowPlan,
    *,
    phase_profile: PhaseProfile | None = None,
    data_profile: PhaseProfile | None = None,
    catalog: Iterable[ToolCapability] | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    is_extract_sync = plan.phase == "extract_sync"
    effective_profile = phase_profile if phase_profile is not None else data_profile

    if not plan.processing_profile.strip():
        errors.append(
            _issue(
                "missing_processing_profile",
                "WorkflowPlan.processing_profile must be non-empty",
                processing_profile=plan.processing_profile,
            )
        )

    if effective_profile is not None and effective_profile.blocking_issues and plan.steps and not is_extract_sync:
        errors.append(
            _issue(
                "blocking_profile_has_active_plan",
                "phase profile has blocking issues but plan contains active steps",
                issues=[issue.type for issue in effective_profile.blocking_issues],
            )
        )
    if (
        isinstance(effective_profile, NavigationFinishProcessingProfile)
        and effective_profile.gridmap_source == "unknown"
        and not effective_profile.pcd_gridmap_tool_available
        and not is_extract_sync
    ):
        errors.append(
            _issue(
                "missing_gridmap_source_or_generator",
                "grid_map is required but no existing source or PCD generator is available",
            )
        )

    catalog_by_tool = _catalog_by_tool(catalog or list_navigation_tool_capabilities())
    selector_facts = _selector_facts(plan, effective_profile)
    for step in plan.steps:
        capability = catalog_by_tool.get(step.tool_name)
        if capability is None:
            errors.append(
                _issue(
                    "unknown_tool",
                    "tool is not available in the navigation capability catalog",
                    tool_name=step.tool_name,
                )
            )
            continue
        if step.variant is not None:
            variant = _find_variant(capability, step.variant)
            if variant is None or variant.status != "available":
                errors.append(
                    _issue(
                        "unknown_or_unavailable_variant",
                        "variant is not available for the selected tool",
                        tool_name=step.tool_name,
                        variant=step.variant,
                    )
                )
                continue
            for selector_key, allowed_values in variant.selectors.items():
                actual = selector_facts.get(selector_key)
                if actual not in allowed_values:
                    errors.append(
                        _issue(
                            "variant_selector_mismatch",
                            "variant selector does not match the phase profile facts",
                            tool_name=step.tool_name,
                            variant=step.variant,
                            selector=selector_key,
                            actual=actual,
                            allowed=allowed_values,
                        )
                    )

    errors.extend(_precondition_validation_errors(plan.steps))
    if not is_extract_sync:
        errors.extend(_calibration_confirmation_validation_errors(plan.steps))

    if not is_extract_sync:
        positions = _step_positions(plan.steps)
        gridmap_position = positions.get("prepare_gridmap_for_projection")
        if gridmap_position is not None:
            tracking_position = positions.get("run_tracking")
            projection_position = positions.get("run_projection_and_trajectory")
            if tracking_position is not None and gridmap_position <= tracking_position:
                errors.append(
                    _issue(
                        "invalid_gridmap_stage_order",
                        "prepare_gridmap_for_projection must run after run_tracking",
                    )
                )
            if projection_position is not None and gridmap_position >= projection_position:
                errors.append(
                    _issue(
                        "invalid_gridmap_stage_order",
                        "prepare_gridmap_for_projection must run before run_projection_and_trajectory",
                    )
                )

    return {"ok": not errors, "errors": errors, "warnings": warnings}
