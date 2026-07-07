from vla_data_juicer_agents.navigation.profiles import TOPIC_OUTPUT_MAP, topics_for_role


def test_topics_for_role_returns_known_aliases_in_priority_order():
    topics = {
        "/cam_video4/csi_cam/image_raw/compressed",
        "/cam_video5/csi_cam/image_raw/compressed",
        "/sport_odom",
    }

    assert topics_for_role(topics, "fisheye_front") == [
        "/cam_video4/csi_cam/image_raw/compressed",
        "/cam_video5/csi_cam/image_raw/compressed",
    ]
    assert topics_for_role(topics, "odom") == ["/sport_odom"]


def test_topic_output_map_contains_mixed_platform_sources():
    assert TOPIC_OUTPUT_MAP["/cam_video4/csi_cam/image_raw/compressed"] == (
        "cam_video4",
        "fisheye_front",
    )
    assert TOPIC_OUTPUT_MAP["/lidar_points"] == ("lidar_points", "r32_rslidar_points")
    assert TOPIC_OUTPUT_MAP["/drivers/ins/Ins"] == ("Ins", "ins")
