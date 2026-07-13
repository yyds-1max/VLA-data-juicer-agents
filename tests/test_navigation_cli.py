import asyncio
import json
from types import SimpleNamespace

import pytest

from test_navigation_context_budget import _call, _write_raw_metadata
from test_navigation_model_authored_flow import _activate_extract_plan
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


def test_cli_direct_run_executes_with_real_attempt_bound_resolver(tmp_path, monkeypatch):
    date = "20270605"
    segment = "20270605_120000"
    settings = NavigationSettings(
        runs_root=tmp_path / "runs",
        vladatasets_root=tmp_path / "data",
    )
    _write_raw_metadata(settings.vladatasets_root, date, segment)
    captured = {}

    async def submit_real_plan(*, services, task, agentscope_session_id, **_kwargs):
        captured["services"] = services
        submitted = await asyncio.to_thread(
            _activate_extract_plan,
            services,
            task,
            agentscope_session_id,
            task.created_by_web_session_id,
        )
        return services.plan_store.get(submitted["plan_id"])

    def capture_executor(*, tools, **_kwargs):
        captured["tool_names"] = {tool.name for tool in tools}
        return {tool.name: tool for tool in tools}

    async def execute_real_tools(tool_map, plan, **_kwargs):
        services = captured["services"]
        while services.plan_store.get(plan.plan_id).status == "active":
            current = services.plan_store.get_current_step(plan.plan_id)["step"]
            result = await asyncio.to_thread(
                _call,
                tool_map[f"{current['action']}_tool"],
                plan_id=plan.plan_id,
                step_id=current["step_id"],
            )
            assert result["ok"] is True
        return "executed from durable plan"

    monkeypatch.setattr("vla_data_juicer_agents.cli.NavigationSettings", lambda: settings)
    monkeypatch.setattr(
        "vla_data_juicer_agents.cli.run_direct_plan_until_submitted",
        submit_real_plan,
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.cli.create_executor_agent",
        capture_executor,
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.cli.run_executor_agent",
        execute_real_tools,
    )

    exit_code = asyncio.run(
        async_main(
            ["run", "--date", date, "--segments", segment, "--dry-run"]
        )
    )

    assert exit_code == 0
    assert "extract_and_sync_navigation_data_tool" in captured["tool_names"]
    assert "prepare_raw_data_tool" in captured["tool_names"]


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
    task = SimpleNamespace(
        task_id="task-1",
        created_by_web_session_id="direct:run",
        agentscope_session_id="direct__run",
    )
    ledger_complete = ledger_status == "dry_run_completed"
    terminal_task = SimpleNamespace(
        task_id="task-1",
        status=SimpleNamespace(
            value=(
                "completed"
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
