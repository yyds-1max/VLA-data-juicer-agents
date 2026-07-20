from __future__ import annotations

import json
from typing import Any

from agentscope.event import ReplyEndEvent, ToolCallStartEvent, ToolResultEndEvent
from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk


_ROUTER_V1_GUIDANCE = """
DataPilot conversation contract v1 (authoritative):
- Every ordinary user message is handled by you first, even while a navigation task is active.
- Use the injected RouterContextEnvelope as volatile authoritative context. Never repeat its JSON or internal metadata to the user.
- Answer ordinary conversation and unrelated questions directly without changing the focused task.
- Ask one short clarification question when a concrete navigation request lacks a date or target.
- Use start_navigation_data_task only to create a concrete navigation task.
- Use continue_navigation_data_task for task input, adjustment, continuation, or resume. Preserve original_user_message exactly.
- Use control_navigation_data_task for an explicit stop or cancel intent.
- Never invent task references or state revisions; copy them exactly from RouterContextEnvelope.
- A successful start/continue/control call transfers response ownership to the runtime or specialist. End immediately after the tool result. Do not produce an Answer, acknowledgement, summary, or another model call.
- If a nonterminal navigation task already occupies the session, do not start a second task.
""".strip()


def _tool_chunk(payload: dict[str, Any], *, ok: bool = True) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))],
        state=ToolResultState.SUCCESS if ok else ToolResultState.ERROR,
        metadata=payload,
    )


class RouterContractV1Middleware(MiddlewareBase):
    """Inject volatile task context and stop Router after a successful delegation."""

    def __init__(self, *, runtime: Any, web_session_id: str, router_session_id: str) -> None:
        self._runtime = runtime
        self._web_session_id = web_session_id
        self._router_session_id = router_session_id

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        envelope = self._runtime.router_context_envelope(self._web_session_id)
        rendered = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        return (
            f"{current_prompt}\n\n{_ROUTER_V1_GUIDANCE}\n\n"
            f"RouterContextEnvelope (volatile; do not quote):\n{rendered}"
        )

    async def on_reply(self, agent: Any, input_kwargs: dict, next_handler: Any):
        tool_names: dict[str, str] = {}
        async for item in next_handler(**input_kwargs):
            yield item
            if isinstance(item, ToolCallStartEvent):
                tool_names[item.tool_call_id] = item.tool_call_name
                continue
            if not isinstance(item, ToolResultEndEvent):
                continue
            tool_name = tool_names.get(item.tool_call_id, "")
            if tool_name not in {
                "start_navigation_data_task",
                "continue_navigation_data_task",
                "control_navigation_data_task",
            }:
                continue
            if not self._runtime.consume_router_terminal_tool(
                router_session_id=self._router_session_id,
                tool_name=tool_name,
            ):
                continue
            yield ReplyEndEvent(
                session_id=agent.state.session_id,
                reply_id=agent.state.reply_id,
            )
            return


class _RouterToolBase(ToolBase):
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False

    def __init__(
        self,
        *,
        runtime: Any,
        web_session_id: str,
        router_session_id: str,
    ) -> None:
        self._runtime = runtime
        self._web_session_id = web_session_id
        self._router_session_id = router_session_id

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: object,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="DataPilot routing action is allowed.",
        )


class StartNavigationDataTaskV1Tool(_RouterToolBase):
    name = "start_navigation_data_task"
    description = (
        "Create one concrete navigation data task. Use only when date and target "
        "are known and no nonterminal navigation task already occupies the conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "request": {"type": "string"},
            "target": {"type": "string"},
            "date": {"type": "string", "pattern": "^[0-9]{8}$"},
            "scene_mode": {
                "type": "string",
                "enum": ["indoor", "outdoor", "unknown"],
            },
            "clips": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["medium", "high"]},
            "response_language": {"type": "string"},
        },
        "required": [
            "request",
            "target",
            "date",
            "reason",
            "missing_fields",
            "confidence",
            "response_language",
        ],
        "additionalProperties": False,
    }

    async def __call__(
        self,
        request: str,
        target: str,
        date: str,
        reason: str,
        missing_fields: list[str],
        confidence: str,
        response_language: str,
        clips: list[str] | None = None,
        scene_mode: str | None = None,
    ) -> ToolChunk:
        if missing_fields:
            return _tool_chunk(
                {"ok": False, "started": False, "message": "任务信息仍不完整。"},
                ok=False,
            )
        try:
            result = await self._runtime.start_navigation_agent_task_v1(
                web_session_id=self._web_session_id,
                router_session_id=self._router_session_id,
                request=request,
                target=target,
                date=date,
                clips=list(clips or []),
                scene_mode=scene_mode,
                reason=reason,
                response_language=response_language,
            )
        except Exception as exc:
            return _tool_chunk(
                self._runtime.safe_router_tool_error(exc, action="start"),
                ok=False,
            )
        return _tool_chunk(
            {"ok": True, "started": True, "task_ref": result["task_ref"]}
        )


class ContinueNavigationDataTaskV1Tool(_RouterToolBase):
    name = "continue_navigation_data_task"
    description = (
        "Continue the focused navigation task with the user's exact message. "
        "Use for providing input, adjusting, continuing, or resuming."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_ref": {"type": "string"},
            "original_user_message": {"type": "string"},
            "intent": {
                "type": "string",
                "enum": ["provide_input", "adjust", "continue", "resume"],
            },
            "response_language": {"type": "string"},
            "expected_task_revision": {"type": "integer", "minimum": 0},
        },
        "required": [
            "task_ref",
            "original_user_message",
            "intent",
            "response_language",
            "expected_task_revision",
        ],
        "additionalProperties": False,
    }

    async def __call__(
        self,
        task_ref: str,
        original_user_message: str,
        intent: str,
        response_language: str,
        expected_task_revision: int,
    ) -> ToolChunk:
        try:
            result = await self._runtime.continue_navigation_agent_task_v1(
                web_session_id=self._web_session_id,
                router_session_id=self._router_session_id,
                task_ref=task_ref,
                original_user_message=original_user_message,
                intent=intent,
                response_language=response_language,
                expected_task_revision=expected_task_revision,
            )
        except Exception as exc:
            return _tool_chunk(
                self._runtime.safe_router_tool_error(exc, action="continue"),
                ok=False,
            )
        return _tool_chunk({"ok": True, "continued": True, "task_ref": result["task_ref"]})


class ControlNavigationDataTaskV1Tool(_RouterToolBase):
    name = "control_navigation_data_task"
    description = "Stop the current navigation run or cancel the whole navigation task."
    input_schema = {
        "type": "object",
        "properties": {
            "task_ref": {"type": "string"},
            "action": {"type": "string", "enum": ["stop", "cancel"]},
            "response_language": {"type": "string"},
            "expected_task_revision": {"type": "integer", "minimum": 0},
        },
        "required": [
            "task_ref",
            "action",
            "response_language",
            "expected_task_revision",
        ],
        "additionalProperties": False,
    }

    async def __call__(
        self,
        task_ref: str,
        action: str,
        response_language: str,
        expected_task_revision: int,
    ) -> ToolChunk:
        try:
            result = await self._runtime.control_navigation_agent_task_v1(
                web_session_id=self._web_session_id,
                router_session_id=self._router_session_id,
                task_ref=task_ref,
                action=action,
                response_language=response_language,
                expected_task_revision=expected_task_revision,
            )
        except Exception as exc:
            return _tool_chunk(
                self._runtime.safe_router_tool_error(exc, action=action),
                ok=False,
            )
        return _tool_chunk({"ok": True, "task_ref": result["task_ref"], "status": result["status"]})


def router_v1_tools(
    *, runtime: Any, web_session_id: str, router_session_id: str
) -> list[ToolBase]:
    arguments = {
        "runtime": runtime,
        "web_session_id": web_session_id,
        "router_session_id": router_session_id,
    }
    return [
        StartNavigationDataTaskV1Tool(**arguments),
        ContinueNavigationDataTaskV1Tool(**arguments),
        ControlNavigationDataTaskV1Tool(**arguments),
    ]
