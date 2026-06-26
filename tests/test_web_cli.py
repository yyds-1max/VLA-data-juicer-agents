from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from vla_data_juicer_agents.web.cli import main


def test_main_sets_env_and_delegates_to_uvicorn(monkeypatch):
    calls = []
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_WORKING_DIR", raising=False)
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_MODEL", raising=False)

    result = main(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--working-dir",
            "/tmp/djx",
            "--model",
            "qwen-test",
            "--reload",
        ]
    )

    assert result == 0
    assert calls == [
        (
            ("vla_data_juicer_agents.web.app:create_app",),
            {
                "factory": True,
                "host": "0.0.0.0",
                "port": 9000,
                "reload": True,
                "app_dir": None,
                "log_level": "info",
            },
        )
    ]
    assert os.environ["VLA_DATA_AGENT_WEB_WORKING_DIR"] == "/tmp/djx"
    assert os.environ["VLA_DATA_AGENT_WEB_MODEL"] == "qwen-test"


def test_main_clears_stale_model_env_when_model_not_passed(monkeypatch):
    calls = []
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_WORKING_DIR", raising=False)
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_MODEL", "stale-model")

    result = main([])

    assert result == 0
    assert calls
    assert os.environ["VLA_DATA_AGENT_WEB_WORKING_DIR"] == "./.djx"
    assert "VLA_DATA_AGENT_WEB_MODEL" not in os.environ
