from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.inspection import inspect_raw_date, list_navigation_dates


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"


def test_list_navigation_dates_finds_raw_dates():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    dates = list_navigation_dates("raw_data", settings=settings)

    assert dates == ["20270515", "20270605"]


def test_inspect_raw_date_reads_topics():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = inspect_raw_date("20270605", settings=settings)

    assert result.exists is True
    assert result.segments[0].name == "20260605_152856"
    topic_names = {topic.name for topic in result.segments[0].topics}
    assert "/cam_video4/csi_cam/image_raw/compressed" in topic_names
    assert "/sport_odom" in topic_names
