import asyncio
import inspect
import json

from vla_data_juicer_agents.navigation.plan_draft_store import InMemoryNavigationPlanDraftStore
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


def test_get_draft_initializes_and_persists_request():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)

    result = _invoke_tool(
        tools["get_workflow_plan_draft_tool"],
        {"date": "20270605", "scene_mode": "out", "segments": ["clip-a"]},
    )

    assert result["ok"] is True
    assert result["draft"]["date"] == "20270605"
    assert result["draft"]["scene_mode"] == "out"
    assert result["draft"]["segments"] == ["clip-a"]
    assert store.load("agent-session-1").date == "20270605"


def test_get_draft_without_existing_state_requires_initial_request():
    store = InMemoryNavigationPlanDraftStore()
    tools = _tools(store)

    result = _invoke_tool(tools["get_workflow_plan_draft_tool"], {})

    assert result["ok"] is False
    assert result["error_type"] == "missing_initial_navigation_request"
    assert "date" in result["missing_fields"]
    assert "scene_mode" in result["missing_fields"]


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
