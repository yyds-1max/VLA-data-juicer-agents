from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
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
        self._background_operations: set[object] = set()
        self._background_idle_callbacks: list[Callable[[], None]] = []

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

    @property
    def has_background_operations(self) -> bool:
        with self._lock:
            return bool(self._background_operations)

    def begin_background_operation(self) -> object:
        """Retain this cancellation context until external work really stops."""
        token = object()
        with self._lock:
            self._background_operations.add(token)
        try:
            self.raise_if_cancelled()
        except BaseException:
            self.end_background_operation(token)
            raise
        return token

    def end_background_operation(self, token: object) -> None:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            if token not in self._background_operations:
                return
            self._background_operations.remove(token)
            if not self._background_operations:
                callbacks = self._background_idle_callbacks
                self._background_idle_callbacks = []
        for callback in callbacks:
            callback()

    @contextmanager
    def track_background_operation(self) -> Iterator[None]:
        token = self.begin_background_operation()
        try:
            yield
        finally:
            self.end_background_operation(token)

    def when_background_idle(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._background_operations:
                self._background_idle_callbacks.append(callback)
                return
        callback()

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
