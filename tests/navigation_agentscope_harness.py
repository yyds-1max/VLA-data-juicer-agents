from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentscope.agent import Agent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit
from pydantic import BaseModel

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import AgentScopeRuntime


ResponseFactory = Callable[
    [list[Msg], list[dict[str, Any]]],
    list[TextBlock | ToolCallBlock],
]


@dataclass(frozen=True)
class ModelInvocation:
    formatted_messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    input_tokens: int
    response_blocks: list[dict[str, Any]]


class ScriptedChatModel(ChatModelBase):
    class Parameters(BaseModel):
        pass

    def __init__(self, *, context_size: int = 131_072) -> None:
        super().__init__(
            credential=SimpleNamespace(),
            model="offline-scripted-qwen",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
            context_size=context_size,
        )
        self.formatter = DashScopeChatFormatter()
        self._responses: list[ResponseFactory] = []
        self.invocations: list[ModelInvocation] = []
        self.compact_events: list[dict[str, Any]] = []
        self.compact_event_count = 0

    def enqueue_tool(
        self,
        name: str,
        arguments: dict[str, Any] | Callable[[list[Msg]], dict[str, Any]],
    ) -> None:
        def response(_messages: list[Msg], tools: list[dict[str, Any]]):
            available = {
                item["function"]["name"]
                for item in tools
            }
            if name not in available:
                raise AssertionError(
                    f"scripted tool {name!r} is not exposed; available={sorted(available)}"
                )
            payload = arguments(_messages) if callable(arguments) else arguments
            return [
                ToolCallBlock(
                    id=f"call-{len(self.invocations) + 1}-{name}",
                    name=name,
                    input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            ]

        self._responses.append(response)

    def enqueue_text(self, text: str) -> None:
        self._responses.append(
            lambda _messages, _tools: [TextBlock(text=text)]
        )

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice=None,
        **kwargs: Any,
    ) -> ChatResponse:
        del model_name, tool_choice, kwargs
        normalized_tools = list(tools or [])
        if any(
            item.get("function", {}).get("name") == "generate_structured_output"
            for item in normalized_tools
        ):
            self.compact_event_count += 1
            self.compact_events.append(
                {
                    "message_count": len(messages),
                    "tool_names": [
                        item.get("function", {}).get("name")
                        for item in normalized_tools
                    ],
                }
            )
        if not self._responses:
            raise AssertionError("scripted model has no response for this invocation")
        factory = self._responses.pop(0)
        blocks = factory(messages, normalized_tools)
        input_tokens = await ChatModelBase.count_tokens(
            self,
            messages=messages,
            tools=normalized_tools,
        )
        output_tokens = await ChatModelBase.count_tokens(
            self,
            messages=[AssistantMsg(name="scripted", content=blocks)],
            tools=None,
        )
        formatted = await self.formatter.format(messages)
        self.invocations.append(
            ModelInvocation(
                formatted_messages=copy.deepcopy(formatted),
                tools=copy.deepcopy(normalized_tools),
                input_tokens=input_tokens,
                response_blocks=[block.model_dump(mode="json") for block in blocks],
            )
        )
        return ChatResponse(
            content=blocks,
            is_last=True,
            usage=ChatUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                time=0.0,
            ),
        )

    def assert_exhausted(self) -> None:
        assert self._responses == []


class LocalAgentScopeStorage:
    def __init__(self) -> None:
        self.agents: dict[str, Any] = {}
        self.sessions: dict[tuple[str, str, str], Any] = {}

    async def upsert_credential(self, _user_id, credential):
        return credential.id

    async def upsert_agent(self, _user_id, record):
        self.agents[record.id] = record
        return record.id

    async def upsert_session(self, user_id, agent_id, config, *, session_id=None):
        session = SimpleNamespace(id=session_id, config=config)
        self.sessions[(user_id, agent_id, session_id)] = session
        return session

    async def get_session(self, user_id, agent_id, session_id):
        return self.sessions.get((user_id, agent_id, session_id))


class LocalChatService:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> None:
        self.runs.append(kwargs)


class LocalChatRunRegistry:
    def __init__(self) -> None:
        self.spawns: list[dict[str, Any]] = []

    def spawn(self, coroutine, *, session_id: str) -> None:
        self.spawns.append({"coroutine": coroutine, "session_id": session_id})

    async def drain(self) -> None:
        while self.spawns:
            spawn = self.spawns.pop(0)
            await spawn["coroutine"]


def runtime_config(workspace_root: Path, *, dry_run: bool = True):
    return AgentScopeRuntimeConfig(
        user_id="offline-test-user",
        redis_url="redis://localhost:6379/0",
        workspace_root=workspace_root,
        dashscope_api_key="offline-test-key",
        dashscope_base_url=None,
        default_model="qwen-default",
        router_model="qwen-router",
        navigation_model="qwen-navigation",
        navigation_dry_run=dry_run,
    )


async def build_runtime(workspace_root: Path, *, dry_run: bool = True):
    storage = LocalAgentScopeStorage()
    registry = LocalChatRunRegistry()
    chat_service = LocalChatService()
    runtime = AgentScopeRuntime(
        config=runtime_config(workspace_root, dry_run=dry_run),
        storage=storage,
        message_bus=object(),
        workspace_manager=object(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_service=chat_service,
                chat_run_registry=registry,
            )
        ),
    )
    await runtime.ensure_bootstrapped()
    return runtime, storage, registry


def build_agent(record, model: ScriptedChatModel, tools: list[Any]) -> Agent:
    return Agent(
        name=record.data.name,
        system_prompt=record.data.system_prompt,
        model=model,
        toolkit=Toolkit(tools=tools),
        context_config=record.data.context_config,
        react_config=record.data.react_config,
    )


def build_agent_with_middlewares(
    record,
    model,
    *,
    tools=None,
    middlewares=None,
    state=None,
):
    return Agent(
        name=record.data.name,
        system_prompt=record.data.system_prompt,
        model=model,
        toolkit=Toolkit(tools=tools or []),
        context_config=record.data.context_config,
        react_config=record.data.react_config,
        middlewares=middlewares or [],
        state=state,
    )


def refresh_tools(agent: Agent, tools: list[Any]) -> None:
    agent.toolkit = Toolkit(tools=tools)


async def run_reply(agent: Agent, text: str):
    events = []
    async for event in agent.reply_stream(UserMsg(name="user", content=text)):
        events.append(event)
    return events


def event_types(events: list[Any]) -> list[str]:
    return [str(getattr(event, "type", type(event).__name__)).upper() for event in events]


def schema_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {
        schema["function"]["name"]
        for schema in schemas
    }


def latest_tool_result_json(messages: list[Msg]) -> dict[str, Any]:
    for message in reversed(messages):
        blocks = message.get_content_blocks("tool_result")
        if not blocks:
            continue
        output = blocks[-1].output
        if isinstance(output, str):
            return json.loads(output)
        return json.loads(
            "".join(block.text for block in output if isinstance(block, TextBlock))
        )
    raise AssertionError("scripted model expected a prior tool result")


def tool_call_names(agent: Agent) -> list[str]:
    return [
        block.name
        for message in agent.state.context
        for block in message.get_content_blocks("tool_call")
    ]


def tool_result_outputs(agent: Agent) -> list[str]:
    outputs: list[str] = []
    for message in agent.state.context:
        for block in message.get_content_blocks("tool_result"):
            if isinstance(block.output, str):
                outputs.append(block.output)
            else:
                outputs.append(
                    "".join(
                        item.text
                        for item in block.output
                        if isinstance(item, TextBlock)
                    )
                )
    return outputs


def text_deltas(events: list[Any]) -> str:
    return "".join(
        str(event.delta)
        for event in events
        if str(getattr(event, "type", "")).upper() == "TEXT_BLOCK_DELTA"
    )
