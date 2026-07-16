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
_ANSWER_MARKER_RE = re.compile(
    r"^\s*(?:Answer|回答)\s*[:：]\s*(?P<text>.*)$",
    flags=re.IGNORECASE | re.DOTALL,
)
_PRIVATE_TRACE_MARKER_RE = re.compile(
    r"^\s*(?:Thought|Observation|Analysis|Action|Final Answer|"
    r"内部思考|原始观察|动作参数|工具参数)\s*[:：]",
    flags=re.IGNORECASE,
)
_PUBLIC_ACTIVITY_FIELDS = ("summary", "observation", "analysis", "action")
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
_UNSAFE_PUBLIC_REPLY_LINE_RE = re.compile(
    r"(?:"
    r"任务\s*ID|task[_ -]?id|plan[_ -]?id|call[_ -]?id|run[_ -]?id|"
    r"\b[a-z][a-z0-9_]*_tool\b|"
    r"^\s*[-*]?\s*[\"']?(?:reply_id|step_id|activity_id|session_id|agent_id|"
    r"tool_call_id|parent_run_id|request_id|origin_key|ledger_state|"
    r"internal_task_id)[\"']?\s*[:：]|"
    r"/(?:media|home|users?|var|tmp|opt|srv)/|[a-z]:\\|"
    r"system\s+prompt|developer\s+message|chain[- ]of[- ]thought|agentscope|"
    r"(?:password|passwd|api[_ -]?key|access[_ -]?token|authorization|"
    r"credential|secret|密码|口令|密钥|令牌)\s*[:：=]|"
    r"bearer\s+[a-z0-9._~+/-]+|\bsk-[a-z0-9_-]{8,}\b|"
    r"根据(?:系统|指导)|我应该|让我(?:向用户|来)|当前状态显示|"
    r"(?:内部|专门的).{0,12}(?:代理|智能体)"
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

_TOOL_PHASES: dict[str, str] = {
    "start_navigation_data_task": "setup",
    "get_navigation_task_context_tool": "setup",
    "list_observation_evidence_tool": "inspection",
    "read_observation_evidence_tool": "inspection",
    "describe_processing_action_tool": "inspection",
    "record_navigation_user_guidance_tool": "planning",
    "submit_extract_sync_plan_tool": "planning",
    "submit_finish_processing_plan_tool": "planning",
    "get_plan_execution_overview_tool": "silent_execution_state",
    "get_current_plan_step_tool": "silent_execution_state",
    "prepare_raw_data_tool": "preparation",
    "extract_and_sync_navigation_data_tool": "extract_sync",
    "assemble_finish_temp_tool": "finish_assembly",
    "run_noobscene_preprocessing_tool": "finish_assembly",
    "request_human_decision": "human_decision",
    "run_initial_annotation_gui_tool": "annotation",
    "run_tracking_tool": "tracking",
    "prepare_gridmap_for_projection_tool": "projection",
    "run_projection_and_trajectory_tool": "projection",
    "validate_navigation_outputs_tool": "verification",
}
_PHASE_START_TEXT: dict[str, str] = {
    "setup": "正在确认任务范围并建立处理上下文。",
    "inspection": "正在核对原始数据、传感器与可用处理条件。",
    "planning": "必要条件正在汇总，接下来生成并校验处理方案。",
    "preparation": "正在准备后续处理所需的原始数据。",
    "extract_sync": "正在提取并同步导航数据，这一步可能需要一些时间。",
    "finish_assembly": "正在整理后续处理所需的中间数据。",
    "human_decision": "继续执行前需要确认会影响处理结果的关键选择。",
    "annotation": "正在生成并检查初始标注结果。",
    "tracking": "正在计算轨迹与跟踪结果。",
    "projection": "正在生成投影与轨迹结果。",
    "verification": "主要处理已经结束，正在核对输出完整性。",
    "generic": "正在执行下一阶段处理并核对结果。",
}
_PHASE_FAILURE_TEXT: dict[str, str] = {
    "planning": "处理方案未通过校验，正在根据反馈调整后重试。",
    "generic": "当前步骤未能完成，正在判断是否需要调整方案。",
}


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
        if payload.get("summary") and not payload.get("analysis"):
            payload = {**payload, "analysis": payload["summary"]}
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


class PublicProgressProjector:
    """Emit compact user-facing progress paragraphs without ReAct labels."""

    def __init__(self, scope: EventScope) -> None:
        self._scope = scope
        self._last_text = ""
        self._phase = ""
        self._phase_completed: dict[str, int] = {}
        self._heartbeat_completed: dict[str, int] = {}
        self._explicit_update_covers_next_start = False
        self._active_calls: dict[str, str] = {}
        self._background_calls: set[str] = set()
        self._terminal_calls: set[str] = set()
        self._failed_phases: set[str] = set()

    def record_public_update(self, payload: dict[str, str]) -> None:
        summary = _public_text(payload.get("summary", ""))
        if not summary:
            summary = " ".join(
                value
                for value in (
                    _public_text(payload.get("observation", "")),
                    _public_text(payload.get("analysis", "")),
                    _public_text(payload.get("action", "")),
                )
                if value
            )
        if not summary or summary == self._last_text:
            return
        self._emit(summary)
        self._explicit_update_covers_next_start = True
        if self._phase:
            self._heartbeat_completed[self._phase] = self._phase_completed.get(
                self._phase,
                0,
            )

    def record_legacy_progress(self, summary: str) -> None:
        self.record_public_update({"summary": summary})

    def tool_started(self, call_id: str, tool_name: str) -> None:
        if not call_id or call_id in self._active_calls or call_id in self._terminal_calls:
            return
        phase = _tool_phase(tool_name)
        self._active_calls[call_id] = phase
        if phase == "silent_execution_state":
            return
        if phase != self._phase:
            self._phase = phase
            self._failed_phases.discard(phase)
            if self._explicit_update_covers_next_start:
                self._explicit_update_covers_next_start = False
            else:
                self._emit(_PHASE_START_TEXT.get(phase, _PHASE_START_TEXT["generic"]))
        elif self._explicit_update_covers_next_start:
            self._explicit_update_covers_next_start = False

    def tool_finished(self, call_id: str, tool_name: str, status: str) -> None:
        if not call_id or call_id in self._terminal_calls:
            return
        phase = self._active_calls.pop(call_id, None) or _tool_phase(tool_name)
        was_background = call_id in self._background_calls
        self._background_calls.discard(call_id)
        self._terminal_calls.add(call_id)
        if phase == "silent_execution_state":
            return
        if was_background and status == "completed":
            self._emit("后台处理已经完成，正在继续核对处理结果。")
            return
        if status == "interrupted":
            self._emit("当前处理已经停止，正在保留已完成的状态。")
            return
        if status == "failed":
            if phase not in self._failed_phases:
                self._failed_phases.add(phase)
                self._emit(_PHASE_FAILURE_TEXT.get(phase, _PHASE_FAILURE_TEXT["generic"]))
            return
        completed = self._phase_completed.get(phase, 0) + 1
        self._phase_completed[phase] = completed
        heartbeat = self._heartbeat_completed.get(phase, 0)
        if phase == self._phase and completed - heartbeat >= 4:
            self._heartbeat_completed[phase] = completed
            self._emit(
                f"已完成 {completed} 项相关检查，正在继续核对并汇总结果。"
            )

    def tool_background(self, call_id: str, tool_name: str) -> None:
        if not call_id or call_id in self._terminal_calls:
            return
        self._active_calls.setdefault(call_id, _tool_phase(tool_name))
        if call_id in self._background_calls:
            return
        self._background_calls.add(call_id)
        self._emit("数据处理仍在后台运行，完成后会自动继续核对结果。")

    def waiting_for_user(self) -> None:
        self._emit("继续执行前需要你确认关键处理选项。")

    def background_resolved(self, call_id: str, tool_name: str, status: str) -> None:
        if call_id in self._active_calls or call_id in self._background_calls:
            self.tool_finished(call_id, tool_name, status)
            return
        if status == "completed":
            self._emit("后台处理已经完成，正在继续核对处理结果。")
        elif status == "interrupted":
            self._emit("后台处理已经停止，正在保留已完成的状态。")
        else:
            self._emit("后台处理未能完成，正在核对失败状态和可恢复步骤。")

    def finish(self, _status: str = "completed") -> None:
        return

    def _emit(self, text: str) -> None:
        safe_text = _public_text(text)
        if not safe_text or safe_text == self._last_text:
            return
        self._last_text = safe_text
        self._scope.emit("progress_update", text=safe_text)


def _tool_presentation(tool_name: str) -> _ToolPresentation:
    for pattern, presentation in _TOOL_PRESENTATIONS:
        if pattern.search(tool_name):
            return presentation
    return _DEFAULT_TOOL_PRESENTATION


def _tool_phase(tool_name: str) -> str:
    normalized = tool_name.strip()
    if normalized in _TOOL_PHASES:
        return _TOOL_PHASES[normalized]
    if normalized.startswith("inspect_navigation_"):
        return "inspection"
    return "generic"


def _public_text(value: object) -> str:
    normalized = re.sub(r"\s+", " ", _text(value)).strip()
    if not normalized or _UNSAFE_PUBLIC_TEXT_RE.search(normalized):
        return ""
    return normalized[:_PUBLIC_TEXT_LIMIT].rstrip()


def sanitize_public_reply(value: object, *, fallback: str = "") -> str:
    """Remove internal/meta lines before reply text enters a public event."""
    lines: list[str] = []
    previous_blank = False
    for raw_line in _text(value).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        if _UNSAFE_PUBLIC_REPLY_LINE_RE.search(stripped):
            continue
        if _PRIVATE_TRACE_MARKER_RE.match(stripped):
            continue
        lines.append(raw_line.rstrip())
        previous_blank = False
    while lines and not lines[-1]:
        lines.pop()
    rendered = "\n".join(lines).strip()
    return rendered or fallback


def _activity_payload(text: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    analysis = payload.get("analysis", payload.get("reasoning", ""))
    values = {
        "summary": payload.get("summary", ""),
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
        emit_progress_events: bool = False,
        public_tool_events: bool = False,
        suppress_pre_tool_text: bool = False,
        activity_title: str = "正在分析并处理请求",
        background_status_resolver: Callable[[str, str], str] | None = None,
        emit_reply_summary_events: bool = False,
        emit_answer_delta_events: bool = False,
    ) -> None:
        self._scope = scope
        self._emit_tool_events = emit_tool_events
        self._emit_text_events = emit_text_events
        self._emit_final_events = emit_final_events
        self._emit_reasoning_events = emit_reasoning_events
        self._emit_reply_summary_events = emit_reply_summary_events
        self._emit_answer_delta_events = emit_answer_delta_events
        self._public_tool_events = public_tool_events
        self._suppress_pre_tool_text = suppress_pre_tool_text
        self._thinking: dict[str, list[str]] = {}
        self._tools: dict[str, _ToolState] = {}
        self._activity_projector = (
            PublicProgressProjector(scope)
            if emit_progress_events
            else PublicActivityProjector(scope, title=activity_title)
            if emit_activity_events
            else None
        )
        self._progress_filter = ProgressSummaryFilter(
            scope,
            activity_projector=self._activity_projector,
            answer_only=emit_answer_delta_events,
        )
        self._reply_text: list[str] = []
        self._current_reply_id = ""
        self._answer_stream_buffer = ""
        self._answer_stream_emitted = False
        self._safe_answer_parts: list[str] = []
        self._background_tools: dict[str, str] = {}
        self._background_status_resolver = background_status_resolver

    def accept(self, event: object) -> None:
        event_type = _event_type(event)
        block_id = _text(getattr(event, "block_id", ""))
        call_id = _text(getattr(event, "tool_call_id", ""))

        if event_type == "REPLY_START":
            self._current_reply_id = _text(getattr(event, "reply_id", ""))
            self._answer_stream_buffer = ""
            self._answer_stream_emitted = False
            self._safe_answer_parts.clear()
        elif event_type == "THINKING_BLOCK_DELTA":
            self._thinking.setdefault(block_id, []).append(_text(getattr(event, "delta", "")))
        elif event_type == "THINKING_BLOCK_END":
            summary = summarize_progress("".join(self._thinking.pop(block_id, [])))
            if summary and self._emit_reasoning_events:
                self._scope.emit("reasoning", summary=summary)
        elif event_type == "TEXT_BLOCK_DELTA":
            self._handle_text_delta(getattr(event, "delta", ""))
        elif event_type == "REPLY_END":
            self._handle_reply_end(_text(getattr(event, "reply_id", "")))
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
            self._retract_answer_stream()
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
        if self._emit_answer_delta_events:
            self._buffer_answer_delta(rendered)
        elif self._emit_text_events:
            self._scope.emit("assistant_delta", delta=rendered)
        self._reply_text.append(rendered)

    def _handle_reply_end(self, reply_id: str = "") -> None:
        if not self._emit_text_events and not self._emit_final_events and self._activity_projector is None:
            return
        self._flush_reply_segment(reply_id=reply_id or self._current_reply_id)
        self._current_reply_id = ""

    def _flush_reply_segment(self, *, reply_id: str = "") -> None:
        if not self._emit_text_events and not self._emit_final_events and self._activity_projector is None:
            return
        rendered = self._progress_filter.flush()
        if rendered:
            if self._emit_answer_delta_events:
                self._buffer_answer_delta(rendered)
            elif self._emit_text_events:
                self._scope.emit("assistant_delta", delta=rendered)
            self._reply_text.append(rendered)
        self._flush_answer_delta_buffer()
        summary_source = (
            "".join(self._safe_answer_parts)
            if self._emit_answer_delta_events
            else self._progress_filter.summary_text() or "".join(self._reply_text)
        )
        summary = sanitize_public_reply(summary_source)
        if (self._emit_final_events or self._emit_reply_summary_events) and summary:
            if self._activity_projector is not None:
                self._activity_projector.finish(
                    "background" if self._background_tools else "completed"
                )
            event_type = "reply_summary" if self._emit_reply_summary_events else "final"
            payload = {"text": summary}
            if reply_id:
                payload["reply_id"] = reply_id
            self._scope.emit(event_type, **payload)
        self._reply_text.clear()
        self._safe_answer_parts.clear()
        self._progress_filter.reset_segment()

    def _discard_reply_segment(self) -> None:
        rendered = self._progress_filter.flush()
        if rendered:
            self._reply_text.append(rendered)
        self._reply_text.clear()
        self._safe_answer_parts.clear()
        self._progress_filter.reset_segment()

    def _buffer_answer_delta(self, rendered: str) -> None:
        """Emit only complete, sanitized public sentences/lines while preserving spacing."""
        self._answer_stream_buffer += rendered
        while True:
            match = re.search(r"[.!?。！？]\s*|\n", self._answer_stream_buffer)
            if match is None:
                return
            end = match.end()
            chunk = self._answer_stream_buffer[:end]
            self._answer_stream_buffer = self._answer_stream_buffer[end:]
            self._emit_safe_answer_chunk(chunk)

    def _flush_answer_delta_buffer(self) -> None:
        if not self._answer_stream_buffer:
            return
        chunk = self._answer_stream_buffer
        self._answer_stream_buffer = ""
        self._emit_safe_answer_chunk(chunk)

    def _emit_safe_answer_chunk(self, chunk: str) -> None:
        if not self._emit_answer_delta_events or not chunk:
            return
        if not chunk.strip():
            if self._answer_stream_emitted:
                payload: dict[str, str] = {"delta": chunk}
                if self._current_reply_id:
                    payload["reply_id"] = self._current_reply_id
                self._scope.emit("answer_delta", **payload)
                self._safe_answer_parts.append(chunk)
            return
        safe = sanitize_public_reply(chunk)
        if not safe:
            return
        leading = chunk[: len(chunk) - len(chunk.lstrip())]
        trailing = chunk[len(chunk.rstrip()) :]
        delta = f"{leading}{safe}{trailing}"
        payload: dict[str, str] = {"delta": delta}
        if self._current_reply_id:
            payload["reply_id"] = self._current_reply_id
        self._scope.emit("answer_delta", **payload)
        self._answer_stream_emitted = True
        self._safe_answer_parts.append(delta)

    def _retract_answer_stream(self) -> None:
        self._answer_stream_buffer = ""
        if (
            not self._emit_answer_delta_events
            or not self._current_reply_id
            or not self._answer_stream_emitted
        ):
            return
        self._scope.emit("answer_reset", reply_id=self._current_reply_id)
        self._answer_stream_emitted = False
        self._safe_answer_parts.clear()

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
        if self._activity_projector is not None:
            resolver = getattr(self._activity_projector, "background_resolved", None)
            if callable(resolver):
                resolver(call_id, tool_name, status)
            else:
                self._activity_projector.tool_finished(call_id, tool_name, status)

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
    """Separate public progress metadata from a safe final-answer segment."""

    def __init__(
        self,
        scope: EventScope,
        *,
        activity_projector: PublicActivityProjector | None = None,
        answer_only: bool = False,
    ) -> None:
        self._scope = scope
        self._activity_projector = activity_projector
        self._answer_only = answer_only
        self._answer_mode = False
        self._buffer = ""
        self._summary_parts: list[str] = []

    def consume_text_delta(self, delta: object) -> str:
        self._buffer += _text(delta)
        output: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            rendered = self._consume_line(line)
            if rendered is not None:
                output.append(rendered + "\n")
                self._summary_parts.append("\n")
        if self._answer_only and not self._answer_mode:
            answer_match = _ANSWER_MARKER_RE.match(self._buffer)
            if answer_match is not None:
                self._summary_parts.clear()
                self._answer_mode = True
                self._buffer = ""
                rendered = self._consume_plain_text(answer_match.group("text"))
                if rendered:
                    output.append(rendered)
        if self._buffer and not self._could_be_marker_line(self._buffer):
            rendered = self._consume_plain_text(self._buffer)
            if rendered is not None:
                output.append(rendered)
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

    def summary_text(self) -> str:
        return "".join(self._summary_parts)

    def reset_segment(self) -> None:
        self._buffer = ""
        self._summary_parts.clear()
        self._answer_mode = False

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
            answer_match = _ANSWER_MARKER_RE.match(line)
            if answer_match is not None:
                if self._answer_only and not self._answer_mode:
                    self._summary_parts.clear()
                self._answer_mode = True
                answer_text = answer_match.group("text")
                return self._consume_plain_text(answer_text)
            return self._consume_plain_text(line)
        summary = summarize_progress(match.group("summary"))
        if summary:
            if self._activity_projector is not None:
                self._activity_projector.record_legacy_progress(summary)
            else:
                self._scope.emit("reasoning", summary=summary)
        return None

    def _consume_plain_text(self, text: str) -> str | None:
        self._summary_parts.append(text)
        if not self._answer_only or self._answer_mode:
            return text
        return None

    @staticmethod
    def _is_progress_line(line: str) -> bool:
        return bool(_PROGRESS_MARKER_RE.match(line) or _ACTIVITY_MARKER_RE.match(line))

    @staticmethod
    def _could_be_marker_line(line: str) -> bool:
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
            "answer:", "answer：", "回答:", "回答：",
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
