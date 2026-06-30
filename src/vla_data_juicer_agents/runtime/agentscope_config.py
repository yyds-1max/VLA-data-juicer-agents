from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_env(name: str) -> str:
    value = _env(name)
    if value is None:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass(frozen=True)
class AgentScopeRuntimeConfig:
    user_id: str
    redis_url: str
    workspace_root: Path
    dashscope_api_key: str
    dashscope_base_url: str | None
    default_model: str
    router_model: str
    navigation_model: str
    credential_id: str = "dashscope-env"
    main_router_agent_id: str = "main-router-agent"
    navigation_agent_id: str = "navigation-data-agent"
    agentscope_mount_path: str = "/api/agentscope"

    def redis_connection_kwargs(self) -> dict[str, str | int | None]:
        parsed = urlparse(self.redis_url)
        if parsed.scheme != "redis":
            raise ValueError("Redis URL must use the redis:// scheme")
        if not parsed.hostname:
            raise ValueError("Redis URL must include a host")

        try:
            port = parsed.port or 6379
        except ValueError as exc:
            raise ValueError("Redis URL port must be an integer") from exc

        try:
            db = int(parsed.path.lstrip("/") or "0")
        except ValueError as exc:
            raise ValueError("Redis URL database path must be an integer") from exc

        return {
            "host": parsed.hostname,
            "port": port,
            "db": db,
            "password": unquote(parsed.password) if parsed.password else None,
        }

    @classmethod
    def from_env(cls, workspace_root: str | Path | None = None) -> AgentScopeRuntimeConfig:
        dashscope_api_key = _required_env("DASHSCOPE_API_KEY")
        router_model_env = _env("VLA_AGENT_ROUTER_MODEL")
        navigation_model_env = _env("VLA_AGENT_NAVIGATION_MODEL")
        default_model = _env("VLA_AGENT_MODEL") or router_model_env or navigation_model_env
        if default_model is None:
            raise RuntimeError(
                "VLA_AGENT_MODEL is required unless VLA_AGENT_ROUTER_MODEL or "
                "VLA_AGENT_NAVIGATION_MODEL is set"
            )

        workspace_root_value = (
            workspace_root
            or _env("VLA_AGENT_WORKSPACE_ROOT")
            or _env("VLA_DATA_AGENT_WEB_WORKING_DIR")
            or "./.djx"
        )

        return cls(
            user_id=_env("VLA_AGENT_USER_ID") or "default",
            redis_url=_env("VLA_AGENT_REDIS_URL") or "redis://localhost:6379/0",
            workspace_root=Path(workspace_root_value),
            dashscope_api_key=dashscope_api_key,
            dashscope_base_url=_env("DASHSCOPE_BASE_URL"),
            default_model=default_model,
            router_model=router_model_env or default_model,
            navigation_model=navigation_model_env or default_model,
            agentscope_mount_path=_env("VLA_AGENTSCOPE_MOUNT_PATH") or "/api/agentscope",
        )
