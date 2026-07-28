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
) -> None:
    speed_overrides: dict[tuple[int, str], float] = {}

    def recompute(frame_index: int) -> None:
        editor.current_index = frame_index
        timestamp = editor.timestamps[frame_index]
        for target_type in editor.target_files:
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
        _apply_command_log(
            editor,
            bindings=request["target_bindings"],
            commands=request["commands"],
        )
        editor.current_index = len(editor.timestamps) - 1
        editor.on_next_click(None)
    finally:
        module.plt.close(editor.fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
