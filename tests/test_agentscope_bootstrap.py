from pathlib import Path

import pytest

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_prompts import (
    main_router_prompt,
    navigation_agent_prompt,
)


class FakeStorage:
    def __init__(self) -> None:
        self.credentials = []
        self.agents = []

    async def upsert_credential(self, user_id, credential_data):
        self.credentials.append((user_id, credential_data))
        return credential_data.id

    async def upsert_agent(self, user_id, agent_record):
        self.agents.append((user_id, agent_record))
        return agent_record.id


def _config(**overrides) -> AgentScopeRuntimeConfig:
    values = {
        "user_id": "alice",
        "redis_url": "redis://localhost:6379/0",
        "workspace_root": Path("/tmp/vla-agent-workspace"),
        "dashscope_api_key": "test-key",
        "dashscope_base_url": None,
        "default_model": "qwen-default",
        "router_model": "qwen-router",
        "navigation_model": "qwen-navigation",
    }
    values.update(overrides)
    return AgentScopeRuntimeConfig(**values)


def test_main_router_prompt_routes_navigation_and_rejects_old_workflow_tools():
    prompt = main_router_prompt()

    assert "MainRouterAgent" in prompt
    assert "Clear navigation requests" in prompt
    assert "Ordinary or non-navigation conversation" in prompt
    assert "answer normally" in prompt
    assert "must not route to NavigationDataAgent" in prompt
    assert "vla_run_workflow" in prompt
    assert "vla_continue_workflow" in prompt
    assert "Do not call" in prompt
    assert "one short clarifying question" in prompt
    assert "user's language" in prompt

    for term in [
        "VLA navigation data",
        "ROS bag/db3",
        "odom",
        "trajectory",
        "gridmap",
        "camera calibration",
        "dataset extraction",
        "sync_data",
        "finish_data",
        "annotation",
        "gen_box.py",
        "tracking",
        "projection",
    ]:
        assert term in prompt

    assert "mock" not in prompt.lower()


def test_navigation_agent_prompt_requires_plan_execute_react_and_human_decisions():
    prompt = navigation_agent_prompt()

    for expected in [
        "NavigationDataAgent",
        "plan-and-execute",
        "ReAct",
        "WorkflowPlan",
        "request_human_decision",
        "Do not ask the user to type",
        "confirm/stop/guidance",
        "GUI can block",
        "final summaries in the user's language",
    ]:
        assert expected in prompt

    assert "mock" not in prompt.lower()


@pytest.mark.asyncio
async def test_bootstrap_agentscope_records_upserts_credential_and_agents():
    storage = FakeStorage()
    config = _config()

    records = await bootstrap_agentscope_records(storage, config)

    assert records.credential_id == "dashscope-env"
    assert records.main_router_agent_id == "main-router-agent"
    assert records.navigation_agent_id == "navigation-data-agent"

    assert len(storage.credentials) == 1
    credential_user_id, credential = storage.credentials[0]
    assert credential_user_id == "alice"
    assert credential.id == "dashscope-env"
    assert credential.name == "DashScope"
    assert credential.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    assert len(storage.agents) == 2
    agent_records = [record for _, record in storage.agents]
    assert [record.user_id for record in agent_records] == ["alice", "alice"]
    assert [record.id for record in agent_records] == [
        "main-router-agent",
        "navigation-data-agent",
    ]
    assert [record.data.id for record in agent_records] == [
        "main-router-agent",
        "navigation-data-agent",
    ]
    assert all(record.id == record.data.id for record in agent_records)
    assert [record.data.name for record in agent_records] == [
        "MainRouterAgent",
        "NavigationDataAgent",
    ]
    assert [record.data.react_config.max_iters for record in agent_records] == [8, 40]
    assert all(record.data.context_config.tool_result_limit == 6000 for record in agent_records)
    assert all("mock" not in record.data.system_prompt.lower() for record in agent_records)
    assert "plan-and-execute" in agent_records[1].data.system_prompt
    assert "ReAct" in agent_records[1].data.system_prompt
    assert "request_human_decision" in agent_records[1].data.system_prompt
    assert "Do not ask the user to type" in agent_records[1].data.system_prompt


@pytest.mark.asyncio
async def test_bootstrap_uses_configured_base_url_and_configured_agent_record_ids():
    storage = FakeStorage()
    config = _config(
        credential_id="dashscope-custom",
        main_router_agent_id="router-custom",
        navigation_agent_id="navigation-custom",
        dashscope_base_url="https://dashscope.example.test",
    )

    records = await bootstrap_agentscope_records(storage, config)

    assert records.credential_id == "dashscope-custom"
    assert records.main_router_agent_id == "router-custom"
    assert records.navigation_agent_id == "navigation-custom"
    assert storage.credentials[0][1].id == "dashscope-custom"
    assert storage.credentials[0][1].base_url == "https://dashscope.example.test"
    assert [record.id for _, record in storage.agents] == [
        "router-custom",
        "navigation-custom",
    ]
    assert all(record.id == record.data.id for _, record in storage.agents)
