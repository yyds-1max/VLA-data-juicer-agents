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
}


def topics_for_role(topics: set[str], role: str) -> list[str]:
    aliases = SENSOR_TOPIC_ALIASES[role]
    return [topic for topic in aliases if topic in topics]
