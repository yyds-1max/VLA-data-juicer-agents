from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from vla_data_juicer_agents.web.schemas import PublicEventRecord


class SessionEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[PublicEventRecord]]] = {}

    @asynccontextmanager
    async def subscribe(
        self,
        session_id: str,
    ) -> AsyncIterator[asyncio.Queue[PublicEventRecord]]:
        queue: asyncio.Queue[PublicEventRecord] = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._subscribers.get(session_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(session_id, None)

    async def publish(self, session_id: str, event: PublicEventRecord) -> None:
        for queue in list(self._subscribers.get(session_id, ())):
            await queue.put(event.model_copy(deep=True))
