from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    DISABLED = "disabled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


class TrainingNodeStatus(StrEnum):
    """Server-owned lifecycle and health state for a training node."""

    PENDING_ENROLLMENT = "pending_enrollment"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    REPAIR_REQUIRED = "repair_required"
    DISABLED = "disabled"


class WorkerHealth(StrEnum):
    """Health reports accepted from an authenticated Training Worker."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    REPAIR_REQUIRED = "repair_required"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.LOST}
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


class ParameterVisibilityCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameter_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    equals: Any


class ParameterChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)

    @field_validator("value", "label")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("enum choices cannot contain control characters")
        normalized = value.strip()
        if not normalized:
            raise ValueError("enum choices cannot be blank")
        return normalized


class ParameterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    label: str = Field(default="", max_length=200)
    kind: Literal["integer", "float", "boolean", "enum", "string"]
    semantic_role: Literal["hyperparameter", "dataset", "stage_input"] = "hyperparameter"
    cli_flag: str = Field(pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
    # ``None`` is retained while reading legacy revisions.  Those revisions
    # encoded booleans as presence-only flags; non-booleans always used a
    # flag/value pair.  New registrations persist an explicit style.
    argument_style: Literal["value", "explicit_boolean", "flag_when_true"] | None = None
    default: Any
    editable: bool = True
    sensitive: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: list[ParameterChoice] | None = Field(default=None, max_length=100)
    pattern: str | None = None
    string_min_length: int | None = Field(default=None, ge=0, le=512)
    string_max_length: int | None = Field(default=None, ge=0, le=512)
    description: str = Field(default="", max_length=120)
    visible_when: ParameterVisibilityCondition | None = None
    display_group: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    display_group_label: str | None = Field(default=None, min_length=2, max_length=30)
    display_group_order: int | None = Field(default=None, ge=0, le=1000)

    @field_validator("choices", mode="before")
    @classmethod
    def normalize_legacy_choices(cls, value: Any) -> Any:
        if value is None:
            return None
        return [
            choice
            if isinstance(choice, dict)
            else {"value": str(choice), "label": str(choice)}
            for choice in value
        ]

    @model_validator(mode="after")
    def validate_definition(self) -> "ParameterDefinition":
        if self.semantic_role == "dataset" and self.kind not in {"string", "enum"}:
            raise ValueError("dataset parameters must be strings or enums")
        if self.semantic_role == "stage_input" and self.kind != "string":
            raise ValueError("stage input parameters must be strings")
        if self.semantic_role == "stage_input" and self.visible_when is not None:
            raise ValueError("stage input parameters cannot depend on another parameter")
        if self.argument_style is None:
            self.argument_style = (
                "flag_when_true" if self.kind == "boolean" else "value"
            )
        if self.kind == "boolean":
            if self.argument_style not in {"explicit_boolean", "flag_when_true"}:
                raise ValueError(
                    "boolean parameters require explicit_boolean or flag_when_true"
                )
        elif self.argument_style != "value":
            raise ValueError(
                "explicit_boolean and flag_when_true are only valid for boolean parameters"
            )
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if any(bound is not None and not math.isfinite(bound) for bound in (self.minimum, self.maximum)):
            raise ValueError("numeric bounds must be finite")
        if self.kind == "integer":
            if any(bound is not None and not float(bound).is_integer() for bound in (self.minimum, self.maximum)):
                raise ValueError("integer parameter bounds must be integers")
            if any(bound is not None and abs(bound) > _MAX_SAFE_INTEGER for bound in (self.minimum, self.maximum)):
                raise ValueError("integer parameter bounds exceed the safe integer range")
        elif self.kind != "float" and (self.minimum is not None or self.maximum is not None):
            raise ValueError("minimum and maximum are only valid for numeric parameters")
        if self.kind == "enum":
            values = [choice.value for choice in self.choices or []]
            if not values or len(set(values)) != len(values):
                raise ValueError("enum parameters require unique choices")
        elif self.choices is not None:
            raise ValueError("choices are only valid for enum parameters")
        if self.kind != "string" and self.pattern is not None:
            raise ValueError("pattern is only valid for string parameters")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError("invalid string pattern") from exc
        if self.kind != "string" and (self.string_min_length is not None or self.string_max_length is not None):
            raise ValueError("string length limits are only valid for string parameters")
        if self.string_min_length is not None and self.string_max_length is not None and self.string_min_length > self.string_max_length:
            raise ValueError("string_min_length cannot exceed string_max_length")
        if self.display_group is None:
            if self.display_group_label is not None or self.display_group_order is not None:
                raise ValueError("display group metadata requires display_group")
        elif self.display_group_label is None or self.display_group_order is None:
            raise ValueError("display_group requires a label and order")
        normalize_parameter_value(self, self.default)
        return self


class ModelRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    working_directory: str = Field(min_length=1, max_length=1024)
    entrypoint: str = Field(min_length=1, max_length=1024)
    fixed_argv: list[str] = Field(default_factory=list, max_length=64)
    output_template: str = Field(default="outputs/{run_ref}", min_length=1, max_length=1024)
    parameter_definitions: list[ParameterDefinition] = Field(default_factory=list, max_length=128)

    @field_validator("working_directory", "entrypoint", "output_template")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("control characters are not allowed")
        return value

    @field_validator("fixed_argv")
    @classmethod
    def validate_fixed_argv(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 1024 or any(c in value for c in ("\x00", "\n", "\r")):
                raise ValueError("fixed argv contains an invalid value")
        return values

    @model_validator(mode="after")
    def validate_unique_parameters(self) -> "ModelRevisionInput":
        names = [item.name for item in self.parameter_definitions]
        flags = [item.cli_flag for item in self.parameter_definitions]
        if len(names) != len(set(names)) or len(flags) != len(set(flags)):
            raise ValueError("parameter names and CLI flags must be unique")
        if sum(
            item.semantic_role == "dataset"
            for item in self.parameter_definitions
        ) > 1:
            raise ValueError("a model family can declare at most one dataset parameter")
        if sum(
            item.semantic_role == "stage_input"
            for item in self.parameter_definitions
        ) > 1:
            raise ValueError("a model family can declare at most one stage input parameter")
        fields = set(re.findall(r"{([^{}]+)}", self.output_template))
        if fields - {"run_ref", "model_ref"}:
            raise ValueError("output template contains an unsupported field")
        return self


class ModelUpdateInput(ModelRevisionInput):
    expected_revision: int = Field(ge=1)


class RunStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: dict[str, Any] = Field(default_factory=dict)
    stage_input_source: Literal["manual", "previous_stage_output"] = "manual"


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family_ref: str = Field(min_length=1, max_length=128)
    server_ref: str = Field(default="fake-local", min_length=1, max_length=128)
    gpu_uuids: list[str] = Field(min_length=1, max_length=8)
    stages: list[RunStageRequest] = Field(min_length=1, max_length=10)
    mode: Literal["simulation"] = "simulation"

    @field_validator("gpu_uuids")
    @classmethod
    def unique_gpus(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("GPU selections must be unique")
        return values

    @model_validator(mode="after")
    def first_stage_is_manual(self) -> "RunRequest":
        if self.stages[0].stage_input_source != "manual":
            raise ValueError("the first stage must use manual input")
        return self


def normalize_parameter_value(definition: ParameterDefinition, value: Any) -> Any:
    kind = definition.kind
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{definition.name} must be a boolean")
        return value
    if kind == "integer":
        if type(value) is not int:
            raise ValueError(f"{definition.name} must be an integer")
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError(f"{definition.name} exceeds the safe integer range")
        numeric: int | float = value
    elif kind == "float":
        if type(value) not in (int, float):
            raise ValueError(f"{definition.name} must be a number")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"{definition.name} must be finite")
        value = numeric
    elif kind in {"enum", "string"}:
        if not isinstance(value, str) or len(value) > 512 or any(c in value for c in ("\x00", "\n", "\r")):
            raise ValueError(f"{definition.name} must be a safe string")
        if kind == "enum" and value not in [choice.value for choice in definition.choices or []]:
            raise ValueError(f"{definition.name} is not an allowed choice")
        if definition.pattern is not None and re.fullmatch(definition.pattern, value) is None:
            raise ValueError(f"{definition.name} does not match its required pattern")
        if kind == "string":
            if definition.string_min_length is not None and len(value) < definition.string_min_length:
                raise ValueError(f"{definition.name} is shorter than its minimum length")
            if definition.string_max_length is not None and len(value) > definition.string_max_length:
                raise ValueError(f"{definition.name} exceeds its maximum length")
        return value
    else:  # pragma: no cover - guarded by the model schema
        raise ValueError(f"unsupported parameter type {kind}")
    if definition.minimum is not None and numeric < definition.minimum:
        raise ValueError(f"{definition.name} is below its minimum")
    if definition.maximum is not None and numeric > definition.maximum:
        raise ValueError(f"{definition.name} exceeds its maximum")
    return value
