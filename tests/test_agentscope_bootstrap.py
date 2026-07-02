from pathlib import Path

import pytest

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime import agentscope_prompts
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


def test_main_router_prompt_presents_datapilot_and_sets_task_readiness_rules():
    prompt = main_router_prompt()

    assert prompt.startswith("You are DataPilot")
    assert "我是 DataPilot，一个 VLA 数据处理助手" in prompt
    assert "Do not reveal internal agent names" in prompt
    assert "Ordinary conversation" in prompt
    assert "Capability questions" in prompt
    assert "do not inspect the workspace" in prompt
    assert "date, path, or" in prompt
    assert "dataset target" in prompt
    assert "ask for the data date or path" in prompt
    assert "indoor or outdoor" in prompt
    assert "combine it with the pending task context" in prompt
    assert "If no clip is specified, process all clips" in prompt
    assert "If a specified clip does not exist" in prompt
    assert "start_navigation_data_task" in prompt
    assert "call start_navigation_data_task" in prompt
    assert "target" in prompt
    assert "scene_mode" in prompt
    assert "missing_fields" in prompt
    assert "confidence" in prompt
    assert "Do not call start_navigation_data_task with non-empty missing_fields" in prompt
    assert "vla_run_workflow" in prompt
    assert "vla_continue_workflow" in prompt
    assert "user's language" in prompt
    assert "You are MainRouterAgent" not in prompt
    assert "route to NavigationDataAgent" not in prompt

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
        "DataPilot's navigation data specialist",
        "plan-and-execute",
        "ReAct",
        "WorkflowPlan",
        "navigation-data-agent-planning-guidance",
        "Structured handoff JSON",
        "get_workflow_plan_draft_tool",
        "update_workflow_plan_draft_tool",
        "finalize_workflow_plan_tool",
        "do not hand-write final WorkflowPlan JSON",
        "read-only inspection tools before execution",
        "inspect_raw_date_tool",
        "infer_navigation_sensor_bindings_tool",
        "infer_navigation_processing_profile_tool",
        "infer_navigation_topic_params_tool",
        "inspect_processing_state_tool",
        "inspect_gridmap_artifacts_tool",
        "inspect_runtime_assets_tool",
        "list_navigation_tool_capabilities_tool",
        "native Ins",
        "odom_to_ins",
        "Capability questions",
        "do not inspect data",
        "scene mode is missing",
        "structured handoff",
        "target",
        "scene_mode",
        "clips",
        "indoor or outdoor",
        "If no clip is specified",
        "If a specified clip does not exist",
        "concise progress updates",
        "request_human_decision",
        "Do not ask the user to type",
        "confirm/stop/guidance",
        "GUI can block",
        "final summaries in the user's language",
    ]:
        assert expected in prompt

    assert "Plan-Agent workflow" not in prompt
    assert "user_confirmation" not in prompt
    assert "exactly `确认`" not in prompt
    assert "You are NavigationDataAgent" not in prompt
    assert "mock" not in prompt.lower()


def test_navigation_agent_prompt_uses_fallback_guidance_when_docs_file_is_missing(monkeypatch):
    def raise_missing_guidance(*args, **kwargs):
        raise OSError("missing guidance")

    monkeypatch.setattr(agentscope_prompts.Path, "read_text", raise_missing_guidance)

    prompt = navigation_agent_prompt()

    for expected in [
        "navigation-data-agent-planning-guidance",
        "get_workflow_plan_draft_tool",
        "finalize_workflow_plan_tool",
        "do not hand-write final WorkflowPlan JSON",
        "inspect_raw_date_tool",
        "list_navigation_tool_capabilities_tool",
        "native Ins",
        "odom_to_ins",
        "do not invent `TOPIC_WHITELIST`, `topic_map`, or `query_dir`",
        "copy them from `infer_navigation_topic_params_tool`",
        "do not invent localization policy or calibration policy",
        "copy them from `infer_navigation_processing_profile_tool`",
        "blocking_issues",
        "do not produce an executable plan",
        "copy_existing_gridmap",
        "generate_from_pcd",
        "skip_if_projection_ready",
        "Do not require data to fit fixed `u_legacy_like` or `go2w_like` classifications",
        "calibration confirmation gate is the first finalized WorkflowPlan step",
        "User confirmation, stop, and guidance decisions use `request_human_decision`",
        "If a unique Ins topic is present",
        "NoobScenes preprocessing skips odom conversion and resize preprocessing",
        "NoobScenes preprocessing runs odom conversion and resize preprocessing",
    ]:
        assert expected in prompt


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
