from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


_STRUCTURED_HANDOFF_MARKER = "Structured handoff JSON:"


class AllClipsSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["all_clips"]


class SelectedClipsSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["selected_clips"]
    clips: list[str] = Field(min_length=1, max_length=200)


class NavigationTaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str
    dataset_date: str = Field(pattern=r"^[0-9]{8}$")
    selection: AllClipsSelection | SelectedClipsSelection = Field(
        discriminator="kind",
    )
    scene_mode: Literal["in", "out"] | None = None
    requested_outcome: Literal[
        "auto",
        "extract_sync",
        "postprocessing",
        "postprocessing_and_fix",
        "trajectory_fix",
    ] = "auto"
    response_language: str


class NavigationTaskEntryError(ValueError):
    """Raised when a router handoff has no valid structured task identity."""


def _structured_handoff_payload_from_message(message: str) -> dict[str, Any] | None:
    if _STRUCTURED_HANDOFF_MARKER not in message:
        return None
    lines = message.split(_STRUCTURED_HANDOFF_MARKER, 1)[1].strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_navigation_task_entry(message: str) -> NavigationTaskEntry:
    payload = _structured_handoff_payload_from_message(message)
    if payload is None:
        raise NavigationTaskEntryError(
            "navigation task entry requires a structured handoff JSON object"
        )
    try:
        return NavigationTaskEntry.model_validate(payload)
    except ValidationError as error:
        detail = str(error).replace("\n", " ")[:1200]
        raise NavigationTaskEntryError(
            f"invalid structured navigation handoff: {detail}"
        ) from None
