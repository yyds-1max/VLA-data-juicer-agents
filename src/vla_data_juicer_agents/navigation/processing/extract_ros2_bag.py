#!/usr/bin/env python3
# # -*- coding: utf-8 -*-
# # 文件名: 1_extract_data_from_bag_multi_process_ros2.py
# # 说明: 在原脚本基础上修正自定义消息(INS等)时间戳问题
# #      优先用 msg.header.stamp（如果合理），否则使用 rosbag2 read_next() 返回的 timestamp（ns）
# #      特别修复 /drivers/ins/Ins 消息字段缺失问题

import os
import argparse
import time
import json
import struct
import multiprocessing
from pathlib import Path

# ==================== 白名单：只拆这些话题 ====================
TOPIC_WHITELIST = [
    # "/cam_dog_front/csi_cam/image_raw/compressed",
    # "/cam_dog_rear/csi_cam/image_raw/compressed",
    # "/cam_video2/csi_cam/image_raw/compressed",
    # "/cam_video4/csi_cam/image_raw/compressed",    
    "/cam_video5/csi_cam/image_raw/compressed",   #u  
    # "/cam_video4/csi_cam/image_raw/compressed",   #go2w
    # "/cam_video6/csi_cam/image_raw/compressed",
    # "/cam_video7/csi_cam/image_raw/compressed",
    # "/cam_video0/csi_cam/image_raw/compressed",

    "/lidar_points",       # u
    # "/rs32_lidar_points",   #go2w

    # "/drivers/ins/Ins",

    #"/utlidar/robot_odom_systime",  # 新增：Odom话题   u 
    "/sport_odom"   #go2w
]


def _load_json_list(value: str, label: str) -> list[str]:
    payload = json.loads(value)
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError(f"{label} must be a JSON list of strings")
    return payload


def resolve_topic_whitelist(topic_whitelist: str | None, topic_whitelist_file: str | Path | None) -> list[str]:
    if topic_whitelist_file is not None:
        return _load_json_list(Path(topic_whitelist_file).read_text(encoding="utf-8"), "--topic_whitelist_file")
    if topic_whitelist is not None:
        return _load_json_list(topic_whitelist, "--topic_whitelist")
    raise ValueError("missing required topic whitelist; pass --topic_whitelist or --topic_whitelist_file")


# ==================== 点云解析 ====================
def read_pointcloud2(msg):
    import numpy as np

    if msg.height == 0 or msg.width == 0:
        return None

    point_step = msg.point_step
    data = msg.data

    offset = {f.name: f.offset for f in msg.fields}
    x_offset = offset.get("x")
    y_offset = offset.get("y")
    z_offset = offset.get("z")
    intensity_offset = offset.get("intensity", offset.get("i"))

    if None in (x_offset, y_offset, z_offset):
        return None

    pts = []
    # 遍历每个点
    for i in range(0, len(data), point_step):
        x = struct.unpack_from("f", data, i + x_offset)[0]
        y = struct.unpack_from("f", data, i + y_offset)[0]
        z = struct.unpack_from("f", data, i + z_offset)[0]
        intensity = 0.0
        if intensity_offset is not None:
            intensity = struct.unpack_from("f", data, i + intensity_offset)[0]
        pts.append([x, y, z, intensity])

    return np.array(pts, dtype=np.float32)

# ==================== 时间戳解析（修正） ====================
def _is_reasonable_unix_time(t):
    """判断 t（秒）是否为合理的 unix 时间（约 2000年 ~ 2100年）"""
    try:
        return 946684800.0 <= float(t) <= 4102444800.0  # between year 2000 and 2100
    except Exception:
        return False

def _find_common_time_field(msg):
    """
    在自定义消息中尝试查找常见时间字段（time, timestamp, time_usec, gps_time 等）
    如果找到并且看上去是秒或微秒数，会返回以秒为单位的时间（float）。
    """
    candidates = []
    # 先直接常用名字尝试
    for name in ('time', 'timestamp', 'time_usec', 'gps_time', 'gps_sec', 'gps_usec', 'utc_time', 'recv_time', 'ts', 'gnss_time'):
        if hasattr(msg, name):
            val = getattr(msg, name)
            # 标量数值
            if isinstance(val, (int, float)):
                candidates.append((name, float(val)))
            # 可能为 Time 类型（有 sec/nanosec）
            if hasattr(val, 'sec') and hasattr(val, 'nanosec'):
                try:
                    candidates.append((name, val.sec + val.nanosec * 1e-9))
                except Exception:
                    pass

    # 递归检查 slots（深度有限）
    def recur_extract(o, prefix="", depth=0):
        if depth > 2:
            return
        if hasattr(o, "__slots__"):
            for s in o.__slots__:
                try:
                    v = getattr(o, s)
                except Exception:
                    continue
                nm = f"{prefix}.{s}" if prefix else s
                if isinstance(v, (int, float)):
                    if 'time' in nm or 'ts' in nm or 'utc' in nm or 'gps' in nm:
                        candidates.append((nm, float(v)))
                elif hasattr(v, "sec") and hasattr(v, "nanosec"):
                    try:
                        candidates.append((nm, v.sec + v.nanosec * 1e-9))
                    except Exception:
                        pass
                elif hasattr(v, "__slots__"):
                    recur_extract(v, nm, depth + 1)
    try:
        recur_extract(msg)
    except Exception:
        pass

    # 评估候选值：如果是微秒级大数（>1e12），当作微秒 -> 转为秒
    for name, val in candidates:
        if val > 1e12:
            val_sec = val * 1e-6
        elif val > 1e9 and val < 1e12:
            # 有可能是纳秒
            val_sec = val * 1e-9
        else:
            val_sec = val
        if _is_reasonable_unix_time(val_sec):
            return val_sec
    return None

def get_real_timestamp(msg, bag_timestamp_ns=None):
    """
    获取消息的真实时间戳（秒）。优先级：
      1) 如果 msg.header.stamp 存在并且是合理的 unix 时间 → 使用它
      2) 尝试查找消息体内常见时间字段（time, timestamp, time_usec 等），并校验其是否合理 → 使用它
      3) 使用 rosbag2 read_next() 返回的 bag_timestamp_ns（纳秒）作为 fallback
      4) 最后 fallback 为当前系统 time.time()
    """
    # 1) header.stamp
    try:
        if hasattr(msg, "header") and hasattr(msg.header, "stamp"):
            hs = msg.header.stamp
            # ROS2 Time 类型通常有 sec + nanosec
            if hasattr(hs, "sec") and hasattr(hs, "nanosec"):
                ts = float(hs.sec) + float(hs.nanosec) * 1e-9
                if _is_reasonable_unix_time(ts):
                    return ts
            # 可能 header.stamp 直接是 float
            if isinstance(hs, (float, int)):
                if _is_reasonable_unix_time(float(hs)):
                    return float(hs)
    except Exception:
        pass

    # 2) 在消息里查找常见时间字段
    try:
        candidate = _find_common_time_field(msg)
        if candidate is not None:
            return candidate
    except Exception:
        pass

    # 3) 使用 bag_timestamp_ns（来自 reader.read_next() 的第三个返回值）
    try:
        if bag_timestamp_ns is not None:
            # bag_timestamp_ns 通常是 uint64 纳秒
            ts = float(bag_timestamp_ns) * 1e-9
            if _is_reasonable_unix_time(ts):
                return ts
            else:
                # 有些情况下 bag timestamp 可能是 monotonic 或记录用的相对时间（极少见）
                # 但在你的场景 bag timestamp 与点云/相机对齐，优先使用
                return ts
    except Exception:
        pass

    # 4) fallback
    return time.time()

# ==================== 保存点云 ====================
def save_pointcloud(msg, save_dir, bag_ts_ns=None):
    import numpy as np
    import pcl

    try:
        os.makedirs(save_dir, exist_ok=True)
        pts = read_pointcloud2(msg)
        if pts is None or len(pts) == 0:
            return

        valid_pts = pts[~np.isnan(pts).any(axis=1)]                
        pointcloud = pcl.PointCloud_PointXYZI(valid_pts[:,0:4])    

        ts = get_real_timestamp(msg, bag_ts_ns)
        pcl.save(pointcloud, os.path.join(save_dir, f"{ts:.6f}.pcd"))

    except Exception as e:
        print(f"[点云保存失败] {e}")

# ==================== 保存图像 ====================
def save_image(msg, save_dir, compressed=False, bag_ts_ns=None):
    import cv2
    import numpy as np

    try:
        os.makedirs(save_dir, exist_ok=True)

        if compressed:
            img = cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
        else:
            # 原始 Image msg: data -> bytes, 需要 reshape
            img = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width, 3)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        if img is None:
            return

        ts = get_real_timestamp(msg, bag_ts_ns)
        cv2.imwrite(os.path.join(save_dir, f"{ts:.6f}.jpg"), img)

    except Exception as e:
        print(f"[图像保存失败] {e}")

# ==================== 保存Odometry消息（新增，遵循原有规范） ====================
def save_odometry(msg, save_dir, bag_ts_ns=None):
    """
    提取Odometry数据，保存到本地JSON文件，以时间戳命名
    """
    try:
        os.makedirs(save_dir, exist_ok=True)
        ts = get_real_timestamp(msg, bag_ts_ns)

        # 构建要保存的数据结构（完全沿用你验证过的字段）
        odometry_data = {
            "header": {
                "stamp": {
                    "secs": msg.header.stamp.sec,
                    "nsecs": msg.header.stamp.nanosec
                },
                "frame_id": msg.header.frame_id
            },
            "child_frame_id": msg.child_frame_id,
            "pose": {
                "pose": {
                    "position": {
                        "x": msg.pose.pose.position.x,
                        "y": msg.pose.pose.position.y,
                        "z": msg.pose.pose.position.z
                    },
                    "orientation": {
                        "x": msg.pose.pose.orientation.x,
                        "y": msg.pose.pose.orientation.y,
                        "z": msg.pose.pose.orientation.z,
                        "w": msg.pose.pose.orientation.w,
                    }
                }
            },
            "twist": {
                "twist": {
                    "linear": {
                        "x": msg.twist.twist.linear.x,
                        "y": msg.twist.twist.linear.y,
                        "z": msg.twist.twist.linear.z
                    },
                    "angular": {
                        "x": msg.twist.twist.angular.x,
                        "y": msg.twist.twist.angular.y,
                        "z": msg.twist.twist.angular.z
                    }
                }
            }
        }

        # 保存JSON文件（与原有代码命名规范一致）
        with open(os.path.join(save_dir, f"{ts:.6f}.json"), "w") as f:
            json.dump(odometry_data, f, indent=4)

    except Exception as e:
        print(f"[Odometry保存失败] {e}")

# ==================== 保存自定义消息（Ins / Location） ====================
def save_custom_msg(msg, save_dir, topic_name, bag_ts_ns=None):
    try:
        os.makedirs(save_dir, exist_ok=True)
        ts = get_real_timestamp(msg, bag_ts_ns)

        # ====== 特殊处理 /drivers/ins/Ins 消息 ======
        if topic_name == "Ins" or "/drivers/ins/Ins" in topic_name:
            data = {
                "header.stamp": f"{ts:.6f}",
                "latitude": float(getattr(msg, 'latitude', 0.0)),
                "longitude": float(getattr(msg, 'longitude', 0.0)),
                "elevation": float(getattr(msg, 'elevation', 0.0)),
                "position_type": int(getattr(msg, 'position_type', 0)),
            }

            # utm_position (geometry_msgs/Point)
            if hasattr(msg, 'utm_position'):
                data["utm_position.x"] = float(getattr(msg.utm_position, 'x', 0.0))
                data["utm_position.y"] = float(getattr(msg.utm_position, 'y', 0.0))
                data["utm_position.z"] = float(getattr(msg.utm_position, 'z', 0.0))
            else:
                data.update({"utm_position.x": 0.0, "utm_position.y": 0.0, "utm_position.z": 0.0})

            # attitude (geometry_msgs/Vector3)
            if hasattr(msg, 'attitude'):
                data["attitude.x"] = float(getattr(msg.attitude, 'x', 0.0))
                data["attitude.y"] = float(getattr(msg.attitude, 'y', 0.0))
                data["attitude.z"] = float(getattr(msg.attitude, 'z', 0.0))
            else:
                data.update({"attitude.x": 0.0, "attitude.y": 0.0, "attitude.z": 0.0})

            # linear_velocity (geometry_msgs/Vector3)
            if hasattr(msg, 'linear_velocity'):
                data["linear_velocity.x"] = float(getattr(msg.linear_velocity, 'x', 0.0))
                data["linear_velocity.y"] = float(getattr(msg.linear_velocity, 'y', 0.0))
                data["linear_velocity.z"] = float(getattr(msg.linear_velocity, 'z', 0.0))
            else:
                data.update({"linear_velocity.x": 0.0, "linear_velocity.y": 0.0, "linear_velocity.z": 0.0})

            # angular_velocity (geometry_msgs/Vector3)
            if hasattr(msg, 'angular_velocity'):
                data["angular_velocity.x"] = float(getattr(msg.angular_velocity, 'x', 0.0))
                data["angular_velocity.y"] = float(getattr(msg.angular_velocity, 'y', 0.0))
                data["angular_velocity.z"] = float(getattr(msg.angular_velocity, 'z', 0.0))
            else:
                data.update({"angular_velocity.x": 0.0, "angular_velocity.y": 0.0, "angular_velocity.z": 0.0})

            # acceleration (geometry_msgs/Vector3)
            if hasattr(msg, 'acceleration'):
                data["acceleration.x"] = float(getattr(msg.acceleration, 'x', 0.0))
                data["acceleration.y"] = float(getattr(msg.acceleration, 'y', 0.0))
                data["acceleration.z"] = float(getattr(msg.acceleration, 'z', 0.0))
            else:
                data.update({"acceleration.x": 0.0, "acceleration.y": 0.0, "acceleration.z": 0.0})

        else:
            # ====== 其他自定义消息：走通用递归提取 ======
            data = {"header.stamp": f"{ts:.6f}"}

            def extract(obj, prefix=""):
                if hasattr(obj, "__slots__"):
                    for slot in obj.__slots__:
                        if slot.startswith("_"):
                            continue
                        try:
                            val = getattr(obj, slot)
                        except Exception:
                            continue
                        key = f"{prefix}.{slot}" if prefix else slot

                        if hasattr(val, "__slots__"):
                            extract(val, key)
                        elif isinstance(val, (list, tuple)):
                            try:
                                data[key] = list(val)
                            except Exception:
                                data[key] = str(val)
                        else:
                            data[key] = val

            extract(msg)

        # 保存 JSON
        with open(os.path.join(save_dir, f"{ts:.6f}.json"), "w") as f:
            json.dump(data, f, indent=4)

    except Exception as e:
        print(f"[{topic_name} 保存失败] {e}")

# ==================== 主提取函数（使用 bag timestamp） ====================
def extract_ros2_bag(bag_path: str, save_root: str, topic_whitelist=None):
    from rosbag2_py import SequentialReader, StorageOptions, ConverterOptions
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    from sensor_msgs.msg import PointCloud2

    if not topic_whitelist:
        raise ValueError("topic_whitelist is required")
    active_topic_whitelist = set(topic_whitelist)
    print(f"开始提取: {bag_path}")

    reader = SequentialReader()
    storage_options = StorageOptions(uri=str(bag_path), storage_id="sqlite3")
    converter_options = ConverterOptions("cdr", "cdr")
    reader.open(storage_options, converter_options)

    topics = reader.get_all_topics_and_types()

    # topic -> 目录
    topic_to_dir = {}
    for t in topics:
        if t.name not in active_topic_whitelist:
            continue
        clean = t.name.replace("/drivers/ins", "").replace("/drivers/canbus", "")
        parts = clean.split("/")
        if len(parts) >= 2:
            topic_to_dir[t.name] = parts[1]

    total = 0
    while reader.has_next():
        # read_next() 返回 (topic, serialized_msg, timestamp_ns)
        topic, data, timestamp_ns = reader.read_next()

        if topic not in active_topic_whitelist:
            continue

        dir_name = topic_to_dir.get(topic)
        if not dir_name:
            continue

        save_dir = os.path.join(save_root, dir_name)
        msg_type_str = next(t.type for t in topics if t.name == topic)

        try:
            if "PointCloud2" in msg_type_str:
                msg = deserialize_message(data, PointCloud2)
                save_pointcloud(msg, save_dir, bag_ts_ns=timestamp_ns)

            elif "CompressedImage" in msg_type_str:
                from sensor_msgs.msg import CompressedImage
                msg = deserialize_message(data, CompressedImage)
                save_image(msg, save_dir, compressed=True, bag_ts_ns=timestamp_ns)

            elif "Image" in msg_type_str:
                from sensor_msgs.msg import Image
                msg = deserialize_message(data, Image)
                save_image(msg, save_dir, compressed=False, bag_ts_ns=timestamp_ns)

            # 新增：Odometry消息处理
            elif "Odometry" in msg_type_str:
                from nav_msgs.msg import Odometry
                msg = deserialize_message(data, Odometry)
                save_odometry(msg, save_dir, bag_ts_ns=timestamp_ns)

            else:
                # 自定义消息
                msg_class = get_message(msg_type_str)
                msg = deserialize_message(data, msg_class)
                # 提取 topic 最后一段作为 topic_name（如 "Ins"）
                topic_short_name = topic.split("/")[-1]
                save_custom_msg(msg, save_dir, topic_short_name, bag_ts_ns=timestamp_ns)

            total += 1

        except Exception as e:
            print(f"[反序列化失败] topic={topic} type={msg_type_str} → {e}")

    print(f"完成: {bag_path} 共保存 {total} 条消息")

# ==================== 主入口 ====================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ROS2 db3 拆包工具（修复自定义消息时间戳）")
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--save_path", type=str, required=True)
    parser.add_argument("--processes_num", type=int, default=4)
    parser.add_argument("--topic_whitelist", type=str, default=None)
    parser.add_argument("--topic_whitelist_file", type=str, default=None)
    args = parser.parse_args()
    if args.topic_whitelist is None and args.topic_whitelist_file is None:
        parser.error("one of --topic_whitelist or --topic_whitelist_file is required")

    start_time = time.time()

    bag_paths = sorted([str(p) for p in Path(args.data_path).rglob("*.db3")])
    if not bag_paths:
        print("没找到任何 .db3 文件！")
        exit(1)

    print(f"共发现 {len(bag_paths)} 个 bag，开始提取...")

    save_root = os.path.join(args.save_path, "tmp_dir")
    os.makedirs(save_root, exist_ok=True)
    topic_whitelist = resolve_topic_whitelist(args.topic_whitelist, args.topic_whitelist_file)

    pool = multiprocessing.Pool(args.processes_num)
    for p in bag_paths:
        pool.apply_async(extract_ros2_bag, (p, save_root, topic_whitelist))
    pool.close()
    pool.join()

    print(f"全部完成！总耗时: {time.time() - start_time:.2f} 秒")
    print(f"数据已保存到: {save_root}")
