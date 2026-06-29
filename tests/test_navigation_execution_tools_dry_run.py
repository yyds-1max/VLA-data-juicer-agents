import asyncio
import inspect
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import (
    assemble_finish_temp,
    build_execution_tools,
    confirm_navigation_calibration_params,
    extract_and_sync_navigation_data,
    generate_gridmap_from_pcd,
    prepare_gridmap_for_projection,
    prepare_raw_data,
    run_initial_annotation_gui,
    run_noobscene_preprocessing,
    run_projection_and_trajectory,
    run_tracking,
    run_tracking_and_projection,
    validate_navigation_outputs,
)
from vla_data_juicer_agents.navigation.models import CommandRecord, ToolResult


def _invoke_tool(tool, arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        return _decode_tool_payload(payload)

    return asyncio.run(_call())


def _decode_tool_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "content"):
        return _decode_tool_payload(payload.content)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        texts = [
            block.text
            for block in payload
            if hasattr(block, "text") and isinstance(block.text, str)
        ]
        if texts:
            return _decode_tool_payload("".join(texts))
    return payload


def test_invoke_tool_helper_uses_agentscope_call_protocol():
    class FakeAgentScopeTool:
        name = "fake_tool"

        async def __call__(self, value: str):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"value": value}))])

        def on_invoke_tool(self, *_args, **_kwargs):
            raise AssertionError("OpenAI on_invoke_tool must not be used")

    assert _invoke_tool(FakeAgentScopeTool(), {"value": "ok"}) == {"value": "ok"}


def _command_text(command: list[str]) -> str:
    return command[2] if command[:2] == ["bash", "-lc"] else " ".join(command)


def _is_tracking_binary_command(command: list[str]) -> bool:
    return _command_text(command).endswith("./bin/main")


def _go2w_topic_params() -> dict:
    return {
        "topic_whitelist": [
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ],
        "topic_map": {
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        "query_dir": "rs32_lidar_points",
    }


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


def test_prepare_raw_data_tool_accepts_json_segments_string(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    tool = {tool.name: tool for tool in build_execution_tools(dry_run=True)}["prepare_raw_data_tool"]

    result = _invoke_tool(tool, {"date": "20270605", "segments": '["20260605_152856"]'})

    assert result["ok"] is True
    assert result["details"]["selected_segments"] == ["20260605_152856"]
    assert not (root / "raw_data" / "20270605_temp").exists()


def test_extract_and_sync_tool_accepts_processing_profile_topic_args_without_dataset_profile(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    monkeypatch.setenv("VLA_DATATOOLBOX_SRC", "/datatoolbox/src")
    tool = {tool.name: tool for tool in build_execution_tools(dry_run=True)}["extract_and_sync_navigation_data_tool"]
    topic_params = _go2w_topic_params()

    result = _invoke_tool(
        tool,
        {
            "date": "20270605",
            "segments": ["20260605_152856"],
            "processing_profile": "parameterized_navigation_v1",
            "platform_hint": "go2w",
            **topic_params,
        },
    )

    assert result["ok"] is True
    assert result["details"]["profile"] == "go2w_like"
    assert result["details"]["extract_topics"] == topic_params["topic_whitelist"]
    assert result["details"]["sync_topic_map"] == topic_params["topic_map"]
    assert result["details"]["query_dir"] == topic_params["query_dir"]


def test_generate_gridmap_from_pcd_dry_run_builds_command(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=Path("/processing"),
    )

    result = generate_gridmap_from_pcd("20270605", ["20260605_152856"], settings=settings, dry_run=True)

    command = result.commands[0].command
    shell = _command_text(command)
    assert "/processing/other_code/pcd_to_grid.py" in shell
    assert "--date" in shell
    assert "20270605" in shell
    assert "--segments" in shell
    assert "20260605_152856" in shell


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
    command_texts = [_command_text(command) for command in commands]
    main_index = next(index for index, text in enumerate(command_texts) if text.endswith("./main_smart_odom.py"))
    img2video_index = next(index for index, text in enumerate(command_texts) if "img2video.py" in text)
    assert main_index < img2video_index
    assert (finish_temp / "v1.0-trainval").as_posix() in produced_paths
    assert (finish_temp / "maps" / "map.png").as_posix() in produced_paths


def test_run_noobscene_preprocessing_runs_odom_conversion_for_odom(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )
    finish_temp = settings.finish_data_root / "20270605_temp"
    finish_temp.mkdir(parents=True)

    result = run_noobscene_preprocessing(
        finish_temp,
        localization_source="odom",
        localization_conversion="odom_to_ins",
        settings=settings,
        dry_run=True,
    )

    shells = [" ".join(record.command) for record in result.commands]
    assert any("1_odom_convert.py" in shell for shell in shells)
    assert any("2_resize.py" in shell for shell in shells)


def test_run_noobscene_preprocessing_preserves_old_positional_call_style(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )
    finish_temp = settings.finish_data_root / "20270605_temp"
    finish_temp.mkdir(parents=True)

    result = run_noobscene_preprocessing(finish_temp, settings, True)

    shells = [" ".join(record.command) for record in result.commands]
    assert result.ok is True
    assert result.details["dry_run"] is True
    assert any("1_odom_convert.py" in shell for shell in shells)
    assert any("2_resize.py" in shell for shell in shells)


def test_run_noobscene_preprocessing_skips_odom_conversion_for_native_ins(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )
    finish_temp = settings.finish_data_root / "20270605_temp"
    finish_temp.mkdir(parents=True)

    result = run_noobscene_preprocessing(
        finish_temp,
        localization_source="ins",
        localization_conversion="none",
        settings=settings,
        dry_run=True,
    )

    shells = [" ".join(record.command) for record in result.commands]
    assert any("0_creat_box.py" in shell for shell in shells)
    assert not any("1_odom_convert.py" in shell for shell in shells)
    assert not any("2_resize.py" in shell for shell in shells)
    assert any("main_smart_odom.py" in shell for shell in shells)


@pytest.mark.parametrize(
    ("localization_source", "localization_conversion"),
    [
        ("ins", "odom_to_ins"),
        ("odom", "none"),
        ("gnss", "none"),
    ],
)
def test_run_noobscene_preprocessing_rejects_unsupported_localization_policy(
    tmp_path,
    localization_source,
    localization_conversion,
):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )
    finish_temp = settings.finish_data_root / "20270605_temp"
    finish_temp.mkdir(parents=True)

    result = run_noobscene_preprocessing(
        finish_temp,
        localization_source=localization_source,
        localization_conversion=localization_conversion,
        settings=settings,
        dry_run=True,
    )

    assert result.ok is False
    assert result.details["error_type"] == "unsupported_localization_policy"
    assert "unsupported localization policy" in result.message
    assert result.commands == []


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
    assert any("/processing/0_1th_box/img2video.py" in shell for shell in shells)


def test_assemble_finish_temp_copies_only_server_finish_inputs_and_profile_sensors(tmp_path):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    clip = root / "clip_data" / "20270605" / "20260605_152856" / "sync_data" / "clip_a"
    for child_name in ("fisheye_front", "r32_rslidar_points", "grid_map", "odom"):
        child = clip / child_name
        child.mkdir(parents=True)
        (child / "data.txt").write_text(child_name, encoding="utf-8")
    sensor_source = processing_root / "NoobScenes" / "params" / "20260529_go2w" / "sensors"
    sensor_source.mkdir(parents=True)
    (sensor_source / "calib.json").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    result = assemble_finish_temp("20270605", dataset_profile="go2w_like", settings=settings, dry_run=False)

    dst = root / "finish_data" / "20270605_temp" / "samples" / "20270605" / "clip_a"
    assert result.ok is True
    assert (dst / "fisheye_front" / "data.txt").read_text(encoding="utf-8") == "fisheye_front"
    assert (dst / "r32_rslidar_points" / "data.txt").read_text(encoding="utf-8") == "r32_rslidar_points"
    assert (dst / "sensors" / "calib.json").exists()
    assert not (dst / "grid_map").exists()
    assert not (dst / "odom").exists()
    assert result.details["sensor_source"].endswith("20260529_go2w/sensors")


def test_confirm_navigation_calibration_params_reports_sensor_source(tmp_path):
    processing_root = tmp_path / "processing"
    sensor_source = processing_root / "NoobScenes" / "params" / "20260409_U" / "sensors"
    sensor_source.mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=processing_root,
    )

    result = confirm_navigation_calibration_params(
        "20270605",
        platform_hint="u",
        user_confirmation="确认",
        settings=settings,
        dry_run=False,
    )

    assert result.ok is True
    assert result.details["sensor_source"].endswith("20260409_U/sensors")
    assert result.details["requires_user_confirmation"] is True
    assert result.produced_paths == []
    assert not (settings.finish_data_root / "20270605_temp").exists()


def test_confirm_navigation_calibration_params_rejects_unconfirmed_input(tmp_path):
    processing_root = tmp_path / "processing"
    sensor_source = processing_root / "NoobScenes" / "params" / "20260529_go2w" / "sensors"
    sensor_source.mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=processing_root,
    )

    result = confirm_navigation_calibration_params(
        "20270605",
        platform_hint="go2w",
        user_confirmation="终止",
        settings=settings,
        dry_run=False,
    )

    assert result.ok is False
    assert result.details["error_type"] == "calibration_params_not_confirmed"
    assert "我没有权利" in result.details["confirmation_prompt"]
    assert result.produced_paths == []
    assert not (settings.finish_data_root / "20270605_temp").exists()


def test_bound_confirm_calibration_tool_rejects_model_supplied_confirmation(tmp_path):
    processing_root = tmp_path / "processing"
    sensor_source = processing_root / "NoobScenes" / "params" / "20260529_go2w" / "sensors"
    sensor_source.mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=processing_root,
    )
    tool = {
        tool.name: tool
        for tool in build_execution_tools(settings=settings, dry_run=False)
    }["confirm_navigation_calibration_params_tool"]

    result = _invoke_tool(
        tool,
        {
            "date": "20270605",
            "platform_hint": "go2w",
            "user_confirmation": "确认",
        },
    )

    assert result["ok"] is False
    assert result["details"]["user_confirmation"] is None
    assert result["details"]["error_type"] == "calibration_params_not_confirmed"
    assert "请精确回复 `确认`" in result["details"]["confirmation_prompt"]


def test_build_execution_tools_exposes_calibration_confirmation(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )
    tools = build_execution_tools(settings=settings, dry_run=True)
    names = {tool.name for tool in tools}

    assert "confirm_navigation_calibration_params_tool" in names


def test_prepare_gridmap_for_projection_copies_and_transforms_existing_gridmap(tmp_path):
    root = tmp_path / "VLADatasets"
    source = root / "clip_data" / "20270605" / "20260605_152856" / "sync_data" / "clip_a" / "grid_map"
    source.mkdir(parents=True)
    (source / "map.json").write_text(json.dumps({"data": list(range(200 * 200)), "meta": "kept"}), encoding="utf-8")
    (source / "map.bin").write_text("binary-ish", encoding="utf-8")
    finish_temp = root / "finish_data" / "20270605_temp"
    settings = NavigationSettings(vladatasets_root=root)

    result = prepare_gridmap_for_projection(
        "20270605",
        segments=["20260605_152856"],
        finish_temp_path=finish_temp,
        settings=settings,
        dry_run=False,
    )

    target = finish_temp / "samples" / "20270605" / "clip_a" / "grid_map"
    transformed = json.loads((target / "map.json").read_text(encoding="utf-8"))
    assert result.ok is True
    assert result.details["source_mode"] == "existing_gridmap"
    assert result.details["prepared_gridmap_count"] == 2
    assert transformed["meta"] == "kept"
    assert transformed["data"][:4] == [39800, 39600, 39400, 39200]
    assert transformed["data"][-4:] == [799, 599, 399, 199]
    assert (target / "map.bin").read_text(encoding="utf-8") == "binary-ish"


def test_prepare_gridmap_for_projection_dry_run_falls_back_to_generation_command(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "clip_data" / "20270605" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))

    result = prepare_gridmap_for_projection(
        "20270605",
        segments=["20260605_152856"],
        settings=settings,
        dry_run=True,
    )

    assert result.ok is True
    assert result.details["source_mode"] == "generated_from_pointcloud"
    assert result.details["prepared_gridmap_count"] == 0
    assert result.details["generated_command_count"] == 1
    assert any("/processing/other_code/pcd_to_grid.py" in _command_text(record.command) for record in result.commands)


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

    result = run_tracking(finish_temp, settings=settings, dry_run=True)

    assert any(_command_text(record.command).endswith("./bin/main") for record in result.commands)
    assert result.details["tracking_yaml_count"] == 1


def test_run_tracking_dry_run_only_runs_tracking_loop(tmp_path):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))

    result = run_tracking(finish_temp, settings=settings, dry_run=True)

    shells = [_command_text(record.command) for record in result.commands]
    assert result.ok is True
    assert len(shells) == 1
    assert shells[0].endswith("./bin/main")
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


def test_extract_and_sync_requires_explicit_topic_params(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, datatoolbox_src=Path("/datatoolbox/src"))

    result = extract_and_sync_navigation_data("20270605", "u_legacy_like", settings=settings, dry_run=True)

    assert result.ok is False
    assert "Missing required explicit navigation topic parameter" in result.message
    assert result.commands == []
    assert result.details["missing_topic_params"] == ["topic_whitelist", "topic_map", "query_dir"]


def test_extract_and_sync_dry_run_accepts_explicit_topic_params(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, datatoolbox_src=Path("/datatoolbox/src"))
    topic_whitelist = [
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/sport_odom",
    ]
    topic_map = {
        "cam_video5": "fisheye_front",
        "lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }

    result = extract_and_sync_navigation_data(
        "20270605",
        "u_legacy_like",
        topic_whitelist=topic_whitelist,
        topic_map=topic_map,
        query_dir="lidar_points",
        settings=settings,
        dry_run=True,
    )

    assert result.ok is True
    assert result.details["extract_topics"] == topic_whitelist
    assert result.details["sync_topic_map"] == topic_map
    assert result.details["query_dir"] == "lidar_points"
    command_texts = [_command_text(record.command) for record in result.commands]
    assert any("/navigation/processing/extract_ros2_bag.py" in text for text in command_texts)
    assert any("/navigation/processing/sync_navigation_data.py" in text for text in command_texts)
    assert any("--topic_whitelist_file" in text for text in command_texts)
    assert any("--topic_map_file" in text for text in command_texts)
    assert not any("1_extract_data_from_bag_multi_process_ros2_U_legacy.py" in text for text in command_texts)
    assert not any("2_sync_data_multi_process_U_legacy.py" in text for text in command_texts)
    sync_commands = [
        record.command for record in result.commands if "sync_navigation_data.py" in _command_text(record.command)
    ]
    assert _argument_after(sync_commands[0], "--query_dir") == "lidar_points"


def test_extract_and_sync_dry_run_uses_u_runtime_setup_with_repository_scripts(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=root,
        datatoolbox_src=Path("/datatoolbox/src"),
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
        gt_dog_root=Path("/gt_dog"),
    )
    topic_whitelist = [
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/utlidar/robot_odom_systime",
    ]
    topic_map = {
        "cam_video5": "fisheye_front",
        "lidar_points": "r32_rslidar_points",
        "utlidar": "odom",
    }

    result = extract_and_sync_navigation_data(
        "20270605",
        "u_legacy_like",
        topic_whitelist=topic_whitelist,
        topic_map=topic_map,
        query_dir="lidar_points",
        settings=settings,
        dry_run=True,
    )

    shells = [record.command[2] for record in result.commands]
    assert all(record.command[:2] == ["bash", "-lc"] for record in result.commands)
    assert any("source /env/setup_data_runtime.sh" in shell for shell in shells)
    assert "extract_ros2_bag.py" in shells[0]
    assert "sync_navigation_data.py" in shells[1]


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

    result = run_projection_and_trajectory(
        finish_temp,
        root / "finish_data" / "20270605",
        settings=settings,
        dry_run=True,
    )

    shells = [_command_text(record.command) for record in result.commands]
    assert any("exec \"$AGENT_DATA_PYTHON\" main.py --data_root" in shell for shell in shells)
    assert any("/processing/2_pt_project/0_img2world.py" in shell for shell in shells)
    assert any("/processing/2_pt_project/4_speed_direction_odom.py" in shell for shell in shells)
    assert any("/processing/2_pt_project/2_othermethod_cjl.py" in shell for shell in shells)
    assert any("/processing/2_pt_project/3_move_dir.py" in shell for shell in shells)
    for shell in shells:
        if "$AGENT_DATA_PYTHON" in shell or "./bin/main" in shell:
            assert "source /env/setup_data_runtime.sh" in shell


def test_projection_and_trajectory_uses_go2w_trajectory_script(tmp_path):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    settings = NavigationSettings(vladatasets_root=root, processing_root=Path("/processing"))

    result = run_projection_and_trajectory(
        finish_temp,
        root / "finish_data" / "20270605",
        dataset_profile="go2w_like",
        settings=settings,
        dry_run=True,
    )

    shells = [_command_text(record.command) for record in result.commands]
    assert any("/processing/2_pt_project/2_othermethod_cjl_0525.py" in shell for shell in shells)
    assert not any("/processing/0_1th_box/img2video.py" in shell for shell in shells)
    assert not any(shell.endswith("./bin/main") for shell in shells)


def test_projection_tool_uses_go2w_script_from_platform_hint_without_dataset_profile(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    monkeypatch.setenv("VLA_PROCESSING_ROOT", "/processing")
    tool = {tool.name: tool for tool in build_execution_tools(dry_run=True)}["run_projection_and_trajectory_tool"]

    result = _invoke_tool(
        tool,
        {
            "finish_temp_path": str(finish_temp),
            "finish_path": str(root / "finish_data" / "20270605"),
            "processing_profile": "parameterized_navigation_v1",
            "platform_hint": "go2w",
        },
    )

    shells = [_command_text(record["command"]) for record in result["commands"]]
    assert result["ok"] is True
    assert result["details"]["trajectory_script"] == "2_othermethod_cjl_0525.py"
    assert any("/processing/2_pt_project/2_othermethod_cjl_0525.py" in shell for shell in shells)


def test_build_execution_tools_registers_split_execution_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(tmp_path / "VLADatasets"))

    names = {tool.name for tool in build_execution_tools(dry_run=True)}

    assert "run_tracking_tool" in names
    assert "prepare_gridmap_for_projection_tool" in names
    assert "run_projection_and_trajectory_tool" in names
    assert "run_tracking_and_projection_tool" in names


def test_validate_navigation_outputs_checks_grid_map(tmp_path):
    root = tmp_path / "VLADatasets"
    final = root / "finish_data" / "20270605"
    (final / "20260605_152856" / "clip_a" / "grid_map").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    result = validate_navigation_outputs("20270605", settings=settings, dry_run=False)

    assert result.ok is True
    assert "grid_map" in result.details["checked_outputs"]
    assert any(path.name == "grid_map" for path in result.produced_paths)


def test_validate_navigation_outputs_rejects_temp_style_grid_map(tmp_path):
    root = tmp_path / "VLADatasets"
    final = root / "finish_data" / "20270605"
    (final / "samples" / "20270605" / "clip_a" / "grid_map").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)

    result = validate_navigation_outputs("20270605", settings=settings, dry_run=False)

    assert result.ok is False


def test_prepare_gridmap_copy_existing_variant_does_not_generate_when_missing(tmp_path, monkeypatch):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    def fail_generate(*args, **kwargs):
        raise AssertionError("copy_existing_gridmap variant must not generate from PCD")

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.execution_tools.generate_gridmap_from_pcd",
        fail_generate,
    )

    result = prepare_gridmap_for_projection(
        "20270605",
        ["segment_a"],
        settings=settings,
        dry_run=False,
        gridmap_variant="copy_existing_gridmap",
    )

    assert result.ok is False
    assert result.details["source_mode"] == "missing_existing_gridmap"


def test_prepare_gridmap_generate_variant_runs_pcd_generator_when_missing(tmp_path, monkeypatch):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")
    calls = []

    def fake_generate(date, segments=None, settings=None, dry_run=False):
        calls.append({"date": date, "segments": segments, "dry_run": dry_run})
        gridmap_dir = settings.clip_data_root / date / "segment_a" / "sync_data" / "clip_a" / "grid_map"
        gridmap_dir.mkdir(parents=True)
        (gridmap_dir / "grid_map.json").write_text("{}", encoding="utf-8")
        return ToolResult(
            ok=True,
            tool_name="generate_gridmap_from_pcd",
            message="generated",
            produced_paths=[settings.clip_data_root / date],
        )

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.execution_tools.generate_gridmap_from_pcd",
        fake_generate,
    )

    result = prepare_gridmap_for_projection(
        "20270605",
        ["segment_a"],
        settings=settings,
        dry_run=False,
        gridmap_variant="generate_from_pcd",
    )

    assert result.ok is True
    assert calls == [{"date": "20270605", "segments": ["segment_a"], "dry_run": False}]
    assert result.details["source_mode"] == "generated_from_pointcloud"
    assert "grid_map" in result.message or "Missing final output" in result.message


def test_extract_and_sync_without_topic_params_does_not_use_profile_specific_script_paths(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, datatoolbox_src=Path("/datatoolbox/src"))

    u_result = extract_and_sync_navigation_data("20270605", "u_legacy_like", settings=settings, dry_run=True)
    go2w_result = extract_and_sync_navigation_data("20270605", "go2w_like", settings=settings, dry_run=True)

    assert u_result.ok is False
    assert go2w_result.ok is False
    assert u_result.commands == []
    assert go2w_result.commands == []
    assert u_result.details["missing_topic_params"] == ["topic_whitelist", "topic_map", "query_dir"]
    assert go2w_result.details["missing_topic_params"] == ["topic_whitelist", "topic_map", "query_dir"]


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
        if _is_tracking_binary_command(command):
            output_root = processing_root / "Data" / "1_img_output"
            (output_root / "tracking_img" / "frame.txt").write_text("frame", encoding="utf-8")
            (output_root / "img_points.txt").write_text("points", encoding="utf-8")
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = run_tracking(finish_temp, settings=settings, dry_run=False)

    assert result.ok is True
    assert (clip / "tracking_img_master_color_color_color" / "frame.txt").read_text(encoding="utf-8") == "frame"
    assert (clip / "img_master_color_color_color.txt").read_text(encoding="utf-8") == "points"


def test_tracking_prepares_clean_output_dir_before_each_non_dry_run_job(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    output_root = processing_root / "Data" / "1_img_output"
    stale_tracking = output_root / "tracking_img"
    stale_tracking.mkdir(parents=True)
    (stale_tracking / "old_frame.txt").write_text("old", encoding="utf-8")
    (output_root / "img_points.txt").write_text("old-points", encoding="utf-8")
    finish_temp = root / "finish_data" / "20270605_temp"
    finish = root / "finish_data" / "20270605"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    finish.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        if _is_tracking_binary_command(command):
            assert stale_tracking.is_dir()
            assert list(stale_tracking.iterdir()) == []
            assert not (output_root / "img_points.txt").exists()
            (stale_tracking / "frame.jpg").write_text("frame", encoding="utf-8")
            (output_root / "img_points.txt").write_text("points", encoding="utf-8")
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = run_tracking(finish_temp, settings=settings, dry_run=False)

    assert result.ok is True
    assert (clip / "tracking_img_master_color_color_color" / "frame.jpg").read_text(encoding="utf-8") == "frame"
    assert (clip / "img_master_color_color_color.txt").read_text(encoding="utf-8") == "points"
    assert result.details["completed_tracking_jobs"] == 1
    assert result.details["failed_tracking_yaml"] is None
    assert len(result.details["moved_outputs"]) == 2


def test_tracking_fails_when_command_does_not_create_tracking_img(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    finish_temp = root / "finish_data" / "20270605_temp"
    finish = root / "finish_data" / "20270605"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    finish.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        if _is_tracking_binary_command(command):
            output_root = processing_root / "Data" / "1_img_output"
            shutil.rmtree(output_root / "tracking_img")
            (output_root / "img_points.txt").write_text("points", encoding="utf-8")
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = run_tracking(finish_temp, settings=settings, dry_run=False)

    assert result.ok is False
    assert "Missing tracking output directory" in result.message
    assert result.details["failed_tracking_yaml"] == str(clip / "master_color_color_color.yaml")


def test_tracking_fails_when_command_does_not_create_img_points(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    finish_temp = root / "finish_data" / "20270605_temp"
    finish = root / "finish_data" / "20270605"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    finish.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        if _is_tracking_binary_command(command):
            output_root = processing_root / "Data" / "1_img_output"
            (output_root / "tracking_img" / "frame.jpg").write_text("frame", encoding="utf-8")
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = run_tracking(finish_temp, settings=settings, dry_run=False)

    assert result.ok is False
    assert "Missing tracking points file" in result.message
    assert result.details["failed_tracking_yaml"] == str(clip / "master_color_color_color.yaml")


def test_tracking_replaces_existing_moved_outputs_on_rerun(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    finish_temp = root / "finish_data" / "20270605_temp"
    finish = root / "finish_data" / "20270605"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    target_tracking = clip / "tracking_img_master_color_color_color"
    target_tracking.mkdir()
    (target_tracking / "old_frame.jpg").write_text("old", encoding="utf-8")
    (clip / "img_master_color_color_color.txt").write_text("old-points", encoding="utf-8")
    finish.mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        if _is_tracking_binary_command(command):
            output_root = processing_root / "Data" / "1_img_output"
            (output_root / "tracking_img" / "frame.jpg").write_text("new", encoding="utf-8")
            (output_root / "img_points.txt").write_text("new-points", encoding="utf-8")
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = run_tracking(finish_temp, settings=settings, dry_run=False)

    assert result.ok is True
    assert not (target_tracking / "old_frame.jpg").exists()
    assert (target_tracking / "frame.jpg").read_text(encoding="utf-8") == "new"
    assert (clip / "img_master_color_color_color.txt").read_text(encoding="utf-8") == "new-points"


def test_tracking_dry_run_does_not_delete_or_move_existing_tracking_outputs(tmp_path):
    root = tmp_path / "VLADatasets"
    processing_root = tmp_path / "processing"
    output_root = processing_root / "Data" / "1_img_output"
    stale_tracking = output_root / "tracking_img"
    stale_tracking.mkdir(parents=True)
    (stale_tracking / "old_frame.jpg").write_text("old", encoding="utf-8")
    (output_root / "img_points.txt").write_text("old-points", encoding="utf-8")
    finish_temp = root / "finish_data" / "20270605_temp"
    finish = root / "finish_data" / "20270605"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856"
    clip.mkdir(parents=True)
    (clip / "master_color_color_color.yaml").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(vladatasets_root=root, processing_root=processing_root)

    result = run_tracking(finish_temp, settings=settings, dry_run=True)

    assert result.ok is True
    assert (stale_tracking / "old_frame.jpg").read_text(encoding="utf-8") == "old"
    assert (output_root / "img_points.txt").read_text(encoding="utf-8") == "old-points"
    assert not (clip / "tracking_img_master_color_color_color").exists()
    assert not (clip / "img_master_color_color_color.txt").exists()


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


def test_bound_run_noobscene_preprocessing_tool_accepts_localization_policy(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=tmp_path / "processing",
    )
    finish_temp = settings.finish_data_root / "20270605_temp"
    finish_temp.mkdir(parents=True)
    tool = {
        tool.name: tool
        for tool in build_execution_tools(settings=settings, dry_run=True)
    }["run_noobscene_preprocessing_tool"]

    result = _invoke_tool(
        tool,
        {
            "finish_temp_path": str(finish_temp),
            "localization_source": "ins",
            "localization_conversion": "none",
        },
    )

    shells = [" ".join(record["command"]) for record in result["commands"]]
    assert result["ok"] is True
    assert result["details"]["localization_source"] == "ins"
    assert result["details"]["localization_conversion"] == "none"
    assert not any("1_odom_convert.py" in shell for shell in shells)
    assert not any("2_resize.py" in shell for shell in shells)


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
        **_go2w_topic_params(),
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
        **_go2w_topic_params(),
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
    topic_whitelist = [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    ]
    topic_map = {
        "cam_video4": "fisheye_front",
        "rs32_lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = extract_and_sync_navigation_data(
        "20270605",
        "go2w_like",
        topic_whitelist=topic_whitelist,
        topic_map=topic_map,
        query_dir="rs32_lidar_points",
        settings=settings,
        dry_run=False,
    )

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
    topic_whitelist = [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    ]
    topic_map = {
        "cam_video4": "fisheye_front",
        "rs32_lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }

    result = extract_and_sync_navigation_data(
        "20270605",
        "go2w_like",
        topic_whitelist=topic_whitelist,
        topic_map=topic_map,
        query_dir="rs32_lidar_points",
        settings=settings,
        dry_run=True,
    )

    assert result.ok is False
    assert "No selected segments" in result.message
    assert result.details["extract_topics"] == topic_whitelist
    assert result.details["sync_topic_map"] == topic_map
