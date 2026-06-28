from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vla_data_juicer_agents.tui.models import (
    AgentState,
    TimelineItem,
    ToolCallState,
    TuiState,
    source_label,
)


def _timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def apply_event(state: TuiState, event: dict[str, Any]) -> None:
    event_type = str(event.get("type", "")).strip()
    if not event_type:
        return
    source = str(event.get("source", "")).strip()
    run_id = str(event.get("run_id", "")).strip()
    parent_run_id = str(event.get("parent_run_id") or "").strip() or None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    timestamp = _timestamp(event.get("timestamp"))
    label = source_label(source)

    if event_type == "reasoning":
        summary = str(payload.get("summary", "")).strip()
        if summary:
            state.timeline.append(
                TimelineItem(
                    kind="reasoning",
                    source_label=label,
                    text=summary,
                    timestamp=timestamp,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                ),
            )
        return

    if event_type == "agent_start":
        state.active_agents[run_id] = AgentState(
            run_id=run_id,
            parent_run_id=parent_run_id,
            source=source,
            started_at=timestamp,
        )
        state.timeline.append(
            TimelineItem(
                kind="agent",
                source_label=label,
                text="started",
                status="started",
                timestamp=timestamp,
                run_id=run_id,
                parent_run_id=parent_run_id,
            ),
        )
        return

    if event_type == "agent_end":
        state.active_agents.pop(run_id, None)
        status = str(payload.get("status", "completed")).strip() or "completed"
        state.timeline.append(
            TimelineItem(
                kind="agent",
                source_label=label,
                text=status,
                status=status,
                timestamp=timestamp,
                run_id=run_id,
                parent_run_id=parent_run_id,
            ),
        )
        return

    if event_type == "tool_start":
        call_id = str(payload.get("call_id", "")).strip()
        if not call_id:
            return
        tool_identity = (run_id, call_id)
        state.active_tools[tool_identity] = ToolCallState(
            call_id=call_id,
            tool=str(payload.get("tool", "unknown_tool")).strip() or "unknown_tool",
            source=source,
            run_id=run_id,
            parent_run_id=parent_run_id,
            started_at=timestamp,
            args_preview=str(payload.get("args", "")).strip(),
        )
        if tool_identity not in state.tool_call_order:
            state.tool_call_order.append(tool_identity)
        return

    if event_type == "tool_end":
        call_id = str(payload.get("call_id", "")).strip()
        tool_identity = (run_id, call_id)
        active = state.active_tools.pop(tool_identity, None)
        if tool_identity in state.tool_call_order:
            state.tool_call_order.remove(tool_identity)
        tool = str(payload.get("tool", "")).strip() or (
            active.tool if active is not None else "unknown_tool"
        )
        status = str(payload.get("status", "")).strip()
        if not status:
            status = "completed" if bool(payload.get("ok", True)) else "failed"
        summary = str(payload.get("summary", "")).strip()
        if not summary:
            summary = str(payload.get("error_type", "")).strip()
        elapsed = None
        if active is not None:
            elapsed = max((timestamp - active.started_at).total_seconds(), 0.0)
        state.timeline.append(
            TimelineItem(
                kind="tool",
                source_label=label,
                text=summary,
                status=status,
                tool=tool,
                timestamp=timestamp,
                elapsed_sec=elapsed,
                run_id=run_id,
                parent_run_id=parent_run_id,
            ),
        )
        return

    if event_type == "assistant_delta":
        return

    if event_type == "final":
        if run_id in state._final_runs:
            return
        state._final_runs.add(run_id)
        state.stop = bool(payload.get("stop", False))
        text = str(payload.get("text", "")).strip()
        if text:
            state.timeline.append(
                TimelineItem(
                    kind="assistant",
                    source_label=label,
                    text=text,
                    timestamp=timestamp,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                ),
            )
        return

    state.timeline.append(
        TimelineItem(
            kind="system",
            source_label=label,
            text=event_type,
            timestamp=timestamp,
            run_id=run_id,
            parent_run_id=parent_run_id,
        ),
    )
