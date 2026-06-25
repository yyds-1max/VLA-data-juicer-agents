from dataclasses import dataclass

from vla_data_juicer_agents.navigation.models import ProfileClassification


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
    "/drivers/ins/Ins": ("drivers", "ins"),
}


def topics_for_role(topics: set[str], role: str) -> list[str]:
    aliases = SENSOR_TOPIC_ALIASES[role]
    return [topic for topic in aliases if topic in topics]


@dataclass(frozen=True)
class NavigationProfile:
    name: str
    required_topics: frozenset[str]
    extract_topics: tuple[str, ...]
    sync_topic_map: dict[str, str]
    lidar_dirs: tuple[str, ...]


PROFILES = {
    "u_legacy_like": NavigationProfile(
        name="u_legacy_like",
        required_topics=frozenset(
            {
                "/cam_video5/csi_cam/image_raw/compressed",
                "/lidar_points",
                "/utlidar/robot_odom_systime",
            }
        ),
        extract_topics=(
            "/cam_video5/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/utlidar/robot_odom_systime",
        ),
        sync_topic_map={
            "cam_video5": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "utlidar": "odom",
        },
        lidar_dirs=("r32_rslidar_points", "lidar_points"),
    ),
    "go2w_like": NavigationProfile(
        name="go2w_like",
        required_topics=frozenset(
            {
                "/cam_video4/csi_cam/image_raw/compressed",
                "/rs32_lidar_points",
                "/sport_odom",
            }
        ),
        extract_topics=(
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ),
        sync_topic_map={
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        lidar_dirs=("r32_rslidar_points", "rs32_lidar_points"),
    ),
}


def get_profile(name: str) -> NavigationProfile:
    return PROFILES[name]


def classify_topics(topics: set[str]) -> ProfileClassification:
    best_profile: NavigationProfile | None = None
    best_matched: set[str] = set()
    best_missing: set[str] = set()
    best_score = 0.0

    for profile in PROFILES.values():
        matched = profile.required_topics.intersection(topics)
        missing = profile.required_topics.difference(topics)
        score = len(matched) / len(profile.required_topics)
        if score > best_score:
            best_profile = profile
            best_matched = matched
            best_missing = missing
            best_score = score

    if best_profile is not None and best_score == 1.0:
        return ProfileClassification(
            profile_name=best_profile.name,
            confidence=1.0,
            matched_topics=sorted(best_matched),
            missing_topics=[],
        )

    notes = ["No dataset profile matched all required topics."]
    return ProfileClassification(
        profile_name=None,
        confidence=best_score,
        matched_topics=sorted(best_matched),
        missing_topics=sorted(best_missing),
        notes=notes,
    )
