from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from vla_data_juicer_agents.web.event_stream import SessionEventBus
from vla_data_juicer_agents.web.schemas import PublicEventRecord
from vla_data_juicer_agents.web.session_store import WebSessionStore


@asynccontextmanager
async def stream_session_events(
    store: WebSessionStore,
    bus: SessionEventBus,
    session_id: str,
    after_sequence: int,
) -> AsyncIterator[AsyncIterator[PublicEventRecord]]:
    async with bus.subscribe(session_id) as queue:
        async def records() -> AsyncIterator[PublicEventRecord]:
            last_sequence = after_sequence
            for record in store.list_public_events(
                session_id,
                after_sequence=last_sequence,
            ):
                last_sequence = record.sequence
                yield record

            while True:
                record = await queue.get()
                if record.sequence > last_sequence:
                    last_sequence = record.sequence
                    yield record

        yield records()


async def iter_sse(
    store: WebSessionStore,
    bus: SessionEventBus,
    session_id: str,
    after_sequence: int,
    *,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[bytes]:
    async with stream_session_events(
        store,
        bus,
        session_id,
        after_sequence,
    ) as records:
        pending_record = asyncio.create_task(
            anext(records),
            name=f"sse-record:{session_id}",
        )
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {pending_record},
                    timeout=heartbeat_seconds,
                )
                if not done:
                    yield b": heartbeat\n\n"
                    continue
                record = pending_record.result()
                pending_record = asyncio.create_task(
                    anext(records),
                    name=f"sse-record:{session_id}",
                )
                yield _encode_data(record)
        finally:
            pending_record.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending_record


def _encode_data(record: PublicEventRecord) -> bytes:
    data = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"data: {data}\n\n".encode("utf-8")
