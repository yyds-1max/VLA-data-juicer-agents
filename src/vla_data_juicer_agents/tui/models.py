from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


SOURCE_LABELS = {
    "main": "Main",
    "navigation.workflow": "Workflow",
    "navigation.plan": "Plan",
    "navigation.executor": "Executor",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "Agent")


@dataclass
class AgentState:
    run_id: str
    parent_run_id: str | None
    source: str
    started_at: datetime


@dataclass
class ToolCallState:
    call_id: str
    tool: str
    source: str
    run_id: str
    parent_run_id: str | None
    started_at: datetime
    args_preview: str = ""


@dataclass
class TimelineItem:
    kind: str
    source_label: str
    text: str = ""
    status: str | None = None
    tool: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_sec: float | None = None
    run_id: str | None = None
    parent_run_id: str | None = None


@dataclass
class TuiState:
    timeline: list[TimelineItem] = field(default_factory=list)
    active_agents: dict[str, AgentState] = field(default_factory=dict)
    active_tools: dict[tuple[str, str], ToolCallState] = field(default_factory=dict)
    tool_call_order: list[tuple[str, str]] = field(default_factory=list)
    stop: bool = False
    _final_runs: set[str] = field(default_factory=set, repr=False)

    def spinner_text(self) -> str:
        for tool_identity in self.tool_call_order:
            tool = self.active_tools.get(tool_identity)
            if tool is not None:
                return f"[{source_label(tool.source)}] running {tool.tool}"

        deepest: AgentState | None = None
        deepest_depth = -1
        for agent in self.active_agents.values():
            depth = self._agent_depth(agent)
            if depth > deepest_depth:
                deepest = agent
                deepest_depth = depth
        if deepest is None:
            return ""
        return f"[{source_label(deepest.source)}] thinking"

    def _agent_depth(self, agent: AgentState) -> int:
        depth = 0
        parent_id = agent.parent_run_id
        visited = {agent.run_id}
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = self.active_agents.get(parent_id)
            if parent is None:
                break
            depth += 1
            parent_id = parent.parent_run_id
        return depth
