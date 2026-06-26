from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter, EventScope


_PREVIEW_LIMIT = 240
_AUTHORIZATION_ASSIGNMENT_PREFIX_PATTERN = re.compile(
    r"\b(authorization(?:[_-]?header)?)\b"
    r"(\s*[=:]\s*)",
    flags=re.IGNORECASE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b(api[_-]?key|token|password|secret|authorization|credential)\b"
    r"(\s*[=:]\s*)"
    r"(?:Bearer(?:\s+|-)[^\s,;]+|\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    flags=re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"\b(Bearer)(?:\s+|-)[^\s,;]+", flags=re.IGNORECASE)


@dataclass
class SessionState:
    working_dir: str = "./.djx"
    history: list[dict[str, str]] = field(default_factory=list)
    pending_workflow_run_dir: str | None = None
    pending_workflow_status: str | None = None
    pending_workflow_input_type: str | None = None


@dataclass(frozen=True)
class TurnContext:
    scope: EventScope
    cancellation: CancellationContext
    owner_id: str
    _closed: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        self._closed.set()


class SessionToolRuntime:
    def __init__(self, *, state: SessionState, event_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.state = state
        self._event_emitter = EventEmitter(
            CallbackEventSink(event_callback) if event_callback is not None else (),
            event_transform=self.redact_event,
        )
        self._turn_lock = threading.RLock()
        self._active_context: TurnContext | None = None
        self._bound_context: ContextVar[TurnContext | None] = ContextVar(
            f"session_turn_{id(self)}",
            default=None,
        )

    @property
    def event_emitter(self) -> EventEmitter:
        return self._event_emitter

    @property
    def active_scope(self) -> EventScope | None:
        context = self.active_context
        return context.scope if context is not None else None

    @property
    def active_cancellation(self) -> CancellationContext | None:
        context = self.active_context
        return context.cancellation if context is not None else None

    @property
    def active_context(self) -> TurnContext | None:
        with self._turn_lock:
            return self._active_context

    def turn_context(self) -> TurnContext | None:
        bound = self._bound_context.get()
        if bound is not None:
            return None if bound.closed else bound
        with self._turn_lock:
            active = self._active_context
            return active if active is not None and not active.closed else None

    def begin_turn(self, scope: EventScope, cancellation: CancellationContext) -> TurnContext:
        context = TurnContext(scope=scope, cancellation=cancellation, owner_id=f"turn_{uuid4().hex}")
        with self._turn_lock:
            self._active_context = context
        self._bound_context.set(context)
        return context

    def end_turn(self, context: TurnContext) -> None:
        context.close()
        bound = self._bound_context.get()
        if bound is not None and bound.owner_id == context.owner_id:
            self._bound_context.set(None)
        with self._turn_lock:
            if self._active_context is not None and self._active_context.owner_id == context.owner_id:
                self._active_context = None

    def emit_event(self, event_type: str, **payload: Any) -> None:
        context = self.turn_context()
        if context is not None:
            context.scope.emit(event_type, **payload)

    def storage_root(self) -> Path:
        return Path(self.state.working_dir or "./.djx").expanduser()

    def context_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "working_dir": self.state.working_dir,
            "history_length": len(self.state.history),
        }
        if self.state.pending_workflow_run_dir:
            payload["pending_workflow"] = {
                "run_dir": self.state.pending_workflow_run_dir,
                "status": self.state.pending_workflow_status,
                "input_type": self.state.pending_workflow_input_type,
            }
        return payload

    @classmethod
    def redact_payload(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            redacted = {}
            for key, item in value.items():
                normalized_key = re.sub(r"[_-]", "", str(key).lower())
                redacted[key] = (
                    "[REDACTED]"
                    if cls._is_sensitive_key(normalized_key)
                    else cls.redact_payload(item)
                )
            return redacted
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls.redact_payload(item) for item in value]
        if isinstance(value, str):
            return cls.redact_text(value)
        return value

    @classmethod
    def redact_event(cls, event: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(event)
        redacted["payload"] = cls.redact_payload(event.get("payload", {}))
        return redacted

    @staticmethod
    def _is_sensitive_key(normalized_key: str) -> bool:
        return (
            "authorization" in normalized_key
            or normalized_key.endswith(
                (
                    "token",
                    "password",
                    "credential",
                    "credentials",
                    "apikey",
                    "privatekey",
                    "passwordhash",
                )
            )
            or normalized_key.endswith("secret")
            or ("secret" in normalized_key and normalized_key.endswith("key"))
        )

    @staticmethod
    def redact_text(value: str) -> str:
        redacted = SessionToolRuntime._redact_authorization_assignments(value)
        redacted = _SECRET_ASSIGNMENT_PATTERN.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            redacted,
        )
        return _BEARER_PATTERN.sub(lambda match: f"{match.group(1)} [REDACTED]", redacted)

    @staticmethod
    def _redact_authorization_assignments(value: str) -> str:
        parts: list[str] = []
        cursor = 0
        while match := _AUTHORIZATION_ASSIGNMENT_PREFIX_PATTERN.search(value, cursor):
            parts.append(value[cursor : match.start()])
            parts.append(f"{match.group(1)}{match.group(2)}[REDACTED]")
            cursor = SessionToolRuntime._authorization_value_end(value, match.end())
        parts.append(value[cursor:])
        return "".join(parts)

    @staticmethod
    def _authorization_value_end(value: str, start: int) -> int:
        quote: str | None = None
        escaped = False
        for index in range(start, len(value)):
            character = value[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character in {";", "\r", "\n"}:
                return index
        return len(value)

    @classmethod
    def _preview(cls, value: Any) -> str:
        if isinstance(value, str):
            text = cls.redact_text(value)
            return text if len(text) <= _PREVIEW_LIMIT else text[: _PREVIEW_LIMIT - 3] + "..."
        try:
            text = json.dumps(cls.redact_payload(value), ensure_ascii=False, default=str)
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
        bound = self._bound_context.get()
        if bound is not None and bound.closed:
            raise TurnCancelled("The originating session turn has ended.")
        context = self.turn_context()
        scope = context.scope if context is not None else None
        cancellation = context.cancellation if context is not None else None
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
                    summary="Tool execution interrupted.",
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
                    summary="Tool execution failed.",
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
