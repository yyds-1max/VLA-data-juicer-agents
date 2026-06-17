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


def _selector_facts(plan: WorkflowPlan, data_profile: NavigationDataProfile | None) -> dict[str, str]:
    facts = {"dataset_profile": plan.dataset_profile}
    if data_profile is not None:
        facts.update(
            {
                "dataset_profile": data_profile.dataset_profile,
                "gridmap_source": data_profile.gridmap_source,
                "pcd_gridmap_tool_available": str(data_profile.pcd_gridmap_tool_available).lower(),
                "projection_input_ready": str(data_profile.projection_input_ready).lower(),
            }
        )
    return facts


def validate_workflow_plan(
    plan: WorkflowPlan,
    *,
    data_profile: NavigationDataProfile | None = None,
    catalog: Iterable[ToolCapability] | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if plan.dataset_profile not in {"u_legacy_like", "go2w_like"}:
        errors.append(
            _issue(
                "unknown_dataset_profile",
                "WorkflowPlan.dataset_profile must be u_legacy_like or go2w_like",
                dataset_profile=plan.dataset_profile,
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
