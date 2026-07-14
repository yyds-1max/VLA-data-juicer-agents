import asyncio
from datetime import UTC, datetime

import pytest

from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import PublicEventRecord


def _record(session_id: str, sequence: int, event: dict) -> PublicEventRecord:
    return PublicEventRecord(
        id=f"event_{sequence}",
        session_id=session_id,
        sequence=sequence,
        dedupe_key=f"{sequence:064x}",
        event=event,
        created_at=datetime.now(UTC).isoformat(),
    )


def test_event_bus_delivers_events_to_subscriber():
    asyncio.run(_assert_event_bus_delivers_events_to_subscriber())


async def _assert_event_bus_delivers_events_to_subscriber():
    bus = SessionEventBus()
    record = _record(
        "session_1",
        1,
        {"type": "reasoning", "payload": {"summary": "working"}},
    )

    async with bus.subscribe("session_1") as queue:
        await bus.publish("session_1", record)
        event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event.event["type"] == "reasoning"


def test_event_bus_scopes_by_session():
    asyncio.run(_assert_event_bus_scopes_by_session())


async def _assert_event_bus_scopes_by_session():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        await bus.publish(
            "session_2",
            _record("session_2", 1, {"type": "final", "payload": {"text": "wrong"}}),
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)


def test_event_bus_isolates_events_between_subscribers():
    asyncio.run(_assert_event_bus_isolates_events_between_subscribers())


async def _assert_event_bus_isolates_events_between_subscribers():
    bus = SessionEventBus()
    original_event = _record("session_1", 1, {
        "type": "reasoning",
        "payload": {"steps": [{"summary": "working"}]},
    })

    async with bus.subscribe("session_1") as queue_1:
        async with bus.subscribe("session_1") as queue_2:
            await bus.publish("session_1", original_event)
            event_1 = await asyncio.wait_for(queue_1.get(), timeout=1)
            event_2 = await asyncio.wait_for(queue_2.get(), timeout=1)

    assert event_1 is not event_2

    event_1.event["payload"]["steps"][0]["summary"] = "mutated"

    assert event_2.event["payload"]["steps"][0]["summary"] == "working"
    assert original_event.event["payload"]["steps"][0]["summary"] == "working"


def test_event_bus_unsubscribes_when_context_exits():
    asyncio.run(_assert_event_bus_unsubscribes_when_context_exits())


async def _assert_event_bus_unsubscribes_when_context_exits():
    bus = SessionEventBus()

    async with bus.subscribe("session_1") as queue:
        pass

    assert "session_1" not in bus._subscribers

    await bus.publish(
        "session_1",
        _record("session_1", 1, {"type": "final", "payload": {"text": "done"}}),
    )

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.05)
