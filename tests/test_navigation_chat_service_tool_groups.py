from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from agentscope.app._service._chat import ChatService
from agentscope.app.storage import ChatModelConfig, SessionConfig
from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import ToolResultBlock, ToolResultState, UserMsg
from agentscope.skill import Skill
from agentscope.tool import FunctionTool, ToolGroup, Toolkit

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
from vla_data_juicer_agents.navigation.agent_tools import (
    resolve_navigation_agent_tools,
)
from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.models import ToolResult
from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    AgentScopeRuntime,
    build_extra_agent_middlewares_factory,
)
from vla_data_juicer_agents.runtime.navigation_tool_surface import (
    NavigationToolSurfaceSyncError,
)
from vla_data_juicer_agents.navigation.plan_models import ExtractSyncPlanInput
from vla_data_juicer_agents.navigation.plan_store import StepClaimOutcome
from vla_data_juicer_agents.navigation.tool_groups import (
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_DIAGNOSTICS,
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_PLAN_AUTHORING,
)
from test_navigation_context_budget import _extract_plan, _write_raw_metadata
from test_navigation_model_authored_flow import _write_finish_inputs


EXTRACT_INSPECTIONS = (
    "inspect_navigation_artifact_state_tool",
    "inspect_navigation_raw_metadata_tool",
    "inspect_navigation_topic_candidates_tool",
    "inspect_navigation_sensor_candidates_tool",
)
EXECUTION_TOOL_NAMES = {
    "get_current_plan_step_tool",
    "get_plan_execution_overview_tool",
    "prepare_raw_data_tool",
    "extract_and_sync_navigation_data_tool",
    "confirm_navigation_calibration_params_tool",
    "assemble_finish_temp_tool",
    "run_noobscene_preprocessing_tool",
    "run_initial_annotation_gui_tool",
    "run_tracking_tool",
    "run_annotation_tracking_workflow_tool",
    "prepare_gridmap_for_projection_tool",
    "run_projection_and_trajectory_tool",
    "run_annotation_postprocessing_workflow_tool",
    "validate_navigation_outputs_tool",
    "open_trajectory_fix_workbench_tool",
    "validate_trajectory_review_outcome_tool",
}
GENERIC_OR_RESET_TOOL_NAMES = {"bash", "read", "task", "reset_tools"}


@pytest.fixture(autouse=True)
def _private_navigation_writer_lock(tmp_path, monkeypatch):
    lock_root = tmp_path / "writer-lock"
    lock_root.mkdir(mode=0o700)
    monkeypatch.setenv(
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        str(lock_root / "navigation.lock"),
    )


class ProcessingSpy:
    def __init__(self, model: ScriptedChatModel) -> None:
        self.model = model
        self.calls: list[tuple[str, int]] = []

    def callable(
        self,
        action: str,
        *,
        ok: bool = True,
        message: str = "completed",
        details: dict | None = None,
    ):
        def invoke(**_kwargs):
            self.calls.append((action, len(self.model.invocations)))
            return ToolResult(
                ok=ok,
                tool_name=action,
                message=message,
                details=details or {},
            )

        return invoke


class AnnotationGatewaySpy:
    """Minimal Annotation boundary used by the M2 chat-surface integration test."""

    def __init__(self) -> None:
        self.tracking_calls: list[dict[str, str]] = []

    def get_processing_facts(self, **_kwargs) -> dict[str, object]:
        return {
            "job_status": "missing",
            "segment_count": 0,
            "tracked_count": 0,
            "ready_for_postprocessing": False,
            "processing_calibration_snapshot_available": False,
        }

    def begin_annotation_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> dict[str, object]:
        self.tracking_calls.append(
            {
                "navigation_task_id": navigation_task_id,
                "plan_id": plan_id,
                "step_id": step_id,
            }
        )
        return {"status": "waiting_initial_annotation"}


class ModelInputProbe(ScriptedChatModel):
    def __init__(self, *, context_size: int = 131_072) -> None:
        super().__init__(context_size=context_size)
        self.token_count_inputs: list[dict[str, object]] = []
        self.api_call_count = 0

    async def count_tokens(self, messages, tools):
        self.token_count_inputs.append(
            {
                "messages": json.dumps(
                    [message.model_dump(mode="json") for message in messages],
                    ensure_ascii=False,
                ),
                "tool_names": schema_names(list(tools or [])),
            }
        )
        return await super().count_tokens(messages, tools)

    async def _call_api(self, *args, **kwargs):
        self.api_call_count += 1
        return await super()._call_api(*args, **kwargs)


def _generic_toolkit_with_skill_group() -> Toolkit:
    def ok() -> dict[str, bool]:
        return {"ok": True}

    return Toolkit(
        tools=[
            FunctionTool(ok, name="bash", is_read_only=True),
            FunctionTool(ok, name="read", is_read_only=True),
            FunctionTool(ok, name="task", is_read_only=True),
        ],
        tool_groups=[
            ToolGroup(
                name="generic_extensions",
                description="Generic skill and MCP-equivalent tools.",
                instructions="FORBIDDEN_GENERIC_GROUP_INSTRUCTIONS",
                tools=[
                    FunctionTool(
                        ok,
                        name="generic_mcp_equivalent_tool",
                        is_read_only=True,
                    )
                ],
                skills_or_loaders=[
                    Skill(
                        name="forbidden_generic_skill",
                        description="FORBIDDEN_GENERIC_SKILL_INSTRUCTIONS",
                        dir="/tmp/forbidden-generic-skill",
                        markdown="# Forbidden generic skill",
                        updated_at=0.0,
                    )
                ],
            )
        ],
    )


def test_execution_denylist_matches_catalog_and_real_external_schema():
    catalog_action_schemas = {
        f"{capability.tool_name}_tool"
        for capability in list_navigation_tool_capabilities()
        if capability.executor_agent_allowed
    }

    assert EXECUTION_TOOL_NAMES == {
        "get_current_plan_step_tool",
        "get_plan_execution_overview_tool",
        *catalog_action_schemas,
    }


def _evidence_by_kind(services, task_id: str) -> dict[str, str]:
    return {
        descriptor.kind: descriptor.ref
        for descriptor in services.observation_store.list_evidence(task_id, limit=50)
    }


def _invocation_index_for_tool(model, tool_name: str, *, start: int = 0) -> int:
    for index, invocation in enumerate(model.invocations[start:], start=start):
        if any(
            block.get("type") == "tool_call" and block.get("name") == tool_name
            for block in invocation.response_blocks
        ):
            return index
    raise AssertionError(f"missing scripted tool invocation: {tool_name}")


def _extract_plan_payload(built, *, one_step: bool = False) -> dict:
    payload = _extract_plan(built.evidence_refs)
    if one_step:
        payload["steps"] = [payload["steps"][0]]
    return payload


def _activate_extract_plan(services, built, *, one_step: bool = False):
    observation = services.observation_store.latest(built.task.task_id)
    assert observation is not None
    return services.plan_store.activate(
        built.task,
        "extract_sync",
        observation.revision,
        ExtractSyncPlanInput.model_validate(
            _extract_plan_payload(built, one_step=one_step)
        ),
        expected_web_session_id=built.task.created_by_web_session_id,
        expected_agentscope_session_id=built.task.agentscope_session_id,
    )


async def _call_tool(tool, **arguments) -> dict:
    response = await tool(**arguments)
    if isinstance(response, dict):
        return response
    if response.metadata:
        return response.metadata
    text = "".join(
        block.text
        for block in response.content
        if hasattr(block, "text")
    )
    return json.loads(text)


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
                "requires_user_confirmation": True,
                "reason": "Confirm the observed profile before use.",
                "evidence_refs": [evidence["calibration_inventory"]],
            },
        },
        "steps": [
            {
                "step_id": "confirm_calibration",
                "action": "confirm_navigation_calibration_params",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": ["calibration"],
            },
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


def _annotation_creation_plan(evidence: dict[str, str]) -> dict:
    """The only valid M2 application-owned path when an Annotation Job is absent."""
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
                "mode": "hardcoded_with_user_confirmation",
                "selected_sensor_source": "NoobScenes/params/selected/sensors",
                "requires_user_confirmation": True,
                "reason": "Confirm the observed processing profile before use.",
                "evidence_refs": [evidence["calibration_inventory"]],
            },
        },
        "steps": [
            {
                "step_id": "confirm_calibration",
                "action": "confirm_navigation_calibration_params",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": ["calibration"],
            },
            {
                "step_id": "annotation_tracking",
                "action": "run_annotation_tracking_workflow",
                "variant": "durable_web_handoff",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["localization", "calibration"],
            },
            {
                "step_id": "postprocess",
                "action": "run_annotation_postprocessing_workflow",
                "variant": "plan_bound_runtime",
                "arguments": {},
                "depends_on": ["annotation_tracking"],
                "failure_policy": "stop",
                "decision_refs": ["localization", "gridmap", "calibration"],
            },
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": ["postprocess"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


async def _real_chat_service_case(
    monkeypatch,
    tmp_path,
    *,
    web_session_id: str,
    annotation_gateway=None,
):
    data_root = tmp_path / "datasets"
    processing_root = tmp_path / "processing"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(data_root))
    monkeypatch.setenv("VLA_PROCESSING_ROOT", str(processing_root))
    config = runtime_config(tmp_path)
    session_id = f"{web_session_id}__{config.navigation_agent_id}"
    date = "20260710"
    segment = "20260710_120000"
    _write_raw_metadata(data_root, date, segment)
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
    runtime.set_annotation_gateway(annotation_gateway)
    services = runtime._navigation_services()
    task = services.task_store.create_task_attempt(
        request="Process navigation data",
        target=date,
        date=date,
        segments=[segment],
        scene_mode=None,
        dry_run=False,
        web_session_id=web_session_id,
        agentscope_session_id=session_id,
    ).task
    for name in EXTRACT_INSPECTIONS:
        tools = {
            tool.name: tool
            for tool in resolve_navigation_agent_tools(
                services=runtime._navigation_services(),
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
                cancellation=None,
            )
        }
        result = await _call_tool(tools[name])
        assert result["ok"] is True
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=runtime._navigation_services(),
            web_session_id=web_session_id,
            agentscope_session_id=session_id,
            cancellation=None,
        )
    }
    context = await _call_tool(tools["get_navigation_task_context_tool"])
    built = SimpleNamespace(
        task=task,
        planning_context_revision=context["planning_context_revision"],
        evidence_refs=_evidence_by_kind(services, task.task_id),
    )
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


def _patch_assembly(monkeypatch, model, *, toolkit=None):
    async def async_get_scripted_model(*_args, **_kwargs):
        return model

    async def async_get_generic_toolkit(**_kwargs):
        return toolkit or generic_toolkit()

    monkeypatch.setattr(chat_service_module, "get_model", async_get_scripted_model)
    monkeypatch.setattr(chat_service_module, "get_toolkit", async_get_generic_toolkit)


@pytest.mark.asyncio
async def test_compression_uses_authorized_surface_before_first_reasoning(
    monkeypatch,
    tmp_path,
):
    config, session_id, _built, _services, storage, _bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-compression-barrier",
        )
    )
    record = storage.agents[config.navigation_agent_id]
    storage.agents[config.navigation_agent_id] = record.model_copy(
        update={
            "data": record.data.model_copy(
                update={
                    "context_config": record.data.context_config.model_copy(
                        update={"trigger_ratio": 0.1, "reserve_ratio": 0.05}
                    )
                }
            )
        }
    )
    state = storage.sessions[
        (config.user_id, config.navigation_agent_id, session_id)
    ].state
    state.context[:] = [
        UserMsg(
            name="user",
            content="PRELOADED_CONTEXT " + "navigation context " * 1_500,
        )
    ]
    state.tool_context.activated_groups[:] = ["generic_extensions"]

    model = ModelInputProbe(context_size=16_384)
    model.enqueue_tool(
        "generate_structured_output",
        {
            "task_overview": "Continue the authorized navigation task.",
            "current_state": "Planning is active.",
            "important_discoveries": "Evidence is stored durably.",
            "next_steps": "Use only the current navigation surface.",
            "context_to_preserve": "Keep the session identity bound.",
        },
    )
    model.enqueue_text("压缩后继续规划。")
    toolkit = _generic_toolkit_with_skill_group()
    _patch_assembly(monkeypatch, model, toolkit=toolkit)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="继续规划"),
    )

    assert model.compact_event_count == 1
    assert model.api_call_count == 2
    assert len(model.invocations) == 2
    first_prepared_input = model.token_count_inputs[0]
    prepared_names = first_prepared_input["tool_names"]
    assert {
        "inspect_navigation_raw_metadata_tool",
        "submit_extract_sync_plan_tool",
    } <= prepared_names
    assert {
        "bash",
        "read",
        "task",
        "generic_mcp_equivalent_tool",
        "Skill",
        "skill_viewer",
    }.isdisjoint(prepared_names)
    assert "FORBIDDEN_GENERIC_SKILL_INSTRUCTIONS" not in first_prepared_input[
        "messages"
    ]

    compression_input = json.dumps(
        model.invocations[0].formatted_messages,
        ensure_ascii=False,
    )
    assert schema_names(model.invocations[0].tools) == {
        "generate_structured_output"
    }
    assert "FORBIDDEN_GENERIC_SKILL_INSTRUCTIONS" not in compression_input
    assert "FORBIDDEN_GENERIC_GROUP_INSTRUCTIONS" not in compression_input
    assert "generic_mcp_equivalent_tool" not in compression_input
    assert "reset_tools" not in compression_input

    reasoning_names = schema_names(model.invocations[1].tools)
    assert "submit_extract_sync_plan_tool" in reasoning_names
    assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(reasoning_names)
    assert "generic_mcp_equivalent_tool" not in reasoning_names
    assert storage.updated_state.tool_context.activated_groups == [
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_INVESTIGATION,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_PLAN_AUTHORING,
        NAVIGATION_DIAGNOSTICS,
    ]
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_missing_attempt_fails_before_compression_or_model_call(
    monkeypatch,
    tmp_path,
):
    config = runtime_config(tmp_path)
    storage = ChatServiceStorage()
    await bootstrap_agentscope_records(storage, config)
    session_id = f"web-missing-attempt__{config.navigation_agent_id}"
    await storage.upsert_session(
        config.user_id,
        config.navigation_agent_id,
        SessionConfig(
            workspace_id="workspace-missing-attempt",
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
    state = storage.sessions[
        (config.user_id, config.navigation_agent_id, session_id)
    ].state
    state.context[:] = [
        UserMsg(name="user", content="PRELOADED_CONTEXT " * 2_000)
    ]
    state.tool_context.activated_groups[:] = ["generic_extensions"]
    model = ModelInputProbe(context_size=4_096)
    toolkit = _generic_toolkit_with_skill_group()
    get_model_calls = 0

    async def get_model_spy(*_args, **_kwargs):
        nonlocal get_model_calls
        get_model_calls += 1
        return model

    async def get_toolkit_spy(**_kwargs):
        return toolkit

    monkeypatch.setattr(chat_service_module, "get_model", get_model_spy)
    monkeypatch.setattr(chat_service_module, "get_toolkit", get_toolkit_spy)

    with pytest.raises(
        NavigationToolSurfaceSyncError,
        match="^navigation tool surface unavailable$",
    ):
        await service._run_impl(
            config.user_id,
            session_id,
            config.navigation_agent_id,
            UserMsg(name="user", content="继续"),
        )

    assert get_model_calls == 1
    assert model.token_count_inputs == []
    assert model.api_call_count == 0
    assert model.invocations == []
    assert model.compact_event_count == 0
    assert len(toolkit.tool_groups) == 1
    assert toolkit.tool_groups[0].name == "basic"
    assert toolkit.tool_groups[0].tools == []


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
    _patch_assembly(monkeypatch, model)
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
    ) = await _real_chat_service_case(monkeypatch, tmp_path, web_session_id="web-plan")
    model = ScriptedChatModel()
    model.enqueue_tool(
        "submit_extract_sync_plan_tool",
        {
            "planning_context_revision": built.planning_context_revision,
            "plan": _extract_plan_payload(built),
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
            "step_id": latest_tool_result_json(messages)["step_id"],
        },
    )
    model.enqueue_text("第一步执行完成。")
    processing_spy = ProcessingSpy(model)
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        processing_spy.callable("prepare_raw_data"),
    )
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
    assert processing_spy.calls == [("prepare_raw_data", 3)]
    assert services.plan_store.get_active_for_task(built.task.task_id) is not None
    assert model.compact_event_count == 0
    assert model.assert_exhausted() is None


@pytest.mark.asyncio
async def test_failed_plan_submission_keeps_planning_surface_in_same_reply(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, storage, bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-stale-submit",
        )
    )
    model = ScriptedChatModel()
    model.enqueue_tool(
        "submit_extract_sync_plan_tool",
        {
            "planning_context_revision": "stale-planning-context-revision",
            "plan": _extract_plan_payload(built),
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
    assert EXECUTION_TOOL_NAMES.isdisjoint(next_names)
    assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(next_names)
    result = latest_tool_result_json(storage.updated_state.context)
    assert result["ok"] is False
    assert result["error_type"] == "planning_context_mismatch"
    assert result["errors"] == [
        {
            "path": "planning_context_revision",
            "code": "stale_planning_context_revision",
            "message": "Planning context revision is stale",
            "allowed_values": [],
        }
    ]
    assert result["retry"] == "resubmit_complete_plan"
    assert services.plan_store.get_active_for_task(built.task.task_id) is None
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_restart_rebuilds_planning_surface_over_stale_execution_cache(
    monkeypatch,
    tmp_path,
):
    config, session_id, _built, _services, storage, _bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-cache-planning",
        )
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
    assert EXECUTION_TOOL_NAMES.isdisjoint(names)
    assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(names)
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
    config, session_id, built, services, storage, bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-cache-execution",
        )
    )
    _activate_extract_plan(services, built)
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
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-reverse",
        )
    )
    plan = _activate_extract_plan(services, built, one_step=True)
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_tool("inspect_navigation_artifact_state_tool", {})
    model.enqueue_text("extract 阶段已完成；是否继续规划 finish？")
    processing_spy = ProcessingSpy(model)
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        processing_spy.callable("prepare_raw_data"),
    )
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
    assert EXECUTION_TOOL_NAMES.isdisjoint(after_final_names)
    assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(after_final_names)
    assert processing_spy.calls == [("prepare_raw_data", 1)]
    assert services.plan_store.get(plan.plan_id).status == "completed"
    task = services.task_store.get_task(built.task.task_id)
    assert task is not None
    assert task.status == NavigationTaskStatus.ACTIVE.value
    assert services.plan_store.get_active_for_task(built.task.task_id) is None
    with sqlite3.connect(services.plan_store.db_path) as connection:
        finish_plan_count = connection.execute(
            "SELECT count(*) FROM navigation_plans WHERE task_id = ? AND phase = ?",
            (built.task.task_id, "finish_processing"),
        ).fetchone()[0]
    assert finish_plan_count == 0
    assert model.compact_event_count == 0
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_later_same_session_finish_plan_executes_and_closes_task(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, storage, bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-two-stage",
        )
    )
    extract_plan = _activate_extract_plan(services, built, one_step=True)
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": extract_plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_tool("inspect_navigation_artifact_state_tool", {})
    model.enqueue_text("extract 已完成；请确认是否继续 finish。")
    processing_spy = ProcessingSpy(model)
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        processing_spy.callable("prepare_raw_data"),
    )
    monkeypatch.setattr(
        plan_execution,
        "validate_navigation_outputs",
        processing_spy.callable("validate_navigation_outputs"),
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
    first_persisted_state = storage.updated_state

    _write_finish_inputs(services.settings)
    final_gridmap = (
        services.settings.finish_data_root
        / built.task.date
        / built.task.segments[0]
        / "clip-1"
        / "grid_map"
    )
    final_gridmap.mkdir(parents=True, exist_ok=True)
    (final_gridmap / "map.json").write_text("{}", encoding="utf-8")
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
        "confirm_navigation_calibration_params_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
            "step_id": "confirm_calibration",
        },
    )

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="继续，室外场景，完成 finish processing。"),
    )

    finish_plan = services.plan_store.get_active_for_task(built.task.task_id)
    assert finish_plan is not None
    assert plan_execution.submit_plan_human_decision(
        plan_store=services.plan_store,
        evidence_store=services.evidence_store,
        plan_id=finish_plan.plan_id,
        step_id="confirm_calibration",
        decision={"action": "confirm"},
        expected_web_session_id=built.task.created_by_web_session_id,
        expected_agentscope_session_id=built.task.agentscope_session_id,
    ) is True
    decision_event = next(
        event
        for event in reversed(bus.events)
        if event.get("type") == "REQUIRE_EXTERNAL_EXECUTION"
    )
    pending_tool_call = decision_event["tool_calls"][0]
    external_result = ExternalExecutionResultEvent(
        reply_id=decision_event["reply_id"],
        execution_results=[
            ToolResultBlock(
                id=pending_tool_call["id"],
                name="confirm_navigation_calibration_params_tool",
                output=json.dumps(
                    {
                        "action": "confirm",
                        "text": "",
                        "request_id": "",
                    },
                    ensure_ascii=False,
                ),
                state=ToolResultState.SUCCESS,
            )
        ],
    )

    model.enqueue_tool(
        "validate_navigation_outputs_tool",
        {
            "plan_id": finish_plan.plan_id,
            "step_id": "validate_outputs",
        },
    )
    model.enqueue_text("finish validation 完成，任务已闭合。")
    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        external_result,
    )

    assert first_persisted_state is not storage.updated_state
    assert len(first_persisted_state.context) < len(storage.updated_state.context)
    assert "extract 已完成；请确认是否继续 finish。" in json.dumps(
        model.invocations[second_run_start].formatted_messages,
        ensure_ascii=False,
    )
    finish_submission_index = _invocation_index_for_tool(
        model,
        "submit_finish_processing_plan_tool",
        start=second_run_start,
    )
    for invocation in model.invocations[second_run_start : finish_submission_index + 1]:
        names = schema_names(invocation.tools)
        assert {
            "submit_extract_sync_plan_tool",
            "submit_finish_processing_plan_tool",
        } <= names
        assert EXECUTION_TOOL_NAMES.isdisjoint(names)
        assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(names)
    human_decision_index = _invocation_index_for_tool(
        model,
        "confirm_navigation_calibration_params_tool",
        start=finish_submission_index + 1,
    )
    human_decision_names = schema_names(model.invocations[human_decision_index].tools)
    assert "confirm_navigation_calibration_params_tool" in human_decision_names
    finish_execution_index = _invocation_index_for_tool(
        model,
        "validate_navigation_outputs_tool",
        start=human_decision_index + 1,
    )
    after_finish_index = finish_execution_index + 1
    finish_execution_names = schema_names(
        model.invocations[finish_execution_index].tools
    )
    assert {
        "get_current_plan_step_tool",
        "validate_navigation_outputs_tool",
    } <= finish_execution_names
    assert "submit_finish_processing_plan_tool" not in finish_execution_names
    assert "submit_extract_sync_plan_tool" not in finish_execution_names
    assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(finish_execution_names)
    after_finish_names = schema_names(model.invocations[after_finish_index].tools)
    assert {
        "inspect_navigation_artifact_state_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
    } <= after_finish_names
    assert EXECUTION_TOOL_NAMES.isdisjoint(after_finish_names)
    assert GENERIC_OR_RESET_TOOL_NAMES.isdisjoint(after_finish_names)
    assert processing_spy.calls == [
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
async def test_missing_annotation_job_requires_scene_guidance_before_creation_handoff(
    monkeypatch,
    tmp_path,
):
    gateway = AnnotationGatewaySpy()
    config, session_id, built, services, storage, bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-m2-missing-annotation",
            annotation_gateway=gateway,
        )
    )
    with sqlite3.connect(services.plan_store.db_path) as connection:
        connection.execute(
            """UPDATE navigation_task_outcomes
               SET requested_outcome = 'postprocessing'
               WHERE task_id = ?""",
            (built.task.task_id,),
        )
    _write_finish_inputs(services.settings)

    model = ScriptedChatModel()
    for name in (
        "inspect_navigation_annotation_job_facts_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "inspect_navigation_gridmap_artifacts_tool",
    ):
        model.enqueue_tool(name, {})
    context_revisions = {}
    model.enqueue_tool("get_navigation_task_context_tool", {})

    def record_scene_guidance(messages):
        context = latest_tool_result_json(messages)
        context_revisions["before_guidance"] = context[
            "planning_context_revision"
        ]
        assert context["scene_mode"] is None
        assert {
            "annotation_job_facts",
            "artifact_state",
            "runtime_assets",
            "calibration_inventory",
            "localization_sources",
            "gridmap_artifacts",
        } <= set(context["observed_kinds"])
        return {"text": "室外场景", "scene_mode": "out"}

    model.enqueue_tool(
        "record_navigation_user_guidance_tool",
        record_scene_guidance,
    )
    model.enqueue_tool("get_navigation_task_context_tool", {})

    def submit_creation_plan(messages):
        context = latest_tool_result_json(messages)
        context_revisions["after_guidance"] = context[
            "planning_context_revision"
        ]
        assert context["scene_mode"] == "out"
        return {
            "planning_context_revision": context[
                "planning_context_revision"
            ],
            "plan": _annotation_creation_plan(
                _evidence_by_kind(services, built.task.task_id)
            ),
        }

    model.enqueue_tool(
        "submit_finish_processing_plan_tool",
        submit_creation_plan,
    )
    model.enqueue_tool(
        "get_current_plan_step_tool",
        lambda messages: {"plan_id": latest_tool_result_json(messages)["plan_id"]},
    )
    model.enqueue_tool(
        "confirm_navigation_calibration_params_tool",
        lambda messages: {
            "plan_id": latest_tool_result_json(messages)["plan_id"],
            "step_id": latest_tool_result_json(messages)["step_id"],
        },
    )
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="自动标注并完成后处理"),
    )

    guidance_index = _invocation_index_for_tool(
        model,
        "record_navigation_user_guidance_tool",
    )
    guidance_names = schema_names(model.invocations[guidance_index].tools)
    assert "record_navigation_user_guidance_tool" in guidance_names
    assert "get_navigation_task_context_tool" in guidance_names
    assert "submit_finish_processing_plan_tool" not in guidance_names
    diagnostic_context_index = _invocation_index_for_tool(
        model,
        "get_navigation_task_context_tool",
    )
    assert diagnostic_context_index < guidance_index
    submission_context_index = _invocation_index_for_tool(
        model,
        "get_navigation_task_context_tool",
        start=guidance_index + 1,
    )
    context_names = schema_names(
        model.invocations[submission_context_index].tools
    )
    assert {
        "get_navigation_task_context_tool",
        "submit_finish_processing_plan_tool",
    } <= context_names
    assert "record_navigation_user_guidance_tool" not in context_names
    assert (
        context_revisions["before_guidance"]
        != context_revisions["after_guidance"]
    )

    plan = services.plan_store.get_active_for_task(built.task.task_id)
    assert plan is not None
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current is not None
    assert current["step"]["step_id"] == "confirm_calibration"
    assert current["step"]["status"] == "waiting_user"
    decision_event = next(
        event
        for event in reversed(bus.events)
        if event.get("type") == "REQUIRE_EXTERNAL_EXECUTION"
    )
    pending_call = decision_event["tool_calls"][0]
    assert plan_execution.submit_plan_human_decision(
        plan_store=services.plan_store,
        evidence_store=services.evidence_store,
        plan_id=plan.plan_id,
        step_id="confirm_calibration",
        decision={"action": "confirm"},
        expected_web_session_id=built.task.created_by_web_session_id,
        expected_agentscope_session_id=built.task.agentscope_session_id,
    ) is True

    model.enqueue_tool(
        "get_current_plan_step_tool",
        {"plan_id": plan.plan_id},
    )
    observed_tracking_step: dict[str, object] = {}

    def _start_tracking_from_current(messages):
        observed_tracking_step.update(latest_tool_result_json(messages))
        return {
            "plan_id": observed_tracking_step["plan_id"],
            "step_id": observed_tracking_step["step_id"],
        }

    model.enqueue_tool(
        "run_annotation_tracking_workflow_tool",
        _start_tracking_from_current,
    )
    external_result = ExternalExecutionResultEvent(
        reply_id=decision_event["reply_id"],
        execution_results=[
            ToolResultBlock(
                id=pending_call["id"],
                name="confirm_navigation_calibration_params_tool",
                output=json.dumps(
                    {"action": "confirm", "text": "", "request_id": ""},
                    ensure_ascii=False,
                ),
                state=ToolResultState.SUCCESS,
            )
        ],
    )
    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        external_result,
    )

    assert observed_tracking_step == {
        "plan_id": plan.plan_id,
        "step_id": "annotation_tracking",
        "action": "run_annotation_tracking_workflow",
        "status": "pending",
    }
    assert gateway.tracking_calls == [
        {
            "navigation_task_id": built.task.task_id,
            "plan_id": plan.plan_id,
            "step_id": "annotation_tracking",
        }
    ]
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current is not None
    assert current["step"]["status"] == "waiting_user"
    assert storage.updated_state is not None
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_running_step_ends_reply_without_polling_model(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, storage, _bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-background-wait",
        )
    )
    plan = _activate_extract_plan(services, built)
    first_step = plan.plan.steps[0]
    assert services.plan_store.claim_step(
        plan.plan_id,
        first_step.step_id,
        first_step.action,
        expected_web_session_id=built.task.created_by_web_session_id,
        expected_agentscope_session_id=built.task.agentscope_session_id,
    ) is StepClaimOutcome.CLAIMED
    model = ScriptedChatModel()
    _patch_assembly(monkeypatch, model)

    await service._run_impl(
        config.user_id,
        session_id,
        config.navigation_agent_id,
        UserMsg(name="user", content="后台步骤仍在运行"),
    )

    assert model.invocations == []
    assert storage.updated_state.tool_context.activated_groups == []
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current["step"]["step_id"] == first_step.step_id
    assert current["step"]["status"] == "running"
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_wrong_step_id_keeps_current_action_available_for_model_retry(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, _storage, _bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-retry",
        )
    )
    plan = _activate_extract_plan(services, built)
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "missing_step"},
    )
    model.enqueue_text("step_id 不匹配，将按当前 ledger step 重试。")
    processing_spy = ProcessingSpy(model)
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        processing_spy.callable("prepare_raw_data", message="unexpected"),
    )
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
    assert processing_spy.calls == []
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current["step"]["step_id"] == "prepare_raw"
    assert current["step"]["status"] == "pending"
    result = latest_tool_result_json(_storage.updated_state.context)
    assert result["ok"] is False
    assert result["error_type"] == "step_not_found"
    assert result["next_action"] == "get_current_step"
    assert model.compact_event_count == 0
    model.assert_exhausted()


@pytest.mark.asyncio
async def test_failed_ledger_step_has_no_executable_wrapper_after_restart(
    monkeypatch,
    tmp_path,
):
    config, session_id, built, services, _storage, _bus, service = (
        await _real_chat_service_case(
            monkeypatch,
            tmp_path,
            web_session_id="web-failed-step",
        )
    )
    plan = _activate_extract_plan(services, built, one_step=True)
    model = ScriptedChatModel()
    model.enqueue_tool(
        "prepare_raw_data_tool",
        {"plan_id": plan.plan_id, "step_id": "prepare_raw"},
    )
    model.enqueue_text("处理失败，当前 Plan 不可重跑。")
    processing_spy = ProcessingSpy(model)
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        processing_spy.callable(
            "prepare_raw_data",
            ok=False,
            message="failed",
            details={"error_type": "processing_failed"},
        ),
    )
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
    assert processing_spy.calls == [("prepare_raw_data", 1)]
    current = services.plan_store.get_current_step(plan.plan_id)
    assert current["step"]["status"] == "failed"
    assert model.compact_event_count == 0
    model.assert_exhausted()
