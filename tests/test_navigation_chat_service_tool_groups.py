from __future__ import annotations

import pytest
from agentscope.app._service._chat import ChatService
from agentscope.app.storage import ChatModelConfig, SessionConfig
from agentscope.message import UserMsg

import agentscope.app._service._chat as chat_service_module
import vla_data_juicer_agents.navigation.plan_execution as plan_execution
from navigation_agentscope_harness import (
    ScriptedChatModel,
    latest_tool_result_json,
    runtime_config,
    schema_names,
)
from navigation_chat_service_harness import (
    ChatServiceBus,
    ChatServiceStorage,
    ChatServiceWorkspaceManager,
    InertManager,
    generic_toolkit,
)
from vla_data_juicer_agents.runtime.agentscope_bootstrap import (
    bootstrap_agentscope_records,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import ToolResult
from vla_data_juicer_agents.navigation.services import NavigationServices
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    AgentScopeRuntime,
    build_extra_agent_middlewares_factory,
)
from vla_data_juicer_agents.navigation.plan_models import ExtractSyncPlanInput
from vla_data_juicer_agents.navigation.tool_groups import (
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_DIAGNOSTICS,
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_PLAN_AUTHORING,
)
from test_navigation_plan_submission_tools import (
    build_services as build_complete_plan_services,
    valid_extract_plan_payload,
)
from test_navigation_context_budget import _write_raw_metadata
from test_navigation_model_authored_flow import _write_finish_inputs


def _evidence_by_kind(services, task_id: str) -> dict[str, str]:
    return {
        descriptor.kind: descriptor.ref
        for descriptor in services.observation_store.list_evidence(task_id, limit=50)
    }


def _minimal_finish_plan(evidence: dict[str, str]) -> dict:
    return {
        "decisions": {
            "localization": {
                "source": "odom",
                "conversion": "odom_to_ins",
                "reason": "Use the observed odometry converter.",
                "evidence_refs": [evidence["localization_sources"]],
            },
            "gridmap": {
                "source": "existing_gridmap",
                "reason": "Use the observed synchronized gridmap.",
                "evidence_refs": [evidence["gridmap_artifacts"]],
            },
            "calibration": {
                "mode": "selected_profile",
                "selected_sensor_source": "NoobScenes/params/selected/sensors",
                "requires_user_confirmation": False,
                "reason": "Use the already selected observed profile.",
                "evidence_refs": [evidence["calibration_inventory"]],
            },
        },
        "steps": [
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


async def _real_chat_service_case(tmp_path, *, web_session_id: str):
    config = runtime_config(tmp_path)
    session_id = f"{web_session_id}__{config.navigation_agent_id}"
    built = build_complete_plan_services(
        tmp_path,
        "extract_sync",
        web_session_id=web_session_id,
        agentscope_session_id=session_id,
    )
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    for segment in built.task.segments or []:
        _write_raw_metadata(settings.vladatasets_root, built.task.date, segment)
    services = NavigationServices(
        settings=settings,
        task_store=SqliteNavigationTaskStore(built.plan_store.db_path),
        observation_store=built.observation_store,
        evidence_store=built.evidence_store,
        plan_store=built.plan_store,
    )
    storage = ChatServiceStorage()
    await bootstrap_agentscope_records(storage, config)
    await storage.upsert_session(
        config.user_id,
        config.navigation_agent_id,
        SessionConfig(
            workspace_id=f"workspace-{web_session_id}",
            chat_model_config=ChatModelConfig(
                type="scripted",
                credential_id=config.credential_id,
                model="scripted",
                parameters={},
            ),
        ),
        session_id=session_id,
    )
    bus = ChatServiceBus()
    workspace_manager = ChatServiceWorkspaceManager()
    runtime = AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=bus,
        workspace_manager=workspace_manager,
        app=None,
    )
    runtime._navigation_services = lambda: services
    service = ChatService(
        storage=storage,
        workspace_manager=workspace_manager,
        scheduler_manager=InertManager(),
        background_task_manager=InertManager(),
        message_bus=bus,
        extra_agent_middlewares=build_extra_agent_middlewares_factory(
            config,
            runtime=runtime,
        ),
    )
    return config, session_id, built, services, storage, bus, service


def _patch_assembly(monkeypatch, model):
    async def async_get_scripted_model(*_args, **_kwargs):
        return model

    async def async_get_generic_toolkit(**_kwargs):
        return generic_toolkit()

    monkeypatch.setattr(chat_service_module, "get_model", async_get_scripted_model)
    monkeypatch.setattr(chat_service_module, "get_toolkit", async_get_generic_toolkit)


@pytest.mark.asyncio
async def test_real_chat_service_smoke(monkeypatch, tmp_path):
    config = runtime_config(tmp_path)
    storage = ChatServiceStorage()
    await bootstrap_agentscope_records(storage, config)
    session_id = f"web-smoke__{config.navigation_agent_id}"
    await storage.upsert_session(
        config.user_id,
        config.navigation_agent_id,
        SessionConfig(
            workspace_id="workspace-smoke",
            chat_model_config=ChatModelConfig(
                type="scripted",
                credential_id=config.credential_id,
                model="scripted",
                parameters={},
            ),
        ),
        session_id=session_id,
    )
    bus = ChatServiceBus()
    workspace_manager = ChatServiceWorkspaceManager()
    runtime = AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=bus,
        workspace_manager=workspace_manager,
        app=None,
    )
    runtime._navigation_services().task_store.create_task_attempt(
        request="处理数据",
        target="20260710",
        date="20260710",
        segments=["20260710_120000"],
        scene_mode="out",
        dry_run=True,
        web_session_id="web-smoke",
        agentscope_session_id=session_id,
    )
    model = ScriptedChatModel()
    model.enqueue_text("收到。")

    async def async_get_scripted_model(*_args, **_kwargs):
        return model

    async def async_get_generic_toolkit(**_kwargs):
        return generic_toolkit()

    monkeypatch.setattr(chat_service_module, "get_model", async_get_scripted_model)
    monkeypatch.setattr(chat_service_module, "get_toolkit", async_get_generic_toolkit)
    service = ChatService(
        storage=storage,
        workspace_manager=workspace_manager,
        scheduler_manager=InertManager(),
        background_task_manager=InertManager(),
        message_bus=bus,
        extra_agent_middlewares=build_extra_agent_middlewares_factory(
            config,
            runtime=runtime,
        ),
    )

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="处理数据"),
    )

    assert storage.updated_state is not None
    assert bus.events
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_plan_acceptance_switches_to_execution_within_same_reply(
    monkeypatch,
    tmp_path,
):
    (
        config,
        session_id,
        built,
        services,
        _storage,
        _bus,
        service,
    ) = await _real_chat_service_case(tmp_path, web_session_id="web-plan")
    model = ScriptedChatModel()
    model.enqueue_tool(
        "submit_extract_sync_plan_tool",
        {
            "planning_context_revision": built.planning_context_revision,
            "plan": valid_extract_plan_payload(built),
        },
    )
    model.enqueue_tool(
        "get_current_plan_step_tool",
        lambda messages: {"plan_id": latest_tool_result_json(messages)["plan_id"]},
    )
    model.enqueue_tool(
        "prepare_raw_data_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
            "step_id": latest_tool_result_json(messages)["step"]["step_id"],
        },
    )
    model.enqueue_text("第一步执行完成。")
    processing_calls = []

    def prepare_raw_data_spy(**_kwargs):
        processing_calls.append(len(model.invocations))
        return ToolResult(
            ok=True,
            tool_name="prepare_raw_data",
            message="completed",
            details={},
        )

    monkeypatch.setattr(plan_execution, "prepare_raw_data", prepare_raw_data_spy)
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="按完整计划开始处理数据"),
    )

    planning_names = schema_names(model.invocations[0].tools)
    execution_names = schema_names(model.invocations[1].tools)
    assert {
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    } <= planning_names
    assert "prepare_raw_data_tool" not in planning_names
    assert {"get_current_plan_step_tool", "prepare_raw_data_tool"} <= execution_names
    assert "describe_processing_action_tool" not in execution_names
    assert "submit_extract_sync_plan_tool" not in execution_names
    assert "reset_tools" not in planning_names | execution_names
    assert processing_calls == [3]
    assert services.plan_store.get_active_for_task(built.task.task_id) is not None
    assert model.compact_event_count == 0
    assert model.assert_exhausted() is None


@pytest.mark.asyncio
async def test_failed_plan_submission_keeps_planning_surface_in_same_reply(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, _services, _storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-stale-submit")
    )
    model = ScriptedChatModel()
    model.enqueue_tool(
        "submit_extract_sync_plan_tool",
        {
            "planning_context_revision": "stale-planning-context-revision",
            "plan": valid_extract_plan_payload(built),
        },
    )
    model.enqueue_text("计划版本已过期，请重新检查。")
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="提交这个计划"),
    )

    next_names = schema_names(model.invocations[1].tools)
    assert {
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    } <= next_names
    assert "prepare_raw_data_tool" not in next_names
    assert "reset_tools" not in next_names
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_restart_rebuilds_planning_surface_over_stale_execution_cache(
    monkeypatch,
    tmp_path,
):
    config, session_id, _built, _services, storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-cache-planning")
    )
    state = storage.sessions[
        (config.user_id, config.navigation_agent_id, session_id)
    ].state
    state.tool_context.activated_groups[:] = [
        NAVIGATION_EXECUTION_STATE,
        NAVIGATION_EXECUTION_ACTIONS,
    ]
    model = ScriptedChatModel()
    model.enqueue_text("当前需要先规划。")
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="继续"),
    )

    names = schema_names(model.invocations[0].tools)
    assert "submit_extract_sync_plan_tool" in names
    assert "prepare_raw_data_tool" not in names
    assert {"bash", "read", "task", "reset_tools"}.isdisjoint(names)
    assert storage.updated_state.tool_context.activated_groups == [
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_INVESTIGATION,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_PLAN_AUTHORING,
        NAVIGATION_DIAGNOSTICS,
    ]


@pytest.mark.asyncio
async def test_restart_rebuilds_execution_surface_over_stale_planning_cache(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-cache-execution")
    )
    observation = services.observation_store.latest(built.task.task_id)
    assert observation is not None
    services.plan_store.activate(
        built.task,
        "extract_sync",
        observation.revision,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="web-cache-execution",
        expected_agentscope_session_id=session_id,
    )
    state = storage.sessions[
        (config.user_id, config.navigation_agent_id, session_id)
    ].state
    state.tool_context.activated_groups[:] = [
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_PLAN_AUTHORING,
    ]
    model = ScriptedChatModel()
    model.enqueue_text("当前继续执行。")
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="继续"),
    )

    names = schema_names(model.invocations[0].tools)
    assert {"get_current_plan_step_tool", "prepare_raw_data_tool"} <= names
    assert "submit_extract_sync_plan_tool" not in names
    assert {"bash", "read", "task", "reset_tools"}.isdisjoint(names)
    assert storage.updated_state.tool_context.activated_groups == [
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_EXECUTION_STATE,
        NAVIGATION_EXECUTION_ACTIONS,
        NAVIGATION_DIAGNOSTICS,
    ]


@pytest.mark.asyncio
async def test_final_extract_action_switches_back_to_planning_in_same_reply(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, _storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-reverse")
    )
    payload = valid_extract_plan_payload(built)
    payload["steps"] = [payload["steps"][0]]
    observation = services.observation_store.latest(built.task.task_id)
    assert observation is not None
    plan = services.plan_store.activate(
        built.task,
        "extract_sync",
        observation.revision,
        ExtractSyncPlanInput.model_validate(payload),
        expected_web_session_id="web-reverse",
        expected_agentscope_session_id=session_id,
    )
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_tool("inspect_navigation_artifact_state_tool", {})
    model.enqueue_text("extract 阶段已完成；是否继续规划 finish？")
    processing_calls = []

    def safe_prepare_raw(**_kwargs):
        processing_calls.append(len(model.invocations))
        return ToolResult(
            ok=True,
            tool_name="prepare_raw_data",
            message="completed",
            details={},
        )

    monkeypatch.setattr(plan_execution, "prepare_raw_data", safe_prepare_raw)
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="执行 extract 最后一步"),
    )

    after_final_names = schema_names(model.invocations[1].tools)
    assert {
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_raw_metadata_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    } <= after_final_names
    assert "get_current_plan_step_tool" not in after_final_names
    assert "prepare_raw_data_tool" not in after_final_names
    assert "reset_tools" not in after_final_names
    assert processing_calls == [1]
    assert services.plan_store.get(plan.plan_id).status == "completed"
    task = services.task_store.get_task(built.task.task_id)
    assert task is not None
    assert task.status == NavigationTaskStatus.ACTIVE.value
    assert services.plan_store.get_active_for_task(built.task.task_id) is None
    assert model.compact_event_count == 0
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_later_same_session_finish_plan_executes_and_closes_task(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, _storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-two-stage")
    )
    extract_payload = valid_extract_plan_payload(built)
    extract_payload["steps"] = [extract_payload["steps"][0]]
    observation = services.observation_store.latest(built.task.task_id)
    assert observation is not None
    extract_plan = services.plan_store.activate(
        built.task,
        "extract_sync",
        observation.revision,
        ExtractSyncPlanInput.model_validate(extract_payload),
        expected_web_session_id="web-two-stage",
        expected_agentscope_session_id=session_id,
    )
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": extract_plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_tool("inspect_navigation_artifact_state_tool", {})
    model.enqueue_text("extract 已完成；请确认是否继续 finish。")
    processing_calls: list[tuple[str, int]] = []

    def safe_prepare_raw(**_kwargs):
        processing_calls.append(("prepare_raw_data", len(model.invocations)))
        return ToolResult(
            ok=True,
            tool_name="prepare_raw_data",
            message="completed",
            details={},
        )

    def safe_validate_outputs(**_kwargs):
        processing_calls.append(("validate_navigation_outputs", len(model.invocations)))
        return ToolResult(
            ok=True,
            tool_name="validate_navigation_outputs",
            message="completed",
            details={},
        )

    monkeypatch.setattr(plan_execution, "prepare_raw_data", safe_prepare_raw)
    monkeypatch.setattr(
        plan_execution,
        "validate_navigation_outputs",
        safe_validate_outputs,
    )
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="执行 extract 最后一步"),
    )
    assert services.plan_store.get(extract_plan.plan_id).status == "completed"
    assert services.task_store.get_task(built.task.task_id).status == (
        NavigationTaskStatus.ACTIVE.value
    )

    _write_finish_inputs(services.settings)
    second_run_start = len(model.invocations)
    model.enqueue_tool(
        "record_navigation_user_guidance_tool",
        {"text": "继续室外 finish processing。", "scene_mode": "out"},
    )
    for name in (
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
    ):
        model.enqueue_tool(name, {})
    model.enqueue_tool("get_navigation_task_context_tool", {})
    model.enqueue_tool(
        "submit_finish_processing_plan_tool",
        lambda messages: {
            "planning_context_revision": latest_tool_result_json(messages)[
                "planning_context_revision"
            ],
            "plan": _minimal_finish_plan(
                _evidence_by_kind(services, built.task.task_id)
            ),
        },
    )
    model.enqueue_tool(
        "validate_navigation_outputs_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
            "step_id": "validate_outputs",
        },
    )
    model.enqueue_text("finish validation 完成，任务已闭合。")

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="继续，室外场景，完成 finish processing。"),
    )

    assert second_run_start == 3
    assert "submit_finish_processing_plan_tool" in schema_names(
        model.invocations[second_run_start].tools
    )
    finish_submission_index = second_run_start + 7
    finish_execution_index = finish_submission_index + 1
    after_finish_index = finish_execution_index + 1
    finish_execution_names = schema_names(
        model.invocations[finish_execution_index].tools
    )
    assert {
        "get_current_plan_step_tool",
        "validate_navigation_outputs_tool",
    } <= finish_execution_names
    assert "submit_finish_processing_plan_tool" not in finish_execution_names
    after_finish_names = schema_names(model.invocations[after_finish_index].tools)
    assert {
        "inspect_navigation_artifact_state_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    } <= after_finish_names
    assert "validate_navigation_outputs_tool" not in after_finish_names
    assert "get_current_plan_step_tool" not in after_finish_names
    assert processing_calls == [
        ("prepare_raw_data", 1),
        ("validate_navigation_outputs", finish_execution_index + 1),
    ]
    assert services.task_store.get_task(built.task.task_id).status == (
        NavigationTaskStatus.COMPLETED.value
    )
    assert services.plan_store.get_active_for_task(built.task.task_id) is None
    assert model.compact_event_count == 0
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_wrong_step_id_keeps_current_action_available_for_model_retry(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, _storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-retry")
    )
    observation = services.observation_store.latest(built.task.task_id)
    assert observation is not None
    plan = services.plan_store.activate(
        built.task,
        "extract_sync",
        observation.revision,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
        expected_web_session_id="web-retry",
        expected_agentscope_session_id=session_id,
    )
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "extract_sync"},
    )
    model.enqueue_text("step_id 不匹配，将按当前 ledger step 重试。")
    processing_calls = []

    def should_not_run(**_kwargs):
        processing_calls.append(len(model.invocations))
        return ToolResult(
            ok=True,
            tool_name="prepare_raw_data",
            message="unexpected",
        )

    monkeypatch.setattr(plan_execution, "prepare_raw_data", should_not_run)
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="执行当前步骤"),
    )

    retry_names = schema_names(model.invocations[1].tools)
    assert "prepare_raw_data_tool" in retry_names
    assert "get_current_plan_step_tool" in retry_names
    assert "submit_extract_sync_plan_tool" not in retry_names
    assert processing_calls == []
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current["step"]["step_id"] == "prepare_raw"
    assert current["step"]["status"] == "pending"
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_failed_ledger_step_has_no_executable_wrapper_after_restart(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, _storage, _bus, service = (
        await _real_chat_service_case(tmp_path, web_session_id="web-failed-step")
    )
    payload = valid_extract_plan_payload(built)
    payload["steps"] = [payload["steps"][0]]
    observation = services.observation_store.latest(built.task.task_id)
    assert observation is not None
    plan = services.plan_store.activate(
        built.task,
        "extract_sync",
        observation.revision,
        ExtractSyncPlanInput.model_validate(payload),
        expected_web_session_id="web-failed-step",
        expected_agentscope_session_id=session_id,
    )
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_text("处理失败，当前 Plan 不可重跑。")
    processing_calls = []

    def fail_once(**_kwargs):
        processing_calls.append(len(model.invocations))
        return ToolResult(
            ok=False,
            tool_name="prepare_raw_data",
            message="failed",
            details={"error_type": "processing_failed"},
        )

    monkeypatch.setattr(plan_execution, "prepare_raw_data", fail_once)
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="执行当前步骤"),
    )
    model.enqueue_text("fresh surface 不允许重跑 failed ledger step。")
    fresh_start = len(model.invocations)
    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="再次继续"),
    )

    assert "prepare_raw_data_tool" not in schema_names(model.invocations[1].tools)
    assert fresh_start == 2
    fresh_names = schema_names(model.invocations[fresh_start].tools)
    assert "prepare_raw_data_tool" not in fresh_names
    assert "submit_extract_sync_plan_tool" in fresh_names
    assert processing_calls == [1]
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current["step"]["status"] == "failed"
    model.assert_exhausted()
