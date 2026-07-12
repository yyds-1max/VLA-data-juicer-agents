import asyncio
import json
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.cli import async_main, parse_args
from vla_data_juicer_agents.core.cancellation import TurnCancelled
from vla_data_juicer_agents.navigation.config import NavigationSettings


def test_parse_plan_dry_run_args():
    args = parse_args(
        ["plan", "--date", "20270605", "--segments", "segment-1", "--dry-run"]
    )
    assert args.command == "plan"
    assert args.segments == ["segment-1"]
    assert args.dry_run is True
    assert args.scene_mode is None


def test_parse_scene_mode_rejects_unknown_value():
    with pytest.raises(SystemExit):
        parse_args(["plan", "--date", "20270605", "--scene-mode", "indoor"])


def test_cli_has_no_deterministic_no_llm_mode():
    with pytest.raises(SystemExit):
        parse_args(["plan", "--date", "20270605", "--no-llm"])


def test_parse_segments_requires_at_least_one_value():
    with pytest.raises(SystemExit):
        parse_args(["plan", "--date", "20270605", "--segments"])


@pytest.mark.parametrize(
    ("ledger_status", "executor_error", "expected_status", "expected_exit"),
    [
        ("failed", None, "failed", 2),
        ("waiting_user", RuntimeError, "waiting_user", 2),
        ("dry_run_completed", None, "completed", 0),
        ("waiting_user", TurnCancelled, "failed", 2),
        ("dry_run_completed", TurnCancelled, "failed", 2),
    ],
)
def test_cli_uses_durable_terminal_state_not_executor_text(
    tmp_path,
    monkeypatch,
    ledger_status,
    executor_error,
    expected_status,
    expected_exit,
):
    settings = NavigationSettings(runs_root=tmp_path / "runs", vladatasets_root=tmp_path / "data")
    task = SimpleNamespace(task_id="task-1", phase=SimpleNamespace(value="extract_sync"))
    ledger_complete = ledger_status == "dry_run_completed"
    terminal_task = SimpleNamespace(
        task_id="task-1",
        phase=SimpleNamespace(value="extract_sync"),
        status=SimpleNamespace(
            value=(
                "needs_rerun"
                if ledger_complete
                else "pending"
                if ledger_status == "waiting_user"
                else ledger_status
            )
        ),
        dry_run=ledger_complete,
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        plan_revision=1,
        model_dump=lambda mode="json": {"plan_id": "plan-1"},
    )
    plan_store = SimpleNamespace(
        get=lambda _plan_id: SimpleNamespace(
            status="completed" if ledger_complete else "active"
        ),
        get_execution_overview=lambda _plan_id: SimpleNamespace(
            model_dump=lambda mode="json": {
                "current_step_id": None if ledger_complete else "step-1"
            }
        ),
        get_current_step=lambda _plan_id: (
            None
            if ledger_complete
            else {"step": {"step_id": "step-1", "status": ledger_status}}
        ),
    )
    services = SimpleNamespace(
        task_store=SimpleNamespace(get_task=lambda _task_id: terminal_task),
        plan_store=plan_store,
    )
    monkeypatch.setattr("vla_data_juicer_agents.cli.NavigationSettings", lambda: settings)
    monkeypatch.setattr(
        "vla_data_juicer_agents.cli.prepare_direct_navigation_entry",
        lambda **_kwargs: (services, task),
    )

    async def fake_plan(**_kwargs):
        return plan

    async def fake_executor(*_args, **_kwargs):
        if executor_error is not None:
            raise executor_error("executor stopped")
        return "assistant says completed"

    monkeypatch.setattr("vla_data_juicer_agents.cli.run_direct_plan_until_submitted", fake_plan)
    monkeypatch.setattr("vla_data_juicer_agents.cli.resolve_navigation_agent_tools", lambda **_kwargs: [])
    monkeypatch.setattr("vla_data_juicer_agents.cli.create_executor_agent", lambda **_kwargs: object())
    monkeypatch.setattr("vla_data_juicer_agents.cli.run_executor_agent", fake_executor)

    exit_code = asyncio.run(async_main(["run", "--date", "20270605", "--dry-run"]))

    assert exit_code == expected_exit
    report_path = next((tmp_path / "runs" / "20270605").glob("*/final_report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is (expected_exit == 0)
    assert report["status"] == expected_status
    if executor_error is TurnCancelled:
        assert report["error_type"] == "TurnCancelled"


def test_cli_completed_entry_skips_plan_and_execution(tmp_path, monkeypatch):
    settings = NavigationSettings(runs_root=tmp_path / "runs", vladatasets_root=tmp_path / "data")
    task = SimpleNamespace(
        task_id="task-1",
        phase=SimpleNamespace(value="completed"),
        status=SimpleNamespace(value="completed"),
        artifact_snapshot=SimpleNamespace(
            model_dump=lambda mode="json": {"final_outputs_exist": True}
        ),
    )
    monkeypatch.setattr("vla_data_juicer_agents.cli.NavigationSettings", lambda: settings)
    monkeypatch.setattr(
        "vla_data_juicer_agents.cli.prepare_direct_navigation_entry",
        lambda **_kwargs: (SimpleNamespace(), task),
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.cli.run_direct_plan_until_submitted",
        lambda **_kwargs: pytest.fail("completed entry must not plan"),
    )

    exit_code = asyncio.run(async_main(["run", "--date", "20270605", "--dry-run"]))

    assert exit_code == 0
