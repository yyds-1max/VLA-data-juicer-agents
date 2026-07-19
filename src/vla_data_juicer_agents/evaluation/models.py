from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base class for versioned evaluation inputs and outputs."""

    model_config = ConfigDict(extra="forbid")


class ConversationTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class CaseLimits(StrictModel):
    max_model_calls: int = Field(default=4, ge=1)
    max_tool_calls: int = Field(default=4, ge=0)
    timeout_seconds: int = Field(default=180, ge=1, le=1800)


class ExpectedHandoff(StrictModel):
    request: str
    target: str
    date: str = Field(pattern=r"^[0-9]{8}$")
    clips: list[str] = Field(default_factory=list)
    response_language: str
    missing_fields: list[str] = Field(default_factory=list)
    allowed_confidence: list[Literal["medium", "high"]] = Field(
        default_factory=lambda: ["medium", "high"],
        min_length=1,
    )
    forbidden_fields: list[str] = Field(default_factory=list)


class ToolExpectations(StrictModel):
    allowed_calls: list[str] = Field(default_factory=list)
    required_counts: dict[str, int] = Field(default_factory=dict)
    handoff_count: int = Field(default=0, ge=0)
    handoff: ExpectedHandoff | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> "ToolExpectations":
        if any(count < 0 for count in self.required_counts.values()):
            raise ValueError("required tool counts cannot be negative")
        if self.handoff is not None and self.handoff_count < 1:
            raise ValueError("handoff expectations require handoff_count >= 1")
        return self


class ResponseExpectations(StrictModel):
    language: Literal["Chinese", "English"] | None = None
    required_any_groups: list[list[str]] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    require_question: bool = False
    max_chars: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_groups(self) -> "ResponseExpectations":
        if any(not group or any(not term for term in group) for group in self.required_any_groups):
            raise ValueError("required_any_groups must contain non-empty terms")
        return self


class CaseExpectations(StrictModel):
    tools: ToolExpectations
    response: ResponseExpectations


class EvaluationCase(StrictModel):
    schema_version: Literal[1]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    suite: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    entrypoint: Literal["router", "navigation", "end_to_end"]
    tags: list[str] = Field(default_factory=list)
    conversation: list[ConversationTurn] = Field(min_length=1)
    limits: CaseLimits = Field(default_factory=CaseLimits)
    expectations: CaseExpectations

    @model_validator(mode="after")
    def validate_conversation(self) -> "EvaluationCase":
        if self.conversation[-1].role != "user":
            raise ValueError("the last conversation turn must be a user turn")
        return self


class ToolCallObservation(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    blocked: bool = False


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class CaseRunObservation(StrictModel):
    final_response: str = ""
    tool_calls: list[ToolCallObservation] = Field(default_factory=list)
    forbidden_calls: list[str] = Field(default_factory=list)
    handoffs: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    visible_tool_sets: list[list[str]] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_calls: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class GradingCheck(StrictModel):
    name: str
    passed: bool
    message: str


class CaseResult(StrictModel):
    case_id: str
    suite: str
    repeat_index: int = Field(default=1, ge=1)
    status: EvaluationStatus
    checks: list[GradingCheck] = Field(default_factory=list)
    observation: CaseRunObservation | None = None
    error_type: str | None = None
    error_message: str | None = None
    metrics: dict[str, int | float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def timeout(
        cls,
        case: EvaluationCase,
        *,
        repeat_index: int = 1,
        message: str = "case exceeded its hard timeout",
        observation: CaseRunObservation | None = None,
    ) -> "CaseResult":
        return cls(
            case_id=case.id,
            suite=case.suite,
            repeat_index=repeat_index,
            status=EvaluationStatus.TIMEOUT,
            observation=observation,
            error_type="TimeoutError",
            error_message=message,
        )

    @classmethod
    def error(
        cls,
        case: EvaluationCase,
        *,
        error: BaseException | str,
        repeat_index: int = 1,
        observation: CaseRunObservation | None = None,
    ) -> "CaseResult":
        return cls(
            case_id=case.id,
            suite=case.suite,
            repeat_index=repeat_index,
            status=EvaluationStatus.ERROR,
            observation=observation,
            error_type=type(error).__name__ if isinstance(error, BaseException) else "EvaluationError",
            error_message=str(error),
        )
