import asyncio

import pytest

from vla_data_juicer_agents.cli import async_main, parse_args


def test_parse_plan_dry_run_args():
    args = parse_args(["plan", "--date", "20270605", "--segments", "20260605_152856", "--dry-run"])

    assert args.command == "plan"
    assert args.date == "20270605"
    assert args.segments == ["20260605_152856"]
    assert args.dry_run is True


def test_parse_segments_requires_at_least_one_value():
    with pytest.raises(SystemExit):
        parse_args(["plan", "--date", "20270605", "--segments"])


def test_run_no_llm_is_rejected_without_dashscope_key(monkeypatch, capsys):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    exit_code = asyncio.run(async_main(["run", "--date", "20270605", "--dry-run", "--no-llm"]))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--no-llm only supports the plan command" in captured.err
