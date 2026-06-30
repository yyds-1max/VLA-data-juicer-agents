import asyncio

from agentscope.permission import PermissionBehavior, PermissionDecision

from vla_data_juicer_agents.navigation.agent_tools import HumanDecisionTool


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
