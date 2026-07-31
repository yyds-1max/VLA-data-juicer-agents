"""Headless command-log driver for the frozen legacy trajectory editor.

This file intentionally contains no trajectory math.  It loads the frozen
business module and invokes its existing editor operations under Xvfb.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any


_TARGET_REF_RE = re.compile(r"^target_[0-9a-f]{32}$")
_TARGET_TYPE_RE = re.compile(r"^(?:master|other[0-9]+)$")
_KINDS = {
    "set_position",
    "set_direction",
    "set_speed",
    "delete_target",
    "add_missing_target",
    "restore_frame",
    "toggle_pass",
}


def _load_request(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "legacy_module_path",
            "segment_root",
            "target_bindings",
            "commands",
        }
        or document["schema_version"] != 1
        or not isinstance(document["legacy_module_path"], str)
        or not isinstance(document["segment_root"], str)
        or not isinstance(document["target_bindings"], dict)
        or not isinstance(document["commands"], list)
    ):
        raise ValueError("invalid Fix driver request")
    bindings = document["target_bindings"]
    if (
        not bindings
        or any(
            not isinstance(target_ref, str)
            or _TARGET_REF_RE.fullmatch(target_ref) is None
            or not isinstance(target_type, str)
            or _TARGET_TYPE_RE.fullmatch(target_type) is None
            for target_ref, target_type in bindings.items()
        )
        or len(set(bindings.values())) != len(bindings)
    ):
        raise ValueError("invalid Fix target bindings")
    for command in document["commands"]:
        if not isinstance(command, dict) or command.get("kind") not in _KINDS:
            raise ValueError("invalid Fix command")
    return document


def _load_frozen_module(module_path: Path, segment_root: Path) -> Any:
    import sys

    sys.argv = [
        str(module_path),
        "--root_data",
        str(segment_root),
    ]
    specification = importlib.util.spec_from_file_location(
        "navigation_odom_v1_frozen_fix",
        module_path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("frozen Fix module cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _frame_index(command: dict[str, Any], total: int) -> int:
    value = command.get("frame_index")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("frame_index must be an integer")
    if value < 0 or value >= total:
        raise ValueError("frame_index is out of bounds")
    return value


def _finite_number(command: dict[str, Any], key: str) -> float:
    value = command.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{key} must be finite")
    return result


def _target_type(
    command: dict[str, Any],
    bindings: dict[str, str],
) -> str:
    target_ref = command.get("target_ref")
    if not isinstance(target_ref, str) or target_ref not in bindings:
        raise ValueError("Fix command target is not bound")
    return bindings[target_ref]


def _ensure_added_info(editor: Any, timestamp: str, target_type: str) -> dict[str, Any]:
    position = editor.modified_target_points[timestamp].get(target_type)
    if position is None:
        raise ValueError("target has no position")
    info = editor.added_target_info[timestamp].get(target_type)
    if not isinstance(info, dict):
        direction = (
            editor.speed_direction_data.get(timestamp, {})
            .get(target_type, {})
            .get("direction_object", 0.0)
        )
        info = {
            "pos": position,
            "dir": float(direction),
            "arrow_line": None,
        }
        editor.added_target_info[timestamp][target_type] = info
    return info


def _apply_command_log(
    editor: Any,
    *,
    bindings: dict[str, str],
    commands: list[dict[str, Any]],
) -> dict[tuple[int, str], float]:
    speed_overrides: dict[tuple[int, str], float] = {}

    def recompute(frame_index: int) -> None:
        editor.current_index = frame_index
        timestamp = editor.timestamps[frame_index]
        for target_type in editor.target_files:
            # The frozen GUI initializes editing metadata for every visible
            # target while rendering the frame, before the operator can press
            # OK.  The headless adapter does not render that frame, so replay
            # the same initialization explicitly.  Otherwise a multi-target
            # frame fails when only one target was edited and on_ok_click()
            # reads the untouched target's direction from a None value.
            if editor.modified_target_points[timestamp].get(target_type) is not None:
                _ensure_added_info(editor, timestamp, target_type)
            override = speed_overrides.get((frame_index, target_type))
            if override is not None:
                editor.target_speed_inputs[timestamp][target_type] = (
                    SimpleNamespace(text=str(override))
                )
        editor.on_ok_click(None)

    for command in commands:
        kind = command["kind"]
        frame_index = _frame_index(command, len(editor.timestamps))
        editor.current_index = frame_index
        timestamp = editor.timestamps[frame_index]

        if kind == "restore_frame":
            editor.on_back_click(None)
            speed_overrides = {
                key: value
                for key, value in speed_overrides.items()
                if key[0] != frame_index
            }
            continue
        if kind == "toggle_pass":
            value = command.get("value")
            if not isinstance(value, bool):
                raise ValueError("toggle_pass.value must be boolean")
            current = bool(
                editor.modified_trajectory[timestamp].get("pass", False)
            )
            if current != value:
                editor.on_pass_click(None)
            continue

        target_type = _target_type(command, bindings)
        if target_type not in editor.target_files:
            raise ValueError("bound target is absent from the legacy input")
        if kind == "delete_target":
            editor.on_delete_target(target_type)
            continue
        if kind == "add_missing_target":
            if frame_index == 0:
                raise ValueError("a missing target cannot be added to frame zero")
            editor.on_add_missing_target(target_type)
            # The legacy GUI required the operator to click OK after pressing
            # "++ target".  The Web command is intentionally atomic: restoring
            # a missing target must also run the same frozen trajectory
            # recomputation, otherwise the saved target has a position but an
            # empty trajectory.
            recompute(frame_index)
            continue
        if kind == "set_position":
            position = (
                _finite_number(command, "x"),
                _finite_number(command, "y"),
            )
            editor.modified_target_points[timestamp][target_type] = position
            info = _ensure_added_info(editor, timestamp, target_type)
            info["pos"] = position
            recompute(frame_index)
            continue
        if kind == "set_direction":
            info = _ensure_added_info(editor, timestamp, target_type)
            info["dir"] = _finite_number(command, "direction")
            recompute(frame_index)
            continue
        if kind == "set_speed":
            speed = _finite_number(command, "speed")
            if speed < 0:
                raise ValueError("speed must be non-negative")
            speed_overrides[(frame_index, target_type)] = speed
            recompute(frame_index)
            continue
        raise ValueError("unsupported Fix command")
    return speed_overrides


def _point_list(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        return []
    result: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return []
        values: list[float] = []
        for item in point[:3]:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return []
            number = float(item)
            if not math.isfinite(number):
                return []
            values.append(number)
        result.append(values)
    return result


def _project_points(editor: Any, points: list[list[float]]) -> list[list[float]]:
    if not points:
        return []
    projected = editor.project_lidar_to_image(
        points,
        editor.sensor_params["image_size"],
        editor.sensor_params,
    )
    result: list[list[float]] = []
    for point in projected:
        try:
            first = float(point[0])
            second = float(point[1])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if math.isfinite(first) and math.isfinite(second):
            result.append([first, second])
    return result


def _preview_speed(
    editor: Any,
    *,
    frame_index: int,
    timestamp: str,
    target_type: str,
    speed_overrides: dict[tuple[int, str], float],
) -> float:
    override = speed_overrides.get((frame_index, target_type))
    if override is not None:
        return override
    speed_info = (
        editor.speed_direction_data.get(timestamp, {})
        .get(target_type, {})
        .get("speed_object", [0.0, 0.0, 0.0])
    )
    if not isinstance(speed_info, (list, tuple)) or len(speed_info) < 2:
        return 0.0
    try:
        return float(math.hypot(float(speed_info[0]), float(speed_info[1])))
    except (TypeError, ValueError):
        return 0.0


def _write_preview_state(
    editor: Any,
    *,
    bindings: dict[str, str],
    speed_overrides: dict[tuple[int, str], float],
    output_path: Path,
) -> None:
    inverse_bindings = {
        target_type: target_ref
        for target_ref, target_type in bindings.items()
    }
    frames: list[dict[str, Any]] = []
    for frame_index, timestamp in enumerate(editor.timestamps):
        raw_frame = editor.modified_trajectory.get(timestamp, {})
        targets: dict[str, dict[str, Any]] = {}
        for target_type, target_ref in inverse_bindings.items():
            raw_target = raw_frame.get(target_type, {})
            if not isinstance(raw_target, dict):
                raw_target = {}
            raw_position = editor.modified_target_points[timestamp].get(
                target_type
            )
            try:
                position_values = [
                    float(raw_position[0]),
                    float(raw_position[1]),
                ]
                position = (
                    position_values
                    if all(math.isfinite(value) for value in position_values)
                    else None
                )
            except (IndexError, KeyError, TypeError, ValueError):
                position = None
            added_info = editor.added_target_info[timestamp].get(target_type)
            raw_direction = (
                added_info.get("dir")
                if isinstance(added_info, dict)
                else (
                    editor.speed_direction_data.get(timestamp, {})
                    .get(target_type, {})
                    .get("direction_object", 0.0)
                )
            )
            try:
                direction_value = float(raw_direction)
                direction = (
                    direction_value
                    if math.isfinite(direction_value)
                    else 0.0
                )
            except (TypeError, ValueError):
                direction = 0.0
            trajectory = _point_list(raw_target.get("traj"))
            camera_position = _project_points(
                editor,
                [[position[0], position[1], 0.0]] if position else [],
            )
            color = raw_target.get("color")
            targets[target_ref] = {
                "label": (
                    "Master"
                    if target_type == "master"
                    else f"Other {target_type.removeprefix('other')}"
                ),
                "position": position,
                "direction": direction,
                "speed": _preview_speed(
                    editor,
                    frame_index=frame_index,
                    timestamp=timestamp,
                    target_type=target_type,
                    speed_overrides=speed_overrides,
                ),
                "color": (
                    [str(item) for item in color[:3]]
                    if isinstance(color, list)
                    else []
                ),
                "image_box": None,
                "trajectory_points": trajectory,
                "camera_position": (
                    camera_position[0] if camera_position else None
                ),
                "camera_trajectory_points": _project_points(
                    editor,
                    trajectory,
                ),
            }
        frames.append(
            {
                "frame_index": frame_index,
                "private_frame_key": timestamp,
                "camera_available": (
                    Path(editor.fisheye_folder) / f"{timestamp}.jpg"
                ).is_file(),
                "gridmap_available": (
                    Path(editor.clip_path) / "grid_map" / f"{timestamp}.json"
                ).is_file(),
                "pass": bool(raw_frame.get("pass", False)),
                "targets": targets,
            }
        )
    document = {
        "schema_version": 1,
        "source": "fix_revision",
        "target_bindings": dict(sorted(bindings.items())),
        "frame_count": len(frames),
        "frames": frames,
    }
    with output_path.open("x", encoding="utf-8") as stream:
        json.dump(
            document,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    request = _load_request(arguments.request)
    module_path = Path(request["legacy_module_path"])
    segment_root = Path(request["segment_root"])
    module = _load_frozen_module(module_path, segment_root)
    editor = module.TrajectoryEditor(str(segment_root))
    try:
        speed_overrides = _apply_command_log(
            editor,
            bindings=request["target_bindings"],
            commands=request["commands"],
        )
        editor.current_index = len(editor.timestamps) - 1
        editor.on_next_click(None)
        _write_preview_state(
            editor,
            bindings=request["target_bindings"],
            speed_overrides=speed_overrides,
            output_path=segment_root / ".system_fix_preview.json",
        )
    finally:
        module.plt.close(editor.fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
