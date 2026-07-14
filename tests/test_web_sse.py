from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import PublicEventRecord
from vla_data_juicer_agents.web.session_store import WebSessionStore
from vla_data_juicer_agents.web.sse import iter_sse, stream_session_events


def _append_event(
    store: WebSessionStore,
    session_id: str,
    identity: str,
    text: str,
) -> PublicEventRecord:
    return store.append_public_event(
        session_id,
        hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        {"type": "assistant_delta", "delta": text},
    )


@pytest.mark.asyncio
async def test_stream_subscribes_before_replay_without_losing_live_event(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("replay race")
    bus = SessionEventBus()
    first_record = _append_event(store, session.id, "first", "first")

    async with stream_session_events(
        store,
        bus,
        session.id,
        after_sequence=0,
    ) as events:
        first = await anext(events)
        second_record = _append_event(store, session.id, "second", "second")
        await bus.publish(session.id, second_record)
        second = await asyncio.wait_for(anext(events), timeout=1)

    assert first.id == first_record.id
    assert second.id == second_record.id
    assert [first.sequence, second.sequence] == [1, 2]


@pytest.mark.asyncio
async def test_stream_deduplicates_replay_live_overlap_by_sequence(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("overlap")
    bus = SessionEventBus()
    first_record = _append_event(store, session.id, "first", "first")

    async with stream_session_events(store, bus, session.id, after_sequence=0) as events:
        first = await anext(events)
        second_record = _append_event(store, session.id, "second", "second")
        await bus.publish(session.id, first_record)
        await bus.publish(session.id, second_record)
        second = await asyncio.wait_for(anext(events), timeout=1)

    assert [first.sequence, second.sequence] == [1, 2]


@pytest.mark.asyncio
async def test_iter_sse_encodes_public_record_as_json_data(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("encode")
    bus = SessionEventBus()
    record = _append_event(store, session.id, "event", "hello")
    stream = iter_sse(store, bus, session.id, after_sequence=0)

    frame = await anext(stream)
    await stream.aclose()

    assert frame.startswith(b"data: ")
    assert frame.endswith(b"\n\n")
    assert json.loads(
        frame.removeprefix(b"data: ").removesuffix(b"\n\n")
    ) == record.model_dump(mode="json")


@pytest.mark.asyncio
async def test_iter_sse_emits_heartbeat_comment_when_idle(tmp_path: Path) -> None:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("heartbeat")
    stream = iter_sse(
        store,
        SessionEventBus(),
        session.id,
        after_sequence=0,
        heartbeat_seconds=0.01,
    )

    frame = await asyncio.wait_for(anext(stream), timeout=1)
    next_frame = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert frame == b": heartbeat\n\n"
    assert next_frame == b": heartbeat\n\n"
