import asyncio

import pytest

from vla_data_juicer_agents.cli import parse_args


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
