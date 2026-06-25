from vla_data_juicer_agents.navigation.profiles import classify_topics, get_profile


def test_legacy_profile_registry_classifies_20270515_topic_family():
    topics = {
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/utlidar/robot_odom_systime",
    }

    result = classify_topics(topics)

    assert result.profile_name == "u_legacy_like"
    assert result.confidence == 1.0


def test_legacy_profile_registry_classifies_20270605_topic_family():
    topics = {
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    }

    result = classify_topics(topics)

    assert result.profile_name == "go2w_like"
    assert result.confidence == 1.0


def test_legacy_profile_registry_contains_sync_mapping():
    profile = get_profile("go2w_like")

    assert profile.sync_topic_map["cam_video4"] == "fisheye_front"
    assert profile.sync_topic_map["rs32_lidar_points"] == "r32_rslidar_points"
    assert profile.sync_topic_map["sport_odom"] == "odom"
