"""Contract-v1 signals and safe user-facing event projection for DataPilot.

This module deliberately has no dependency on AgentScope event objects.  Agent
adapters may translate their private stream into :class:`SpecialistSignalV1`,
while the web runtime only consumes the bounded :class:`PublicEventV1` values
returned by :class:`SpecialistSignalProjector`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


PUBLIC_CONTRACT_VERSION = 1
PROGRESS_TARGET_CHARS = 120
PUBLIC_TEXT_HARD_LIMIT = 240
PROGRESS_UPDATES_PER_PHASE = 2
PROGRESS_UPDATES_PER_TURN = 8
PROGRESS_COALESCE_SECONDS = 2.0

TaskStatus: TypeAlias = Literal[
    "active",
    "waiting_user",
    "pausing",
    "paused",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
    "needs_replan",
    "superseded",
]
PublicPhase: TypeAlias = Literal[
    "setup",
    "inspection",
    "planning",
    "preparation",
    "extract_sync",
    "finish_assembly",
    "human_decision",
    "annotation",
    "tracking",
    "projection",
    "verification",
    "generic",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _SignalBase(_StrictModel):
    version: Literal[1]
    signal_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=0)
    web_session_id: str = Field(min_length=1, max_length=512)
    turn_id: str = Field(min_length=1, max_length=512)
    task_id: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=512)


class ProgressSignalV1(_SignalBase):
    kind: Literal["progress"]
    operation: Literal["start", "append", "finish"]
    phase: PublicPhase
    text: str | None = Field(default=None, max_length=8_000)
    status: Literal["running", "completed", "failed", "interrupted"] = "running"
    done: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_count_and_operation(self) -> "ProgressSignalV1":
        if (self.done is None) != (self.total is None):
            raise ValueError("done and total must be provided together")
        if self.done is not None and self.total is not None and self.done > self.total:
            raise ValueError("done must not exceed total")
        if self.unit is not None and self.total is None:
            raise ValueError("unit requires done and total")
        if self.operation != "finish" and self.status != "running":
            raise ValueError("only a finish progress signal may use a terminal status")
        return self


class ActionSignalV1(_SignalBase):
    kind: Literal["action"]
    operation: Literal["start", "finish"]
    tool_name: str = Field(min_length=1, max_length=200)
    call_id: str = Field(min_length=1, max_length=512)
    status: Literal["running", "background", "completed", "failed", "interrupted"] = "running"
    message: str | None = Field(default=None, max_length=8_000)
    done: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_action(self) -> "ActionSignalV1":
        if (self.done is None) != (self.total is None):
            raise ValueError("done and total must be provided together")
        if self.done is not None and self.total is not None and self.done > self.total:
            raise ValueError("done must not exceed total")
        if self.unit is not None and self.total is None:
            raise ValueError("unit requires done and total")
        if self.operation == "start" and self.status != "running":
            raise ValueError("an action start must have running status")
        if self.operation == "finish" and self.status == "running":
            raise ValueError("an action finish must have a terminal status")
        return self


class InteractionOptionV1(_StrictModel):
    option_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    destructive: bool = False


class RequestInputSignalV1(_SignalBase):
    kind: Literal["request_input"]
    interaction_ref: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    interaction_kind: Literal[
        "high_risk_confirmation",
        "single_select",
        "multi_select",
        "calibration_preview",
    ]
    blocking: bool = True
    risk: Literal["low", "medium", "high"]
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2_000)
    options: tuple[InteractionOptionV1, ...] = Field(min_length=1, max_length=20)
    interaction_revision: int = Field(ge=1)
    expected_task_revision: int = Field(ge=0)
    expires_at: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_options(self) -> "RequestInputSignalV1":
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("option_id values must be unique")
        if self.risk == "high" and not self.blocking:
            raise ValueError("a high-risk interaction must be blocking")
        return self


class FinalSignalV1(_SignalBase):
    kind: Literal["final"]
    text: str = Field(max_length=40_000)
    task_status: TaskStatus


class ArtifactSignalV1(_SignalBase):
    kind: Literal["artifact"]
    artifact_ref: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=300)
    media_type: str | None = Field(default=None, max_length=120)
    internal_path: str | None = Field(default=None, max_length=8_000)


class InternalSignalV1(_SignalBase):
    kind: Literal["internal"]
    category: str = Field(min_length=1, max_length=120)
    detail: dict[str, Any] = Field(default_factory=dict)


SpecialistSignalV1: TypeAlias = Annotated[
    ProgressSignalV1
    | ActionSignalV1
    | RequestInputSignalV1
    | FinalSignalV1
    | ArtifactSignalV1
    | InternalSignalV1,
    Field(discriminator="kind"),
]
_SIGNAL_ADAPTER = TypeAdapter(SpecialistSignalV1)


def validate_specialist_signal(value: object) -> SpecialistSignalV1:
    """Strictly parse an untrusted adapter payload as contract v1."""
    return _SIGNAL_ADAPTER.validate_python(value)


class ActionVisibility(StrEnum):
    SILENT = "silent"
    GROUPED = "grouped"
    VISIBLE = "visible"


@dataclass(frozen=True, slots=True)
class PublicActionDefinition:
    internal_tool: str
    action_code: str
    display_name: str
    visibility: ActionVisibility
    group: str
    phase: PublicPhase


_ACTION_DEFINITIONS = (
    # Router orchestration is useful as a single visible product action.
    PublicActionDefinition("start_navigation_data_task", "start_navigation", "启动导航数据处理", ActionVisibility.VISIBLE, "navigation_task", "setup"),
    PublicActionDefinition("continue_navigation_data_task", "continue_navigation", "继续导航数据处理", ActionVisibility.VISIBLE, "navigation_task", "setup"),
    PublicActionDefinition("control_navigation_data_task", "control_navigation", "更新导航任务状态", ActionVisibility.VISIBLE, "navigation_task", "setup"),
    # Repeated reads are grouped into one semantic inspection action.
    PublicActionDefinition("inspect_navigation_raw_metadata_tool", "inspect_data", "检查当前数据状态", ActionVisibility.GROUPED, "data_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_sensor_candidates_tool", "inspect_data", "检查当前数据状态", ActionVisibility.GROUPED, "data_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_topic_candidates_tool", "inspect_data", "检查当前数据状态", ActionVisibility.GROUPED, "data_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_artifact_state_tool", "inspect_artifacts", "核对已有处理结果", ActionVisibility.GROUPED, "artifact_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_gridmap_artifacts_tool", "inspect_artifacts", "核对已有处理结果", ActionVisibility.GROUPED, "artifact_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_runtime_assets_tool", "inspect_environment", "检查处理环境", ActionVisibility.GROUPED, "environment_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_calibration_inventory_tool", "inspect_parameters", "核对标定参数", ActionVisibility.GROUPED, "parameter_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_localization_sources_tool", "inspect_localization", "核对定位数据", ActionVisibility.GROUPED, "localization_inspection", "inspection"),
    PublicActionDefinition("inspect_navigation_annotation_job_facts_tool", "inspect_annotation_state", "核对标注任务状态", ActionVisibility.GROUPED, "annotation_inspection", "inspection"),
    PublicActionDefinition("list_observation_evidence_tool", "read_evidence", "汇总检查依据", ActionVisibility.GROUPED, "evidence_read", "inspection"),
    PublicActionDefinition("read_observation_evidence_tool", "read_evidence", "汇总检查依据", ActionVisibility.GROUPED, "evidence_read", "inspection"),
    PublicActionDefinition("describe_processing_action_tool", "review_capability", "核对处理条件", ActionVisibility.GROUPED, "capability_review", "inspection"),
    PublicActionDefinition("record_navigation_user_guidance_tool", "record_preferences", "记录处理要求", ActionVisibility.GROUPED, "planning", "planning"),
    PublicActionDefinition("complete_navigation_task_tool", "complete_task", "完成导航数据任务", ActionVisibility.SILENT, "task_state", "verification"),
    PublicActionDefinition("submit_extract_sync_plan_tool", "prepare_plan", "生成并校验处理方案", ActionVisibility.VISIBLE, "planning", "planning"),
    PublicActionDefinition("submit_finish_processing_plan_tool", "prepare_plan", "生成并校验处理方案", ActionVisibility.VISIBLE, "planning", "planning"),
    PublicActionDefinition("submit_trajectory_review_plan_tool", "prepare_review_plan", "生成并校验轨迹复核方案", ActionVisibility.VISIBLE, "planning", "planning"),
    PublicActionDefinition("prepare_raw_data_tool", "prepare_data", "准备原始数据", ActionVisibility.VISIBLE, "preparation", "preparation"),
    PublicActionDefinition("extract_and_sync_navigation_data_tool", "extract_sync", "提取并同步导航数据", ActionVisibility.VISIBLE, "extract_sync", "extract_sync"),
    PublicActionDefinition("assemble_finish_temp_tool", "assemble_data", "整理中间数据", ActionVisibility.VISIBLE, "finish_assembly", "finish_assembly"),
    PublicActionDefinition("run_noobscene_preprocessing_tool", "preprocess_data", "预处理导航数据", ActionVisibility.VISIBLE, "finish_assembly", "finish_assembly"),
    PublicActionDefinition("confirm_navigation_calibration_params_tool", "confirm_parameters", "确认标定参数", ActionVisibility.VISIBLE, "human_decision", "human_decision"),
    PublicActionDefinition("request_human_decision", "confirm_parameters", "确认关键处理选项", ActionVisibility.VISIBLE, "human_decision", "human_decision"),
    PublicActionDefinition("run_initial_annotation_gui_tool", "annotate_data", "生成初始标注", ActionVisibility.VISIBLE, "annotation", "annotation"),
    PublicActionDefinition("run_tracking_tool", "track_trajectory", "计算轨迹与跟踪结果", ActionVisibility.VISIBLE, "tracking", "tracking"),
    PublicActionDefinition("run_annotation_tracking_workflow_tool", "annotation_workbench", "等待首帧标注并继续跟踪", ActionVisibility.VISIBLE, "annotation", "annotation"),
    PublicActionDefinition("prepare_gridmap_for_projection_tool", "prepare_projection", "准备投影数据", ActionVisibility.VISIBLE, "projection", "projection"),
    PublicActionDefinition("run_projection_and_trajectory_tool", "project_trajectory", "生成投影与轨迹结果", ActionVisibility.VISIBLE, "projection", "projection"),
    PublicActionDefinition("run_annotation_postprocessing_workflow_tool", "postprocess_trajectory", "执行后处理并生成轨迹", ActionVisibility.VISIBLE, "projection", "projection"),
    PublicActionDefinition("validate_navigation_outputs_tool", "verify_outputs", "核对输出完整性", ActionVisibility.VISIBLE, "verification", "verification"),
    PublicActionDefinition("open_trajectory_fix_workbench_tool", "open_fix_workbench", "进入轨迹修正工作台", ActionVisibility.VISIBLE, "human_decision", "human_decision"),
    PublicActionDefinition("validate_trajectory_review_outcome_tool", "verify_review_outcome", "核对轨迹复核结果", ActionVisibility.VISIBLE, "verification", "verification"),
    # State reads are internal mechanics and never need their own public row.
    PublicActionDefinition("get_navigation_task_context_tool", "read_task_state", "读取任务状态", ActionVisibility.SILENT, "execution_state", "setup"),
    PublicActionDefinition("get_plan_execution_overview_tool", "read_execution_state", "读取执行状态", ActionVisibility.SILENT, "execution_state", "generic"),
    PublicActionDefinition("get_current_plan_step_tool", "read_execution_state", "读取执行状态", ActionVisibility.SILENT, "execution_state", "generic"),
)

_UNKNOWN_ACTION = PublicActionDefinition(
    internal_tool="<unknown>",
    action_code="processing_step",
    display_name="执行处理步骤",
    visibility=ActionVisibility.GROUPED,
    group="generic_processing",
    phase="generic",
)


class PublicActionRegistry:
    """Exact allowlist from internal tool identities to semantic public actions."""

    def __init__(self, definitions: tuple[PublicActionDefinition, ...] = _ACTION_DEFINITIONS) -> None:
        mapping: dict[str, PublicActionDefinition] = {}
        for definition in definitions:
            if definition.internal_tool in mapping:
                raise ValueError(f"duplicate public action mapping: {definition.internal_tool}")
            mapping[definition.internal_tool] = definition
        self._mapping = mapping

    def resolve(self, internal_tool: str) -> PublicActionDefinition:
        # Never echo an unknown tool name into the fallback definition.
        return self._mapping.get(internal_tool, _UNKNOWN_ACTION)

    def is_known(self, internal_tool: str) -> bool:
        return internal_tool in self._mapping


PUBLIC_EVENT_TYPES = frozenset(
    {
        "turn_start",
        "turn_state",
        "progress_start",
        "progress_delta",
        "progress_end",
        "action_start",
        "action_end",
        "interaction_required",
        "interaction_resolved",
        "task_state_updated",
        "artifact_ready",
        "answer_delta",
        "answer_reset",
        "final",
        "warning",
        "error",
    }
)
PublicEventType: TypeAlias = Literal[
    "turn_start", "turn_state", "progress_start", "progress_delta", "progress_end",
    "action_start", "action_end", "interaction_required", "interaction_resolved",
    "task_state_updated", "artifact_ready", "answer_delta", "answer_reset", "final",
    "warning", "error",
]
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "source", "tool", "tool_name", "call_id", "tool_call_id", "run_id",
        "parent_run_id", "agent", "agent_id", "task_id", "internal_task_id",
        "web_session_id", "session_id", "plan_id", "step_id", "reply_id",
    }
)


class PublicEventV1(_StrictModel):
    version: Literal[1] = 1
    type: PublicEventType
    task_ref: str | None = Field(default=None, min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_must_not_expose_internal_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        def check(item: object) -> None:
            if isinstance(item, dict):
                forbidden = _FORBIDDEN_PUBLIC_KEYS.intersection(item)
                if forbidden:
                    raise ValueError(f"public payload contains forbidden keys: {sorted(forbidden)}")
                for child in item.values():
                    check(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    check(child)
            elif isinstance(item, str) and _contains_unsafe_public_text(item):
                raise ValueError("public payload contains unsafe text")

        check(value)
        return value


_UNIX_PATH_RE = re.compile(
    r"(?<![\w:])/(?:[^/\s,;，；。！？]+/)*[^/\s,;，；。！？]+",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\s,;，；。！？]+\\?)+")
_UNC_PATH_RE = re.compile(r"\\\\[^\s\\,;，；。！？]+\\[^\s,;，；。！？]+")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{6,}", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_ -]?key|access[_ -]?token|password|passwd|secret|credential)\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)
_CN_SECRET_RE = re.compile(r"(?:密码|口令|密钥|令牌)\s*(?:[:：=]|是|为)\s*[^\s,;，；]+")
_INTERNAL_ID_RE = re.compile(
    r"\b(?:task|plan|step|reply|session|agent|activity|request|origin|call|run|tool_call|parent_run)[_ -]?id\s*[:=：]?\s*[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_INTERNAL_REF_RE = re.compile(
    r"\b(?:nav_plan_[0-9a-f]{24,64}|"
    r"[a-z][a-z0-9_]*(?:step|phase)_[a-z0-9_-]+)\b",
    re.IGNORECASE,
)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{24,64}\b", re.IGNORECASE)
_TOOL_NAME_RE = re.compile(
    r"\b(?:[a-z][a-z0-9_]{2,}_tool|"
    r"(?:start|continue|control)_navigation_data_task|"
    r"run_annotation_(?:tracking|postprocessing)_workflow)\b",
    re.IGNORECASE,
)
_AGENT_NAME_RE = re.compile(r"\b(?:MainRouter|NavigationDataAgent|[A-Za-z][A-Za-z0-9]*Agent|AgentScope)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(
    r"(?<!\w)\d+(?:\.\d+)?\s*(?:%|percent\b)|"
    r"百分之[零〇一二三四五六七八九十百两\d.]+|%",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[^。！？.!?]*(?:[。！？.!?]+|$)")


def _contains_unsafe_public_text(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in (
            _UNIX_PATH_RE, _WINDOWS_PATH_RE, _UNC_PATH_RE, _BEARER_RE, _SECRET_RE,
            _CN_SECRET_RE, _INTERNAL_ID_RE, _INTERNAL_REF_RE, _TOOL_NAME_RE,
            _AGENT_NAME_RE,
            _UUID_RE, _LONG_HEX_RE, _PERCENT_RE,
        )
    )


def _redact_text(text: object) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    replacements = (
        (_UNIX_PATH_RE, "[已隐藏路径]"),
        (_WINDOWS_PATH_RE, "[已隐藏路径]"),
        (_UNC_PATH_RE, "[已隐藏路径]"),
        (_BEARER_RE, "[已隐藏凭据]"),
        (_SECRET_RE, "[已隐藏凭据]"),
        (_CN_SECRET_RE, "[已隐藏凭据]"),
        (_INTERNAL_ID_RE, "[已隐藏内部标识]"),
        (_INTERNAL_REF_RE, "[已隐藏内部标识]"),
        (_UUID_RE, "[已隐藏内部标识]"),
        (_LONG_HEX_RE, "[已隐藏内部标识]"),
        (_TOOL_NAME_RE, "[已隐藏内部操作]"),
        (_AGENT_NAME_RE, "DataPilot"),
        (_PERCENT_RE, ""),
    )
    for pattern, replacement in replacements:
        value = pattern.sub(replacement, value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;，。；！？!?])", r"\1", value)
    return value.strip(" ,;，；")


def sanitize_progress_text(text: object) -> str:
    """Redact progress text, preserve at most two sentences, and bound it."""
    value = _redact_text(text)
    sentences = [match.group(0).strip() for match in _SENTENCE_RE.finditer(value) if match.group(0).strip()]
    selected = sentences[:2]
    if not selected:
        return ""
    # Prefer a complete sentence at the 120-character target. A single longer
    # sentence remains useful, but is always cut at the 240-character hard cap.
    compact: list[str] = []
    for sentence in selected:
        candidate = " ".join((*compact, sentence))
        if compact and len(candidate) > PROGRESS_TARGET_CHARS:
            break
        compact.append(sentence)
    value = " ".join(compact or selected[:1])
    if len(value) > PUBLIC_TEXT_HARD_LIMIT:
        value = value[: PUBLIC_TEXT_HARD_LIMIT - 1].rstrip() + "…"
    return value


def sanitize_final_text(text: object) -> str:
    """Redact final output line-by-line and remove empty/unsafe remnants."""
    safe_lines: list[str] = []
    for line in str(text or "").splitlines():
        safe = _redact_text(line)
        if safe and not _contains_unsafe_public_text(safe):
            safe_lines.append(safe)
    return "\n".join(safe_lines).strip()


_FINAL_FALLBACKS: dict[str, str] = {
    "completed": "任务已完成，你可以查看最新结果。",
    "failed": "任务未能完成。请检查任务状态后重试。",
    "waiting_user": "继续处理前需要你的补充或确认。",
    "needs_replan": "当前处理方案需要调整后才能继续。",
    "paused": "任务已暂停，需要时可以告诉我继续。",
    "pausing": "任务正在安全暂停，稍后可以继续。",
    "cancelling": "任务正在安全取消。",
    "cancelled": "任务已取消。",
    "superseded": "该任务已由新的处理请求替代。",
    "active": "任务仍在处理中，后续进展会继续更新。",
}


def final_fallback(task_status: TaskStatus) -> str:
    return _FINAL_FALLBACKS[task_status]


def _count_payload(done: int | None, total: int | None, unit: str | None) -> dict[str, Any] | None:
    if done is None or total is None:
        return None
    safe_unit = sanitize_progress_text(unit or "项") or "项"
    return {"done": done, "total": total, "unit": safe_unit[:24]}


@dataclass(slots=True)
class _ProgressState:
    task_ref: str
    phase: PublicPhase
    started: bool = False
    finished: bool = False
    emitted_texts: set[str] = field(default_factory=set)
    update_count: int = 0
    last_emit_at: float | None = None
    pending_texts: list[str] = field(default_factory=list)
    pending_count: dict[str, Any] | None = None


@dataclass(slots=True)
class _ActionState:
    action_ref: str
    definition: PublicActionDefinition


class SpecialistSignalProjector:
    """Stateful, deterministic projection from private signals to public events."""

    def __init__(
        self,
        *,
        registry: PublicActionRegistry | None = None,
        clock: Any = time.monotonic,
        coalesce_seconds: float = PROGRESS_COALESCE_SECONDS,
    ) -> None:
        self._registry = registry or PublicActionRegistry()
        self._clock = clock
        self._coalesce_seconds = coalesce_seconds
        self._progress: dict[tuple[str, PublicPhase], _ProgressState] = {}
        self._turn_updates: dict[str, int] = {}
        self._actions_by_call: dict[tuple[str, str], _ActionState] = {}
        self._grouped_actions: dict[tuple[str, str], _ActionState] = {}
        self._next_action_number: dict[str, int] = {}
        self._seen_signals: set[tuple[str, str]] = set()

    def project(
        self,
        signal: SpecialistSignalV1 | dict[str, Any],
        *,
        task_ref: str,
        now: float | None = None,
    ) -> list[PublicEventV1]:
        parsed = validate_specialist_signal(signal)
        signal_key = (parsed.web_session_id, parsed.signal_id)
        if signal_key in self._seen_signals:
            return []
        timestamp = self._clock() if now is None else now
        if isinstance(parsed, InternalSignalV1):
            self._seen_signals.add(signal_key)
            return []
        if isinstance(parsed, ProgressSignalV1):
            events = self._project_progress(parsed, task_ref=task_ref, now=timestamp)
        elif isinstance(parsed, ActionSignalV1):
            events = self._project_action(parsed, task_ref=task_ref)
        elif isinstance(parsed, RequestInputSignalV1):
            events = self._project_request_input(parsed, task_ref=task_ref)
        elif isinstance(parsed, ArtifactSignalV1):
            events = self._project_artifact(parsed, task_ref=task_ref)
        else:
            events = self._project_final(parsed, task_ref=task_ref, now=timestamp)
        self._seen_signals.add(signal_key)
        return events

    def flush(
        self,
        *,
        now: float | None = None,
        turn_id: str | None = None,
        force: bool = False,
    ) -> list[PublicEventV1]:
        timestamp = self._clock() if now is None else now
        events: list[PublicEventV1] = []
        for (state_turn_id, _phase), state in sorted(self._progress.items()):
            if turn_id is not None and state_turn_id != turn_id:
                continue
            if not state.pending_texts:
                continue
            if not force and state.last_emit_at is not None and timestamp - state.last_emit_at < self._coalesce_seconds:
                continue
            event = self._emit_pending_progress(state_turn_id, state, now=timestamp)
            if event is not None:
                events.append(event)
        return events

    def _project_progress(self, signal: ProgressSignalV1, *, task_ref: str, now: float) -> list[PublicEventV1]:
        key = (signal.turn_id, signal.phase)
        state = self._progress.setdefault(key, _ProgressState(task_ref=task_ref, phase=signal.phase))
        if state.task_ref != task_ref:
            raise ValueError("one turn/phase cannot be projected under different task references")
        if state.finished:
            return []

        events: list[PublicEventV1] = []
        text = sanitize_progress_text(signal.text)
        count = _count_payload(signal.done, signal.total, signal.unit)
        if signal.operation == "finish":
            if not state.started:
                state.started = True
                state.last_emit_at = now
                events.append(
                    self._event("progress_start", task_ref, {"phase": state.phase})
                )
            if text:
                self._queue_progress(signal.turn_id, state, text, count)
            events.extend(self.flush(now=now, turn_id=signal.turn_id, force=True))
            payload: dict[str, Any] = {"phase": state.phase, "status": signal.status}
            if count is not None:
                payload["count"] = count
            events.append(self._event("progress_end", task_ref, payload))
            state.finished = True
            return events

        if not state.started:
            state.started = True
            state.last_emit_at = now
            payload = {"phase": state.phase}
            if text and self._can_emit_progress(signal.turn_id, state):
                payload["summary"] = text
                if count is not None:
                    payload["count"] = count
                self._mark_progress_emitted(signal.turn_id, state, text, now)
            events.append(self._event("progress_start", task_ref, payload))
            return events

        if not text or text in state.emitted_texts or text in state.pending_texts:
            if count is not None:
                state.pending_count = count
            return events
        if state.last_emit_at is not None and now - state.last_emit_at < self._coalesce_seconds:
            self._queue_progress(signal.turn_id, state, text, count)
            return events
        self._queue_progress(signal.turn_id, state, text, count)
        event = self._emit_pending_progress(signal.turn_id, state, now=now)
        return [event] if event is not None else []

    def _queue_progress(self, turn_id: str, state: _ProgressState, text: str, count: dict[str, Any] | None) -> None:
        if not self._can_emit_progress(turn_id, state):
            return
        if text not in state.emitted_texts and text not in state.pending_texts:
            state.pending_texts.append(text)
        if count is not None:
            state.pending_count = count

    def _emit_pending_progress(self, turn_id: str, state: _ProgressState, *, now: float) -> PublicEventV1 | None:
        if not state.pending_texts or not self._can_emit_progress(turn_id, state):
            state.pending_texts.clear()
            state.pending_count = None
            return None
        summary = sanitize_progress_text("；".join(state.pending_texts))
        state.pending_texts.clear()
        payload: dict[str, Any] = {"phase": state.phase, "summary": summary}
        if state.pending_count is not None:
            payload["count"] = state.pending_count
        state.pending_count = None
        self._mark_progress_emitted(turn_id, state, summary, now)
        return self._event("progress_delta", state.task_ref, payload)

    def _can_emit_progress(self, turn_id: str, state: _ProgressState) -> bool:
        return (
            state.update_count < PROGRESS_UPDATES_PER_PHASE
            and self._turn_updates.get(turn_id, 0) < PROGRESS_UPDATES_PER_TURN
        )

    def _mark_progress_emitted(self, turn_id: str, state: _ProgressState, text: str, now: float) -> None:
        state.emitted_texts.add(text)
        state.update_count += 1
        state.last_emit_at = now
        self._turn_updates[turn_id] = self._turn_updates.get(turn_id, 0) + 1

    def _project_action(self, signal: ActionSignalV1, *, task_ref: str) -> list[PublicEventV1]:
        definition = self._registry.resolve(signal.tool_name)
        if definition.visibility == ActionVisibility.SILENT:
            return []
        call_key = (signal.turn_id, signal.call_id)
        if definition.visibility == ActionVisibility.GROUPED:
            group_key = (signal.turn_id, definition.group)
            action = self._grouped_actions.get(group_key)
            if action is None:
                action = self._new_action(signal.turn_id, definition)
                self._grouped_actions[group_key] = action
            self._actions_by_call[call_key] = action
        else:
            action = self._actions_by_call.get(call_key)
            if action is None:
                action = self._new_action(signal.turn_id, definition)
                self._actions_by_call[call_key] = action

        payload: dict[str, Any] = {
            "action_ref": action.action_ref,
            "action_code": definition.action_code,
            "display_name": definition.display_name,
            "group": definition.group,
            "phase": definition.phase,
            "status": signal.status,
        }
        message = sanitize_progress_text(signal.message)
        if message:
            payload["summary"] = message
        count = _count_payload(signal.done, signal.total, signal.unit)
        if count is not None:
            payload["count"] = count
        event_type: PublicEventType = "action_start" if signal.operation == "start" else "action_end"
        return [self._event(event_type, task_ref, payload)]

    def _new_action(self, turn_id: str, definition: PublicActionDefinition) -> _ActionState:
        number = self._next_action_number.get(turn_id, 0) + 1
        self._next_action_number[turn_id] = number
        return _ActionState(action_ref=f"action-{number}", definition=definition)

    def _project_request_input(self, signal: RequestInputSignalV1, *, task_ref: str) -> list[PublicEventV1]:
        options = []
        for option in signal.options:
            item: dict[str, Any] = {
                "option_id": option.option_id,
                "label": sanitize_progress_text(option.label) or "选项",
                "destructive": option.destructive,
            }
            description = sanitize_progress_text(option.description)
            if description:
                item["description"] = description
            options.append(item)
        payload = {
            "interaction_ref": signal.interaction_ref,
            "kind": signal.interaction_kind,
            "blocking": signal.blocking,
            "risk": signal.risk,
            "title": sanitize_progress_text(signal.title) or "需要你的选择",
            "summary": sanitize_progress_text(signal.summary) or "继续前需要你的补充或确认。",
            "options": options,
            "interaction_revision": signal.interaction_revision,
            "expected_task_revision": signal.expected_task_revision,
            "expires_at": signal.expires_at,
        }
        return [self._event("interaction_required", task_ref, payload)]

    def _project_artifact(self, signal: ArtifactSignalV1, *, task_ref: str) -> list[PublicEventV1]:
        payload = {
            "artifact_ref": signal.artifact_ref,
            "label": sanitize_progress_text(signal.label) or "处理结果",
        }
        if signal.media_type and re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", signal.media_type):
            payload["media_type"] = signal.media_type
        return [self._event("artifact_ready", task_ref, payload)]

    def _project_final(self, signal: FinalSignalV1, *, task_ref: str, now: float) -> list[PublicEventV1]:
        events = self.flush(now=now, turn_id=signal.turn_id, force=True)
        text = sanitize_final_text(signal.text) or final_fallback(signal.task_status)
        events.append(self._event("final", task_ref, {"text": text, "task_status": signal.task_status}))
        return events

    @staticmethod
    def _event(event_type: PublicEventType, task_ref: str, payload: dict[str, Any]) -> PublicEventV1:
        return PublicEventV1(type=event_type, task_ref=task_ref, payload=payload)


__all__ = [
    "ActionSignalV1",
    "ActionVisibility",
    "ArtifactSignalV1",
    "FinalSignalV1",
    "InteractionOptionV1",
    "InternalSignalV1",
    "PROGRESS_COALESCE_SECONDS",
    "PROGRESS_TARGET_CHARS",
    "PROGRESS_UPDATES_PER_PHASE",
    "PROGRESS_UPDATES_PER_TURN",
    "PUBLIC_CONTRACT_VERSION",
    "PUBLIC_EVENT_TYPES",
    "PUBLIC_TEXT_HARD_LIMIT",
    "ProgressSignalV1",
    "PublicActionDefinition",
    "PublicActionRegistry",
    "PublicEventV1",
    "RequestInputSignalV1",
    "SpecialistSignalProjector",
    "SpecialistSignalV1",
    "final_fallback",
    "sanitize_final_text",
    "sanitize_progress_text",
    "validate_specialist_signal",
]
