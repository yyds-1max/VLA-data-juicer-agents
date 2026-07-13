import json
import shutil
from types import SimpleNamespace

from test_navigation_context_budget import (
    _call,
    _extract_plan,
    _tool_map,
    _write_raw_metadata,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.services import build_navigation_services
from vla_data_juicer_agents.navigation.task_reconciliation import prepare_navigation_task_entry
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime


DATE = "20260710"
SEGMENT = "20260710_120000"


def _entry(
    services,
    *,
    session_id="direct-flow",
    web_session_id=None,
    scene_mode=None,
    dry_run=True,
):
    return prepare_navigation_task_entry(
        task_store=services.task_store,
        observation_store=services.observation_store,
        evidence_store=services.evidence_store,
        message="Structured handoff JSON: "
        + json.dumps(
            {
                "date": DATE,
                "segments": [SEGMENT],
                "scene_mode": scene_mode,
                "dry_run": dry_run,
                "request": "process the selected navigation data",
            }
        ),
        web_session_id=web_session_id,
        agentscope_session_id=session_id,
        settings=services.settings,
    )


def _tools(services, session_id, web_session_id=None):
    if web_session_id is None:
        return _tool_map(services, session_id)
    from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools

    return {
        tool.name: tool
        for tool in resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id=session_id,
            web_session_id=web_session_id,
            cancellation=None,
        )
    }


def _complete_required_inspections(services, session_id, web_session_id=None):
    while True:
        tools = _tools(services, session_id, web_session_id)
        names = sorted(name for name in tools if name.startswith("inspect_navigation_"))
        if not names:
            return tools
        for name in names:
            assert _call(tools[name])["ok"] is True


def _evidence_by_kind(services, task_id):
    return {
        descriptor.kind: descriptor.ref
        for descriptor in services.observation_store.list_evidence(task_id, limit=50)
    }


def _activate_extract_plan(services, task, session_id, web_session_id=None):
    tools = _complete_required_inspections(services, session_id, web_session_id)
    context = _call(tools["get_navigation_task_context_tool"])
    payload = _extract_plan(_evidence_by_kind(services, task.task_id))
    result = _call(
        tools["submit_extract_sync_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=payload,
    )
    assert result["ok"] is True
    return result


def _finish_plan(evidence):
    return {
        "decisions": {
            "localization": {
                "source": "odom",
                "conversion": "odom_to_ins",
                "reason": "The observed odometry converter is available.",
                "evidence_refs": [evidence["localization_sources"]],
            },
            "gridmap": {
                "source": "existing_gridmap",
                "reason": "Use the measured existing grid map.",
                "evidence_refs": [evidence["gridmap_artifacts"]],
            },
            "calibration": {
                "mode": "hardcoded_with_user_confirmation",
                "selected_sensor_source": "NoobScenes/params/selected/sensors",
                "requires_user_confirmation": True,
                "reason": "Use the observed selected calibration directory.",
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
                "step_id": "tracking",
                "action": "run_tracking",
                "variant": "default",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["localization"],
            },
            {
                "step_id": "prepare_gridmap",
                "action": "prepare_gridmap_for_projection",
                "variant": "copy_existing_gridmap",
                "arguments": {},
                "depends_on": ["tracking"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
            {
                "step_id": "projection",
                "action": "run_projection_and_trajectory",
                "variant": "cjl_with_gridmap",
                "arguments": {},
                "depends_on": ["prepare_gridmap"],
                "failure_policy": "stop",
                "decision_refs": ["localization", "gridmap"],
            },
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": ["projection"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


def _settings(tmp_path):
    return NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )


def test_raw_only_entry_submits_model_plan_and_executes_all_steps_in_dry_run(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    services = build_navigation_services(tmp_path, settings)
    task = _entry(services)

    assert task.phase.value == "extract_sync"
    submitted = _activate_extract_plan(services, task, "direct-flow")
    while services.plan_store.get(submitted["plan_id"]).status == "active":
        tools = _tools(services, "direct-flow")
        current = services.plan_store.get_current_step(submitted["plan_id"])
        step = current["step"]
        result = _call(
            tools[f"{step['action']}_tool"],
            plan_id=submitted["plan_id"],
            step_id=step["step_id"],
        )
        assert result["ok"] is True

    overview = services.plan_store.get_execution_overview(submitted["plan_id"])
    assert overview.completed_steps == overview.total_steps == 2
    assert (settings.raw_data_root / DATE / SEGMENT / "metadata.yaml").exists()
    assert not (settings.clip_data_root / DATE / SEGMENT / "sync_data").exists()


def test_existing_sync_and_scene_mode_select_finish_plan_without_extract_tools(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    gridmap = settings.clip_data_root / DATE / SEGMENT / "sync_data" / "clip-1" / "grid_map"
    gridmap.mkdir(parents=True)
    (gridmap / "map.json").write_text("{}", encoding="utf-8")
    (settings.processing_root / "NoobScenes" / "params" / "selected" / "sensors").mkdir(
        parents=True
    )
    converter = settings.processing_root / "NoobScenes" / "include" / "1_odom_convert.py"
    converter.parent.mkdir(parents=True)
    converter.write_text("# converter", encoding="utf-8")
    projection = settings.processing_root / "2_pt_project" / "2_othermethod_cjl.py"
    projection.parent.mkdir(parents=True)
    projection.write_text("# projection", encoding="utf-8")
    services = build_navigation_services(tmp_path, settings)
    task = _entry(services, scene_mode="out")

    assert task.phase.value == "finish_processing"
    tools = _complete_required_inspections(services, "direct-flow")
    assert "submit_extract_sync_plan_tool" not in tools
    context = _call(tools["get_navigation_task_context_tool"])
    result = _call(
        tools["submit_finish_processing_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=_finish_plan(_evidence_by_kind(services, task.task_id)),
    )

    assert result["ok"] is True
    assert services.plan_store.get(result["plan_id"]).phase == "finish_processing"


def test_completed_outputs_expose_no_processing_tools_and_deletion_reselects_finish(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    (settings.clip_data_root / DATE / SEGMENT / "sync_data").mkdir(parents=True)
    final_grid = settings.finish_data_root / DATE / SEGMENT / "clip-1" / "grid_map"
    final_grid.mkdir(parents=True)
    services = build_navigation_services(tmp_path, settings)
    completed = _entry(services, scene_mode="out")

    assert completed.phase.value == "completed"
    names = set(_tools(services, "direct-flow"))
    assert names == {
        "get_navigation_task_state_tool",
        "list_navigation_task_evidence_tool",
        "read_navigation_task_evidence_tool",
    }

    shutil.rmtree(settings.finish_data_root / DATE)
    resumed = _entry(services, scene_mode="out")
    assert resumed.phase.value == "finish_processing"
    assert "submit_extract_sync_plan_tool" not in _complete_required_inspections(
        services, "direct-flow"
    )


def test_cleared_conversation_recovers_phase_plan_and_current_step_from_sqlite(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    services = build_navigation_services(tmp_path, settings)
    web_session_id = "web-owner"
    session_id = "web-owner__navigation-data-agent"
    task = _entry(
        services,
        session_id=session_id,
        web_session_id=web_session_id,
    )
    submitted = _activate_extract_plan(
        services,
        task,
        session_id,
        web_session_id,
    )

    recovered_runtime = object.__new__(AgentScopeRuntime)
    recovered_runtime.config = SimpleNamespace(workspace_root=tmp_path)
    recovered_runtime._navigation_services = lambda: services
    anchor = recovered_runtime._navigation_durable_state_anchor(
        session_id,
        web_session_id=web_session_id,
    )

    assert anchor == {
        "task_id": task.task_id,
        "phase": "extract_sync",
        "task_status": anchor["task_status"],
        "observation_revision": anchor["observation_revision"],
        "active_plan_id": submitted["plan_id"],
        "active_plan_revision": submitted["plan_revision"],
        "current_step_id": "prepare_raw",
    }
