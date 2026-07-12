import asyncio
import json
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.cli import async_main, parse_args
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


def test_cli_uses_durable_failed_state_not_executor_text(tmp_path, monkeypatch):
    settings = NavigationSettings(runs_root=tmp_path / "runs", vladatasets_root=tmp_path / "data")
    task = SimpleNamespace(task_id="task-1", phase=SimpleNamespace(value="extract_sync"))
    failed_task = SimpleNamespace(
        task_id="task-1",
        phase=SimpleNamespace(value="extract_sync"),
        status=SimpleNamespace(value="failed"),
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        plan_revision=1,
        model_dump=lambda mode="json": {"plan_id": "plan-1"},
    )
    plan_store = SimpleNamespace(
        get=lambda _plan_id: SimpleNamespace(status="active"),
        get_execution_overview=lambda _plan_id: SimpleNamespace(
            model_dump=lambda mode="json": {"current_step_id": "step-1"}
        ),
        get_current_step=lambda _plan_id: {"step": {"step_id": "step-1"}},
    )
    services = SimpleNamespace(
        task_store=SimpleNamespace(get_task=lambda _task_id: failed_task),
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
        return "assistant says completed"

    monkeypatch.setattr("vla_data_juicer_agents.cli.run_direct_plan_until_submitted", fake_plan)
    monkeypatch.setattr("vla_data_juicer_agents.cli.resolve_navigation_agent_tools", lambda **_kwargs: [])
    monkeypatch.setattr("vla_data_juicer_agents.cli.create_executor_agent", lambda **_kwargs: object())
    monkeypatch.setattr("vla_data_juicer_agents.cli.run_executor_agent", fake_executor)

    exit_code = asyncio.run(async_main(["run", "--date", "20270605", "--dry-run"]))

    assert exit_code == 2
    report_path = next((tmp_path / "runs" / "20270605").glob("*/final_report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["status"] == "failed"


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
