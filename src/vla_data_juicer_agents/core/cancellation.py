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
        self._agents_quiescent = threading.Event()
        self._agents_quiescent.set()
        self._workers: set[object] = set()
        self._workers_quiescent = threading.Event()
        self._workers_quiescent.set()

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

    def reserve_worker(self) -> object:
        """Register work whose lifetime can outlive its cancelled asyncio wrapper."""
        token = object()
        with self._lock:
            self._workers.add(token)
            self._workers_quiescent.clear()
        return token

    def finish_worker(self, token: object) -> None:
        """Mark a reserved worker complete from the actual worker's finally block."""
        with self._lock:
            self._workers.discard(token)
            if not self._workers:
                self._workers_quiescent.set()

    async def wait_for_workers(self, *, timeout: float | None = None) -> bool:
        """Wait until every tracked worker has actually returned.

        Cancelling an ``asyncio.to_thread`` awaiter does not stop its thread.  Stop
        acknowledgement therefore uses this worker-owned completion signal rather
        than the lifetime of the asynchronous wrapper.
        """
        return await self._wait_threading_event(
            self._workers_quiescent,
            timeout=timeout,
        )

    async def wait_for_quiescence(self, *, timeout: float | None = None) -> bool:
        """Wait for both tracked asyncio owners and real worker threads to exit."""
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        agents_done = await self._wait_threading_event(
            self._agents_quiescent,
            timeout=timeout,
        )
        if not agents_done:
            return False
        remaining = (
            None
            if deadline is None
            else max(0.0, deadline - asyncio.get_running_loop().time())
        )
        return await self.wait_for_workers(timeout=remaining)

    @staticmethod
    async def _wait_threading_event(
        event: threading.Event,
        *,
        timeout: float | None,
    ) -> bool:
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while not event.is_set():
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)
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
            self._agents_quiescent.clear()
        try:
            self.raise_if_cancelled()
            yield
        finally:
            with self._lock:
                self._agents.pop(token, None)
                if not self._agents:
                    self._agents_quiescent.set()


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
