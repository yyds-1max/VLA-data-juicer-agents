import asyncio
import json
from pathlib import Path

import pytest

from vla_data_juicer_agents.cli import async_main, parse_args


def test_parse_plan_dry_run_args():
    args = parse_args(
        ["plan", "--date", "20270605", "--segments", "20260605_152856", "--dry-run", "--scene-mode", "out"]
    )

    assert args.command == "plan"
    assert args.date == "20270605"
    assert args.segments == ["20260605_152856"]
    assert args.dry_run is True
    assert args.scene_mode == "out"


def test_parse_scene_mode_rejects_unknown_value():
    with pytest.raises(SystemExit):
        parse_args(["plan", "--date", "20270605", "--scene-mode", "indoor"])


def test_plan_requires_scene_mode(capsys):
    exit_code = asyncio.run(async_main(["plan", "--date", "20270605", "--dry-run", "--no-llm"]))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "scene-mode" in captured.err
    assert "in" in captured.err
    assert "out" in captured.err


def test_parse_segments_requires_at_least_one_value():
    with pytest.raises(SystemExit):
        parse_args(["plan", "--date", "20270605", "--segments"])


def test_run_no_llm_is_rejected_without_dashscope_key(monkeypatch, capsys):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    exit_code = asyncio.run(
        async_main(["run", "--date", "20270605", "--dry-run", "--no-llm", "--scene-mode", "out"])
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--no-llm only supports the plan command" in captured.err


def test_no_llm_plan_writes_run_state(tmp_path, monkeypatch, capsys):
    fixture_root = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(fixture_root))
    monkeypatch.setenv("VLA_RUNS_ROOT", str(runs_root))

    exit_code = asyncio.run(async_main(["plan", "--date", "20270605", "--dry-run", "--no-llm", "--scene-mode", "out"]))

    capsys.readouterr()
    assert exit_code == 0
    run_dirs = list((runs_root / "20270605").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    assert request["date"] == "20270605"
    assert request["scene_mode"] == "out"
    assert plan["date"] == "20270605"
    assert plan["scene_mode"] == "out"
    assert plan["processing_profile"] == "parameterized_navigation_v1"
    assert [step["tool_name"] for step in plan["steps"][:4]] == [
        "confirm_navigation_calibration_params",
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
        "assemble_finish_temp",
    ]
    steps = {step["tool_name"]: step for step in plan["steps"]}
    assert steps["confirm_navigation_calibration_params"]["preconditions"] == []
    assert steps["prepare_raw_data"]["preconditions"] == ["confirm_navigation_calibration_params"]
    assert steps["extract_and_sync_navigation_data"]["preconditions"] == ["prepare_raw_data"]
    assert steps["assemble_finish_temp"]["preconditions"] == ["extract_and_sync_navigation_data"]
    extract_args = steps["extract_and_sync_navigation_data"]["arguments"]
    assert extract_args["processing_profile"] == "parameterized_navigation_v1"
    assert extract_args["platform_hint"] == "go2w"
    assert "dataset_profile" not in extract_args
    assert extract_args["topic_whitelist"] == [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    ]
    assert extract_args["topic_map"] == {
        "cam_video4": "fisheye_front",
        "rs32_lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }
    assert extract_args["query_dir"] == "rs32_lidar_points"
    final_report = json.loads((run_dir / "final_report.json").read_text(encoding="utf-8"))
    assert final_report["status"] == "planned"
    assert final_report["ok"] is True
