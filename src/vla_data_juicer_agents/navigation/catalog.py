from __future__ import annotations

from typing import Literal

from agentscope.tool import FunctionTool
from pydantic import BaseModel, Field


CapabilityStatus = Literal["available", "planned", "placeholder", "deprecated"]
ToolEffect = Literal["read", "write", "execute", "external"]
CapabilityPhase = Literal[
    "extract_sync",
    "finish_processing",
    "trajectory_review",
]
CAPABILITY_CATALOG_REVISION = "navigation-capabilities-v5"


class ToolVariantCapability(BaseModel):
    id: str
    status: CapabilityStatus = "available"
    selectors: dict[str, list[str]] = Field(default_factory=dict)
    notes: str = ""


class ToolCapability(BaseModel):
    tool_name: str
    stage_kind: str
    effects: ToolEffect
    variants: list[ToolVariantCapability] = Field(default_factory=list)
    supports_dry_run: bool = False
    plan_agent_allowed: bool = False
    executor_agent_allowed: bool = False
    human_blocking: bool = False
    locks_navigation_target: bool = False
    phase: CapabilityPhase | None = None
    argument_model: str | None = None
    declared_output_kinds: list[str] = Field(default_factory=list)


NAVIGATION_TOOL_CAPABILITIES: tuple[ToolCapability, ...] = (
    ToolCapability(
        tool_name="inspect_raw_date",
        stage_kind="inspect_raw_date",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="inspect_navigation_raw_metadata",
        stage_kind="inspect_navigation_raw_metadata",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="extract_sync",
        declared_output_kinds=["raw_metadata"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_sensor_candidates",
        stage_kind="inspect_navigation_sensor_candidates",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="extract_sync",
        declared_output_kinds=["sensor_candidates"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_topic_candidates",
        stage_kind="inspect_navigation_topic_candidates",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="extract_sync",
        declared_output_kinds=["topic_candidates"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_artifact_state",
        stage_kind="inspect_navigation_artifact_state",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        declared_output_kinds=["artifact_state"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_gridmap_artifacts",
        stage_kind="inspect_navigation_gridmap_artifacts",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="finish_processing",
        declared_output_kinds=["gridmap_artifacts"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_runtime_assets",
        stage_kind="inspect_navigation_runtime_assets",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="finish_processing",
        declared_output_kinds=["runtime_assets"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_calibration_inventory",
        stage_kind="inspect_navigation_calibration_inventory",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="finish_processing",
        declared_output_kinds=["calibration_inventory"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_localization_sources",
        stage_kind="inspect_navigation_localization_sources",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
        phase="finish_processing",
        declared_output_kinds=["localization_sources"],
    ),
    ToolCapability(
        tool_name="inspect_navigation_annotation_job_facts",
        stage_kind="inspect_navigation_annotation_job_facts",
        effects="read",
        variants=[ToolVariantCapability(id="bounded_application_facts")],
        plan_agent_allowed=True,
        declared_output_kinds=["annotation_job_facts"],
    ),
    ToolCapability(
        tool_name="prepare_raw_data",
        stage_kind="prepare_raw_data",
        effects="write",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="extract_sync",
        argument_model="EmptyArguments",
        declared_output_kinds=["raw_temp", "clip_root"],
    ),
    ToolCapability(
        tool_name="extract_and_sync_navigation_data",
        stage_kind="extract_and_sync_navigation_data",
        effects="execute",
        variants=[
            ToolVariantCapability(
                id="explicit_topic_params",
                notes=(
                    "The model fills full ROS topics in topic_whitelist, extracted_dir-to-output_dir "
                    "routes in topic_map, and one relative extracted query_dir matching the selected "
                    "reference sensor. Use observed sensor bindings and topic-route evidence."
                ),
            ),
        ],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="extract_sync",
        argument_model="ExtractSyncArguments",
        declared_output_kinds=["sync_data"],
    ),
    ToolCapability(
        tool_name="confirm_navigation_calibration_params",
        stage_kind="confirm_navigation_calibration_params",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        human_blocking=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["calibration_confirmation"],
    ),
    ToolCapability(
        tool_name="assemble_finish_temp",
        stage_kind="assemble_finish_temp",
        effects="write",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["finish_temp"],
    ),
    ToolCapability(
        tool_name="run_noobscene_preprocessing",
        stage_kind="run_noobscene_preprocessing",
        effects="execute",
        variants=[
            ToolVariantCapability(
                id="default",
                notes=(
                    "Execution selects main_smart.py for native Ins or "
                    "main_smart_odom.py plus odom conversion/resize for odom."
                ),
            )
        ],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["noobscene_metadata"],
    ),
    ToolCapability(
        tool_name="run_initial_annotation_gui",
        stage_kind="run_initial_annotation_gui",
        effects="external",
        variants=[ToolVariantCapability(id="human_gui")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["annotation_yaml"],
    ),
    ToolCapability(
        tool_name="run_tracking",
        stage_kind="run_tracking",
        effects="execute",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["tracking_outputs"],
    ),
    ToolCapability(
        tool_name="run_annotation_tracking_workflow",
        stage_kind="run_annotation_tracking_workflow",
        effects="external",
        variants=[ToolVariantCapability(id="durable_web_handoff")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        human_blocking=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["tracked_annotation_job"],
    ),
    ToolCapability(
        tool_name="prepare_gridmap_for_projection",
        stage_kind="prepare_gridmap_for_projection",
        effects="execute",
        variants=[
            ToolVariantCapability(
                id="copy_existing_gridmap",
                selectors={"gridmap_source": ["existing_gridmap"]},
            ),
            ToolVariantCapability(
                id="generate_from_pcd",
                selectors={"gridmap_source": ["generated_from_pcd"]},
            ),
            ToolVariantCapability(
                id="skip_if_projection_ready",
                selectors={"gridmap_source": ["projection_ready"]},
            ),
        ],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["projection_gridmap"],
    ),
    ToolCapability(
        tool_name="run_projection_and_trajectory",
        stage_kind="run_projection_and_trajectory",
        effects="execute",
        variants=[
            ToolVariantCapability(
                id="cjl_with_gridmap",
                selectors={"localization_source": ["ins"]},
                notes="Native Ins pipeline: use Ins speed/direction and the standard CJL trajectory script.",
            ),
            ToolVariantCapability(
                id="cjl_0525_with_gridmap",
                selectors={"localization_source": ["odom"]},
                notes="Odom pipeline: use odom speed/direction and the CJL 0525 trajectory script.",
            ),
        ],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["finish_data", "trajectory"],
    ),
    ToolCapability(
        tool_name="run_annotation_postprocessing_workflow",
        stage_kind="run_annotation_postprocessing_workflow",
        effects="execute",
        variants=[ToolVariantCapability(id="plan_bound_runtime")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        locks_navigation_target=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["finish_data", "trajectory", "trajectory_review"],
    ),
    ToolCapability(
        tool_name="validate_navigation_outputs",
        stage_kind="validate_navigation_outputs",
        effects="read",
        variants=[ToolVariantCapability(id="expect_gridmap")],
        supports_dry_run=True,
        plan_agent_allowed=True,
        executor_agent_allowed=True,
        phase="finish_processing",
        argument_model="EmptyArguments",
        declared_output_kinds=["validated_navigation_outputs"],
    ),
    ToolCapability(
        tool_name="open_trajectory_fix_workbench",
        stage_kind="open_trajectory_fix_workbench",
        effects="external",
        variants=[ToolVariantCapability(id="durable_human_handoff")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        human_blocking=True,
        locks_navigation_target=True,
        phase="trajectory_review",
        argument_model="EmptyArguments",
        declared_output_kinds=["trajectory_review_handoff"],
    ),
    ToolCapability(
        tool_name="validate_trajectory_review_outcome",
        stage_kind="validate_trajectory_review_outcome",
        effects="read",
        variants=[ToolVariantCapability(id="approved_or_terminal")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        phase="trajectory_review",
        argument_model="EmptyArguments",
        declared_output_kinds=["validated_trajectory_review"],
    ),
)


def list_navigation_tool_capabilities() -> list[ToolCapability]:
    return [capability.model_copy(deep=True) for capability in NAVIGATION_TOOL_CAPABILITIES]


def navigation_tool_capabilities_payload() -> dict:
    return {
        "scenario": "navigation_vla",
        "revision": CAPABILITY_CATALOG_REVISION,
        "capabilities": [
            capability.model_dump(mode="json")
            for capability in list_navigation_tool_capabilities()
        ],
    }


def _list_navigation_tool_capabilities_tool() -> dict:
    return navigation_tool_capabilities_payload()


list_navigation_tool_capabilities_tool = FunctionTool(
    _list_navigation_tool_capabilities_tool,
    name="list_navigation_tool_capabilities_tool",
    is_read_only=True,
)
