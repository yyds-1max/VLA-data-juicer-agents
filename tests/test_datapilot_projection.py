from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.event import (
    CustomEvent,
    HintBlockEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultState,
)
from agentscope.tool import Toolkit, ToolResponse

from vla_data_juicer_agents.runtime.datapilot_projection import (
    DataPilotReplyProjectionMiddleware,
    DataPilotToolOutcomeMiddleware,
    classify_real_tool_response,
    sanitize_agent_event,
)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.dedupe_keys: list[str] = []
        self.starts: list[tuple[str, str]] = []
        self.terminals: list[tuple[str, str, str, str | None]] = []
        self.terminal_written = asyncio.Event()

    def projection_private_identities(self) -> set[str]:
        return {
            "MainRouterAgent",
            "main-router-agent",
            "NavigationDataAgent",
            "navigation-data-agent",
        }

    async def project_agent_event(
        self,
        session_id: str,
        *,
        dedupe_key: str,
        event: dict[str, Any],
    ) -> None:
        await asyncio.sleep(0)
        self.events.append(event)
        self.dedupe_keys.append(dedupe_key)

    async def start_public_tool(
        self,
        session_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        self.starts.append((tool_call_id, tool_name))

    async def finish_public_tool(
        self,
        session_id: str,
        *,
        tool_call_id: str,
        status: str,
        summary: str,
        error_type: str | None,
    ) -> None:
        self.terminals.append((tool_call_id, status, summary, error_type))
        self.terminal_written.set()


async def _run_acting_chain(
    middlewares: list[Any],
    agent: Any,
    tool_call: ToolCallBlock,
    terminal_handler,
) -> list[Any]:
    async def execute(index: int, **input_kwargs: Any):
        if index == len(middlewares):
            async for item in terminal_handler(**input_kwargs):
                yield item
            return

        middleware = middlewares[index]

        async def next_handler(**kwargs: Any):
            async for item in execute(index + 1, **kwargs):
                yield item

        async for item in middleware.on_acting(
            agent=agent,
            input_kwargs=input_kwargs,
            next_handler=next_handler,
        ):
            yield item

    return [item async for item in execute(0, tool_call=tool_call)]


class RecordingBackgroundManager:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None

    async def register_task(self, *, asyncio_task: asyncio.Task, **_kwargs: Any) -> str:
        self.task = asyncio_task
        return "background-task-1"


class RecordingMessageBus:
    def __init__(self) -> None:
        self.queued: list[tuple[str, dict[str, Any]]] = []
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def queue_push(self, key: str, payload: dict[str, Any]) -> None:
        self.queued.append((key, payload))

    async def publish(self, key: str, payload: dict[str, Any]) -> None:
        self.published.append((key, payload))


def test_sanitize_agent_event_publicizes_agent_name_without_explicit_identity_set():
    raw = ReplyStartEvent(
        session_id="internal-router-session",
        reply_id="reply-1",
        name="MainRouterAgent",
    ).model_dump(mode="json")

    public = sanitize_agent_event(raw, public_name="DataPilot")

    assert public["name"] == "DataPilot"
    assert "session_id" not in public


@pytest.mark.asyncio
async def test_reply_projection_persists_before_yield_and_hides_internal_identity():
    source_events = [
        ReplyStartEvent(
            session_id="internal-nav-session",
            reply_id="reply-1",
            name="navigation-data-agent",
        ),
        TextBlockDeltaEvent(
            reply_id="reply-1",
            block_id="block-1",
            delta="处理中",
        ),
        ToolCallStartEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            tool_call_name="extract",
        ),
        ReplyEndEvent(
            session_id="internal-nav-session",
            reply_id="reply-1",
        ),
    ]
    sink = RecordingSink()
    middleware = DataPilotReplyProjectionMiddleware("internal-nav-session", sink)

    async def handler(**_kwargs: Any):
        for event in source_events:
            yield event

    projected = middleware.on_reply(SimpleNamespace(), {}, handler)
    first = await anext(projected)

    assert first is source_events[0]
    assert sink.events[0]["name"] == "DataPilot"
    assert sink.events[0]["type"] == "REPLY_START"
    assert sink.events[0]["reply_id"] == "reply-1"
    assert sink.events[0]["created_at"] == source_events[0].created_at
    assert "session_id" not in sink.events[0]
    assert "internal-nav-session" not in json.dumps(sink.events)
    assert "navigation-data-agent" not in json.dumps(sink.events)
    assert sink.dedupe_keys == [
        hashlib.sha256(b"internal-nav-session:reply-1:0").hexdigest()
    ]
    assert "internal-nav-session" not in sink.dedupe_keys[0]

    yielded = [first, *[event async for event in projected]]
    assert yielded == source_events
    assert [event["type"] for event in sink.events] == [
        "REPLY_START",
        "TEXT_BLOCK_DELTA",
        "TOOL_CALL_START",
        "REPLY_END",
    ]
    assert sink.events[1]["block_id"] == "block-1"
    assert sink.events[1]["delta"] == "处理中"
    assert sink.events[2]["tool_call_id"] == "call-1"
    assert sink.events[2]["tool_call_name"] == "extract"
    assert sink.events[3]["finished_reason"] == "completed"
    assert all(len(key) == 64 for key in sink.dedupe_keys)


@pytest.mark.asyncio
async def test_reply_projection_suppresses_every_native_tool_result_event():
    source_events = [
        ReplyStartEvent(
            session_id="internal-nav-session",
            reply_id="reply-1",
            name="navigation-data-agent",
        ),
        ToolResultStartEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            tool_call_name="extract",
        ),
        ToolResultTextDeltaEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            delta="placeholder",
        ),
        ToolResultDataDeltaEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            media_type="image/png",
            data="aW1hZ2U=",
        ),
        ToolResultEndEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            state=ToolResultState.SUCCESS,
        ),
        ReplyEndEvent(
            session_id="internal-nav-session",
            reply_id="reply-1",
        ),
    ]
    sink = RecordingSink()
    middleware = DataPilotReplyProjectionMiddleware("internal-nav-session", sink)

    async def handler(**_kwargs: Any):
        for event in source_events:
            yield event

    yielded = [event async for event in middleware.on_reply(SimpleNamespace(), {}, handler)]

    assert yielded == source_events
    assert [event["type"] for event in sink.events] == ["REPLY_START", "REPLY_END"]


@pytest.mark.asyncio
async def test_reply_projection_skips_final_msg_and_recursively_hides_private_identities():
    private_session = "internal-nav-session"
    custom = CustomEvent(
        name="datapilot_progress",
        metadata={
            "worker_agent_id": "main-router-agent",
            "safe_meta": "keep-meta",
        },
        value={
            "worker_session_id": private_session,
            "nested": {
                "name": "NavigationDataAgent",
                "safe": "keep-value",
            },
            "description": (
                "MainRouterAgent delegated from main-router-agent to "
                f"NavigationDataAgent in {private_session}"
            ),
            "identity_keyed": {"navigation-data-agent": "hidden"},
            "tool_name": "extract_navigation_data",
        },
    )
    final_msg = AssistantMsg(
        name="NavigationDataAgent",
        content=[TextBlock(text="done")],
    )
    source_items = [
        ReplyStartEvent(
            session_id=private_session,
            reply_id="reply-1",
            name="NavigationDataAgent",
        ),
        HintBlockEvent(
            reply_id="reply-1",
            block_id="hint-1",
            source=(
                "NavigationDataAgent/navigation-data-agent/"
                f"{private_session}"
            ),
            hint="continue",
        ),
        custom,
        final_msg,
        ReplyEndEvent(
            session_id=private_session,
            reply_id="reply-1",
        ),
    ]
    sink = RecordingSink()
    middleware = DataPilotReplyProjectionMiddleware(private_session, sink)
    agent = SimpleNamespace(name="NavigationDataAgent")

    async def handler(**_kwargs: Any):
        for item in source_items:
            yield item

    yielded = [item async for item in middleware.on_reply(agent, {}, handler)]

    assert yielded == source_items
    assert [event["type"] for event in sink.events] == [
        "REPLY_START",
        "HINT_BLOCK",
        "CUSTOM",
        "REPLY_END",
    ]
    public_json = json.dumps(sink.events, ensure_ascii=False)
    for private in (
        private_session,
        "MainRouterAgent",
        "main-router-agent",
        "NavigationDataAgent",
        "navigation-data-agent",
    ):
        assert private not in public_json
    assert sink.events[1]["source"] == "DataPilot/DataPilot/DataPilot"
    assert sink.events[2]["name"] == "datapilot_progress"
    assert sink.events[2]["metadata"] == {"safe_meta": "keep-meta"}
    assert sink.events[2]["value"]["nested"] == {
        "name": "DataPilot",
        "safe": "keep-value",
    }
    assert sink.events[2]["value"]["identity_keyed"] == {"DataPilot": "hidden"}
    assert sink.events[2]["value"]["tool_name"] == "extract_navigation_data"
    assert sink.dedupe_keys[-1] == hashlib.sha256(
        f"{private_session}:reply-1:3".encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            ToolResponse(
                content=[TextBlock(text='{"ok":true,"message":"done"}')],
                state=ToolResultState.SUCCESS,
            ),
            ("success", "done", None),
        ),
        (
            ToolResponse(
                content=[
                    TextBlock(
                        text=(
                            '{"ok":false,"message":"bad input",'
                            '"error_type":"invalid_input"}'
                        )
                    )
                ],
                state=ToolResultState.SUCCESS,
            ),
            ("failure", "bad input", "invalid_input"),
        ),
        (
            ToolResponse(
                content=[TextBlock(text="denied")],
                state=ToolResultState.DENIED,
            ),
            ("failure", "denied", "denied"),
        ),
    ],
)
def test_classify_real_tool_response_requires_real_success_without_ok_false(
    response: ToolResponse,
    expected: tuple[str, str, str | None],
) -> None:
    assert classify_real_tool_response(response) == expected


@pytest.mark.asyncio
async def test_tool_outcome_records_exception_as_failure_and_reraises():
    sink = RecordingSink()
    middleware = DataPilotToolOutcomeMiddleware("internal-nav-session", sink)
    tool_call = ToolCallBlock(
        id="call-1",
        name="extract",
        input="{}",
    )

    async def failing_handler(**_kwargs: Any):
        raise RuntimeError("worker exploded")
        yield

    with pytest.raises(RuntimeError, match="worker exploded"):
        _ = [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": tool_call},
                failing_handler,
            )
        ]

    assert sink.starts == [("call-1", "extract")]
    assert sink.terminals == [
        ("call-1", "failure", "worker exploded", "RuntimeError")
    ]


@pytest.mark.parametrize(
    ("response", "expected_terminal"),
    [
        (
            ToolResponse(
                id="call-1",
                content=[TextBlock(text='{"ok":true,"message":"done"}')],
                state=ToolResultState.SUCCESS,
            ),
            ("call-1", "success", "done", None),
        ),
        (
            ToolResponse(
                id="call-1",
                content=[
                    TextBlock(
                        text=(
                            '{"ok":false,"message":"bad input",'
                            '"error_type":"invalid_input"}'
                        )
                    )
                ],
                state=ToolResultState.SUCCESS,
            ),
            ("call-1", "failure", "bad input", "invalid_input"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_tool_outcome_records_real_response_terminal_before_yield(
    response: ToolResponse,
    expected_terminal: tuple[str, str, str, str | None],
) -> None:
    sink = RecordingSink()
    middleware = DataPilotToolOutcomeMiddleware("internal-nav-session", sink)
    tool_call = ToolCallBlock(id="call-1", name="extract", input="{}")

    async def handler(**_kwargs: Any):
        yield response

    stream = middleware.on_acting(
        SimpleNamespace(),
        {"tool_call": tool_call},
        handler,
    )
    yielded = await anext(stream)

    assert yielded is response
    assert sink.starts == [("call-1", "extract")]
    assert sink.terminals == [expected_terminal]
    assert sink.terminals[0][1] != "stopped"


@pytest.mark.asyncio
async def test_tool_outcome_records_non_user_cancellation_as_failure_and_reraises():
    sink = RecordingSink()
    middleware = DataPilotToolOutcomeMiddleware("internal-nav-session", sink)
    tool_call = ToolCallBlock(id="call-1", name="extract", input="{}")

    async def cancelled_handler(**_kwargs: Any):
        raise asyncio.CancelledError("worker cancelled")
        yield

    with pytest.raises(asyncio.CancelledError, match="worker cancelled"):
        _ = [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": tool_call},
                cancelled_handler,
            )
        ]

    assert sink.starts == [("call-1", "extract")]
    assert sink.terminals == [
        ("call-1", "failure", "worker cancelled", "CancelledError")
    ]
    assert all(status != "stopped" for _, status, _, _ in sink.terminals)


@pytest.mark.asyncio
async def test_real_background_ok_false_wins_over_tool_offload_synthetic_success(
    monkeypatch,
):
    sink = RecordingSink()
    background_manager = RecordingBackgroundManager()
    message_bus = RecordingMessageBus()
    created_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def tracked_create_task(coroutine, *args, **kwargs):
        task = real_create_task(coroutine, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", tracked_create_task)
    offload = ToolOffloadMiddleware(
        bg_manager=background_manager,
        message_bus=message_bus,
        user_id="alice",
        agent_id="navigation-data-agent",
        timeout_secs=0.001,
    )
    outcome = DataPilotToolOutcomeMiddleware("internal-nav-session", sink)
    agent = SimpleNamespace(
        name="NavigationDataAgent",
        state=SimpleNamespace(session_id="internal-nav-session"),
        toolkit=Toolkit(),
    )
    tool_call = ToolCallBlock(
        id="call-1",
        name="delayed_extract",
        input="{}",
    )
    release = asyncio.Event()

    async def delayed_failed_tool(**_kwargs: Any):
        await release.wait()
        yield ToolResponse(
            id="call-1",
            content=[
                TextBlock(
                    text=(
                        '{"ok":false,"message":"extract failed",'
                        '"error_type":"extract_sync_failed"}'
                    )
                )
            ],
            state=ToolResultState.SUCCESS,
        )

    items = await _run_acting_chain(
        [offload, outcome],
        agent,
        tool_call,
        delayed_failed_tool,
    )

    assert items[-1].state is ToolResultState.SUCCESS
    assert sink.starts == [("call-1", "delayed_extract")]
    assert sink.terminals == []
    assert background_manager.task is not None

    release.set()
    await asyncio.wait_for(background_manager.task, timeout=1)
    await asyncio.wait_for(sink.terminal_written.wait(), timeout=1)
    await asyncio.gather(*created_tasks)

    assert sink.terminals == [
        ("call-1", "failure", "extract failed", "extract_sync_failed")
    ]
    assert len(created_tasks) == 2
    assert all(task.done() for task in created_tasks)
