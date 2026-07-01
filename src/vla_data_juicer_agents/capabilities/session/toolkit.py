from __future__ import annotations

import os
from typing import Any

from agentscope.tool import Toolkit

from vla_data_juicer_agents.adapters.agentscope import build_agentscope_tool
from vla_data_juicer_agents.capabilities.session.runtime import SessionToolRuntime
from vla_data_juicer_agents.core.tool import ToolContext, ToolSpec, list_tool_specs


_GROUP_PRIORITY = {
    "vla": 10,
    "workflow": 20,
}
_WEB_SESSION_EXCLUDED_TOOL_NAMES = {"vla_run_workflow", "vla_continue_workflow"}


def _tool_context(runtime: SessionToolRuntime) -> ToolContext:
    root = str(runtime.storage_root())
    turn = runtime.turn_context()
    return ToolContext(
        working_dir=str(runtime.state.working_dir or "./.djx"),
        artifacts_dir=root,
        env=dict(os.environ),
        runtime_values={
            "session_runtime": runtime,
            "event_emitter": runtime.event_emitter,
            "event_scope": turn.scope if turn is not None else None,
            "cancellation": turn.cancellation if turn is not None else None,
            "emit_event": runtime.emit_event,
        },
    )


def _sort_key(spec: ToolSpec) -> tuple[int, str]:
    priority = min((_GROUP_PRIORITY.get(tag, 999) for tag in spec.tags), default=999)
    return priority, spec.name


def get_session_tool_specs() -> list[ToolSpec]:
    specs = [spec for spec in list_tool_specs() if spec.name not in _WEB_SESSION_EXCLUDED_TOOL_NAMES]
    return sorted(specs, key=_sort_key)


def build_session_toolkit(runtime: SessionToolRuntime) -> Toolkit:
    tools = [
        build_agentscope_tool(
            spec,
            ctx_factory=lambda runtime=runtime: _tool_context(runtime),
            runtime_invoke=runtime.invoke_tool,
        )
        for spec in get_session_tool_specs()
    ]
    return Toolkit(tools=tools)
