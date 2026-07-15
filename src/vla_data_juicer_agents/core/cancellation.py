from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any


_logger = logging.getLogger(__name__)


class TurnCancelled(RuntimeError):
    """Raised when the active user turn has been interrupted."""


class CancellationContext:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._agents: dict[object, tuple[asyncio.Task[Any], asyncio.AbstractEventLoop]] = {}

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TurnCancelled("The current turn was interrupted.")

    def cancel(self) -> bool:
        with self._lock:
            if self.cancelled:
                return False
            self._cancelled.set()
            registrations = tuple(self._agents.values())

        unique_registrations = {id(task): (task, loop) for task, loop in registrations}
        for task, loop in unique_registrations.values():
            if loop.is_closed() or task.done():
                continue
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                _logger.debug("Event loop closed while scheduling task cancellation")
        return True

    @asynccontextmanager
    async def track_agent(self, agent: Any) -> AsyncIterator[None]:
        del agent
        token = object()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Agent tracking requires an active asyncio task.")
        with self._lock:
            self._agents[token] = (task, asyncio.get_running_loop())
        try:
            self.raise_if_cancelled()
            yield
        finally:
            with self._lock:
                self._agents.pop(token, None)


_CURRENT: ContextVar[CancellationContext | None] = ContextVar(
    "vla_turn_cancellation",
    default=None,
)


@contextmanager
def bind_cancellation(cancellation: CancellationContext | None) -> Iterator[None]:
    token = _CURRENT.set(cancellation)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_cancellation() -> CancellationContext | None:
    return _CURRENT.get()
