from dataclasses import FrozenInstanceError

import pytest
from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.tool_groups import (
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_DIAGNOSTICS,
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_GROUP_NAMES,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_PLAN_AUTHORING,
    NavigationToolGroupDefinition,
    NavigationToolSurface,
    NavigationToolSurfacePolicy,
    classify_fixed_navigation_tools,
)


def _tool(name: str) -> FunctionTool:
    def implementation() -> dict[str, bool]:
        return {"ok": True}

    return FunctionTool(implementation, name=name, is_read_only=True)


def _all_groups() -> dict[str, NavigationToolGroupDefinition]:
    return {
        name: NavigationToolGroupDefinition(name=name, description=name, tools=())
        for name in NAVIGATION_GROUP_NAMES
    }


def test_group_definition_is_immutable_and_has_no_default_instructions():
    definition = NavigationToolGroupDefinition(
        name=NAVIGATION_EVIDENCE_READ,
        description="Read bounded task evidence.",
        tools=(),
    )

    assert definition.instructions is None
    with pytest.raises(FrozenInstanceError):
        definition.description = "changed"


def test_fixed_navigation_tools_are_classified_exactly_once():
    tools = [
        _tool("list_observation_evidence_tool"),
        _tool("read_observation_evidence_tool"),
        _tool("inspect_navigation_raw_metadata_tool"),
        _tool("inspect_navigation_sensor_candidates_tool"),
        _tool("inspect_navigation_topic_candidates_tool"),
        _tool("inspect_navigation_runtime_assets_tool"),
        _tool("inspect_navigation_calibration_inventory_tool"),
        _tool("inspect_navigation_localization_sources_tool"),
        _tool("inspect_navigation_artifact_state_tool"),
        _tool("inspect_navigation_gridmap_artifacts_tool"),
        _tool("get_navigation_task_context_tool"),
        _tool("describe_processing_action_tool"),
        _tool("record_navigation_user_guidance_tool"),
        _tool("submit_extract_sync_plan_tool"),
        _tool("submit_finish_processing_plan_tool"),
        _tool("get_plan_execution_overview_tool"),
        _tool("get_current_plan_step_tool"),
    ]

    grouped = classify_fixed_navigation_tools(tools)

    assert {name: {tool.name for tool in group} for name, group in grouped.items()} == {
        NAVIGATION_EVIDENCE_READ: {
            "list_observation_evidence_tool",
            "read_observation_evidence_tool",
        },
        NAVIGATION_INVESTIGATION: {
            "inspect_navigation_raw_metadata_tool",
            "inspect_navigation_sensor_candidates_tool",
            "inspect_navigation_topic_candidates_tool",
            "inspect_navigation_runtime_assets_tool",
            "inspect_navigation_calibration_inventory_tool",
            "inspect_navigation_localization_sources_tool",
        },
        NAVIGATION_ARTIFACT_CHECKS: {
            "inspect_navigation_artifact_state_tool",
            "inspect_navigation_gridmap_artifacts_tool",
        },
        NAVIGATION_PLAN_AUTHORING: {
            "get_navigation_task_context_tool",
            "describe_processing_action_tool",
            "record_navigation_user_guidance_tool",
            "submit_extract_sync_plan_tool",
            "submit_finish_processing_plan_tool",
        },
        NAVIGATION_EXECUTION_STATE: {
            "get_plan_execution_overview_tool",
            "get_current_plan_step_tool",
        },
    }


@pytest.mark.parametrize(
    "names, message",
    [
        (["unknown_navigation_tool"], "unclassified"),
        (
            ["read_observation_evidence_tool", "read_observation_evidence_tool"],
            "duplicate",
        ),
        (["unknown_navigation_tool", "unknown_navigation_tool"], "duplicate"),
    ],
)
def test_fixed_classification_rejects_unknown_or_duplicate_tools(names, message):
    with pytest.raises(ValueError, match=message):
        classify_fixed_navigation_tools([_tool(name) for name in names])


def test_policy_exposes_exact_groups_for_each_activity():
    all_groups = _all_groups()

    planning_surface = NavigationToolSurfacePolicy.resolve("planning", all_groups)
    assert planning_surface.active_group_names == (
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_INVESTIGATION,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_PLAN_AUTHORING,
        NAVIGATION_DIAGNOSTICS,
    )
    execution_surface = NavigationToolSurfacePolicy.resolve("execution", all_groups)
    assert execution_surface.active_group_names == (
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_EXECUTION_STATE,
        NAVIGATION_EXECUTION_ACTIONS,
        NAVIGATION_DIAGNOSTICS,
    )
    recovery_surface = NavigationToolSurfacePolicy.resolve(
        "recovery_required", all_groups
    )
    assert recovery_surface.active_group_names == (
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_EXECUTION_STATE,
        NAVIGATION_DIAGNOSTICS,
    )
    assert planning_surface.group(NAVIGATION_DIAGNOSTICS).tools == ()
    assert all(
        group.instructions is None
        for surface in (planning_surface, execution_surface, recovery_surface)
        for group in surface.groups
    )


def test_policy_rejects_a_missing_required_group_definition():
    all_groups = _all_groups()
    del all_groups[NAVIGATION_PLAN_AUTHORING]

    with pytest.raises(ValueError, match="missing"):
        NavigationToolSurfacePolicy.resolve("planning", all_groups)


def test_policy_hides_every_tool_while_the_current_step_is_running():
    surface = NavigationToolSurfacePolicy.resolve(
        "execution",
        _all_groups(),
        current_step_status="running",
    )

    assert surface.waiting_for_running_step is True
    assert surface.groups == ()
    assert surface.active_group_names == ()
    assert surface.flatten_active_tools() == []


def test_surface_flattens_tools_in_group_order():
    first = _tool("first_tool")
    second = _tool("second_tool")
    third = _tool("third_tool")
    surface = NavigationToolSurface(
        activity="planning",
        groups=(
            NavigationToolGroupDefinition("first", "first", (first, second)),
            NavigationToolGroupDefinition("second", "second", (third,)),
        ),
        active_group_names=("first", "second"),
    )

    assert surface.flatten_active_tools() == [first, second, third]


def test_surface_rejects_duplicate_tool_names_across_groups():
    surface = NavigationToolSurface(
        activity="execution",
        groups=(
            NavigationToolGroupDefinition(
                "first", "first", (_tool("duplicate_tool"),)
            ),
            NavigationToolGroupDefinition(
                "second", "second", (_tool("duplicate_tool"),)
            ),
        ),
        active_group_names=("first", "second"),
    )

    with pytest.raises(ValueError, match="duplicate"):
        surface.flatten_active_tools()
