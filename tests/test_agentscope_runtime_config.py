from pathlib import Path

import pytest

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig


def test_from_env_reads_required_values_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-default")
    monkeypatch.delenv("VLA_AGENT_ROUTER_MODEL", raising=False)
    monkeypatch.delenv("VLA_AGENT_NAVIGATION_MODEL", raising=False)
    monkeypatch.delenv("VLA_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("VLA_DATA_AGENT_WEB_WORKING_DIR", raising=False)

    config = AgentScopeRuntimeConfig.from_env(workspace_root=tmp_path)

    assert config.user_id == "default"
    assert config.redis_url == "redis://localhost:6379/0"
    assert config.workspace_root == tmp_path
    assert config.dashscope_api_key == "test-key"
    assert config.dashscope_base_url is None
    assert config.default_model == "qwen-default"
    assert config.router_model == "qwen-default"
    assert config.navigation_model == "qwen-default"
    assert config.credential_id == "dashscope-env"
    assert config.main_router_agent_id == "main-router-agent"
    assert config.navigation_agent_id == "navigation-data-agent"
    assert config.agentscope_mount_path == "/api/agentscope"


def test_from_env_uses_separate_router_and_navigation_models(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY", " test-key ")
    monkeypatch.setenv("VLA_AGENT_MODEL", " qwen-default ")
    monkeypatch.setenv("VLA_AGENT_ROUTER_MODEL", " qwen-router ")
    monkeypatch.setenv("VLA_AGENT_NAVIGATION_MODEL", " qwen-navigation ")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", " https://dashscope.example.test ")
    monkeypatch.setenv("VLA_AGENT_USER_ID", " alice ")
    monkeypatch.setenv("VLA_AGENT_REDIS_URL", " redis://redis:6379/2 ")
    monkeypatch.setenv("VLA_AGENTSCOPE_MOUNT_PATH", " /agentscope ")

    config = AgentScopeRuntimeConfig.from_env(workspace_root=tmp_path)

    assert config.user_id == "alice"
    assert config.redis_url == "redis://redis:6379/2"
    assert config.dashscope_base_url == "https://dashscope.example.test"
    assert config.default_model == "qwen-default"
    assert config.router_model == "qwen-router"
    assert config.navigation_model == "qwen-navigation"
    assert config.agentscope_mount_path == "/agentscope"


def test_from_env_requires_dashscope_api_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "   ")
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-default")

    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        AgentScopeRuntimeConfig.from_env()


def test_from_env_requires_model(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("VLA_AGENT_MODEL", "   ")
    monkeypatch.setenv("VLA_AGENT_ROUTER_MODEL", "\t")
    monkeypatch.setenv("VLA_AGENT_NAVIGATION_MODEL", "\n")

    with pytest.raises(RuntimeError, match="VLA_AGENT_MODEL"):
        AgentScopeRuntimeConfig.from_env()


def test_from_env_uses_workspace_env_precedence(monkeypatch, tmp_path):
    workspace_root = tmp_path / "agent-workspace"
    web_working_dir = tmp_path / "web-workspace"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-default")
    monkeypatch.setenv("VLA_AGENT_WORKSPACE_ROOT", f" {workspace_root} ")
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(web_working_dir))

    config = AgentScopeRuntimeConfig.from_env()

    assert config.workspace_root == workspace_root


def test_from_env_uses_web_working_dir_when_agent_workspace_missing(monkeypatch, tmp_path):
    web_working_dir = tmp_path / "web-workspace"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-default")
    monkeypatch.delenv("VLA_AGENT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("VLA_DATA_AGENT_WEB_WORKING_DIR", str(web_working_dir))

    config = AgentScopeRuntimeConfig.from_env()

    assert config.workspace_root == web_working_dir


def test_redis_connection_kwargs_parses_redis_url():
    config = AgentScopeRuntimeConfig(
        user_id="default",
        redis_url="redis://redis:6379/2",
        workspace_root=Path("./.djx"),
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-default",
        navigation_model="qwen-default",
    )

    assert config.redis_connection_kwargs() == {
        "host": "redis",
        "port": 6379,
        "db": 2,
        "password": None,
    }


def test_redis_connection_kwargs_percent_decodes_password():
    config = AgentScopeRuntimeConfig(
        user_id="default",
        redis_url="redis://:pa%24%24%20word@redis:6380/1",
        workspace_root=Path("./.djx"),
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-default",
        navigation_model="qwen-default",
    )

    assert config.redis_connection_kwargs() == {
        "host": "redis",
        "port": 6380,
        "db": 1,
        "password": "pa$$ word",
    }


def test_redis_connection_kwargs_rejects_non_redis_url():
    config = AgentScopeRuntimeConfig(
        user_id="default",
        redis_url="http://redis:6379/2",
        workspace_root=Path("./.djx"),
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-default",
        navigation_model="qwen-default",
    )

    with pytest.raises(ValueError, match="redis://"):
        config.redis_connection_kwargs()


@pytest.mark.parametrize(
    ("redis_url", "message"),
    [
        ("redis:///2", "host"),
        ("redis://redis:bad/2", "port"),
        ("redis://redis:6379/not-a-db", "database"),
    ],
)
def test_redis_connection_kwargs_rejects_malformed_redis_urls(redis_url, message):
    config = AgentScopeRuntimeConfig(
        user_id="default",
        redis_url=redis_url,
        workspace_root=Path("./.djx"),
        dashscope_api_key="test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-default",
        navigation_model="qwen-default",
    )

    with pytest.raises(ValueError, match=message):
        config.redis_connection_kwargs()
