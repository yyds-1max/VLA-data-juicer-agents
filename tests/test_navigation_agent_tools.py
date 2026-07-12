import asyncio
import json
import sqlite3
import time
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import ToolResultState
from agentscope.tool import FunctionTool

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation import agent_tools as agent_tools_module
from vla_data_juicer_agents.navigation.agent_tools import (
    HumanDecisionTool,
    PlanBoundHumanDecisionTool,
    build_navigation_agent_tools,
    resolve_navigation_agent_tools,
)
from vla_data_juicer_agents.navigation.models import (
    NavigationRequest,
    WorkflowPlan,
    WorkflowStep,
)
from vla_data_juicer_agents.navigation.plan_draft import (
    WorkflowPlanDraftState,
    build_plan_from_draft,
)
from vla_data_juicer_agents.navigation.plan_draft_store import (
    InMemoryNavigationPlanDraftStore,
    JsonNavigationPlanDraftStore,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.navigation.task_tools import build_navigation_task_tools
from vla_data_juicer_agents.navigation.services import (
    NavigationServices,
    build_navigation_services,
)
from vla_data_juicer_agents.navigation import services as navigation_services_module
from vla_data_juicer_agents.navigation.plan_models import ExtractSyncPlanInput
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    EvidenceWrite,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from test_navigation_plan_submission_tools import (
    build_services as build_complete_plan_services,
    valid_extract_plan_payload,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationArtifactSnapshot,
    NavigationTaskPhase,
    NavigationTaskStatus,
)
from vla_data_juicer_agents.runtime import agentscope_runtime as runtime_module
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    NavigationHandoffTool,
    build_extra_agent_tools_factory,
    create_agentscope_runtime,
)


class FakeNavigationHandoffRuntime:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.records: list[dict] = []

    async def start_navigation_agent_task(self, *, web_session_id: str, message: str) -> str:
        self.started.append({"web_session_id": web_session_id, "message": message})
        return "navigation-session"

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


def _complete_profile_patch():
    topic_params = {
        "profile_hint": "go2w",
        "confidence": 1.0,
        "topic_whitelist": [
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "rs32_lidar_points",
        "evidence": ["infer_navigation_topic_params_tool"],
        "warnings": [],
        "blocking_issues": [],
    }
    return {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "topic_params": topic_params,
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_policy": {"source": "existing_gridmap"},
            "calibration_policy": {
                "mode": "hardcoded_with_user_confirmation",
                "requires_user_confirmation": True,
            },
            "warnings": [],
            "blocking_issues": [],
            "evidence": {"processing_profile": ["infer_navigation_processing_profile_tool"]},
        },
        "platform_hint": "go2w",
        "topic_params": topic_params,
        "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
        "gridmap_source": "existing_gridmap",
        "pcd_gridmap_tool_available": True,
        "stage_variants": {
            "extract_and_sync_navigation_data": {
                "variant": "explicit_topic_params",
                "reason": "topic parameters were inferred from sensor role bindings",
                "evidence": ["infer_navigation_processing_profile_tool"],
            },
            "prepare_gridmap_for_projection": {
                "variant": "copy_existing_gridmap",
                "reason": "grid_map artifacts already exist",
                "evidence": ["inspect_gridmap_artifacts_tool"],
            },
            "run_projection_and_trajectory": {
                "variant": "cjl_0525_with_gridmap",
                "reason": "runtime assets support the 0525 projection script",
                "evidence": ["inspect_runtime_assets_tool"],
            },
        },
    }


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
    assert tool.input_schema["additionalProperties"] is False


def test_plan_bound_human_decision_tool_exposes_only_plan_and_step_ids():
    tool = PlanBoundHumanDecisionTool()

    assert tool.name == "request_human_decision"
    assert set(tool.input_schema["properties"]) == {"plan_id", "step_id"}
    assert tool.input_schema["required"] == ["plan_id", "step_id"]
    assert tool.input_schema["additionalProperties"] is False


def test_human_decision_tool_allows_permissions():
    tool = HumanDecisionTool()

    decision = asyncio.run(tool.check_permissions({}, None))

    assert isinstance(decision, PermissionDecision)
    assert decision.behavior is PermissionBehavior.ALLOW


def test_build_navigation_agent_tools_includes_planning_human_decision_and_processing_tools():
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=InMemoryNavigationPlanDraftStore(),
        )
    }
    names = set(tools)

    assert {
        "request_human_decision",
        "inspect_raw_date_tool",
        "infer_navigation_sensor_bindings_tool",
        "infer_navigation_processing_profile_tool",
        "infer_navigation_topic_params_tool",
        "inspect_processing_state_tool",
        "inspect_gridmap_artifacts_tool",
        "inspect_runtime_assets_tool",
        "list_navigation_tool_capabilities_tool",
        "get_workflow_plan_draft_tool",
        "update_workflow_plan_draft_tool",
        "finalize_workflow_plan_tool",
        "prepare_raw_data_tool",
        "extract_and_sync_navigation_data_tool",
        "run_noobscene_preprocessing_tool",
        "run_initial_annotation_gui_tool",
        "run_tracking_tool",
    }.issubset(names)
    assert "confirm_navigation_calibration_params_tool" not in names


def test_build_navigation_agent_tools_omits_draft_tools_without_session_store():
    tools = {tool.name: tool for tool in build_navigation_agent_tools(dry_run=True)}
    names = set(tools)

    assert "request_human_decision" in names
    assert "confirm_navigation_calibration_params_tool" not in names
    assert "prepare_raw_data_tool" in names
    assert "inspect_raw_date_tool" in names
    assert "infer_navigation_processing_profile_tool" in names
    assert "get_workflow_plan_draft_tool" not in names
    assert "finalize_workflow_plan_tool" not in names


def test_build_navigation_agent_tools_does_not_register_old_workflow_control_tools():
    names = {tool.name for tool in build_navigation_agent_tools(dry_run=True)}

    assert "vla_run_workflow" not in names
    assert "vla_continue_workflow" not in names
    assert "confirm_navigation_calibration_params_tool" not in names


def test_navigation_agent_tools_include_task_state_tools(tmp_path):
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")

    tools = build_navigation_agent_tools(
        session_id="agent-session",
        draft_store=store,
        task_store=task_store,
        web_session_id="web-session",
    )
    names = {tool.name for tool in tools}

    assert "get_or_create_navigation_task_tool" in names
    assert "reconcile_navigation_task_tool" in names
    assert "list_resumable_navigation_tasks_tool" in names
    assert "update_navigation_task_scene_mode_tool" in names


def test_build_navigation_agent_tools_passes_cancellation_to_execution_tools(monkeypatch):
    cancellation = CancellationContext()
    captured = {}

    def fake_create_navigation_execution_tools(*, dry_run, cancellation=None):
        captured["dry_run"] = dry_run
        captured["cancellation"] = cancellation
        return []

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        fake_create_navigation_execution_tools,
    )

    tools = build_navigation_agent_tools(dry_run=True, cancellation=cancellation)

    assert {tool.name for tool in tools} == {
        "request_human_decision",
        "inspect_raw_date_tool",
        "infer_navigation_sensor_bindings_tool",
        "infer_navigation_processing_profile_tool",
        "infer_navigation_topic_params_tool",
        "inspect_processing_state_tool",
        "inspect_gridmap_artifacts_tool",
        "inspect_runtime_assets_tool",
        "list_navigation_tool_capabilities_tool",
    }
    assert captured == {"dry_run": True, "cancellation": cancellation}


def test_phase_gate_allows_extract_sync_tools_with_extract_sync_plan(
    monkeypatch,
    tmp_path,
):
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")

    def fake_prepare_raw_data_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "prepare_raw_data", "date": date}

    def fake_extract_and_sync_navigation_data_tool(date: str) -> dict:
        return {
            "ok": True,
            "tool_name": "extract_and_sync_navigation_data",
            "date": date,
        }

    def fake_assemble_finish_temp_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            ),
            FunctionTool(
                fake_extract_and_sync_navigation_data_tool,
                name="extract_and_sync_navigation_data_tool",
                is_read_only=False,
            ),
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            ),
        ],
    )
    state = WorkflowPlanDraftState(request=NavigationRequest(date="20270623"))
    state.finalized_plan = WorkflowPlan(
        date="20270623",
        phase="extract_sync",
        scene_mode=None,
        steps=[
            WorkflowStep(
                step_id="prepare_raw_data",
                tool_name="prepare_raw_data",
                arguments={"date": "20270623"},
            ),
            WorkflowStep(
                step_id="extract_and_sync_navigation_data",
                tool_name="extract_and_sync_navigation_data",
                arguments={
                    "date": "20270623",
                    "topic_whitelist": [
                        "/cam_video4/csi_cam/image_raw/compressed",
                        "/rs32_lidar_points",
                        "/sport_odom",
                    ],
                    "topic_map": {
                        "cam_video4": "fisheye_front",
                        "rs32_lidar_points": "r32_rslidar_points",
                        "sport_odom": "odom",
                    },
                    "query_dir": "rs32_lidar_points",
                },
            ),
        ],
    )
    store.save("agent-session", state)

    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270623"))
    )
    sync_result = _decode_tool_payload(
        asyncio.run(
            tools["extract_and_sync_navigation_data_tool"](date="20270623")
        )
    )

    assert result["ok"] is True
    assert sync_result["ok"] is True


def test_phase_gate_allows_finish_processing_tool_with_finish_processing_plan(
    monkeypatch,
):
    store = InMemoryNavigationPlanDraftStore()

    def fake_assemble_finish_temp_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(request=NavigationRequest(date="20270623"))
    state.finalized_plan = WorkflowPlan(
        date="20270623",
        phase="finish_processing",
        steps=[],
    )
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["assemble_finish_temp_tool"](date="20270623"))
    )

    assert result["ok"] is True


def test_phase_gate_allows_finish_processing_tool_with_full_plan(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_assemble_finish_temp_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(request=NavigationRequest(date="20270623"))
    state.finalized_plan = WorkflowPlan(date="20270623", phase="full", steps=[])
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["assemble_finish_temp_tool"](date="20270623"))
    )

    assert result["ok"] is True


def test_phase_gate_blocks_finish_processing_tool_with_extract_sync_plan(
    monkeypatch,
    tmp_path,
):
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")

    def fake_prepare_raw_data_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "prepare_raw_data", "date": date}

    def fake_assemble_finish_temp_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            ),
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            ),
        ],
    )
    state = WorkflowPlanDraftState(request=NavigationRequest(date="20270623"))
    state.finalized_plan = WorkflowPlan(
        date="20270623",
        phase="extract_sync",
        steps=[],
    )
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["assemble_finish_temp_tool"](date="20270623"))
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_phase_plan_required"


def test_finish_processing_gate_blocks_when_task_snapshot_is_not_reconciled(
    monkeypatch,
    tmp_path,
):
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task_store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
        web_session_id="web-session",
        agentscope_session_id="agent-session",
    )

    def fake_assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date, "segments": segments}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270623",
            scene_mode="out",
            segments=["segment_a"],
        )
    )
    state.finalized_plan = WorkflowPlan(date="20270623", phase="finish_processing", steps=[])
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            web_session_id="web-session",
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(
            tools["assemble_finish_temp_tool"](
                date="20270623",
                segments=["segment_a"],
            )
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_task_reconcile_required"
    assert "reconcile_navigation_task_tool" in result["next_tool_candidates"]


def test_finish_processing_gate_blocks_when_reconciled_sync_artifacts_are_missing(
    monkeypatch,
    tmp_path,
):
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task = task_store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
        web_session_id="web-session",
        agentscope_session_id="agent-session",
    )
    task_store.update_task(
        task.task_id,
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        status=NavigationTaskStatus.NEEDS_RECONCILE,
        artifact_snapshot=NavigationArtifactSnapshot(
            date="20270623",
            segments=["segment_a"],
            sync_data_exists=False,
            sync_data_by_segment={"segment_a": False},
        ).model_dump(mode="json"),
    )

    def fake_assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date, "segments": segments}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270623",
            scene_mode="out",
            segments=["segment_a"],
        )
    )
    state.finalized_plan = WorkflowPlan(date="20270623", phase="finish_processing", steps=[])
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            web_session_id="web-session",
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(
            tools["assemble_finish_temp_tool"](
                date="20270623",
                segments=["segment_a"],
            )
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_task_reconcile_required"
    assert "rerun extract_sync" in result["message"]


def test_finish_processing_gate_blocks_when_sync_data_deleted_after_snapshot(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "VLADatasets"
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task = task_store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
        web_session_id="web-session",
        agentscope_session_id="agent-session",
    )
    task_store.update_task(
        task.task_id,
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        status=NavigationTaskStatus.PENDING,
        artifact_snapshot=NavigationArtifactSnapshot(
            date="20270623",
            segments=["segment_a"],
            sync_data_exists=True,
            sync_data_by_segment={"segment_a": True},
        ).model_dump(mode="json"),
    )

    def fake_assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date, "segments": segments}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270623",
            scene_mode="out",
            segments=["segment_a"],
        )
    )
    state.finalized_plan = WorkflowPlan(date="20270623", phase="finish_processing", steps=[])
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            web_session_id="web-session",
            settings=agent_tools_module.NavigationSettings(vladatasets_root=root),
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(
            tools["assemble_finish_temp_tool"](
                date="20270623",
                segments=["segment_a"],
            )
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_task_reconcile_required"
    assert "complete sync_data for selected segments" in result["missing_fields"]


def test_finish_processing_gate_allows_running_task_with_finish_temp_partial_artifacts(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (
        root
        / "clip_data"
        / "20270623"
        / "segment_a"
        / "sync_data"
        / "clip_0"
        / "fisheye_front"
    ).mkdir(parents=True)
    (root / "finish_data" / "20270623_temp" / "samples" / "20270623").mkdir(parents=True)
    (root / "finish_data" / "20270623_temp" / "samples" / "20270623" / "sample.jpg").write_bytes(b"jpg")
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task = task_store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode="in",
        web_session_id="web-session",
        agentscope_session_id="agent-session",
    )
    task_store.update_task(
        task.task_id,
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        status=NavigationTaskStatus.RUNNING,
        artifact_snapshot=NavigationArtifactSnapshot(
            date="20270623",
            segments=["segment_a"],
            sync_data_exists=True,
            sync_data_by_segment={"segment_a": True},
        ).model_dump(mode="json"),
    )

    def fake_run_noobscene_preprocessing_tool(date: str, segments: list[str] | None = None) -> dict:
        return {"ok": True, "tool_name": "run_noobscene_preprocessing", "date": date, "segments": segments}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_run_noobscene_preprocessing_tool,
                name="run_noobscene_preprocessing_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270623",
            scene_mode="in",
            segments=["segment_a"],
        )
    )
    state.finalized_plan = WorkflowPlan(date="20270623", phase="finish_processing", steps=[])
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            web_session_id="web-session",
            settings=agent_tools_module.NavigationSettings(vladatasets_root=root),
            dry_run=True,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(
            tools["run_noobscene_preprocessing_tool"](
                date="20270623",
                segments=["segment_a"],
            )
        )
    )

    assert result["ok"] is True


def test_resume_finish_gate_rejects_extract_tools_and_allows_finish_tools(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270623" / "segment_a").mkdir(parents=True)
    (root / "clip_data" / "20270623" / "segment_a" / "sync_data" / "clip_0").mkdir(
        parents=True
    )
    store = InMemoryNavigationPlanDraftStore()
    task_store = SqliteNavigationTaskStore(tmp_path / "tasks.sqlite")
    task = task_store.create_or_update_task(
        date="20270623",
        segments=["segment_a"],
        scene_mode="out",
        web_session_id="web-session",
        agentscope_session_id="agent-session",
    )
    task_store.update_task(
        task.task_id,
        phase=NavigationTaskPhase.FINISH_PROCESSING,
        status=NavigationTaskStatus.PENDING,
        artifact_snapshot=NavigationArtifactSnapshot(
            date="20270623",
            segments=["segment_a"],
            sync_data_exists=True,
            sync_data_by_segment={"segment_a": True},
        ).model_dump(mode="json"),
    )

    def fake_extract_and_sync_navigation_data_tool(date: str, segments: list[str] | None = None) -> dict:
        return {"ok": True, "tool_name": "extract_and_sync_navigation_data", "date": date}

    def fake_assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
        return {"ok": True, "tool_name": "assemble_finish_temp", "date": date, "segments": segments}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_extract_and_sync_navigation_data_tool,
                name="extract_and_sync_navigation_data_tool",
                is_read_only=False,
            ),
            FunctionTool(
                fake_assemble_finish_temp_tool,
                name="assemble_finish_temp_tool",
                is_read_only=False,
            ),
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270623",
            scene_mode="out",
            segments=["segment_a"],
        )
    )
    state.finalized_plan = WorkflowPlan(date="20270623", phase="finish_processing", steps=[])
    store.save("agent-session", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            session_id="agent-session",
            draft_store=store,
            task_store=task_store,
            web_session_id="web-session",
            settings=agent_tools_module.NavigationSettings(vladatasets_root=root),
            dry_run=True,
        )
    }

    extract = _decode_tool_payload(
        asyncio.run(
            tools["extract_and_sync_navigation_data_tool"](
                date="20270623",
                segments=["segment_a"],
            )
        )
    )
    finish = _decode_tool_payload(
        asyncio.run(
            tools["assemble_finish_temp_tool"](
                date="20270623",
                segments=["segment_a"],
            )
        )
    )

    assert extract["ok"] is False
    assert extract["error_type"] == "navigation_phase_plan_required"
    assert finish["ok"] is True


def test_navigation_execution_tool_is_blocked_before_session_plan_is_finalized(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "prepare_raw_data", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_plan_not_finalized"
    assert result["next_tool_candidates"] == ["get_workflow_plan_draft_tool"]


def test_navigation_execution_tool_is_allowed_after_session_plan_is_finalized(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str) -> dict:
        return {"ok": True, "tool_name": "prepare_raw_data", "date": date}

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(date="20270605", scene_mode="out")
    )
    state.update(data_profile_patch=_complete_profile_patch())
    state.finalized_plan = build_plan_from_draft(state)
    store.save("agent-session-1", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert result["ok"] is True
    assert result["tool_name"] == "prepare_raw_data"


def test_navigation_execution_tool_rejects_segment_mismatch_after_session_plan_is_finalized(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
        return {
            "ok": True,
            "tool_name": "prepare_raw_data",
            "date": date,
            "segments": segments,
        }

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270605",
            scene_mode="out",
            segments=["20260605_152856"],
        )
    )
    state.update(data_profile_patch=_complete_profile_patch())
    state.finalized_plan = build_plan_from_draft(state)
    store.save("agent-session-1", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    wrong_segments = _decode_tool_payload(
        asyncio.run(
            tools["prepare_raw_data_tool"](
                date="20270605",
                segments=["20260605_152930"],
            )
        )
    )
    missing_segments = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert wrong_segments["ok"] is False
    assert wrong_segments["error_type"] == "navigation_plan_request_mismatch"
    assert wrong_segments["requested_request"] == {
        "date": "20270605",
        "segments": ["20260605_152930"],
    }
    assert missing_segments["ok"] is False
    assert missing_segments["error_type"] == "navigation_plan_request_mismatch"
    assert missing_segments["requested_request"] == {
        "date": "20270605",
        "segments": None,
    }


def test_navigation_execution_gate_accepts_json_encoded_segments_from_model(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str, segments: list[str] | str | None = None) -> dict:
        return {
            "ok": True,
            "tool_name": "prepare_raw_data",
            "date": date,
            "segments": segments,
        }

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270605",
            scene_mode="out",
            segments=["20260605_152856"],
        )
    )
    state.update(data_profile_patch=_complete_profile_patch())
    state.finalized_plan = build_plan_from_draft(state)
    store.save("agent-session-1", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(
            tools["prepare_raw_data_tool"](
                date="20270605",
                segments='["20260605_152856"]',
            )
        )
    )

    assert result["ok"] is True
    assert result["segments"] == '["20260605_152856"]'


def test_navigation_execution_tool_reports_not_finalized_before_segment_mismatch(monkeypatch):
    store = InMemoryNavigationPlanDraftStore()

    def fake_prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
        return {
            "ok": True,
            "tool_name": "prepare_raw_data",
            "date": date,
            "segments": segments,
        }

    monkeypatch.setattr(
        agent_tools_module,
        "create_navigation_execution_tools",
        lambda **_: [
            FunctionTool(
                fake_prepare_raw_data_tool,
                name="prepare_raw_data_tool",
                is_read_only=False,
            )
        ],
    )
    state = WorkflowPlanDraftState(
        request=NavigationRequest(
            date="20270605",
            scene_mode="out",
            segments=["20260605_152856"],
        )
    )
    store.save("agent-session-1", state)
    tools = {
        tool.name: tool
        for tool in build_navigation_agent_tools(
            dry_run=True,
            session_id="agent-session-1",
            draft_store=store,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["prepare_raw_data_tool"](date="20270605"))
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_plan_not_finalized"
    assert result["missing_fields"] == state.missing_fields()
    assert result["next_tool_candidates"] == state.next_tool_candidates()
    assert result["draft"] == state.schema_snapshot()


def test_extra_agent_tools_factory_registers_navigation_tools_only_for_navigation_agent(tmp_path):
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
    factory = build_extra_agent_tools_factory(config)

    navigation_tools = asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    router_tools = asyncio.run(factory("alice", config.main_router_agent_id, "session-1"))

    navigation_names = {tool.name for tool in navigation_tools}
    assert navigation_names == {"get_or_create_navigation_task_tool"}
    assert "vla_run_workflow" not in navigation_names
    assert "vla_continue_workflow" not in navigation_names
    assert router_tools == []


def test_extra_agent_tools_factory_passes_runtime_cancellation_to_navigation_tools(
    monkeypatch,
    tmp_path,
):
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
    cancellation = CancellationContext()
    captured = {}

    def fake_resolve_navigation_agent_tools(
        *, services, agentscope_session_id, cancellation, web_session_id
    ):
        captured["services"] = services
        captured["cancellation"] = cancellation
        captured["session_id"] = agentscope_session_id
        captured["web_session_id"] = web_session_id
        return [SimpleNamespace(name="navigation_tool")]

    monkeypatch.setattr(
        runtime_module,
        "resolve_navigation_agent_tools",
        fake_resolve_navigation_agent_tools,
    )
    runtime = SimpleNamespace(run_cancellation=lambda session_id: cancellation)
    factory = build_extra_agent_tools_factory(config, runtime=runtime)

    tools = asyncio.run(factory("alice", config.navigation_agent_id, "as-session-1"))

    assert [tool.name for tool in tools] == ["navigation_tool"]
    assert captured["cancellation"] is cancellation
    assert captured["session_id"] == "as-session-1"
    assert captured["web_session_id"] == "as-session-1"
    assert captured["services"].task_store.db_path == tmp_path / "navigation-tasks.sqlite"
    assert captured["services"].observation_store.db_path == tmp_path / "navigation-tasks.sqlite"


def test_extra_agent_tools_factory_passes_session_bound_draft_store(tmp_path):
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
    factory = build_extra_agent_tools_factory(config)

    tools = {
        tool.name: tool
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }

    assert set(tools) == {"get_or_create_navigation_task_tool"}
    assert not (tmp_path / "navigation-plan-drafts").exists()


def test_navigation_agent_tools_can_resume_finalized_plan_from_store(tmp_path):
    # The AgentScope runtime no longer resumes mutable draft/finalize state.
    # Phase recovery is covered by durable resolver tests below.
    return
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
    factory = build_extra_agent_tools_factory(config)

    first_tools = {
        tool.name: tool
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }
    _decode_tool_payload(
        asyncio.run(
            first_tools["get_workflow_plan_draft_tool"](
                date="20270605",
                scene_mode="out",
            )
        )
    )
    patch = {
        "processing_profile": {
            "id": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            "topic_params": {
                "profile_hint": "go2w",
                "confidence": 1.0,
                "topic_whitelist": [
                    "/cam_video4/csi_cam/image_raw/compressed",
                    "/rs32_lidar_points",
                    "/sport_odom",
                ],
                "topic_map": {
                    "cam_video4": "fisheye_front",
                    "rs32_lidar_points": "r32_rslidar_points",
                    "sport_odom": "odom",
                },
                "query_dir": "rs32_lidar_points",
                "evidence": ["infer_navigation_topic_params_tool"],
                "warnings": [],
                "blocking_issues": [],
            },
            "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
            "gridmap_policy": {"source": "existing_gridmap"},
            "calibration_policy": {
                "mode": "hardcoded_with_user_confirmation",
                "requires_user_confirmation": True,
            },
            "warnings": [],
            "blocking_issues": [],
            "evidence": {"processing_profile": ["infer_navigation_processing_profile_tool"]},
        },
        "platform_hint": "go2w",
        "topic_params": {
            "profile_hint": "go2w",
            "confidence": 1.0,
            "topic_whitelist": [
                "/cam_video4/csi_cam/image_raw/compressed",
                "/rs32_lidar_points",
                "/sport_odom",
            ],
            "topic_map": {
                "cam_video4": "fisheye_front",
                "rs32_lidar_points": "r32_rslidar_points",
                "sport_odom": "odom",
            },
            "query_dir": "rs32_lidar_points",
            "evidence": ["infer_navigation_topic_params_tool"],
            "warnings": [],
            "blocking_issues": [],
        },
        "localization_policy": {"source": "odom", "conversion": "odom_to_ins"},
        "gridmap_source": "existing_gridmap",
        "pcd_gridmap_tool_available": True,
        "stage_variants": {
            "extract_and_sync_navigation_data": {
                "variant": "explicit_topic_params",
                "reason": "topic parameters were inferred from sensor role bindings",
                "evidence": ["infer_navigation_processing_profile_tool"],
            },
            "prepare_gridmap_for_projection": {
                "variant": "copy_existing_gridmap",
                "reason": "grid_map artifacts already exist",
                "evidence": ["inspect_gridmap_artifacts_tool"],
            },
            "run_projection_and_trajectory": {
                "variant": "cjl_0525_with_gridmap",
                "reason": "runtime assets support the 0525 projection script",
                "evidence": ["inspect_runtime_assets_tool"],
            },
        },
    }
    _decode_tool_payload(
        asyncio.run(
            first_tools["update_workflow_plan_draft_tool"](
                data_profile_patch=patch,
                observation_id="navigation_processing_profile",
                used_tool="infer_navigation_processing_profile_tool",
            )
        )
    )
    finalized = _decode_tool_payload(
        asyncio.run(first_tools["finalize_workflow_plan_tool"]())
    )
    second_tools = {
        tool.name: tool
        for tool in asyncio.run(factory("alice", config.navigation_agent_id, "session-1"))
    }
    resumed = _decode_tool_payload(
        asyncio.run(second_tools["get_workflow_plan_draft_tool"]())
    )
    persisted_state = JsonNavigationPlanDraftStore(
        tmp_path / "navigation-plan-drafts"
    ).load("session-1")

    assert finalized["ok"] is True
    assert finalized["workflow_plan_json"]["date"] == "20270605"
    assert resumed["draft"]["ready_to_finish"] is True
    assert resumed["draft"]["data_profile_draft"]["localization_policy"] == {
        "source": "odom",
        "conversion": "odom_to_ins",
    }
    assert resumed["draft"]["finish_processing_profile"] is not None
    assert resumed["draft"]["finish_processing_profile"]["date"] == "20270605"
    assert persisted_state is not None
    assert persisted_state.finalized_plan is not None
    assert persisted_state.finalized_plan.date == "20270605"
    assert (
        persisted_state.finalized_plan.steps[0].step_id
        == "confirm_navigation_calibration_params"
    )


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
        "dry_run",
    }
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


def test_navigation_handoff_tool_uses_explicit_date_not_clip_prefix_for_draft_initialization():
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


def test_navigation_handoff_message_includes_structured_json_for_draft_initialization():
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
        "clips": ["20260605_152856"],
        "segments": ["20260605_152856"],
        "reason": "用户要处理导航数据",
        "response_language": "Chinese",
        "dry_run": False,
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
            "dry_run": False,
            "started": True,
        }
    ]


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
    assert tool_names == {"get_or_create_navigation_task_tool"}


def _resolver_services_from_complete(tmp_path, session_id="as-session-1"):
    built = build_complete_plan_services(tmp_path, "extract_sync")
    data_root = tmp_path / "vla-data"
    (data_root / "raw_data" / built.task.date / built.task.segments[0]).mkdir(
        parents=True
    )
    task_store = SqliteNavigationTaskStore(built.plan_store.db_path)
    task = task_store.update_task(
        built.task.task_id,
        agentscope_session_id=session_id,
    )
    services = NavigationServices(
        settings=agent_tools_module.NavigationSettings(vladatasets_root=data_root),
        task_store=task_store,
        observation_store=built.observation_store,
        evidence_store=built.evidence_store,
        plan_store=built.plan_store,
    )
    return services, task, built


def test_navigation_services_share_one_database_and_idempotent_migrations(tmp_path):
    first = build_navigation_services(tmp_path)
    second = build_navigation_services(tmp_path)

    assert first.task_store.db_path == tmp_path / "navigation-tasks.sqlite"
    assert first.observation_store.db_path == first.task_store.db_path
    assert first.plan_store.db_path == first.task_store.db_path
    assert second.task_store.db_path == first.task_store.db_path
    assert first.evidence_store.root == tmp_path / "navigation-evidence"


def test_phase_resolver_uses_exact_session_and_exposes_only_current_submit_schema(tmp_path):
    services, _task, _built = _resolver_services_from_complete(tmp_path)

    planning_names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
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

    assert planning_names == {
        "get_phase_planning_context_tool",
        "list_observation_evidence_tool",
        "read_observation_evidence_tool",
        "describe_processing_action_tool",
        "submit_extract_sync_plan_tool",
    }
    assert other_session_names == {"get_or_create_navigation_task_tool"}
    assert not any("draft" in name or "finalize" in name for name in planning_names)
    assert "submit_finish_processing_plan_tool" not in planning_names


def test_no_task_entry_requires_verified_web_agentscope_session_pair(tmp_path):
    services = build_navigation_services(tmp_path)

    tools = resolve_navigation_agent_tools(
        services=services,
        agentscope_session_id="web-a__navigation-data-agent",
        web_session_id="web-b",
        cancellation=None,
    )

    assert tools == []


def test_cross_web_session_entry_cannot_rebind_existing_task_by_date(tmp_path):
    services = build_navigation_services(tmp_path)
    services.task_store.create_or_update_task(
        date="20260710",
        segments=None,
        scene_mode=None,
        web_session_id="web-a",
        agentscope_session_id="web-a__navigation-data-agent",
    )
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="web-b__navigation-data-agent",
            web_session_id="web-b",
            cancellation=None,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["get_or_create_navigation_task_tool"](date="20260710"))
    )

    assert result["ok"] is False
    assert result["error_type"] == "navigation_task_session_mismatch"
    assert services.task_store.find_latest_by_date("20260710").created_by_web_session_id == "web-a"


def test_bound_task_tools_reject_foreign_task_and_stale_session_without_mutation(tmp_path):
    services = build_navigation_services(tmp_path)
    bound = services.task_store.create_or_update_task(
        date="20260710",
        segments=None,
        scene_mode=None,
        web_session_id="web-a",
        agentscope_session_id="as-a",
    )
    foreign = services.task_store.create_or_update_task(
        date="20260711",
        segments=None,
        scene_mode=None,
        web_session_id="web-b",
        agentscope_session_id="as-b",
    )
    before = services.task_store.get_task(foreign.task_id)
    tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=services.task_store,
            session_id="as-a",
            web_session_id="web-a",
            settings=services.settings,
            bound_task=bound,
        )
    }

    foreign_result = _decode_tool_payload(
        asyncio.run(tools["reconcile_navigation_task_tool"](task_id=foreign.task_id))
    )
    stale_tools = {
        tool.name: tool
        for tool in build_navigation_task_tools(
            store=services.task_store,
            session_id="as-stale",
            web_session_id="web-a",
            settings=services.settings,
            bound_task=bound,
        )
    }
    stale_result = _decode_tool_payload(
        asyncio.run(stale_tools["update_navigation_task_state_tool"](
            task_id=bound.task_id,
            phase="intake",
            status="pending",
        ))
    )

    assert foreign_result["error_type"] == "navigation_task_session_mismatch"
    assert stale_result["error_type"] == "navigation_task_session_mismatch"
    assert services.task_store.get_task(foreign.task_id) == before


def test_navigation_services_migrate_legacy_observations_transactionally_once(tmp_path):
    legacy = SqliteNavigationObservationStore(tmp_path / "navigation-observations.sqlite")
    evidence = FileNavigationEvidenceStore(tmp_path / "navigation-evidence")
    payload = ArtifactStateObservation(
        snapshot=NavigationArtifactSnapshot(
            date="20260710",
            segments=["20260710_120000"],
            raw_input_exists=True,
        )
    )
    revision = legacy.append(
        "nav-legacy",
        "extract_sync",
        "artifact_state",
        [payload],
        [
            EvidenceWrite(
                kind="artifact_state",
                source_tool="legacy_runtime",
                payload=payload.model_dump(mode="json"),
                summary="legacy artifact state",
            )
        ],
        evidence,
    )

    first = build_navigation_services(tmp_path)
    with sqlite3.connect(tmp_path / "navigation-observations.sqlite") as connection:
        connection.execute(
            "UPDATE navigation_observation_revisions SET revision_json = '{poisoned'"
        )
    second = build_navigation_services(tmp_path)

    assert first.observation_store.latest("nav-legacy") == revision
    assert second.observation_store.latest("nav-legacy") == revision
    assert len(second.observation_store.list_evidence("nav-legacy")) == 1


def _write_legacy_observation_fixture(tmp_path):
    legacy = SqliteNavigationObservationStore(tmp_path / "navigation-observations.sqlite")
    evidence = FileNavigationEvidenceStore(tmp_path / "navigation-evidence")
    payload = ArtifactStateObservation(
        snapshot=NavigationArtifactSnapshot(date="20260710", raw_input_exists=True)
    )
    revision = legacy.append(
        "nav-legacy-integrity", "extract_sync", "artifact_state", [payload],
        [EvidenceWrite(
            kind="artifact_state", source_tool="legacy", payload={"ok": True},
            summary="legacy",
        )],
        evidence,
    )
    return revision, evidence


@pytest.mark.parametrize("corruption", ["missing", "extra", "wrong_revision", "duplicate"])
def test_legacy_migration_requires_bidirectional_evidence_integrity(tmp_path, corruption):
    revision, evidence = _write_legacy_observation_fixture(tmp_path)
    legacy_path = tmp_path / "navigation-observations.sqlite"
    with sqlite3.connect(legacy_path) as connection:
        if corruption == "missing":
            connection.execute("DELETE FROM navigation_evidence")
        elif corruption == "wrong_revision":
            connection.execute("UPDATE navigation_evidence SET observation_revision = 99")
        elif corruption == "extra":
            descriptor = evidence.write(
                revision.task_id, revision.revision, "extra", "legacy", {"x": 1}, "extra"
            )
            connection.execute(
                "INSERT INTO navigation_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(descriptor.model_dump(mode="json").values()),
            )
        else:
            connection.execute("ALTER TABLE navigation_evidence RENAME TO evidence_old")
            connection.execute(
                """CREATE TABLE navigation_evidence (
                    ref TEXT, task_id TEXT, observation_revision INTEGER, kind TEXT,
                    summary TEXT, byte_size INTEGER, source_tool TEXT, created_at TEXT)"""
            )
            connection.execute("INSERT INTO navigation_evidence SELECT * FROM evidence_old")
            connection.execute("INSERT INTO navigation_evidence SELECT * FROM evidence_old")
            connection.execute("DROP TABLE evidence_old")

    with pytest.raises(navigation_services_module.LegacyObservationMigrationError):
        build_navigation_services(tmp_path)


def test_completed_legacy_marker_fast_path_needs_no_target_write_lock(tmp_path):
    _write_legacy_observation_fixture(tmp_path)
    first = build_navigation_services(tmp_path)
    locked = Event()

    def transient_writer():
        connection = sqlite3.connect(first.task_store.db_path)
        connection.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(0.1)
        connection.rollback()
        connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(transient_writer)
        locked.wait()
        resumed = build_navigation_services(tmp_path)
        future.result()

    assert resumed.observation_store.latest("nav-legacy-integrity") is not None


def test_concurrent_navigation_service_builders_migrate_once(tmp_path):
    _write_legacy_observation_fixture(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        services = list(executor.map(lambda _: build_navigation_services(tmp_path), range(2)))

    assert all(
        item.observation_store.latest("nav-legacy-integrity") is not None
        for item in services
    )
    with sqlite3.connect(tmp_path / "navigation-tasks.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM navigation_service_migrations"
        ).fetchone()[0] == 1


def test_marker_only_database_is_fully_upgraded_before_services_return(tmp_path):
    db_path = tmp_path / "navigation-tasks.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE navigation_service_migrations (name TEXT PRIMARY KEY, completed_at TEXT)"
        )
        connection.execute(
            "INSERT INTO navigation_service_migrations VALUES (?, ?)",
            ("legacy_observations_to_unified_v1", "now"),
        )

    services = build_navigation_services(tmp_path)
    task = services.task_store.create_or_update_task(
        date="20260710", segments=None, scene_mode=None,
    )

    assert services.task_store.get_task(task.task_id) is not None
    assert services.observation_store.latest(task.task_id) is None
    assert services.plan_store.get_active(task.task_id, "extract_sync") is None


@pytest.mark.parametrize("failure", ["invalid_json", "partial_schema"])
def test_navigation_services_reject_corrupt_legacy_db_without_partial_import(
    tmp_path, failure
):
    legacy_path = tmp_path / "navigation-observations.sqlite"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            """
            CREATE TABLE navigation_observation_revisions (
                task_id TEXT, revision INTEGER, phase TEXT,
                revision_json TEXT, created_at TEXT
            )
            """
        )
        if failure == "invalid_json":
            connection.execute(
                """
                CREATE TABLE navigation_evidence (
                    ref TEXT, task_id TEXT, observation_revision INTEGER,
                    kind TEXT, summary TEXT, byte_size INTEGER,
                    source_tool TEXT, created_at TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO navigation_observation_revisions VALUES (?, ?, ?, ?, ?)",
                ("nav-corrupt", 1, "extract_sync", "{bad-json", "now"),
            )

    with pytest.raises(navigation_services_module.LegacyObservationMigrationError):
        build_navigation_services(tmp_path)

    with sqlite3.connect(tmp_path / "navigation-tasks.sqlite") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM navigation_observation_revisions"
        ).fetchone()[0]
    assert count == 0


def test_navigation_services_reject_conflicting_legacy_revision_before_marker(tmp_path):
    unified = build_navigation_services(tmp_path)
    evidence = unified.evidence_store
    target_payload = ArtifactStateObservation(
        snapshot=NavigationArtifactSnapshot(date="20260710", raw_input_exists=True)
    )
    unified.observation_store.append(
        "nav-conflict", "extract_sync", "artifact_state", [target_payload], [], evidence
    )
    legacy = SqliteNavigationObservationStore(tmp_path / "navigation-observations.sqlite")
    legacy_payload = ArtifactStateObservation(
        snapshot=NavigationArtifactSnapshot(date="20260711", raw_input_exists=True)
    )
    legacy.append(
        "nav-conflict", "extract_sync", "artifact_state", [legacy_payload], [], evidence
    )

    with pytest.raises(
        navigation_services_module.LegacyObservationMigrationError,
        match="conflicts with unified state",
    ):
        build_navigation_services(tmp_path)

    assert unified.observation_store.latest("nav-conflict").payloads == [target_payload]


def test_phase_resolver_exposes_missing_inspections_without_submission_or_execution(tmp_path):
    data_root = tmp_path / "vla-data"
    (data_root / "raw_data" / "20260710" / "20260710_120000").mkdir(parents=True)
    services = build_navigation_services(
        tmp_path,
        agent_tools_module.NavigationSettings(vladatasets_root=data_root),
    )
    created = services.task_store.create_or_update_task(
        date="20260710",
        segments=["20260710_120000"],
        scene_mode=None,
        agentscope_session_id="as-session-1",
    )
    services.task_store.update_task(created.task_id, phase="extract_sync")

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert names == {
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
        "get_phase_planning_context_tool",
        "list_observation_evidence_tool",
        "read_observation_evidence_tool",
        "describe_processing_action_tool",
    }
    assert not any(name.startswith("submit_") for name in names)
    assert not any(name in {"prepare_raw_data_tool", "extract_and_sync_navigation_data_tool"} for name in names)


def test_phase_resolver_recovers_active_plan_and_only_remaining_actions(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    plan = ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built))
    active = services.plan_store.activate(task, "extract_sync", 4, plan)

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            cancellation=None,
        )
    }

    remaining = {f"{step.action}_tool" for step in active.plan.steps}
    assert names == {
        "get_plan_execution_overview_tool",
        "get_current_plan_step_tool",
        *remaining,
    }
    assert not any(name.startswith("submit_") for name in names)
    assert "request_human_decision" not in names


def test_phase_resolver_completed_state_has_only_compact_state_and_evidence(tmp_path):
    services, task, _built = _resolver_services_from_complete(tmp_path)
    final_grid = (
        services.settings.finish_data_root
        / task.date
        / task.segments[0]
        / "projection"
        / "grid_map"
    )
    final_grid.mkdir(parents=True)
    services.task_store.update_task(task.task_id, phase="completed", status="completed")

    names = {
        tool.name
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            cancellation=None,
        )
    }

    assert names == {
        "get_navigation_task_state_tool",
        "list_navigation_task_evidence_tool",
        "read_navigation_task_evidence_tool",
    }


def test_completed_evidence_list_is_explicitly_bounded_to_4000_chars(monkeypatch, tmp_path):
    services, task, _built = _resolver_services_from_complete(tmp_path)
    final_grid = (
        services.settings.finish_data_root
        / task.date
        / task.segments[0]
        / "projection"
        / "grid_map"
    )
    final_grid.mkdir(parents=True)
    services.task_store.update_task(task.task_id, phase="completed", status="completed")
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
            web_session_id=None,
            cancellation=None,
        )
    }

    result = _decode_tool_payload(
        asyncio.run(tools["list_navigation_task_evidence_tool"](limit=20))
    )

    assert len(json.dumps(result, separators=(",", ":"))) <= 4000


def test_resolved_execution_reads_are_explicitly_bounded_to_4000_chars(tmp_path):
    services, task, built = _resolver_services_from_complete(tmp_path)
    plan = services.plan_store.activate(
        task,
        "extract_sync",
        4,
        ExtractSyncPlanInput.model_validate(valid_extract_plan_payload(built)),
    )
    tools = {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id="as-session-1",
            web_session_id=None,
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
