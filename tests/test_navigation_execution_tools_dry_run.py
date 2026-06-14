from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import (
    generate_gridmap_from_pcd,
    prepare_raw_data,
)


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
