import shutil
from pathlib import Path
from typing import Any

from agents import function_tool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import DATE_RE, ToolResult
from vla_data_juicer_agents.navigation.profiles import get_profile
from vla_data_juicer_agents.navigation.subprocess_runner import run_command


def _validate_date(date: str) -> str:
    import re

    if not re.match(DATE_RE, date):
        raise ValueError("date must use YYYYMMDD format")
    return date


def _selected_segments(raw_date_path: Path, segments: list[str] | None) -> list[str]:
    if not raw_date_path.exists():
        raise FileNotFoundError(f"Segment root does not exist: {raw_date_path}")

    available = sorted(path.name for path in raw_date_path.iterdir() if path.is_dir())
    if segments is None:
        return available

    missing = [segment for segment in segments if segment not in available]
    if missing:
        raise FileNotFoundError(f"Requested segments do not exist: {missing}")
    return segments


def _selected_segments_for_dry_run(primary_path: Path, fallback_path: Path, segments: list[str] | None) -> list[str]:
    if primary_path.exists():
        return _selected_segments(primary_path, segments)
    if segments is not None and not fallback_path.exists():
        return segments
    return _selected_segments(fallback_path, segments)


def _resolve_data_path(path: str | Path, settings: NavigationSettings) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = settings.vladatasets_root / resolved

    root = settings.vladatasets_root.resolve(strict=False)
    resolved = resolved.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Resolved path must be within {root}: {resolved}")
    return resolved


def _commands_ok(commands) -> bool:
    return bool(commands) and all(command.dry_run or command.return_code == 0 for command in commands)


def _commands_and_outputs_ok(commands, produced_paths: list[Path], dry_run: bool) -> bool:
    if not _commands_ok(commands):
        return False
    return dry_run or all(path.exists() for path in produced_paths)


def _missing_outputs(produced_paths: list[Path], dry_run: bool) -> list[Path]:
    if dry_run:
        return []
    return [path for path in produced_paths if not path.exists()]


def prepare_raw_data(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    raw_date_path = settings.raw_data_root / date
    selected = _selected_segments(raw_date_path, segments)
    raw_temp_path = settings.raw_data_root / f"{date}_temp"
    clip_date_path = settings.clip_data_root / date

    if not dry_run:
        clip_date_path.mkdir(parents=True, exist_ok=True)
        raw_temp_path.mkdir(parents=True, exist_ok=True)
        for segment in selected:
            source = raw_date_path / segment
            target = raw_temp_path / segment
            if target.exists() or target.is_symlink():
                continue
            target.symlink_to(source, target_is_directory=True)

    return ToolResult(
        ok=True,
        tool_name="prepare_raw_data",
        message=f"Prepared {len(selected)} raw segments for {date}.",
        produced_paths=[raw_temp_path, clip_date_path],
        details={"selected_segments": selected, "dry_run": dry_run},
    )


def generate_gridmap_from_pcd(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    command = [
        settings.python_bin,
        str(settings.pcd_to_grid_script),
        "--base-path",
        str(settings.clip_data_root),
        "--date",
        date,
    ]
    if segments:
        command.extend(["--segments", *segments])

    record = run_command(command, dry_run=dry_run)
    produced_paths = [settings.clip_data_root / date]
    missing_outputs = _missing_outputs(produced_paths, dry_run)
    ok = (dry_run or record.return_code == 0) and not missing_outputs
    return ToolResult(
        ok=ok,
        tool_name="generate_gridmap_from_pcd",
        message=(
            "Generated gridmap from PCD."
            if ok
            else f"Missing expected output: {missing_outputs[0]}" if missing_outputs else "Gridmap generation failed."
        ),
        produced_paths=produced_paths,
        commands=[record],
        details={"date": date, "segments": segments, "dry_run": dry_run},
    )


def extract_and_sync_navigation_data(
    date: str,
    dataset_profile: str,
    segments: list[str] | None = None,
    processes_num: int = 4,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    profile = get_profile(dataset_profile)
    raw_temp_path = settings.raw_data_root / f"{date}_temp"
    if dry_run:
        selected = _selected_segments_for_dry_run(raw_temp_path, settings.raw_data_root / date, segments)
    else:
        selected = _selected_segments(raw_temp_path, segments)
    extract_script = settings.datatoolbox_src / "1_extract_data_from_bag_multi_process_ros2_U.py"
    sync_script = settings.datatoolbox_src / "2_sync_data_multi_process_U.py"
    commands = []
    if not selected:
        return ToolResult(
            ok=False,
            tool_name="extract_and_sync_navigation_data",
            message="No selected segments for extract/sync.",
            produced_paths=[settings.clip_data_root / date],
            details={"profile": profile.name, "selected_segments": selected, "dry_run": dry_run},
        )

    for segment in selected:
        data_path = raw_temp_path / segment
        save_path = settings.clip_data_root / date / segment
        commands.append(
            run_command(
                [
                    settings.python_bin,
                    str(extract_script),
                    "--data_path",
                    str(data_path),
                    "--save_path",
                    str(save_path),
                    "--processes_num",
                    str(processes_num),
                ],
                cwd=settings.datatoolbox_src,
                dry_run=dry_run,
            )
        )
        commands.append(
            run_command(
                [
                    settings.python_bin,
                    str(sync_script),
                    "--data_path",
                    str(save_path),
                    "--query_dir",
                    profile.lidar_dirs[-1],
                    "--output_dir",
                    "sync_data",
                    "--sequence_prefix",
                    f"{segment}_zhigu_wuhan",
                    "--processes_num",
                    str(processes_num),
                ],
                cwd=settings.datatoolbox_src,
                dry_run=dry_run,
            )
        )

    missing_sync_data = [
        settings.clip_data_root / date / segment / "sync_data"
        for segment in selected
        if not dry_run and not (settings.clip_data_root / date / segment / "sync_data").exists()
    ]
    ok = _commands_ok(commands) and not missing_sync_data
    return ToolResult(
        ok=ok,
        tool_name="extract_and_sync_navigation_data",
        message=(
            "Extracted and synchronized navigation data."
            if ok
            else f"Missing expected sync_data: {missing_sync_data[0]}" if missing_sync_data else "Extract/sync failed."
        ),
        produced_paths=[settings.clip_data_root / date],
        commands=commands,
        details={
            "profile": profile.name,
            "selected_segments": selected,
            "missing_sync_data": [str(path) for path in missing_sync_data],
            "dry_run": dry_run,
        },
    )


def assemble_finish_temp(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    clip_date_root = settings.clip_data_root / date
    if dry_run:
        selected = _selected_segments_for_dry_run(clip_date_root, settings.raw_data_root / date, segments)
    else:
        selected = _selected_segments(clip_date_root, segments)
    finish_temp = settings.finish_data_root / f"{date}_temp"
    samples_date_root = finish_temp / "samples" / date
    sensor_source = settings.processing_root / "NoobScenes" / "params" / "20260409_U" / "sensors"
    copied_clips: list[str] = []

    for segment in selected:
        sync_root = clip_date_root / segment / "sync_data"
        if not sync_root.exists() and not dry_run:
            raise FileNotFoundError(f"Missing sync_data directory: {sync_root}")
        if dry_run and not sync_root.exists():
            copied_clips.append(segment)
            continue

        src_clips = sorted(path for path in sync_root.iterdir() if path.is_dir())
        if dry_run and not src_clips:
            copied_clips.append(segment)
            continue

        for src_clip in src_clips:
            dst_clip = samples_date_root / src_clip.name
            copied_clips.append(src_clip.name)
            if dry_run:
                continue

            dst_clip.mkdir(parents=True, exist_ok=True)
            if sensor_source.exists():
                shutil.copytree(sensor_source, dst_clip / "sensors", dirs_exist_ok=True)
            for child_name in ("fisheye_front", "r32_rslidar_points", "grid_map", "odom"):
                src_child = src_clip / child_name
                if src_child.exists():
                    shutil.copytree(src_child, dst_clip / child_name, dirs_exist_ok=True)

    if not dry_run:
        samples_date_root.mkdir(parents=True, exist_ok=True)

    return ToolResult(
        ok=True,
        tool_name="assemble_finish_temp",
        message=f"Assembled {len(copied_clips)} clips into finish temp layout.",
        produced_paths=[finish_temp],
        details={"date": date, "selected_segments": selected, "copied_clips": copied_clips, "dry_run": dry_run},
    )


def run_noobscene_preprocessing(
    finish_temp_path: str | Path,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    commands = [
        run_command(
            [
                settings.python_bin,
                str(settings.processing_root / "NoobScenes" / "include" / "0_creat_box.py"),
                "--dataset_root",
                str(root),
            ],
            dry_run=dry_run,
        ),
        run_command(
            [
                settings.python_bin,
                str(settings.processing_root / "NoobScenes" / "include" / "1_odom_convert.py"),
                "--temp_path",
                str(root),
            ],
            dry_run=dry_run,
        ),
        run_command(
            [
                settings.python_bin,
                str(settings.processing_root / "NoobScenes" / "include" / "2_resize.py"),
                "--temp_path",
                str(root),
            ],
            dry_run=dry_run,
        ),
    ]
    produced_paths = [root / "v1.0-trainval"]
    missing_outputs = _missing_outputs(produced_paths, dry_run)
    ok = _commands_and_outputs_ok(commands, produced_paths, dry_run)
    return ToolResult(
        ok=ok,
        tool_name="run_noobscene_preprocessing",
        message=(
            "Ran NoobScenes preprocessing."
            if ok
            else f"Missing expected output: {missing_outputs[0]}" if missing_outputs else "NoobScenes preprocessing failed."
        ),
        produced_paths=produced_paths,
        commands=commands,
        details={"dry_run": dry_run},
    )


def run_initial_annotation_gui(
    finish_temp_path: str | Path,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    record = run_command(
        [settings.python_bin, str(settings.gen_box_script), "--dataset_root", str(root)],
        dry_run=dry_run,
    )
    yaml_count = 0 if dry_run else len(list((root / "samples").glob("*/*/*.yaml")))
    ok = dry_run or (record.return_code == 0 and yaml_count > 0)
    return ToolResult(
        ok=ok,
        tool_name="run_initial_annotation_gui",
        message="Annotation GUI completed." if ok else "Annotation GUI did not produce YAML files.",
        produced_paths=[root],
        commands=[record],
        details={"dry_run": dry_run, "yaml_count": yaml_count, "human_blocking": True},
    )


def run_tracking_and_projection(
    finish_temp_path: str | Path,
    finish_path: str | Path,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    final = _resolve_data_path(finish_path, settings)
    pt_project = settings.processing_root / "2_pt_project"
    commands = [
        run_command(
            [
                settings.python_bin,
                str(settings.processing_root / "0_1th_box" / "img2video.py"),
                "--dataset_root",
                str(root),
            ],
            dry_run=dry_run,
        ),
        run_command(
            [settings.python_bin, "main.py", "--data_root", str(root)],
            cwd=settings.processing_root / "NuscenesAanlysis_smart_pts_project",
            dry_run=dry_run,
        ),
        run_command(
            [settings.python_bin, str(pt_project / "0_img2world.py"), str(root)],
            cwd=pt_project,
            dry_run=dry_run,
        ),
        run_command(
            [settings.python_bin, str(pt_project / "4_speed_direction_odom.py"), str(root)],
            cwd=pt_project,
            dry_run=dry_run,
        ),
        run_command(
            [settings.python_bin, str(pt_project / "2_othermethod_cjl.py"), str(root)],
            cwd=pt_project,
            dry_run=dry_run,
        ),
        run_command(
            [
                settings.python_bin,
                str(pt_project / "3_move_dir.py"),
                "--root_path",
                str(final),
                "--temp_path",
                str(root),
            ],
            cwd=pt_project,
            dry_run=dry_run,
        ),
    ]
    produced_paths = [final]
    missing_outputs = _missing_outputs(produced_paths, dry_run)
    ok = _commands_and_outputs_ok(commands, produced_paths, dry_run)
    return ToolResult(
        ok=ok,
        tool_name="run_tracking_and_projection",
        message=(
            "Ran tracking and projection."
            if ok
            else f"Missing expected output: {missing_outputs[0]}" if missing_outputs else "Tracking/projection failed."
        ),
        produced_paths=produced_paths,
        commands=commands,
        details={"dry_run": dry_run},
    )


def validate_navigation_outputs(
    date: str,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    final = settings.finish_data_root / date
    exists = final.exists()
    ok = dry_run or exists
    return ToolResult(
        ok=ok,
        tool_name="validate_navigation_outputs",
        message="Validation completed." if ok else f"Missing final output: {final}",
        produced_paths=[final],
        details={"exists": exists, "dry_run": dry_run},
    )


def build_execution_tools(dry_run: bool = False) -> list[Any]:
    @function_tool(strict_mode=False, name_override="prepare_raw_data_tool")
    def bound_prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
        return prepare_raw_data(date, segments, dry_run=dry_run).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="extract_and_sync_navigation_data_tool")
    def bound_extract_and_sync_navigation_data_tool(
        date: str,
        dataset_profile: str,
        segments: list[str] | None = None,
        processes_num: int = 4,
    ) -> dict:
        return extract_and_sync_navigation_data(
            date,
            dataset_profile,
            segments,
            processes_num,
            dry_run=dry_run,
        ).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="generate_gridmap_from_pcd_tool")
    def bound_generate_gridmap_from_pcd_tool(date: str, segments: list[str] | None = None) -> dict:
        return generate_gridmap_from_pcd(date, segments, dry_run=dry_run).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="assemble_finish_temp_tool")
    def bound_assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
        return assemble_finish_temp(date, segments, dry_run=dry_run).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="run_noobscene_preprocessing_tool")
    def bound_run_noobscene_preprocessing_tool(finish_temp_path: str) -> dict:
        return run_noobscene_preprocessing(finish_temp_path, dry_run=dry_run).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="run_initial_annotation_gui_tool")
    def bound_run_initial_annotation_gui_tool(finish_temp_path: str) -> dict:
        return run_initial_annotation_gui(finish_temp_path, dry_run=dry_run).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="run_tracking_and_projection_tool")
    def bound_run_tracking_and_projection_tool(finish_temp_path: str, finish_path: str) -> dict:
        return run_tracking_and_projection(finish_temp_path, finish_path, dry_run=dry_run).model_dump(mode="json")

    @function_tool(strict_mode=False, name_override="validate_navigation_outputs_tool")
    def bound_validate_navigation_outputs_tool(date: str) -> dict:
        return validate_navigation_outputs(date, dry_run=dry_run).model_dump(mode="json")

    return [
        bound_prepare_raw_data_tool,
        bound_extract_and_sync_navigation_data_tool,
        bound_generate_gridmap_from_pcd_tool,
        bound_assemble_finish_temp_tool,
        bound_run_noobscene_preprocessing_tool,
        bound_run_initial_annotation_gui_tool,
        bound_run_tracking_and_projection_tool,
        bound_validate_navigation_outputs_tool,
    ]


(
    prepare_raw_data_tool,
    extract_and_sync_navigation_data_tool,
    generate_gridmap_from_pcd_tool,
    assemble_finish_temp_tool,
    run_noobscene_preprocessing_tool,
    run_initial_annotation_gui_tool,
    run_tracking_and_projection_tool,
    validate_navigation_outputs_tool,
) = build_execution_tools(dry_run=False)
