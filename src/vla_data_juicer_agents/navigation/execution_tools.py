import shutil
import re
import json
from pathlib import Path
from typing import Any

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import DATE_RE, ToolResult
from vla_data_juicer_agents.navigation.runtime import (
    data_runtime_command,
    python_data_command,
    run_u_python_command,
)
from vla_data_juicer_agents.navigation.subprocess_runner import run_command


PROCESSING_SCRIPT_ROOT = Path(__file__).resolve().parent / "processing"
REPOSITORY_EXTRACT_SCRIPT = PROCESSING_SCRIPT_ROOT / "extract_ros2_bag.py"
REPOSITORY_SYNC_SCRIPT = PROCESSING_SCRIPT_ROOT / "sync_navigation_data.py"
SYNC_TOLERANCE_MS = 100
REQUIRED_CALIBRATION_FILES = ("fisheye_front.json", "r32_rslidar_points.json")


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


def _missing_topic_param_names(
    topic_whitelist: list[str] | None,
    topic_map: dict[str, str] | None,
    query_dir: str | None,
) -> list[str]:
    missing = []
    if not topic_whitelist:
        missing.append("topic_whitelist")
    if not topic_map:
        missing.append("topic_map")
    if not query_dir:
        missing.append("query_dir")
    return missing


def _missing_outputs(produced_paths: list[Path], dry_run: bool) -> list[Path]:
    if dry_run:
        return []
    return [path for path in produced_paths if not path.exists()]


def _replace_with_symlink(link_path: Path, target_path: Path) -> None:
    if link_path.is_symlink() or link_path.is_file():
        link_path.unlink()
    elif link_path.exists():
        raise FileExistsError(f"Refusing to replace existing directory with symlink: {link_path}")
    link_path.symlink_to(target_path, target_is_directory=True)


def _copy_directory_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        elif child.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)


def _delete_existing_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _write_json_parameter_file(path: Path, payload: Any, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _prepare_tracking_output(img_output_dir: Path) -> tuple[bool, str | None]:
    tracking_img = img_output_dir / "tracking_img"
    img_points = img_output_dir / "img_points.txt"
    try:
        img_output_dir.mkdir(parents=True, exist_ok=True)
        _delete_existing_path(tracking_img)
        _delete_existing_path(img_points)
        tracking_img.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return False, f"Failed to prepare tracking output directory: {tracking_img}: {exc}"
    return True, None


def _replace_path(source: Path, target: Path, kind: str) -> tuple[bool, str | None]:
    if kind == "dir":
        if not source.exists() or not source.is_dir() or source.is_symlink():
            return False, f"Missing tracking output directory: {source}"
    elif kind == "file":
        if not source.exists() or not source.is_file():
            return False, f"Missing tracking points file: {source}"
    else:
        raise ValueError(f"Unsupported path kind: {kind}")

    try:
        _delete_existing_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        label = "tracking output directory" if kind == "dir" else "tracking points file"
        return False, f"Failed to move {label}: {source} -> {target}: {exc}"
    return True, None


def _validated_sensor_source(
    settings: NavigationSettings,
    selected_sensor_source: str | Path,
) -> Path:
    source = Path(selected_sensor_source)
    if not source.is_absolute():
        source = settings.processing_root / source
    source = source.resolve(strict=False)
    processing_root = settings.processing_root.resolve(strict=False)
    if not source.is_relative_to(processing_root):
        raise ValueError("selected_sensor_source must resolve under processing_root")
    return source


def _missing_calibration_files(sensor_source: Path) -> list[Path]:
    return [
        sensor_source / name
        for name in REQUIRED_CALIBRATION_FILES
        if not (sensor_source / name).is_file()
    ]


def _transform_gridmap_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 200 * 200:
        return payload
    rows = [data[index : index + 200] for index in range(0, len(data), 200)]
    transformed = [
        value
        for transposed_row in zip(*rows)
        for value in reversed(transposed_row)
    ]
    updated = dict(payload)
    updated["data"] = transformed
    return updated


def _copy_gridmap_file(source: Path, target: Path, dry_run: bool) -> None:
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        target.write_text(json.dumps(_transform_gridmap_payload(payload), ensure_ascii=False), encoding="utf-8")
    else:
        shutil.copy2(source, target)


def _discover_gridmap_dirs(
    date: str,
    segments: list[str] | None,
    settings: NavigationSettings,
) -> list[tuple[Path, str]]:
    segment_roots = (
        [settings.clip_data_root / date / segment for segment in segments]
        if segments
        else sorted(path for path in (settings.clip_data_root / date).iterdir() if path.is_dir())
        if (settings.clip_data_root / date).exists()
        else []
    )
    discovered: list[tuple[Path, str]] = []
    for segment_root in segment_roots:
        sync_root = segment_root / "sync_data"
        if not sync_root.exists():
            continue
        for grid_map_dir in sorted(sync_root.glob("*/grid_map")):
            if grid_map_dir.is_dir() and any(path.is_file() for path in grid_map_dir.glob("*.json")):
                discovered.append((grid_map_dir, grid_map_dir.parent.name))
    return discovered


def _tracking_yaml_paths(root: Path) -> tuple[list[Path], re.Pattern[str]]:
    tracking_yaml_re = re.compile(r"^((master|other[0-9]+)_[a-z]+_[a-z]+_[a-z]+)\.yaml$")
    return (
        sorted(
            path
            for path in (root / "samples").glob("*/*/*.yaml")
            if tracking_yaml_re.match(path.name)
        ),
        tracking_yaml_re,
    )


def _run_tracking_loop(
    root: Path,
    settings: NavigationSettings,
    dry_run: bool,
) -> tuple[list, str | None, dict[str, Any]]:
    tracking_root = settings.processing_root / "1_onnx_tam"
    param_dir = settings.processing_root / "Data" / "3_param"
    img_output_dir = settings.processing_root / "Data" / "1_img_output"
    tracking_yamls, tracking_yaml_re = _tracking_yaml_paths(root)
    commands = []
    tracking_error = None
    completed_tracking_jobs = 0
    failed_tracking_yaml = None
    moved_outputs: list[dict[str, str]] = []

    if not dry_run and not tracking_yamls:
        tracking_error = f"No tracking YAML files found under {root / 'samples'}"

    if tracking_error is None:
        for yaml_path in tracking_yamls:
            match = tracking_yaml_re.match(yaml_path.name)
            assert match is not None
            yaml_stem = match.group(1)
            if not dry_run:
                prepared, prepare_error = _prepare_tracking_output(img_output_dir)
                if not prepared:
                    tracking_error = prepare_error
                    failed_tracking_yaml = str(yaml_path)
                    break
                dog_yaml = param_dir / "dog.yaml"
                dog_yaml.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(yaml_path, dog_yaml)

            tracking_record = run_command(
                data_runtime_command(settings.runtime, ["./bin/main"]),
                cwd=tracking_root,
                dry_run=dry_run,
            )
            commands.append(tracking_record)
            if not dry_run and tracking_record.return_code != 0:
                tracking_error = f"Tracking command failed for {yaml_path}"
                failed_tracking_yaml = str(yaml_path)
                break

            if not dry_run:
                tracking_img = img_output_dir / "tracking_img"
                tracking_img_target = yaml_path.parent / f"tracking_img_{yaml_stem}"
                img_points = img_output_dir / "img_points.txt"
                img_points_target = yaml_path.parent / f"img_{yaml_stem}.txt"
                for source, target, kind in (
                    (tracking_img, tracking_img_target, "dir"),
                    (img_points, img_points_target, "file"),
                ):
                    moved, move_error = _replace_path(source, target, kind)
                    if not moved:
                        tracking_error = move_error
                        failed_tracking_yaml = str(yaml_path)
                        break
                    moved_outputs.append(
                        {
                            "source": str(source),
                            "target": str(target),
                            "kind": kind,
                        }
                    )
                if tracking_error is not None:
                    break
                completed_tracking_jobs += 1

    details = {
        "dry_run": dry_run,
        "tracking_yaml_count": len(tracking_yamls),
        "completed_tracking_jobs": completed_tracking_jobs,
        "failed_tracking_yaml": failed_tracking_yaml,
        "moved_outputs": moved_outputs,
    }
    return commands, tracking_error, details


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
    args = [
        "--base-path",
        settings.clip_data_root,
        "--date",
        date,
    ]
    if segments:
        args.extend(["--segments", *segments])
    command = python_data_command(settings.runtime, settings.pcd_to_grid_script, args)

    record = run_command(command, dry_run=dry_run)
    produced_paths = [settings.clip_data_root / date]
    if dry_run:
        grid_jsons = []
        missing_grid_segments = []
    elif segments:
        segment_grid_jsons = {
            segment: sorted((settings.clip_data_root / date / segment).glob("**/grid_map/*.json"))
            for segment in segments
        }
        grid_jsons = [path for paths in segment_grid_jsons.values() for path in paths]
        missing_grid_segments = [segment for segment, paths in segment_grid_jsons.items() if not paths]
    else:
        grid_jsons = sorted((settings.clip_data_root / date).glob("**/grid_map/*.json"))
        missing_grid_segments = []
    missing_outputs = _missing_outputs(produced_paths, dry_run)
    missing_grid_json = (
        not dry_run
        and record.return_code == 0
        and not missing_outputs
        and (bool(missing_grid_segments) if segments else not grid_jsons)
    )
    ok = (dry_run or record.return_code == 0) and not missing_outputs and not missing_grid_json
    return ToolResult(
        ok=ok,
        tool_name="generate_gridmap_from_pcd",
        message=(
            "Generated gridmap from PCD."
            if ok
            else (
                "Missing expected grid_map JSON under requested segments: "
                f"{', '.join(missing_grid_segments)}"
            ) if missing_grid_segments
            else f"Missing expected grid_map JSON under: {settings.clip_data_root / date}" if missing_grid_json
            else f"Missing expected output: {missing_outputs[0]}" if missing_outputs else "Gridmap generation failed."
        ),
        produced_paths=produced_paths,
        commands=[record],
        details={
            "date": date,
            "segments": segments,
            "dry_run": dry_run,
            "grid_json_count": len(grid_jsons),
            "missing_grid_segments": missing_grid_segments,
        },
    )


def extract_and_sync_navigation_data(
    date: str,
    segments: list[str] | None = None,
    processes_num: int = 4,
    topic_whitelist: list[str] | None = None,
    topic_map: dict[str, str] | None = None,
    query_dir: str | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    missing_topic_params = _missing_topic_param_names(topic_whitelist, topic_map, query_dir)
    if missing_topic_params:
        return ToolResult(
            ok=False,
            tool_name="extract_and_sync_navigation_data",
            message=(
                "Missing required explicit navigation topic parameter(s): "
                + ", ".join(missing_topic_params)
            ),
            produced_paths=[settings.clip_data_root / date],
            details={
                "missing_topic_params": missing_topic_params,
                "dry_run": dry_run,
            },
        )
    raw_temp_path = settings.raw_data_root / f"{date}_temp"
    if dry_run:
        selected = _selected_segments_for_dry_run(raw_temp_path, settings.raw_data_root / date, segments)
    else:
        selected = _selected_segments(raw_temp_path, segments)
    effective_topic_whitelist = list(topic_whitelist)
    effective_topic_map = dict(topic_map)
    effective_query_dir = query_dir
    commands = []
    if not selected:
        return ToolResult(
            ok=False,
            tool_name="extract_and_sync_navigation_data",
            message="No selected segments for extract/sync.",
            produced_paths=[settings.clip_data_root / date],
            details={
                "selected_segments": selected,
                "extract_topics": effective_topic_whitelist,
                "sync_topic_map": effective_topic_map,
                "query_dir": effective_query_dir,
                "script_source": "repository",
                "dry_run": dry_run,
            },
        )

    for segment in selected:
        data_path = raw_temp_path / segment
        save_path = settings.clip_data_root / date / segment
        config_dir = save_path / "_agent_config"
        topic_whitelist_file = config_dir / "topic_whitelist.json"
        topic_map_file = config_dir / "topic_map.json"
        _write_json_parameter_file(topic_whitelist_file, effective_topic_whitelist, dry_run)
        _write_json_parameter_file(topic_map_file, effective_topic_map, dry_run)
        commands.append(
            run_command(
                run_u_python_command(
                    settings.runtime,
                    script_name=REPOSITORY_EXTRACT_SCRIPT,
                    args=[
                        "--data_path",
                        data_path,
                        "--save_path",
                        save_path,
                        "--processes_num",
                        str(processes_num),
                    ]
                    + ["--topic_whitelist_file", topic_whitelist_file],
                    ros2_setup_bash=settings.ros2_setup_bash,
                    ros2_ws_setup_bash=settings.ros2_ws_setup_bash,
                    shm_msgs_lib_dir=settings.shm_msgs_lib_dir,
                ),
                cwd=None,
                dry_run=dry_run,
            )
        )
        sync_command = python_data_command(
            settings.runtime,
            REPOSITORY_SYNC_SCRIPT,
            [
                "--data_path",
                save_path,
                "--query_dir",
                effective_query_dir,
                "--output_dir",
                "sync_data",
                "--sequence_prefix",
                f"{segment}_zhigu_wuhan",
                "--processes_num",
                str(processes_num),
                "--topic_map_file",
                topic_map_file,
            ],
        )
        commands.append(
            run_command(
                sync_command,
                cwd=None,
                dry_run=dry_run,
            )
        )

    missing_sync_data = [
        settings.clip_data_root / date / segment / "sync_data"
        for segment in selected
        if not dry_run and not (settings.clip_data_root / date / segment / "sync_data").exists()
    ]
    expected_sync_dirs = tuple(effective_topic_map.values())
    missing_sync_topic_dirs = []
    if not dry_run:
        for segment in selected:
            sync_root = settings.clip_data_root / date / segment / "sync_data"
            if not sync_root.exists():
                continue
            sync_clips = sorted(path for path in sync_root.iterdir() if path.is_dir())
            if not any(
                all(
                    (clip / child_name).is_dir()
                    and any(path.is_file() for path in (clip / child_name).iterdir())
                    for child_name in expected_sync_dirs
                )
                for clip in sync_clips
            ):
                missing_sync_topic_dirs.append(str(sync_root))

    ok = _commands_ok(commands) and not missing_sync_data and not missing_sync_topic_dirs
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
            "selected_segments": selected,
            "extract_topics": effective_topic_whitelist,
            "sync_topic_map": effective_topic_map,
            "query_dir": effective_query_dir,
            "tolerance_ms": SYNC_TOLERANCE_MS,
            "script_source": "repository",
            "missing_sync_data": [str(path) for path in missing_sync_data],
            "missing_sync_topic_dirs": missing_sync_topic_dirs,
            "dry_run": dry_run,
        },
    )


def assemble_finish_temp(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
    selected_sensor_source: str | Path = "",
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
    sensor_source = _validated_sensor_source(
        settings,
        selected_sensor_source,
    )
    if not dry_run:
        missing_calibration_files = _missing_calibration_files(sensor_source)
        if missing_calibration_files:
            raise FileNotFoundError(
                "Selected calibration profile is incomplete: "
                f"{[str(path) for path in missing_calibration_files]}"
            )
    copied_clips: list[str] = []
    incomplete_clips: list[str] = []

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
            for child_name in ("fisheye_front", "r32_rslidar_points"):
                src_child = src_clip / child_name
                if src_child.is_dir() and any(path.is_file() for path in src_child.iterdir()):
                    shutil.copytree(src_child, dst_clip / child_name, dirs_exist_ok=True)
                else:
                    incomplete_clips.append(str(src_clip))

    if not dry_run:
        samples_date_root.mkdir(parents=True, exist_ok=True)

    ok = dry_run or (bool(copied_clips) and not incomplete_clips)
    return ToolResult(
        ok=ok,
        tool_name="assemble_finish_temp",
        message=(
            f"Assembled {len(copied_clips)} clips into finish temp layout."
            if ok
            else "Finish inputs are missing non-empty fisheye or lidar directories."
        ),
        produced_paths=[finish_temp],
        details={
            "date": date,
            "selected_segments": selected,
            "copied_clips": copied_clips,
            "incomplete_clips": sorted(set(incomplete_clips)),
            "sensor_source": str(sensor_source),
            "dry_run": dry_run,
        },
    )


def confirm_navigation_calibration_params(
    date: str,
    segments: list[str] | None = None,
    user_confirmation: str | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
    selected_sensor_source: str | Path = "",
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    sensor_source = _validated_sensor_source(
        settings,
        selected_sensor_source,
    )
    missing_calibration_files = (
        [] if dry_run else _missing_calibration_files(sensor_source)
    )
    finish_temp = settings.finish_data_root / f"{date}_temp"
    target_copy_path = finish_temp / "samples" / date / "<clip>" / "sensors"
    confirmation_prompt = (
        f"当前硬编码相机传感器参数路径: {sensor_source}\n"
        f"目标复制路径: {target_copy_path}\n"
        "请确认该路径适用于本次导航数据处理。"
        "如确认继续，请精确回复 `确认`；如需终止，请回复 `终止`。"
        "如果需要更换参数，请先在服务器对应参数文件或代码中修改。"
        "我没有权利替你修改该文件。"
    )
    confirmed = user_confirmation == "确认" and not missing_calibration_files
    if missing_calibration_files:
        result_message = (
            "所选标定参数目录不完整，缺少文件："
            + ", ".join(str(path) for path in missing_calibration_files)
        )
        error_type = "incomplete_calibration_profile"
    else:
        result_message = confirmation_prompt
        error_type = None if confirmed else "calibration_params_not_confirmed"

    return ToolResult(
        ok=confirmed,
        tool_name="confirm_navigation_calibration_params",
        message=result_message,
        produced_paths=[],
        details={
            "date": date,
            "segments": segments,
            "finish_temp": str(finish_temp),
            "sensor_source": str(sensor_source),
            "target_copy_path": str(target_copy_path),
            "requires_user_confirmation": True,
            "user_confirmation": user_confirmation,
            "confirmed": confirmed,
            "missing_calibration_files": [
                str(path) for path in missing_calibration_files
            ],
            "confirmation_prompt": confirmation_prompt,
            "error_type": error_type,
            "dry_run": dry_run,
        },
    )


def prepare_gridmap_for_projection(
    date: str,
    segments: list[str] | None = None,
    finish_temp_path: str | Path | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
    gridmap_variant: str | None = None,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings) if finish_temp_path is not None else settings.finish_data_root / f"{date}_temp"
    commands = []
    generated_result: ToolResult | None = None
    gridmap_dirs = _discover_gridmap_dirs(date, segments, settings)
    source_mode = "existing_gridmap"

    if not gridmap_dirs and gridmap_variant == "copy_existing_gridmap":
        return ToolResult(
            ok=False,
            tool_name="prepare_gridmap_for_projection",
            message=f"No existing grid_map found under: {settings.clip_data_root / date}",
            produced_paths=[root / "samples" / date],
            details={
                "date": date,
                "segments": segments,
                "source_mode": "missing_existing_gridmap",
                "gridmap_variant": gridmap_variant,
                "prepared_gridmap_count": 0,
                "generated_command_count": 0,
                "copied_targets": [],
                "dry_run": dry_run,
            },
        )

    if not gridmap_dirs and gridmap_variant in {None, "generate_from_pcd"}:
        generated_result = generate_gridmap_from_pcd(date, segments, settings=settings, dry_run=dry_run)
        commands.extend(generated_result.commands)
        source_mode = "generated_from_pointcloud"
        if not dry_run and generated_result.ok:
            gridmap_dirs = _discover_gridmap_dirs(date, segments, settings)

    prepared_count = 0
    copied_targets: list[str] = []
    if gridmap_dirs:
        for gridmap_dir, clip_name in gridmap_dirs:
            target_dir = root / "samples" / date / clip_name / "grid_map"
            for source_file in sorted(path for path in gridmap_dir.iterdir() if path.is_file()):
                prepared_count += 1
                target_file = target_dir / source_file.name
                copied_targets.append(str(target_file))
                _copy_gridmap_file(source_file, target_file, dry_run)

    ok = dry_run or bool(gridmap_dirs)
    if generated_result is not None and not dry_run:
        ok = generated_result.ok and bool(gridmap_dirs)
    return ToolResult(
        ok=ok,
        tool_name="prepare_gridmap_for_projection",
        message=(
            "Prepared grid_map for projection."
            if ok
            else f"No grid_map found under: {settings.clip_data_root / date}"
        ),
        produced_paths=[root / "samples" / date],
        commands=commands,
        details={
            "date": date,
            "segments": segments,
            "source_mode": source_mode,
            "gridmap_variant": gridmap_variant,
            "prepared_gridmap_count": prepared_count,
            "generated_command_count": len(commands),
            "copied_targets": copied_targets,
            "dry_run": dry_run,
        },
    )


def run_noobscene_preprocessing(
    finish_temp_path: str | Path,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
    *,
    localization_source: str = "odom",
    localization_conversion: str = "odom_to_ins",
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    noobscene_root = settings.processing_root / "NoobScenes"
    trainval_path = root / "v1.0-trainval"
    map_path = root / "maps" / "map.png"
    odom_policy = localization_source == "odom" and localization_conversion == "odom_to_ins"
    native_ins_policy = localization_source == "ins" and localization_conversion == "none"
    if not (odom_policy or native_ins_policy):
        return ToolResult(
            ok=False,
            tool_name="run_noobscene_preprocessing",
            message=(
                "unsupported localization policy for NoobScenes preprocessing: "
                f"source={localization_source}, conversion={localization_conversion}."
            ),
            produced_paths=[],
            commands=[],
            details={
                "error_type": "unsupported_localization_policy",
                "localization_source": localization_source,
                "localization_conversion": localization_conversion,
                "dry_run": dry_run,
            },
        )

    noobscene_main = (
        "./main_smart.py" if native_ins_policy else "./main_smart_odom.py"
    )
    commands = [
        run_command(
            python_data_command(
                settings.runtime,
                noobscene_root / "include" / "0_creat_box.py",
                ["--dataset_root", root],
            ),
            dry_run=dry_run,
        ),
    ]
    if odom_policy:
        commands.extend([
            run_command(
                python_data_command(
                    settings.runtime,
                    noobscene_root / "include" / "1_odom_convert.py",
                    ["--temp_path", root],
                ),
                dry_run=dry_run,
            ),
            run_command(
                python_data_command(
                    settings.runtime,
                    noobscene_root / "include" / "2_resize.py",
                    ["--temp_path", root],
                ),
                dry_run=dry_run,
            ),
        ])

    if dry_run or _commands_ok(commands):
        if not dry_run:
            trainval_path.mkdir(parents=True, exist_ok=True)
            _replace_with_symlink(noobscene_root / "samples", root / "samples")

        commands.append(
            run_command(
                python_data_command(settings.runtime, noobscene_main),
                cwd=noobscene_root,
                dry_run=dry_run,
            )
        )
        commands.append(
            run_command(
                python_data_command(
                    settings.runtime,
                    settings.processing_root / "0_1th_box" / "img2video.py",
                    ["--dataset_root", root],
                ),
                dry_run=dry_run,
            )
        )

        if not dry_run and commands[-2].return_code == 0:
            develop_path = noobscene_root / "v1.0-develop"
            if develop_path.exists():
                _copy_directory_contents(develop_path, trainval_path)
            source_map = noobscene_root / "maps" / "map.png"
            if source_map.exists() and not map_path.exists():
                map_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_map, map_path)

    produced_paths = [trainval_path, map_path]
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
        details={
            "dry_run": dry_run,
            "localization_source": localization_source,
            "localization_conversion": localization_conversion,
            "noobscene_main": (
                "main_smart.py" if native_ins_policy else "main_smart_odom.py"
            ),
        },
    )


def run_initial_annotation_gui(
    finish_temp_path: str | Path,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    record = run_command(
        python_data_command(settings.runtime, settings.gen_box_script, ["--dataset_root", root]),
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


def run_tracking(
    finish_temp_path: str | Path,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    commands, tracking_error, details = _run_tracking_loop(root, settings, dry_run)
    ok = tracking_error is None and _commands_ok(commands)
    return ToolResult(
        ok=ok,
        tool_name="run_tracking",
        message=(
            "Ran tracking."
            if ok
            else tracking_error if tracking_error
            else "Tracking failed."
        ),
        produced_paths=[root],
        commands=commands,
        details=details,
    )


def _projection_variant_from_execution_args(
    *,
    projection_variant: str | None,
    localization_source: str,
) -> str:
    expected_variant = {
        "ins": "cjl_with_gridmap",
        "odom": "cjl_0525_with_gridmap",
    }.get(localization_source)
    if expected_variant is None:
        raise ValueError("localization_source must be one of: ins, odom")
    if projection_variant in {None, ""}:
        return expected_variant
    if projection_variant != expected_variant:
        raise ValueError(
            "projection_variant does not match localization_source: "
            f"expected {expected_variant} for {localization_source}"
        )
    return projection_variant


def _trajectory_script_for_projection_variant(projection_variant: str) -> tuple[str, str]:
    if projection_variant == "cjl_with_gridmap":
        return "2_othermethod_cjl.py", "cjl_with_gridmap"
    if projection_variant == "cjl_0525_with_gridmap":
        return "2_othermethod_cjl_0525.py", "cjl_0525_with_gridmap"
    raise ValueError(
        "projection_variant must be one of: cjl_with_gridmap, cjl_0525_with_gridmap"
    )


def run_projection_and_trajectory(
    finish_temp_path: str | Path,
    finish_path: str | Path,
    projection_variant: str | None = None,
    localization_source: str = "odom",
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    settings = settings or NavigationSettings()
    root = _resolve_data_path(finish_temp_path, settings)
    final = _resolve_data_path(finish_path, settings)
    pt_project = settings.processing_root / "2_pt_project"
    selected_projection_variant = _projection_variant_from_execution_args(
        projection_variant=projection_variant,
        localization_source=localization_source,
    )
    trajectory_script, effective_projection_variant = _trajectory_script_for_projection_variant(
        selected_projection_variant
    )
    speed_direction_script = (
        "4_speed_direction_Ins.py"
        if localization_source == "ins"
        else "4_speed_direction_odom.py"
    )
    commands = [
        run_command(
            python_data_command(settings.runtime, "main.py", ["--data_root", root]),
            cwd=settings.processing_root / "NuscenesAanlysis_smart_pts_project",
            dry_run=dry_run,
        ),
        run_command(
            python_data_command(settings.runtime, pt_project / "0_img2world.py", [root]),
            cwd=pt_project,
            dry_run=dry_run,
        ),
        run_command(
            python_data_command(settings.runtime, pt_project / speed_direction_script, [root]),
            cwd=pt_project,
            dry_run=dry_run,
        ),
        run_command(
            python_data_command(settings.runtime, pt_project / trajectory_script, [root]),
            cwd=pt_project,
            dry_run=dry_run,
        ),
        run_command(
            python_data_command(
                settings.runtime,
                pt_project / "3_move_dir.py",
                [
                    "--root_path",
                    final,
                    "--temp_path",
                    root,
                ],
            ),
            cwd=pt_project,
            dry_run=dry_run,
        ),
    ]
    produced_paths = [final]
    missing_outputs = _missing_outputs(produced_paths, dry_run)
    ok = _commands_and_outputs_ok(commands, produced_paths, dry_run)
    return ToolResult(
        ok=ok,
        tool_name="run_projection_and_trajectory",
        message=(
            "Ran projection and trajectory."
            if ok
            else f"Missing expected output: {missing_outputs[0]}" if missing_outputs else "Projection/trajectory failed."
        ),
        produced_paths=produced_paths,
        commands=commands,
        details={
            "dry_run": dry_run,
            "projection_variant": effective_projection_variant,
            "trajectory_script": trajectory_script,
            "localization_source": localization_source,
            "speed_direction_script": speed_direction_script,
        },
    )


def validate_navigation_outputs(
    date: str,
    segments: list[str] | None = None,
    settings: NavigationSettings | None = None,
    dry_run: bool = False,
) -> ToolResult:
    date = _validate_date(date)
    settings = settings or NavigationSettings()
    final = settings.finish_data_root / date
    selected_segments = (
        list(segments)
        if segments is not None
        else sorted(
            path.name
            for path in final.iterdir()
            if path.is_dir() and path.name != "samples"
        )
        if final.exists()
        else []
    )
    grid_map_dirs = []
    complete_segments = []
    if not dry_run and final.exists():
        for segment_name in selected_segments:
            segment_path = final / segment_name
            segment_grid_maps = sorted(
                clip / "grid_map"
                for clip in segment_path.iterdir()
                if clip.is_dir()
                and (clip / "grid_map").is_dir()
                and any((clip / "grid_map").glob("*.json"))
            ) if segment_path.is_dir() else []
            grid_map_dirs.extend(segment_grid_maps)
            if segment_grid_maps:
                complete_segments.append(segment_name)
    exists = final.exists()
    missing_segments = sorted(set(selected_segments) - set(complete_segments))
    has_grid_map = bool(selected_segments) and not missing_segments
    ok = dry_run or (exists and has_grid_map)
    missing_label = "grid_map" if exists and not has_grid_map else str(final)
    return ToolResult(
        ok=ok,
        tool_name="validate_navigation_outputs",
        message="Validation completed." if ok else f"Missing final output: {missing_label}",
        produced_paths=[final, *grid_map_dirs],
        details={
            "exists": exists,
            "has_grid_map": has_grid_map,
            "selected_segments": selected_segments,
            "missing_grid_map_segments": missing_segments,
            "checked_outputs": ["finish_data", "grid_map"],
            "dry_run": dry_run,
        },
    )
