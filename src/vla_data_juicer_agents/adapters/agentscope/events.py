"""Normalize AgentScope streaming events for the shared event transport."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.core.events import EventScope


_SENTENCE_RE = re.compile(r".*?[.!?。！？]")
_PROGRESS_MARKER_RE = re.compile(
    r"^\s*(?:Progress|进度|思考摘要|思考)\s*[:：]\s*(?P<summary>.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
_ACTIVITY_MARKER_RE = re.compile(
    r"^\s*(?:Activity|活动)\s*[:：]\s*(?P<payload>.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
_PRIVATE_TRACE_MARKER_RE = re.compile(
    r"^\s*(?:Thought|Observation|Analysis|Action|Final Answer|"
    r"内部思考|原始观察|动作参数|工具参数)\s*[:：]",
    flags=re.IGNORECASE,
)
_PUBLIC_ACTIVITY_FIELDS = ("observation", "analysis", "action")
_PUBLIC_TEXT_LIMIT = 240
_UNSAFE_PUBLIC_TEXT_RE = re.compile(
    r"(?:"
    r"system\s+prompt|developer\s+message|chain[- ]of[- ]thought|"
    r"tool[_ -]?call|call[_ -]?id|run[_ -]?id|parent[_ -]?run|"
    r"\b[a-z][a-z0-9_]*_tool\b|"
    r"/(?:media|home|users?|var|tmp|opt|srv)/|[a-z]:\\|"
    r"```|<\/?(?:think|tool|system)\b|"
    r"\b[a-z][a-z0-9]*agent\b|agentscope|"
    r"\b(?:api[_-]?key|password|authorization|bearer\s+[a-z0-9._-]+)\b"
    r")",
    flags=re.IGNORECASE,
)
_HUMAN_DECISION_TOOL_NAMES = {
    "request_human_decision",
}


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


@dataclass(frozen=True)
class _ToolPresentation:
    action: str
    analysis: str
    completed_observation: str


_TOOL_PRESENTATIONS: tuple[tuple[re.Pattern[str], _ToolPresentation], ...] = (
    (
        re.compile(r"human.*decision|request.*decision|confirm", re.IGNORECASE),
        _ToolPresentation(
            "等待你确认关键处理选项",
            "继续执行前需要确认会影响处理结果的关键选择。",
            "已收到关键处理选项的确认结果。",
        ),
    ),
    (
        re.compile(r"start.*navigation|handoff|delegate", re.IGNORECASE),
        _ToolPresentation(
            "启动导航数据处理",
            "该请求需要进入导航数据处理流程继续完成。",
            "导航数据处理流程已经启动。",
        ),
    ),
    (
        re.compile(r"inspect|scan|list|profile|status|current|read|get|check", re.IGNORECASE),
        _ToolPresentation(
            "检查当前数据状态",
            "需要先获得最新事实，再决定下一步处理。",
            "已经获得最新的数据检查结果。",
        ),
    ),
    (
        re.compile(r"plan|submit", re.IGNORECASE),
        _ToolPresentation(
            "生成并校验处理方案",
            "需要把已确认的事实整理成可执行且可校验的处理方案。",
            "处理方案已经完成校验。",
        ),
    ),
    (
        re.compile(r"extract|unpack|parse", re.IGNORECASE),
        _ToolPresentation(
            "提取导航数据",
            "需要先提取后续处理依赖的导航数据。",
            "导航数据提取步骤已经结束。",
        ),
    ),
    (
        re.compile(r"sync|align", re.IGNORECASE),
        _ToolPresentation(
            "同步多模态数据",
            "需要按时间关系对齐不同来源的数据。",
            "多模态数据同步步骤已经结束。",
        ),
    ),
    (
        re.compile(r"calibr|camera|parameter|config", re.IGNORECASE),
        _ToolPresentation(
            "核对处理参数",
            "需要确认当前参数满足后续处理条件。",
            "处理参数核对已经结束。",
        ),
    ),
    (
        re.compile(r"annotat|label", re.IGNORECASE),
        _ToolPresentation(
            "生成标注数据",
            "现有数据已经具备进入标注处理的条件。",
            "标注数据生成步骤已经结束。",
        ),
    ),
    (
        re.compile(r"track|trajectory", re.IGNORECASE),
        _ToolPresentation(
            "计算轨迹与跟踪结果",
            "需要根据当前数据计算可用于后续处理的轨迹结果。",
            "轨迹与跟踪计算步骤已经结束。",
        ),
    ),
    (
        re.compile(r"project", re.IGNORECASE),
        _ToolPresentation(
            "生成投影结果",
            "需要将已经对齐的数据转换为目标投影结果。",
            "投影结果生成步骤已经结束。",
        ),
    ),
)
_DEFAULT_TOOL_PRESENTATION = _ToolPresentation(
    "执行下一步处理",
    "根据当前信息，需要执行下一步操作以获得新的环境反馈。",
    "这一步处理已经结束。",
)


class PublicActivityProjector:
    """Project internal ReAct events into a bounded user-facing activity stream."""

    def __init__(self, scope: EventScope, *, title: str = "正在分析并处理请求") -> None:
        self._scope = scope
        self._activity_id = f"activity-{uuid4().hex}"
        self._title = _public_text(title) or "正在分析并处理请求"
        self._sequence = 0
        self._started = False
        self._finished = False
        self._pending_step: dict[str, Any] | None = None
        self._tool_steps: dict[str, dict[str, Any]] = {}

    def record_public_update(self, payload: dict[str, str]) -> None:
        fields = {field: _public_text(payload.get(field, "")) for field in _PUBLIC_ACTIVITY_FIELDS}
        if not any(fields.values()):
            return
        self._sequence += 1
        step = {
            "id": f"step-{self._sequence}",
            "sequence": self._sequence,
            "status": "reasoning",
            **{field: value for field, value in fields.items() if value},
        }
        self._pending_step = step
        self._emit_step(step)

    def record_legacy_progress(self, summary: str) -> None:
        safe_summary = _public_text(summary)
        if safe_summary:
            self.record_public_update({"analysis": safe_summary})

    def tool_started(self, call_id: str, tool_name: str) -> None:
        presentation = _tool_presentation(tool_name)
        step = self._pending_step
        if step is None:
            self._sequence += 1
            step = {
                "id": f"step-{self._sequence}",
                "sequence": self._sequence,
                "status": "acting",
                "analysis": presentation.analysis,
                "action": presentation.action,
            }
        else:
            step = dict(step)
            step["status"] = "acting"
            step.setdefault("analysis", presentation.analysis)
            step.setdefault("action", presentation.action)
        self._pending_step = step
        self._tool_steps[call_id] = step
        self._emit_step(step)

    def tool_finished(self, call_id: str, tool_name: str, status: str) -> None:
        step = self._tool_steps.pop(call_id, None)
        if step is None:
            self.tool_started(call_id, tool_name)
            step = self._tool_steps.pop(call_id)
        presentation = _tool_presentation(tool_name)
        step = dict(step)
        step["status"] = status
        if not step.get("observation"):
            if status == "completed":
                step["observation"] = presentation.completed_observation
            elif status == "interrupted":
                step["observation"] = "这一步已经中断。"
            else:
                step["observation"] = "这一步未能完成，正在判断是否需要调整方案。"
        self._pending_step = None
        self._emit_step(step)

    def tool_background(self, call_id: str, tool_name: str) -> None:
        step = self._tool_steps.pop(call_id, None)
        if step is None:
            self.tool_started(call_id, tool_name)
            step = self._tool_steps.pop(call_id)
        step = dict(step)
        step["status"] = "background"
        step.setdefault("observation", "这一步已转入后台，完成后会继续处理结果。")
        self._pending_step = None
        self._emit_step(step, activity_status="background")

    def waiting_for_user(self) -> None:
        step = self._pending_step
        if step is None:
            self._sequence += 1
            step = {
                "id": f"step-{self._sequence}",
                "sequence": self._sequence,
                "analysis": "继续执行前需要确认会影响处理结果的关键选择。",
                "action": "等待你确认关键处理选项",
            }
        step = dict(step)
        step["status"] = "waiting"
        self._pending_step = step
        self._emit_step(step, activity_status="waiting")

    def finish(self, status: str = "completed") -> None:
        if not self._started or self._finished:
            return
        self._finished = True
        self._scope.emit(
            "activity_delta",
            activity_id=self._activity_id,
            status=status,
        )

    def _emit_step(self, step: dict[str, Any], *, activity_status: str = "running") -> None:
        event_type = "activity_delta" if self._started else "activity_snapshot"
        payload: dict[str, Any] = {
            "activity_id": self._activity_id,
            "title": self._title,
            "status": activity_status,
        }
        if self._started:
            payload["step"] = step
        else:
            payload["steps"] = [step]
            self._started = True
        self._scope.emit(event_type, **payload)


def _tool_presentation(tool_name: str) -> _ToolPresentation:
    for pattern, presentation in _TOOL_PRESENTATIONS:
        if pattern.search(tool_name):
            return presentation
    return _DEFAULT_TOOL_PRESENTATION


def _public_text(value: object) -> str:
    normalized = re.sub(r"\s+", " ", _text(value)).strip()
    if not normalized or _UNSAFE_PUBLIC_TEXT_RE.search(normalized):
        return ""
    return normalized[:_PUBLIC_TEXT_LIMIT].rstrip()


def _activity_payload(text: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    analysis = payload.get("analysis", payload.get("reasoning", ""))
    values = {
        "observation": payload.get("observation", ""),
        "analysis": analysis,
        "action": payload.get("action", payload.get("next_action", "")),
    }
    return {
        key: value
        for key, value in values.items()
        if isinstance(value, str) and _public_text(value)
    }


class AgentScopeEventAdapter:
    """Translate AgentScope stream events into transport-neutral events."""

    def __init__(
        self,
        scope: EventScope,
        emit_tool_events: bool = True,
        emit_text_events: bool = False,
        emit_final_events: bool = False,
        emit_reasoning_events: bool = True,
        emit_activity_events: bool = False,
        public_tool_events: bool = False,
        suppress_pre_tool_text: bool = False,
        activity_title: str = "正在分析并处理请求",
        background_status_resolver: Callable[[str, str], str] | None = None,
    ) -> None:
        self._scope = scope
        self._emit_tool_events = emit_tool_events
        self._emit_text_events = emit_text_events
        self._emit_final_events = emit_final_events
        self._emit_reasoning_events = emit_reasoning_events
        self._public_tool_events = public_tool_events
        self._suppress_pre_tool_text = suppress_pre_tool_text
        self._thinking: dict[str, list[str]] = {}
        self._tools: dict[str, _ToolState] = {}
        self._activity_projector = (
            PublicActivityProjector(scope, title=activity_title)
            if emit_activity_events
            else None
        )
        self._progress_filter = ProgressSummaryFilter(
            scope,
            activity_projector=self._activity_projector,
        )
        self._reply_text: list[str] = []
        self._background_tools: dict[str, str] = {}
        self._background_status_resolver = background_status_resolver

    def accept(self, event: object) -> None:
        event_type = _event_type(event)
        block_id = _text(getattr(event, "block_id", ""))
        call_id = _text(getattr(event, "tool_call_id", ""))

        if event_type == "THINKING_BLOCK_DELTA":
            self._thinking.setdefault(block_id, []).append(_text(getattr(event, "delta", "")))
        elif event_type == "THINKING_BLOCK_END":
            summary = summarize_progress("".join(self._thinking.pop(block_id, [])))
            if summary and self._emit_reasoning_events:
                self._scope.emit("reasoning", summary=summary)
        elif event_type == "TEXT_BLOCK_DELTA":
            self._handle_text_delta(getattr(event, "delta", ""))
        elif event_type == "REPLY_END":
            self._handle_reply_end()
        elif event_type == "TOOL_CALL_START":
            state = self._tools.setdefault(call_id, _ToolState())
            state.name = _text(getattr(event, "tool_call_name", "")) or state.name
            self._emit_tool_start(call_id, state)
        elif event_type == "TOOL_CALL_DELTA":
            self._tools.setdefault(call_id, _ToolState()).arguments.append(
                _text(getattr(event, "delta", ""))
            )
        elif event_type == "TOOL_RESULT_START":
            state = self._tools.setdefault(call_id, _ToolState())
            state.name = _text(getattr(event, "tool_call_name", "")) or state.name
            self._emit_tool_start(call_id, state)
        elif event_type == "TOOL_RESULT_TEXT_DELTA":
            self._tools.setdefault(call_id, _ToolState()).result.append(
                _text(getattr(event, "delta", ""))
            )
        elif event_type == "TOOL_RESULT_END":
            state = self._tools.pop(call_id, _ToolState())
            if state.started:
                result_text = "".join(state.result)
                status = self._tool_status(getattr(event, "state", "success"), result_text)
                payload = {
                    "tool": state.name,
                    "call_id": call_id,
                    "status": status,
                }
                if self._emit_tool_events:
                    if status == "background":
                        self._scope.emit("tool_background", **payload)
                    else:
                        if not self._public_tool_events:
                            payload["summary"] = summarize_progress(result_text)
                            error_type = _result_payload_error_type(result_text)
                            if error_type:
                                payload["error_type"] = error_type
                        self._scope.emit("tool_end", **payload)
                if status == "background":
                    self._background_tools[call_id] = state.name
                    if self._activity_projector is not None:
                        self._activity_projector.tool_background(call_id, state.name)
                elif self._activity_projector is not None:
                    self._activity_projector.tool_finished(
                        call_id,
                        state.name,
                        str(payload["status"]),
                    )
        elif event_type == "HINT_BLOCK":
            self._handle_hint_block(event)
        elif event_type == "REQUIRE_EXTERNAL_EXECUTION":
            self._handle_require_external_execution(event)

    def _emit_tool_start(self, call_id: str, state: _ToolState) -> None:
        if state.started:
            return
        if self._suppress_pre_tool_text:
            self._discard_reply_segment()
        else:
            self._flush_reply_segment()
        state.started = True
        if self._emit_tool_events:
            payload = {"tool": state.name, "call_id": call_id}
            if not self._public_tool_events:
                payload["args"] = "".join(state.arguments)
            self._scope.emit("tool_start", **payload)
        if self._activity_projector is not None:
            self._activity_projector.tool_started(call_id, state.name)

    def close_active_tools(self, status: str) -> None:
        tools = self._tools
        self._tools = {}
        self._thinking.clear()
        for call_id, state in tools.items():
            if state.started:
                payload = {"tool": state.name, "call_id": call_id, "status": status}
                if self._emit_tool_events:
                    if not self._public_tool_events:
                        payload["summary"] = summarize_progress("".join(state.result))
                    self._scope.emit("tool_end", **payload)
                if self._activity_projector is not None:
                    self._activity_projector.tool_finished(call_id, state.name, status)
        if self._activity_projector is not None and status in {"failed", "interrupted"}:
            self._activity_projector.finish(status)

    def _handle_text_delta(self, delta: object) -> None:
        if not self._emit_text_events and not self._emit_final_events and self._activity_projector is None:
            return
        rendered = self._progress_filter.consume_text_delta(delta)
        if not rendered:
            return
        if self._emit_text_events:
            self._scope.emit("assistant_delta", delta=rendered)
        self._reply_text.append(rendered)

    def _handle_reply_end(self) -> None:
        if not self._emit_text_events and not self._emit_final_events and self._activity_projector is None:
            return
        self._flush_reply_segment()

    def _flush_reply_segment(self) -> None:
        if not self._emit_text_events and not self._emit_final_events and self._activity_projector is None:
            return
        rendered = self._progress_filter.flush()
        if rendered:
            if self._emit_text_events:
                self._scope.emit("assistant_delta", delta=rendered)
            self._reply_text.append(rendered)
        if self._emit_final_events and self._reply_text:
            if self._activity_projector is not None:
                self._activity_projector.finish(
                    "background" if self._background_tools else "completed"
                )
            self._scope.emit("final", text="".join(self._reply_text))
        self._reply_text.clear()

    def _discard_reply_segment(self) -> None:
        rendered = self._progress_filter.flush()
        if rendered:
            self._reply_text.append(rendered)
        self._reply_text.clear()

    @staticmethod
    def _tool_status(state: object, result_text: str = "") -> str:
        if _is_background_placeholder(result_text):
            return "background"
        value = getattr(state, "value", state)
        normalized = _text(value).lower()
        if normalized == "interrupted":
            return "interrupted"
        if normalized in {"success", "completed"} and not _result_payload_failed(result_text):
            return "completed"
        return "failed"

    def _handle_hint_block(self, event: object) -> None:
        identity = _background_hint_identity(getattr(event, "source", None))
        if identity is None:
            return
        tool_name, call_id = identity
        status = _background_hint_status(getattr(event, "hint", ""))
        if status is None and self._background_status_resolver is not None:
            status = self._background_status_resolver(tool_name, call_id)
        if status not in {"completed", "failed", "interrupted"}:
            status = "failed"
        self._background_tools.pop(call_id, None)
        if self._emit_tool_events:
            self._scope.emit(
                "tool_end",
                tool=tool_name,
                call_id=call_id,
                status=status,
            )

    def _handle_require_external_execution(self, event: object) -> None:
        reply_id = _text(getattr(event, "reply_id", ""))
        for tool_call in getattr(event, "tool_calls", []) or []:
            tool_name = _text(getattr(tool_call, "name", ""))
            if tool_name not in _HUMAN_DECISION_TOOL_NAMES:
                continue
            tool_input = _external_tool_input(getattr(tool_call, "input", {}))
            payload = _human_decision_payload(tool_name, tool_input)
            if self._activity_projector is not None:
                self._activity_projector.waiting_for_user()
            self._scope.emit(
                "human_decision_required",
                reply_id=reply_id,
                tool_call_id=_text(getattr(tool_call, "id", "")),
                **payload,
            )


class ProgressSummaryFilter:
    """Convert public progress marker text into reasoning events."""

    def __init__(
        self,
        scope: EventScope,
        *,
        activity_projector: PublicActivityProjector | None = None,
    ) -> None:
        self._scope = scope
        self._activity_projector = activity_projector
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
        activity_match = _ACTIVITY_MARKER_RE.match(line)
        if activity_match is not None:
            if self._activity_projector is not None:
                payload = _activity_payload(activity_match.group("payload"))
                if payload:
                    self._activity_projector.record_public_update(payload)
            return None
        if self._activity_projector is not None and _PRIVATE_TRACE_MARKER_RE.match(line):
            return None
        match = _PROGRESS_MARKER_RE.match(line)
        if match is None:
            return line
        summary = summarize_progress(match.group("summary"))
        if summary:
            if self._activity_projector is not None:
                self._activity_projector.record_legacy_progress(summary)
            else:
                self._scope.emit("reasoning", summary=summary)
        return None

    @staticmethod
    def _is_progress_line(line: str) -> bool:
        return bool(_PROGRESS_MARKER_RE.match(line) or _ACTIVITY_MARKER_RE.match(line))

    @staticmethod
    def _could_be_progress_line(line: str) -> bool:
        stripped = line.lstrip()
        if not stripped:
            return True
        markers = (
            "activity:", "activity：", "活动:", "活动：",
            "progress:", "progress：", "进度:", "进度：",
            "思考摘要:", "思考摘要：", "思考:", "思考：",
            "thought:", "thought：", "observation:", "observation：",
            "analysis:", "analysis：", "action:", "action：",
            "final answer:", "final answer：", "内部思考:", "内部思考：",
            "原始观察:", "原始观察：", "动作参数:", "动作参数：",
            "工具参数:", "工具参数：",
        )
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


def _is_background_placeholder(result_text: str) -> bool:
    normalized = result_text.lower()
    return (
        "<system-reminder>" in normalized
        and "running in background" in normalized
        and "you will be notified automatically" in normalized
    ) or (
        "<system-reminder>" in normalized
        and "running in background" in normalized
        and "for over" in normalized
    )


def _background_hint_identity(source: object) -> tuple[str, str] | None:
    if not isinstance(source, str):
        return None
    try:
        payload = json.loads(source)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("label") != "tool_output":
        return None
    sublabel = payload.get("sublabel")
    if not isinstance(sublabel, str) or " · " not in sublabel:
        return None
    tool_name, call_id = sublabel.rsplit(" · ", 1)
    tool_name = tool_name.strip()
    call_id = call_id.strip()
    if not tool_name or not call_id:
        return None
    return tool_name, call_id


def _background_hint_status(hint: object) -> str | None:
    text = _hint_text(hint)
    payload = _embedded_result_payload(text)
    if isinstance(payload, dict):
        if payload.get("ok") is False:
            error_type = payload.get("error_type")
            if not error_type and isinstance(payload.get("details"), dict):
                error_type = payload["details"].get("error_type")
            return "interrupted" if error_type == "turn_cancelled" else "failed"
        if payload.get("ok") is True:
            return "completed"
        status = payload.get("status")
        if status in {"completed", "failed", "interrupted"}:
            return str(status)
    if "has completed with no output" in text.lower():
        return "completed"
    return None


def _hint_text(hint: object) -> str:
    if isinstance(hint, str):
        return hint
    if not isinstance(hint, list):
        return ""
    parts: list[str] = []
    for block in hint:
        if isinstance(block, dict):
            value = block.get("text")
        else:
            value = getattr(block, "text", None)
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _embedded_result_payload(text: str) -> dict[str, Any] | None:
    marker = "Result:\n\n"
    start = text.find(marker)
    if start < 0:
        return None
    candidate = text[start + len(marker) :]
    end = candidate.rfind("</system-notification>")
    if end >= 0:
        candidate = candidate[:end]
    candidate = candidate.strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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


def _human_decision_payload(tool_name: str, tool_input: dict[str, Any]) -> dict[str, str]:
    decision_type = tool_input.get("decision_type")
    if not isinstance(decision_type, str) or not decision_type:
        decision_type = "other"
    request_id = tool_input.get("request_id")
    summary = tool_input.get("summary")
    payload = {
        "decision_type": decision_type,
        "request_id": request_id if isinstance(request_id, str) else "",
        "summary": summary if isinstance(summary, str) else "",
    }
    plan_id = tool_input.get("plan_id")
    step_id = tool_input.get("step_id")
    if isinstance(plan_id, str) and isinstance(step_id, str):
        payload["plan_id"] = plan_id
        payload["step_id"] = step_id
    return payload
