import shutil
from types import SimpleNamespace

import vla_data_juicer_agents.navigation.plan_execution as plan_execution
from test_navigation_context_budget import (
    _call,
    _extract_plan,
    _write_raw_metadata,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import ToolResult
from vla_data_juicer_agents.navigation.services import build_navigation_services
from vla_data_juicer_agents.navigation.task_state import NavigationTaskStatus
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime


DATE = "20260710"
SEGMENT = "20260710_120000"


def _entry(
    services,
    *,
    session_id="direct-flow",
    web_session_id="direct-flow",
    scene_mode=None,
    dry_run=True,
    request="process the selected navigation data",
):
    web_session_id = web_session_id or session_id
    return services.task_store.create_task_attempt(
        request=request,
        target=DATE,
        date=DATE,
        segments=[SEGMENT],
        scene_mode=scene_mode,
        dry_run=dry_run,
        web_session_id=web_session_id,
        agentscope_session_id=session_id,
    ).task


def _tools(services, session_id, web_session_id="direct-flow"):
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


def _complete_required_inspections(services, session_id, web_session_id="direct-flow"):
    tools = _tools(services, session_id, web_session_id)
    names = sorted(name for name in tools if name.startswith("inspect_navigation_"))
    assert names
    for name in names:
        assert _call(tools[name])["ok"] is True
    return _tools(services, session_id, web_session_id)


def _inspect(services, session_id, names, web_session_id="direct-flow"):
    calls = []
    for name in names:
        tools = _tools(services, session_id, web_session_id)
        result = _call(tools[name])
        assert result["ok"] is True
        calls.append(name)
    return calls, _tools(services, session_id, web_session_id)


def _evidence_by_kind(services, task_id):
    return {
        descriptor.kind: descriptor.ref
        for descriptor in services.observation_store.list_evidence(task_id, limit=50)
    }


def _activate_extract_plan(services, task, session_id, web_session_id="direct-flow"):
    _, tools = _inspect(
        services,
        session_id,
        [
            "inspect_navigation_artifact_state_tool",
            "inspect_navigation_raw_metadata_tool",
            "inspect_navigation_topic_candidates_tool",
            "inspect_navigation_sensor_candidates_tool",
        ],
        web_session_id,
    )
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


def _write_finish_inputs(settings):
    sync_gridmap = (
        settings.clip_data_root / DATE / SEGMENT / "sync_data" / "clip-1" / "grid_map"
    )
    sync_gridmap.mkdir(parents=True, exist_ok=True)
    (sync_gridmap / "map.json").write_text("{}", encoding="utf-8")
    (settings.processing_root / "NoobScenes" / "params" / "selected" / "sensors").mkdir(
        parents=True,
        exist_ok=True,
    )
    converter = settings.processing_root / "NoobScenes" / "include" / "1_odom_convert.py"
    converter.parent.mkdir(parents=True, exist_ok=True)
    converter.write_text("# converter", encoding="utf-8")
    projection = settings.processing_root / "2_pt_project" / "2_othermethod_cjl.py"
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_text("# projection", encoding="utf-8")


def _execute_active_plan(services, session_id, plan_id, web_session_id="direct-flow"):
    calls = []
    while services.plan_store.get(plan_id).status == "active":
        tools = _tools(services, session_id, web_session_id)
        current = services.plan_store.get_current_step(plan_id)
        step = current["step"]
        calls.append(step["action"])
        result = _call(
            tools[f"{step['action']}_tool"],
            plan_id=plan_id,
            step_id=step["step_id"],
        )
        assert result["ok"] is True
    return calls


def test_raw_only_entry_submits_model_plan_and_executes_canonical_arguments(
    monkeypatch,
    tmp_path,
):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    services = build_navigation_services(tmp_path, settings)
    task = _entry(services)

    assert services.observation_store.latest(task.task_id) is None
    assert not hasattr(task, "phase")
    captured = {}

    def record(action):
        def run(**kwargs):
            captured[action] = kwargs
            return ToolResult(ok=True, tool_name=action, message="completed")

        return run

    monkeypatch.setattr(plan_execution, "prepare_raw_data", record("prepare_raw_data"))
    monkeypatch.setattr(
        plan_execution,
        "extract_and_sync_navigation_data",
        record("extract_and_sync_navigation_data"),
    )

    submitted = _activate_extract_plan(services, task, "direct-flow")
    executed = _execute_active_plan(services, "direct-flow", submitted["plan_id"])

    overview = services.plan_store.get_execution_overview(submitted["plan_id"])
    assert overview.completed_steps == overview.total_steps == 2
    assert executed == ["prepare_raw_data", "extract_and_sync_navigation_data"]
    assert captured["prepare_raw_data"] == {
        "date": DATE,
        "segments": [SEGMENT],
        "settings": settings,
        "dry_run": True,
    }
    assert captured["extract_and_sync_navigation_data"] == {
        "date": DATE,
        "segments": [SEGMENT],
        "processes_num": 2,
        "topic_whitelist": [
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "/cam_video4/csi_cam/image_raw/compressed": "fisheye_front",
            "/rs32_lidar_points": "lidar",
            "/sport_odom": "odom",
        },
        "query_dir": "rs32_lidar_points",
        "settings": settings,
        "dry_run": True,
    }
    durable_task = services.task_store.get_task(task.task_id)
    assert durable_task.status == NavigationTaskStatus.ACTIVE
    assert not hasattr(durable_task, "phase")
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

    assert task.accepted_plan_phase is None
    tools = _complete_required_inspections(services, "direct-flow")
    assert "submit_extract_sync_plan_tool" in tools
    assert "submit_finish_processing_plan_tool" in tools
    context = _call(tools["get_navigation_task_context_tool"])
    result = _call(
        tools["submit_finish_processing_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=_finish_plan(_evidence_by_kind(services, task.task_id)),
    )

    assert result["ok"] is True
    assert services.plan_store.get(result["plan_id"]).phase == "finish_processing"


def test_same_session_waits_for_user_then_reinspects_before_finish_plan(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    services = build_navigation_services(tmp_path, settings)
    task = _entry(services)
    extract = _activate_extract_plan(services, task, "direct-flow")
    _execute_active_plan(services, "direct-flow", extract["plan_id"])

    tools_after_extract = _tools(services, "direct-flow")
    assert "inspect_navigation_artifact_state_tool" in tools_after_extract
    assert "submit_extract_sync_plan_tool" in tools_after_extract
    assert "submit_finish_processing_plan_tool" in tools_after_extract
    assert "prepare_raw_data_tool" not in tools_after_extract
    assert services.plan_store.get_active(task.task_id, "finish_processing") is None

    _write_finish_inputs(settings)
    post_extract_turn = [
        "inspect_navigation_artifact_state_tool",
        "assistant: extract/sync verified complete; continue to finish processing?",
    ]
    inspected = _call(tools_after_extract[post_extract_turn[0]])
    assert inspected["ok"] is True
    assert post_extract_turn[-1].endswith("?")
    assert not any("submit_finish_processing_plan" in event for event in post_extract_turn)
    assert services.plan_store.get_active(task.task_id, "finish_processing") is None

    continuation_tools = _tools(services, "direct-flow")
    guidance = _call(
        continuation_tools["record_navigation_user_guidance_tool"],
        text="Continue with finish processing in outdoor scene mode.",
        scene_mode="out",
    )
    assert guidance["ok"] is True
    calls, planning_tools = _inspect(
        services,
        "direct-flow",
        [
            "inspect_navigation_artifact_state_tool",
            "inspect_navigation_gridmap_artifacts_tool",
            "inspect_navigation_runtime_assets_tool",
            "inspect_navigation_calibration_inventory_tool",
            "inspect_navigation_localization_sources_tool",
        ],
    )
    assert calls[0] == "inspect_navigation_artifact_state_tool"
    context = _call(planning_tools["get_navigation_task_context_tool"])
    finish = _call(
        planning_tools["submit_finish_processing_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=_finish_plan(_evidence_by_kind(services, task.task_id)),
    )

    assert finish["ok"] is True
    assert services.task_store.get_task(task.task_id).scene_mode == "out"
    assert services.plan_store.get(finish["plan_id"]).phase == "finish_processing"


def test_new_session_distrusts_sync_claim_and_selects_stage_from_inspection(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    services = build_navigation_services(tmp_path, settings)
    old = _entry(services, session_id="old", web_session_id="web-old")
    old_plan = _activate_extract_plan(services, old, "old", "web-old")
    _execute_active_plan(services, "old", old_plan["plan_id"], "web-old")
    _write_finish_inputs(settings)

    new = _entry(
        services,
        session_id="new",
        web_session_id="web-new",
        scene_mode="out",
        request="同步已完成，请继续处理",
    )
    assert new.task_id != old.task_id
    assert services.observation_store.latest(new.task_id) is None
    assert services.plan_store.get_active_for_task(new.task_id) is None

    first_result = _call(_tools(services, "new", "web-new")["inspect_navigation_artifact_state_tool"])
    first_observation = services.observation_store.latest(new.task_id)
    assert first_result["ok"] is True
    assert first_observation.completed_kinds == ["artifact_state"]
    assert first_observation.payloads[0].snapshot.sync_data_exists is True

    _, planning_tools = _inspect(
        services,
        "new",
        [
            "inspect_navigation_gridmap_artifacts_tool",
            "inspect_navigation_runtime_assets_tool",
            "inspect_navigation_calibration_inventory_tool",
            "inspect_navigation_localization_sources_tool",
        ],
        "web-new",
    )
    context = _call(planning_tools["get_navigation_task_context_tool"])
    finish = _call(
        planning_tools["submit_finish_processing_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=_finish_plan(_evidence_by_kind(services, new.task_id)),
    )
    assert finish["ok"] is True
    assert services.plan_store.get(finish["plan_id"]).phase == "finish_processing"


def test_deleted_products_make_new_attempt_reinspect_and_rerun_extract_sync(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    _write_finish_inputs(settings)
    final = settings.finish_data_root / DATE / SEGMENT / "clip-1"
    final.mkdir(parents=True)
    services = build_navigation_services(tmp_path, settings)
    historical = _entry(services, session_id="historical", web_session_id="web-historical")
    services.task_store.update_task_for_session(
        historical.task_id,
        web_session_id="web-historical",
        agentscope_session_id="historical",
        status=NavigationTaskStatus.COMPLETED.value,
    )
    shutil.rmtree(settings.clip_data_root / DATE)
    shutil.rmtree(settings.finish_data_root / DATE)

    current = _entry(
        services,
        session_id="current",
        web_session_id="web-current",
        request="同步已完成，请继续处理",
    )
    first = _call(
        _tools(services, "current", "web-current")["inspect_navigation_artifact_state_tool"]
    )
    observation = services.observation_store.latest(current.task_id)
    artifact = observation.payloads[0].snapshot
    assert first["ok"] is True
    assert artifact.raw_input_exists is True
    assert artifact.sync_data_exists is False
    assert artifact.final_outputs_exist is False

    _, tools = _inspect(
        services,
        "current",
        [
            "inspect_navigation_raw_metadata_tool",
            "inspect_navigation_topic_candidates_tool",
            "inspect_navigation_sensor_candidates_tool",
        ],
        "web-current",
    )
    context = _call(tools["get_navigation_task_context_tool"])
    rerun = _call(
        tools["submit_extract_sync_plan_tool"],
        planning_context_revision=context["planning_context_revision"],
        plan=_extract_plan(_evidence_by_kind(services, current.task_id)),
    )
    assert rerun["ok"] is True
    assert current.task_id != historical.task_id
    assert services.plan_store.get(rerun["plan_id"]).phase == "extract_sync"


def test_existing_outputs_are_not_entry_facts_and_new_session_reinspects(tmp_path):
    settings = _settings(tmp_path)
    _write_raw_metadata(settings.vladatasets_root, DATE, SEGMENT)
    (settings.clip_data_root / DATE / SEGMENT / "sync_data").mkdir(parents=True)
    final_grid = settings.finish_data_root / DATE / SEGMENT / "clip-1" / "grid_map"
    final_grid.mkdir(parents=True)
    services = build_navigation_services(tmp_path, settings)
    first = _entry(services, session_id="first", scene_mode="out")

    assert services.observation_store.latest(first.task_id) is None
    names = set(_tools(services, "first"))
    assert "inspect_navigation_artifact_state_tool" in names
    assert "submit_extract_sync_plan_tool" in names
    assert "submit_finish_processing_plan_tool" in names
    assert not any(name.endswith("_data_tool") for name in names)

    shutil.rmtree(settings.finish_data_root / DATE)
    second = _entry(services, session_id="second", scene_mode="out")
    assert second.task_id != first.task_id
    assert services.observation_store.latest(second.task_id) is None
    planning = _complete_required_inspections(services, "second")
    assert "submit_extract_sync_plan_tool" in planning
    assert "submit_finish_processing_plan_tool" in planning


def test_cleared_conversation_recovers_compact_plan_and_ledger_anchor_from_sqlite(tmp_path):
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
        "task_attempt_id": task.task_id,
        "observation_revision": anchor["observation_revision"],
        "accepted_plan_id": submitted["plan_id"],
        "accepted_plan_revision": submitted["plan_revision"],
        "current_ledger_step": "prepare_raw",
        "execution_status": "pending",
    }
