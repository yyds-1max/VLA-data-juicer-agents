from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChunk, ToolResponse

from navigation_agentscope_harness import (
    ScriptedChatModel,
    build_agent_with_middlewares,
)
from vla_data_juicer_agents.navigation.tool_groups import (
    NavigationToolGroupDefinition,
    NavigationToolSurface,
)
from vla_data_juicer_agents.runtime.navigation_tool_surface import (
    NavigationToolSurfaceMiddleware,
    NavigationToolSurfaceSyncError,
)


def _tool(name: str) -> FunctionTool:
    def implementation() -> dict[str, bool]:
        return {"ok": True}

    return FunctionTool(implementation, name=name, is_read_only=True)


def _surface(activity: str, *group_names: str) -> NavigationToolSurface:
    groups = tuple(
        NavigationToolGroupDefinition(
            name=name,
            description=f"Authorized {name} tools.",
            instructions=f"Use only {name} tools.",
            tools=(_tool(f"{name}_tool"),),
        )
        for name in group_names
    )
    return NavigationToolSurface(
        activity=activity,
        groups=groups,
        active_group_names=tuple(group_names),
    )


@pytest.fixture
def planning() -> NavigationToolSurface:
    return _surface("planning", "planning_evidence", "planning_authoring")


@pytest.fixture
def execution() -> NavigationToolSurface:
    return _surface("execution", "execution_state", "execution_actions")


class SequenceResolver:
    def __init__(self, *results: NavigationToolSurface | None | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, **_kwargs: Any) -> NavigationToolSurface | None:
        result = self._results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def agent_state() -> AgentState:
    state = AgentState()
    state.tool_context.activated_groups.extend(
        ["stale_execution_state", "stale_execution_actions"]
    )
    return state


@pytest.fixture
def record() -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            name="navigation-test-agent",
            system_prompt="Navigate using only authorized tools.",
            context_config=SimpleNamespace(),
            react_config=SimpleNamespace(),
        )
    )


@pytest.fixture
def generic_tools() -> list[FunctionTool]:
    return [
        _tool(name)
        for name in ("bash", "read", "write", "task", "schedule", "team", "skill_viewer")
    ]


def _middleware(monkeypatch, resolver: SequenceResolver):
    monkeypatch.setattr(
        "vla_data_juicer_agents.runtime.navigation_tool_surface."
        "resolve_navigation_tool_surface",
        resolver,
    )
    return NavigationToolSurfaceMiddleware(
        services=object(),
        web_session_id="web-session-1",
        agentscope_session_id="agentscope-session-1",
        cancellation=None,
    )


def _agent(
    record,
    generic_tools,
    agent_state,
    middleware,
) -> Agent:
    return build_agent_with_middlewares(
        record,
        ScriptedChatModel(),
        tools=generic_tools,
        middlewares=[middleware],
        state=agent_state,
    )


@pytest.mark.asyncio
async def test_reply_refreshes_surface_before_forwarding_to_agent(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    planning,
):
    resolver = SequenceResolver(planning)
    middleware = _middleware(monkeypatch, resolver)
    agent = _agent(record, generic_tools, agent_state, middleware)
    seen = []

    async def forwarding_reply(**_kwargs):
        seen.append(
            (
                [group.name for group in agent.toolkit.tool_groups],
                [
                    tool.name
                    for group in agent.toolkit.tool_groups
                    for tool in group.tools
                ],
                list(agent.state.tool_context.activated_groups),
            )
        )
        yield "forwarded"

    events = [
        event
        async for event in middleware.on_reply(
            agent,
            {"inputs": None},
            forwarding_reply,
        )
    ]

    assert seen == [
        (
            ["basic", "planning_evidence", "planning_authoring"],
            ["planning_evidence_tool", "planning_authoring_tool"],
            list(planning.active_group_names),
        )
    ]
    assert resolver.calls == 1
    assert events == ["forwarded"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver_result",
    [None, RuntimeError("sensitive sqlite path: /private/navigation.sqlite")],
)
async def test_reply_refresh_failure_clears_surface_before_forwarding(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    resolver_result,
):
    middleware = _middleware(monkeypatch, SequenceResolver(resolver_result))
    agent = _agent(record, generic_tools, agent_state, middleware)
    group_list = agent.toolkit.tool_groups
    handler_calls = 0

    async def must_not_run(**_kwargs):
        nonlocal handler_calls
        handler_calls += 1
        yield "unexpected"

    with pytest.raises(NavigationToolSurfaceSyncError) as error:
        _ = [
            item
            async for item in middleware.on_reply(
                agent,
                {"inputs": None},
                must_not_run,
            )
        ]

    assert str(error.value) == "navigation tool surface unavailable"
    assert "sensitive" not in str(error.value)
    assert handler_calls == 0
    assert agent.toolkit.tool_groups is group_list
    assert len(agent.toolkit.tool_groups) == 1
    assert agent.toolkit.tool_groups[0].name == "basic"
    assert agent.toolkit.tool_groups[0].tools == []
    assert agent.state.tool_context.activated_groups == []


@pytest.mark.asyncio
async def test_reasoning_refresh_overwrites_stale_groups_and_removes_generic_tools(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    planning,
):
    resolver = SequenceResolver(planning)
    middleware = _middleware(monkeypatch, resolver)
    agent = _agent(record, generic_tools, agent_state, middleware)
    group_list = agent.toolkit.tool_groups

    async def forwarding_reasoning(**_kwargs):
        yield "forwarded"

    events = [
        event
        async for event in middleware.on_reasoning(
            agent,
            {"tool_choice": None},
            forwarding_reasoning,
        )
    ]

    assert agent.toolkit.tool_groups is group_list
    assert agent.toolkit.tool_groups[0].name == "basic"
    assert agent.toolkit.tool_groups[0].tools == []
    assert agent.state.tool_context.activated_groups == list(
        planning.active_group_names
    )
    assert resolver.calls == 1
    assert events == ["forwarded"]


@pytest.mark.asyncio
async def test_reasoning_resolves_fresh_surface_on_every_call(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    planning,
    execution,
):
    resolver = SequenceResolver(planning, execution)
    middleware = _middleware(monkeypatch, resolver)
    agent = _agent(record, generic_tools, agent_state, middleware)

    async def forwarding_reasoning(**_kwargs):
        yield "forwarded"

    for expected in (planning, execution):
        _ = [
            item
            async for item in middleware.on_reasoning(
                agent,
                {"tool_choice": None},
                forwarding_reasoning,
            )
        ]
        assert agent.state.tool_context.activated_groups == list(
            expected.active_group_names
        )

    assert resolver.calls == 2


@pytest.mark.asyncio
async def test_terminal_tool_response_refreshes_before_it_is_yielded(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    execution,
):
    resolver = SequenceResolver(execution)
    middleware = _middleware(monkeypatch, resolver)
    agent = _agent(record, generic_tools, agent_state, middleware)
    seen = []

    async def forwarding_tool(**_kwargs):
        yield ToolResponse(
            id="submit",
            content=[TextBlock(text='{"ok": true}')],
        )

    async for item in middleware.on_acting(
        agent,
        {
            "tool_call": ToolCallBlock(
                id="submit",
                name="submit_extract_sync_plan_tool",
                input="{}",
            )
        },
        forwarding_tool,
    ):
        if isinstance(item, ToolResponse):
            seen.append(tuple(agent.state.tool_context.activated_groups))

    assert seen == [execution.active_group_names]


@pytest.mark.asyncio
async def test_model_call_hides_reset_tools_schema(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    planning,
):
    middleware = _middleware(monkeypatch, SequenceResolver(planning))
    agent = _agent(record, generic_tools, agent_state, middleware)
    reset_schema = {"type": "function", "function": {"name": "reset_tools"}}
    domain_schema = {"type": "function", "function": {"name": "domain_tool"}}
    captured_names = []
    sentinel = object()

    async def capture_model_call(**kwargs):
        captured_names.extend(
            schema["function"]["name"] for schema in kwargs["tools"]
        )
        return sentinel

    result = await middleware.on_model_call(
        agent,
        {
            "messages": [],
            "tools": [reset_schema, domain_schema],
            "tool_choice": None,
            "current_model": agent.model,
        },
        capture_model_call,
    )

    assert result is sentinel
    assert captured_names == ["domain_tool"]


@pytest.mark.asyncio
async def test_fabricated_reset_tools_is_rejected_without_state_change(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    planning,
):
    middleware = _middleware(monkeypatch, SequenceResolver(planning))
    agent = _agent(record, generic_tools, agent_state, middleware)
    before = list(agent.state.tool_context.activated_groups)

    async def must_not_run(**_kwargs):
        raise AssertionError("fabricated reset_tools must not reach AgentScope")
        yield

    responses = [
        item
        async for item in middleware.on_acting(
            agent,
            {
                "tool_call": ToolCallBlock(
                    id="reset",
                    name="reset_tools",
                    input="{}",
                )
            },
            must_not_run,
        )
    ]

    assert responses[-1].state is ToolResultState.ERROR
    assert responses[-1].metadata == {
        "ok": False,
        "error_type": "navigation_tool_groups_system_managed",
    }
    assert agent.state.tool_context.activated_groups == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver_result",
    [None, RuntimeError("sensitive sqlite path: /private/navigation.sqlite")],
)
async def test_refresh_failure_clears_surface_and_raises_bounded_error(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    resolver_result,
):
    middleware = _middleware(monkeypatch, SequenceResolver(resolver_result))
    agent = _agent(record, generic_tools, agent_state, middleware)
    group_list = agent.toolkit.tool_groups

    async def must_not_run(**_kwargs):
        raise AssertionError("reasoning must not continue after refresh failure")
        yield

    with pytest.raises(NavigationToolSurfaceSyncError) as error:
        _ = [
            item
            async for item in middleware.on_reasoning(
                agent,
                {"tool_choice": None},
                must_not_run,
            )
        ]

    assert str(error.value) == "navigation tool surface unavailable"
    assert "sensitive" not in str(error.value)
    assert agent.toolkit.tool_groups is group_list
    assert len(agent.toolkit.tool_groups) == 1
    assert agent.toolkit.tool_groups[0].name == "basic"
    assert agent.toolkit.tool_groups[0].tools == []
    assert agent.state.tool_context.activated_groups == []


@pytest.mark.asyncio
async def test_error_responses_follow_authoritative_surface_not_response_text(
    monkeypatch,
    record,
    generic_tools,
    agent_state,
    execution,
    planning,
):
    resolver = SequenceResolver(execution, planning)
    middleware = _middleware(monkeypatch, resolver)
    agent = _agent(record, generic_tools, agent_state, middleware)

    async def failed_tool(**kwargs):
        yield ToolChunk(
            content=[TextBlock(text="partial")],
            state=ToolResultState.RUNNING,
            is_last=False,
        )
        yield ToolResponse(
            id=kwargs["tool_call"].id,
            content=[
                TextBlock(
                    text='{"ok": false, "error_type": "inactive_navigation_plan"}'
                )
            ],
            state=ToolResultState.ERROR,
        )

    states = []
    for call_id in ("failed-while-active", "failed-after-inactivation"):
        async for item in middleware.on_acting(
            agent,
            {
                "tool_call": ToolCallBlock(
                    id=call_id,
                    name="execution_actions_tool",
                    input="{}",
                )
            },
            failed_tool,
        ):
            if isinstance(item, ToolResponse):
                states.append(
                    (
                        tuple(agent.state.tool_context.activated_groups),
                        {
                            tool.name
                            for group in agent.toolkit.tool_groups
                            for tool in group.tools
                        },
                    )
                )

    assert states[0][0] == execution.active_group_names
    assert "execution_actions_tool" in states[0][1]
    assert states[1][0] == planning.active_group_names
    assert "execution_actions_tool" not in states[1][1]
    assert resolver.calls == 2
