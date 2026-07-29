"""System-managed AgentScope tool groups for a navigation session."""

from __future__ import annotations

import json

from agentscope.agent import Agent
from agentscope.event import (
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from agentscope.message import AssistantMsg, TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolGroup, ToolResponse

from vla_data_juicer_agents.annotation.models import (
    public_annotation_error_ref,
)
from vla_data_juicer_agents.core.cancellation import CancellationContext
from vla_data_juicer_agents.navigation.agent_tools import (
    resolve_navigation_tool_surface,
)
from vla_data_juicer_agents.navigation.services import NavigationServices


class NavigationToolSurfaceSyncError(RuntimeError):
    """Raised after a navigation surface refresh has failed closed."""


def _operator_recovery_payload(response: ToolResponse) -> dict[str, str] | None:
    """Recognize the one system-owned terminal recovery disposition."""

    candidates: list[object] = []
    metadata = getattr(response, "metadata", None)
    if isinstance(metadata, dict):
        candidates.append(metadata)
    content = getattr(response, "content", None)
    blocks = content if isinstance(content, list) else [content]
    for block in blocks:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            candidates.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            continue
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and candidate.get("next_action") == "operator_recovery_required"
        ):
            projected = {"next_action": "operator_recovery_required"}
            error_ref = public_annotation_error_ref(candidate.get("error_ref"))
            if error_ref is not None:
                projected["error_ref"] = error_ref
            return projected
    return None


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
        self._operator_recovery: dict[str, str] | None = None

    def _clear(self, agent: Agent) -> None:
        agent.toolkit.tool_groups[:] = [ToolGroup(name="basic")]
        agent.state.tool_context.activated_groups[:] = []

    def _synchronize(self, agent: Agent) -> str | None:
        try:
            surface = resolve_navigation_tool_surface(
                services=self._services,
                web_session_id=self._web_session_id,
                agentscope_session_id=self._agentscope_session_id,
                cancellation=self._cancellation,
            )
            if surface is None:
                raise LookupError("missing authorized navigation attempt")
            if surface.waiting_for_running_step:
                self._clear(agent)
                return surface.suspended_step_status or "running"
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
            return None
        except Exception as error:
            self._clear(agent)
            raise NavigationToolSurfaceSyncError(
                "navigation tool surface unavailable"
            ) from error

    async def on_reply(self, agent, input_kwargs, next_handler):
        self._operator_recovery = None
        self._synchronize(agent)
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        if self._operator_recovery is not None:
            recovery = self._operator_recovery
            self._operator_recovery = None
            task_store = getattr(self._services, "task_store", None)
            find_by_session = getattr(task_store, "find_by_session", None)
            task = (
                find_by_session(
                    web_session_id=self._web_session_id,
                    agentscope_session_id=self._agentscope_session_id,
                )
                if callable(find_by_session)
                else None
            )
            request = str(getattr(task, "request", "") or "")
            error_ref = recovery.get("error_ref")
            if any("\u4e00" <= char <= "\u9fff" for char in request):
                message = (
                    "Answer:\n处理未启动，需要运维人员恢复处理环境后再继续。"
                )
                if error_ref is not None:
                    message += f"错误参考：{error_ref}。"
            else:
                message = (
                    "Answer:\nProcessing did not start. Operator recovery is "
                    "required before processing can continue."
                )
                if error_ref is not None:
                    message += f" Error reference: {error_ref}."
            block_id = f"operator_recovery_{agent.state.reply_id}"
            save_to_context = getattr(agent, "_save_to_context", None)
            if callable(save_to_context):
                save_to_context([TextBlock(text=message)])
            yield TextBlockStartEvent(
                reply_id=agent.state.reply_id,
                block_id=block_id,
            )
            yield TextBlockDeltaEvent(
                reply_id=agent.state.reply_id,
                block_id=block_id,
                delta=message,
            )
            yield TextBlockEndEvent(
                reply_id=agent.state.reply_id,
                block_id=block_id,
            )
            yield AssistantMsg(
                id=agent.state.reply_id,
                name=agent.name,
                content=message,
            )
            return
        suspended_status = self._synchronize(agent)
        if suspended_status is not None:
            message = (
                "The navigation workflow is waiting for required user input "
                "and the session will resume automatically after that input "
                "is submitted."
                if suspended_status == "waiting_user"
                else (
                    "Background navigation processing is still running; "
                    "the session will resume automatically after completion."
                )
            )
            yield AssistantMsg(
                id=agent.state.reply_id,
                name=agent.name,
                content=message,
            )
            return
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
        if self._operator_recovery is not None:
            yield ToolResponse(
                id=tool_call.id,
                content=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "ok": False,
                                "error_type": "operator_recovery_in_progress",
                                "message": (
                                    "A prior tool result requires operator "
                                    "recovery; this tool was not executed."
                                ),
                                "next_action": "operator_recovery_required",
                            },
                            separators=(",", ":"),
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
            return
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
                recovery = _operator_recovery_payload(item)
                if recovery is not None:
                    self._operator_recovery = recovery
                else:
                    self._synchronize(agent)
            yield item
