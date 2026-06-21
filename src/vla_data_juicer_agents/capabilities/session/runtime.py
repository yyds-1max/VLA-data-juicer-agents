from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter, EventScope


_PREVIEW_LIMIT = 240


@dataclass
class SessionState:
    working_dir: str = "./.djx"
    history: list[dict[str, str]] = field(default_factory=list)


class SessionToolRuntime:
    def __init__(self, *, state: SessionState, event_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.state = state
        self._event_emitter = EventEmitter(
            CallbackEventSink(event_callback) if event_callback is not None else (),
        )
        self._turn_lock = threading.RLock()
        self._active_scope: EventScope | None = None
        self._active_cancellation: CancellationContext | None = None

    @property
    def event_emitter(self) -> EventEmitter:
        return self._event_emitter

    @property
    def active_scope(self) -> EventScope | None:
        with self._turn_lock:
            return self._active_scope

    @property
    def active_cancellation(self) -> CancellationContext | None:
        with self._turn_lock:
            return self._active_cancellation

    def begin_turn(self, scope: EventScope, cancellation: CancellationContext) -> None:
        with self._turn_lock:
            self._active_scope = scope
            self._active_cancellation = cancellation

    def end_turn(self) -> None:
        with self._turn_lock:
            self._active_scope = None
            self._active_cancellation = None

    def storage_root(self) -> Path:
        return Path(self.state.working_dir or "./.djx").expanduser()

    def context_payload(self) -> dict[str, Any]:
        return {
            "working_dir": self.state.working_dir,
            "history_length": len(self.state.history),
        }

    @staticmethod
    def _preview(value: Any) -> str:
        if isinstance(value, str):
            text = value
            return text if len(text) <= _PREVIEW_LIMIT else text[: _PREVIEW_LIMIT - 3] + "..."
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
        return text if len(text) <= _PREVIEW_LIMIT else text[: _PREVIEW_LIMIT - 3] + "..."

    async def invoke_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        fn: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        call_id = f"tool_{uuid4().hex[:10]}"
        scope = self.active_scope
        cancellation = self.active_cancellation
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if scope is not None:
            scope.emit(
                "tool_start",
                tool=tool_name,
                call_id=call_id,
                args=self._preview(args),
            )
        try:
            payload = fn()
            if inspect.isawaitable(payload):
                payload = await payload
        except TurnCancelled as exc:
            if scope is not None:
                scope.emit(
                    "tool_end",
                    tool=tool_name,
                    call_id=call_id,
                    ok=False,
                    status="interrupted",
                    error_type=type(exc).__name__,
                    summary=self._preview(str(exc)),
                )
            raise
        except asyncio.CancelledError as exc:
            interrupted = cancellation is not None and cancellation.cancelled
            if scope is not None:
                scope.emit(
                    "tool_end",
                    tool=tool_name,
                    call_id=call_id,
                    ok=False,
                    status="interrupted" if interrupted else "failed",
                    error_type=type(exc).__name__,
                    summary="The current turn was interrupted." if interrupted else "Tool execution was cancelled.",
                )
            raise
        except Exception as exc:
            if scope is not None:
                scope.emit(
                    "tool_end",
                    tool=tool_name,
                    call_id=call_id,
                    ok=False,
                    status="failed",
                    error_type=type(exc).__name__,
                    summary=self._preview(str(exc)),
                )
            raise
        ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
        summary = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        if scope is not None:
            scope.emit(
                "tool_end",
                tool=tool_name,
                call_id=call_id,
                ok=ok,
                status="completed" if ok else "failed",
                result=self._preview(payload),
                summary=self._preview(summary),
            )
        return payload
