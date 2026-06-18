from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any


_logger = logging.getLogger(__name__)


def _consume_interrupt_result(future: concurrent.futures.Future[Any]) -> None:
    try:
        error = future.exception()
    except concurrent.futures.CancelledError:
        return
    except Exception:
        _logger.exception("Failed to inspect Agent interrupt result")
        return
    if error is not None:
        _logger.error(
            "Agent interrupt failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


class TurnCancelled(RuntimeError):
    """Raised when the active user turn has been interrupted."""


class CancellationContext:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._agents: dict[object, tuple[Any, asyncio.AbstractEventLoop]] = {}

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

        unique_registrations = {id(agent): (agent, loop) for agent, loop in registrations}
        for agent, loop in unique_registrations.values():
            if not loop.is_closed():
                coroutine = agent.interrupt()
                try:
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                except Exception:
                    coroutine.close()
                    _logger.exception("Failed to schedule Agent interrupt")
                else:
                    future.add_done_callback(_consume_interrupt_result)
        return True

    @asynccontextmanager
    async def track_agent(self, agent: Any) -> AsyncIterator[None]:
        token = object()
        with self._lock:
            self._agents[token] = (agent, asyncio.get_running_loop())
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
