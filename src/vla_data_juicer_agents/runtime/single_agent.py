from __future__ import annotations

import json
from typing import Any

from agentscope.event import ReplyEndEvent, ToolCallStartEvent, ToolResultEndEvent
from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from vla_data_juicer_agents.runtime.agentscope_prompts import main_router_v1_prompt


_SCOPE_SELECTION_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "const": "all_clips"},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "const": "selected_clips"},
                "clips": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "pattern": "^(?!\\.{1,2}$)[^/\\\\\\r\\n]+$",
                    },
                    "minItems": 1,
                    "maxItems": 200,
                    "uniqueItems": True,
                },
            },
            "required": ["kind", "clips"],
            "additionalProperties": False,
        },
    ],
    "discriminator": {"propertyName": "kind"},
    "description": (
        "The user-selectable task scope. Use all_clips when no clips were "
        "specified. Treat every clip string as an opaque identifier: a date-like "
        "prefix does not have to match dataset_date. Internal segments and "
        "sequences are never selectable."
    ),
}


def _tool_chunk(payload: dict[str, Any], *, ok: bool = True) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))],
        state=ToolResultState.SUCCESS if ok else ToolResultState.ERROR,
        metadata=payload,
    )


def _router_tool_success(
    *,
    operation: str,
    result: dict[str, Any],
    default_status: str,
) -> dict[str, Any]:
    task_ref = str(result.get("task_ref") or "") or None
    status = str(result.get("status") or default_status)
    latest_task = result.get("latest_task")
    if not isinstance(latest_task, dict):
        latest_task = {
            "task_ref": task_ref,
            "status": status,
        }
    return {
        "ok": True,
        "operation": operation,
        "accepted": True,
        "task_ref": task_ref,
        "status": status,
        "error": None,
        "latest_task": latest_task,
    }


def _router_tool_failure(
    *,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {
            "code": str(
                payload.get("error_type")
                or payload.get("code")
                or "navigation_runtime_error"
            ),
            "message": str(payload.get("message") or "DataPilot 暂时无法执行该任务操作。"),
            "retryable": bool(payload.get("retryable", False)),
        }
    return {
        "ok": False,
        "operation": str(payload.get("operation") or operation),
        "accepted": False,
        "task_ref": payload.get("task_ref"),
        "status": payload.get("status"),
        "error": error,
        "latest_task": payload.get("latest_task"),
    }


class RouterContractV1Middleware(MiddlewareBase):
    """Inject volatile task context and stop Router after a successful delegation."""

    def __init__(self, *, runtime: Any, web_session_id: str, router_session_id: str) -> None:
        self._runtime = runtime
        self._web_session_id = web_session_id
        self._router_session_id = router_session_id

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        del agent, current_prompt
        envelope = self._runtime.router_context_envelope(
            self._web_session_id,
            router_session_id=self._router_session_id,
        )
        rendered = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        return (
            f"{main_router_v1_prompt()}\n\n"
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
        "Create one navigation task for a YYYYMMDD dataset date and either all_clips "
        "or selected_clips. Dataset date selects the storage directory; clip IDs are "
        "opaque and their date-like prefixes may differ. Scene mode is optional and "
        "must not block start."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "scope_source": {
                "type": "string",
                "enum": ["request_context", "interpreted_user_text"],
                "description": (
                    "Use request_context only for an exact trusted scope injected by "
                    "the runtime with kind navigation_dataset_selection_v1; otherwise "
                    "use interpreted_user_text."
                ),
            },
            "dataset_date": {
                "type": "string",
                "pattern": "^[0-9]{8}$",
                "description": (
                    "Navigation dataset storage-directory date in YYYYMMDD form. "
                    "Do not derive it from or compare it with clip ID prefixes."
                ),
            },
            "selection": _SCOPE_SELECTION_SCHEMA,
            "scene_mode": {
                "type": "string",
                "enum": ["indoor", "outdoor"],
                "description": (
                    "Optional scene context. Omit it when the user or trusted request "
                    "context did not explicitly provide indoor/outdoor information."
                ),
            },
        },
        "required": ["scope_source", "dataset_date", "selection"],
        "additionalProperties": False,
    }

    async def __call__(
        self,
        scope_source: str,
        dataset_date: str,
        selection: dict[str, Any],
        scene_mode: str | None = None,
    ) -> ToolChunk:
        try:
            result = await self._runtime.start_navigation_agent_task_v1(
                web_session_id=self._web_session_id,
                router_session_id=self._router_session_id,
                scope_source=scope_source,
                dataset_date=dataset_date,
                selection=dict(selection),
                scene_mode=scene_mode,
            )
        except Exception as exc:
            return _tool_chunk(
                _router_tool_failure(
                    operation="start",
                    payload=self._runtime.safe_router_tool_error(
                        exc,
                        action="start",
                        web_session_id=self._web_session_id,
                    ),
                ),
                ok=False,
            )
        return _tool_chunk(_router_tool_success(operation="start", result=result, default_status="active"))


class ContinueNavigationDataTaskV1Tool(_RouterToolBase):
    name = "continue_navigation_data_task"
    description = (
        "Continue the focused task using the current user turn and authoritative "
        "runtime context. The model supplies no task identity, revision, or user text."
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    async def __call__(self) -> ToolChunk:
        try:
            result = await self._runtime.continue_navigation_agent_task_v1(
                web_session_id=self._web_session_id,
                router_session_id=self._router_session_id,
            )
        except Exception as exc:
            return _tool_chunk(
                _router_tool_failure(
                    operation="continue",
                    payload=self._runtime.safe_router_tool_error(
                        exc,
                        action="continue",
                        web_session_id=self._web_session_id,
                    ),
                ),
                ok=False,
            )
        return _tool_chunk(
            _router_tool_success(
                operation="continue",
                result=result,
                default_status="active",
            )
        )


class ControlNavigationDataTaskV1Tool(_RouterToolBase):
    name = "control_navigation_data_task"
    description = "Stop the current navigation run or cancel the whole navigation task."
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["stop", "cancel"]},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def __call__(self, action: str) -> ToolChunk:
        try:
            result = await self._runtime.control_navigation_agent_task_v1(
                web_session_id=self._web_session_id,
                router_session_id=self._router_session_id,
                action=action,
            )
        except Exception as exc:
            return _tool_chunk(
                _router_tool_failure(
                    operation=action,
                    payload=self._runtime.safe_router_tool_error(
                        exc,
                        action=action,
                        web_session_id=self._web_session_id,
                    ),
                ),
                ok=False,
            )
        return _tool_chunk(
            _router_tool_success(
                operation=action,
                result=result,
                default_status="paused" if action == "stop" else "cancelled",
            )
        )


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
