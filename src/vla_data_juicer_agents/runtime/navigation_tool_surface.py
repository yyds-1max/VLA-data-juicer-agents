"""System-managed AgentScope tool groups for a navigation session."""

from __future__ import annotations

from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolGroup, ToolResponse

from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.agent_tools import (
    resolve_navigation_tool_surface,
)
from vla_data_juicer_agents.navigation.services import NavigationServices


class NavigationToolSurfaceSyncError(RuntimeError):
    """Raised after a navigation surface refresh has failed closed."""


class NavigationToolSurfaceMiddleware(MiddlewareBase):
    """Project the durable navigation state onto an AgentScope Toolkit."""

    def __init__(
        self,
        *,
        services: NavigationServices,
        web_session_id: str,
        agentscope_session_id: str,
        cancellation: CancellationContext | None,
    ) -> None:
        self._services = services
        self._web_session_id = web_session_id
        self._agentscope_session_id = agentscope_session_id
        self._cancellation = cancellation

    def _clear(self, agent: Agent) -> None:
        agent.toolkit.tool_groups[:] = [ToolGroup(name="basic")]
        agent.state.tool_context.activated_groups[:] = []

    def _synchronize(self, agent: Agent) -> None:
        try:
            surface = resolve_navigation_tool_surface(
                services=self._services,
                web_session_id=self._web_session_id,
                agentscope_session_id=self._agentscope_session_id,
                cancellation=self._cancellation,
            )
            if surface is None:
                raise LookupError("missing authorized navigation attempt")
            groups = [
                ToolGroup(
                    name=definition.name,
                    description=definition.description,
                    instructions=definition.instructions,
                    tools=list(definition.tools),
                )
                for definition in surface.groups
            ]
            agent.toolkit.tool_groups[:] = [ToolGroup(name="basic"), *groups]
            agent.state.tool_context.activated_groups[:] = list(
                surface.active_group_names
            )
        except Exception as error:
            self._clear(agent)
            raise NavigationToolSurfaceSyncError(
                "navigation tool surface unavailable"
            ) from error

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        self._synchronize(agent)
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_model_call(self, agent, input_kwargs, next_handler):
        tools = [
            schema
            for schema in input_kwargs["tools"]
            if schema.get("function", {}).get("name") != "reset_tools"
        ]
        return await next_handler(**{**input_kwargs, "tools": tools})

    async def on_acting(self, agent, input_kwargs, next_handler):
        tool_call = input_kwargs["tool_call"]
        if tool_call.name == "reset_tools":
            yield ToolResponse(
                id=tool_call.id,
                content=[
                    TextBlock(
                        text="Navigation tool groups are system managed."
                    )
                ],
                state=ToolResultState.ERROR,
                metadata={
                    "ok": False,
                    "error_type": "navigation_tool_groups_system_managed",
                },
            )
            return
        async for item in next_handler(**input_kwargs):
            if isinstance(item, ToolResponse):
                self._synchronize(agent)
            yield item
