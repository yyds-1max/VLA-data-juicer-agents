"""Normalize AgentScope streaming events for the shared event transport."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from vla_data_juicer_agents.core.events import EventScope


_SENTENCE_RE = re.compile(r".*?[.!?。！？](?:\s+|$)")


def _event_type(event: object) -> str:
    event_type = getattr(event, "type", None)
    if hasattr(event_type, "value"):
        return str(event_type.value)
    if event_type is not None:
        return str(event_type)
    return type(event).__name__


def _text(value: object) -> str:
    return value if isinstance(value, str) else str(value or "")


def summarize_progress(text: object, *, max_chars: int = 240) -> str:
    """Return a compact, display-safe progress summary."""
    normalized = re.sub(
        r"^\s*(?:Thought\s*[:：]|思考\s*[:：])\s*",
        "",
        _text(text),
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""

    sentences = [match.group(0).strip() for match in _SENTENCE_RE.finditer(normalized)]
    consumed = sum(match.end() - match.start() for match in _SENTENCE_RE.finditer(normalized))
    if consumed < len(normalized):
        sentences.append(normalized[consumed:].strip())
    summary = " ".join(sentence for sentence in sentences[:2] if sentence)
    return summary[:max_chars].rstrip()


@dataclass
class _ToolState:
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    result: list[str] = field(default_factory=list)
    started: bool = False


class AgentScopeEventAdapter:
    """Translate AgentScope stream events into transport-neutral events."""

    def __init__(self, scope: EventScope, emit_tool_events: bool = True) -> None:
        self._scope = scope
        self._emit_tool_events = emit_tool_events
        self._thinking: dict[str, list[str]] = {}
        self._tools: dict[str, _ToolState] = {}

    def accept(self, event: object) -> None:
        event_type = _event_type(event)
        block_id = _text(getattr(event, "block_id", ""))
        call_id = _text(getattr(event, "tool_call_id", ""))

        if event_type == "THINKING_BLOCK_DELTA":
            self._thinking.setdefault(block_id, []).append(_text(getattr(event, "delta", "")))
        elif event_type == "THINKING_BLOCK_END":
            summary = summarize_progress("".join(self._thinking.pop(block_id, [])))
            if summary:
                self._scope.emit("reasoning", summary=summary)
        elif event_type == "TOOL_CALL_START":
            state = self._tools.setdefault(call_id, _ToolState())
            state.name = _text(getattr(event, "tool_call_name", "")) or state.name
        elif event_type == "TOOL_CALL_DELTA":
            self._tools.setdefault(call_id, _ToolState()).arguments.append(
                _text(getattr(event, "delta", ""))
            )
        elif event_type == "TOOL_RESULT_START":
            state = self._tools.setdefault(call_id, _ToolState())
            state.name = _text(getattr(event, "tool_call_name", "")) or state.name
            state.started = True
            if self._emit_tool_events:
                self._scope.emit(
                    "tool_start",
                    tool=state.name,
                    call_id=call_id,
                    args="".join(state.arguments),
                )
        elif event_type == "TOOL_RESULT_TEXT_DELTA":
            self._tools.setdefault(call_id, _ToolState()).result.append(
                _text(getattr(event, "delta", ""))
            )
        elif event_type == "TOOL_RESULT_END":
            state = self._tools.pop(call_id, _ToolState())
            if self._emit_tool_events and state.started:
                self._scope.emit(
                    "tool_end",
                    tool=state.name,
                    call_id=call_id,
                    status=self._tool_status(getattr(event, "state", "success")),
                    summary=summarize_progress("".join(state.result)),
                )

    @staticmethod
    def _tool_status(state: object) -> str:
        value = getattr(state, "value", state)
        normalized = _text(value).lower()
        if normalized == "interrupted":
            return "interrupted"
        if normalized in {"success", "completed"}:
            return "completed"
        return "failed"
