from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import agentscope.app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig


@dataclass
class AgentScopeRuntime:
    config: AgentScopeRuntimeConfig
    storage: Any
    message_bus: Any
    workspace_manager: Any
    app: Any
    web_sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    bootstrapped: bool = False

    def __post_init__(self) -> None:
        self._bootstrap_lock = asyncio.Lock()

    async def ensure_bootstrapped(self) -> None:
        if self.bootstrapped:
            return

        async with self._bootstrap_lock:
            if self.bootstrapped:
                return
            await bootstrap_agentscope_records(self.storage, self.config)
            self.bootstrapped = True


def create_agentscope_runtime(config: AgentScopeRuntimeConfig) -> AgentScopeRuntime:
    redis_kwargs = config.redis_connection_kwargs()
    storage = RedisStorage(**redis_kwargs)
    message_bus = RedisMessageBus(**redis_kwargs)
    workspace_manager = LocalWorkspaceManager(
        basedir=str(config.workspace_root / "agentscope-workspaces"),
    )
    app = agentscope.app.create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_tools=None,
        title="DataPilot AgentScope Runtime",
    )

    return AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        app=app,
    )
