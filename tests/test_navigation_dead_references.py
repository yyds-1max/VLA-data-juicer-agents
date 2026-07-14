from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
TEST_ROOT = ROOT / "tests"
FRONTEND_SOURCE_ROOT = ROOT / "frontend" / "src"


def test_datapilot_production_has_no_obsolete_web_event_bridge_references():
    backend_forbidden = (
        "forward_events_until_idle",
        "subscribe_web_session_events",
        "web_session_subscription_key",
        "@app.websocket",
        "event_cursor",
        "save_agentscope_event_cursor",
    )
    frontend_forbidden = ("WebSocket", "openSessionEvents")

    backend_paths = [
        *(
            SOURCE_ROOT / "vla_data_juicer_agents" / "web"
        ).rglob("*.py"),
        SOURCE_ROOT
        / "vla_data_juicer_agents"
        / "runtime"
        / "agentscope_runtime.py",
    ]
    frontend_paths = [
        *FRONTEND_SOURCE_ROOT.rglob("*.ts"),
        *FRONTEND_SOURCE_ROOT.rglob("*.tsx"),
    ]

    matches: list[str] = []
    for path in backend_paths:
        text = path.read_text(encoding="utf-8")
        for symbol in backend_forbidden:
            if symbol in text:
                matches.append(f"{path.relative_to(ROOT)}: {symbol}")
    for path in frontend_paths:
        text = path.read_text(encoding="utf-8")
        for symbol in frontend_forbidden:
            if symbol in text:
                matches.append(f"{path.relative_to(ROOT)}: {symbol}")

    assert matches == []


def test_readme_documents_datapilot_durable_session_runtime_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required = (
        "AgentScope 2.0.4",
        "Redis",
        "/api/sessions/{id}/stream",
        "success",
        "failure",
        "stopped",
        "raw/sync/clip/finish",
    )
    assert [fragment for fragment in required if fragment not in readme] == []


def test_navigation_source_has_no_superseded_dataset_state_machine_references():
    forbidden = (
        "prepare_navigation_" + "task_entry",
        "reconcile_navigation_" + "task",
        "create_or_" + "update_task",
        "find_latest_" + "by_date",
        "latest_web_" + "session_id",
        "artifact_snapshot_" + "json",
        "waiting_" + "reason",
        "next_required_" + "input",
        "latest_" + "run_id",
        "last_completed_" + "step",
        "drift_" + "json",
        "needs_" + "reconcile",
        "waiting_scene_" + "mode",
        "get_or_create_navigation_" + "task_tool",
        "reconcile_navigation_" + "task_tool",
        "get_phase_planning_" + "context_tool",
        "PhasePlanning" + "Context",
        "PHASE_REQUIRED_" + "OBSERVATIONS",
        "NavigationTask" + "Drift",
    )

    matches: list[str] = []
    for scan_root in (SOURCE_ROOT, TEST_ROOT):
        for path in scan_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for symbol in forbidden:
                if symbol in text:
                    matches.append(f"{path.relative_to(ROOT)}: {symbol}")
    assert matches == []


def test_plan_execution_does_not_inspect_artifacts_automatically():
    source = (
        SOURCE_ROOT
        / "vla_data_juicer_agents"
        / "navigation"
        / "plan_execution.py"
    ).read_text(encoding="utf-8")

    assert "build_navigation_" + "artifact_snapshot" not in source
