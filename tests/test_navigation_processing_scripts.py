import json
import struct
import sys
from argparse import Namespace
from types import ModuleType
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import extract_and_sync_navigation_data
from vla_data_juicer_agents.navigation.models import CommandRecord
from vla_data_juicer_agents.navigation.processing import extract_ros2_bag, sync_navigation_data


def test_extract_script_resolves_inline_topic_whitelist():
    topics = extract_ros2_bag.resolve_topic_whitelist(
        json.dumps(["/cam_video4/csi_cam/image_raw/compressed", "/rs32_lidar_points"]),
        None,
    )

    assert topics == ["/cam_video4/csi_cam/image_raw/compressed", "/rs32_lidar_points"]


def test_extract_script_resolves_topic_whitelist_file(tmp_path):
    path = tmp_path / "topics.json"
    path.write_text(json.dumps(["/cam_video5/csi_cam/image_raw/compressed", "/lidar_points"]), encoding="utf-8")

    topics = extract_ros2_bag.resolve_topic_whitelist(None, path)

    assert topics == ["/cam_video5/csi_cam/image_raw/compressed", "/lidar_points"]


def test_extract_script_requires_explicit_topic_whitelist():
    with pytest.raises(ValueError, match="topic whitelist"):
        extract_ros2_bag.resolve_topic_whitelist(None, None)


def test_sync_script_resolves_inline_topic_map():
    topic_map = sync_navigation_data.resolve_topic_map(
        json.dumps({"cam_video4": "fisheye_front", "rs32_lidar_points": "r32_rslidar_points"}),
        None,
    )

    assert topic_map == {"cam_video4": "fisheye_front", "rs32_lidar_points": "r32_rslidar_points"}


def test_sync_script_resolves_topic_map_file(tmp_path):
    path = tmp_path / "topic_map.json"
    path.write_text(json.dumps({"sport_odom": "odom"}), encoding="utf-8")

    topic_map = sync_navigation_data.resolve_topic_map(None, path)

    assert topic_map == {"sport_odom": "odom"}


def test_sync_script_requires_explicit_topic_map():
    with pytest.raises(ValueError, match="topic map"):
        sync_navigation_data.resolve_topic_map(None, None)


def test_sync_data_renames_copied_files_to_timestamps(tmp_path):
    data_path = tmp_path / "clip"
    for dirname in ("cam_video5", "lidar_points", "sport_odom"):
        sensor_dir = data_path / "tmp_dir" / dirname
        sensor_dir.mkdir(parents=True)
        (sensor_dir / "1000.000000.txt").write_text(dirname, encoding="utf-8")
    opt = Namespace(
        data_path=str(data_path),
        query_dir="lidar_points",
        output_dir="sync_data",
        sequence_prefix="segment",
        max_file_num_in_one_dir=600,
        processes_num=1,
    )

    sync_navigation_data.sync_data(
        opt,
        {
            "cam_video5": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
    )

    sequence_root = data_path / "sync_data" / "segment_0"
    assert (sequence_root / "fisheye_front" / "1000.000000.txt").exists()
    assert (sequence_root / "r32_rslidar_points" / "1000.000000.txt").exists()
    assert (sequence_root / "odom" / "1000.000000.txt").exists()
    assert not (sequence_root / "fisheye_front" / "000000.txt").exists()


def test_save_pointcloud_writes_pcd_with_pcl(monkeypatch, tmp_path):
    calls = {}

    class FakePointCloud:
        def __init__(self, rows):
            calls["rows"] = rows

    fake_pcl = SimpleNamespace(
        PointCloud_PointXYZI=lambda rows: FakePointCloud(rows),
        save=lambda cloud, path: calls.update({"path": path, "cloud": cloud}),
    )
    monkeypatch.setitem(sys.modules, "pcl", fake_pcl)
    msg = SimpleNamespace(
        height=1,
        width=1,
        header=SimpleNamespace(stamp=SimpleNamespace(sec=1700000000, nanosec=0)),
        fields=[
            SimpleNamespace(name="x", offset=0),
            SimpleNamespace(name="y", offset=4),
            SimpleNamespace(name="z", offset=8),
            SimpleNamespace(name="intensity", offset=12),
        ],
        point_step=16,
        data=struct.pack("ffff", 1.0, 2.0, 3.0, 4.0),
    )

    extract_ros2_bag.save_pointcloud(msg, tmp_path, 123)

    assert calls["path"].endswith("1700000000.000000.pcd")
    assert calls["rows"].tolist() == [[1.0, 2.0, 3.0, 4.0]]


def test_extract_and_sync_non_dry_run_writes_topic_config_files(tmp_path, monkeypatch):
    root = tmp_path / "VLADatasets"
    segment = "20260605_152856"
    (root / "raw_data" / "20270605_temp" / segment).mkdir(parents=True)
    settings = NavigationSettings(vladatasets_root=root)
    topic_whitelist = ["/cam_video5/csi_cam/image_raw/compressed", "/lidar_points", "/sport_odom"]
    topic_map = {
        "cam_video5": "fisheye_front",
        "lidar_points": "r32_rslidar_points",
        "sport_odom": "odom",
    }

    def fake_run_command(command, cwd=None, dry_run=False, timeout_seconds=None):
        sync_root = root / "clip_data" / "20270605" / segment / "sync_data" / "clip_a"
        for dirname in topic_map.values():
            (sync_root / dirname).mkdir(parents=True, exist_ok=True)
        return CommandRecord(command=command, cwd=cwd, dry_run=dry_run, return_code=0)

    monkeypatch.setattr("vla_data_juicer_agents.navigation.execution_tools.run_command", fake_run_command)

    result = extract_and_sync_navigation_data(
        "20270605",
        "u_legacy_like",
        topic_whitelist=topic_whitelist,
        topic_map=topic_map,
        query_dir="lidar_points",
        settings=settings,
        dry_run=False,
    )

    config_dir = root / "clip_data" / "20270605" / segment / "_agent_config"
    assert result.ok is True
    assert json.loads((config_dir / "topic_whitelist.json").read_text(encoding="utf-8")) == topic_whitelist
    assert json.loads((config_dir / "topic_map.json").read_text(encoding="utf-8")) == topic_map


def test_save_odometry_preserves_legacy_schema(tmp_path):
    stamp = SimpleNamespace(sec=1700000000, nanosec=123000000)
    msg = SimpleNamespace(
        header=SimpleNamespace(stamp=stamp, frame_id="odom"),
        child_frame_id="base_link",
        pose=SimpleNamespace(
            pose=SimpleNamespace(
                position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
                orientation=SimpleNamespace(x=0.1, y=0.2, z=0.3, w=0.4),
            )
        ),
        twist=SimpleNamespace(
            twist=SimpleNamespace(
                linear=SimpleNamespace(x=4.0, y=5.0, z=6.0),
                angular=SimpleNamespace(x=0.5, y=0.6, z=0.7),
            )
        ),
    )

    extract_ros2_bag.save_odometry(msg, tmp_path, 123)

    payload = json.loads((tmp_path / "1700000000.123000.json").read_text(encoding="utf-8"))
    assert payload["header"] == {"stamp": {"secs": 1700000000, "nsecs": 123000000}, "frame_id": "odom"}
    assert payload["child_frame_id"] == "base_link"
    assert payload["pose"]["pose"]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert payload["twist"]["twist"]["angular"] == {"x": 0.5, "y": 0.6, "z": 0.7}


def test_extract_ros2_bag_continues_after_single_message_failure(monkeypatch, tmp_path):
    calls = []

    class FakeReader:
        def __init__(self):
            self.messages = [
                ("/lidar_points", b"bad", 1_000_000_000),
                ("/sport_odom", b"ok", 2_000_000_000),
            ]
            self.index = 0

        def open(self, *_args):
            return None

        def get_all_topics_and_types(self):
            return [
                SimpleNamespace(name="/lidar_points", type="sensor_msgs/msg/PointCloud2"),
                SimpleNamespace(name="/sport_odom", type="nav_msgs/msg/Odometry"),
            ]

        def has_next(self):
            return self.index < len(self.messages)

        def read_next(self):
            message = self.messages[self.index]
            self.index += 1
            return message

    rosbag2_py = ModuleType("rosbag2_py")
    rosbag2_py.SequentialReader = FakeReader
    rosbag2_py.StorageOptions = lambda **_kwargs: object()
    rosbag2_py.ConverterOptions = lambda *_args: object()
    sensor_msgs = ModuleType("sensor_msgs")
    sensor_msgs_msg = ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.PointCloud2 = type("PointCloud2", (), {})
    sensor_msgs_msg.CompressedImage = type("CompressedImage", (), {})
    sensor_msgs_msg.Image = type("Image", (), {})
    nav_msgs = ModuleType("nav_msgs")
    nav_msgs_msg = ModuleType("nav_msgs.msg")
    nav_msgs_msg.Odometry = type("Odometry", (), {})
    rclpy = ModuleType("rclpy")
    rclpy_serialization = ModuleType("rclpy.serialization")
    rclpy_serialization.deserialize_message = lambda data, _msg_type: data
    rosidl_runtime_py = ModuleType("rosidl_runtime_py")
    rosidl_utilities = ModuleType("rosidl_runtime_py.utilities")
    rosidl_utilities.get_message = lambda _msg_type: object

    for name, module in {
        "rosbag2_py": rosbag2_py,
        "sensor_msgs": sensor_msgs,
        "sensor_msgs.msg": sensor_msgs_msg,
        "nav_msgs": nav_msgs,
        "nav_msgs.msg": nav_msgs_msg,
        "rclpy": rclpy,
        "rclpy.serialization": rclpy_serialization,
        "rosidl_runtime_py": rosidl_runtime_py,
        "rosidl_runtime_py.utilities": rosidl_utilities,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(
        extract_ros2_bag,
        "save_pointcloud",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad point")),
    )
    monkeypatch.setattr(extract_ros2_bag, "save_odometry", lambda *_args, **_kwargs: calls.append("odom"))

    extract_ros2_bag.extract_ros2_bag(
        tmp_path / "bag.db3",
        tmp_path / "out",
        ["/lidar_points", "/sport_odom"],
    )

    assert calls == ["odom"]
