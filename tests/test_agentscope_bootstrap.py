from pathlib import Path

import pytest

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_prompts import (
    main_router_prompt,
    navigation_agent_prompt,
)
from vla_data_juicer_agents.runtime.agentscope_runtime import NavigationHandoffTool


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


def test_main_router_prompt_is_triage_only_and_handles_handoff_truthfully():
    prompt = main_router_prompt()

    assert prompt.startswith("You are DataPilot")
    assert "我是 DataPilot，一个 VLA 数据处理助手" in prompt
    assert "Do not reveal internal agent names" in prompt
    assert "Ordinary conversation" in prompt
    assert "Capability questions" in prompt
    assert "concrete navigation-processing request" in prompt
    assert "date, path, or dataset target" in prompt
    assert "start_navigation_data_task" in prompt
    for preserved in ["request", "target", "date", "clips", "scene_mode", "response_language"]:
        assert preserved in prompt
    for required_handoff_field in NavigationHandoffTool.input_schema["required"]:
        assert required_handoff_field in prompt
    assert "all required fields exactly once" in prompt
    assert "The only optional fields are clips and scene_mode" in prompt
    assert "use clips, never segments" in prompt
    assert "set missing_fields to []" in prompt
    assert "set confidence to medium or high" in prompt
    assert "always include a concise reason" in prompt
    assert "`ok: true` and `started: true`" in prompt
    assert "`ok: false`" in prompt
    assert "never claim" in prompt.lower()
    assert "shell" in prompt.lower()
    assert "You are MainRouterAgent" not in prompt
    assert "route to NavigationDataAgent" not in prompt

    for artifact_or_stage_rule in [
        "ROS bag/db3",
        "odom",
        "gridmap",
        "camera calibration",
        "sync_data",
        "finish_data",
        "annotation",
        "tracking",
        "projection",
        "extract/sync",
        "finish-processing",
        "active phase",
    ]:
        assert artifact_or_stage_rule not in prompt

    assert "mock" not in prompt.lower()
    assert "Activity: a concise user-facing statement" in prompt
    assert "without a preceding Activity violates this output contract" in prompt
    assert "Do not output Thought, Observation, Analysis, Action" in prompt
    assert "private chain-of-thought" in prompt
    assert "tool or function names" in prompt
    assert "`Answer:` is a presentation-channel marker" in prompt
    assert "does not mean that the overall task or conversation is complete" in prompt
    assert "ordinary conversation, capability answers, clarification questions" in prompt
    assert "Whenever this reply yields control back to the user" in prompt
    assert "even if a later turn may call tools" in prompt
    assert "output only one short `Answer:` clarification question" in prompt
    assert "no identity/capability preamble, Activity, explanation, examples, or bullet list" in prompt
    assert "Answer: 请提供要处理数据的日期、路径或数据目标？" in prompt
    assert "copy the entire latest user message verbatim" in prompt
    assert "punctuation, blank lines, labels, and list items" in prompt
    assert "Never summarize, paraphrase, normalize, translate, or omit" in prompt


def test_all_agent_prompts_define_answer_as_the_persistent_chat_channel():
    for prompt in (main_router_prompt(), navigation_agent_prompt()):
        assert "Activity lines are transient progress metadata" in prompt
        assert "every persistent assistant chat message" in prompt
        assert "requests for missing information" in prompt
        assert "partial or stage results, and final results" in prompt
        assert "put a question to the user after `Answer:`" in prompt
        assert "Never call a tool after beginning `Answer:` in the same reply" in prompt


def test_router_handoff_schema_omits_dry_run():
    schema = NavigationHandoffTool.input_schema

    assert "dry_run" not in schema["properties"]
    assert "dry_run" not in schema["required"]
    assert _config().navigation_dry_run is False


def test_navigation_agent_prompt_requires_model_directed_investigation_and_plans():
    prompt = navigation_agent_prompt()

    for exact_concept in [
        "Investigate before deciding",
        "user claims",
        "conversation memory",
        "older task status",
        "older product snapshots",
        "current product facts",
        "Call inspection tools yourself in every fresh task attempt",
        "which investigation tools",
        "processing stage",
        "decisions, steps, variants, and business parameters",
        "one-time optimistic-concurrency token",
        "Any later investigation or user-guidance update",
        "immediately before submitting a Plan",
        "complete strict JSON Plan",
        "resubmit the whole Plan",
        "After a complete Plan is accepted",
        "running in the background",
        "never poll with get_current_plan_step_tool",
        "wake the same session automatically",
        "verify the produced outputs",
        "ask whether to continue",
        "finish-processing inputs",
        "same-session",
        "new Web session",
        "fresh task attempt",
    ]:
        assert exact_concept in prompt

    for duplicated_or_legacy_concept in [
        "phase_profile_schema",
        "data_profile_draft",
        "data_profile_patch",
        "runtime-selected phase",
        "active phase",
        "Reconcile raw, intermediate, and final artifacts",
        "one submission tool exposed",
        "phase schema",
    ]:
        assert duplicated_or_legacy_concept not in prompt

    for retained_contract in [
        "plan-and-execute",
        "ReAct",
        "confirm_navigation_calibration_params_tool",
        "Do not ask the user to type",
        "confirm/stop/guidance",
        "GUI can block",
        "final summaries in the user's language",
        "concise progress updates",
        "cancelled",
    ]:
        assert retained_contract in prompt
    assert "request_human_decision" not in prompt
    assert "Plan-Agent workflow" not in prompt
    assert "user_confirmation" not in prompt
    assert "exactly `确认`" not in prompt
    assert "继续执行 plus" not in prompt
    assert "You are NavigationDataAgent" not in prompt
    assert "mock" not in prompt.lower()
    assert "Activity: a concise user-facing statement" in prompt
    assert "without a preceding Activity violates this output contract" in prompt
    assert "Do not output Thought, Observation, Analysis, Action" in prompt
    assert "transient progress metadata shown in the processing disclosure" in prompt
    assert "not persistent assistant chat messages" in prompt


def test_navigation_agent_prompt_states_system_managed_phase_transitions():
    prompt = navigation_agent_prompt()

    for exact_contract in [
        "Plan submission never starts processing",
        "continue the same reply",
        "read the accepted Plan's current step",
        "call the matching plan-bound tool",
        "do not use generic shell or file tools",
        "after the last Plan step",
        "investigation/planning tools become available again",
    ]:
        assert exact_contract in prompt
    assert prompt.lower().count("after extract/sync") == 1


def test_navigation_agent_prompt_has_no_legacy_guidance_loader_contract():
    prompt = navigation_agent_prompt()

    assert "accepted Plan and execution ledger are durable" in prompt
    assert "navigation-data-agent-planning-guidance" not in prompt
    assert "data_profile_patch" not in prompt


@pytest.mark.asyncio
async def test_bootstrap_injects_navigation_guidance_once_and_never_into_router():
    storage = FakeStorage()

    await bootstrap_agentscope_records(storage, _config())

    router_prompt, navigation_prompt = [
        record.data.system_prompt for _, record in storage.agents
    ]
    guidance_marker = "# Navigation Plan Agent Guidance"
    assert guidance_marker not in router_prompt
    assert navigation_prompt.count(guidance_marker) == 1
    assert navigation_prompt.count("## Product dependency map") == 1
    assert navigation_prompt.count("## Four bounded few-shots") == 1
    assert navigation_prompt.count("### Few-shot 4: invalid complete Plan") == 1


@pytest.mark.asyncio
async def test_bootstrap_fails_closed_before_writes_when_navigation_guidance_is_missing(
    tmp_path, monkeypatch
):
    missing = tmp_path / "missing-navigation-guidance.md"
    monkeypatch.setattr(
        "vla_data_juicer_agents.runtime.agentscope_prompts.NAVIGATION_AGENT_GUIDANCE_PATH",
        missing,
        raising=False,
    )
    storage = FakeStorage()

    with pytest.raises(RuntimeError, match="navigation agent guidance.*missing"):
        await bootstrap_agentscope_records(storage, _config())

    assert storage.credentials == []
    assert storage.agents == []


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
    assert "confirm_navigation_calibration_params_tool" in agent_records[1].data.system_prompt
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
