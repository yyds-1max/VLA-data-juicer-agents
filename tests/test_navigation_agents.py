import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.event import RequireUserConfirmEvent
from agentscope.message import ToolCallBlock

from vla_data_juicer_agents.navigation.models import NavigationDataProfile, NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import WorkflowPlanDraftState
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled, current_cancellation
from vla_data_juicer_agents.core.events import EventEmitter, JsonlEventSink
from vla_data_juicer_agents.navigation.agents import (
    DRAFT_PLAN_AGENT_INSTRUCTIONS,
    EXECUTOR_AGENT_INSTRUCTIONS,
    PLAN_AGENT_INSTRUCTIONS,
    create_executor_agent,
    create_plan_agent,
)
from vla_data_juicer_agents.navigation.inspection import infer_navigation_processing_profile_tool
from vla_data_juicer_agents.navigation.workflow import (
    build_deterministic_plan_template,
    run_executor_agent,
    run_plan_agent,
)


def _invoke_tool(tool, arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        return _decode_tool_payload(payload)

    return asyncio.run(_call())


def _decode_tool_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "content"):
        return _decode_tool_payload(payload.content)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        texts = [
            block.text
            for block in payload
            if hasattr(block, "text") and isinstance(block.text, str)
        ]
        if texts:
            return _decode_tool_payload("".join(texts))
    return payload


def _complete_go2w_profile_patch():
    return {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "sensor_bindings": {
                "fisheye_front": {
                    "role": "fisheye_front",
                    "topic": "/cam_video4/csi_cam/image_raw/compressed",
                    "message_type": "sensor_msgs/msg/CompressedImage",
                    "kind": "camera",
                },
                "lidar": {
                    "role": "lidar",
                    "topic": "/rs32_lidar_points",
                    "message_type": "sensor_msgs/msg/PointCloud2",
                    "kind": "lidar",
                },
                "localization": {
                    "role": "localization",
                    "topic": "/sport_odom",
                    "message_type": "nav_msgs/msg/Odometry",
                    "kind": "odom",
                },
                "evidence": ["infer_navigation_sensor_bindings_tool"],
            },
            "topic_params": _go2w_topic_params(),
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_policy": {"source": "existing_gridmap"},
            "calibration_policy": {
                "mode": "hardcoded_with_user_confirmation",
                "requires_user_confirmation": True,
            },
            "evidence": {
                "processing_profile": ["infer_navigation_processing_profile_tool"],
                "sensor_bindings": ["infer_navigation_sensor_bindings_tool"],
            },
        },
        "platform_hint": "go2w",
        "sensor_bindings": {
            "fisheye_front": {
                "role": "fisheye_front",
                "topic": "/cam_video4/csi_cam/image_raw/compressed",
                "message_type": "sensor_msgs/msg/CompressedImage",
                "kind": "camera",
            },
            "lidar": {
                "role": "lidar",
                "topic": "/rs32_lidar_points",
                "message_type": "sensor_msgs/msg/PointCloud2",
                "kind": "lidar",
            },
            "localization": {
                "role": "localization",
                "topic": "/sport_odom",
                "message_type": "nav_msgs/msg/Odometry",
                "kind": "odom",
            },
            "evidence": ["infer_navigation_sensor_bindings_tool"],
        },
        "topic_params": _go2w_topic_params(),
        "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
        "gridmap_source": "existing_gridmap",
        "pcd_gridmap_tool_available": True,
        "stage_variants": {
            "extract_and_sync_navigation_data": {
                "variant": "go2w_like",
                "reason": "processing profile inferred go2w platform bindings",
                "evidence": ["infer_navigation_processing_profile_tool"],
            },
            "prepare_gridmap_for_projection": {
                "variant": "copy_existing_gridmap",
                "reason": "grid_map artifacts already exist",
                "evidence": ["inspect_gridmap_artifacts_tool"],
            },
            "run_projection_and_trajectory": {
                "variant": "cjl_0525_with_gridmap",
                "reason": "go2w platform uses the 0525 projection script",
                "evidence": ["inspect_runtime_assets_tool"],
            },
        },
    }


def _complete_go2w_data_profile() -> NavigationDataProfile:
    return NavigationDataProfile.model_validate(
        {
            "date": "20270605",
            "scene_mode": "out",
            **_complete_go2w_profile_patch(),
        }
    )


def _complete_go2w_workflow_plan():
    return build_deterministic_plan_template(
        "20270605",
        "parameterized_navigation_v1",
        None,
        scene_mode="out",
        data_profile=_complete_go2w_data_profile(),
    )


def _parameterized_go2w_plan_without_profile_facts():
    plan = build_deterministic_plan_template(
        "20270605",
        "parameterized_navigation_v1",
        None,
        scene_mode="out",
    )
    plan.platform_hint = "go2w"
    for step in plan.steps:
        if "platform_hint" in step.arguments:
            step.arguments["platform_hint"] = "go2w"
        if step.tool_name == "extract_and_sync_navigation_data":
            step.variant = "go2w_like"
            step.arguments["platform_hint"] = "go2w"
        elif step.tool_name == "run_projection_and_trajectory":
            step.variant = "cjl_0525_with_gridmap"
            step.arguments["platform_hint"] = "go2w"
        elif step.tool_name == "prepare_gridmap_for_projection":
            step.variant = None
            step.decision_ref = None
            step.evidence = []
    return plan


def _go2w_topic_params():
    return {
        "profile_hint": "go2w_like",
        "confidence": 1.0,
        "topic_whitelist": [
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "rs32_lidar_points",
        "evidence": ["infer_navigation_topic_params_tool"],
        "warnings": [],
        "blocking_issues": [],
    }


def _message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.text
            for block in content
            if hasattr(block, "text") and isinstance(block.text, str)
        )
    return str(content)


def test_invoke_tool_helper_uses_agentscope_call_protocol():
    class FakeAgentScopeTool:
        name = "fake_tool"

        def __call__(self, value: str):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"value": value}))])

        def on_invoke_tool(self, *_args, **_kwargs):
            raise AssertionError("OpenAI on_invoke_tool must not be used")

    assert _invoke_tool(FakeAgentScopeTool(), {"value": "ok"}) == {"value": "ok"}


def test_create_plan_agent_has_read_only_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_plan_agent()
    tool_names = {tool.name for tool in agent.tools}

    assert "inspect_raw_date_tool" in tool_names
    assert "classify_navigation_dataset_tool" not in tool_names
    assert "infer_navigation_sensor_bindings_tool" in tool_names
    assert "infer_navigation_processing_profile_tool" in tool_names
    assert "inspect_processing_state_tool" in tool_names
    assert "inspect_gridmap_artifacts_tool" in tool_names
    assert "inspect_runtime_assets_tool" in tool_names
    assert "infer_navigation_topic_params_tool" in tool_names
    assert "list_navigation_tool_capabilities_tool" in tool_names


def test_create_plan_agent_with_request_has_draft_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    agent = create_plan_agent(request=request)
    tool_names = {tool.name for tool in agent.tools}

    assert "update_workflow_plan_draft_tool" in tool_names
    assert "get_workflow_plan_draft_tool" in tool_names
    assert "finalize_workflow_plan_tool" in tool_names


def test_create_plan_agent_without_request_does_not_prompt_for_missing_draft_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    agent = create_plan_agent()

    assert "finalize_workflow_plan_tool" not in {tool.name for tool in agent.tools}
    assert "finalize_workflow_plan_tool" not in agent.instructions


def test_create_executor_agent_has_execution_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_executor_agent(dry_run=True)
    tool_names = {tool.name for tool in agent.tools}

    assert "prepare_raw_data_tool" in tool_names
    assert "run_initial_annotation_gui_tool" in tool_names


def test_create_qwen_model_uses_agentscope_dashscope(monkeypatch):
    from agentscope.model import DashScopeChatModel
    from vla_data_juicer_agents.navigation.agents import create_qwen_model

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    model = create_qwen_model(model="qwen-plus")

    assert isinstance(model, DashScopeChatModel)
    assert model.model == "qwen-plus"


def test_create_executor_agent_dry_run_binds_execution_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    agent = create_executor_agent(dry_run=True)
    tool = {tool.name: tool for tool in agent.tools}["prepare_raw_data_tool"]

    result = _invoke_tool(tool, {"date": "20270605"})

    assert result["ok"] is True
    assert result["details"]["dry_run"] is True
    assert not (root / "raw_data" / "20270605_temp").exists()
    assert not (root / "clip_data" / "20270605").exists()


def test_executor_tools_check_and_bind_shared_cancellation(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    cancellation = CancellationContext()
    seen = []

    def fake_prepare(date, segments=None, settings=None, dry_run=False):
        seen.append(current_cancellation())
        return SimpleNamespace(model_dump=lambda mode="json": {"ok": True})

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.execution_tools.prepare_raw_data",
        fake_prepare,
    )
    agent = create_executor_agent(dry_run=True, cancellation=cancellation)
    tool = {tool.name: tool for tool in agent.tools}["prepare_raw_data_tool"]

    assert _invoke_tool(tool, {"date": "20270605"}) == {"ok": True}
    assert seen == [cancellation]
    cancellation.cancel()
    with pytest.raises(TurnCancelled):
        _invoke_tool(tool, {"date": "20270605"})
    assert seen == [cancellation]


def test_plan_agent_processing_profile_tool_accepts_empty_segments_string(tmp_path, monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(fixture_root))

    result = _invoke_tool(infer_navigation_processing_profile_tool, {"date": "20270605", "segments": ""})

    assert result["id"] == "parameterized_navigation_v1"
    assert result["platform_hint"] == "go2w"
    assert result["topic_params"]["profile_hint"] == "go2w"
    assert result["localization_policy"] == {"source": "odom", "conversion": "odom_to_ins"}


def test_plan_agent_processing_profile_tool_accepts_json_segments_string(tmp_path, monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(fixture_root))

    result = _invoke_tool(infer_navigation_processing_profile_tool, {"date": "20270605", "segments": '["20260605_152856"]'})

    assert result["id"] == "parameterized_navigation_v1"
    assert result["platform_hint"] == "go2w"
    assert result["topic_params"]["profile_hint"] == "go2w"


def test_plan_agent_draft_tools_finalize_internal_workflow_plan(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    agent = create_plan_agent(request=request)
    tools = {tool.name: tool for tool in agent.tools}
    complete_patch = _complete_go2w_profile_patch()

    sensor_update_result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": {"sensor_bindings": complete_patch["sensor_bindings"]},
            "observation_id": "navigation_sensor_bindings",
            "used_tool": "infer_navigation_sensor_bindings_tool",
        },
    )
    processing_profile = dict(complete_patch["processing_profile"])
    processing_profile.pop("sensor_bindings")
    processing_profile["gridmap_policy"] = {"source": "generated_from_pcd"}
    processing_profile["evidence"] = {
        "processing_profile": ["infer_navigation_processing_profile_tool"],
    }
    processing_stage_variants = {
        "extract_and_sync_navigation_data": {
            **complete_patch["stage_variants"]["extract_and_sync_navigation_data"],
            "evidence": ["infer_navigation_processing_profile_tool"],
        },
    }
    processing_update_result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": {
                "processing_profile": processing_profile,
                "platform_hint": complete_patch["platform_hint"],
                "topic_params": complete_patch["topic_params"],
                "localization_policy": complete_patch["localization_policy"],
                "stage_variants": processing_stage_variants,
            },
            "observation_id": "navigation_processing_profile",
            "used_tool": "infer_navigation_processing_profile_tool",
        },
    )
    gridmap_update_result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": {
                "gridmap_source": complete_patch["gridmap_source"],
                "pcd_gridmap_tool_available": complete_patch["pcd_gridmap_tool_available"],
                "stage_variants": {
                    "prepare_gridmap_for_projection": {
                        **complete_patch["stage_variants"]["prepare_gridmap_for_projection"],
                        "evidence": ["inspect_gridmap_artifacts_tool"],
                    },
                },
            },
            "observation_id": "gridmap_artifacts",
            "used_tool": "inspect_gridmap_artifacts_tool",
        },
    )
    runtime_update_result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": {
                "stage_variants": {
                    "run_projection_and_trajectory": {
                        **complete_patch["stage_variants"]["run_projection_and_trajectory"],
                        "evidence": ["inspect_runtime_assets_tool"],
                    },
                },
            },
            "observation_id": "runtime_assets_or_tool_capabilities",
            "used_tool": "inspect_runtime_assets_tool",
        },
    )
    finalize_result = _invoke_tool(tools["finalize_workflow_plan_tool"], {})

    assert sensor_update_result["ok"] is True
    assert processing_update_result["ok"] is True
    assert gridmap_update_result["ok"] is True
    assert runtime_update_result["ok"] is True
    assert processing_update_result["draft"]["data_profile_draft"]["processing_profile"]["gridmap_policy"] == {
        "source": "generated_from_pcd"
    }
    assert gridmap_update_result["draft"]["data_profile_draft"]["gridmap_source"] == "existing_gridmap"
    plan = finalize_result["workflow_plan_json"]
    assert plan["date"] == "20270605"
    assert plan["scene_mode"] == "out"
    assert plan["processing_profile"] == "parameterized_navigation_v1"
    assert plan["platform_hint"] == "go2w"
    draft_profile = runtime_update_result["draft"]["data_profile_draft"]
    assert draft_profile["sensor_bindings"]["localization"]["topic"] == "/sport_odom"
    assert draft_profile["localization_policy"] == {"source": "odom", "conversion": "odom_to_ins"}
    assert draft_profile["processing_profile"]["calibration_policy"] == {
        "mode": "hardcoded_with_user_confirmation",
        "requires_user_confirmation": True,
    }
    assert draft_profile["gridmap_source"] == "existing_gridmap"
    assert draft_profile["pcd_gridmap_tool_available"] is True
    assert draft_profile["stage_variants"]["extract_and_sync_navigation_data"]["evidence"] == [
        "infer_navigation_processing_profile_tool"
    ]
    assert draft_profile["stage_variants"]["prepare_gridmap_for_projection"]["evidence"] == [
        "inspect_gridmap_artifacts_tool"
    ]
    assert draft_profile["stage_variants"]["run_projection_and_trajectory"]["evidence"] == [
        "inspect_runtime_assets_tool"
    ]
    assert runtime_update_result["draft"]["completed_observations"] == [
        {
            "observation_id": "navigation_sensor_bindings",
            "used_tool": "infer_navigation_sensor_bindings_tool",
        },
        {
            "observation_id": "navigation_processing_profile",
            "used_tool": "infer_navigation_processing_profile_tool",
        },
        {
            "observation_id": "gridmap_artifacts",
            "used_tool": "inspect_gridmap_artifacts_tool",
        },
        {
            "observation_id": "runtime_assets_or_tool_capabilities",
            "used_tool": "inspect_runtime_assets_tool",
        },
    ]
    assert [step["tool_name"] for step in plan["steps"]] == [
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
        "confirm_navigation_calibration_params",
        "assemble_finish_temp",
        "run_noobscene_preprocessing",
        "run_initial_annotation_gui",
        "run_tracking",
        "prepare_gridmap_for_projection",
        "run_projection_and_trajectory",
        "validate_navigation_outputs",
    ]


def test_plan_agent_update_tool_accepts_json_string_data_profile_patch(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    request = NavigationRequest(date="20270515", dry_run=True, scene_mode="out")
    agent = create_plan_agent(request=request)
    tool = {tool.name: tool for tool in agent.tools}["update_workflow_plan_draft_tool"]

    result = _invoke_tool(
        tool,
        {
            "data_profile_patch": '{"processing_profile": {"id": "parameterized_navigation_v1", "platform_hint": "u"}}',
            "observation_id": "navigation_processing_profile",
            "used_tool": "infer_navigation_processing_profile_tool",
        },
    )

    assert result["ok"] is True
    assert result["draft"]["data_profile_draft"]["processing_profile"]["id"] == "parameterized_navigation_v1"
    assert result["draft"]["data_profile_draft"]["platform_hint"] == "u"
    assert "topic_params" in result["draft"]["missing_fields"]


def test_plan_agent_draft_missing_fields_include_scene_mode():
    state = WorkflowPlanDraftState(request=NavigationRequest(date="20270605", dry_run=True))

    update_result = state.update(processing_profile="parameterized_navigation_v1", platform_hint="go2w")
    snapshot = state.schema_snapshot()

    assert update_result["ok"] is True
    assert snapshot["scene_mode"] == "<in|out>"
    assert "scene_mode" in state.missing_fields()
    assert "scene_mode" in snapshot["missing_fields"]


def test_plan_agent_draft_finalize_requires_scene_mode(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    request = NavigationRequest(date="20270605", dry_run=True)
    agent = create_plan_agent(request=request)
    tools = {tool.name: tool for tool in agent.tools}

    update_result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {"processing_profile": "parameterized_navigation_v1", "platform_hint": "go2w"},
    )

    assert update_result["ok"] is True
    with pytest.raises(ValueError, match="NavigationDataProfile draft is incomplete.*scene_mode"):
        _invoke_tool(tools["finalize_workflow_plan_tool"], {})


def test_executor_agent_has_sdk_tool_for_each_plan_step(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_executor_agent(dry_run=True)
    tool_names = {tool.name for tool in agent.tools}
    plan = build_deterministic_plan_template(
        date="20270605",
        processing_profile="parameterized_navigation_v1",
        segments=None,
        scene_mode="out",
    )

    planned_tools = {"confirm_navigation_calibration_params_tool"}
    missing_tools = [
        f"{step.tool_name}_tool"
        for step in plan.steps
        if f"{step.tool_name}_tool" not in tool_names
        and f"{step.tool_name}_tool" not in planned_tools
    ]

    assert missing_tools == []
    assert "prepare_raw_data_tool" in agent.instructions


def test_plan_template_requires_scene_mode():
    with pytest.raises(ValueError, match="scene_mode is required"):
        build_deterministic_plan_template(
            date="20270605",
            processing_profile="parameterized_navigation_v1",
            segments=None,
        )

    with pytest.raises(ValueError, match="scene_mode is required"):
        build_deterministic_plan_template(
            date="20270605",
            processing_profile="parameterized_navigation_v1",
            segments=None,
            scene_mode=None,
        )


def test_plan_template_includes_human_gui_step():
    plan = build_deterministic_plan_template(
        date="20270605",
        processing_profile="parameterized_navigation_v1",
        segments=None,
        scene_mode="out",
    )

    gui_steps = [step for step in plan.steps if step.tool_name == "run_initial_annotation_gui"]
    assert len(gui_steps) == 1
    assert gui_steps[0].human_blocking is True


def test_plan_template_accepts_legacy_dataset_profile_keyword():
    plan = build_deterministic_plan_template(
        "20270605",
        None,
        None,
        scene_mode="out",
        dataset_profile="go2w_like",
    )
    step_ids = [step.step_id for step in plan.steps]

    assert plan.processing_profile == "go2w_like"
    assert plan.platform_hint == "go2w"
    assert "confirm_navigation_calibration_params" in step_ids


def test_plan_template_uses_finish_data_paths_for_gui_and_validation():
    plan = build_deterministic_plan_template(
        date="20270605",
        processing_profile="parameterized_navigation_v1",
        segments=None,
        scene_mode="out",
    )
    steps = {step.tool_name: step for step in plan.steps}

    assert plan.scene_mode == "out"
    assert plan.processing_profile == "parameterized_navigation_v1"
    assert plan.platform_hint == "unknown"
    assert steps["confirm_navigation_calibration_params"].arguments == {
        "date": "20270605",
        "segments": None,
        "platform_hint": "unknown",
    }
    assert steps["confirm_navigation_calibration_params"].preconditions == [
        "extract_and_sync_navigation_data"
    ]
    assert steps["assemble_finish_temp"].arguments == {
        "date": "20270605",
        "segments": None,
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "unknown",
    }
    assert steps["assemble_finish_temp"].preconditions == ["confirm_navigation_calibration_params"]
    assert steps["run_noobscene_preprocessing"].arguments == {
        "finish_temp_path": "finish_data/20270605_temp",
        "localization_source": "odom",
        "localization_conversion": "odom_to_ins",
    }
    assert steps["run_noobscene_preprocessing"].expected_outputs == ["finish_data/20270605_temp"]
    assert steps["run_initial_annotation_gui"].arguments == {"finish_temp_path": "finish_data/20270605_temp"}
    assert steps["run_initial_annotation_gui"].expected_outputs == ["finish_data/20270605_temp"]
    assert steps["run_tracking"].arguments == {"finish_temp_path": "finish_data/20270605_temp"}
    assert steps["run_tracking"].expected_outputs == ["finish_data/20270605"]
    assert steps["prepare_gridmap_for_projection"].arguments == {
        "date": "20270605",
        "segments": None,
        "finish_temp_path": "finish_data/20270605_temp",
    }
    assert steps["prepare_gridmap_for_projection"].expected_outputs == ["gridmap/20270605"]
    assert steps["run_projection_and_trajectory"].arguments == {
        "finish_temp_path": "finish_data/20270605_temp",
        "finish_path": "finish_data/20270605",
        "processing_profile": "parameterized_navigation_v1",
        "platform_hint": "unknown",
    }
    assert steps["run_projection_and_trajectory"].expected_outputs == ["finish_data/20270605"]
    assert steps["validate_navigation_outputs"].arguments == {"date": "20270605"}
    assert steps["validate_navigation_outputs"].expected_outputs == ["finish_data/20270605"]


def test_plan_template_uses_expected_step_order_without_legacy_gridmap_step():
    plan = build_deterministic_plan_template(
        date="20270605",
        processing_profile="parameterized_navigation_v1",
        segments=["20260605_152856"],
        scene_mode="in",
    )

    assert [step.tool_name for step in plan.steps] == [
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
        "confirm_navigation_calibration_params",
        "assemble_finish_temp",
        "run_noobscene_preprocessing",
        "run_initial_annotation_gui",
        "run_tracking",
        "prepare_gridmap_for_projection",
        "run_projection_and_trajectory",
        "validate_navigation_outputs",
    ]
    assert "generate_gridmap_from_pcd" not in [step.tool_name for step in plan.steps]
    assert plan.scene_mode == "in"


def test_validated_workflow_plan_rejects_missing_calibration_confirmation():
    from vla_data_juicer_agents.navigation.workflow import _validated_workflow_plan

    plan = _parameterized_go2w_plan_without_profile_facts()
    plan_without_confirmation = plan.model_copy(
        update={
            "steps": [
                step
                for step in plan.steps
                if step.step_id != "confirm_navigation_calibration_params"
            ]
        }
    )

    with pytest.raises(ValueError, match="confirm_navigation_calibration_params"):
        _validated_workflow_plan(plan_without_confirmation)


def test_validated_workflow_plan_rejects_non_blocking_calibration_confirmation():
    from vla_data_juicer_agents.navigation.workflow import _validated_workflow_plan

    plan = _parameterized_go2w_plan_without_profile_facts()
    plan_with_non_blocking_confirmation = plan.model_copy(deep=True)
    confirmation = next(
        step
        for step in plan_with_non_blocking_confirmation.steps
        if step.step_id == "confirm_navigation_calibration_params"
    )
    confirmation.human_blocking = False

    with pytest.raises(ValueError, match="confirm_navigation_calibration_params"):
        _validated_workflow_plan(plan_with_non_blocking_confirmation)


def test_validated_workflow_plan_accepts_calibration_confirmation_invariant():
    from vla_data_juicer_agents.navigation.workflow import _validated_workflow_plan

    plan = _parameterized_go2w_plan_without_profile_facts()

    assert _validated_workflow_plan(plan) == plan


def test_agent_instructions_require_scene_mode_and_new_projection_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    plan_agent = create_plan_agent()
    executor_agent = create_executor_agent(dry_run=True)

    for instructions in (plan_agent.instructions, executor_agent.instructions):
        assert "scene_mode" in instructions
        assert "run_tracking" in instructions
        assert "prepare_gridmap_for_projection" in instructions
        assert "run_projection_and_trajectory" in instructions
        assert "tracking" in instructions
        assert "projection" in instructions


def test_plan_agent_instructions_reference_guidance_and_lightweight_profile(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    plan_agent = create_plan_agent()

    assert "navigation-plan-agent-guidance" in plan_agent.instructions
    assert "lightweight NavigationDataProfile" in plan_agent.instructions
    assert "stage_variants" in plan_agent.instructions
    assert "list_navigation_tool_capabilities_tool" in plan_agent.instructions


def test_plan_agent_instructions_use_processing_profile_not_dataset_profile():
    instructions = PLAN_AGENT_INSTRUCTIONS

    assert "infer_navigation_processing_profile_tool" in instructions
    assert "sensor bindings" in instructions
    assert "processing_profile" in instructions
    assert "classify_navigation_dataset_tool" not in instructions
    assert "Supported profiles are u_legacy_like and go2w_like" not in instructions
    assert "The only human-blocking step is gen_box.py" not in instructions
    assert "Calibration confirmation" in instructions
    assert "gen_box.py" in instructions
    assert "before assemble_finish_temp" in instructions


def test_draft_plan_agent_instructions_use_processing_profile_flow():
    instructions = DRAFT_PLAN_AGENT_INSTRUCTIONS

    assert "processing_profile" in instructions
    assert "sensor_bindings" in instructions
    assert "localization_policy" in instructions
    assert "calibration_policy" in instructions
    assert "infer_navigation_processing_profile_tool" in instructions
    assert "dataset_profile" not in instructions
    assert "classification" not in instructions


def test_executor_agent_instructions_require_exact_calibration_confirmation():
    instructions = EXECUTOR_AGENT_INSTRUCTIONS

    assert "confirm_navigation_calibration_params" in instructions
    assert "user_confirmation is exactly `确认`" in instructions
    assert "`终止`" in instructions
    assert "calibration_params_not_confirmed" in instructions
    assert "localization_source and localization_conversion from WorkflowPlan" in instructions


def test_parse_workflow_plan_output_accepts_json_string():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "parameterized_navigation_v1", None, scene_mode="out").model_dump(mode="json")

    plan = _parse_workflow_plan_output(json.dumps(payload))

    assert plan.date == "20270605"
    assert plan.steps[0].tool_name == "prepare_raw_data"


def test_parse_workflow_plan_output_accepts_dict():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "parameterized_navigation_v1", None, scene_mode="out").model_dump(mode="json")

    plan = _parse_workflow_plan_output(payload)

    assert plan.processing_profile == "parameterized_navigation_v1"
    assert plan.platform_hint == "unknown"


def test_parse_workflow_plan_output_maps_legacy_dataset_profile():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "parameterized_navigation_v1", None, scene_mode="out").model_dump(mode="json")
    payload.pop("processing_profile")
    payload.pop("platform_hint")
    payload["dataset_profile"] = "go2w_like"

    plan = _parse_workflow_plan_output(payload)

    assert plan.processing_profile == "go2w_like"
    assert plan.platform_hint == "go2w"


def test_parse_workflow_plan_output_accepts_fenced_json():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "parameterized_navigation_v1", None, scene_mode="out").model_dump(mode="json")
    fenced = f"```json\n{json.dumps(payload)}\n```"

    plan = _parse_workflow_plan_output(fenced)

    assert plan.date == "20270605"


def test_parse_workflow_plan_output_rejects_invalid_output():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    with pytest.raises(ValueError, match="Unable to parse WorkflowPlan output"):
        _parse_workflow_plan_output("not json at all")


def test_create_qwen_model_requires_dashscope_key(monkeypatch):
    from vla_data_juicer_agents.navigation.agents import create_qwen_model

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY is required"):
        create_qwen_model()


def test_run_plan_agent_streams_events_to_run_state(tmp_path):
    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    plan = _parameterized_go2w_plan_without_profile_facts()
    plan_json = plan.model_dump_json()
    run_store = WorkflowRunStore(tmp_path / "runs")
    run_dir = run_store.create_run(request.date)
    scope = EventEmitter(JsonlEventSink(run_dir / "events.jsonl")).scope(
        "navigation.plan",
        run_id="plan-run",
        parent_run_id="workflow-run",
    )

    class FakeStreamAgent:
        async def reply_stream(self, _msg):
            yield SimpleNamespace(type="MODEL_CALL_START", model="qwen-plus")
            yield SimpleNamespace(type="TOOL_CALL_START", name="inspect_raw_date_tool")
            yield SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call_1")
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=plan_json)
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    parsed_plan = asyncio.run(
        run_plan_agent(
            FakeStreamAgent(),
            request,
            run_store=run_store,
            run_dir=run_dir,
            event_scope=scope,
        )
    )

    events_path = run_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert parsed_plan == plan
    assert [(event["type"], event["source"], event["run_id"], event["parent_run_id"]) for event in events] == [
        ("agent_start", "navigation.plan", "plan-run", "workflow-run"),
        ("agent_end", "navigation.plan", "plan-run", "workflow-run"),
    ]
    assert events[-1]["payload"] == {"status": "completed"}
    assert all("event_type" not in event for event in events)


def test_run_plan_agent_auto_confirms_tool_calls(tmp_path):
    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    plan = _parameterized_go2w_plan_without_profile_facts()
    run_store = WorkflowRunStore(tmp_path / "runs")
    run_dir = run_store.create_run(request.date)
    scope = EventEmitter(JsonlEventSink(run_dir / "events.jsonl")).scope("navigation.plan")

    class FakeConfirmingAgent:
        def __init__(self):
            self.inputs = []

        async def reply_stream(self, msg):
            self.inputs.append(msg)
            if len(self.inputs) == 1:
                yield RequireUserConfirmEvent(
                    reply_id="reply_1",
                    tool_calls=[
                        ToolCallBlock(
                            id="call_1",
                            name="inspect_raw_date_tool",
                            input='{"date": "20270605"}',
                        )
                    ],
                )
                return
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=plan.model_dump_json())
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    agent = FakeConfirmingAgent()

    parsed_plan = asyncio.run(
        run_plan_agent(
            agent,
            request,
            run_store=run_store,
            run_dir=run_dir,
            event_scope=scope,
        )
    )

    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert parsed_plan == plan
    assert len(agent.inputs) == 2
    assert agent.inputs[1].confirm_results[0].confirmed is True
    assert [(event["type"], event["payload"]) for event in events] == [
        ("agent_start", {}),
        ("agent_end", {"status": "completed"}),
    ]


def test_agent_stream_allows_ten_tool_confirmation_rounds():
    from vla_data_juicer_agents.navigation.workflow import _run_agent_stream

    class FakeTenToolAgent:
        def __init__(self):
            self.inputs = []

        async def reply_stream(self, msg):
            self.inputs.append(msg)
            if len(self.inputs) <= 10:
                yield RequireUserConfirmEvent(
                    reply_id=f"reply_{len(self.inputs)}",
                    tool_calls=[
                        ToolCallBlock(
                            id=f"call_{len(self.inputs)}",
                            name="get_workflow_plan_draft_tool",
                            input="{}",
                        )
                    ],
                )
                return
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="finished")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_final")

    agent = FakeTenToolAgent()

    output = asyncio.run(_run_agent_stream(agent, "prompt"))

    assert output == "finished"
    assert len(agent.inputs) == 11


def test_run_plan_agent_rejects_invalid_plan_variant(tmp_path):
    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    plan = _parameterized_go2w_plan_without_profile_facts()
    gridmap = next(step for step in plan.steps if step.tool_name == "prepare_gridmap_for_projection")
    gridmap.variant = "made_up_variant"
    run_store = WorkflowRunStore(tmp_path / "runs")
    run_dir = run_store.create_run(request.date)

    class FakeInvalidPlanAgent:
        async def reply_stream(self, _msg):
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=plan.model_dump_json())
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    with pytest.raises(ValueError, match="WorkflowPlan validation failed"):
        asyncio.run(run_plan_agent(FakeInvalidPlanAgent(), request, run_store=run_store, run_dir=run_dir))


def test_run_plan_agent_prompt_injects_current_profile_draft_state():
    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    plan = _parameterized_go2w_plan_without_profile_facts()

    class FakeDraftAwareAgent:
        def __init__(self):
            self.workflow_plan_draft_state = WorkflowPlanDraftState(request=request)
            self.prompts = []

        async def reply_stream(self, msg):
            self.prompts.append(_message_text(msg.content))
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=plan.model_dump_json())
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    agent = FakeDraftAwareAgent()

    asyncio.run(run_plan_agent(agent, request))

    prompt = agent.prompts[0]
    assert "NavigationDataProfile schema" in prompt
    assert "data_profile_draft" in prompt
    assert "missing_fields" in prompt
    assert "ready_to_finish" in prompt
    assert "data_profile_patch" in prompt


def test_navigation_prompts_require_concise_action_oriented_progress():
    for instructions in (PLAN_AGENT_INSTRUCTIONS, EXECUTOR_AGENT_INSTRUCTIONS):
        assert "Progress: <one or two concise, action-oriented sentences" in instructions
        assert "reasoning summary" in instructions
        assert "next action" in instructions
        assert "not the full hidden chain-of-thought" in instructions
        assert "Do not reveal draft notes" in instructions
        assert "do not print textual ReAct labels" in instructions
        assert "ToolName[arguments]" in instructions

    request = NavigationRequest(date="20270605", dry_run=True, scene_mode="out")
    plan = _parameterized_go2w_plan_without_profile_facts()

    class CapturingAgent:
        def __init__(self, output):
            self.output = output
            self.prompts = []

        async def reply_stream(self, msg):
            self.prompts.append(_message_text(msg.content))
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=self.output)

    plan_agent = CapturingAgent(plan.model_dump_json())
    executor_agent = CapturingAgent("done")
    asyncio.run(run_plan_agent(plan_agent, request, response_language="Chinese"))
    asyncio.run(run_executor_agent(executor_agent, plan, response_language="Chinese"))

    assert "Progress: <one or two concise, action-oriented sentences" in plan_agent.prompts[0]
    assert "Progress: <one or two concise, action-oriented sentences" in executor_agent.prompts[0]
    assert "must be written in Chinese" in plan_agent.prompts[0]
    assert "must be written in Chinese" in executor_agent.prompts[0]
    assert "write the summary text after it in Chinese" in plan_agent.prompts[0]
    assert "Action: choose" not in plan_agent.prompts[0]
    assert "Call one read-only tool with ToolName[arguments]" not in plan_agent.prompts[0]
    assert "Do not output textual Thought: or Action: lines" in plan_agent.prompts[0]
