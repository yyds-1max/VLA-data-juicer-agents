"""Compact, model-authored turn outcomes consumed by the runtime control plane."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AwaitUserDispositionV1(BaseModel):
    """Declare that the current specialist turn must yield for user input.

    The model supplies only business semantics and public copy.  Task identity,
    revisions, persistence, response authority, and state transitions remain
    runtime-owned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    kind: Literal["await_user"] = "await_user"
    purpose: Literal[
        "stage_transition",
        "collect_finish_processing_inputs",
        "task_clarification",
    ]
    requested_fields: tuple[
        Literal[
            "continue_processing",
            "scene_mode",
            "task_guidance",
        ],
        ...,
    ] = Field(min_length=1, max_length=8)
    response_channel: Literal["router_text"] = "router_text"
    public_prompt: str = Field(min_length=1, max_length=2_000)

    @field_validator("requested_fields")
    @classmethod
    def require_unique_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested_fields must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_fields_for_purpose(self) -> "AwaitUserDispositionV1":
        """Keep conversational waits separate from structured parameter dialogs."""
        fields = set(self.requested_fields)
        finish_fields = {"continue_processing", "scene_mode"}
        if self.purpose == "stage_transition":
            if "continue_processing" not in fields or not fields <= finish_fields:
                raise ValueError(
                    "stage_transition must request continue_processing and may also "
                    "request scene_mode"
                )
        elif self.purpose == "collect_finish_processing_inputs":
            if not fields <= finish_fields:
                raise ValueError(
                    "collect_finish_processing_inputs may request only continuation "
                    "or scene mode"
                )
        elif fields != {"task_guidance"}:
            raise ValueError("task_clarification must request only task_guidance")
        return self

    @field_validator("public_prompt")
    @classmethod
    def normalize_public_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("public_prompt must not be blank")
        return normalized


__all__ = ["AwaitUserDispositionV1"]
