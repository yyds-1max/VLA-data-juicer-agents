from __future__ import annotations

from dataclasses import dataclass

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import AgentData, AgentRecord, StorageBase
from agentscope.credential import DashScopeCredential

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_prompts import (
    main_router_prompt,
    navigation_agent_prompt,
)


DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True)
class BootstrappedAgentRecords:
    credential_id: str
    main_router_agent_id: str
    navigation_agent_id: str


def _context_config() -> ContextConfig:
    return ContextConfig(tool_result_limit=6000)


def _agent_record(
    *,
    user_id: str,
    agent_id: str,
    name: str,
    system_prompt: str,
    max_iters: int,
) -> AgentRecord:
    return AgentRecord(
        id=agent_id,
        user_id=user_id,
        data=AgentData(
            id=agent_id,
            name=name,
            system_prompt=system_prompt,
            context_config=_context_config(),
            react_config=ReActConfig(max_iters=max_iters),
        ),
    )


async def bootstrap_agentscope_records(
    storage: StorageBase,
    config: AgentScopeRuntimeConfig,
) -> BootstrappedAgentRecords:
    credential = DashScopeCredential(
        id=config.credential_id,
        name="DashScope",
        api_key=config.dashscope_api_key,
        base_url=config.dashscope_base_url or DEFAULT_DASHSCOPE_BASE_URL,
    )
    credential_id = await storage.upsert_credential(config.user_id, credential)

    main_router_agent_id = await storage.upsert_agent(
        config.user_id,
        _agent_record(
            user_id=config.user_id,
            agent_id=config.main_router_agent_id,
            name="MainRouterAgent",
            system_prompt=main_router_prompt(),
            max_iters=8,
        ),
    )
    navigation_agent_id = await storage.upsert_agent(
        config.user_id,
        _agent_record(
            user_id=config.user_id,
            agent_id=config.navigation_agent_id,
            name="NavigationDataAgent",
            system_prompt=navigation_agent_prompt(),
            max_iters=40,
        ),
    )

    return BootstrappedAgentRecords(
        credential_id=credential_id,
        main_router_agent_id=main_router_agent_id,
        navigation_agent_id=navigation_agent_id,
    )
