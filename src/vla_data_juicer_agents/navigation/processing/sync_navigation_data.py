#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from pathlib import Path


DEFAULT_TOPIC_MAP = {
    "cam_video5": "fisheye_front",
    "lidar_points": "r32_rslidar_points",
    "utlidar": "odom",
}


def _load_json_dict(value: str, label: str) -> dict[str, str]:
    payload = json.loads(value)
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(child, str) for key, child in payload.items()
    ):
        raise ValueError(f"{label} must be a JSON object with string keys and values")
    return payload


def resolve_topic_map(topic_map: str | None, topic_map_file: str | Path | None) -> dict[str, str]:
    if topic_map_file is not None:
        return _load_json_dict(Path(topic_map_file).read_text(encoding="utf-8"), "--topic_map_file")
    if topic_map is not None:
        return _load_json_dict(topic_map, "--topic_map")
    return dict(DEFAULT_TOPIC_MAP)


def string2time(name: str) -> float:
    stem, _ = os.path.splitext(name)
    return float(stem)


def get_seq_name(seq_index: int, opt: argparse.Namespace) -> str:
    return f"{opt.sequence_prefix}_{seq_index}" if opt.sequence_prefix else str(seq_index)


def _nearest_index(values: list[float], target: float) -> int:
    return min(range(len(values)), key=lambda index: abs(values[index] - target))


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _rename_numbered_files_to_timestamps(sync_data_path: Path) -> None:
    for sequence_root in sorted(path for path in sync_data_path.iterdir() if path.is_dir()):
        times_path = sequence_root / "times.json"
        if not times_path.exists():
            continue
        times = json.loads(times_path.read_text(encoding="utf-8"))
        if not isinstance(times, dict):
            continue
        for sensor_dir in sorted(path for path in sequence_root.iterdir() if path.is_dir()):
            for file_path in sorted(path for path in sensor_dir.iterdir() if path.is_file()):
                timestamp = times.get(file_path.stem)
                if not isinstance(timestamp, str):
                    continue
                target = file_path.with_name(f"{timestamp}{file_path.suffix}")
                if target == file_path:
                    continue
                if target.exists():
                    target.unlink()
                file_path.rename(target)


def sync_data(opt: argparse.Namespace, topic_map: dict[str, str]) -> None:
    tmp_data_path = Path(opt.data_path) / "tmp_dir"
    sync_data_path = Path(opt.data_path) / opt.output_dir
    sync_data_path.mkdir(parents=True, exist_ok=True)

    search_dirs = sorted(path.name for path in tmp_data_path.iterdir() if path.is_dir())
    query_path = tmp_data_path / opt.query_dir
    query_file_names = sorted(path.name for path in query_path.iterdir() if path.is_file())
    query_file_times = [string2time(name) for name in query_file_names]

    search_file_names: dict[str, list[str]] = {}
    search_file_times: dict[str, list[float]] = {}
    for search_dir in search_dirs:
        files = sorted(path.name for path in (tmp_data_path / search_dir).iterdir() if path.is_file())
        search_file_names[search_dir] = files
        search_file_times[search_dir] = [string2time(name) for name in files]

    valid_query_indexes: list[int] = []
    nearest_by_dir: dict[str, list[int]] = {search_dir: [] for search_dir in search_dirs}
    for query_index, query_time in enumerate(query_file_times):
        nearest_for_query: dict[str, int] = {}
        valid = True
        for search_dir in search_dirs:
            times = search_file_times[search_dir]
            if not times:
                valid = False
                break
            nearest = _nearest_index(times, query_time)
            if abs(times[nearest] - query_time) > 0.1:
                valid = False
                break
            nearest_for_query[search_dir] = nearest
        if valid:
            valid_query_indexes.append(query_index)
            for search_dir, nearest in nearest_for_query.items():
                nearest_by_dir[search_dir].append(nearest)

    sequence_count = math.ceil(len(valid_query_indexes) / opt.max_file_num_in_one_dir) if valid_query_indexes else 0
    total_times: dict[str, str] = {}
    for sequence_index in range(sequence_count):
        seq_name = get_seq_name(sequence_index, opt)
        seq_root = sync_data_path / seq_name
        for search_dir in search_dirs:
            (seq_root / topic_map.get(search_dir, search_dir)).mkdir(parents=True, exist_ok=True)

        sequence_times = {}
        start = sequence_index * opt.max_file_num_in_one_dir
        end = min(start + opt.max_file_num_in_one_dir, len(valid_query_indexes))
        for output_index in range(start, end):
            query_file = query_file_names[valid_query_indexes[output_index]]
            timestamp, _ = os.path.splitext(query_file)
            output_name = str(output_index).zfill(6)
            sequence_times[output_name] = timestamp
            total_times[output_name] = timestamp

        (seq_root / "times.json").write_text(json.dumps(sequence_times, indent=4), encoding="utf-8")

    (sync_data_path / "times.json").write_text(json.dumps(total_times, indent=4), encoding="utf-8")

    for search_dir in search_dirs:
        mapped_dir = topic_map.get(search_dir, search_dir)
        files = search_file_names[search_dir]
        for output_index, nearest in enumerate(nearest_by_dir[search_dir]):
            if output_index % 5 != 0:
                continue
            seq_index = int(output_index / opt.max_file_num_in_one_dir)
            seq_name = get_seq_name(seq_index, opt)
            _, ext = os.path.splitext(files[nearest])
            src = tmp_data_path / search_dir / files[nearest]
            dst = sync_data_path / seq_name / mapped_dir / f"{str(output_index).zfill(6)}{ext}"
            _copy_file(src, dst)
    _rename_numbered_files_to_timestamps(sync_data_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize extracted navigation topic directories.")
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--query_dir", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="sync_data")
    parser.add_argument("--sequence_prefix", type=str, default="")
    parser.add_argument("--max_file_num_in_one_dir", type=int, default=600)
    parser.add_argument("--processes_num", type=int, default=4)
    parser.add_argument("--topic_map", type=str, default=None)
    parser.add_argument("--topic_map_file", type=str, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    opt = build_parser().parse_args(argv)
    sync_data(opt, resolve_topic_map(opt.topic_map, opt.topic_map_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
