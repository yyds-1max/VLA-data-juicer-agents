from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

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
from vla_data_juicer_agents.navigation.observation_models import (
    CalibrationInventoryObservation,
    EvidenceDescriptor,
    GridmapArtifactsObservation,
    LocalizationSourcesObservation,
    NavigationObservationRevision,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    SensorCandidatesObservation,
    TopicCandidatesObservation,
)
from vla_data_juicer_agents.navigation.plan_models import (
    EmptyArguments,
    ExtractSyncArguments,
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    PlanValidationIssue,
    PlanValidationReport,
)
from vla_data_juicer_agents.navigation.planning_context import (
    PHASE_REQUIRED_OBSERVATIONS,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask


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


# Complete model-authored plan validation.  This intentionally does not depend on
# the legacy profile/draft validator above; both paths coexist only until the
# later legacy-removal task lands.
MAX_PUBLIC_PLAN_VALIDATION_ISSUES = 8
_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "EmptyArguments": EmptyArguments,
    "ExtractSyncArguments": ExtractSyncArguments,
}
_GRIDMAP_VARIANT_BY_SOURCE = {
    "existing_gridmap": "copy_existing_gridmap",
    "generated_from_pcd": "generate_from_pcd",
    "projection_ready": "skip_if_projection_ready",
}


def _plan_issue(
    path: str,
    code: str,
    message: str,
    allowed_values: Sequence[str] = (),
) -> PlanValidationIssue:
    return PlanValidationIssue(
        path=path,
        code=code,
        message=message,
        allowed_values=list(allowed_values),
    )


def _bounded_allowed_values(values: Iterable[str]) -> list[str]:
    normalized = sorted(set(values))
    if len(normalized) <= 20:
        return normalized
    return ["see_observation_evidence"]


def _report(
    errors: Iterable[PlanValidationIssue],
    warnings: Iterable[PlanValidationIssue] = (),
) -> PlanValidationReport:
    def stable_unique(
        issues: Iterable[PlanValidationIssue],
    ) -> list[PlanValidationIssue]:
        indexed: dict[tuple[str, str], PlanValidationIssue] = {}
        for issue in issues:
            indexed.setdefault((issue.path, issue.code), issue)
        return [indexed[key] for key in sorted(indexed)][:MAX_PUBLIC_PLAN_VALIDATION_ISSUES]

    public_errors = stable_unique(errors)
    public_warnings = stable_unique(warnings)
    return PlanValidationReport(
        ok=not public_errors,
        errors=public_errors,
        warnings=public_warnings,
    )


def _payload_of_type(
    observation: NavigationObservationRevision,
    model: type[BaseModel],
) -> Any | None:
    return next(
        (payload for payload in observation.payloads if isinstance(payload, model)),
        None,
    )


def _capability_items(
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> list[ToolCapability]:
    raw_items: Sequence[ToolCapability | dict[str, Any]]
    if isinstance(capabilities, dict):
        raw_items = capabilities.get("capabilities", [])
    else:
        raw_items = capabilities
    return [
        item if isinstance(item, ToolCapability) else ToolCapability.model_validate(item)
        for item in raw_items
    ]


def _decision_entries(
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput,
) -> list[tuple[str, Any]]:
    names = type(plan.decisions).model_fields
    return [(name, getattr(plan.decisions, name)) for name in names]


def _validate_evidence_refs(
    *,
    task: NavigationTask,
    observation: NavigationObservationRevision,
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput,
    evidence: Sequence[EvidenceDescriptor],
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    by_ref = {descriptor.ref: descriptor for descriptor in evidence}
    for decision_name, decision in _decision_entries(plan):
        for index, ref in enumerate(decision.evidence_refs):
            path = f"plan.decisions.{decision_name}.evidence_refs.{index}"
            descriptor = by_ref.get(ref)
            if descriptor is None:
                errors.append(
                    _plan_issue(
                        path,
                        "unknown_evidence_ref",
                        "Evidence reference does not exist",
                    )
                )
            elif descriptor.task_id != task.task_id:
                errors.append(
                    _plan_issue(
                        path,
                        "evidence_task_mismatch",
                        "Evidence reference belongs to another task",
                    )
                )
            elif descriptor.observation_revision > observation.revision:
                errors.append(
                    _plan_issue(
                        path,
                        "evidence_revision_mismatch",
                        "Evidence reference is newer than the planning observation",
                        [str(observation.revision)],
                    )
                )
    return errors


def _validate_extract_references(
    observation: NavigationObservationRevision,
    plan: ExtractSyncPlanInput,
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    raw = _payload_of_type(observation, RawMetadataObservation)
    topics = _payload_of_type(observation, TopicCandidatesObservation)
    sensors = _payload_of_type(observation, SensorCandidatesObservation)
    available_topics: set[str] = set()
    if raw is not None:
        available_topics.update(measurement.topic for measurement in raw.topics)
    if topics is not None:
        available_topics.update(topics.available_topics)
    allowed_topics = _bounded_allowed_values(available_topics)

    observed_bindings: dict[str, set[str]] = {}
    if sensors is not None:
        for candidate in sensors.candidates:
            observed_bindings.setdefault(candidate.role, set()).add(candidate.topic)

    for role, topic in plan.decisions.sensor_bindings.bindings.items():
        path = f"plan.decisions.sensor_bindings.bindings.{role}"
        if topic not in available_topics:
            errors.append(
                _plan_issue(
                    path,
                    "unobserved_topic",
                    "Selected topic was not observed",
                    allowed_topics,
                )
            )
        elif role not in observed_bindings or topic not in observed_bindings[role]:
            errors.append(
                _plan_issue(
                    path,
                    "unobserved_sensor_binding",
                    "Sensor binding was not present in observed candidates",
                    _bounded_allowed_values(observed_bindings.get(role, set())),
                )
            )

    selection = plan.decisions.topic_selection
    for index, topic in enumerate(selection.topic_whitelist):
        if topic not in available_topics:
            errors.append(
                _plan_issue(
                    f"plan.decisions.topic_selection.topic_whitelist.{index}",
                    "unobserved_topic",
                    "Selected topic was not observed",
                    allowed_topics,
                )
            )
    binding_roles = sorted(plan.decisions.sensor_bindings.bindings)
    for topic, role in selection.topic_map.items():
        path = f"plan.decisions.topic_selection.topic_map.{topic}"
        if topic not in available_topics:
            errors.append(
                _plan_issue(
                    path,
                    "unobserved_topic",
                    "Selected topic was not observed",
                    allowed_topics,
                )
            )
        if role not in plan.decisions.sensor_bindings.bindings:
            errors.append(
                _plan_issue(
                    path,
                    "unknown_sensor_role",
                    "Mapped sensor role does not exist",
                    binding_roles,
                )
            )

    reference = plan.decisions.time_sync.reference_sensor
    if reference not in plan.decisions.sensor_bindings.bindings:
        errors.append(
            _plan_issue(
                "plan.decisions.time_sync.reference_sensor",
                "unknown_sensor_role",
                "Referenced sensor role does not exist",
                binding_roles,
            )
        )
    return errors


def _validate_finish_references(
    observation: NavigationObservationRevision,
    plan: FinishProcessingPlanInput,
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    localization = _payload_of_type(observation, LocalizationSourcesObservation)
    gridmap = _payload_of_type(observation, GridmapArtifactsObservation)
    runtime = _payload_of_type(observation, RuntimeAssetsObservation)
    calibration = _payload_of_type(observation, CalibrationInventoryObservation)

    localization_decision = plan.decisions.localization
    if (
        localization is not None
        and localization_decision.source not in localization.available_sources
    ):
        errors.append(
            _plan_issue(
                "plan.decisions.localization.source",
                "unobserved_localization_source",
                "Localization source was not observed",
                localization.available_sources,
            )
        )
    expected_conversion = (
        "odom_to_ins" if localization_decision.source == "odom" else "none"
    )
    if localization_decision.conversion != expected_conversion:
        errors.append(
            _plan_issue(
                "plan.decisions.localization.conversion",
                "invalid_localization_conversion",
                "Localization conversion does not match the selected source",
                [expected_conversion],
            )
        )
    elif (
        localization_decision.conversion == "odom_to_ins"
        and localization is not None
        and not localization.conversion_available
    ):
        errors.append(
            _plan_issue(
                "plan.decisions.localization.conversion",
                "localization_conversion_unavailable",
                "The observed odom-to-ins converter is unavailable",
            )
        )

    gridmap_decision = plan.decisions.gridmap
    source_observed = {
        "existing_gridmap": bool(gridmap and gridmap.existing_gridmap_paths),
        "generated_from_pcd": bool(gridmap and gridmap.pcd_sources),
        "projection_ready": bool(gridmap and gridmap.projection_ready),
    }
    if not source_observed[gridmap_decision.source]:
        errors.append(
            _plan_issue(
                "plan.decisions.gridmap.source",
                "unobserved_gridmap_source",
                "Gridmap source is not supported by observed artifacts",
                [source for source, observed in source_observed.items() if observed],
            )
        )
    if (
        gridmap_decision.source == "generated_from_pcd"
        and runtime is not None
        and not runtime.pcd_gridmap_tool_available
    ):
        errors.append(
            _plan_issue(
                "plan.decisions.gridmap.source",
                "gridmap_capability_unavailable",
                "PCD gridmap generation capability is unavailable",
            )
        )

    calibration_decision = plan.decisions.calibration
    if (
        calibration is not None
        and calibration_decision.selected_sensor_source not in calibration.sensor_sources
    ):
        errors.append(
            _plan_issue(
                "plan.decisions.calibration.selected_sensor_source",
                "unobserved_calibration_source",
                "Calibration sensor source was not observed",
                calibration.sensor_sources,
            )
        )
    if (
        calibration_decision.mode == "hardcoded_with_user_confirmation"
        and not calibration_decision.requires_user_confirmation
    ):
        errors.append(
            _plan_issue(
                "plan.decisions.calibration.requires_user_confirmation",
                "calibration_confirmation_required",
                "Hardcoded calibration requires user confirmation",
                ["true"],
            )
        )

    for index, step in enumerate(plan.steps):
        if step.action == "prepare_gridmap_for_projection":
            expected_variant = _GRIDMAP_VARIANT_BY_SOURCE[gridmap_decision.source]
            if step.variant != expected_variant:
                errors.append(
                    _plan_issue(
                        f"plan.steps.{index}.variant",
                        "variant_decision_mismatch",
                        "Gridmap variant does not match the selected source",
                        [expected_variant],
                    )
                )
        if (
            step.action == "run_projection_and_trajectory"
            and runtime is not None
            and not runtime.projection_variants.get(step.variant, False)
        ):
            errors.append(
                _plan_issue(
                    f"plan.steps.{index}.variant",
                    "projection_variant_unavailable",
                    "Projection variant is unavailable in observed runtime assets",
                    [name for name, available in runtime.projection_variants.items() if available],
                )
            )
    return errors


def _validate_capability_contracts(
    *,
    phase: str,
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput,
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    phase_capabilities = {
        capability.tool_name: capability
        for capability in _capability_items(capabilities)
        if capability.phase == phase and capability.executor_agent_allowed
    }
    allowed_actions = sorted(
        action
        for action, capability in phase_capabilities.items()
        if any(variant.status == "available" for variant in capability.variants)
    )
    decision_names = sorted(type(plan.decisions).model_fields)
    for index, step in enumerate(plan.steps):
        capability = phase_capabilities.get(step.action)
        if capability is None:
            errors.append(
                _plan_issue(
                    f"plan.steps.{index}.action",
                    "unknown_action",
                    "Action is not available in the active phase",
                    allowed_actions,
                )
            )
            continue
        allowed_variants = sorted(
            variant.id
            for variant in capability.variants
            if variant.status == "available"
        )
        if step.variant not in allowed_variants:
            errors.append(
                _plan_issue(
                    f"plan.steps.{index}.variant",
                    "unknown_variant",
                    "Variant is not available for the selected action",
                    allowed_variants,
                )
            )
        argument_model = _ARGUMENT_MODELS.get(capability.argument_model or "")
        arguments = (
            step.arguments.model_dump(mode="json")
            if isinstance(step.arguments, BaseModel)
            else step.arguments
        )
        if argument_model is None:
            errors.append(
                _plan_issue(
                    f"plan.steps.{index}.arguments",
                    "unknown_argument_contract",
                    "Action argument contract is not registered",
                )
            )
        else:
            try:
                argument_model.model_validate(arguments)
            except ValidationError:
                errors.append(
                    _plan_issue(
                        f"plan.steps.{index}.arguments",
                        "invalid_arguments",
                        "Arguments do not satisfy the selected action contract",
                    )
                )
        for ref_index, ref in enumerate(step.decision_refs):
            if ref not in decision_names:
                errors.append(
                    _plan_issue(
                        f"plan.steps.{index}.decision_refs.{ref_index}",
                        "unknown_decision_ref",
                        "Step decision reference does not exist",
                        decision_names,
                    )
                )
    return errors


def _validate_dependencies(
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput,
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    positions: dict[str, int] = {}
    for index, step in enumerate(plan.steps):
        if step.step_id in positions:
            errors.append(
                _plan_issue(
                    f"plan.steps.{index}.step_id",
                    "duplicate_step_id",
                    "Step id is duplicated",
                )
            )
        else:
            positions[step.step_id] = index

    unknown_dependencies = False
    for index, step in enumerate(plan.steps):
        for dependency_index, dependency in enumerate(step.depends_on):
            path = f"plan.steps.{index}.depends_on.{dependency_index}"
            if dependency not in positions:
                unknown_dependencies = True
                errors.append(
                    _plan_issue(
                        path,
                        "unknown_dependency",
                        "Dependency does not reference a plan step",
                        positions,
                    )
                )
            elif positions[dependency] >= index:
                errors.append(
                    _plan_issue(
                        path,
                        "dependency_not_before_step",
                        "Dependency must appear before the dependent step",
                    )
                )

    if unknown_dependencies or len(positions) != len(plan.steps):
        return errors

    graph = {step.step_id: list(step.depends_on) for step in plan.steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False
        visiting.add(step_id)
        if any(visit(dependency) for dependency in graph[step_id]):
            return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    if any(visit(step_id) for step_id in graph if step_id not in visited):
        errors.append(
            _plan_issue(
                "plan.steps",
                "dependency_cycle",
                "Step dependencies must form an acyclic graph",
            )
        )
    return errors


def _validate_finish_business_order(
    plan: FinishProcessingPlanInput,
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    positions: dict[str, int] = {}
    for index, step in enumerate(plan.steps):
        positions.setdefault(step.action, index)

    confirmation = positions.get("confirm_navigation_calibration_params")
    if plan.decisions.calibration.requires_user_confirmation and confirmation is None:
        errors.append(
            _plan_issue(
                "plan.steps",
                "missing_calibration_confirmation",
                "Plan requires a calibration confirmation step",
                ["confirm_navigation_calibration_params"],
            )
        )
    elif confirmation is not None and confirmation != 0:
        errors.append(
            _plan_issue(
                f"plan.steps.{confirmation}",
                "calibration_confirmation_not_first",
                "Calibration confirmation must precede processing",
            )
        )

    tracking = positions.get("run_tracking")
    gridmap = positions.get("prepare_gridmap_for_projection")
    projection = positions.get("run_projection_and_trajectory")
    validation = positions.get("validate_navigation_outputs")
    if tracking is not None and gridmap is not None and gridmap < tracking:
        errors.append(
            _plan_issue(
                f"plan.steps.{gridmap}",
                "gridmap_before_tracking",
                "Gridmap preparation must run after tracking",
            )
        )
    if gridmap is not None and projection is not None and projection < gridmap:
        errors.append(
            _plan_issue(
                f"plan.steps.{projection}",
                "projection_before_gridmap",
                "Projection must run after gridmap preparation",
            )
        )
    if validation is not None and validation != len(plan.steps) - 1:
        errors.append(
            _plan_issue(
                f"plan.steps.{validation}",
                "validation_not_last",
                "Output validation must be the final plan step",
            )
        )
    return errors


def validate_navigation_plan(
    *,
    task: NavigationTask,
    observation: NavigationObservationRevision,
    plan: ExtractSyncPlanInput | FinishProcessingPlanInput,
    evidence: Sequence[EvidenceDescriptor],
    capabilities: Sequence[ToolCapability] | dict[str, Any],
) -> PlanValidationReport:
    """Validate one complete model-authored phase plan without mutating stores."""
    errors: list[PlanValidationIssue] = []
    if isinstance(plan, ExtractSyncPlanInput):
        phase = "extract_sync"
    elif isinstance(plan, FinishProcessingPlanInput):
        phase = "finish_processing"
    else:
        return _report(
            [
                _plan_issue(
                    "plan",
                    "unsupported_plan_type",
                    "Plan type is not supported",
                )
            ]
        )

    # Stage 1: bound task, phase, and observation completeness.
    if task.phase.value != phase:
        errors.append(
            _plan_issue(
                "task.phase",
                "task_phase_mismatch",
                "Plan type does not match the active task phase",
                [task.phase.value],
            )
        )
    if observation.task_id != task.task_id:
        errors.append(
            _plan_issue(
                "observation.task_id",
                "observation_task_mismatch",
                "Observation belongs to another task",
            )
        )
    if observation.phase.value != phase:
        errors.append(
            _plan_issue(
                "observation.phase",
                "observation_phase_mismatch",
                "Observation does not match the submitted plan phase",
                [phase],
            )
        )
    required = PHASE_REQUIRED_OBSERVATIONS[phase]
    missing = [kind for kind in required if kind not in observation.completed_kinds]
    if missing:
        errors.append(
            _plan_issue(
                "observation.completed_kinds",
                "missing_required_observation",
                "Required phase observations are incomplete",
                missing,
            )
        )

    # Stage 2: decision evidence ownership and snapshot revision.
    errors.extend(
        _validate_evidence_refs(
            task=task,
            observation=observation,
            plan=plan,
            evidence=evidence,
        )
    )

    # Stage 3: references selected by the model must exist in observed facts.
    if isinstance(plan, ExtractSyncPlanInput):
        errors.extend(_validate_extract_references(observation, plan))
    else:
        errors.extend(_validate_finish_references(observation, plan))

    # Stage 4: selected actions, variants, arguments, and decision links.
    errors.extend(
        _validate_capability_contracts(
            phase=phase,
            plan=plan,
            capabilities=capabilities,
        )
    )

    # Stages 5 and 6: dependency graph followed by navigation business order.
    errors.extend(_validate_dependencies(plan))
    if isinstance(plan, FinishProcessingPlanInput):
        errors.extend(_validate_finish_business_order(plan))
    return _report(errors)
