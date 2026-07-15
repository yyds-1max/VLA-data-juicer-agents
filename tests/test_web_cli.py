from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.web.cli import _agentscope_runtime_from_env, main


def test_main_sets_env_and_delegates_to_uvicorn(monkeypatch):
    calls = []
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.delenv("VLA_AGENT_ENABLE_AGENTSCOPE", raising=False)
    monkeypatch.setattr(
        "vla_data_juicer_agents.web.cli._agentscope_runtime_from_env",
        lambda _working_dir: SimpleNamespace(),
    )
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_WORKING_DIR", raising=False)
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_MODEL", raising=False)
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_FRONTEND_DIST", raising=False)

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
            "--frontend-dist",
            "frontend/dist",
            "--reload",
        ]
    )

    assert result == 0
    assert calls == [
        (
            ("vla_data_juicer_agents.web.cli:create_cli_app",),
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
    assert os.environ["VLA_DATA_AGENT_WEB_FRONTEND_DIST"] == "frontend/dist"


def test_main_clears_stale_model_env_when_model_not_passed(monkeypatch):
    calls = []
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.delenv("VLA_AGENT_ENABLE_AGENTSCOPE", raising=False)
    monkeypatch.setattr(
        "vla_data_juicer_agents.web.cli._agentscope_runtime_from_env",
        lambda _working_dir: SimpleNamespace(),
    )
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_WORKING_DIR", raising=False)
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_MODEL", "stale-model")
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_FRONTEND_DIST", "stale-dist")

    result = main([])

    assert result == 0
    assert calls
    assert os.environ["VLA_DATA_AGENT_WEB_WORKING_DIR"] == "./.djx"
    assert "VLA_DATA_AGENT_WEB_MODEL" not in os.environ
    assert "VLA_DATA_AGENT_WEB_FRONTEND_DIST" not in os.environ


def test_agentscope_runtime_from_env_rejects_legacy_disable_switch(monkeypatch):
    monkeypatch.setenv("VLA_AGENT_ENABLE_AGENTSCOPE", " FALSE ")

    with pytest.raises(RuntimeError, match="AgentScope"):
        _agentscope_runtime_from_env("/tmp/djx")


def test_main_returns_nonzero_with_clear_agentscope_config_error(monkeypatch, capsys):
    calls = []
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.delenv("VLA_AGENT_ENABLE_AGENTSCOPE", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("VLA_AGENT_MODEL", raising=False)
    monkeypatch.delenv("VLA_AGENT_ROUTER_MODEL", raising=False)
    monkeypatch.delenv("VLA_AGENT_NAVIGATION_MODEL", raising=False)

    result = main(["--working-dir", "/tmp/djx"])

    assert result != 0
    assert calls == []
    assert "AgentScope runtime configuration failed" in capsys.readouterr().err
