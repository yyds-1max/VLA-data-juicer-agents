from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ValidationError

from vla_data_juicer_agents.navigation.catalog import ToolCapability
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
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
from vla_data_juicer_agents.navigation.planning_context import PLAN_REQUIRED_OBSERVATIONS
from vla_data_juicer_agents.navigation.profiles import topic_route
from vla_data_juicer_agents.navigation.task_state import NavigationTask

MAX_PUBLIC_PLAN_VALIDATION_ISSUES = 8
MAX_PUBLIC_ALLOWED_VALUES = 20
_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "EmptyArguments": EmptyArguments,
    "ExtractSyncArguments": ExtractSyncArguments,
}
_GRIDMAP_VARIANT_BY_SOURCE = {
    "existing_gridmap": "copy_existing_gridmap",
    "generated_from_pcd": "generate_from_pcd",
    "projection_ready": "skip_if_projection_ready",
}
_REQUIRED_PAYLOAD_TYPES: dict[str, type[BaseModel]] = {
    "artifact_state": ArtifactStateObservation,
    "raw_metadata": RawMetadataObservation,
    "sensor_candidates": SensorCandidatesObservation,
    "topic_candidates": TopicCandidatesObservation,
    "gridmap_artifacts": GridmapArtifactsObservation,
    "runtime_assets": RuntimeAssetsObservation,
    "calibration_inventory": CalibrationInventoryObservation,
    "localization_sources": LocalizationSourcesObservation,
}


def _plan_issue(
    path: str,
    code: str,
    message: str,
    allowed_values: Sequence[str] = (),
) -> PlanValidationIssue:
    normalized_allowed_values = sorted({str(value) for value in allowed_values})[
        :MAX_PUBLIC_ALLOWED_VALUES
    ]
    return PlanValidationIssue(
        path=path,
        code=code,
        message=message,
        allowed_values=normalized_allowed_values,
    )


def _bounded_allowed_values(
    values: Iterable[str],
    *,
    overflow_values: Iterable[str] = (),
) -> list[str]:
    normalized = sorted({str(value) for value in values})
    if len(normalized) <= MAX_PUBLIC_ALLOWED_VALUES:
        return normalized
    overflow = sorted({str(value) for value in overflow_values})
    return overflow[:MAX_PUBLIC_ALLOWED_VALUES]


def _observation_inventory_pointer(
    *,
    observation: NavigationObservationRevision,
    evidence: Sequence[EvidenceDescriptor],
    kind: str,
) -> list[str]:
    matching = [
        descriptor
        for descriptor in evidence
        if descriptor.task_id == observation.task_id
        and descriptor.observation_revision <= observation.revision
        and descriptor.kind == kind
    ]
    if matching:
        current = max(
            matching,
            key=lambda descriptor: (
                descriptor.observation_revision,
                descriptor.created_at,
                descriptor.ref,
            ),
        )
        return [f"evidence_ref:{current.ref}"]
    return [f"observation.payloads[kind={kind}]"]


def _topic_inventory_kinds(
    *,
    raw_topics: set[str],
    candidate_topics: set[str],
) -> list[str]:
    if raw_topics and raw_topics <= candidate_topics:
        return ["topic_candidates"]
    if candidate_topics and candidate_topics <= raw_topics:
        return ["raw_metadata"]
    return [
        kind
        for kind, values in (
            ("raw_metadata", raw_topics),
            ("topic_candidates", candidate_topics),
        )
        if values
    ]


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
    evidence: Sequence[EvidenceDescriptor],
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    raw = _payload_of_type(observation, RawMetadataObservation)
    topics = _payload_of_type(observation, TopicCandidatesObservation)
    sensors = _payload_of_type(observation, SensorCandidatesObservation)
    raw_topics = (
        {measurement.topic for measurement in raw.topics}
        if raw is not None
        else set()
    )
    candidate_topics = set(topics.available_topics) if topics is not None else set()
    available_topics = raw_topics | candidate_topics
    inventory_kinds = _topic_inventory_kinds(
        raw_topics=raw_topics,
        candidate_topics=candidate_topics,
    )
    allowed_topics = _bounded_allowed_values(
        available_topics,
        overflow_values=(
            pointer
            for kind in inventory_kinds
            for pointer in _observation_inventory_pointer(
                observation=observation,
                evidence=evidence,
                kind=kind,
            )
        ),
    )

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
                    _bounded_allowed_values(
                        observed_bindings.get(role, set()),
                        overflow_values=_observation_inventory_pointer(
                            observation=observation,
                            evidence=evidence,
                            kind="sensor_candidates",
                        ),
                    ),
                )
            )

    selection = plan.decisions.topic_selection
    selected_topics = set(selection.topic_whitelist)
    has_unobserved_selected_topic = False
    for index, topic in enumerate(selection.topic_whitelist):
        if topic not in available_topics:
            has_unobserved_selected_topic = True
            errors.append(
                _plan_issue(
                    f"plan.decisions.topic_selection.topic_whitelist.{index}",
                    "unobserved_topic",
                    "Selected topic was not observed",
                    allowed_topics,
                )
            )

    binding_roles = sorted(plan.decisions.sensor_bindings.bindings)
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
    if has_unobserved_selected_topic:
        return errors

    raw_message_types = (
        {measurement.topic: measurement.message_type for measurement in raw.topics}
        if raw is not None
        else {}
    )
    observed_routes = {
        (route.topic, route.role): (route.extracted_dir, route.output_dir)
        for route in topics.routes
    } if topics is not None else {}

    def selected_route(role: str, topic: str) -> tuple[str, str]:
        observed = observed_routes.get((topic, role))
        if observed is not None:
            return observed
        return topic_route(
            topic,
            role,
            message_type=raw_message_types.get(topic),
        )

    expected_map: dict[str, str] = {}
    bound_topics = set(plan.decisions.sensor_bindings.bindings.values())
    for role, topic in plan.decisions.sensor_bindings.bindings.items():
        if topic not in selected_topics:
            errors.append(
                _plan_issue(
                    f"plan.decisions.sensor_bindings.bindings.{role}",
                    "binding_topic_not_selected",
                    "Bound sensor topic is missing from topic_whitelist",
                    selection.topic_whitelist,
                )
            )
            continue
        extracted_dir, output_dir = selected_route(role, topic)
        previous = expected_map.get(extracted_dir)
        if previous is not None and previous != output_dir:
            errors.append(
                _plan_issue(
                    f"plan.decisions.sensor_bindings.bindings.{role}",
                    "conflicting_topic_route",
                    "Selected bindings require conflicting outputs for one extracted directory",
                    [previous, output_dir],
                )
            )
        expected_map[extracted_dir] = output_dir

    for index, topic in enumerate(selection.topic_whitelist):
        if topic in available_topics and topic not in bound_topics:
            errors.append(
                _plan_issue(
                    f"plan.decisions.topic_selection.topic_whitelist.{index}",
                    "unbound_selected_topic",
                    "Every extracted topic must be justified by a selected sensor binding",
                    sorted(bound_topics),
                )
            )

    allowed_routes = [f"{source}->{target}" for source, target in expected_map.items()]
    for extracted_dir, output_dir in selection.topic_map.items():
        path = f"plan.decisions.topic_selection.topic_map.{extracted_dir}"
        expected_output = expected_map.get(extracted_dir)
        if expected_output is None:
            errors.append(
                _plan_issue(
                    path,
                    "unknown_extracted_topic_dir",
                    "topic_map key is not an extracted directory for a selected binding",
                    allowed_routes,
                )
            )
        elif output_dir != expected_output:
            errors.append(
                _plan_issue(
                    path,
                    "invalid_sync_output_dir",
                    "topic_map value is not the canonical output directory for this topic",
                    [expected_output],
                )
            )
    for extracted_dir, output_dir in expected_map.items():
        if extracted_dir not in selection.topic_map:
            errors.append(
                _plan_issue(
                    f"plan.decisions.topic_selection.topic_map.{extracted_dir}",
                    "missing_topic_route",
                    "Selected binding is missing its extracted-to-output directory mapping",
                    [output_dir],
                )
            )

    if reference in plan.decisions.sensor_bindings.bindings:
        reference_topic = plan.decisions.sensor_bindings.bindings[reference]
        expected_query_dir, _ = selected_route(reference, reference_topic)
        query_path = PurePosixPath(selection.query_dir)
        if (
            query_path.is_absolute()
            or selection.query_dir in {".", ".."}
            or len(query_path.parts) != 1
        ):
            errors.append(
                _plan_issue(
                    "plan.decisions.topic_selection.query_dir",
                    "invalid_query_dir",
                    "query_dir must be one relative tmp_dir child name, not a path or ROS topic",
                    [expected_query_dir],
                )
            )
        elif selection.query_dir != expected_query_dir:
            errors.append(
                _plan_issue(
                    "plan.decisions.topic_selection.query_dir",
                    "query_dir_reference_mismatch",
                    "query_dir does not match the extracted directory of reference_sensor",
                    [expected_query_dir],
                )
            )
    return errors


def _validate_finish_references(
    observation: NavigationObservationRevision,
    plan: FinishProcessingPlanInput,
    evidence: Sequence[EvidenceDescriptor],
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

    if runtime is not None:
        localization_source = localization_decision.source
        if not runtime.noobscene_localization_variants.get(localization_source, False):
            errors.append(
                _plan_issue(
                    "plan.decisions.localization.source",
                    "noobscene_localization_variant_unavailable",
                    "The selected localization-specific NoobScenes script is unavailable",
                    [
                        source
                        for source, available in runtime.noobscene_localization_variants.items()
                        if available
                    ],
                )
            )
        if not runtime.speed_direction_variants.get(localization_source, False):
            errors.append(
                _plan_issue(
                    "plan.decisions.localization.source",
                    "speed_direction_variant_unavailable",
                    "The selected localization-specific speed/direction script is unavailable",
                    [
                        source
                        for source, available in runtime.speed_direction_variants.items()
                        if available
                    ],
                )
            )

    gridmap_decision = plan.decisions.gridmap
    source_observed = {
        "existing_gridmap": bool(gridmap and gridmap.existing_gridmap_paths),
        "generated_from_pcd": bool(gridmap and gridmap.pcd_sources),
        "projection_ready": bool(gridmap and gridmap.projection_ready),
    }
    gridmap_source = str(gridmap_decision.source)
    if gridmap_source not in source_observed:
        errors.append(
            _plan_issue(
                "plan.decisions.gridmap.source",
                "invalid_gridmap_source",
                "Gridmap source is not supported",
                source_observed,
            )
        )
    elif not source_observed[gridmap_source]:
        errors.append(
            _plan_issue(
                "plan.decisions.gridmap.source",
                "unobserved_gridmap_source",
                "Gridmap source is not supported by observed artifacts",
                [source for source, observed in source_observed.items() if observed],
            )
        )
    if (
        gridmap_source == "generated_from_pcd"
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
                _bounded_allowed_values(
                    calibration.sensor_sources,
                    overflow_values=_observation_inventory_pointer(
                        observation=observation,
                        evidence=evidence,
                        kind="calibration_inventory",
                    ),
                ),
            )
        )
    if not calibration_decision.requires_user_confirmation:
        errors.append(
            _plan_issue(
                "plan.decisions.calibration.requires_user_confirmation",
                "calibration_confirmation_required",
                "Selected calibration always requires explicit user confirmation",
                ["true"],
            )
        )

    for index, step in enumerate(plan.steps):
        if (
            step.action == "run_initial_annotation_gui"
            and runtime is not None
            and not runtime.manual_annotation_gui_available
        ):
            errors.append(
                _plan_issue(
                    f"plan.steps.{index}.action",
                    "runtime_action_unavailable",
                    "Manual annotation GUI is unavailable in observed runtime assets",
                    _observation_inventory_pointer(
                        observation=observation,
                        evidence=evidence,
                        kind="runtime_assets",
                    ),
                )
            )
        if step.action == "prepare_gridmap_for_projection":
            expected_variant = _GRIDMAP_VARIANT_BY_SOURCE.get(gridmap_source)
            if expected_variant is not None and step.variant != expected_variant:
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
                    _bounded_allowed_values(
                        (
                            name
                            for name, available in runtime.projection_variants.items()
                            if available
                        ),
                        overflow_values=_observation_inventory_pointer(
                            observation=observation,
                            evidence=evidence,
                            kind="runtime_assets",
                        ),
                    ),
                )
            )
        if step.action == "run_projection_and_trajectory":
            expected_projection_variant = {
                "ins": "cjl_with_gridmap",
                "odom": "cjl_0525_with_gridmap",
            }[localization_decision.source]
            if step.variant != expected_projection_variant:
                errors.append(
                    _plan_issue(
                        f"plan.steps.{index}.variant",
                        "projection_localization_mismatch",
                        "Projection variant does not match the selected localization pipeline",
                        [expected_projection_variant],
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
                    _bounded_allowed_values(
                        allowed_actions,
                        overflow_values=(
                            f"action_contract:{action}"
                            for action in allowed_actions
                        ),
                    ),
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
                    _bounded_allowed_values(
                        allowed_variants,
                        overflow_values=[f"action_contract:{step.action}"],
                    ),
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
                    [f"action_contract:{step.action}"],
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
                        [f"action_contract:{step.action}"],
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
                        _bounded_allowed_values(
                            positions,
                            overflow_values=["plan.steps[*].step_id"],
                        ),
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
    observation: NavigationObservationRevision,
) -> list[PlanValidationIssue]:
    errors: list[PlanValidationIssue] = []
    positions: dict[str, int] = {}
    for index, step in enumerate(plan.steps):
        positions.setdefault(step.action, index)

    artifact = _payload_of_type(observation, ArtifactStateObservation)
    final_outputs_complete = bool(
        artifact
        and artifact.snapshot.final_outputs_exist
        and artifact.snapshot.final_grid_map_exists
    )
    full_pipeline = [
        "confirm_navigation_calibration_params",
        "assemble_finish_temp",
        "run_noobscene_preprocessing",
        "run_initial_annotation_gui",
        "run_tracking",
        "prepare_gridmap_for_projection",
        "run_projection_and_trajectory",
        "validate_navigation_outputs",
    ]
    required_actions = ["validate_navigation_outputs"] if final_outputs_complete else full_pipeline
    missing_actions = [action for action in required_actions if action not in positions]
    if missing_actions:
        errors.append(
            _plan_issue(
                "plan.steps",
                "incomplete_finish_pipeline",
                (
                    "Finish Plan is missing required actions for the observed artifact state"
                ),
                missing_actions,
            )
        )

    present_pipeline = [action for action in full_pipeline if action in positions]
    if any(
        positions[earlier] >= positions[later]
        for earlier, later in zip(present_pipeline, present_pipeline[1:])
    ):
        errors.append(
            _plan_issue(
                "plan.steps",
                "invalid_finish_pipeline_order",
                "Finish processing actions must follow the canonical business order",
                full_pipeline,
            )
        )

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

    # Stage 1: bound task and submitted-plan-specific observation completeness.
    if observation.task_id != task.task_id:
        errors.append(
            _plan_issue(
                "observation.task_id",
                "observation_task_mismatch",
                "Observation belongs to another task",
            )
        )
    required = PLAN_REQUIRED_OBSERVATIONS[phase]
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
    if phase == "finish_processing" and task.scene_mode not in {"in", "out"}:
        errors.append(
            _plan_issue(
                "task.scene_mode",
                "missing_scene_mode",
                "Indoor/outdoor must be asked and recorded before finish planning",
                ["in", "out"],
            )
        )
    for kind in required:
        payload_type = _REQUIRED_PAYLOAD_TYPES[kind]
        if not any(isinstance(payload, payload_type) for payload in observation.payloads):
            errors.append(
                _plan_issue(
                    f"observation.payloads.{kind}",
                    "missing_required_observation_payload",
                    "Required typed observation payload is missing",
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
        errors.extend(_validate_extract_references(observation, plan, evidence))
    else:
        errors.extend(_validate_finish_references(observation, plan, evidence))

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
        errors.extend(_validate_finish_business_order(plan, observation))
    return _report(errors)
