import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agentscope.message import ToolResultState

from vla_data_juicer_agents.navigation import agent_tools as agent_tools_module
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.agent_tools import (
    PlanBoundHumanDecisionTool,
    resolve_navigation_agent_tools,
    resolve_navigation_tool_surface,
)
from vla_data_juicer_agents.navigation.tool_groups import (
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_PLAN_AUTHORING,
)
from vla_data_juicer_agents.navigation.task_store import (
    NavigationStateResetRequired,
    SqliteNavigationTaskStore,
)
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools
from vla_data_juicer_agents.navigation.services import (
    NavigationServices,
    build_navigation_services,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
)
from vla_data_juicer_agents.navigation.plan_store import StepClaimOutcome
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from test_navigation_plan_submission_tools import (
    build_services as build_complete_plan_services,
    valid_extract_plan_payload,
    valid_finish_plan_payload,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationArtifactSnapshot,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.runtime import agentscope_runtime as runtime_module
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    NavigationHandoffTool,
    build_extra_agent_tools_factory,
    create_agentscope_runtime,
)


PLANNING_TOOL_NAMES = {
    "inspect_navigation_raw_metadata_tool",
    "inspect_navigation_sensor_candidates_tool",
    "inspect_navigation_topic_candidates_tool",
    "inspect_navigation_artifact_state_tool",
    "inspect_navigation_gridmap_artifacts_tool",
    "inspect_navigation_runtime_assets_tool",
    "inspect_navigation_calibration_inventory_tool",
    "inspect_navigation_localization_sources_tool",
    "get_navigation_task_context_tool",
    "list_observation_evidence_tool",
    "read_observation_evidence_tool",
    "describe_processing_action_tool",
    "record_navigation_user_guidance_tool",
    "submit_extract_sync_plan_tool",
    "submit_finish_processing_plan_tool",
}

EXECUTION_OBSERVATION_TOOL_NAMES = {
    "list_observation_evidence_tool",
    "read_observation_evidence_tool",
    "inspect_navigation_artifact_state_tool",
    "inspect_navigation_gridmap_artifacts_tool",
}


class FakeNavigationHandoffRuntime:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.records: list[dict] = []

    async def start_navigation_agent_task(self, *, web_session_id: str, message: str) -> str:
        self.started.append({"web_session_id": web_session_id, "message": message})
        return SimpleNamespace(
            task_id="nav-test-1",
            agentscope_session_id="navigation-session",
        )

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


def test_plan_bound_human_decision_tool_exposes_only_plan_and_step_ids():
    tool = PlanBoundHumanDecisionTool()

    assert tool.name == "request_human_decision"
    assert set(tool.input_schema["properties"]) == {"plan_id", "step_id"}
    assert tool.input_schema["required"] == ["plan_id", "step_id"]
    assert tool.input_schema["additionalProperties"] is False


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
    assert "dry_run" not in tool.input_schema["properties"]
    assert tool.input_schema["required"] == [
        "request",
        "target",
        "date",
        "reason",
        "missing_fields",
        "confidence",
        "response_language",
    ]
    assert tool.input_schema["properties"]["scene_mode"]["enum"] == ["indoor", "outdoor", "unknown"]
    assert tool.input_schema["properties"]["confidence"]["enum"] == ["low", "medium", "high"]
    assert tool.input_schema["properties"]["missing_fields"]["items"]["enum"] == [
        "request",
        "target",
        "date",
        "clips",
        "other",
    ]


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
    result_payload = json.loads(_text(result))
    assert result_payload == result.metadata
    assert result_payload == {
        "ok": True,
        "started": True,
        "task_id": "nav-test-1",
        "message": "导航数据任务已启动。",
    }
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


def test_navigation_handoff_tool_uses_explicit_date_not_clip_prefix_for_task_entry():
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


def test_navigation_handoff_message_includes_structured_json_for_task_entry():
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
        "segments": ["20260605_152856"],
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
            "ok": True,
            "started": True,
            "task_id": "nav-test-1",
        }
    ]


def test_navigation_handoff_tool_returns_authoritative_busy_json():
    class BusyRuntime(FakeNavigationHandoffRuntime):
        async def start_navigation_agent_task(self, **_kwargs):
            raise runtime_module.NavigationDataBusyError("busy")

    tool = NavigationHandoffTool(runtime=BusyRuntime(), web_session_id="web-1")

    result = asyncio.run(
        tool(
            request="处理 20270605 的导航数据",
            target="20270605",
            date="20270605",
            reason="用户请求处理",
            missing_fields=[],
            confidence="high",
            response_language="Chinese",
        )
    )

    assert result.state is ToolResultState.ERROR
    assert json.loads(_text(result)) == result.metadata == {
        "ok": False,
        "started": False,
        "error_type": "navigation_data_busy",
        "message": "该目标当前有正在运行的数据写入操作。",
    }


def test_navigation_handoff_tool_bounds_unknown_start_failure(caplog):
    class FailingRuntime(FakeNavigationHandoffRuntime):
        async def start_navigation_agent_task(self, **_kwargs):
            raise RuntimeError("secret internal failure")

    tool = NavigationHandoffTool(runtime=FailingRuntime(), web_session_id="web-1")

    result = asyncio.run(
        tool(
            request="处理 20270605 的导航数据",
            target="20270605",
            date="20270605",
            reason="用户请求处理",
            missing_fields=[],
            confidence="high",
            response_language="Chinese",
        )
    )

    payload = json.loads(_text(result))
    assert result.state is ToolResultState.ERROR
    assert payload == result.metadata
    assert payload["ok"] is False
    assert payload["started"] is False
    assert payload["error_type"] == "navigation_start_failed"
    assert "secret internal failure" not in _text(result)
    assert "correlation_id=" in caplog.text


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
    assert tool_names == set()


def _resolver_services_from_complete(
    tmp_path,
    session_id="as-session-1",
    phase="extract_sync",
):
    built = build_complete_plan_services(
        tmp_path,
        phase,
        web_session_id=session_id,
        agentscope_session_id=session_id,
    )
    data_root = tmp_path / "vla-data"
    (data_root / "raw_data" / built.task.date / built.task.segments[0]).mkdir(
        parents=True
    )
    task_store = SqliteNavigationTaskStore(built.plan_store.db_path)
    task = built.task
    services = NavigationServices(
        settings=NavigationSettings(vladatasets_root=data_root),
        task_store=task_store,
        observation_store=built.observation_store,
        evidence_store=built.evidence_store,
        plan_store=built.plan_store,
    )
    return services, task, built


def _surface(services, session_id):
    return resolve_navigation_tool_surface(
        services=services,
        agentscope_session_id=session_id,
        web_session_id=session_id,
        cancellation=None,
    )


def _group_tool_names(surface):
    return {
        group.name: {tool.name for tool in group.tools}
        for group in surface.groups
    }


def _terminalize_plan(services, plan, session_id):
    for step in plan.plan.steps:
        assert services.plan_store.claim_step(
            plan.plan_id,
            step.step_id,
            step.action,
            expected_web_session_id=session_id,
            expected_agentscope_session_id=session_id,
        ) is StepClaimOutcome.CLAIMED
        staged = services.plan_store.stage_step_result(
            plan.plan_id,
            step.step_id,
            expected_action=step.action,
            target_status="completed",
            full_result={"ok": True, "message": "completed"},
            result_summary={"ok": True, "message": "completed"},
            expected_web_session_id=session_id,
            expected_agentscope_session_id=session_id,
        )
        assert staged.result_ref is not None
        assert services.plan_store.finalize_staged_step(
            plan.plan_id,
            step.step_id,
            expected_action=step.action,
            expected_web_session_id=session_id,
            expected_agentscope_session_id=session_id,
        )


def test_grouped_surface_resolves_planning_catalog(tmp_path):
    services, _task, _built = _resolver_services_from_complete(tmp_path)

    planning = _surface(services, "as-session-1")

    assert planning is not None
    assert planning.activity == "planning"
    assert "describe_processing_action_tool" in {
        tool.name for tool in planning.group(NAVIGATION_PLAN_AUTHORING).tools
    }
    with pytest.raises(LookupError):
        planning.group(NAVIGATION_EXECUTION_ACTIONS)


def test_grouped_surface_resolves_execution_catalog(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )

    execution = _surface(services, "as-session-1")

    assert execution is not None
    assert execution.activity == "execution"
    names_by_group = _group_tool_names(execution)
    assert names_by_group[NAVIGATION_EXECUTION_STATE] == {
        "get_plan_execution_overview_tool",
        "get_current_plan_step_tool",
    }
    assert "prepare_raw_data_tool" in names_by_group[NAVIGATION_EXECUTION_ACTIONS]
    with pytest.raises(LookupError):
        execution.group(NAVIGATION_PLAN_AUTHORING)


def test_grouped_surface_hides_execution_actions_during_recovery(tmp_path):
    services, task, built = _resolver_services_from_complete(
        tmp_path,
        phase="finish_processing",
    )
    observation = services.observation_store.latest(task.task_id)
    assert observation is not None
    active = services.plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        FinishProcessingPlanInput.model_validate(valid_finish_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    first_step = active.plan.steps[0]
    assert services.plan_store.mark_waiting_user(
        active.plan_id,
        first_step.step_id,
        first_step.action,
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    assert services.plan_store.mark_human_decision_recovery_required(
        active.plan_id,
        first_step.step_id,
        reason_code="ambiguous_delivery_state",
        request_anchor={"plan_id": active.plan_id, "step_id": first_step.step_id},
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )

    recovery = _surface(services, "as-session-1")

    assert recovery is not None
    assert recovery.activity == "recovery_required"
    with pytest.raises(LookupError):
        recovery.group(NAVIGATION_EXECUTION_ACTIONS)
    with pytest.raises(LookupError):
        recovery.group(NAVIGATION_PLAN_AUTHORING)


def test_grouped_surface_reverse_transition_returns_completed_extract_to_planning(
    tmp_path,
):
    services, task, built = _resolver_services_from_complete(tmp_path)
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    _terminalize_plan(services, active, "as-session-1")

    planning = _surface(services, "as-session-1")

    assert planning is not None
    assert planning.activity == "planning"
    assert planning.group(NAVIGATION_ARTIFACT_CHECKS).tools
    assert planning.group(NAVIGATION_PLAN_AUTHORING).tools
    with pytest.raises(LookupError):
        planning.group(NAVIGATION_EXECUTION_STATE)
    with pytest.raises(LookupError):
        planning.group(NAVIGATION_EXECUTION_ACTIONS)


def test_grouped_surface_finish_plan_completion_has_no_execution_actions(tmp_path):
    services, task, built = _resolver_services_from_complete(
        tmp_path,
        phase="finish_processing",
    )
    observation = services.observation_store.latest(task.task_id)
    assert observation is not None
    active = services.plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        FinishProcessingPlanInput.model_validate(valid_finish_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    _terminalize_plan(services, active, "as-session-1")

    surface = _surface(services, "as-session-1")

    assert services.task_store.get_task(task.task_id).status == NavigationTaskStatus.COMPLETED
    assert surface is not None
    assert surface.activity == "planning"
    with pytest.raises(LookupError):
        surface.group(NAVIGATION_EXECUTION_ACTIONS)


def test_flat_adapter_matches_grouped_surface_order(tmp_path):
    services, _task, _built = _resolver_services_from_complete(tmp_path)
    surface = _surface(services, "as-session-1")
    flat = resolve_navigation_agent_tools(
        services=services,
        agentscope_session_id="as-session-1",
        web_session_id="as-session-1",
        cancellation=None,
    )

    assert surface is not None
    assert [tool.name for tool in flat] == [
        tool.name for tool in surface.flatten_active_tools()
    ]


def test_navigation_services_share_one_database_and_idempotent_migrations(tmp_path):
    first = build_navigation_services(tmp_path)
    second = build_navigation_services(tmp_path)

    assert first.task_store.db_path == tmp_path / "navigation-tasks.sqlite"
    assert first.observation_store.db_path == first.task_store.db_path
    assert first.plan_store.db_path == first.task_store.db_path
    assert second.task_store.db_path == first.task_store.db_path
    assert first.evidence_store.root == tmp_path / "navigation-evidence"


def test_activity_resolver_uses_exact_session_and_exposes_both_submit_schemas(tmp_path):
    services, _task, _built = _resolver_services_from_complete(tmp_path)

    planning_names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }
    other_session_names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-2",
            cancellation=None,
        )
    }

    assert planning_names == PLANNING_TOOL_NAMES
    assert other_session_names == set()
    assert not any("draft" in name or "finalize" in name for name in planning_names)


def test_no_task_entry_requires_verified_web_agentscope_session_pair(tmp_path):
    services = build_navigation_services(tmp_path)

    tools = resolve_navigation_agent_tools(
        services=services,
        agentscope_session_id="web-a__navigation-data-agent",
        web_session_id="web-b",
        cancellation=None,
    )

    assert tools == []


def test_direct_attempt_resolver_uses_durable_exact_pair_not_id_shape(tmp_path):
    services = build_navigation_services(tmp_path)
    services.task_store.create_task_attempt(
        request="Direct navigation workflow",
        target="20260710",
        date="20260710",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id="direct:run-1",
        agentscope_session_id="direct__run-1",
    )

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            web_session_id="direct:run-1",
            agentscope_session_id="direct__run-1",
            cancellation=None,
        )
    }

    assert any(name.startswith("inspect_navigation_") for name in names)
    assert "submit_extract_sync_plan_tool" in names
    assert "submit_finish_processing_plan_tool" in names


def test_cross_web_session_without_bound_attempt_exposes_no_task_mutation(tmp_path):
    services = build_navigation_services(tmp_path)
    existing = services.task_store.create_task_attempt(
        request="Process navigation data",
        target="20260710",
        date="20260710",
        segments=None,
        scene_mode=None,
        dry_run=False,
        web_session_id="web-a",
        agentscope_session_id="web-a__navigation-data-agent",
    ).task
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="web-b__navigation-data-agent",
            web_session_id="web-b",
            cancellation=None,
        )
    }

    assert tools == {}
    assert services.task_store.get_task(existing.task_id) == existing


def test_navigation_services_initialize_phase_neutral_observation_schema_repeatedly(tmp_path):
    from vla_data_juicer_agents.navigation import services as services_module

    first = build_navigation_services(tmp_path)
    second = build_navigation_services(tmp_path)

    assert first.observation_store.db_path == second.observation_store.db_path
    with sqlite3.connect(first.observation_store.db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(navigation_observation_revisions)"
            )
        }
        migration_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='navigation_service_migrations'"
        ).fetchone()
    assert columns == {"task_id", "revision", "revision_json", "created_at"}
    assert migration_table is None
    assert not hasattr(services_module, "_migrate_legacy_observations")


def test_phase_bearing_observation_schema_requires_reset_without_copying_legacy_state(
    tmp_path,
):
    services = build_navigation_services(tmp_path)
    target_path = services.task_store.db_path
    with sqlite3.connect(target_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE navigation_evidence")
        connection.execute("DROP TABLE navigation_observation_revisions")
        connection.execute(
            """CREATE TABLE navigation_observation_revisions (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                phase TEXT NOT NULL,
                revision_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, revision)
            )"""
        )
        connection.execute(
            """CREATE TABLE navigation_evidence (
                ref TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                observation_revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                source_tool TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id, observation_revision)
                    REFERENCES navigation_observation_revisions(task_id, revision)
            )"""
        )
        connection.execute(
            """CREATE INDEX idx_navigation_evidence_task_revision_kind
               ON navigation_evidence (task_id, observation_revision, kind)"""
        )
        connection.execute(
            "INSERT INTO navigation_observation_revisions VALUES (?, ?, ?, ?, ?)",
            ("legacy-task", 1, "extract_sync", "{}", "now"),
        )
    legacy_path = tmp_path / "navigation-observations.sqlite"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("CREATE TABLE legacy_sentinel (value TEXT)")
        connection.execute("INSERT INTO legacy_sentinel VALUES ('do-not-copy')")
    target_before = target_path.read_bytes()
    legacy_before = legacy_path.read_bytes()

    with pytest.raises(NavigationStateResetRequired):
        build_navigation_services(tmp_path)

    assert target_path.read_bytes() == target_before
    assert legacy_path.read_bytes() == legacy_before


def test_activity_resolver_exposes_all_planning_tools_before_facts_are_complete(tmp_path):
    data_root = tmp_path / "vla-data"
    (data_root / "raw_data" / "20260710" / "20260710_120000").mkdir(parents=True)
    services = build_navigation_services(
        tmp_path,
        NavigationSettings(vladatasets_root=data_root),
    )
    created = services.task_store.create_task_attempt(
        request="Process navigation data",
        target="20260710",
        date="20260710",
        segments=["20260710_120000"],
        scene_mode=None,
        dry_run=False,
        web_session_id="as-session-1",
        agentscope_session_id="as-session-1",
    ).task

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert names == PLANNING_TOOL_NAMES
    assert not any(name in {"prepare_raw_data_tool", "extract_and_sync_navigation_data_tool"} for name in names)


def test_activity_resolver_recovers_active_plan_and_only_remaining_actions(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built))
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        plan,
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    remaining = {f"{step.action}_tool" for step in active.plan.steps}
    assert names == {
        "get_plan_execution_overview_tool",
        "get_current_plan_step_tool",
        *EXECUTION_OBSERVATION_TOOL_NAMES,
        *remaining,
    }
    assert not any(name.startswith("submit_") for name in names)
    assert "request_human_decision" not in names


def test_resolver_and_execution_builder_consume_one_repository_snapshot(
    tmp_path,
    monkeypatch,
):
    services, task, built = _resolver_services_from_complete(tmp_path)
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    overview = services.plan_store.get_execution_overview(active.plan_id)
    current = services.plan_store.get_current_step(active.plan_id)
    snapshot = SimpleNamespace(
        task=services.task_store.get_task(task.task_id),
        active_plan=active,
        overview=overview,
        current=current,
        handoff=None,
        staged_result=None,
        dependency_statuses={},
        activity="execution",
    )
    monkeypatch.setattr(
        services.plan_store,
        "read_execution_snapshot",
        lambda **_kwargs: snapshot,
        raising=False,
    )
    monkeypatch.setattr(
        services.task_store,
        "find_by_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolver must not split the snapshot read")
        ),
    )
    monkeypatch.setattr(
        services.plan_store,
        "get_active_for_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("builder must consume the resolver snapshot")
        ),
    )

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert names == {
        "get_plan_execution_overview_tool",
        "get_current_plan_step_tool",
        "prepare_raw_data_tool",
        "extract_and_sync_navigation_data_tool",
        *EXECUTION_OBSERVATION_TOOL_NAMES,
    }


@pytest.mark.parametrize(
    "mutation",
    ["superseded", "session_mismatch", "accepted_phase_changed"],
)
def test_stale_execution_read_tools_reauthorize_at_call_time(tmp_path, mutation):
    services, task, built = _resolver_services_from_complete(tmp_path)
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }
    if mutation == "superseded":
        services.plan_store.activate(
            task,
            "extract_sync",
            5,
            ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
            expected_web_session_id="as-session-1",
            expected_agentscope_session_id="as-session-1",
        )
    elif mutation == "session_mismatch":
        with sqlite3.connect(services.plan_store.db_path) as connection:
            connection.execute(
                """UPDATE navigation_tasks
                   SET created_by_web_session_id = ?, agentscope_session_id = ?
                   WHERE task_id = ?""",
                ("other-web", "other-agent", task.task_id),
            )
    else:
        with sqlite3.connect(services.plan_store.db_path) as connection:
            connection.execute(
                """UPDATE navigation_tasks SET accepted_plan_phase = 'finish_processing'
                   WHERE task_id = ?""",
                (task.task_id,),
            )

    overview = _decode_tool_payload(
        asyncio.run(tools["get_plan_execution_overview_tool"](plan_id=active.plan_id))
    )
    current = _decode_tool_payload(
        asyncio.run(tools["get_current_plan_step_tool"](plan_id=active.plan_id))
    )

    assert overview == {"ok": False, "error_type": "inactive_navigation_plan"}
    assert current == {"ok": False, "error_type": "inactive_navigation_plan"}


def test_new_attempt_for_same_session_revokes_all_tools_from_previous_attempt(
    tmp_path,
    monkeypatch,
):
    from vla_data_juicer_agents.navigation import plan_execution

    services, task_a, built = _resolver_services_from_complete(tmp_path)
    plan_a = services.plan_store.activate(
        task_a,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        lambda **kwargs: invoked.append(kwargs)
        or {
            "ok": True,
            "tool_name": "prepare_raw_data",
            "message": "must not run",
        },
    )
    tools_a = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }
    task_a_before = services.task_store.get_task(task_a.task_id)
    plan_a_before = services.plan_store.get(plan_a.plan_id)
    ledger_a_before = services.plan_store.get_execution_overview(plan_a.plan_id)

    task_b = services.task_store.create_task_attempt(
        request="process a different navigation target",
        target="20260711/20260711_120000",
        date="20260711",
        segments=["20260711_120000"],
        scene_mode=None,
        dry_run=True,
        web_session_id="as-session-1",
        agentscope_session_id="as-session-1",
    ).task
    task_b_before = services.task_store.get_task(task_b.task_id)
    resolver_b_names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    overview = _decode_tool_payload(
        asyncio.run(
            tools_a["get_plan_execution_overview_tool"](plan_id=plan_a.plan_id)
        )
    )
    current = _decode_tool_payload(
        asyncio.run(tools_a["get_current_plan_step_tool"](plan_id=plan_a.plan_id))
    )
    processing = _decode_tool_payload(
        asyncio.run(
            tools_a["prepare_raw_data_tool"](
                plan_id=plan_a.plan_id,
                step_id="prepare_raw",
            )
        )
    )

    assert resolver_b_names == PLANNING_TOOL_NAMES
    assert overview == {"ok": False, "error_type": "inactive_navigation_plan"}
    assert current == {"ok": False, "error_type": "inactive_navigation_plan"}
    assert processing["ok"] is False
    assert processing["error_type"] == "navigation_task_session_mismatch"
    assert invoked == []
    assert services.task_store.get_task(task_a.task_id) == task_a_before
    assert services.plan_store.get(plan_a.plan_id) == plan_a_before
    assert services.plan_store.get_execution_overview(plan_a.plan_id) == ledger_a_before
    assert services.task_store.get_task(task_b.task_id) == task_b_before
    assert services.plan_store.get_active_for_task(task_b.task_id) is None


def test_new_attempt_fences_every_mutating_tool_from_captured_planning_toolkit(
    tmp_path,
):
    services, task_a, built = _resolver_services_from_complete(tmp_path)
    tools_a = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }
    context = _decode_tool_payload(
        asyncio.run(tools_a["get_navigation_task_context_tool"]())
    )
    task_b = services.task_store.create_task_attempt(
        request="process a different navigation target",
        target="20260711/20260711_120000",
        date="20260711",
        segments=["20260711_120000"],
        scene_mode=None,
        dry_run=True,
        web_session_id="as-session-1",
        agentscope_session_id="as-session-1",
    ).task

    def durable_rows():
        with sqlite3.connect(services.task_store.db_path) as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name LIKE 'navigation_%' "
                    "ORDER BY name"
                )
            ]
            return {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            }

    def evidence_files():
        return {
            str(path.relative_to(services.evidence_store.root)): path.read_bytes()
            for path in services.evidence_store.root.rglob("*")
            if path.is_file()
        }

    durable_before = durable_rows()
    evidence_before = evidence_files()

    extract = _decode_tool_payload(
        asyncio.run(
            tools_a["submit_extract_sync_plan_tool"](
                planning_context_revision=context["planning_context_revision"],
                plan=valid_extract_plan_payload(built),
            )
        )
    )
    finish = _decode_tool_payload(
        asyncio.run(
            tools_a["submit_finish_processing_plan_tool"](
                planning_context_revision=context["planning_context_revision"],
                plan={},
            )
        )
    )
    inspection = _decode_tool_payload(
        asyncio.run(tools_a["inspect_navigation_artifact_state_tool"]())
    )
    guidance = _decode_tool_payload(
        asyncio.run(
            tools_a["record_navigation_user_guidance_tool"](
                text="stale guidance must not be recorded",
                scene_mode="out",
            )
        )
    )

    assert extract["ok"] is False
    assert extract["error_type"] == "submission_audit_failed"
    assert finish["ok"] is False
    assert finish["error_type"] == "submission_audit_failed"
    assert inspection["ok"] is False
    assert inspection["error_type"] == "permission_error"
    assert guidance["ok"] is False
    assert guidance["error_type"] == "navigation_task_session_mismatch"
    assert durable_rows() == durable_before
    assert evidence_files() == evidence_before
    assert services.task_store.find_by_session(
        web_session_id="as-session-1",
        agentscope_session_id="as-session-1",
    ).task_id == task_b.task_id
    assert services.plan_store.get_active_for_task(task_a.task_id) is None


def test_exact_attempt_retry_keeps_the_same_current_task_authorized(tmp_path):
    services = build_navigation_services(tmp_path)
    arguments = {
        "request": "process navigation target",
        "target": "20260711/20260711_120000",
        "date": "20260711",
        "segments": ["20260711_120000"],
        "scene_mode": None,
        "dry_run": True,
        "web_session_id": "web-owner",
        "agentscope_session_id": "web-owner__agent",
    }
    first = services.task_store.create_task_attempt(**arguments)
    retry = services.task_store.create_task_attempt(**arguments)

    snapshot = services.plan_store.read_execution_snapshot(
        web_session_id="web-owner",
        agentscope_session_id="web-owner__agent",
        task_id=first.task.task_id,
    )

    assert first.created is True
    assert retry.created is False
    assert retry.task.task_id == first.task.task_id
    assert snapshot is not None
    assert snapshot.task.task_id == first.task.task_id


def test_activity_resolver_returns_failed_active_ledger_to_planning(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    first_step = active.plan.steps[0]
    with sqlite3.connect(services.plan_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_task_steps SET status = 'failed'
               WHERE plan_id = ? AND step_id = ?""",
            (active.plan_id, first_step.step_id),
        )

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert names == PLANNING_TOOL_NAMES


def test_runtime_anchor_does_not_reconcile_or_append_observation(tmp_path, monkeypatch):
    services, task, _built = _resolver_services_from_complete(tmp_path)
    with sqlite3.connect(services.task_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_tasks
               SET created_by_web_session_id = ?, agentscope_session_id = ?
               WHERE task_id = ?""",
            ("web-owner", "web-owner__as", task.task_id),
        )
    bound = services.task_store.get_task(task.task_id)
    assert bound is not None
    before = services.observation_store.latest(task.task_id)
    runtime = object.__new__(runtime_module.AgentScopeRuntime)
    runtime.config = SimpleNamespace(workspace_root=tmp_path)
    runtime._navigation_services = lambda: services

    anchor = runtime._navigation_durable_state_anchor(
        "web-owner__as", web_session_id="web-owner"
    )

    assert anchor["task_attempt_id"] == task.task_id
    assert services.observation_store.latest(task.task_id) == before


def test_runtime_anchor_exposes_accepted_plan_without_phase_routing(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    with sqlite3.connect(services.task_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_tasks
               SET created_by_web_session_id = ?, agentscope_session_id = ?
               WHERE task_id = ?""",
            ("web-owner", "web-owner__as", task.task_id),
        )
    runtime = object.__new__(runtime_module.AgentScopeRuntime)
    runtime.config = SimpleNamespace(workspace_root=tmp_path)
    runtime._navigation_services = lambda: services

    anchor = runtime._navigation_durable_state_anchor(
        "web-owner__as",
        web_session_id="web-owner",
    )

    assert anchor["accepted_plan_id"] == active.plan_id
    assert anchor["accepted_plan_revision"] == active.plan_revision
    assert "phase" not in anchor


def test_task_status_completed_without_active_plan_remains_in_planning_activity(tmp_path):
    services, task, _built = _resolver_services_from_complete(tmp_path)
    final_grid = (
        services.settings.finish_data_root
        / task.date
        / task.segments[0]
        / "projection"
        / "grid_map"
    )
    final_grid.mkdir(parents=True)
    services.task_store.update_task_for_session(
        task.task_id, web_session_id="as-session-1", agentscope_session_id="as-session-1",
        status="completed",
    )

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert names == PLANNING_TOOL_NAMES


def test_planning_evidence_list_is_explicitly_bounded_to_5500_chars(monkeypatch, tmp_path):
    services, task, _built = _resolver_services_from_complete(tmp_path)
    final_grid = (
        services.settings.finish_data_root
        / task.date
        / task.segments[0]
        / "projection"
        / "grid_map"
    )
    final_grid.mkdir(parents=True)
    services.task_store.update_task_for_session(
        task.task_id, web_session_id="as-session-1", agentscope_session_id="as-session-1",
        status="completed",
    )
    rows = [
        SimpleNamespace(
            model_dump=lambda index=index, **_: {
                "ref": f"evidence:{index}",
                "summary": "x" * 450,
            }
        )
        for index in range(20)
    ]
    monkeypatch.setattr(services.observation_store, "list_evidence", lambda *_, **__: rows)
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
                web_session_id="as-session-1",
            cancellation=None,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["list_observation_evidence_tool"](limit=20))
    )

    assert len(json.dumps(result, separators=(",", ":"))) <= 5500


def test_resolved_execution_reads_are_explicitly_bounded_to_4000_chars(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    plan = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    overview = _decode_tool_payload(
        asyncio.run(tools["get_plan_execution_overview_tool"](plan_id=plan.plan_id))
    )
    current = _decode_tool_payload(
        asyncio.run(tools["get_current_plan_step_tool"](plan_id=plan.plan_id))
    )

    assert len(json.dumps(overview, separators=(",", ":"))) <= 4000
    assert len(json.dumps(current, separators=(",", ":"))) <= 4000


def test_activity_resolver_fresh_attempt_exposes_all_planning_tools_without_mutation(
    tmp_path,
):
    services = build_navigation_services(tmp_path)
    task = services.task_store.create_task_attempt(
        request="process current navigation data",
        target="20260710/20260710_120000",
        date="20260710",
        segments=["20260710_120000"],
        scene_mode=None,
        dry_run=False,
        web_session_id="web-owner",
        agentscope_session_id="web-owner__navigation-data-agent",
    ).task
    before = services.task_store.get_task(task.task_id)

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="web-owner__navigation-data-agent",
            web_session_id="web-owner",
            cancellation=None,
        )
    }

    assert names == {
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "get_navigation_task_context_tool",
        "list_observation_evidence_tool",
        "read_observation_evidence_tool",
        "describe_processing_action_tool",
        "record_navigation_user_guidance_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    }
    assert services.task_store.get_task(task.task_id) == before
    assert services.observation_store.latest(task.task_id) is None


def test_activity_resolver_without_bound_attempt_fails_closed(tmp_path):
    services = build_navigation_services(tmp_path)

    tools = resolve_navigation_agent_tools(
        services=services,
        agentscope_session_id="web-owner__navigation-data-agent",
        web_session_id="web-owner",
        cancellation=None,
    )

    assert tools == []


def test_completed_extract_plan_returns_to_stage_neutral_planning_tools(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    active = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="as-session-1",
        expected_agentscope_session_id="as-session-1",
    )
    with sqlite3.connect(services.plan_store.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' WHERE plan_id = ?",
            (active.plan_id,),
        )
        connection.execute(
            "UPDATE navigation_plans SET status = 'completed' WHERE plan_id = ?",
            (active.plan_id,),
        )

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert "submit_extract_sync_plan_tool" in names
    assert "submit_finish_processing_plan_tool" in names
    assert "inspect_navigation_artifact_state_tool" in names
    assert "get_plan_execution_overview_tool" not in names
    assert "get_current_plan_step_tool" not in names
