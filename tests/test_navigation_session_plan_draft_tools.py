import asyncio
import inspect
import json

import pytest

from vla_data_juicer_agents.navigation.plan_draft_store import InMemoryNavigationPlanDraftStore
from vla_data_juicer_agents.navigation import session_plan_draft_tools as session_tools_module
from vla_data_juicer_agents.navigation.session_plan_draft_tools import (
    build_session_plan_draft_tools,
)


def _invoke_tool(tool, arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        if isinstance(payload, str):
            return json.loads(payload)
        if isinstance(payload, dict):
            return payload
        if hasattr(payload, "content"):
            return json.loads("".join(block.text for block in payload.content))
        return payload

    return asyncio.run(_call())


def _tools(store, session_id="agent-session-1"):
    return {
        tool.name: tool
        for tool in build_session_plan_draft_tools(
            store=store,
            session_id=session_id,
        )
    }


def _complete_profile_patch():
    topic_params = {
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
    localization_policy = {"source": "odom", "conversion": "odom_to_ins"}
    gridmap_policy = {"source": "existing_gridmap"}
    calibration_policy = {
        "mode": "hardcoded_with_user_confirmation",
        "requires_user_confirmation": True,
    }
    stage_variants = {
        "extract_and_sync_navigation_data": {
            "variant": "explicit_topic_params",
            "reason": "topic parameters were inferred from sensor role bindings",
            "evidence": ["infer_navigation_processing_profile_tool"],
        },
        "prepare_gridmap_for_projection": {
            "variant": "copy_existing_gridmap",
            "reason": "grid_map artifacts already exist",
            "evidence": ["inspect_gridmap_artifacts_tool"],
        },
        "run_projection_and_trajectory": {
            "variant": "cjl_0525_with_gridmap",
            "reason": "runtime assets support the 0525 projection script",
            "evidence": ["inspect_runtime_assets_tool"],
        },
    }
    return {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "topic_params": topic_params,
            "localization_policy": localization_policy,
            "gridmap_policy": gridmap_policy,
            "calibration_policy": calibration_policy,
            "warnings": [],
            "blocking_issues": [],
            "evidence": {
                "processing_profile": ["infer_navigation_processing_profile_tool"],
                "topic_params": ["infer_navigation_topic_params_tool"],
            },
        },
        "platform_hint": "go2w",
        "topic_params": topic_params,
        "localization_policy": localization_policy,
        "gridmap_source": "existing_gridmap",
        "pcd_gridmap_tool_available": True,
        "stage_variants": stage_variants,
        "warnings": [],
        "blocking_issues": [],
        "evidence": {
            "processing_profile": ["infer_navigation_processing_profile_tool"],
            "topic_params": ["infer_navigation_topic_params_tool"],
            "gridmap_artifacts": ["inspect_gridmap_artifacts_tool"],
            "runtime_assets_or_tool_capabilities": ["inspect_runtime_assets_tool"],
        },
    }


@pytest.fixture
def complete_extract_sync_draft_payload():
    return {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "topic_params": {
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
            },
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
        },
        "topic_params": {
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
        },
        "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
        "stage_variants": {
            "extract_and_sync_navigation_data": {
                "variant": "explicit_topic_params",
                "reason": "topic params inferred from metadata",
                "evidence": ["metadata.yaml"],
            }
        },
    }


def test_session_draft_tools_are_marked_mutating():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)

    assert tools["get_workflow_plan_draft_tool"].is_read_only is False
    assert tools["update_workflow_plan_draft_tool"].is_read_only is False
    assert tools["finalize_extract_sync_plan_tool"].is_read_only is False
    assert tools["finalize_finish_processing_plan_tool"].is_read_only is False
    assert tools["finalize_workflow_plan_tool"].is_read_only is False


def test_session_update_draft_tool_exposes_only_new_patch_contract():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    schema = tools["update_workflow_plan_draft_tool"].input_schema

    assert set(schema["properties"]) == {
        "data_profile_patch",
        "observation_id",
        "used_tool",
    }
    assert "processing_profile" not in schema["properties"]
    assert "dataset_profile" not in schema["properties"]
    assert "profile" not in schema["properties"]
    assert "platform_hint" not in schema["properties"]
    assert "data_profile" not in schema["properties"]
    assert schema["required"] == [
        "data_profile_patch",
        "observation_id",
        "used_tool",
    ]
    patch_schema = schema["properties"]["data_profile_patch"]
    schema_options = patch_schema.get("anyOf", [patch_schema])
    assert {"type": "string"} not in schema_options


def test_session_update_draft_tool_rejects_string_patch_payload():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )

    result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": '{"platform_hint":"go2w"}',
            "observation_id": "sensor_bindings",
            "used_tool": "infer_navigation_sensor_bindings_tool",
        },
    )

    assert result["ok"] is False
    assert result["error_type"] == "invalid_data_profile_patch"
    assert store.load("agent-session-1").data_profile_draft == {}


def test_session_update_draft_tool_rejects_empty_patch_without_advancing_observation():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )

    result = _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": {},
            "observation_id": "raw_metadata",
            "used_tool": "inspect_raw_date_tool",
        },
    )

    state = store.load("agent-session-1")
    assert result["ok"] is False
    assert result["error_type"] == "empty_data_profile_patch"
    assert state.completed_observations == []
    assert state.next_tool_candidates() == ["inspect_raw_date_tool"]


def test_get_draft_tool_exposes_no_legacy_segments_or_dry_run_args():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    schema = tools["get_workflow_plan_draft_tool"].input_schema

    assert set(schema["properties"]) == {"date", "scene_mode"}
    assert "segments" not in schema["properties"]
    assert "dry_run" not in schema["properties"]


def test_get_draft_initializes_and_persists_request_without_segments():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)

    result = _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )

    assert result["ok"] is True
    assert result["draft"]["date"] == "20270605"
    assert result["draft"]["scene_mode"] == "out"
    assert result["draft"]["segments"] is None
    assert store.load("agent-session-1").date == "20270605"


def test_get_workflow_plan_draft_allows_date_without_scene_mode_for_extract_sync():
    store = InMemoryNavigationPlanDraftStore()
    tools = {
        tool.name: tool
        for tool in build_session_plan_draft_tools(store=store, session_id="session-a")
    }

    result = _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270623"},
    )

    assert result["ok"] is True
    assert result["draft"]["date"] == "20270623"
    assert result["draft"]["scene_mode"] is None
    assert "scene_mode" not in result["draft"]["missing_fields"]


def test_extract_sync_plan_finalizes_without_scene_mode(complete_extract_sync_draft_payload):
    store = InMemoryNavigationPlanDraftStore()
    tools = {
        tool.name: tool
        for tool in build_session_plan_draft_tools(store=store, session_id="session-a")
    }
    _invoke_tool(tools["get_workflow_plan_draft_tool"], {"date": "20270623"})
    for observation_id, used_tool in [
        ("raw_metadata", "inspect_raw_date_tool"),
        ("sensor_bindings", "infer_navigation_sensor_bindings_tool"),
        ("navigation_processing_profile", "infer_navigation_processing_profile_tool"),
    ]:
        _invoke_tool(
            tools["update_workflow_plan_draft_tool"],
            {
                "data_profile_patch": {"evidence": {observation_id: [used_tool]}},
                "observation_id": observation_id,
                "used_tool": used_tool,
            },
        )
    _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": complete_extract_sync_draft_payload,
            "observation_id": "navigation_topic_params",
            "used_tool": "infer_navigation_topic_params_tool",
        },
    )

    result = _invoke_tool(tools["finalize_extract_sync_plan_tool"], {})

    assert result["ok"] is True
    assert result["workflow_plan_json"]["phase"] == "extract_sync"
    assert result["workflow_plan_json"]["scene_mode"] is None
    assert [step["tool_name"] for step in result["workflow_plan_json"]["steps"]] == [
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
    ]


def test_get_workflow_plan_draft_advances_to_finish_processing_when_scene_mode_arrives_later(
    complete_extract_sync_draft_payload,
):
    store = InMemoryNavigationPlanDraftStore()
    tools = {
        tool.name: tool
        for tool in build_session_plan_draft_tools(store=store, session_id="session-a")
    }
    _invoke_tool(tools["get_workflow_plan_draft_tool"], {"date": "20270623"})
    for observation_id, used_tool in [
        ("raw_metadata", "inspect_raw_date_tool"),
        ("sensor_bindings", "infer_navigation_sensor_bindings_tool"),
        ("navigation_processing_profile", "infer_navigation_processing_profile_tool"),
    ]:
        _invoke_tool(
            tools["update_workflow_plan_draft_tool"],
            {
                "data_profile_patch": {"evidence": {observation_id: [used_tool]}},
                "observation_id": observation_id,
                "used_tool": used_tool,
            },
        )
    _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": complete_extract_sync_draft_payload,
            "observation_id": "navigation_topic_params",
            "used_tool": "infer_navigation_topic_params_tool",
        },
    )
    _invoke_tool(tools["finalize_extract_sync_plan_tool"], {})

    resumed = _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270623", "scene_mode": "out"},
    )

    assert resumed["ok"] is True
    assert resumed["draft"]["scene_mode"] == "out"
    assert resumed["draft"]["plan_phase"] == "finish_processing"
    assert resumed["draft"]["next_required_observation"]["observation_id"] == "processing_state"
    assert resumed["draft"]["next_tool_candidates"] == ["inspect_processing_state_tool"]


def test_get_draft_without_existing_state_requires_initial_request():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)

    result = _invoke_tool(tools["get_workflow_plan_draft_tool"], {})

    assert result["ok"] is False
    assert result["error_type"] == "missing_initial_navigation_request"
    assert "date" in result["missing_fields"]
    assert "scene_mode" not in result["missing_fields"]


def test_update_persists_partial_patch_for_same_session():
    store = InMemoryNavigationPlanDraftStore()
    first_tools = _tools(store)
    _invoke_tool(
        first_tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )

    update_result = _invoke_tool(
        first_tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": {"gridmap_source": "existing_gridmap"},
            "observation_id": "gridmap_artifacts",
            "used_tool": "inspect_gridmap_artifacts_tool",
        },
    )
    second_tools = _tools(store)
    resumed = _invoke_tool(second_tools["get_workflow_plan_draft_tool"], {})

    assert update_result["ok"] is True
    assert resumed["draft"]["data_profile_draft"]["gridmap_source"] == "existing_gridmap"
    assert resumed["draft"]["completed_observations"] == [
        {
            "observation_id": "gridmap_artifacts",
            "used_tool": "inspect_gridmap_artifacts_tool",
        }
    ]


def test_get_draft_rejects_request_mismatch_for_existing_session():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )

    result = _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270606", "scene_mode": "in"},
    )

    assert result["ok"] is False
    assert result["error_type"] == "workflow_plan_draft_request_mismatch"
    assert result["existing_request"] == {
        "date": "20270605",
        "segments": None,
        "scene_mode": "out",
        "dry_run": False,
    }
    assert result["requested_request"] == {
        "date": "20270606",
        "scene_mode": "in",
    }
    assert store.load("agent-session-1").date == "20270605"


def test_finalize_returns_structured_error_when_draft_is_incomplete():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )

    result = _invoke_tool(tools["finalize_workflow_plan_tool"], {})

    assert result["ok"] is False
    assert result["error_type"] == "workflow_plan_draft_incomplete"
    assert "processing_profile" in result["missing_fields"]
    assert "next_tool_candidates" in result


def test_finalize_success_persists_finalized_plan_for_same_session():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )
    _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": _complete_profile_patch(),
            "observation_id": "navigation_processing_profile",
            "used_tool": "infer_navigation_processing_profile_tool",
        },
    )

    result = _invoke_tool(tools["finalize_workflow_plan_tool"], {})
    persisted_state = store.load("agent-session-1")

    assert result["ok"] is True
    assert "workflow_plan_json" in result
    assert result["draft"]["finalized_plan"] is not None
    assert result["draft"]["finalized_plan"]["date"] == "20270605"
    assert persisted_state.finalized_plan is not None
    assert persisted_state.finalized_plan.steps[0].step_id == "confirm_navigation_calibration_params"


def test_finalize_rejects_plan_when_workflow_validation_fails(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)
    _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out"},
    )
    _invoke_tool(
        tools["update_workflow_plan_draft_tool"],
        {
            "data_profile_patch": _complete_profile_patch(),
            "observation_id": "navigation_processing_profile",
            "used_tool": "infer_navigation_processing_profile_tool",
        },
    )

    def fail_validation(*_args, **_kwargs):
        return {
            "ok": False,
            "errors": [
                {
                    "type": "invalid_gridmap_stage_order",
                    "message": "gridmap must be prepared after tracking",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        session_tools_module,
        "validate_workflow_plan",
        fail_validation,
        raising=False,
    )

    result = _invoke_tool(tools["finalize_workflow_plan_tool"], {})
    persisted_state = store.load("agent-session-1")

    assert result["ok"] is False
    assert result["error_type"] == "workflow_plan_validation_failed"
    assert result["validation_errors"][0]["type"] == "invalid_gridmap_stage_order"
    assert persisted_state.finalized_plan is None
