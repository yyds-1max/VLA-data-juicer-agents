import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import NavigationRequest


def test_navigation_request_defaults_to_all_segments():
    request = NavigationRequest(date="20270605")
    assert request.segments is None
    assert request.scene_mode is None
    assert request.dry_run is False


def test_navigation_request_accepts_scene_mode():
    assert NavigationRequest(date="20270605", scene_mode="in").scene_mode == "in"


def test_navigation_request_rejects_unknown_scene_mode():
    with pytest.raises(ValueError):
        NavigationRequest(date="20270605", scene_mode="indoor")


def test_navigation_request_rejects_bad_date():
    with pytest.raises(ValueError):
        NavigationRequest(date="2026-06-05")


def test_navigation_settings_derives_data_roots(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")
    assert settings.raw_data_root == tmp_path / "VLADatasets" / "raw_data"
    assert settings.clip_data_root == tmp_path / "VLADatasets" / "clip_data"
    assert settings.finish_data_root == tmp_path / "VLADatasets" / "finish_data"
