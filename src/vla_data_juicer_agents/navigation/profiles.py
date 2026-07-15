SENSOR_TOPIC_ALIASES = {
    "fisheye_front": (
        "/cam_video4/csi_cam/image_raw/compressed",
        "/cam_video5/csi_cam/image_raw/compressed",
    ),
    "lidar": (
        "/lidar_points",
        "/rs32_lidar_points",
        "/r32_rslidar_points",
    ),
    "odom": (
        "/sport_odom",
        "/utlidar/robot_odom_systime",
    ),
    "ins": (
        "/drivers/ins/Ins",
    ),
    "gridmap": (
        "/grid_map",
        "/gridmap",
    ),
}

TOPIC_OUTPUT_MAP = {
    "/cam_video4/csi_cam/image_raw/compressed": ("cam_video4", "fisheye_front"),
    "/cam_video5/csi_cam/image_raw/compressed": ("cam_video5", "fisheye_front"),
    "/lidar_points": ("lidar_points", "r32_rslidar_points"),
    "/rs32_lidar_points": ("rs32_lidar_points", "r32_rslidar_points"),
    "/r32_rslidar_points": ("r32_rslidar_points", "r32_rslidar_points"),
    "/sport_odom": ("sport_odom", "odom"),
    "/utlidar/robot_odom_systime": ("utlidar", "odom"),
    "/drivers/ins/Ins": ("Ins", "ins"),
    "/grid_map": ("grid_map", "grid_map"),
    "/gridmap": ("gridmap", "grid_map"),
}

ROLE_OUTPUT_DIR = {
    "fisheye_front": "fisheye_front",
    "lidar": "r32_rslidar_points",
    "odom": "odom",
    "ins": "ins",
    "gridmap": "grid_map",
}


def topics_for_role(topics: set[str], role: str) -> list[str]:
    aliases = SENSOR_TOPIC_ALIASES[role]
    return [topic for topic in aliases if topic in topics]


def extracted_dir_for_topic(topic: str) -> str:
    """Return the directory name produced by extract_ros2_bag for a ROS topic."""
    known = TOPIC_OUTPUT_MAP.get(topic)
    if known is not None:
        return known[0]
    clean = topic.replace("/drivers/ins", "").replace("/drivers/canbus", "")
    parts = [part for part in clean.split("/") if part]
    if not parts:
        raise ValueError(f"topic does not produce an extracted directory: {topic!r}")
    return parts[0]


def output_dir_for_role(role: str, *, message_type: str | None = None) -> str:
    """Return the canonical sync_data directory for a selected sensor role."""
    if role == "localization":
        return "odom" if message_type and "Odometry" in message_type else "ins"
    try:
        return ROLE_OUTPUT_DIR[role]
    except KeyError as error:
        raise ValueError(f"unsupported navigation sensor role: {role}") from error


def topic_route(topic: str, role: str, *, message_type: str | None = None) -> tuple[str, str]:
    """Resolve the source and canonical output directories for a model-selected topic."""
    known = TOPIC_OUTPUT_MAP.get(topic)
    if known is not None:
        return known
    return extracted_dir_for_topic(topic), output_dir_for_role(
        role,
        message_type=message_type,
    )
