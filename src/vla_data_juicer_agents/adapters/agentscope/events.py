"""Normalize AgentScope streaming events for the shared event transport."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from vla_data_juicer_agents.core.events import EventScope


_SENTENCE_RE = re.compile(r".*?[.!?。！？]")
_PROGRESS_MARKER_RE = re.compile(
    r"^\s*(?:Progress|进度|思考摘要|思考)\s*[:：]\s*(?P<summary>.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


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

    matches = list(_SENTENCE_RE.finditer(normalized))
    sentences = [match.group(0).strip() for match in matches]
    consumed = matches[-1].end() if matches else 0
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

    def __init__(
        self,
        scope: EventScope,
        emit_tool_events: bool = True,
        emit_text_events: bool = False,
        emit_final_events: bool = False,
    ) -> None:
        self._scope = scope
        self._emit_tool_events = emit_tool_events
        self._emit_text_events = emit_text_events
        self._emit_final_events = emit_final_events
        self._thinking: dict[str, list[str]] = {}
        self._tools: dict[str, _ToolState] = {}
        self._progress_filter = ProgressSummaryFilter(scope)
        self._reply_text: list[str] = []

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
        elif event_type == "TEXT_BLOCK_DELTA":
            self._handle_text_delta(getattr(event, "delta", ""))
        elif event_type == "REPLY_END":
            self._handle_reply_end()
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
                result_text = "".join(state.result)
                payload = {
                    "tool": state.name,
                    "call_id": call_id,
                    "status": self._tool_status(getattr(event, "state", "success"), result_text),
                    "summary": summarize_progress(result_text),
                }
                error_type = _result_payload_error_type(result_text)
                if error_type:
                    payload["error_type"] = error_type
                self._scope.emit("tool_end", **payload)
        elif event_type == "REQUIRE_EXTERNAL_EXECUTION":
            self._handle_require_external_execution(event)

    def close_active_tools(self, status: str) -> None:
        tools = self._tools
        self._tools = {}
        self._thinking.clear()
        if not self._emit_tool_events:
            return
        for call_id, state in tools.items():
            if state.started:
                self._scope.emit(
                    "tool_end",
                    tool=state.name,
                    call_id=call_id,
                    status=status,
                    summary=summarize_progress("".join(state.result)),
                )

    def _handle_text_delta(self, delta: object) -> None:
        if not self._emit_text_events and not self._emit_final_events:
            return
        rendered = self._progress_filter.consume_text_delta(delta)
        if not rendered:
            return
        if self._emit_text_events:
            self._scope.emit("assistant_delta", delta=rendered)
        self._reply_text.append(rendered)

    def _handle_reply_end(self) -> None:
        if not self._emit_text_events and not self._emit_final_events:
            return
        rendered = self._progress_filter.flush()
        if rendered:
            if self._emit_text_events:
                self._scope.emit("assistant_delta", delta=rendered)
            self._reply_text.append(rendered)
        if self._emit_final_events:
            self._scope.emit("final", text="".join(self._reply_text))
        self._reply_text.clear()

    @staticmethod
    def _tool_status(state: object, result_text: str = "") -> str:
        value = getattr(state, "value", state)
        normalized = _text(value).lower()
        if normalized == "interrupted":
            return "interrupted"
        if normalized in {"success", "completed"} and not _result_payload_failed(result_text):
            return "completed"
        return "failed"

    def _handle_require_external_execution(self, event: object) -> None:
        reply_id = _text(getattr(event, "reply_id", ""))
        for tool_call in getattr(event, "tool_calls", []) or []:
            if _text(getattr(tool_call, "name", "")) != "request_human_decision":
                continue
            tool_input = _external_tool_input(getattr(tool_call, "input", {}))
            self._scope.emit(
                "human_decision_required",
                reply_id=reply_id,
                tool_call_id=_text(getattr(tool_call, "id", "")),
                decision_type=_text(tool_input.get("decision_type")) or "other",
                request_id=_text(tool_input.get("request_id")),
                summary=_text(tool_input.get("summary")),
            )


class ProgressSummaryFilter:
    """Convert public progress marker text into reasoning events."""

    def __init__(self, scope: EventScope) -> None:
        self._scope = scope
        self._buffer = ""

    def consume_text_delta(self, delta: object) -> str:
        self._buffer += _text(delta)
        output: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            rendered = self._consume_line(line)
            if rendered is not None:
                output.append(rendered + "\n")
        if self._buffer and not self._could_be_progress_line(self._buffer):
            output.append(self._buffer)
            self._buffer = ""
        return "".join(output)

    def flush_progress_only(self) -> None:
        if self._is_progress_line(self._buffer):
            self._consume_line(self._buffer)
            self._buffer = ""

    def flush(self) -> str:
        buffered = self._buffer
        self._buffer = ""
        rendered = self._consume_line(buffered)
        return "" if rendered is None else rendered

    def _consume_line(self, line: str) -> str | None:
        match = _PROGRESS_MARKER_RE.match(line)
        if match is None:
            return line
        summary = summarize_progress(match.group("summary"))
        if summary:
            self._scope.emit("reasoning", summary=summary)
        return None

    @staticmethod
    def _is_progress_line(line: str) -> bool:
        return bool(_PROGRESS_MARKER_RE.match(line))

    @staticmethod
    def _could_be_progress_line(line: str) -> bool:
        stripped = line.lstrip()
        if not stripped:
            return True
        markers = ("progress:", "progress：", "进度:", "进度：", "思考摘要:", "思考摘要：", "思考:", "思考：")
        normalized = stripped.lower()
        return any(marker.startswith(normalized) or normalized.startswith(marker) for marker in markers)


def _result_payload_failed(result_text: str) -> bool:
    text = result_text.strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("ok") is False


def _result_payload_error_type(result_text: str) -> str:
    text = result_text.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error_type = payload.get("error_type")
    if not error_type and isinstance(payload.get("details"), dict):
        error_type = payload["details"].get("error_type")
    return error_type.strip() if isinstance(error_type, str) else ""


def _external_tool_input(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}
