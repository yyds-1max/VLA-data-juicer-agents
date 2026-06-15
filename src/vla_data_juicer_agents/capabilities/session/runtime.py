from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass
class SessionState:
    working_dir: str = "./.djx"
    history: list[dict[str, str]] = field(default_factory=list)


class SessionToolRuntime:
    def __init__(self, *, state: SessionState, event_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.state = state
        self._event_callback = event_callback

    def storage_root(self) -> Path:
        return Path(self.state.working_dir or "./.djx").expanduser()

    def context_payload(self) -> dict[str, Any]:
        return {
            "working_dir": self.state.working_dir,
            "history_length": len(self.state.history),
        }

    def emit_event(self, event_type: str, **payload: Any) -> None:
        if self._event_callback is None:
            return
        event = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        self._event_callback(event)

    async def invoke_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        fn: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        call_id = f"tool_{uuid4().hex[:10]}"
        self.emit_event("tool_start", tool=tool_name, call_id=call_id, args=args)
        try:
            payload = fn()
            if hasattr(payload, "__await__"):
                payload = await payload
        except Exception as exc:
            self.emit_event("tool_end", tool=tool_name, call_id=call_id, ok=False, error_type=type(exc).__name__, summary=str(exc))
            raise
        ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
        summary = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        self.emit_event("tool_end", tool=tool_name, call_id=call_id, ok=ok, summary=summary)
        return payload

