from pathlib import Path

import pytest

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_prompts import (
    main_router_v1_prompt,
    navigation_agent_prompt,
)
from vla_data_juicer_agents.runtime.single_agent import StartNavigationDataTaskV1Tool


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


def test_main_router_prompt_is_v1_triage_and_handles_delegation_truthfully():
    prompt = main_router_v1_prompt()

    assert prompt.startswith("You are DataPilot")
    assert "Do not reveal internal agent names" in prompt
    assert "ordinary conversation" in prompt
    assert "capability questions" in prompt
    assert "start_navigation_data_task" in prompt
    for preserved in ["dataset_date", "selection", "clips"]:
        assert preserved in prompt
    for required_handoff_field in StartNavigationDataTaskV1Tool.input_schema["required"]:
        assert required_handoff_field in prompt
    assert "all_clips" in prompt
    assert "selected_clips" in prompt
    assert "No clip list means all clips" in prompt
    assert "A clip ID is an opaque child-directory" in prompt
    assert "20270605` with clip `20260605_152856` is valid" in prompt
    assert "Never ask for or accept an internal segment or sequence" in prompt
    assert "Scene mode is optional" in prompt
    assert "continue_navigation_data_task has no model-authored arguments" in prompt
    assert "control_navigation_data_task accepts only `action`" in prompt
    assert "End immediately after the tool result" in prompt
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
    assert "routing actions do not produce Activity lines" in prompt
    assert "Do not output Thought, Observation, Analysis, Action" in prompt
    assert "private chain-of-thought" in prompt
    assert "tool names" in prompt
    assert "persistent user-visible Router message after an `Answer:` line" in prompt
    assert "ask exactly one short clarification question" in prompt


def test_agent_prompts_define_answer_as_the_persistent_chat_channel():
    router_prompt = main_router_v1_prompt()
    navigation_prompt = navigation_agent_prompt()
    assert "persistent user-visible Router message after an `Answer:` line" in router_prompt
    assert "never call a tool after beginning it" in router_prompt
    assert "Activity lines are transient progress metadata" in navigation_prompt
    assert "every persistent assistant chat message" in navigation_prompt
    assert "Never call a tool after beginning `Answer:` in the same reply" in navigation_prompt
    assert "AwaitUser:" in navigation_prompt
    assert "one concise, user-facing status summary" in navigation_prompt
    assert "one streamed, persistent assistant message" in navigation_prompt
    assert "it never creates a second final" in navigation_prompt
    assert "AwaitUser:" not in router_prompt


def test_navigation_prompt_distinguishes_new_stage_gate_from_existing_products():
    prompt = navigation_agent_prompt()

    assert "newly completed and verified in this task attempt" in prompt
    assert "mandatory stage gate" in prompt
    assert "If it explicitly authorizes later processing" in prompt
    assert "If it does not explicitly authorize later processing" in prompt
    assert "do not ask for continuation again" in prompt
    assert "ask only for `scene_mode` when it is missing" in prompt
    assert "the runtime owns the state transition" in prompt


def test_navigation_prompt_keeps_trajectory_review_in_its_narrow_phase():
    prompt = navigation_agent_prompt()

    assert "target is `trajectory_review`" in prompt
    assert "bound Annotation Job facts once" in prompt
    assert "submitting exactly one complete trajectory-review Plan" in prompt
    assert "Do not inspect raw metadata, topics" in prompt
    assert "do not submit it again" in prompt
    assert "execute the current `open_trajectory_fix_workbench` step" in prompt


def test_router_start_schema_omits_dry_run_and_model_restatements():
    schema = StartNavigationDataTaskV1Tool.input_schema

    assert "dry_run" not in schema["properties"]
    assert "dry_run" not in schema["required"]
    for removed in [
        "request",
        "target",
        "reason",
        "missing_fields",
        "confidence",
        "response_language",
    ]:
        assert removed not in schema["properties"]
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
    assert navigation_prompt.count("## Six bounded few-shots") == 1
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
