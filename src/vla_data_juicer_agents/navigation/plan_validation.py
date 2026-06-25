from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from vla_data_juicer_agents.navigation.catalog import (
    ToolCapability,
    ToolVariantCapability,
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.models import NavigationDataProfile, WorkflowPlan, WorkflowStep


def _issue(issue_type: str, message: str, **details: Any) -> dict[str, Any]:
    issue = {"type": issue_type, "message": message}
    if details:
        issue["details"] = details
    return issue


def _catalog_by_tool(catalog: Iterable[ToolCapability]) -> dict[str, ToolCapability]:
    return {capability.tool_name: capability for capability in catalog}


def _step_positions(steps: list[WorkflowStep]) -> dict[str, int]:
    return {step.tool_name: index for index, step in enumerate(steps)}


def _find_variant(
    capability: ToolCapability,
    variant_id: str,
) -> ToolVariantCapability | None:
    for variant in capability.variants:
        if variant.id == variant_id:
            return variant
    return None


def _legacy_dataset_profile_fact(facts: dict[str, str]) -> str | None:
    processing_profile = facts.get("processing_profile")
    platform_hint = facts.get("platform_hint")
    topic_profile_hint = facts.get("topic_profile_hint")
    if processing_profile in {"go2w_like", "u_legacy_like"}:
        return processing_profile
    if platform_hint == "go2w" or topic_profile_hint == "go2w_like":
        return "go2w_like"
    if platform_hint == "u" or topic_profile_hint in {"u", "u_like", "u_legacy_like"}:
        return "u_legacy_like"
    return None


def _selector_facts(plan: WorkflowPlan, data_profile: NavigationDataProfile | None) -> dict[str, str]:
    facts = {
        "processing_profile": plan.processing_profile,
        "platform_hint": plan.platform_hint,
    }
    if data_profile is not None:
        facts.update(
            {
                "platform_hint": data_profile.platform_hint,
                "gridmap_source": data_profile.gridmap_source,
                "pcd_gridmap_tool_available": str(data_profile.pcd_gridmap_tool_available).lower(),
                "projection_input_ready": str(data_profile.projection_input_ready).lower(),
            }
        )
        if data_profile.localization_policy is not None:
            facts.update(
                {
                    "localization_source": data_profile.localization_policy.source,
                    "localization_conversion": data_profile.localization_policy.conversion,
                }
            )
        if data_profile.topic_params is not None:
            if data_profile.topic_params.profile_hint is not None:
                facts["topic_profile_hint"] = data_profile.topic_params.profile_hint
            if data_profile.topic_params.query_dir is not None:
                facts["query_dir"] = data_profile.topic_params.query_dir
        if data_profile.processing_profile is not None:
            processing_profile = data_profile.processing_profile
            if (
                "topic_profile_hint" not in facts
                and processing_profile.topic_params.profile_hint is not None
            ):
                facts["topic_profile_hint"] = processing_profile.topic_params.profile_hint
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
    legacy_dataset_profile = _legacy_dataset_profile_fact(facts)
    if legacy_dataset_profile is not None:
        facts["dataset_profile"] = legacy_dataset_profile
    return facts


def validate_workflow_plan(
    plan: WorkflowPlan,
    *,
    data_profile: NavigationDataProfile | None = None,
    catalog: Iterable[ToolCapability] | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not plan.processing_profile.strip():
        errors.append(
            _issue(
                "missing_processing_profile",
                "WorkflowPlan.processing_profile must be non-empty",
                processing_profile=plan.processing_profile,
            )
        )

    if data_profile is not None and data_profile.blocking_issues and plan.steps:
        errors.append(
            _issue(
                "blocking_profile_has_active_plan",
                "data profile has blocking issues but plan contains active steps",
                issues=[issue.type for issue in data_profile.blocking_issues],
            )
        )
    if (
        data_profile is not None
        and data_profile.gridmap_source == "unknown"
        and not data_profile.pcd_gridmap_tool_available
    ):
        errors.append(
            _issue(
                "missing_gridmap_source_or_generator",
                "grid_map is required but no existing source or PCD generator is available",
            )
        )

    catalog_by_tool = _catalog_by_tool(catalog or list_navigation_tool_capabilities())
    selector_facts = _selector_facts(plan, data_profile)
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
                            "variant selector does not match the data profile facts",
                            tool_name=step.tool_name,
                            variant=step.variant,
                            selector=selector_key,
                            actual=actual,
                            allowed=allowed_values,
                        )
                    )

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
