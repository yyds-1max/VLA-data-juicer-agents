from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
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
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
)
from vla_data_juicer_agents.navigation.plan_store import SqliteNavigationPlanRepository
from vla_data_juicer_agents.navigation.plan_store import ActivePlanExecutionConflict
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


def test_artifact_reconciliation_invalidates_plan_and_ledger_before_invocation(
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
    raw_root = services.settings.raw_data_root / services.task.date
    for child in raw_root.iterdir():
        child.rmdir()
    raw_root.rmdir()

    result = call_tool(
        services.tools()["extract_and_sync_navigation_data_tool"],
        plan_id=services.plan.plan_id,
        step_id="sync",
    )

    assert result["error_type"] == "plan_invalidated_by_artifacts"
    assert invoked == []
    assert services.plan_store.get(services.plan.plan_id).status == "invalidated"
    assert services.plan_store.get_current_step(services.plan.plan_id)["step"]["status"] == "needs_replan"
    assert services.task_store.get_task(services.task.task_id).status == "needs_replan"


def test_finish_plan_invalidates_when_reconciliation_falls_back_to_extract_sync(
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
        task.phase,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
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

    assert permission.behavior.value == "deny"
    assert plan_store.get(plan.plan_id).status == "invalidated"
    assert task_store.get_task(task.task_id).status == "needs_replan"


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

    assert claimed is False
    assert services.plan_store.get_current_step(old_plan.plan_id)["step"]["status"] == "pending"


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
    second = call_tool(tool, plan_id=services.plan.plan_id, step_id="sync")

    assert first["error_type"] == "result_finalize_retry_required"
    assert second["ok"] is True
    assert len(invoked) == 1
    assert services.plan_store.get_staged_step_result(services.plan.plan_id, "sync") is None


def test_staged_result_finalizes_before_expected_artifact_phase_advance_invalidates_plan(
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
        task.phase,
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
        task.phase,
        "calibration_inventory",
        [CalibrationInventoryObservation(sensor_sources=[source])],
        [],
        evidence_store,
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
