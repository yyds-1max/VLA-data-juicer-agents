import asyncio
import json
from types import SimpleNamespace

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import ToolResultState
from agentscope.tool import FunctionTool

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation import agent_tools as agent_tools_module
from vla_data_juicer_agents.navigation.agent_tools import (
    HumanDecisionTool,
    build_navigation_agent_tools,
)
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.plan_draft import (
    WorkflowPlanDraftState,
    build_plan_from_draft,
)
from vla_data_juicer_agents.navigation.plan_draft_store import (
    InMemoryNavigationPlanDraftStore,
    JsonNavigationPlanDraftStore,
)
from vla_data_juicer_agents.runtime import agentscope_runtime as runtime_module
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    NavigationHandoffTool,
    build_extra_agent_tools_factory,
    create_agentscope_runtime,
)


class FakeNavigationHandoffRuntime:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.records: list[dict] = []

    async def start_navigation_agent_task(self, *, web_session_id: str, message: str) -> str:
        self.started.append({"web_session_id": web_session_id, "message": message})
        return "navigation-session"

    def record_navigation_handoff(self, payload: dict) -> None:
        self.records.append(payload)


def _text(chunk) -> str:
    return chunk.content[0].text


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


def _complete_profile_patch():
    topic_params = {
        "profile_hint": "go2w",
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
    return {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "topic_params": topic_params,
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_policy": {"source": "existing_gridmap"},
            "calibration_policy": {
                "mode": "hardcoded_with_user_confirmation",
                "requires_user_confirmation": True,
            },
            "warnings": [],
            "blocking_issues": [],
            "evidence": {"processing_profile": ["infer_navigation_processing_profile_tool"]},
        },
        "platform_hint": "go2w",
        "topic_params": topic_params,
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


def test_human_decision_tool_declares_external_read_only_schema():
    tool = HumanDecisionTool()

    assert tool.name == "request_human_decision"
    assert tool.is_external_tool is True
    assert tool.is_read_only is True
    assert set(tool.input_schema["properties"]) == {
        "decision_type",
        "request_id",
        "summary",
    }
    assert tool.input_schema["required"] == [
        "decision_type",
        "request_id",
        "summary",
    ]


def test_human_decision_tool_allows_permissions():
    tool = HumanDecisionTool()

    decision = asyncio.run(tool.check_permissions({}, None))

    assert isinstance(decision, PermissionDecision)
    assert decision.behavior is PermissionBehavior.ALLOW


def test_build_navigation_agent_tools_includes_planning_human_decision_and_processing_tools():
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=InMemoryNavigationPlanDraftStore(),
        )
    }
    names = set(tools)

    assert {
        "request_human_decision",
        "confirm_navigation_calibration_params_tool",
        "inspect_raw_date_tool",
        "infer_navigation_sensor_bindings_tool",
        "infer_navigation_processing_profile_tool",
        "infer_navigation_topic_params_tool",
        "inspect_processing_state_tool",
        "inspect_gridmap_artifacts_tool",
        "inspect_runtime_assets_tool",
        "list_navigation_tool_capabilities_tool",
        "get_workflow_plan_draft_tool",
        "update_workflow_plan_draft_tool",
        "finalize_workflow_plan_tool",
        "prepare_raw_data_tool",
        "extract_and_sync_navigation_data_tool",
        "run_noobscene_preprocessing_tool",
        "run_initial_annotation_gui_tool",
        "run_tracking_tool",
    }.issubset(names)
    assert tools["confirm_navigation_calibration_params_tool"].is_external_tool is True
    assert tools["confirm_navigation_calibration_params_tool"].is_read_only is True


def test_build_navigation_agent_tools_omits_draft_tools_without_session_store():
    tools = {tool.name: tool for tool in build_navigation_agent_tools(dry_run=True)}
    names = set(tools)

    assert "request_human_decision" in names
    assert tools["confirm_navigation_calibration_params_tool"].is_external_tool is True
    assert "prepare_raw_data_tool" in names
    assert "inspect_raw_date_tool" in names
    assert "infer_navigation_processing_profile_tool" in names
    assert "get_workflow_plan_draft_tool" not in names
    assert "finalize_workflow_plan_tool" not in names


def test_build_navigation_agent_tools_does_not_register_old_workflow_control_tools():
    names = {tool.name for tool in build_navigation_agent_tools(dry_run=True)}

    assert "vla_run_workflow" not in names
    assert "vla_continue_workflow" not in names


def test_build_navigation_agent_tools_passes_cancellation_to_execution_tools(monkeypatch):
    cancellation = CancellationContext()
    captured = {}

    def fake_create_navigation_execution_tools(*, dry_run, cancellation=None):
        captured["dry_run"] = dry_run
        captured["cancellation"] = cancellation
        return []

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        fake_create_navigation_execution_tools,
    )

    tools = build_navigation_agent_tools(dry_run=True, cancellation=cancellation)

    assert {tool.name for tool in tools} == {
        "request_human_decision",
        "confirm_navigation_calibration_params_tool",
        "inspect_raw_date_tool",
        "infer_navigation_sensor_bindings_tool",
        "infer_navigation_processing_profile_tool",
        "infer_navigation_topic_params_tool",
        "inspect_processing_state_tool",
        "inspect_gridmap_artifacts_tool",
        "inspect_runtime_assets_tool",
        "list_navigation_tool_capabilities_tool",
    }
    assert captured == {"dry_run": True, "cancellation": cancellation}


def test_navigation_execution_tool_is_blocked_before_session_plan_is_finalized(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "prepare_raw_data", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_plan_not_finalized"
    assert result["next_tool_candidates"] == ["get_workflow_plan_draft_tool"]


def test_navigation_execution_tool_is_allowed_after_session_plan_is_finalized(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "prepare_raw_data", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    state.update(data_profile_patch=_complete_profile_patch())
    state.finalized_plan = build_plan_from_draft(state)
    store.save("agent-session-1", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert result["ok"] is True
    assert result["tool_name"] == "prepare_raw_data"


def test_navigation_execution_tool_rejects_segment_mismatch_after_session_plan_is_finalized(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
        return {
            "ok": True,
            "tool_name": "prepare_raw_data",
            "date": date,
            "segments": segments,
        }

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270605",
            scene_mode="out",
            segments=["20260605_152856"],
        )
    )
    state.update(data_profile_patch=_complete_profile_patch())
    state.finalized_plan = build_plan_from_draft(state)
    store.save("agent-session-1", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    wrong_segments = _decode_tool_payload(
        asyncio.run(
            tools["prepare_raw_data_tool"](
                date="20270605",
                segments=["20260605_152930"],
            )
        )
    )
    missing_segments = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert wrong_segments["ok"] is False
    assert wrong_segments["error_type"] == "navigation_plan_request_mismatch"
    assert wrong_segments["requested_request"] == {
        "date": "20270605",
        "segments": ["20260605_152930"],
    }
    assert missing_segments["ok"] is False
    assert missing_segments["error_type"] == "navigation_plan_request_mismatch"
    assert missing_segments["requested_request"] == {
        "date": "20270605",
        "segments": None,
    }


def test_extra_agent_tools_factory_registers_navigation_tools_only_for_navigation_agent(tmp_path):
    config = AgentScopeRuntimeConfig(
        user_id="alice",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
    )
    factory = build_extra_agent_tools_factory(config)

    navigation_tools = asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    router_tools = asyncio.run(factory("alice", config.main_router_agent_id, "session-1"))

    navigation_names = {tool.name for tool in navigation_tools}
    assert "request_human_decision" in navigation_names
    assert "extract_and_sync_navigation_data_tool" in navigation_names
    assert "vla_run_workflow" not in navigation_names
    assert "vla_continue_workflow" not in navigation_names
    assert router_tools == []


def test_extra_agent_tools_factory_passes_runtime_cancellation_to_navigation_tools(
    monkeypatch,
    tmp_path,
):
    config = AgentScopeRuntimeConfig(
        user_id="alice",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
    )
    cancellation = CancellationContext()
    captured = {}

    def fake_build_navigation_agent_tools(
        *,
        dry_run,
        cancellation=None,
        session_id=None,
        draft_store=None,
    ):
        captured["dry_run"] = dry_run
        captured["cancellation"] = cancellation
        captured["session_id"] = session_id
        captured["draft_store"] = draft_store
        return [SimpleNamespace(name="navigation_tool")]

    monkeypatch.setattr(
        runtime_module,
        "build_navigation_agent_tools",
        fake_build_navigation_agent_tools,
    )
    runtime = SimpleNamespace(run_cancellation=lambda session_id: cancellation)
    factory = build_extra_agent_tools_factory(config, runtime=runtime)

    tools = asyncio.run(factory("alice", config.navigation_agent_id, "as-session-1"))

    assert [tool.name for tool in tools] == ["navigation_tool"]
    assert captured["dry_run"] is False
    assert captured["cancellation"] is cancellation
    assert captured["session_id"] == "as-session-1"
    assert captured["draft_store"].root == tmp_path / "navigation-plan-drafts"


def test_extra_agent_tools_factory_passes_session_bound_draft_store(tmp_path):
    config = AgentScopeRuntimeConfig(
        user_id="alice",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
    )
    factory = build_extra_agent_tools_factory(config)

    tools = {
        tool.name: tool
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }

    assert "get_workflow_plan_draft_tool" in tools
    result = _decode_tool_payload(
        asyncio.run(
            tools["get_workflow_plan_draft_tool"](
                date="20270605",
                scene_mode="out",
            )
        )
    )
    assert result["ok"] is True
    assert result["draft"]["date"] == "20270605"
    assert (tmp_path / "navigation-plan-drafts").exists()


def test_navigation_agent_tools_can_resume_finalized_plan_from_store(tmp_path):
    config = AgentScopeRuntimeConfig(
        user_id="alice",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
    )
    factory = build_extra_agent_tools_factory(config)

    first_tools = {
        tool.name: tool
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }
    _decode_tool_payload(
        asyncio.run(
            first_tools["get_workflow_plan_draft_tool"](
                date="20270605",
                scene_mode="out",
            )
        )
    )
    patch = {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "topic_params": {
                "profile_hint": "go2w",
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
            },
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_policy": {"source": "existing_gridmap"},
            "calibration_policy": {
                "mode": "hardcoded_with_user_confirmation",
                "requires_user_confirmation": True,
            },
            "warnings": [],
            "blocking_issues": [],
            "evidence": {"processing_profile": ["infer_navigation_processing_profile_tool"]},
        },
        "platform_hint": "go2w",
        "topic_params": {
            "profile_hint": "go2w",
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
        },
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
    _decode_tool_payload(
        asyncio.run(
            first_tools["update_workflow_plan_draft_tool"](
                data_profile_patch=patch,
                observation_id="navigation_processing_profile",
                used_tool="infer_navigation_processing_profile_tool",
            )
        )
    )
    finalized = _decode_tool_payload(
        asyncio.run(first_tools["finalize_workflow_plan_tool"]())
    )
    second_tools = {
        tool.name: tool
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }
    resumed = _decode_tool_payload(
        asyncio.run(second_tools["get_workflow_plan_draft_tool"]())
    )
    persisted_state = JsonNavigationPlanDraftStore(
        tmp_path / "navigation-plan-drafts"
    ).load("session-1")

    assert finalized["ok"] is True
    assert finalized["workflow_plan_json"]["date"] == "20270605"
    assert resumed["draft"]["ready_to_finish"] is True
    assert resumed["draft"]["data_profile_draft"]["localization_policy"] == {
        "source": "odom",
        "conversion": "odom_to_ins",
    }
    assert resumed["draft"]["data_profile"] is not None
    assert resumed["draft"]["data_profile"]["date"] == "20270605"
    assert persisted_state is not None
    assert persisted_state.finalized_plan is not None
    assert persisted_state.finalized_plan.date == "20270605"
    assert (
        persisted_state.finalized_plan.steps[0].step_id
        == "confirm_navigation_calibration_params"
    )


def test_extra_agent_tools_factory_registers_router_handoff_when_runtime_available(tmp_path):
    config = AgentScopeRuntimeConfig(
        user_id="alice",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
    )
    runtime = SimpleNamespace()
    factory = build_extra_agent_tools_factory(config, runtime=runtime)

    router_tools = asyncio.run(factory("alice", config.main_router_agent_id, "web-1__main-router-agent"))

    assert {tool.name for tool in router_tools} == {"start_navigation_data_task"}


def test_navigation_handoff_tool_declares_structured_schema():
    tool = NavigationHandoffTool(
        runtime=FakeNavigationHandoffRuntime(),
        web_session_id="web-1",
    )

    assert set(tool.input_schema["properties"]) == {
        "request",
        "target",
        "date",
        "scene_mode",
        "clips",
        "reason",
        "missing_fields",
        "confidence",
        "response_language",
    }
    assert tool.input_schema["required"] == [
        "request",
        "target",
        "date",
        "scene_mode",
        "reason",
        "missing_fields",
        "confidence",
        "response_language",
    ]
    assert tool.input_schema["properties"]["scene_mode"]["enum"] == ["indoor", "outdoor", "unknown"]
    assert tool.input_schema["properties"]["confidence"]["enum"] == ["low", "medium", "high"]


def test_navigation_handoff_tool_rejects_missing_fields_without_starting_navigation():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    result = asyncio.run(
        tool(
            request="处理导航数据",
            target="20270605",
            date="20270605",
            scene_mode="unknown",
            reason="用户想处理导航数据",
            missing_fields=["scene_mode"],
            confidence="high",
            response_language="Chinese",
        )
    )

    assert result.state is ToolResultState.ERROR
    assert "missing_fields" in _text(result)
    assert runtime.started == []
    assert runtime.records[-1]["started"] is False
    assert runtime.records[-1]["missing_fields"] == ["scene_mode"]


def test_navigation_handoff_tool_rejects_low_confidence_without_starting_navigation():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    result = asyncio.run(
        tool(
            request="你能处理导航数据吗",
            target="",
            date="",
            scene_mode="",
            reason="用户只是询问能力",
            missing_fields=[],
            confidence="low",
            response_language="Chinese",
        )
    )

    assert result.state is ToolResultState.ERROR
    assert "confidence" in _text(result)
    assert runtime.started == []
    assert runtime.records[-1]["started"] is False
    assert runtime.records[-1]["confidence"] == "low"


def test_navigation_handoff_tool_rejects_unsupported_confidence_without_starting_navigation():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    result = asyncio.run(
        tool(
            request="处理 20270605 的室外导航数据",
            target="20270605",
            date="20270605",
            scene_mode="outdoor",
            reason="用户看起来想处理导航数据",
            missing_fields=[],
            confidence="unknown",
            response_language="Chinese",
        )
    )

    assert result.state is ToolResultState.ERROR
    assert "confidence" in _text(result)
    assert runtime.started == []
    assert runtime.records[-1]["started"] is False
    assert runtime.records[-1]["confidence"] == "unknown"


def test_navigation_handoff_tool_starts_navigation_with_structured_context():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    result = asyncio.run(
        tool(
            request="处理 20270605 的室外导航数据",
            target="20270605",
            date="20270605",
            scene_mode="outdoor",
            clips=[],
            reason="用户给出了日期和室外场景并要求处理导航数据",
            missing_fields=[],
            confidence="high",
            response_language="Chinese",
        )
    )

    assert result.state is ToolResultState.SUCCESS
    assert runtime.started[0]["web_session_id"] == "web-1"
    message = runtime.started[0]["message"]
    assert "导航数据处理请求：" in message
    assert "用户原始请求: 处理 20270605 的室外导航数据" in message
    assert "处理目标: 20270605" in message
    assert "场景模式: outdoor" in message
    assert "clips: all" in message
    assert "转交原因: 用户给出了日期和室外场景并要求处理导航数据" in message
    assert "回复语言: Chinese" in message
    assert "请始终使用中文回复用户。" in message


def test_navigation_handoff_tool_uses_explicit_date_not_clip_prefix_for_draft_initialization():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    asyncio.run(
        tool(
            request="处理导航数据，指定 clip 为 20260605_152856",
            target="20260605_152856",
            date="20270605",
            scene_mode="outdoor",
            clips=["20260605_152856"],
            reason="用户给出了数据日期、室外场景和指定 clip",
            missing_fields=[],
            confidence="high",
            response_language="Chinese",
        )
    )

    message = runtime.started[0]["message"]
    json_text = message.split("Structured handoff JSON:", 1)[1].strip()
    payload = json.loads(json_text)

    assert payload["date"] == "20270605"
    assert payload["target"] == "20260605_152856"
    assert payload["segments"] == ["20260605_152856"]


def test_navigation_handoff_message_includes_structured_json_for_draft_initialization():
    message = runtime_module._navigation_handoff_message(
        request="处理 20270605 的导航数据",
        target="20270605",
        date="20270605",
        scene_mode="outdoor",
        clips=["20260605_152856"],
        reason="用户要处理导航数据",
        response_language="Chinese",
    )

    json_text = message.split("Structured handoff JSON:", 1)[1].strip()
    payload = json.loads(json_text)

    assert payload == {
        "request": "处理 20270605 的导航数据",
        "target": "20270605",
        "date": "20270605",
        "scene_mode": "out",
        "clips": ["20260605_152856"],
        "segments": ["20260605_152856"],
        "reason": "用户要处理导航数据",
        "response_language": "Chinese",
    }


def test_navigation_handoff_message_prefers_task_date_over_clip_prefix_date():
    message = runtime_module._navigation_handoff_message(
        request="请帮我处理一下20270605的导航数据，室外数据。只处理20260605_152856就可以。",
        target="20260605_152856",
        date="20270605",
        scene_mode="outdoor",
        clips=["20260605_152856"],
        reason="用户给出了日期、室外场景和指定 clip",
        response_language="Chinese",
    )

    json_text = message.split("Structured handoff JSON:", 1)[1].strip()
    payload = json.loads(json_text)

    assert payload["date"] == "20270605"
    assert payload["target"] == "20260605_152856"
    assert payload["segments"] == ["20260605_152856"]


def test_navigation_handoff_tool_records_observability_payload():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    asyncio.run(
        tool(
            request="处理 20270605 clip_001 的室内导航数据",
            target="20270605",
            date="20270605",
            scene_mode="indoor",
            clips=["clip_001"],
            reason="用户明确指定日期、clip 和室内场景",
            missing_fields=[],
            confidence="medium",
            response_language="Chinese",
        )
    )

    assert runtime.records == [
        {
            "web_session_id": "web-1",
            "request": "处理 20270605 clip_001 的室内导航数据",
            "target": "20270605",
            "date": "20270605",
            "scene_mode": "indoor",
            "clips": ["clip_001"],
            "reason": "用户明确指定日期、clip 和室内场景",
            "missing_fields": [],
            "confidence": "medium",
            "response_language": "Chinese",
            "started": True,
        }
    ]


def test_create_agentscope_runtime_wires_navigation_tools_factory(monkeypatch, tmp_path):
    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr(runtime_module.agentscope.app, "create_app", fake_create_app)
    config = AgentScopeRuntimeConfig(
        user_id="alice",
        redis_url="redis://localhost:6379/0",
        workspace_root=tmp_path,
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
    )

    create_agentscope_runtime(config)

    factory = captured["extra_agent_tools"]
    assert factory is not None
    tool_names = {
        tool.name
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }
    assert "request_human_decision" in tool_names
    assert "extract_and_sync_navigation_data_tool" in tool_names
