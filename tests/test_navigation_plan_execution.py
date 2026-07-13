from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

import vla_data_juicer_agents.navigation.plan_execution as plan_execution
from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.models import ToolResult
from vla_data_juicer_agents.navigation.observation_models import (
    CalibrationInventoryObservation,
    EvidenceWrite,
    GridmapArtifactsObservation,
    RuntimeAssetsObservation,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
)
from vla_data_juicer_agents.navigation.plan_store import (
    ActivePlanExecutionConflict,
    SqliteNavigationPlanRepository,
    StepClaimOutcome,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.runtime.agentscope_runtime import (
    _enrich_plan_human_decision_event,
    _human_decision_payload_from_tool_call,
)


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
        return json.loads(
            "".join(
                block.text
                for block in payload
                if hasattr(block, "text") and isinstance(block.text, str)
            )
        )
    raise TypeError(f"unsupported tool payload: {type(payload)!r}")


def call_tool(tool, **arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        return _decode_tool_payload(payload)

    return asyncio.run(_call())


def ok_result(tool_name: str, *, blob: str = "") -> ToolResult:
    return ToolResult(
        ok=True,
        tool_name=tool_name,
        message="completed",
        details={"full_blob": blob},
    )


def failed_result(tool_name: str) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        message="failed",
        details={"error_type": "processing_failed"},
    )


def extract_plan(*, two_steps: bool = False) -> ExtractSyncPlanInput:
    steps = []
    if two_steps:
        steps.append(
            {
                "step_id": "prepare",
                "action": "prepare_raw_data",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": [],
            }
        )
    steps.append(
        {
            "step_id": "sync",
            "action": "extract_and_sync_navigation_data",
            "variant": "explicit_topic_params",
            "arguments": {"processes_num": 8},
            "depends_on": ["prepare"] if two_steps else [],
            "failure_policy": "stop",
            "decision_refs": ["topic_selection"],
        }
    )
    return ExtractSyncPlanInput.model_validate(
        {
            "decisions": {
                "sensor_bindings": {
                    "bindings": {
                        "fisheye_front": "/camera/front/image",
                        "lidar": "/lidar/points",
                        "odom": "/localization/odom",
                    },
                    "reason": "observed",
                    "evidence_refs": ["evidence:sensors"],
                },
                "topic_selection": {
                    "topic_whitelist": ["/camera/front/image", "/lidar/points"],
                    "topic_map": {
                        "/camera/front/image": "fisheye_front",
                        "/lidar/points": "r32_rslidar_points",
                    },
                    "query_dir": "r32_rslidar_points",
                    "reason": "observed",
                    "evidence_refs": ["evidence:topics"],
                },
                "time_sync": {
                    "reference_sensor": "lidar",
                    "method": "nearest_timestamp",
                    "tolerance_ms": 50,
                    "reason": "observed",
                    "evidence_refs": ["evidence:timing"],
                },
            },
            "steps": steps,
        }
    )


def finish_plan(sensor_source: str) -> FinishProcessingPlanInput:
    common = {"depends_on": [], "failure_policy": "stop", "decision_refs": []}
    return FinishProcessingPlanInput.model_validate(
        {
            "decisions": {
                "localization": {
                    "source": "odom",
                    "conversion": "odom_to_ins",
                    "reason": "observed",
                    "evidence_refs": ["evidence:localization"],
                },
                "gridmap": {
                    "source": "existing_gridmap",
                    "reason": "observed",
                    "evidence_refs": ["evidence:gridmap"],
                },
                "calibration": {
                    "mode": "hardcoded_with_user_confirmation",
                    "selected_sensor_source": sensor_source,
                    "requires_user_confirmation": True,
                    "reason": "observed",
                    "evidence_refs": ["evidence:calibration"],
                },
            },
            "steps": [
                {
                    **common,
                    "step_id": "confirm",
                    "action": "confirm_navigation_calibration_params",
                    "variant": "default",
                    "arguments": {},
                    "decision_refs": ["calibration"],
                },
                {
                    **common,
                    "step_id": "assemble",
                    "action": "assemble_finish_temp",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": ["confirm"],
                    "decision_refs": ["calibration"],
                },
                {
                    **common,
                    "step_id": "preprocess",
                    "action": "run_noobscene_preprocessing",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": ["assemble"],
                    "decision_refs": ["localization"],
                },
                {
                    **common,
                    "step_id": "annotate",
                    "action": "run_initial_annotation_gui",
                    "variant": "human_gui",
                    "arguments": {},
                    "depends_on": ["preprocess"],
                },
                {
                    **common,
                    "step_id": "tracking",
                    "action": "run_tracking",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": ["annotate"],
                },
                {
                    **common,
                    "step_id": "gridmap",
                    "action": "prepare_gridmap_for_projection",
                    "variant": "copy_existing_gridmap",
                    "arguments": {},
                    "depends_on": ["tracking"],
                    "decision_refs": ["gridmap"],
                },
                {
                    **common,
                    "step_id": "projection",
                    "action": "run_projection_and_trajectory",
                    "variant": "cjl_0525_with_gridmap",
                    "arguments": {},
                    "depends_on": ["gridmap"],
                    "decision_refs": ["localization", "gridmap"],
                },
                {
                    **common,
                    "step_id": "validate",
                    "action": "validate_navigation_outputs",
                    "variant": "expect_gridmap",
                    "arguments": {},
                    "depends_on": ["projection"],
                    "decision_refs": ["gridmap"],
                },
            ],
        }
    )


@dataclass
class Services:
    task: NavigationTask
    task_store: SqliteNavigationTaskStore
    observation_store: SqliteNavigationObservationStore
    evidence_store: FileNavigationEvidenceStore
    plan_store: SqliteNavigationPlanRepository
    settings: NavigationSettings
    plan: object

    def tools(self, *, cancellation: CancellationContext | None = None):
        return {
            tool.name: tool
            for tool in plan_execution.build_plan_bound_execution_tools(
                task=self.task,
                plan_store=self.plan_store,
                evidence_store=self.evidence_store,
                settings=self.settings,
                dry_run=True,
                cancellation=cancellation,
            )
        }


def build_services(tmp_path: Path, *, two_steps: bool = False) -> Services:
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    segment = "20260710_120000"
    (settings.raw_data_root / "20260710" / segment).mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20260710",
        segments=[segment],
        scene_mode=None,
        dry_run=True,
    )
    task = task_store.update_task(task.task_id, phase="extract_sync", status="pending")
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(task, "extract_sync", 1, extract_plan(two_steps=two_steps))
    return Services(
        task=task,
        task_store=task_store,
        observation_store=observation_store,
        evidence_store=evidence_store,
        plan_store=plan_store,
        settings=settings,
        plan=plan,
    )


def test_extract_wrapper_loads_canonical_topics_from_plan(monkeypatch, tmp_path):
    services = build_services(tmp_path)
    captured = {}
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: captured.update(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["ok"] is True
    assert captured == {
        "date": services.task.date,
        "segments": services.task.segments,
        "processes_num": 8,
        "topic_whitelist": services.plan.plan.decisions.topic_selection.topic_whitelist,
        "topic_map": services.plan.plan.decisions.topic_selection.topic_map,
        "query_dir": services.plan.plan.decisions.topic_selection.query_dir,
        "settings": services.settings,
        "dry_run": True,
    }


def test_plan_bound_tools_expose_only_ids_and_all_remaining_distinct_actions(tmp_path):
    services = build_services(tmp_path, two_steps=True)
    tools = services.tools()

    assert set(tools) == {
        "prepare_raw_data_tool",
        "extract_and_sync_navigation_data_tool",
    }
    for tool in tools.values():
        assert set(tool.input_schema["properties"]) == {"plan_id", "step_id"}
        assert set(tool.input_schema["required"]) == {"plan_id", "step_id"}
        assert tool.input_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("mutation", "tool_name", "step_id", "error_type"),
    [
        ("wrong_plan", "prepare_raw_data_tool", "prepare", "plan_not_active"),
        ("non_current", "extract_and_sync_navigation_data_tool", "sync", "step_not_current"),
        ("action_mismatch", "extract_and_sync_navigation_data_tool", "prepare", "step_action_mismatch"),
        ("inactive", "prepare_raw_data_tool", "prepare", "plan_not_active"),
    ],
)
def test_execution_gate_rejects_without_invoking(
    monkeypatch,
    tmp_path,
    mutation,
    tool_name,
    step_id,
    error_type,
):
    services = build_services(tmp_path, two_steps=True)
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        lambda **kwargs: invoked.append(kwargs) or ok_result("prepare_raw_data"),
    )
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )
    plan_id = services.plan.plan_id
    if mutation == "wrong_plan":
        plan_id = "nav_plan_missing"
    elif mutation == "inactive":
        services.plan_store.activate(
            services.task,
            "extract_sync",
            1,
            extract_plan(two_steps=True),
        )

    result = call_tool(services.tools()[tool_name], plan_id=plan_id, step_id=step_id)

    assert result["ok"] is False
    assert result["error_type"] == error_type
    assert invoked == []
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000


def test_execution_gate_rejects_unmet_dependency_without_invoking(monkeypatch, tmp_path):
    services = build_services(tmp_path, two_steps=True)
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )
    with sqlite3.connect(services.plan_store.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET sequence = 2 "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (services.plan.plan_id,),
        )

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["error_type"] == "step_dependencies_unmet"
    assert invoked == []


def test_changed_input_precondition_records_evidence_without_artifact_reconciliation(
    monkeypatch,
    tmp_path,
):
    from vla_data_juicer_agents.navigation import artifact_inspection, task_reconciliation

    services = build_services(tmp_path)
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )
    monkeypatch.setattr(
        artifact_inspection,
        "build_navigation_artifact_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("artifact snapshot called")),
    )
    monkeypatch.setattr(
        plan_execution,
        "build_navigation_artifact_snapshot",
        artifact_inspection.build_navigation_artifact_snapshot,
        raising=False,
    )
    monkeypatch.setattr(
        task_reconciliation,
        "reconcile_navigation_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("task reconcile called")),
    )
    monkeypatch.setattr(
        plan_execution,
        "reconcile_navigation_task",
        task_reconciliation.reconcile_navigation_task,
        raising=False,
    )
    raw_root = services.settings.raw_data_root / services.task.date
    for child in raw_root.iterdir():
        child.rmdir()
    raw_root.rmdir()

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["ok"] is False
    assert result["error_type"] == "input_precondition_changed"
    assert result["next_action"] == "submit_complete_plan"
    assert result["result_ref"]
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000
    assert invoked == []
    assert services.plan_store.get(services.plan.plan_id).status == "invalidated"
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "needs_replan"
    evidence = services.evidence_store.read(services.task.task_id, result["result_ref"])
    assert evidence["data"]["missing_inputs"]


@pytest.mark.parametrize(
    ("decision_source", "step_variant", "removed_input"),
    [
        ("existing_gridmap", "copy_existing_gridmap", "gridmap_json"),
        ("generated_from_pcd", "generate_from_pcd", "pcd_source"),
        ("generated_from_pcd", "generate_from_pcd", "pcd_runtime"),
        ("projection_ready", "skip_if_projection_ready", "gridmap_json"),
    ],
)
def test_gridmap_step_rejects_drift_in_exact_observed_inputs(
    monkeypatch,
    tmp_path,
    decision_source,
    step_variant,
    removed_input,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    date, segment = "20260710", "segment-a"
    sensor_source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / sensor_source).mkdir(parents=True)
    gridmap_dir = tmp_path / "observed" / "grid_map"
    gridmap_dir.mkdir(parents=True)
    gridmap_json = gridmap_dir / "map.json"
    gridmap_json.write_text("{}", encoding="utf-8")
    pcd_source = tmp_path / "observed" / "source.pcd"
    pcd_source.write_text("pcd", encoding="utf-8")
    settings.pcd_to_grid_script.parent.mkdir(parents=True)
    settings.pcd_to_grid_script.write_text("# runtime", encoding="utf-8")

    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(
        task.task_id,
        phase="finish_processing",
        status="pending",
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation_store = SqliteNavigationObservationStore(db_path)
    observation = observation_store.append(
        task.task_id,
        "gridmap_artifacts",
        [
            GridmapArtifactsObservation(
                existing_gridmap_paths=[str(gridmap_dir)]
                if decision_source != "generated_from_pcd"
                else [],
                pcd_sources=[str(pcd_source)]
                if decision_source == "generated_from_pcd"
                else [],
                projection_ready=decision_source == "projection_ready",
            ),
            RuntimeAssetsObservation(
                pcd_gridmap_tool_available=True,
                manual_annotation_gui_available=True,
                projection_variants={"cjl_0525_with_gridmap": True},
            ),
            CalibrationInventoryObservation(sensor_sources=[sensor_source]),
        ],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_payload = finish_plan(sensor_source).model_dump(mode="json")
    plan_payload["decisions"]["gridmap"]["source"] = decision_source
    next(
        step for step in plan_payload["steps"] if step["step_id"] == "gridmap"
    )["variant"] = step_variant
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        FinishProcessingPlanInput.model_validate(plan_payload),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' "
            "WHERE plan_id = ? AND step_id IN "
            "('confirm', 'assemble', 'preprocess', 'annotate', 'tracking')",
            (plan.plan_id,),
        )

    removed = {
        "gridmap_json": gridmap_json,
        "pcd_source": pcd_source,
        "pcd_runtime": settings.pcd_to_grid_script,
    }[removed_input]
    removed.unlink()
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "prepare_gridmap_for_projection",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("prepare_gridmap_for_projection"),
    )
    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
    }

    result = call_tool(
        tools["prepare_gridmap_for_projection_tool"],
        plan_id=plan.plan_id,
        step_id="gridmap",
    )

    assert result["ok"] is False
    assert result["error_type"] == "input_precondition_changed"
    assert result["result_ref"]
    assert invoked == []
    assert plan_store.get(plan.plan_id).status == "invalidated"
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "needs_replan"
    evidence = evidence_store.read(task.task_id, result["result_ref"])
    expected_missing = gridmap_dir if removed_input == "gridmap_json" else removed
    assert str(expected_missing) in evidence["data"]["missing_inputs"]


def test_plan_bound_writer_returns_compact_busy_without_invocation(monkeypatch, tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    date, segment = "20260710", "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    plan_store = SqliteNavigationPlanRepository(db_path)
    tasks = []
    plans = []
    for owner in ("web-a", "web-b"):
        agent = f"{owner}-agent"
        task = task_store.create_task_attempt(
            request="process navigation data",
            target=date,
            date=date,
            segments=[segment],
            scene_mode=None,
            dry_run=False,
            web_session_id=owner,
            agentscope_session_id=agent,
        ).task
        plan = plan_store.activate(
            task,
            "extract_sync",
            1,
            extract_plan(two_steps=True),
            expected_web_session_id=owner,
            expected_agentscope_session_id=agent,
        )
        tasks.append((task, owner, agent))
        plans.append(plan)
    assert plan_store.claim_step(
        plans[0].plan_id,
        "prepare",
        "prepare_raw_data",
        expected_web_session_id="web-a",
        expected_agentscope_session_id="web-a-agent",
    ) is StepClaimOutcome.CLAIMED
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        lambda **kwargs: invoked.append(kwargs) or ok_result("prepare_raw_data"),
    )
    task, owner, agent = tasks[1]
    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=FileNavigationEvidenceStore(tmp_path / "evidence"),
            settings=settings,
            dry_run=False,
            cancellation=None,
            web_session_id=owner,
            agentscope_session_id=agent,
        )
    }

    result = call_tool(
        tools["prepare_raw_data_tool"],
        plan_id=plans[1].plan_id,
        step_id="prepare",
    )

    assert result == {
        "ok": False,
        "error_type": "navigation_data_busy",
        "message": "An overlapping navigation data write is already running.",
        "retry": "wait_and_reinspect",
    }
    assert invoked == []


def test_finish_plan_execution_permission_does_not_infer_phase_from_artifacts(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / source).mkdir(parents=True)
    date = "20260710"
    segment = "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(task.task_id, phase="finish_processing", status="pending")
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = observation_store.append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(source),
    )

    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
    }
    permission = asyncio.run(
        tools["request_human_decision"].check_permissions(
            {"plan_id": plan.plan_id, "step_id": "confirm"},
            None,
        )
    )

    assert permission.behavior.value == "allow"
    assert plan_store.get(plan.plan_id).status == "active"
    assert task_store.get_task(task.task_id).status.value == "pending"


def test_human_decision_permission_fails_closed_when_sensor_input_disappears(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/observed/sensors"
    source_path = settings.processing_root / source
    source_path.mkdir(parents=True)
    date, segment = "20260710", "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(task.task_id, phase="finish_processing", status="pending")
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = observation_store.append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(source),
    )
    tool = next(
        candidate
        for candidate in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
        if candidate.name == "request_human_decision"
    )
    source_path.rmdir()

    permission = asyncio.run(
        tool.check_permissions({"plan_id": plan.plan_id, "step_id": "confirm"}, None)
    )

    assert permission.behavior.value == "deny"
    assert "concrete input" in permission.message
    assert plan_store.get(plan.plan_id).status == "invalidated"
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "needs_replan"
    evidence_files = list((evidence_store.root / task.task_id / str(observation.revision)).glob("*.json"))
    assert len(evidence_files) == 1
    assert json.loads(evidence_files[0].read_text(encoding="utf-8"))["missing_inputs"]


def test_waiting_human_decision_retry_enters_audited_recovery_when_input_drifts(
    monkeypatch,
    tmp_path,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/observed/sensors"
    source_path = settings.processing_root / source
    source_path.mkdir(parents=True)
    date, segment = "20260710", "segment-a"
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(
        task.task_id,
        phase="finish_processing",
        status="pending",
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = SqliteNavigationObservationStore(db_path).append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    owner, agent = "web-owner", "web-owner-agent"
    task = task_store.update_task(
        task.task_id,
        created_by_web_session_id=owner,
        latest_web_session_id=owner,
        agentscope_session_id=agent,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(source),
        expected_web_session_id=owner,
        expected_agentscope_session_id=agent,
    )
    outcomes = []
    original_prepare = plan_execution.prepare_plan_human_decision

    def capture_prepare(**kwargs):
        outcome = original_prepare(**kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(plan_execution, "prepare_plan_human_decision", capture_prepare)
    request_tool = next(
        tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
            web_session_id=owner,
            agentscope_session_id=agent,
        )
        if tool.name == "request_human_decision"
    )

    first_permission = asyncio.run(
        request_tool.check_permissions(
            {"plan_id": plan.plan_id, "step_id": "confirm"},
            None,
        )
    )
    assert first_permission.behavior.value == "allow"
    assert outcomes[-1] is None
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "waiting_user"
    source_path.rmdir()

    retry_permission = asyncio.run(
        request_tool.check_permissions(
            {"plan_id": plan.plan_id, "step_id": "confirm"},
            None,
        )
    )
    denied = outcomes[-1]

    assert retry_permission.behavior.value == "deny"
    assert denied is not None
    assert denied["error_type"] == "input_precondition_changed"
    assert denied["result_ref"]
    assert denied["recovery_required"] is True
    handoff = plan_store.get_human_decision_handoff(plan.plan_id, "confirm")
    assert handoff is not None
    assert handoff.status == "recovery_required"
    assert handoff.delivery_status == "recovery_required"
    assert handoff.recovery_reason_code == "input_precondition_changed"
    assert handoff.decision == {
        "plan_id": plan.plan_id,
        "request_state": "waiting_user",
        "step_id": "confirm",
    }
    assert plan_store.get(plan.plan_id).status == "active"
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "waiting_user"
    evidence = evidence_store.read(task.task_id, denied["result_ref"])
    assert str(source_path) in evidence["data"]["missing_inputs"]

    recovered = plan_store.quarantine_human_decision_handoff(
        plan.plan_id,
        "confirm",
        expected_web_session_id=owner,
        reason="accepted calibration source disappeared before retry",
    )

    assert recovered["handoff_status"] == "quarantined"
    assert plan_store.get(plan.plan_id).status == "invalidated"
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "needs_replan"
    assert task_store.get_task(task.task_id).status.value == "needs_replan"


def test_waiting_human_decision_retry_without_drift_remains_allowed(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / source).mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20260710",
        segments=["segment-a"],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(
        task.task_id,
        phase="finish_processing",
        status="pending",
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = SqliteNavigationObservationStore(db_path).append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(source),
    )
    arguments = {
        "task": task,
        "plan_store": plan_store,
        "evidence_store": evidence_store,
        "settings": settings,
        "plan_id": plan.plan_id,
        "step_id": "confirm",
    }

    assert plan_execution.prepare_plan_human_decision(**arguments) is None
    assert plan_execution.prepare_plan_human_decision(**arguments) is None
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "waiting_user"
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is None


def test_stale_agentscope_session_cannot_claim_plan_bound_step(monkeypatch, tmp_path):
    services = build_services(tmp_path)
    bound = services.task_store.update_task(
        services.task.task_id,
        created_by_web_session_id="web-owner",
        latest_web_session_id="web-owner",
        agentscope_session_id="as-old",
    )
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs) or ok_result("extract_and_sync_navigation_data"),
    )
    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=bound,
            plan_store=services.plan_store,
            evidence_store=services.evidence_store,
            settings=services.settings,
            dry_run=True,
            cancellation=None,
            web_session_id="web-owner",
            agentscope_session_id="as-old",
        )
    }
    services.task_store.create_or_update_task(
        date=bound.date,
        segments=bound.segments,
        scene_mode=bound.scene_mode,
        dry_run=bound.dry_run,
        web_session_id="web-owner",
        agentscope_session_id="as-new",
    )

    result = call_tool(
        tools["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["error_type"] == "navigation_task_session_mismatch"
    assert invoked == []


def test_old_wrapper_cannot_claim_after_same_session_creates_new_current_attempt(
    monkeypatch,
    tmp_path,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    date, segment = "20260710", "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    owner, agent = "web-owner", "web-owner-agent"
    task = task_store.create_task_attempt(
        request="process A",
        target=date,
        date=date,
        segments=[segment],
        scene_mode=None,
        dry_run=False,
        web_session_id=owner,
        agentscope_session_id=agent,
    ).task
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "extract_sync",
        1,
        extract_plan(two_steps=True),
        expected_web_session_id=owner,
        expected_agentscope_session_id=agent,
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "prepare_raw_data",
        lambda **kwargs: invoked.append(kwargs) or ok_result("prepare_raw_data"),
    )
    tool = next(
        candidate
        for candidate in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=False,
            cancellation=None,
            web_session_id=owner,
            agentscope_session_id=agent,
        )
        if candidate.name == "prepare_raw_data_tool"
    )
    barrier = Barrier(2)

    def create_new_current_attempt():
        barrier.wait()
        task_store.create_task_attempt(
            request="process B",
            target="20260711",
            date="20260711",
            segments=["segment-b"],
            scene_mode=None,
            dry_run=False,
            web_session_id=owner,
            agentscope_session_id=agent,
        )
        barrier.wait()

    original_verify = plan_execution.verify_plan_step_preconditions

    def gate_then_wait_for_new_attempt(**kwargs):
        result = original_verify(**kwargs)
        barrier.wait()
        barrier.wait()
        return result

    monkeypatch.setattr(
        plan_execution,
        "verify_plan_step_preconditions",
        gate_then_wait_for_new_attempt,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(create_new_current_attempt)
        result = call_tool(tool, plan_id=plan.plan_id, step_id="prepare")
        future.result()

    assert result["ok"] is False
    assert result["error_type"] == "navigation_task_session_mismatch"
    assert invoked == []
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "pending"


def test_execution_snapshot_cannot_overwrite_same_owner_rebind(monkeypatch, tmp_path):
    services = build_services(tmp_path)
    bound = services.task_store.update_task(
        services.task.task_id,
        created_by_web_session_id="web-owner",
        latest_web_session_id="web-owner",
        agentscope_session_id="as-old",
    )
    invoked = []

    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs) or ok_result("extract_and_sync_navigation_data"),
    )
    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=bound,
            plan_store=services.plan_store,
            evidence_store=services.evidence_store,
            settings=services.settings,
            dry_run=True,
            cancellation=None,
            web_session_id="web-owner",
            agentscope_session_id="as-old",
        )
    }
    services.task_store.create_or_update_task(
        date=bound.date,
        segments=bound.segments,
        scene_mode=bound.scene_mode,
        dry_run=bound.dry_run,
        web_session_id="web-owner",
        agentscope_session_id="as-new",
    )

    result = call_tool(
        tools["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["error_type"] == "navigation_task_session_mismatch"
    assert invoked == []
    current = services.task_store.get_task(bound.task_id)
    assert current.agentscope_session_id == "as-new"
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "pending"


def test_superseded_plan_cannot_claim_pending_step(tmp_path):
    services = build_services(tmp_path)
    old_plan = services.plan
    services.plan_store.activate(
        services.task,
        "extract_sync",
        1,
        extract_plan(),
    )

    claimed = services.plan_store.claim_step(
        old_plan.plan_id,
        "sync",
        "extract_and_sync_navigation_data",
    )

    assert claimed is StepClaimOutcome.NOT_CLAIMABLE
    assert services.plan_store.get_current_step(old_plan.plan_id)["step"]["status"] == "pending"


def test_stale_execution_toolkit_rejects_before_artifact_or_durable_state_mutation(
    tmp_path,
):
    services = build_services(tmp_path)
    stale_tool = services.tools()["extract_and_sync_navigation_data_tool"]
    old_plan = services.plan
    replacement = services.plan_store.activate(
        services.task,
        "extract_sync",
        2,
        extract_plan(),
    )
    task_before = services.task_store.get_task(services.task.task_id)
    replacement_before = services.plan_store.get(replacement.plan_id)
    ledger_before = services.plan_store.get_execution_overview(replacement.plan_id)
    raw_root = services.settings.raw_data_root / services.task.date
    for child in raw_root.iterdir():
        child.rmdir()
    raw_root.rmdir()

    result = call_tool(
        stale_tool,
        plan_id=old_plan.plan_id,
        step_id="sync",
    )

    assert result["ok"] is False
    assert result["error_type"] == "plan_not_active"
    assert services.task_store.get_task(services.task.task_id) == task_before
    assert services.plan_store.get(replacement.plan_id) == replacement_before
    assert services.plan_store.get_execution_overview(replacement.plan_id) == ledger_before


def test_success_persists_full_task_scoped_evidence_but_compact_ledger_and_response(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []

    def successful_action(**kwargs):
        with sqlite3.connect(services.plan_store.db_path) as connection:
            status = connection.execute(
                "SELECT status FROM navigation_task_steps WHERE plan_id = ? AND step_id = 'sync'",
                (services.plan.plan_id,),
            ).fetchone()[0]
        invoked.append((kwargs, status))
        return ok_result("extract_and_sync_navigation_data", blob="x" * 4_500)

    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        successful_action,
    )
    tool = services.tools()["extract_and_sync_navigation_data_tool"]
    result = call_tool(
        tool,
        plan_id=services.plan.plan_id,
        step_id="sync",
    )
    duplicate = call_tool(
        tool,
        plan_id=services.plan.plan_id,
        step_id="sync",
    )
    overview = services.plan_store.get_execution_overview(services.plan.plan_id)
    with sqlite3.connect(services.plan_store.db_path) as connection:
        row = connection.execute(
            "SELECT result_summary_json, result_ref FROM navigation_task_steps "
            "WHERE plan_id = ? AND step_id = 'sync'",
            (services.plan.plan_id,),
        ).fetchone()

    assert result["ok"] is True
    assert duplicate["error_type"] == "plan_not_active"
    assert len(invoked) == 1
    assert invoked[0][1] == "running"
    assert result["next_action"] is None
    assert overview.status == "completed"
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000
    assert len(row[0]) <= 4000
    full = services.evidence_store.read(services.task.task_id, row[1])
    assert full["data"]["details"]["full_blob"] == "x" * 4_500


def test_activate_rejects_supersede_after_step_claim_without_losing_execution(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []

    def action(**kwargs):
        invoked.append(kwargs)
        with pytest.raises(ActivePlanExecutionConflict):
            services.plan_store.activate(
                services.task,
                "extract_sync",
                1,
                extract_plan(),
            )
        return ok_result("extract_and_sync_navigation_data")

    monkeypatch.setattr(plan_execution, "extract_and_sync_navigation_data", action)

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["ok"] is True
    assert len(invoked) == 1
    assert services.plan_store.get(services.plan.plan_id).status == "completed"
    assert services.plan_store.get_execution_overview(services.plan.plan_id).completed_steps == 1


def test_evidence_write_failure_recovers_from_durable_result_without_reinvoking(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )
    original_write = services.evidence_store.write
    writes = 0

    def flaky_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("temporary evidence outage")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(services.evidence_store, "write", flaky_write)
    tool = services.tools()["extract_and_sync_navigation_data_tool"]

    first = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")
    persisted_before_recovery = services.task_store.get_task(services.task.task_id)
    assert persisted_before_recovery.phase.value == "extract_sync"
    fresh_tools = {
        candidate.name: candidate
        for candidate in plan_execution.build_plan_bound_execution_tools(
            task=persisted_before_recovery,
            plan_store=services.plan_store,
            evidence_store=services.evidence_store,
            settings=services.settings,
            dry_run=True,
            cancellation=None,
        )
    }
    second = call_tool(
        fresh_tools["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert first["error_type"] == "result_finalize_retry_required"
    assert second["ok"] is True
    assert len(invoked) == 1
    assert services.plan_store.get_staged_step_result(services.plan.plan_id, "sync") is None


def test_staged_result_finalizes_without_execution_time_phase_inference(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []

    def action(**kwargs):
        invoked.append(kwargs)
        segment = services.task.segments[0]
        (services.settings.clip_data_root / services.task.date / segment / "sync_data").mkdir(
            parents=True
        )
        return ok_result("extract_and_sync_navigation_data")

    monkeypatch.setattr(plan_execution, "extract_and_sync_navigation_data", action)
    original_write = services.evidence_store.write
    writes = 0

    def flaky_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("temporary evidence outage")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(services.evidence_store, "write", flaky_write)
    tool = services.tools()["extract_and_sync_navigation_data_tool"]

    first = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")
    second = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    assert first["error_type"] == "result_finalize_retry_required"
    assert second["ok"] is True
    assert len(invoked) == 1
    assert services.plan_store.get(services.plan.plan_id).status == "completed"
    assert services.task_store.get_task(services.task.task_id).phase.value == "extract_sync"


@pytest.mark.parametrize(
    "method_name",
    ["attach_staged_result_evidence", "finalize_staged_step"],
)
def test_sql_finalize_failure_recovers_without_reinvoking(
    monkeypatch,
    tmp_path,
    method_name,
):
    services = build_services(tmp_path)
    invoked = []
    evidence_writes = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )
    original_write = services.evidence_store.write

    def recording_write(*args, **kwargs):
        descriptor = original_write(*args, **kwargs)
        evidence_writes.append(descriptor.ref)
        return descriptor

    monkeypatch.setattr(services.evidence_store, "write", recording_write)
    original_method = getattr(services.plan_store, method_name)
    attempts = 0

    def flaky_method(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("temporary sqlite failure")
        return original_method(*args, **kwargs)

    monkeypatch.setattr(services.plan_store, method_name, flaky_method)
    tool = services.tools()["extract_and_sync_navigation_data_tool"]

    first = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")
    second = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    assert first["error_type"] == "result_finalize_retry_required"
    assert second["ok"] is True
    assert len(invoked) == 1
    assert services.plan_store.get_staged_step_result(services.plan.plan_id, "sync") is None
    if method_name == "finalize_staged_step":
        assert len(evidence_writes) == 1


def test_underlying_exception_stages_failure_and_retry_only_finalizes(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []

    def explode(**kwargs):
        invoked.append(kwargs)
        raise RuntimeError("processor exploded")

    monkeypatch.setattr(plan_execution, "extract_and_sync_navigation_data", explode)
    original_finalize = services.plan_store.finalize_staged_step
    attempts = 0

    def flaky_finalize(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("temporary finalize failure")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(services.plan_store, "finalize_staged_step", flaky_finalize)
    tool = services.tools()["extract_and_sync_navigation_data_tool"]

    first = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")
    second = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    assert first["error_type"] == "result_finalize_retry_required"
    assert second["ok"] is False
    assert second["error_type"] == "processing_exception"
    assert second["next_action"] == "submit_complete_plan"
    assert len(invoked) == 1


def test_processing_result_recursively_redacts_secrets_before_outbox_and_evidence(
    monkeypatch, tmp_path
):
    services = build_services(tmp_path)
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: {
            "ok": True,
            "tool_name": "extract_and_sync_navigation_data",
            "message": "done",
            "details": {
                "password": "p",
                "nested": {"api_key": "k", "safe": "visible"},
                "items": [{"authorization": "Bearer x", "cookie": "c"}],
            },
        },
    )

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )
    payload = services.evidence_store.read(
        services.task.task_id, result["result_ref"]
    )["data"]

    assert payload["details"] == {
        "password": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "safe": "visible"},
        "items": [{"authorization": "[REDACTED]", "cookie": "[REDACTED]"}],
    }


def test_oversized_processing_result_is_bounded_and_moves_to_needs_replan(
    monkeypatch, tmp_path
):
    services = build_services(tmp_path)
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: ok_result(
            "extract_and_sync_navigation_data", blob="x" * 300_000
        ),
    )

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["error_type"] == "processing_result_oversized"
    assert result["status"] == "needs_replan"
    assert len(json.dumps(result)) <= 4000
    assert services.plan_store.get(services.plan.plan_id).status == "invalidated"
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "needs_replan"


def test_running_step_without_staged_result_transitions_to_needs_replan(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )
    tool = services.tools()["extract_and_sync_navigation_data_tool"]
    assert services.plan_store.claim_step(
        services.plan.plan_id,
        "sync",
        "extract_and_sync_navigation_data",
    )

    result = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    assert result["error_type"] == "step_recovery_requires_replan"
    assert result["next_action"] == "submit_complete_plan"
    assert invoked == []
    assert services.plan_store.get(services.plan.plan_id).status == "invalidated"
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "needs_replan"


def test_failed_step_is_recorded_exactly_once_and_duplicate_does_not_reinvoke(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or failed_result("extract_and_sync_navigation_data"),
    )
    tool = services.tools()["extract_and_sync_navigation_data_tool"]

    first = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")
    second = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    assert first["ok"] is False
    assert first["next_action"] == "submit_complete_plan"
    assert second["error_type"] == "step_already_terminal"
    assert second["next_action"] == "submit_complete_plan"
    assert len(invoked) == 1
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "failed"


def test_failed_step_does_not_infer_artifact_state_and_exposes_no_fresh_execution_tools(
    monkeypatch, tmp_path
):
    services = build_services(tmp_path)

    def fail_after_writing_artifacts(**kwargs):
        segment = services.task.segments[0]
        (services.settings.clip_data_root / services.task.date / segment / "sync_data").mkdir(
            parents=True
        )
        return failed_result("extract_and_sync_navigation_data")

    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        fail_after_writing_artifacts,
    )
    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )
    stored = services.task_store.get_task(services.task.task_id)
    fresh = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=stored,
            plan_store=SqliteNavigationPlanRepository(services.plan_store.db_path),
            evidence_store=FileNavigationEvidenceStore(services.evidence_store.root),
            settings=services.settings,
            dry_run=True,
            cancellation=None,
        )
    }
    assert result["status"] == "failed"
    assert stored.phase.value == "extract_sync"
    assert stored.status.value == "failed"
    assert stored.artifact_snapshot is None
    assert fresh == {}


def test_failed_validation_does_not_infer_final_artifacts_or_expose_execution_tools(
    monkeypatch, tmp_path
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    date, segment = "20260710", "segment-a"
    source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / source).mkdir(parents=True)
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    (settings.clip_data_root / date / segment / "sync_data").mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date, segments=[segment], scene_mode="out", dry_run=True
    )
    task = task_store.update_task(
        task.task_id, phase="finish_processing", status="pending"
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = SqliteNavigationObservationStore(db_path).append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task, "finish_processing", observation.revision, finish_plan(source)
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' "
            "WHERE plan_id = ? AND step_id != 'validate'",
            (plan.plan_id,),
        )

    def fail_after_final_artifacts(**kwargs):
        (settings.finish_data_root / date / segment / "scene" / "grid_map").mkdir(
            parents=True
        )
        return failed_result("validate_navigation_outputs")

    monkeypatch.setattr(
        plan_execution, "validate_navigation_outputs", fail_after_final_artifacts
    )
    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
    }
    result = call_tool(
        tools["validate_navigation_outputs_tool"],
        plan_id=plan.plan_id,
        step_id="validate",
    )
    stored = task_store.get_task(task.task_id)
    fresh_tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=stored,
            plan_store=SqliteNavigationPlanRepository(db_path),
            evidence_store=FileNavigationEvidenceStore(tmp_path / "evidence"),
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
    }
    assert result["status"] == "failed"
    assert stored.phase.value == "finish_processing"
    assert stored.status.value == "failed"
    assert stored.artifact_snapshot is None
    assert fresh_tools == {}


def test_force_running_recovery_refuses_to_orphan_task_phase_handoff(tmp_path):
    services = build_services(tmp_path)
    assert services.plan_store.claim_step(
        services.plan.plan_id, "sync", "extract_and_sync_navigation_data"
    )
    with sqlite3.connect(services.plan_store.db_path) as connection:
        now = "2026-07-12T00:00:00+00:00"
        connection.execute(
            """INSERT INTO navigation_human_decision_handoffs (
                   plan_id, step_id, task_id, decision_key, decision_json,
                   status, delivery_status, created_at, updated_at
               ) VALUES (?, 'reviewer-handoff', ?, 'decision', '{}', 'pending',
                         'delivered', ?, ?)""",
            (services.plan.plan_id, services.task.task_id, now, now),
        )

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["error_type"] == "human_handoff_recovery_required"
    assert result["next_action"] == "retry_human_decision_handoff"
    assert services.plan_store.get(services.plan.plan_id).status == "active"
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "running"


def test_cancelled_execution_never_marks_running_or_invokes(monkeypatch, tmp_path):
    services = build_services(tmp_path)
    cancellation = CancellationContext()
    cancellation.cancel()
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        lambda **kwargs: invoked.append(kwargs)
        or ok_result("extract_and_sync_navigation_data"),
    )

    with pytest.raises(TurnCancelled):
        call_tool(
            services.tools(cancellation=cancellation)["extract_and_sync_navigation_data_tool"],
            plan_id=services.plan.plan_id,
            step_id="sync",
        )

    assert invoked == []
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "pending"


def test_cancelled_result_finalize_failure_is_recoverable_without_reinvoking(
    monkeypatch,
    tmp_path,
):
    services = build_services(tmp_path)
    cancellation = CancellationContext()
    invoked = []

    def cancel_during_action(**kwargs):
        invoked.append(kwargs)
        cancellation.cancel()
        cancellation.raise_if_cancelled()

    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        cancel_during_action,
    )
    original_finalize = services.plan_store.finalize_staged_step
    attempts = 0

    def flaky_finalize(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("temporary cancellation finalize failure")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(services.plan_store, "finalize_staged_step", flaky_finalize)
    tool = services.tools(cancellation=cancellation)["extract_and_sync_navigation_data_tool"]

    with pytest.raises(TurnCancelled):
        call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    staged = services.plan_store.get_staged_step_result(services.plan.plan_id, "sync")
    assert staged is not None
    assert staged.target_status == "failed"

    recovery_tool = services.tools()["extract_and_sync_navigation_data_tool"]
    recovered = call_tool(
        recovery_tool,
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert recovered["status"] == "failed"
    assert recovered["error_type"] == "turn_cancelled"
    assert recovered["next_action"] == "submit_complete_plan"
    assert len(invoked) == 1


def test_resolve_finish_arguments_are_derived_from_task_decisions_and_settings(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/selected/sensors"
    plan = finish_plan(source)
    task = NavigationTask(
        task_id="nav-finish",
        date="20260710",
        segments=["segment-a"],
        scene_mode="out",
        phase="finish_processing",
        dry_run=True,
    )
    by_action = {
        step.action: plan_execution.resolve_step_arguments(
            task=task,
            plan=plan,
            step=step,
            settings=settings,
        )
        for step in plan.steps
    }

    assert by_action["confirm_navigation_calibration_params"]["selected_sensor_source"] == (
        settings.processing_root / source
    ).resolve(strict=False)
    assert by_action["assemble_finish_temp"]["selected_sensor_source"] == (
        settings.processing_root / source
    ).resolve(strict=False)
    assert by_action["run_noobscene_preprocessing"] == {
        "finish_temp_path": settings.finish_data_root / "20260710_temp",
        "localization_source": "odom",
        "localization_conversion": "odom_to_ins",
        "settings": settings,
        "dry_run": True,
    }
    assert by_action["prepare_gridmap_for_projection"]["gridmap_variant"] == (
        "copy_existing_gridmap"
    )
    assert by_action["run_projection_and_trajectory"]["projection_variant"] == (
        "cjl_0525_with_gridmap"
    )
    assert by_action["run_projection_and_trajectory"]["finish_path"] == (
        settings.finish_data_root / "20260710"
    )


@pytest.mark.parametrize(
    "sensor_source",
    ["../outside/sensors", "/tmp/arbitrary/sensors", "NoobScenes/params/not_observed/sensors"],
)
def test_calibration_source_must_match_plan_revision_inventory_and_processing_root(
    monkeypatch,
    tmp_path,
    sensor_source,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    observed_source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / observed_source).mkdir(parents=True)
    date = "20260710"
    segment = "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    (settings.clip_data_root / date / segment / "sync_data").mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(task.task_id, phase="finish_processing", status="pending")
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = observation_store.append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[observed_source])],
        [
            EvidenceWrite(
                kind="calibration_inventory",
                source_tool="inspect_navigation_calibration_inventory_tool",
                payload={"sensor_sources": [observed_source]},
                summary="observed calibration inventory",
            )
        ],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(sensor_source),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' "
            "WHERE plan_id = ? AND step_id = 'confirm'",
            (plan.plan_id,),
        )
    invoked = []
    monkeypatch.setattr(
        plan_execution,
        "assemble_finish_temp",
        lambda **kwargs: invoked.append(kwargs) or ok_result("assemble_finish_temp"),
    )

    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
    }
    result = call_tool(
        tools["assemble_finish_temp_tool"],
        plan_id=plan.plan_id,
        step_id="assemble",
    )

    assert result["error_type"] == "calibration_source_invalid"
    assert invoked == []


@pytest.mark.parametrize(
    ("action", "expected_status", "expected_current"),
    [
        ("confirm", "completed", "assemble"),
        ("stop", "failed", "confirm"),
    ],
)
def test_plan_bound_human_decision_waits_and_transitions_ledger_exactly_once(
    tmp_path,
    action,
    expected_status,
    expected_current,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / source).mkdir(parents=True)
    date = "20260710"
    segment = "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    (settings.clip_data_root / date / segment / "sync_data").mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(task.task_id, phase="finish_processing", status="pending")
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = observation_store.append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(source),
    )
    tools = {
        tool.name: tool
        for tool in plan_execution.build_plan_bound_execution_tools(
            task=task,
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=settings,
            dry_run=True,
            cancellation=None,
        )
    }
    assert set(tools) == {
        "request_human_decision",
        "assemble_finish_temp_tool",
        "run_noobscene_preprocessing_tool",
        "run_initial_annotation_gui_tool",
        "run_tracking_tool",
        "prepare_gridmap_for_projection_tool",
        "run_projection_and_trajectory_tool",
        "validate_navigation_outputs_tool",
    }
    request_tool = tools["request_human_decision"]

    permission = asyncio.run(
        request_tool.check_permissions(
            {"plan_id": plan.plan_id, "step_id": "confirm"},
            None,
        )
    )
    metadata = _human_decision_payload_from_tool_call(
        SimpleNamespace(
            name="request_human_decision",
            input={"plan_id": plan.plan_id, "step_id": "confirm"},
        ),
        plan_store=plan_store,
    )
    live_event = _enrich_plan_human_decision_event(
        {
            "type": "human_decision_required",
            "payload": {
                "reply_id": "reply-1",
                "tool_call_id": "call-1",
                "plan_id": plan.plan_id,
                "step_id": "confirm",
                "decision_type": "other",
                "request_id": "",
                "summary": "",
            },
        },
        plan_store=plan_store,
    )
    first = plan_execution.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=evidence_store,
        plan_id=plan.plan_id,
        step_id="confirm",
        decision={"action": action},
    )
    duplicate = plan_execution.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=evidence_store,
        plan_id=plan.plan_id,
        step_id="confirm",
        decision={"action": action},
    )

    assert permission.behavior.value == "allow"
    assert metadata == {
        "request_id": f"{plan.plan_id}:confirm",
        "decision_type": "camera_params",
        "summary": (
            "请确认本计划选定的相机标定参数："
            f"{source}。确认后将继续执行下一计划步骤。"
        ),
        "plan_id": plan.plan_id,
        "step_id": "confirm",
    }
    assert live_event["payload"] == {
        **metadata,
        "reply_id": "reply-1",
        "tool_call_id": "call-1",
    }
    assert first is True
    assert duplicate is True
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is not None
    current = plan_store.get_current_step(plan.plan_id)
    assert current["step"]["step_id"] == expected_current
    with sqlite3.connect(db_path) as connection:
        status = connection.execute(
            "SELECT status FROM navigation_task_steps WHERE plan_id = ? AND step_id = 'confirm'",
            (plan.plan_id,),
        ).fetchone()[0]
    assert status == expected_status


def test_human_decision_finalize_does_not_borrow_rebound_session_authority(
    monkeypatch,
    tmp_path,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "datasets",
        processing_root=tmp_path / "processing",
    )
    source = "NoobScenes/params/observed/sensors"
    (settings.processing_root / source).mkdir(parents=True)
    date = "20260710"
    segment = "segment-a"
    (settings.raw_data_root / date / segment).mkdir(parents=True)
    (settings.clip_data_root / date / segment / "sync_data").mkdir(parents=True)
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date=date,
        segments=[segment],
        scene_mode="out",
        dry_run=True,
    )
    task = task_store.update_task(
        task.task_id,
        phase="finish_processing",
        status="pending",
    )
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    observation = observation_store.append(
        task.task_id,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
        expected_web_session_id=None,
        expected_agentscope_session_id=None,
    )
    task = task_store.update_task(
        task.task_id,
        created_by_web_session_id="web-old",
        latest_web_session_id="web-old",
        agentscope_session_id="agentscope-old",
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task,
        "finish_processing",
        observation.revision,
        finish_plan(source),
        expected_web_session_id="web-old",
        expected_agentscope_session_id="agentscope-old",
    )
    original_stage = plan_store.stage_human_decision_handoff

    def stage_then_rebind(*args, **kwargs):
        outcome = original_stage(*args, **kwargs)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """UPDATE navigation_tasks
                   SET created_by_web_session_id = 'web-new',
                       latest_web_session_id = 'web-new',
                       agentscope_session_id = 'agentscope-new'
                   WHERE task_id = ?""",
                (task.task_id,),
            )
        return outcome

    monkeypatch.setattr(
        plan_store,
        "stage_human_decision_handoff",
        stage_then_rebind,
    )

    accepted = plan_execution.submit_plan_human_decision(
        plan_store=plan_store,
        evidence_store=evidence_store,
        plan_id=plan.plan_id,
        step_id="confirm",
        decision={"action": "confirm"},
        expected_web_session_id="web-old",
        expected_agentscope_session_id="agentscope-old",
    )

    assert accepted is False
    assert plan_store.get_human_decision_handoff(plan.plan_id, "confirm") is not None
    assert plan_store.get_staged_step_result(plan.plan_id, "confirm") is not None
    assert plan_store.get_current_step(plan.plan_id)["step"]["status"] == "pending"
    assert plan_store.get(plan.plan_id).status == "active"
