import asyncio

from agentscope.permission import PermissionBehavior, PermissionDecision

from vla_data_juicer_agents.navigation.agent_tools import (
    HumanDecisionTool,
    build_navigation_agent_tools,
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
