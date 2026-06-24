#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import struct
import time
from pathlib import Path
from typing import Any


DEFAULT_TOPIC_WHITELIST = [
    "/cam_video5/csi_cam/image_raw/compressed",
    "/lidar_points",
    "/utlidar/robot_odom_systime",
]


def _load_json_list(value: str, label: str) -> list[str]:
    payload = json.loads(value)
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError(f"{label} must be a JSON list of strings")
    return payload


def resolve_topic_whitelist(
    topic_whitelist: str | None,
    topic_whitelist_file: str | Path | None,
) -> list[str]:
    if topic_whitelist_file is not None:
        return _load_json_list(Path(topic_whitelist_file).read_text(encoding="utf-8"), "--topic_whitelist_file")
    if topic_whitelist is not None:
        return _load_json_list(topic_whitelist, "--topic_whitelist")
    return list(DEFAULT_TOPIC_WHITELIST)


def _stamp_seconds(header_stamp: Any, bag_ts_ns: int) -> float:
    sec = getattr(header_stamp, "sec", None)
    nanosec = getattr(header_stamp, "nanosec", None)
    if isinstance(sec, int) and isinstance(nanosec, int) and sec > 0:
        return sec + nanosec / 1_000_000_000
    return bag_ts_ns / 1_000_000_000


def _message_timestamp(msg: Any, bag_ts_ns: int) -> float:
    header = getattr(msg, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is not None:
        return _stamp_seconds(stamp, bag_ts_ns)
    return bag_ts_ns / 1_000_000_000


def _topic_output_dir(topic: str) -> str | None:
    clean = topic.replace("/drivers/ins", "").replace("/drivers/canbus", "")
    parts = clean.split("/")
    return parts[1] if len(parts) >= 2 and parts[1] else None


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _timestamp_name(timestamp: float, suffix: str) -> str:
    return f"{timestamp:.6f}{suffix}"


def _pointcloud_rows(msg: Any) -> list[list[float]]:
    offsets = {field.name: field.offset for field in msg.fields}
    x_offset = offsets.get("x")
    y_offset = offsets.get("y")
    z_offset = offsets.get("z")
    intensity_offset = offsets.get("intensity", offsets.get("i"))
    if None in (x_offset, y_offset, z_offset):
        return []

    rows = []
    for index in range(0, len(msg.data), msg.point_step):
        row = [
            struct.unpack_from("f", msg.data, index + x_offset)[0],
            struct.unpack_from("f", msg.data, index + y_offset)[0],
            struct.unpack_from("f", msg.data, index + z_offset)[0],
            (
                struct.unpack_from("f", msg.data, index + intensity_offset)[0]
                if intensity_offset is not None
                else 0.0
            ),
        ]
        rows.append(row)
    return rows


def _save_pointcloud(msg: Any, save_dir: Path, bag_ts_ns: int) -> None:
    import pcl

    timestamp = _message_timestamp(msg, bag_ts_ns)
    path = save_dir / _timestamp_name(timestamp, ".pcd")
    save_dir.mkdir(parents=True, exist_ok=True)
    cloud = pcl.PointCloud_PointXYZI()
    cloud.from_list(_pointcloud_rows(msg))
    pcl.save(cloud, str(path))


def _save_odometry(msg: Any, save_dir: Path, bag_ts_ns: int) -> None:
    timestamp = _message_timestamp(msg, bag_ts_ns)
    pose = msg.pose.pose
    twist = msg.twist.twist
    payload = {
        "timestamp": timestamp,
        "header": {
            "stamp": {
                "secs": msg.header.stamp.sec,
                "nsecs": msg.header.stamp.nanosec,
            },
            "frame_id": msg.header.frame_id,
        },
        "child_frame_id": msg.child_frame_id,
        "pose": {
            "pose": {
                "position": {
                    "x": pose.position.x,
                    "y": pose.position.y,
                    "z": pose.position.z,
                },
                "orientation": {
                    "x": pose.orientation.x,
                    "y": pose.orientation.y,
                    "z": pose.orientation.z,
                    "w": pose.orientation.w,
                },
            }
        },
        "twist": {
            "twist": {
                "linear": {
                    "x": twist.linear.x,
                    "y": twist.linear.y,
                    "z": twist.linear.z,
                },
                "angular": {
                    "x": twist.angular.x,
                    "y": twist.angular.y,
                    "z": twist.angular.z,
                },
            }
        },
    }
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / _timestamp_name(timestamp, ".json")).write_text(json.dumps(payload, indent=4), encoding="utf-8")


def _save_generic(msg: Any, save_dir: Path, bag_ts_ns: int) -> None:
    timestamp = _message_timestamp(msg, bag_ts_ns)
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / _timestamp_name(timestamp, ".txt")).write_text(str(msg), encoding="utf-8")


def _save_message(
    topic: str,
    data: bytes,
    timestamp_ns: int,
    msg_type: str,
    save_dir: Path,
    *,
    compressed_image_type: Any,
    image_type: Any,
    odometry_type: Any,
    pointcloud_type: Any,
) -> None:
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    if "PointCloud2" in msg_type:
        _save_pointcloud(deserialize_message(data, pointcloud_type), save_dir, timestamp_ns)
    elif "CompressedImage" in msg_type:
        msg = deserialize_message(data, compressed_image_type)
        _write_bytes(save_dir / _timestamp_name(_message_timestamp(msg, timestamp_ns), ".jpg"), bytes(msg.data))
    elif "Image" in msg_type:
        msg = deserialize_message(data, image_type)
        _write_bytes(save_dir / _timestamp_name(_message_timestamp(msg, timestamp_ns), ".bin"), bytes(msg.data))
    elif "Odometry" in msg_type:
        _save_odometry(deserialize_message(data, odometry_type), save_dir, timestamp_ns)
    else:
        _save_generic(deserialize_message(data, get_message(msg_type)), save_dir, timestamp_ns)


def extract_ros2_bag(bag_path: str | Path, save_root: str | Path, topic_whitelist: list[str]) -> int:
    from nav_msgs.msg import Odometry
    from rosbag2_py import ConverterOptions, SequentialReader, StorageOptions
    from sensor_msgs.msg import CompressedImage, Image, PointCloud2

    reader = SequentialReader()
    reader.open(
        StorageOptions(uri=str(bag_path), storage_id="sqlite3"),
        ConverterOptions("cdr", "cdr"),
    )
    topics = reader.get_all_topics_and_types()
    topic_types = {topic.name: topic.type for topic in topics}
    whitelist = set(topic_whitelist)
    topic_to_dir = {
        topic.name: _topic_output_dir(topic.name)
        for topic in topics
        if topic.name in whitelist and _topic_output_dir(topic.name)
    }

    total = 0
    while reader.has_next():
        topic, data, timestamp_ns = reader.read_next()
        output_dir = topic_to_dir.get(topic)
        if output_dir is None:
            continue

        save_dir = Path(save_root) / output_dir
        msg_type = topic_types[topic]
        try:
            _save_message(
                topic,
                data,
                timestamp_ns,
                msg_type,
                save_dir,
                compressed_image_type=CompressedImage,
                image_type=Image,
                odometry_type=Odometry,
                pointcloud_type=PointCloud2,
            )
        except Exception as exc:
            print(f"[message save failed] topic={topic} type={msg_type}: {exc}")
            continue
        total += 1
    return total


def _extract_worker(args: tuple[str, str, list[str]]) -> int:
    bag_path, save_root, topic_whitelist = args
    return extract_ros2_bag(bag_path, save_root, topic_whitelist)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract selected ROS2 bag topics for navigation processing.")
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--save_path", type=str, required=True)
    parser.add_argument("--processes_num", type=int, default=4)
    parser.add_argument("--topic_whitelist", type=str, default=None)
    parser.add_argument("--topic_whitelist_file", type=str, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start_time = time.time()
    topic_whitelist = resolve_topic_whitelist(args.topic_whitelist, args.topic_whitelist_file)
    bag_paths = sorted(str(path) for path in Path(args.data_path).rglob("*.db3"))
    if not bag_paths:
        print("No .db3 files found.")
        return 1

    save_root = os.path.join(args.save_path, "tmp_dir")
    os.makedirs(save_root, exist_ok=True)
    worker_args = [(path, save_root, topic_whitelist) for path in bag_paths]
    if args.processes_num <= 1 or len(worker_args) == 1:
        totals = [_extract_worker(item) for item in worker_args]
    else:
        with multiprocessing.Pool(args.processes_num) as pool:
            totals = pool.map(_extract_worker, worker_args)
    print(f"Extracted {sum(totals)} messages from {len(bag_paths)} bag(s) in {time.time() - start_time:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
