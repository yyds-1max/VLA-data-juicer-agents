import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import (
    assemble_finish_temp,
    build_execution_tools,
    extract_and_sync_navigation_data,
    generate_gridmap_from_pcd,
    prepare_raw_data,
    run_initial_annotation_gui,
    run_noobscene_preprocessing,
)
from vla_data_juicer_agents.navigation.models import CommandRecord


def _invoke_tool(tool, arguments):
    ctx = SimpleNamespace(tool_name=tool.name, run_config=None, context=None)
    payload = asyncio.run(tool.on_invoke_tool(ctx, json.dumps(arguments)))
    return json.loads(payload) if isinstance(payload, str) else payload


def test_prepare_raw_data_dry_run_defaults_to_all_segments(tmp_path):
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    (raw_date / "20260605_152930").mkdir()
    settings = NavigationSettings(vladatasets_root=root)

    result = prepare_raw_data("20270605", settings=settings, dry_run=True)

    assert result.ok is True
    assert "20260605_152856" in result.details["selected_segments"]
    assert "20260605_152930" in result.details["selected_segments"]


def test_generate_gridmap_from_pcd_dry_run_builds_command(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=Path("/processing"),
    )

    result = generate_gridmap_from_pcd("20270605", ["20260605_152856"], settings=settings, dry_run=True)

    command = result.commands[0].command
    assert command[:2] == ["python3", "/processing/other_code/pcd_to_grid.py"]
    assert "--date" in command
    assert "20270605" in command
    assert "--segments" in command
    assert "20260605_152856" in command


def test_bound_dry_run_prepare_tool_does_not_create_outputs(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    tool = {tool.name: tool for tool in build_execution_tools(dry_run=True)}["prepare_raw_data_tool"]

    result = _invoke_tool(tool, {"date": "20270605"})

    assert result["ok"] is True
    assert result["details"]["dry_run"] is True
    assert not (root / "raw_data" / "20270605_temp").exists()
    assert not (root / "clip_data" / "20270605").exists()


def test_execution_functions_validate_malformed_date(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    with pytest.raises(ValueError, match="date must use YYYYMMDD format"):
        prepare_raw_data("2026-06-05", settings=settings, dry_run=True)


def test_finish_paths_must_stay_under_vladatasets_root(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    with pytest.raises(ValueError, match="must be within"):
        run_noobscene_preprocessing(tmp_path / "outside", settings=settings, dry_run=True)


def test_initial_annotation_gui_accepts_in_root_finish_path_dry_run(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")
    finish_temp = settings.finish_data_root / "20270605_temp"

    result = run_initial_annotation_gui(finish_temp, settings=settings, dry_run=True)

    assert result.ok is True
    assert result.details["dry_run"] is True


def test_dry_run_execution_steps_are_composable_without_prior_outputs(tmp_path):
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    raw_temp = root / "raw_data" / "20270605_temp"
    clip_date = root / "clip_data" / "20270605"
    for base in (raw_date, raw_temp, clip_date):
        (base / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))

    prepare_result = prepare_raw_data("20270605", settings=settings, dry_run=True)
    extract_result = extract_and_sync_navigation_data(
        "20270605",
        "go2w_like",
        settings=settings,
        dry_run=True,
    )
    assemble_result = assemble_finish_temp("20270605", settings=settings, dry_run=True)

    assert prepare_result.ok is True
    assert extract_result.ok is True
    assert assemble_result.ok is True
    assert assemble_result.details["copied_clips"] == ["20260605_152856"]
    assert not (root / "finish_data").exists()


def test_dry_run_chain_from_raw_fixtures_only_does_not_create_intermediate_dirs(tmp_path):
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))

    prepare_result = prepare_raw_data("20270605", settings=settings, dry_run=True)
    extract_result = extract_and_sync_navigation_data(
        "20270605",
        "go2w_like",
        settings=settings,
        dry_run=True,
    )
    assemble_result = assemble_finish_temp("20270605", settings=settings, dry_run=True)

    assert prepare_result.ok is True
    assert extract_result.ok is True
    assert assemble_result.ok is True
    assert extract_result.details["selected_segments"] == ["20260605_152856"]
    assert assemble_result.details["selected_segments"] == ["20260605_152856"]
    assert assemble_result.details["copied_clips"] == ["20260605_152856"]
    assert not (root / "raw_data" / "20270605_temp").exists()
    assert not (root / "clip_data" / "20270605").exists()
    assert not (root / "finish_data" / "20270605_temp").exists()


def test_extract_and_sync_reports_missing_sync_data_after_successful_commands(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = extract_and_sync_navigation_data("20270605", "go2w_like", settings=settings, dry_run=False)

    assert result.ok is False
    assert "Missing expected sync_data" in result.message
    assert result.details["missing_sync_data"] == [
        str(root / "clip_data" / "20270605" / "20260605_152856" / "sync_data")
    ]


def test_generate_gridmap_reports_missing_output_after_successful_command(tmp_path, monkeypatch):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = generate_gridmap_from_pcd("20270605", settings=settings, dry_run=False)

    assert result.ok is False
    assert "Missing expected output" in result.message


def test_extract_and_sync_empty_segment_root_is_not_successful(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    result = extract_and_sync_navigation_data("20270605", "go2w_like", settings=settings, dry_run=True)

    assert result.ok is False
    assert "No selected segments" in result.message
