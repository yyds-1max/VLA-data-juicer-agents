from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal

from agentscope.event import EventBase
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from vla_data_juicer_agents.core.cancellation import (
    CancellationContext,
    bind_cancellation,
)


SUPPRESSED_TOOL_RESULT_EVENTS = {
    "TOOL_RESULT_START",
    "TOOL_RESULT_TEXT_DELTA",
    "TOOL_RESULT_DATA_DELTA",
    "TOOL_RESULT_END",
}
SUPPRESSED_THINKING_EVENTS = {
    "THINKING_BLOCK_START",
    "THINKING_BLOCK_DELTA",
    "THINKING_BLOCK_END",
}
SUPPRESSED_PUBLIC_EVENTS = SUPPRESSED_TOOL_RESULT_EVENTS | SUPPRESSED_THINKING_EVENTS
_AGENT_NAMED_EVENT_TYPES = {"REPLY_START", "EXCEED_MAX_ITERS"}
_PUBLIC_CORRELATION_FIELDS = {"id", "reply_id", "block_id", "tool_call_id"}


def _event_type(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    return str(event_type).upper() if event_type is not None else ""


def _is_private_identity_field(key: str) -> bool:
    collapsed = "".join(
        character for character in key.lower() if character.isalnum()
    )
    return collapsed.endswith("agentid") or collapsed.endswith("sessionid")


def _replace_private_identities(
    value: str,
    private_identities: set[str],
    public_name: str,
) -> str:
    public = value
    for private in sorted(private_identities, key=len, reverse=True):
        if private:
            public = public.replace(private, public_name)
    return public


def _strip_internal_identity(
    value: Any,
    *,
    private_identities: set[str],
    public_name: str,
    preserve_string: bool = False,
) -> Any:
    if isinstance(value, dict):
        return {
            _replace_private_identities(
                key,
                private_identities,
                public_name,
            ): _strip_internal_identity(
                item,
                private_identities=private_identities,
                public_name=public_name,
                preserve_string=(
                    key in _PUBLIC_CORRELATION_FIELDS and isinstance(item, str)
                ),
            )
            for key, item in value.items()
            if not _is_private_identity_field(key)
        }
    if isinstance(value, list):
        return [
            _strip_internal_identity(
                item,
                private_identities=private_identities,
                public_name=public_name,
            )
            for item in value
        ]
    if isinstance(value, str):
        if preserve_string:
            return value
        return _replace_private_identities(value, private_identities, public_name)
    return value


def sanitize_agent_event(
    event: dict[str, Any],
    *,
    public_name: str = "DataPilot",
    private_identities: set[str] | None = None,
) -> dict[str, Any]:
    """Remove private runtime identities without changing AgentScope payloads."""
    identities = set(private_identities or ())
    if _event_type(event) in _AGENT_NAMED_EVENT_TYPES:
        agent_name = event.get("name")
        if isinstance(agent_name, str):
            identities.add(agent_name)
    return _strip_internal_identity(
        event,
        private_identities=identities,
        public_name=public_name,
    )


def _projection_private_identities(agent: Any, sink: Any, session_id: str) -> set[str]:
    identities = {session_id}
    agent_name = getattr(agent, "name", None)
    if isinstance(agent_name, str):
        identities.add(agent_name)
    provider = getattr(sink, "projection_private_identities", None)
    if callable(provider):
        identities.update(
            identity
            for identity in provider()
            if isinstance(identity, str) and identity
        )
    return identities


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
        inputs = input_kwargs.get("inputs")
        reply_id = str(getattr(inputs, "reply_id", "") or "")
        continuation_scope = ""
        if reply_id and isinstance(inputs, EventBase):
            metadata = getattr(inputs, "metadata", {}) or {}
            stable_input_id = metadata.get("idempotency_key") or getattr(
                inputs,
                "id",
                "",
            )
            continuation_scope = (
                f"{_event_type(inputs.model_dump(mode='json'))}:"
                f"{stable_input_id}"
            )
        ordinal = 0
        async for event in next_handler(**input_kwargs):
            if not isinstance(event, EventBase):
                yield event
                continue
            raw = event.model_dump(mode="json")
            event_type = _event_type(raw)
            if event_type == "REPLY_START":
                reply_id = str(raw.get("reply_id", ""))
                continuation_scope = ""
                ordinal = 0
            if event_type not in SUPPRESSED_PUBLIC_EVENTS:
                public = sanitize_agent_event(
                    raw,
                    public_name="DataPilot",
                    private_identities=_projection_private_identities(
                        agent,
                        self._sink,
                        self._session_id,
                    ),
                )
                identity = f"{self._session_id}:{reply_id}:"
                if continuation_scope:
                    identity += f"continuation:{continuation_scope}:"
                identity += str(ordinal)
                await self._sink.project_agent_event(
                    self._session_id,
                    dedupe_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    event=public,
                )
                ordinal += 1
            yield event


class DataPilotRunBoundaryMiddleware(MiddlewareBase):
    """Bind cancellation to every ChatService run and fence stopped wakeups."""

    def __init__(self, session_id: str, sink: Any) -> None:
        self._session_id = session_id
        self._sink = sink

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        should_suppress = getattr(self._sink, "should_suppress_wakeup", None)
        if (
            input_kwargs.get("inputs") is None
            and callable(should_suppress)
            and should_suppress(self._session_id)
        ):
            # Ack the already-dequeued wakeup without a model call or a
            # public reply.  ChatService still receives a final Msg.
            yield AssistantMsg(
                name=getattr(agent, "name", "DataPilot"),
                content=[],
            )
            return

        cancellation = self._sink.run_cancellation(self._session_id)
        if cancellation is None:
            cancellation = CancellationContext()
        self._sink.register_run_cancellation(
            self._session_id,
            cancellation,
        )
        try:
            admit_generation = getattr(
                self._sink,
                "admit_user_execution_generation",
                None,
            )
            inputs = input_kwargs.get("inputs")
            is_user_input = (
                isinstance(inputs, Msg) and inputs.role == "user"
            ) or (
                isinstance(inputs, list)
                and bool(inputs)
                and all(
                    isinstance(item, Msg) and item.role == "user"
                    for item in inputs
                )
            )
            if is_user_input and callable(admit_generation):
                # ChatService has acquired its distributed session-run lock
                # before invoking middleware. The async completion hook first
                # proves Stop can receive requests, then advances the public
                # generation without another scheduling point. Idle wakeups
                # never clear the stop fence.
                complete_admission = getattr(
                    self._sink,
                    "complete_user_execution_admission",
                    None,
                )
                if callable(complete_admission):
                    await complete_admission(self._session_id, cancellation)
                else:
                    admit_generation(self._session_id, cancellation)
            async with cancellation.track_agent(self._session_id):
                with bind_cancellation(cancellation):
                    async for item in next_handler(**input_kwargs):
                        yield item
        finally:
            self._sink.clear_run_cancellation(
                self._session_id,
                cancellation,
            )


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

    def _stopped_delivery(self, tool_call_id: str, finished: Any) -> bool:
        should_suppress = getattr(
            self._sink,
            "should_suppress_tool_delivery",
            None,
        )
        return bool(
            finished is None
            and callable(should_suppress)
            and should_suppress(self._session_id, tool_call_id)
        )

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
        stream = next_handler(**input_kwargs)
        while True:
            try:
                item = await anext(stream)
            except StopAsyncIteration:
                break
            except (Exception, asyncio.CancelledError) as exc:
                finished = await self._sink.finish_public_tool(
                    self._session_id,
                    tool_call_id=tool_call.id,
                    status="failure",
                    summary=str(exc),
                    error_type=type(exc).__name__,
                )
                if self._stopped_delivery(tool_call.id, finished):
                    raise asyncio.CancelledError(
                        "explicitly stopped tool result suppressed"
                    ) from None
                raise
            if isinstance(item, ToolResponse):
                status, summary, error_type = classify_real_tool_response(item)
                finished = await self._sink.finish_public_tool(
                    self._session_id,
                    tool_call_id=tool_call.id,
                    status=status,
                    summary=summary,
                    error_type=error_type,
                )
                if self._stopped_delivery(tool_call.id, finished):
                    # ToolOffload treats task cancellation as a deliberate
                    # no-delivery outcome, preventing a stopped late result
                    # from entering the inbox and waking another model run.
                    raise asyncio.CancelledError(
                        "explicitly stopped tool result suppressed"
                    )
            yield item
