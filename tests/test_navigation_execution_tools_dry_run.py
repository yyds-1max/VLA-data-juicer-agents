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
    run_tracking_and_projection,
)
from vla_data_juicer_agents.navigation.models import CommandRecord


def _invoke_tool(tool, arguments):
    ctx = SimpleNamespace(tool_name=tool.name, run_config=None, context=None)
    payload = asyncio.run(tool.on_invoke_tool(ctx, json.dumps(arguments)))
    return json.loads(payload) if isinstance(payload, str) else payload


def _command_text(command: list[str]) -> str:
    return command[2] if command[:2] == ["bash", "-lc"] else " ".join(command)


def _argument_after(command: list[str], option: str) -> str | None:
    if command[:2] == ["bash", "-lc"]:
        parts = command[2].split()
    else:
        parts = command
    return parts[parts.index(option) + 1] if option in parts else None


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


def test_generate_gridmap_from_pcd_dry_run_uses_data_runtime_setup(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=Path("/processing"),
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    result = generate_gridmap_from_pcd("20270605", ["20260605_152856"], settings=settings, dry_run=True)

    command = result.commands[0].command
    assert command[:2] == ["bash", "-lc"]
    shell = command[2]
    assert "source /env/setup_data_runtime.sh" in shell
    assert 'exec "$AGENT_DATA_PYTHON" /processing/other_code/pcd_to_grid.py' in shell
    assert "--date 20270605" in shell
    assert "--segments 20260605_152856" in shell


def test_noobscene_preprocessing_dry_run_includes_develop_generation_outputs(tmp_path):
    root = tmp_path / "VLADatasets"
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))
    finish_temp = settings.finish_data_root / "20270605_temp"

    result = run_noobscene_preprocessing(finish_temp, settings=settings, dry_run=True)

    commands = [record.command for record in result.commands]
    produced_paths = {path.as_posix() for path in result.produced_paths}
    assert any(command[-1] == "./main_smart_odom.py" for command in commands)
    assert (finish_temp / "v1.0-trainval").as_posix() in produced_paths
    assert (finish_temp / "maps" / "map.png").as_posix() in produced_paths


def test_noobscene_preprocessing_dry_run_uses_data_runtime_setup(tmp_path):
    root = tmp_path / "VLADatasets"
    settings = NavigationSettings(
        vladatasets_root=root,
        processing_root=Path("/processing"),
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )
    finish_temp = settings.finish_data_root / "20270605_temp"

    result = run_noobscene_preprocessing(finish_temp, settings=settings, dry_run=True)

    shells = [_command_text(record.command) for record in result.commands]
    assert all(record.command[:2] == ["bash", "-lc"] for record in result.commands)
    assert all("source /env/setup_data_runtime.sh" in shell for shell in shells)
    assert any("/processing/NoobScenes/include/0_creat_box.py" in shell for shell in shells)
    assert any("/processing/NoobScenes/include/1_odom_convert.py" in shell for shell in shells)
    assert any("/processing/NoobScenes/include/2_resize.py" in shell for shell in shells)
    assert any('exec "$AGENT_DATA_PYTHON" ./main_smart_odom.py' in shell for shell in shells)


def test_initial_annotation_gui_dry_run_uses_data_runtime_setup(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=Path("/processing"),
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )
    finish_temp = settings.finish_data_root / "20270605_temp"

    result = run_initial_annotation_gui(finish_temp, settings=settings, dry_run=True)

    command = result.commands[0].command
    assert command[:2] == ["bash", "-lc"]
    shell = command[2]
    assert "source /env/setup_data_runtime.sh" in shell
    assert 'exec "$AGENT_DATA_PYTHON" /processing/0_1th_box/gen_box.py' in shell


def test_tracking_and_projection_dry_run_runs_tracking_for_matching_yaml(tmp_path):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))

    result = run_tracking_and_projection(
        finish_temp,
        root / "finish_data" / "20270605",
        settings=settings,
        dry_run=True,
    )

    assert any(_command_text(record.command).endswith("./bin/main") for record in result.commands)
    assert result.details["tracking_yaml_count"] == 1


def test_tracking_binary_dry_run_uses_data_runtime_setup(tmp_path):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(
        vladatasets_root=root,
        processing_root=Path("/processing"),
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    result = run_tracking_and_projection(
        finish_temp,
        root / "finish_data" / "20270605",
        settings=settings,
        dry_run=True,
    )

    tracking_commands = [
        record.command for record in result.commands if _command_text(record.command).endswith("./bin/main")
    ]
    assert tracking_commands
    assert tracking_commands[0][:2] == ["bash", "-lc"]
    assert "source /env/setup_data_runtime.sh && exec ./bin/main" in tracking_commands[0][2]


def test_generate_gridmap_requires_grid_json_after_successful_command(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    (root / "clip_data" / "20270605" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = generate_gridmap_from_pcd("20270605", settings=settings, dry_run=False)

    assert result.ok is False
    assert "grid_map" in result.message


def test_u_legacy_like_dry_run_reports_topic_mapping_and_lidar_query_dir(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, datatoolbox_src=Path("/datatoolbox/src"))

    result = extract_and_sync_navigation_data("20270605", "u_legacy_like", settings=settings, dry_run=True)

    sync_commands = [
        record.command for record in result.commands if "2_sync_data_multi_process_U_legacy.py" in _command_text(record.command)
    ]
    assert result.details["extract_topics"] == [
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/utlidar/robot_odom_systime",
    ]
    assert result.details["sync_topic_map"] == {
        "cam_video5": "fisheye_front",
        "lidar_points": "r32_rslidar_points",
        "utlidar": "odom",
    }
    assert sync_commands
    assert _argument_after(sync_commands[0], "--query_dir") == "lidar_points"


def test_extract_and_sync_dry_run_uses_u_runtime_setup_and_profile_script(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=root,
        datatoolbox_src=Path("/datatoolbox/src"),
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
        gt_dog_root=Path("/gt_dog"),
    )

    result = extract_and_sync_navigation_data("20270605", "u_legacy_like", settings=settings, dry_run=True)

    shells = [record.command[2] for record in result.commands]
    assert all(record.command[:2] == ["bash", "-lc"] for record in result.commands)
    assert any("source /env/setup_data_runtime.sh" in shell for shell in shells)
    assert all("source /gt_dog/modules/message/ros2/install/setup.bash" in shell for shell in shells)
    assert all("source /gt_dog/modules/ros2_ws/src/install/setup.bash" in shell for shell in shells)
    assert all("/gt_dog/modules/message/shm/install/shm_msgs/lib" in shell for shell in shells)
    assert "1_extract_data_from_bag_multi_process_ros2_U_legacy.py" in shells[0]
    assert "2_sync_data_multi_process_U_legacy.py" in shells[1]


def test_projection_python_steps_dry_run_use_data_runtime_setup(tmp_path):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(
        vladatasets_root=root,
        processing_root=Path("/processing"),
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    result = run_tracking_and_projection(
        finish_temp,
        root / "finish_data" / "20270605",
        settings=settings,
        dry_run=True,
    )

    shells = [_command_text(record.command) for record in result.commands]
    assert any("/processing/0_1th_box/img2video.py" in shell for shell in shells)
    assert any("exec \"$AGENT_DATA_PYTHON\" main.py --data_root" in shell for shell in shells)
    assert any("/processing/2_pt_project/0_img2world.py" in shell for shell in shells)
    assert any("/processing/2_pt_project/4_speed_direction_odom.py" in shell for shell in shells)
    assert any("/processing/2_pt_project/2_othermethod_cjl.py" in shell for shell in shells)
    assert any("/processing/2_pt_project/3_move_dir.py" in shell for shell in shells)
    for shell in shells:
        if "$AGENT_DATA_PYTHON" in shell or "./bin/main" in shell:
            assert "source /env/setup_data_runtime.sh" in shell


def test_extract_and_sync_uses_profile_specific_script_paths_in_dry_run(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, datatoolbox_src=Path("/datatoolbox/src"))

    u_result = extract_and_sync_navigation_data("20270605", "u_legacy_like", settings=settings, dry_run=True)
    go2w_result = extract_and_sync_navigation_data("20270605", "go2w_like", settings=settings, dry_run=True)

    u_scripts = [
        script_name
        for script_name in (
            "1_extract_data_from_bag_multi_process_ros2_U_legacy.py",
            "2_sync_data_multi_process_U_legacy.py",
        )
        if any(script_name in _command_text(record.command) for record in u_result.commands)
    ]
    go2w_scripts = [
        script_name
        for script_name in (
            "1_extract_data_from_bag_multi_process_ros2_U.py",
            "2_sync_data_multi_process_U.py",
        )
        if any(script_name in _command_text(record.command) for record in go2w_result.commands)
    ]
    assert u_scripts == [
        "1_extract_data_from_bag_multi_process_ros2_U_legacy.py",
        "2_sync_data_multi_process_U_legacy.py",
    ]
    assert go2w_scripts == [
        "1_extract_data_from_bag_multi_process_ros2_U.py",
        "2_sync_data_multi_process_U.py",
    ]
    assert all("_legacy.py" not in script for script in go2w_scripts)


def test_tracking_moves_original_data_outputs_to_clip_dir(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    finish_temp = root / "finish_data" / "20270605_temp"
    finish = root / "finish_data" / "20270605"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    yaml_path = clip / "master_color_color_color.yaml"
    yaml_path.write_text("{}", encoding="utf-8")
    finish.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        if command == ["./bin/main"]:
            output_root = processing_root / "Data" / "1_img_output"
            (output_root / "tracking_img").mkdir(parents=True)
            (output_root / "tracking_img" / "frame.txt").write_text("frame", encoding="utf-8")
            (output_root / "img_points.txt").write_text("points", encoding="utf-8")
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = run_tracking_and_projection(finish_temp, finish, settings=settings, dry_run=False)

    assert result.ok is True
    assert (clip / "tracking_img_master_color_color_color" / "frame.txt").read_text(encoding="utf-8") == "frame"
    assert (clip / "img_master_color_color_color.txt").read_text(encoding="utf-8") == "points"


def test_generate_gridmap_requires_grid_json_under_requested_segment(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    date = "20270605"
    requested_segment = "20260605_152856"
    other_segment = "20260605_152930"
    (root / "clip_data" / date / requested_segment).mkdir(parents=True)
    other_grid_map = root / "clip_data" / date / other_segment / "sync_data" / "clip_a" / "grid_map"
    other_grid_map.mkdir(parents=True)
    (other_grid_map / "a.json").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = generate_gridmap_from_pcd(date, [requested_segment], settings=settings, dry_run=False)

    assert result.ok is False
    assert requested_segment in result.message


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
    assert result.details["extract_topics"] == [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    ]
    assert result.details["sync_topic_map"] == {
        "cam_video4": "fisheye_front",
        "rs32_lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }
