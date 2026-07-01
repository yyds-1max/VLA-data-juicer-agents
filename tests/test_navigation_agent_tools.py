import asyncio
from types import SimpleNamespace

from agentscope.permission import PermissionBehavior, PermissionDecision

from vla_data_juicer_agents.navigation.agent_tools import (
    HumanDecisionTool,
    build_navigation_agent_tools,
)
from vla_data_juicer_agents.runtime import agentscope_runtime as runtime_module
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    build_extra_agent_tools_factory,
    create_agentscope_runtime,
)


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
