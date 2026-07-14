from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse


SUPPRESSED_TOOL_RESULT_EVENTS = {
    "TOOL_RESULT_START",
    "TOOL_RESULT_TEXT_DELTA",
    "TOOL_RESULT_DATA_DELTA",
    "TOOL_RESULT_END",
}
_INTERNAL_IDENTITY_FIELDS = {
    "agent_id",
    "agentscope_session_id",
    "session_id",
}


def _event_type(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    return str(event_type).upper() if event_type is not None else ""


def _strip_internal_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_internal_identity(item)
            for key, item in value.items()
            if key not in _INTERNAL_IDENTITY_FIELDS
        }
    if isinstance(value, list):
        return [_strip_internal_identity(item) for item in value]
    return value


def sanitize_agent_event(
    event: dict[str, Any],
    *,
    public_name: str = "DataPilot",
) -> dict[str, Any]:
    """Remove private runtime identities without changing AgentScope payloads."""
    public = _strip_internal_identity(event)
    if "name" in public:
        public["name"] = public_name
    return public


class DataPilotReplyProjectionMiddleware(MiddlewareBase):
    def __init__(self, session_id: str, sink: Any) -> None:
        self._session_id = session_id
        self._sink = sink

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        reply_id = ""
        ordinal = 0
        async for event in next_handler(**input_kwargs):
            raw = event.model_dump(mode="json")
            event_type = _event_type(raw)
            if event_type == "REPLY_START":
                reply_id = str(raw.get("reply_id", ""))
                ordinal = 0
            if event_type not in SUPPRESSED_TOOL_RESULT_EVENTS:
                public = sanitize_agent_event(raw, public_name="DataPilot")
                identity = f"{self._session_id}:{reply_id}:{ordinal}"
                await self._sink.project_agent_event(
                    self._session_id,
                    dedupe_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    event=public,
                )
                ordinal += 1
            yield event


def _response_text(response: ToolResponse) -> str:
    return "".join(
        block.text for block in response.content if isinstance(block, TextBlock)
    ).strip()


def _decoded_content(response: ToolResponse) -> list[Any]:
    decoded: list[Any] = []
    for block in response.content:
        if not isinstance(block, TextBlock):
            continue
        text = block.text.strip()
        if not text:
            continue
        try:
            decoded.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            continue
    return decoded


def _contains_ok_false(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("ok") is False or any(
            _contains_ok_false(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_ok_false(item) for item in value)
    return False


def _structured_string(values: list[Any], key: str) -> str | None:
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def classify_real_tool_response(
    response: ToolResponse,
) -> tuple[Literal["success", "failure"], str, str | None]:
    decoded = _decoded_content(response)
    text = _response_text(response)
    summary = next(
        (
            candidate
            for key in ("summary", "message", "error", "detail")
            if (candidate := _structured_string(decoded, key)) is not None
        ),
        text,
    )
    state = (
        response.state.value
        if isinstance(response.state, ToolResultState)
        else str(response.state)
    )
    error_type = _structured_string(decoded, "error_type")

    if response.state == ToolResultState.SUCCESS and not any(
        _contains_ok_false(value) for value in decoded
    ):
        return "success", summary, None
    return "failure", summary, error_type or state


class DataPilotToolOutcomeMiddleware(MiddlewareBase):
    def __init__(self, session_id: str, sink: Any) -> None:
        self._session_id = session_id
        self._sink = sink

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs["tool_call"]
        await self._sink.start_public_tool(
            self._session_id,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
        )
        try:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, ToolResponse):
                    status, summary, error_type = classify_real_tool_response(item)
                    await self._sink.finish_public_tool(
                        self._session_id,
                        tool_call_id=tool_call.id,
                        status=status,
                        summary=summary,
                        error_type=error_type,
                    )
                yield item
        except (Exception, asyncio.CancelledError) as exc:
            await self._sink.finish_public_tool(
                self._session_id,
                tool_call_id=tool_call.id,
                status="failure",
                summary=str(exc),
                error_type=type(exc).__name__,
            )
            raise
