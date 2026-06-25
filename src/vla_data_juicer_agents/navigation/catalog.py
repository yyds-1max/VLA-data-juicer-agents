from __future__ import annotations

from typing import Literal

from agentscope.tool import FunctionTool
from pydantic import BaseModel, Field


CapabilityStatus = Literal["available", "planned", "placeholder", "deprecated"]
ToolEffect = Literal["read", "write", "execute", "external"]


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


NAVIGATION_TOOL_CAPABILITIES: tuple[ToolCapability, ...] = (
    ToolCapability(
        tool_name="inspect_raw_date",
        stage_kind="inspect_raw_date",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="classify_navigation_dataset",
        stage_kind="classify_navigation_dataset",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=False,
    ),
    ToolCapability(
        tool_name="infer_navigation_sensor_bindings",
        stage_kind="infer_navigation_sensor_bindings",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="infer_navigation_processing_profile",
        stage_kind="infer_navigation_processing_profile",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="inspect_processing_state",
        stage_kind="inspect_processing_state",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="inspect_gridmap_artifacts",
        stage_kind="inspect_gridmap_artifacts",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="inspect_runtime_assets",
        stage_kind="inspect_runtime_assets",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        plan_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="prepare_raw_data",
        stage_kind="prepare_raw_data",
        effects="write",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="extract_and_sync_navigation_data",
        stage_kind="extract_and_sync_navigation_data",
        effects="execute",
        variants=[
            ToolVariantCapability(
                id="u_legacy_like",
                selectors={
                    "processing_profile": ["parameterized_navigation_v1", "u_legacy_like"],
                    "platform_hint": ["u"],
                },
            ),
            ToolVariantCapability(
                id="go2w_like",
                selectors={
                    "processing_profile": ["parameterized_navigation_v1", "go2w_like"],
                    "platform_hint": ["go2w"],
                },
            ),
        ],
        supports_dry_run=True,
        executor_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="confirm_navigation_calibration_params",
        stage_kind="confirm_navigation_calibration_params",
        effects="read",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
        human_blocking=True,
    ),
    ToolCapability(
        tool_name="assemble_finish_temp",
        stage_kind="assemble_finish_temp",
        effects="write",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="run_noobscene_preprocessing",
        stage_kind="run_noobscene_preprocessing",
        effects="execute",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="run_initial_annotation_gui",
        stage_kind="run_initial_annotation_gui",
        effects="external",
        variants=[ToolVariantCapability(id="human_gui")],
        supports_dry_run=True,
        executor_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="run_tracking",
        stage_kind="run_tracking",
        effects="execute",
        variants=[ToolVariantCapability(id="default")],
        supports_dry_run=True,
        executor_agent_allowed=True,
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
    ),
    ToolCapability(
        tool_name="run_projection_and_trajectory",
        stage_kind="run_projection_and_trajectory",
        effects="execute",
        variants=[
            ToolVariantCapability(
                id="cjl_with_gridmap",
                selectors={
                    "processing_profile": ["parameterized_navigation_v1", "u_legacy_like"],
                    "platform_hint": ["u"],
                },
            ),
            ToolVariantCapability(
                id="cjl_0525_with_gridmap",
                selectors={
                    "processing_profile": ["parameterized_navigation_v1", "go2w_like"],
                    "platform_hint": ["go2w"],
                },
            ),
        ],
        supports_dry_run=True,
        executor_agent_allowed=True,
    ),
    ToolCapability(
        tool_name="validate_navigation_outputs",
        stage_kind="validate_navigation_outputs",
        effects="read",
        variants=[ToolVariantCapability(id="expect_gridmap")],
        supports_dry_run=True,
        plan_agent_allowed=True,
        executor_agent_allowed=True,
    ),
)


def list_navigation_tool_capabilities() -> list[ToolCapability]:
    return [capability.model_copy(deep=True) for capability in NAVIGATION_TOOL_CAPABILITIES]


def navigation_tool_capabilities_payload() -> dict:
    return {
        "scenario": "navigation_vla",
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
