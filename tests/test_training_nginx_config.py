from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "deployment" / "nginx" / "datapilot-training-center.conf"


def test_training_center_proxy_allowlists_real_worker_protocol() -> None:
    config = CONFIG.read_text(encoding="utf-8")

    assert "/training-actions/poll$" in config
    assert (
        "/training-actions/training_action_[A-Za-z0-9_-]{8,160}/result$"
        in config
    )
    assert "/runs/run_[A-Za-z0-9_-]{8,160}/updates$" in config
    assert "proxy_read_timeout 35s;" in config
    assert "location / {\n        return 404;\n    }" in config
