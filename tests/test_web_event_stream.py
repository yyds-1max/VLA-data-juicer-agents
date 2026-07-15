import asyncio

import pytest

from vla_data_juicer_agents.web.event_stream import SessionEventBus


def test_event_bus_delivers_events_to_subscriber():
    asyncio.run(_assert_event_bus_delivers_events_to_subscriber())


async def _assert_event_bus_delivers_events_to_subscriber():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        await bus.publish("session_1", {"type": "reasoning", "payload": {"summary": "working"}})
        event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event["type"] == "reasoning"


def test_event_bus_scopes_by_session():
    asyncio.run(_assert_event_bus_scopes_by_session())


async def _assert_event_bus_scopes_by_session():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        await bus.publish("session_2", {"type": "final", "payload": {"text": "wrong"}})

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)


def test_event_bus_isolates_events_between_subscribers():
    asyncio.run(_assert_event_bus_isolates_events_between_subscribers())


async def _assert_event_bus_isolates_events_between_subscribers():
    bus = SessionEventBus()
    original_event = {
        "type": "reasoning",
        "payload": {"steps": [{"summary": "working"}]},
    }

    async with bus.subscribe("session_1") as queue_1:
        async with bus.subscribe("session_1") as queue_2:
            await bus.publish("session_1", original_event)
            event_1 = await asyncio.wait_for(queue_1.get(), timeout=1)
            event_2 = await asyncio.wait_for(queue_2.get(), timeout=1)

    assert event_1 is not event_2

    event_1["payload"]["steps"][0]["summary"] = "mutated"

    assert event_2["payload"]["steps"][0]["summary"] == "working"
    assert original_event["payload"]["steps"][0]["summary"] == "working"


def test_event_bus_unsubscribes_when_context_exits():
    asyncio.run(_assert_event_bus_unsubscribes_when_context_exits())


async def _assert_event_bus_unsubscribes_when_context_exits():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        pass

    assert "session_1" not in bus._subscribers

    await bus.publish("session_1", {"type": "final", "payload": {"text": "done"}})

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.05)
