from vla_data_juicer_agents.cli import parse_args


def test_parse_plan_dry_run_args():
    args = parse_args(["plan", "--date", "20270605", "--segments", "20260605_152856", "--dry-run"])

    assert args.command == "plan"
    assert args.date == "20270605"
    assert args.segments == ["20260605_152856"]
    assert args.dry_run is True
