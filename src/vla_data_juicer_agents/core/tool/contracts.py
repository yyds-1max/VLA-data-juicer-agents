from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolContext:
    working_dir: str
    artifacts_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    runtime_values: dict[str, Any] = field(default_factory=dict)

    def artifacts_path(self) -> Path:
        return Path(self.artifacts_dir or self.working_dir).expanduser()


ToolExecutor = Callable[[ToolContext, BaseModel], dict[str, Any] | Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    executor: ToolExecutor
    tags: tuple[str, ...] = ()
    effects: Literal["read", "write", "execute"] = "read"
    confirmation: Literal["none", "required"] = "none"

