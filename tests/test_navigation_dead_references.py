from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
TEST_ROOT = ROOT / "tests"


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
