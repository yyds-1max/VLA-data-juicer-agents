import asyncio
from types import SimpleNamespace

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import ToolResultState

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation import agent_tools as agent_tools_module
from vla_data_juicer_agents.navigation.agent_tools import (
    HumanDecisionTool,
    build_navigation_agent_tools,
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


def test_build_navigation_agent_tools_includes_human_decision_and_existing_processing_tools():
    names = {tool.name for tool in build_navigation_agent_tools(dry_run=True)}

    assert {
        "request_human_decision",
        "prepare_raw_data_tool",
        "extract_and_sync_navigation_data_tool",
        "run_initial_annotation_gui_tool",
        "run_tracking_tool",
    }.issubset(names)


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

    assert {tool.name for tool in tools} == {"request_human_decision"}
    assert captured == {"dry_run": True, "cancellation": cancellation}


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

    def fake_build_navigation_agent_tools(*, dry_run, cancellation=None):
        captured["dry_run"] = dry_run
        captured["cancellation"] = cancellation
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
    assert captured == {"dry_run": False, "cancellation": cancellation}


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


def test_navigation_handoff_tool_records_observability_payload():
    runtime = FakeNavigationHandoffRuntime()
    tool = NavigationHandoffTool(runtime=runtime, web_session_id="web-1")

    asyncio.run(
        tool(
            request="处理 20270605 clip_001 的室内导航数据",
            target="20270605",
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
