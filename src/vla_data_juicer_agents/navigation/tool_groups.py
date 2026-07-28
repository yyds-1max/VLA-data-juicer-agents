from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from agentscope.tool import ToolBase


NavigationActivity = Literal["planning", "execution", "recovery_required"]

NAVIGATION_EVIDENCE_READ = "navigation_evidence_read"
NAVIGATION_INVESTIGATION = "navigation_investigation"
NAVIGATION_ARTIFACT_CHECKS = "navigation_artifact_checks"
NAVIGATION_PLAN_AUTHORING = "navigation_plan_authoring"
NAVIGATION_EXECUTION_STATE = "navigation_execution_state"
NAVIGATION_EXECUTION_ACTIONS = "navigation_execution_actions"
NAVIGATION_DIAGNOSTICS = "navigation_diagnostics"
NAVIGATION_GROUP_NAMES = (
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_PLAN_AUTHORING,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_DIAGNOSTICS,
)


@dataclass(frozen=True)
class NavigationToolGroupDefinition:
    name: str
    description: str
    tools: tuple[ToolBase, ...]
    instructions: str | None = None


@dataclass(frozen=True)
class NavigationToolSurface:
    activity: NavigationActivity
    groups: tuple[NavigationToolGroupDefinition, ...]
    active_group_names: tuple[str, ...]
    waiting_for_running_step: bool = False

    def group(self, name: str) -> NavigationToolGroupDefinition:
        for group in self.groups:
            if group.name == name:
                return group
        raise LookupError(name)

    def flatten_active_tools(self) -> list[ToolBase]:
        tools = [tool for group in self.groups for tool in group.tools]
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate navigation tool names across active groups")
        return tools


_FIXED_TOOL_GROUP_BY_NAME = {
    "list_observation_evidence_tool": NAVIGATION_EVIDENCE_READ,
    "read_observation_evidence_tool": NAVIGATION_EVIDENCE_READ,
    "inspect_navigation_raw_metadata_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_sensor_candidates_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_topic_candidates_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_runtime_assets_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_calibration_inventory_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_localization_sources_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_annotation_job_facts_tool": NAVIGATION_INVESTIGATION,
    "inspect_navigation_artifact_state_tool": NAVIGATION_ARTIFACT_CHECKS,
    "inspect_navigation_gridmap_artifacts_tool": NAVIGATION_ARTIFACT_CHECKS,
    "get_navigation_task_context_tool": NAVIGATION_PLAN_AUTHORING,
    "describe_processing_action_tool": NAVIGATION_PLAN_AUTHORING,
    "record_navigation_user_guidance_tool": NAVIGATION_PLAN_AUTHORING,
    "complete_navigation_task_tool": NAVIGATION_PLAN_AUTHORING,
    "submit_extract_sync_plan_tool": NAVIGATION_PLAN_AUTHORING,
    "submit_finish_processing_plan_tool": NAVIGATION_PLAN_AUTHORING,
    "submit_trajectory_review_plan_tool": NAVIGATION_PLAN_AUTHORING,
    "get_plan_execution_overview_tool": NAVIGATION_EXECUTION_STATE,
    "get_current_plan_step_tool": NAVIGATION_EXECUTION_STATE,
}

_FIXED_GROUP_NAMES = (
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_PLAN_AUTHORING,
    NAVIGATION_EXECUTION_STATE,
)


def classify_fixed_navigation_tools(
    tools: Sequence[ToolBase],
) -> dict[str, tuple[ToolBase, ...]]:
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("duplicate fixed navigation tool names")

    grouped: dict[str, list[ToolBase]] = {name: [] for name in _FIXED_GROUP_NAMES}

    for tool in tools:
        group_name = _FIXED_TOOL_GROUP_BY_NAME.get(tool.name)
        if group_name is None:
            raise ValueError(f"unclassified fixed navigation tool: {tool.name}")
        grouped[group_name].append(tool)

    return {name: tuple(grouped[name]) for name in _FIXED_GROUP_NAMES}


class NavigationToolSurfacePolicy:
    _GROUP_NAMES_BY_ACTIVITY: Mapping[NavigationActivity, tuple[str, ...]] = {
        "planning": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_INVESTIGATION,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_PLAN_AUTHORING,
            NAVIGATION_DIAGNOSTICS,
        ),
        "execution": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_EXECUTION_STATE,
            NAVIGATION_EXECUTION_ACTIONS,
            NAVIGATION_DIAGNOSTICS,
        ),
        "recovery_required": (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_EXECUTION_STATE,
            NAVIGATION_DIAGNOSTICS,
        ),
    }

    @classmethod
    def resolve(
        cls,
        activity: NavigationActivity,
        groups_by_name: Mapping[str, NavigationToolGroupDefinition],
        *,
        current_step_status: str | None = None,
    ) -> NavigationToolSurface:
        if activity == "execution" and current_step_status == "running":
            return NavigationToolSurface(
                activity=activity,
                groups=(),
                active_group_names=(),
                waiting_for_running_step=True,
            )
        try:
            group_names = cls._GROUP_NAMES_BY_ACTIVITY[activity]
        except KeyError as exc:
            raise ValueError(f"unsupported navigation activity: {activity}") from exc

        missing = [name for name in group_names if name not in groups_by_name]
        if missing:
            raise ValueError(
                "missing required navigation tool group definitions: "
                + ", ".join(missing)
            )

        groups = tuple(groups_by_name[name] for name in group_names)
        return NavigationToolSurface(
            activity=activity,
            groups=groups,
            active_group_names=tuple(group.name for group in groups),
        )
