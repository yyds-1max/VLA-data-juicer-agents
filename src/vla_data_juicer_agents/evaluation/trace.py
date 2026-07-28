"""Non-invasive trace collection and safety guards for local evaluations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from vla_data_juicer_agents.adapters.agentscope.events import AgentScopeEventAdapter
from vla_data_juicer_agents.core.events import CallbackEventSink, EventEmitter


_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "system_prompt",
    "thinking",
)
_API_KEY_PATTERN = re.compile(
    r"(?i)(?:bearer\s+)?(?<![a-z0-9])(?:sk-[a-z0-9_-]{12,}|dashscope[-_a-z0-9]{12,})",
)
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:Users|home|private|tmp|var|opt|srv|media)"
    r"(?:/[^\s\"'<>|,;:)}\]]*)*",
    flags=re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z]:\\[^\s\"'<>|,;)}\]]+",
)
_THINKING_EVENT_PREFIX = "THINKING"
_EVALUATION_ALLOWED_TOOLS = frozenset(
    {
        "start_navigation_data_task",
        "continue_navigation_data_task",
        "control_navigation_data_task",
        "inspect_navigation_raw_metadata_tool",
        "inspect_navigation_sensor_candidates_tool",
        "inspect_navigation_topic_candidates_tool",
        "inspect_navigation_runtime_assets_tool",
        "inspect_navigation_calibration_inventory_tool",
        "inspect_navigation_localization_sources_tool",
        "inspect_navigation_annotation_job_facts_tool",
        "inspect_navigation_artifact_state_tool",
        "inspect_navigation_gridmap_artifacts_tool",
        "get_navigation_task_context_tool",
        "describe_processing_action_tool",
        "submit_extract_sync_plan_tool",
        "submit_finish_processing_plan_tool",
        "submit_trajectory_review_plan_tool",
        "run_annotation_postprocessing_workflow_tool",
        "open_trajectory_fix_workbench_tool",
    },
)
_STREAM_DELTA_TYPES = frozenset(
    {"TEXT_BLOCK_DELTA", "TOOL_CALL_DELTA", "TOOL_RESULT_TEXT_DELTA"},
)


def schema_hash(tools: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable digest for the exact model-visible tool schemas."""

    payload = json.dumps(
        list(tools),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tool_names(tools: Sequence[Mapping[str, Any]]) -> list[str]:
    """Extract model-visible tool names without retaining their prompts."""

    names: list[str] = []
    for schema in tools:
        name = schema.get("function", {}).get("name")
        if isinstance(name, str):
            names.append(name)
    return names


@dataclass
class TraceRecorder:
    """Collect the minimum trace needed by deterministic evaluation graders."""

    sensitive_paths: tuple[str, ...] = ()
    events: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    forbidden_calls: list[dict[str, Any]] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    _tool_calls_by_id: dict[str, dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @classmethod
    def for_workspace(cls, workspace_root: str | Path) -> "TraceRecorder":
        return cls(sensitive_paths=(str(Path(workspace_root).resolve()),))

    def redact(self, value: Any) -> Any:
        """Recursively remove secrets, private prompts and temporary paths."""

        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).lower()
                if any(part in normalized for part in _SECRET_KEY_PARTS):
                    output[str(key)] = "[REDACTED]"
                else:
                    output[str(key)] = self.redact(item)
            return output
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            text = value
            for path in sorted(self.sensitive_paths, key=len, reverse=True):
                if path:
                    text = text.replace(path, "[WORKSPACE]")
            text = _API_KEY_PATTERN.sub("[REDACTED]", text)
            text = _POSIX_ABSOLUTE_PATH_PATTERN.sub("[PATH]", text)
            return _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("[PATH]", text)
        return deepcopy(value)

    @staticmethod
    def _stream_key(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
        event_type = str(event.get("type", "")).upper()
        if event_type not in _STREAM_DELTA_TYPES:
            return None
        if event_type == "TEXT_BLOCK_DELTA":
            identity = str(event.get("block_id") or event.get("reply_id") or "")
        else:
            identity = str(event.get("tool_call_id") or "")
        return event_type, identity, "delta"

    def sanitized_events(self) -> tuple[dict[str, Any], ...]:
        """Return a deep-copied trace redacted again after stream reassembly."""

        events = deepcopy(self.events)
        streams: dict[tuple[str, str, str], list[int]] = {}
        for index, event in enumerate(events):
            key = self._stream_key(event)
            if key is not None:
                streams.setdefault(key, []).append(index)

        for indices in streams.values():
            chunks = [str(events[index].get("delta", "")) for index in indices]
            joined = "".join(chunks)
            redacted = str(self.redact(joined))
            if redacted == joined:
                continue
            cursor = 0
            for position, (index, chunk) in enumerate(zip(indices, chunks, strict=True)):
                if position == len(indices) - 1:
                    replacement = redacted[cursor:]
                else:
                    replacement = redacted[cursor : cursor + len(chunk)]
                    cursor += len(replacement)
                events[index]["delta"] = replacement
        return tuple(self.redact(event) for event in events)

    def _public_final_text(self, events: Sequence[Mapping[str, Any]]) -> str:
        projected: list[dict[str, Any]] = []
        scope = EventEmitter(CallbackEventSink(projected.append)).scope(
            "evaluation",
            run_id="evaluation-public-reply",
        )
        adapter = AgentScopeEventAdapter(
            scope,
            emit_tool_events=True,
            emit_text_events=False,
            emit_final_events=False,
            emit_reasoning_events=False,
            emit_progress_events=True,
            emit_reply_summary_events=True,
            emit_answer_delta_events=True,
            # Mirror the production public terminal protocol for both Router
            # and bounded Navigation specialist evaluation entrypoints.
            recover_unmarked_terminal_answer=True,
            public_tool_events=True,
            suppress_pre_tool_text=True,
        )
        for event in events:
            adapter.accept(SimpleNamespace(**dict(event)))
        summaries = [
            event.get("payload", {}).get("text", "")
            for event in projected
            if event.get("type") == "reply_summary"
            and isinstance(event.get("payload"), Mapping)
        ]
        return str(self.redact(summaries[-1] if summaries else ""))

    def sanitized_snapshot(self) -> dict[str, Any]:
        """Build the only trace representation that may leave this process."""

        events = self.sanitized_events()
        return {
            "events": events,
            "model_calls": tuple(self.redact(self.model_calls)),
            "tool_calls": tuple(self.redact(self.tool_calls)),
            "forbidden_calls": tuple(self.redact(self.forbidden_calls)),
            "handoffs": tuple(self.redact(self.handoffs)),
            "final_text": self._public_final_text(events),
            "token_usage": dict(self.token_usage),
        }

    def accept_event(self, event: Mapping[str, Any]) -> None:
        """Record one AgentScope event, excluding all thinking events."""

        event_type = str(event.get("type", "")).upper()
        if event_type.startswith(_THINKING_EVENT_PREFIX):
            return
        raw = deepcopy(dict(event))
        self.events.append(raw)

        if event_type == "TOOL_CALL_START":
            call = {
                "id": str(raw.get("tool_call_id", "")),
                "name": str(raw.get("tool_call_name", "")),
                "input": "",
                "result": "",
            }
            self.tool_calls.append(call)
            self._tool_calls_by_id[call["id"]] = call
            # Some AgentScope builtins pause for framework-level permission
            # before ``on_acting`` is reached.  They are already prevented
            # from executing, but still count as forbidden model behaviour.
            if call["name"] not in _EVALUATION_ALLOWED_TOOLS:
                self.record_forbidden_call(call_id=call["id"], name=call["name"])
        elif event_type == "TOOL_CALL_DELTA":
            call_id = str(raw.get("tool_call_id", ""))
            call = self._tool_calls_by_id.get(call_id)
            if call is not None:
                call["input"] += str(raw.get("delta", ""))
        elif event_type == "TOOL_RESULT_TEXT_DELTA":
            call_id = str(raw.get("tool_call_id", ""))
            call = self._tool_calls_by_id.get(call_id)
            if call is not None:
                call["result"] += str(raw.get("delta", ""))

    def record_model_call(
        self,
        *,
        model_name: str,
        tools: Sequence[Mapping[str, Any]],
    ) -> None:
        self.model_calls.append(
            {
                "model_name": model_name,
                "tools": tool_names(tools),
                "schema_hash": schema_hash(tools),
            },
        )

    def record_forbidden_call(self, *, call_id: str, name: str) -> None:
        record = {"id": call_id, "name": name}
        if record not in self.forbidden_calls:
            self.forbidden_calls.append(record)

    def record_handoff(self, payload: Mapping[str, Any]) -> None:
        self.handoffs.append(self.redact(dict(payload)))

    @property
    def final_text(self) -> str:
        return self._public_final_text(self.sanitized_events())

    @property
    def token_usage(self) -> dict[str, int]:
        input_tokens = 0
        output_tokens = 0
        for event in self.events:
            if str(event.get("type", "")).upper() != "MODEL_CALL_END":
                continue
            input_tokens += int(event.get("input_tokens", 0) or 0)
            output_tokens += int(event.get("output_tokens", 0) or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }


class TraceMiddleware(MiddlewareBase):
    """Observe model-visible schemas without changing any model input."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    async def on_model_call(
        self,
        agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        del agent
        current_model = input_kwargs.get("current_model")
        model_name = str(
            getattr(current_model, "model_name", None)
            or getattr(current_model, "model", None)
            or type(current_model).__name__
        )
        self._recorder.record_model_call(
            model_name=model_name,
            tools=list(input_kwargs.get("tools") or []),
        )
        return await next_handler(**input_kwargs)


class EvaluationSafetyMiddleware(MiddlewareBase):
    """Fail closed before any generic AgentScope tool can produce effects."""

    ALLOWED_TOOL_NAMES = _EVALUATION_ALLOWED_TOOLS

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    async def on_acting(
        self,
        agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        del agent
        tool_call = input_kwargs["tool_call"]
        if tool_call.name not in self.ALLOWED_TOOL_NAMES:
            self._recorder.record_forbidden_call(
                call_id=tool_call.id,
                name=tool_call.name,
            )
            yield ToolResponse(
                id=tool_call.id,
                content=[
                    TextBlock(
                        text=(
                            "Tool execution is disabled in the local evaluation "
                            "environment."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                metadata={
                    "ok": False,
                    "error_type": "evaluation_forbidden_tool",
                    "tool_name": tool_call.name,
                },
            )
            return

        async for item in next_handler(**input_kwargs):
            yield item
