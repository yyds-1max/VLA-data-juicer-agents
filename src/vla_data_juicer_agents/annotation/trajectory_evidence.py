"""Deterministic, read-only projection of frozen navigation trajectory output.

This module does not calculate or repair trajectories.  It only joins the
files already produced by the frozen M2 Runtime into a bounded private state
that can be projected through opaque target and frame identities.
"""

from __future__ import annotations

import json
import binascii
from collections import Counter
from dataclasses import dataclass
from math import hypot, isclose, isfinite
from pathlib import Path
import re
import struct
from typing import Any
import zlib

from vla_data_juicer_agents.annotation.runtime import (
    RuntimeExecutionError,
    _read_stable_regular_bytes,
    _regular_directory,
)


_TARGET_TYPE_RE = re.compile(r"^(?:master|other[0-9]+)$")
_TARGET_REF_RE = re.compile(r"^target_[0-9a-f]{32}$")
_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_FRAMES = 100_000
_MAX_TRAJECTORY_POINTS = 10_000
_MAX_GRID_SIDE = 2_048
_FIX_PREVIEW_FILE = ".system_fix_preview.json"


@dataclass(frozen=True)
class GridmapPayload:
    width: int
    height: int
    values: list[float]
    background: float
    resolution: float
    x_range: tuple[float, float]
    y_range: tuple[float, float]


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    content = _read_stable_regular_bytes(path)
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            f"The {label} document exceeds the evidence limit.",
        )
    try:
        value = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            f"The {label} document is invalid.",
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            f"The {label} document must be an object.",
        )
    return value


def _one_file(root: Path, pattern: str, *, label: str) -> Path:
    matches = sorted(
        path
        for path in root.glob(pattern)
        if path.is_file() and not path.is_symlink()
    )
    if len(matches) != 1:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            f"The trajectory revision does not contain exactly one {label}.",
        )
    return matches[0]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _number_pair(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    x = _finite_number(value[0])
    y = _finite_number(value[1])
    if x is None or y is None:
        return None
    return [x, y]


def _number_list(
    value: Any,
    *,
    minimum: int,
    maximum: int,
) -> list[float] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) < minimum
        or len(value) > maximum
    ):
        return None
    result: list[float] = []
    for item in value:
        number = _finite_number(item)
        if number is None:
            return None
        result.append(number)
    return result


def _trajectory_points(value: Any) -> list[list[float]]:
    if not isinstance(value, list) or len(value) > _MAX_TRAJECTORY_POINTS:
        return []
    result: list[list[float]] = []
    for point in value:
        normalized = _number_list(point, minimum=2, maximum=3)
        if normalized is None:
            return []
        result.append(normalized)
    return result


def _gridmap_payload(content: bytes) -> GridmapPayload:
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The gridmap evidence exceeds the supported limit.",
        )
    try:
        payload = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The gridmap evidence is invalid.",
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The gridmap evidence must be an object.",
        )
    resolution = _finite_number(payload.get("resolution"))
    x_range = _number_pair(payload.get("x_range"))
    y_range = _number_pair(payload.get("y_range"))
    raw_grid_size = payload.get("grid_size")
    raw_data = payload.get("data")
    if (
        resolution is None
        or resolution <= 0
        or x_range is None
        or y_range is None
        or not isinstance(raw_data, list)
    ):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The gridmap evidence metadata is invalid.",
        )
    x_cells = (x_range[1] - x_range[0]) / resolution
    y_cells = (y_range[1] - y_range[0]) / resolution
    width = round(x_cells)
    height = round(y_cells)
    if (
        x_range[0] >= x_range[1]
        or y_range[0] >= y_range[1]
        or not isclose(x_cells, width, rel_tol=1e-9, abs_tol=1e-6)
        or not isclose(y_cells, height, rel_tol=1e-9, abs_tol=1e-6)
        or width < 1
        or height < 1
        or width > _MAX_GRID_SIDE
        or height > _MAX_GRID_SIDE
    ):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The gridmap evidence dimensions are invalid.",
        )
    # Production gridmaps use a scalar side length.  Some historical test
    # fixtures use the legacy [height, width] form.  The frozen Fix editor
    # derives the matrix dimensions from ranges and resolution, so both
    # metadata shapes must agree with those derived dimensions.
    if isinstance(raw_grid_size, int) and not isinstance(raw_grid_size, bool):
        grid_size_matches = (
            width == height == raw_grid_size
        )
    elif (
        isinstance(raw_grid_size, list)
        and len(raw_grid_size) == 2
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in raw_grid_size
        )
    ):
        grid_size_matches = raw_grid_size == [height, width]
    else:
        grid_size_matches = False
    if not grid_size_matches or len(raw_data) != width * height:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The gridmap evidence dimensions are invalid.",
        )
    values: list[float] = []
    for value in raw_data:
        number = _finite_number(value)
        if number is None:
            raise RuntimeExecutionError(
                "trajectory_evidence_unavailable",
                "The gridmap evidence contains an invalid cell.",
            )
        values.append(number)
    background = Counter(values).most_common(1)[0][0]
    return GridmapPayload(
        width=width,
        height=height,
        values=values,
        background=background,
        resolution=resolution,
        x_range=(x_range[0], x_range[1]),
        y_range=(y_range[0], y_range[1]),
    )


def gridmap_metadata(content: bytes) -> dict[str, Any]:
    payload = _gridmap_payload(content)
    return {
        "width": payload.height,
        "height": payload.width,
        "resolution": payload.resolution,
        "x_range": list(payload.x_range),
        "y_range": list(payload.y_range),
    }


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def render_gridmap_png(content: bytes) -> tuple[bytes, int, int]:
    """Render the legacy BEV grid orientation without changing source data."""

    payload = _gridmap_payload(content)
    width = payload.width
    height = payload.height
    values = payload.values
    background = payload.background
    foreground = [value for value in values if value != background]
    minimum = min(foreground) if foreground else 0.0
    maximum = max(foreground) if foreground else 1.0
    span = max(maximum - minimum, 1e-12)
    output_width = height
    output_height = width
    rows = [
        bytearray([0]) + bytearray(output_width * 3)
        for _ in range(output_height)
    ]
    for source_y in range(height):
        for source_x in range(width):
            value = values[source_y * width + source_x]
            if value == background:
                color = (244, 247, 250)
            else:
                normalized = max(0.0, min(1.0, (value - minimum) / span))
                # A bounded viridis-like reverse ramp mirrors the legacy
                # ``viridis_r`` evidence view without importing plotting code.
                color = (
                    int(68 + 185 * (1.0 - normalized)),
                    int(1 + 220 * (1.0 - abs(normalized - 0.5) * 2.0)),
                    int(84 + 146 * normalized),
                )
            # Legacy Fix displays scatter(grid_y, grid_x), reverses the
            # horizontal Y axis, and keeps X-forward increasing upward.
            output_x = height - 1 - source_y
            output_y = width - 1 - source_x
            pixel_offset = 1 + output_x * 3
            rows[output_y][pixel_offset : pixel_offset + 3] = bytes(color)
    scanlines = b"".join(bytes(row) for row in rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(
                ">IIBBBBB",
                output_width,
                output_height,
                8,
                2,
                0,
                0,
                0,
            ),
        )
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + _png_chunk(b"IEND", b"")
    )
    return png, output_width, output_height


def _target_positions(
    segment_root: Path,
    *,
    target_type: str,
    frame_count: int,
) -> list[list[float] | None]:
    files = sorted(
        path
        for path in segment_root.glob(f"{target_type}_*.txt")
        if path.is_file()
        and not path.is_symlink()
        and not path.name.startswith("img_")
    )
    if len(files) != 1:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The trajectory target position file set is incomplete.",
        )
    content = _read_stable_regular_bytes(files[0])
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "A trajectory target position file exceeds the evidence limit.",
        )
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "A trajectory target position file is invalid.",
        ) from exc
    if len(lines) > frame_count:
        lines = lines[:frame_count]
    positions: list[list[float] | None] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            positions.append(None)
            continue
        try:
            values = [float(value) for value in stripped.split()]
        except ValueError as exc:
            raise RuntimeExecutionError(
                "trajectory_evidence_unavailable",
                "A trajectory target position row is invalid.",
            ) from exc
        if len(values) < 2 or any(not isfinite(value) for value in values):
            raise RuntimeExecutionError(
                "trajectory_evidence_unavailable",
                "A trajectory target position row is invalid.",
            )
        positions.append([values[0], values[1]])
    positions.extend([None] * (frame_count - len(positions)))
    return positions


def _target_label(target_type: str) -> str:
    if target_type == "master":
        return "Master"
    return f"Other {target_type.removeprefix('other')}"


def build_trajectory_revision_state(
    segment_root: Path,
    *,
    target_bindings: dict[str, str],
) -> dict[str, Any]:
    """Create a private evidence index without changing frozen output."""

    if not _regular_directory(segment_root):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The trajectory revision artifact is unavailable.",
        )
    if (
        not target_bindings
        or any(
            _TARGET_REF_RE.fullmatch(target_ref) is None
            or _TARGET_TYPE_RE.fullmatch(target_type) is None
            for target_ref, target_type in target_bindings.items()
        )
        or len(set(target_bindings.values())) != len(target_bindings)
    ):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The trajectory target binding is invalid.",
        )

    trajectory_path = _one_file(
        segment_root,
        "*_trajectory.json",
        label="trajectory file",
    )
    speed_path = _one_file(
        segment_root,
        "*_speed_direction.json",
        label="speed/direction file",
    )
    trajectory = _load_object(trajectory_path, label="trajectory")
    speed_direction = _load_object(speed_path, label="speed/direction")
    try:
        frame_keys = sorted(trajectory, key=float)
    except (TypeError, ValueError) as exc:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The trajectory frame keys are invalid.",
        ) from exc
    if not frame_keys or len(frame_keys) > _MAX_FRAMES:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The trajectory frame count is unsupported.",
        )
    if any(
        key not in speed_direction
        or not isinstance(trajectory[key], dict)
        or not isinstance(speed_direction[key], dict)
        for key in frame_keys
    ):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The trajectory and motion frame sets are inconsistent.",
        )

    inverse_bindings = {
        target_type: target_ref
        for target_ref, target_type in target_bindings.items()
    }
    positions = {
        target_type: _target_positions(
            segment_root,
            target_type=target_type,
            frame_count=len(frame_keys),
        )
        for target_type in inverse_bindings
    }
    frames: list[dict[str, Any]] = []
    for frame_index, frame_key in enumerate(frame_keys):
        raw_trajectory_frame = trajectory[frame_key]
        raw_speed_frame = speed_direction[frame_key]
        projected_targets: dict[str, dict[str, Any]] = {}
        for target_type, target_ref in inverse_bindings.items():
            raw_target = raw_trajectory_frame.get(target_type)
            raw_motion = raw_speed_frame.get(target_type)
            trajectory_target = (
                raw_target if isinstance(raw_target, dict) else {}
            )
            motion_target = raw_motion if isinstance(raw_motion, dict) else {}
            velocity = _number_list(
                motion_target.get("speed_object"),
                minimum=2,
                maximum=3,
            )
            speed = (
                hypot(velocity[0], velocity[1])
                if velocity is not None
                else None
            )
            direction = _finite_number(
                motion_target.get("direction_object"),
            )
            color = trajectory_target.get("color")
            projected_targets[target_ref] = {
                "label": _target_label(target_type),
                "position": positions[target_type][frame_index],
                "direction": direction,
                "speed": speed,
                "color": (
                    [str(item) for item in color[:3]]
                    if isinstance(color, list)
                    else []
                ),
                "image_box": _number_list(
                    trajectory_target.get("img"),
                    minimum=4,
                    maximum=4,
                ),
                "trajectory_points": _trajectory_points(
                    trajectory_target.get("traj"),
                ),
            }
        camera = segment_root / "fisheye_front" / f"{frame_key}.jpg"
        gridmap = segment_root / "grid_map" / f"{frame_key}.json"
        rendered_gridmap_width = None
        rendered_gridmap_height = None
        gridmap_available = gridmap.is_file() and not gridmap.is_symlink()
        if gridmap_available:
            try:
                payload = _gridmap_payload(
                    _read_stable_regular_bytes(gridmap)
                )
                # The legacy Fix view plots ``grid_y`` on the horizontal axis
                # and ``grid_x`` on the vertical axis.  The safe PNG therefore
                # has the transposed display dimensions.
                rendered_gridmap_width = payload.height
                rendered_gridmap_height = payload.width
                gridmap_resolution = payload.resolution
                gridmap_x_range = list(payload.x_range)
                gridmap_y_range = list(payload.y_range)
            except (OSError, RuntimeExecutionError):
                gridmap_available = False
                gridmap_resolution = None
                gridmap_x_range = None
                gridmap_y_range = None
        else:
            gridmap_resolution = None
            gridmap_x_range = None
            gridmap_y_range = None
        frames.append(
            {
                "frame_index": frame_index,
                "private_frame_key": frame_key,
                "camera_available": camera.is_file() and not camera.is_symlink(),
                "gridmap_available": gridmap_available,
                "gridmap_width": rendered_gridmap_width,
                "gridmap_height": rendered_gridmap_height,
                "gridmap_resolution": gridmap_resolution,
                "gridmap_x_range": gridmap_x_range,
                "gridmap_y_range": gridmap_y_range,
                "pass": bool(raw_trajectory_frame.get("pass", False)),
                "targets": projected_targets,
            }
        )
    return {
        "schema_version": 1,
        "target_bindings": dict(sorted(target_bindings.items())),
        "frame_count": len(frames),
        "frames": frames,
    }


def load_fix_revision_preview_state(
    segment_root: Path,
    *,
    expected_target_bindings: dict[str, str],
) -> dict[str, Any]:
    """Load the bounded evidence emitted by the frozen Fix adapter.

    The adapter derives all trajectory and camera projection points by calling
    the legacy editor itself.  This reader only validates and exposes that
    already-produced evidence; it does not reproduce Fix math.
    """

    if not _regular_directory(segment_root):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The Fix revision artifact is unavailable.",
        )
    preview = _load_object(
        segment_root / _FIX_PREVIEW_FILE,
        label="Fix revision preview",
    )
    if (
        preview.get("schema_version") != 1
        or preview.get("source") != "fix_revision"
        or preview.get("target_bindings")
        != dict(sorted(expected_target_bindings.items()))
        or not isinstance(preview.get("frames"), list)
        or preview.get("frame_count") != len(preview["frames"])
        or not preview["frames"]
        or len(preview["frames"]) > _MAX_FRAMES
    ):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The Fix revision preview contract is invalid.",
        )
    expected_refs = set(expected_target_bindings)
    frames: list[dict[str, Any]] = []
    frame_keys: set[str] = set()
    for expected_index, raw_frame in enumerate(preview["frames"]):
        if not isinstance(raw_frame, dict):
            raise RuntimeExecutionError(
                "trajectory_evidence_unavailable",
                "A Fix revision preview frame is invalid.",
            )
        frame_key = raw_frame.get("private_frame_key")
        raw_targets = raw_frame.get("targets")
        if (
            raw_frame.get("frame_index") != expected_index
            or not isinstance(frame_key, str)
            or not frame_key
            or Path(frame_key).name != frame_key
            or frame_key in frame_keys
            or not isinstance(raw_targets, dict)
            or set(raw_targets) != expected_refs
        ):
            raise RuntimeExecutionError(
                "trajectory_evidence_unavailable",
                "A Fix revision preview frame identity is invalid.",
            )
        frame_keys.add(frame_key)
        targets: dict[str, dict[str, Any]] = {}
        for target_ref, raw_target in raw_targets.items():
            if not isinstance(raw_target, dict):
                raise RuntimeExecutionError(
                    "trajectory_evidence_unavailable",
                    "A Fix revision preview target is invalid.",
                )
            position = (
                _number_pair(raw_target.get("position"))
                if raw_target.get("position") is not None
                else None
            )
            direction = _finite_number(raw_target.get("direction"))
            speed = _finite_number(raw_target.get("speed"))
            camera_position = (
                _number_pair(raw_target.get("camera_position"))
                if raw_target.get("camera_position") is not None
                else None
            )
            camera_trajectory = _trajectory_points(
                raw_target.get("camera_trajectory_points"),
            )
            trajectory = _trajectory_points(
                raw_target.get("trajectory_points"),
            )
            label = raw_target.get("label")
            color = raw_target.get("color")
            if (
                not isinstance(label, str)
                or not label
                or (raw_target.get("position") is not None and position is None)
                or direction is None
                or speed is None
                or speed < 0
                or (
                    raw_target.get("camera_position") is not None
                    and camera_position is None
                )
                or not isinstance(color, list)
                or any(not isinstance(item, str) for item in color[:3])
            ):
                raise RuntimeExecutionError(
                    "trajectory_evidence_unavailable",
                    "A Fix revision preview target value is invalid.",
                )
            targets[target_ref] = {
                "label": label,
                "position": position,
                "direction": direction,
                "speed": speed,
                "color": [str(item) for item in color[:3]],
                "image_box": None,
                "trajectory_points": trajectory,
                "camera_position": camera_position,
                "camera_trajectory_points": camera_trajectory,
            }

        camera = segment_root / "fisheye_front" / f"{frame_key}.jpg"
        gridmap = segment_root / "grid_map" / f"{frame_key}.json"
        gridmap_available = gridmap.is_file() and not gridmap.is_symlink()
        gridmap_width = None
        gridmap_height = None
        gridmap_resolution = None
        gridmap_x_range = None
        gridmap_y_range = None
        if gridmap_available:
            try:
                payload = _gridmap_payload(_read_stable_regular_bytes(gridmap))
                gridmap_width = payload.height
                gridmap_height = payload.width
                gridmap_resolution = payload.resolution
                gridmap_x_range = list(payload.x_range)
                gridmap_y_range = list(payload.y_range)
            except (OSError, RuntimeExecutionError):
                gridmap_available = False
        frames.append(
            {
                "frame_index": expected_index,
                "private_frame_key": frame_key,
                "camera_available": (
                    camera.is_file() and not camera.is_symlink()
                ),
                "projection_available": False,
                "gridmap_available": gridmap_available,
                "gridmap_width": gridmap_width,
                "gridmap_height": gridmap_height,
                "gridmap_resolution": gridmap_resolution,
                "gridmap_x_range": gridmap_x_range,
                "gridmap_y_range": gridmap_y_range,
                "pass": bool(raw_frame.get("pass", False)),
                "targets": targets,
            }
        )
    return {
        "schema_version": 1,
        "source": "fix_revision",
        "target_bindings": dict(sorted(expected_target_bindings.items())),
        "frame_count": len(frames),
        "frames": frames,
    }


def resolve_evidence_file(
    segment_root: Path,
    state: dict[str, Any],
    *,
    frame_index: int,
    kind: str,
) -> Path:
    if kind not in {"camera", "gridmap", "projection"}:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The requested evidence kind is unsupported.",
        )
    frames = state.get("frames")
    if (
        not isinstance(frames, list)
        or isinstance(frame_index, bool)
        or frame_index < 0
        or frame_index >= len(frames)
        or not isinstance(frames[frame_index], dict)
    ):
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The requested evidence frame is unavailable.",
        )
    frame_key = frames[frame_index].get("private_frame_key")
    if not isinstance(frame_key, str) or Path(frame_key).name != frame_key:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The requested evidence frame is invalid.",
        )
    if kind == "camera":
        relative = Path("fisheye_front", f"{frame_key}.jpg")
    elif kind == "gridmap":
        relative = Path("grid_map", f"{frame_key}.json")
    else:
        relative = Path("rout_plot_v2", f"{frame_key}.png")
    try:
        root = segment_root.resolve(strict=True)
        lexical_candidate = root / relative
        if lexical_candidate.is_symlink():
            raise OSError("evidence file is a symlink")
        candidate = lexical_candidate.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The requested evidence file is unavailable.",
        ) from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeExecutionError(
            "trajectory_evidence_unavailable",
            "The requested evidence file is unavailable.",
        )
    return candidate
