import type { AgentEvent, PendingHumanDecision } from "../api/types";

export type TimelineKind = "reasoning" | "tool" | "agent" | "assistant" | "system";

export interface TimelineItem {
  kind: TimelineKind;
  source: string;
  text: string;
  status?: string;
  runId?: string | null;
  parentRunId?: string | null;
}

export interface ActiveAgent {
  source: string;
  runId: string;
  parentRunId: string | null;
  startedAt: number;
}

export interface ActiveTool {
  source: string;
  tool: string;
  callId: string;
  runId: string;
  parentRunId: string | null;
  startedAt: number;
}

export interface RunState {
  timeline: TimelineItem[];
  activeAgents: Record<string, ActiveAgent>;
  activeTools: Record<string, ActiveTool>;
  finalRunIds: Record<string, true>;
  pendingHumanDecision: PendingHumanDecision | null;
  activeText: string;
  activeStartedAt: number | null;
  running: boolean;
  interrupting: boolean;
  appliedEventKeys: Record<string, true>;
}

export function createEmptyRunState(): RunState {
  return {
    timeline: [],
    activeAgents: {},
    activeTools: {},
    finalRunIds: {},
    pendingHumanDecision: null,
    activeText: "",
    activeStartedAt: null,
    running: false,
    interrupting: false,
    appliedEventKeys: {},
  };
}

export function applyAgentEvent(state: RunState, event: AgentEvent): void {
  const type = event.type.trim();
  if (!type) {
    return;
  }

  const source = normalizeText(event.source) || "main";
  const runId = normalizeText(event.run_id);
  const parentRunId = normalizeNullableText(event.parent_run_id);
  const payload = event.payload ?? {};
  const label = sourceLabel(source);

  if (type === "agent_start") {
    const startedAt = timestampMs(event.timestamp);
    if (runId) {
      delete state.finalRunIds[runId];
    }
    state.activeAgents[agentKey(runId, source)] = { source, runId, parentRunId, startedAt };
    state.running = true;
    state.activeText = thinkingText(source, label);
    state.activeStartedAt = startedAt;
    return;
  }

  if (type === "reasoning") {
    const summary = normalizeText(payload.summary);
    if (summary) {
      state.timeline.push({
        kind: "reasoning",
        source,
        text: summary,
        runId,
        parentRunId,
      });
    }
    return;
  }

  if (type === "tool_start") {
    const callId = normalizeText(payload.call_id) || normalizeText(payload.callId);
    if (!callId) {
      return;
    }

    const tool = normalizeText(payload.tool) || "unknown_tool";
    state.activeTools[toolKey(runId, callId)] = {
      source,
      tool,
      callId,
      runId,
      parentRunId,
      startedAt: timestampMs(event.timestamp),
    };
    state.running = true;
    state.activeText = `正在调用工具 ${tool}`;
    state.activeStartedAt = state.activeTools[toolKey(runId, callId)].startedAt;
    return;
  }

  if (type === "tool_end") {
    const callId = normalizeText(payload.call_id) || normalizeText(payload.callId);
    const key = toolKey(runId, callId);
    const active = state.activeTools[key];
    if (active) {
      delete state.activeTools[key];
    }

    const tool = normalizeText(payload.tool) || active?.tool || "unknown_tool";
    const status = toolStatus(payload);
    const elapsed = elapsedSeconds(active?.startedAt, event.timestamp);
    state.timeline.push({
      kind: "tool",
      source,
      text: toolCompletionText(status, tool, elapsed),
      status,
      runId,
      parentRunId,
    });
    refreshRunningText(state);
    return;
  }

  if (type === "human_decision_required") {
    state.pendingHumanDecision = {
      replyId: normalizeText(payload.reply_id) || normalizeText(payload.replyId),
      toolCallId: normalizeText(payload.tool_call_id) || normalizeText(payload.toolCallId),
      requestId: normalizeText(payload.request_id) || normalizeText(payload.requestId),
      decisionType: normalizeText(payload.decision_type) || normalizeText(payload.decisionType) || "other",
      summary: normalizeText(payload.summary),
    };
    state.running = false;
    state.interrupting = false;
    state.activeText = "";
    state.activeStartedAt = null;
    return;
  }

  if (type === "assistant_delta") {
    const delta = normalizeText(payload.delta);
    if (!delta) {
      return;
    }
    const startsNewReply = Boolean(runId && state.finalRunIds[runId]);
    if (startsNewReply) {
      delete state.finalRunIds[runId];
    }
    const existing = startsNewReply ? undefined : findAssistantItem(state, source, runId);
    if (existing) {
      existing.text += delta;
    } else {
      state.timeline.push({
        kind: "assistant",
        source,
        text: delta,
        runId,
        parentRunId,
      });
    }
    state.running = true;
    state.interrupting = false;
    state.activeText = "";
    state.activeStartedAt = null;
    return;
  }

  if (type === "agent_end") {
    delete state.activeAgents[agentKey(runId, source)];
    refreshRunningText(state);
    return;
  }

  if (type === "final") {
    if (runId) {
      if (state.finalRunIds[runId]) {
        return;
      }
      state.finalRunIds[runId] = true;
    }

    const text = normalizeText(payload.text);
    if (text) {
      const existing = runId ? findAssistantItem(state, source, runId) : undefined;
      if (existing) {
        existing.text = text;
      } else {
        state.timeline.push({
          kind: "assistant",
          source,
          text,
          runId,
          parentRunId,
        });
      }
    }
    clearMatchingActiveRun(state, runId, source);
    refreshRunningText(state);
    return;
  }

  if (type === "interrupt_requested") {
    state.interrupting = true;
    state.running = true;
    return;
  }

  state.timeline.push({
    kind: "system",
    source,
    text: type,
    runId,
    parentRunId,
  });
}

function findAssistantItem(state: RunState, source: string, runId: string): TimelineItem | undefined {
  for (let index = state.timeline.length - 1; index >= 0; index -= 1) {
    const item = state.timeline[index];
    if (item.kind !== "assistant" || item.source !== source) {
      continue;
    }
    if (runId) {
      if (item.runId === runId) {
        return item;
      }
      continue;
    }
    if (!item.runId) {
      return item;
    }
  }
  return undefined;
}

function refreshRunningText(state: RunState): void {
  const activeTool = Object.values(state.activeTools)[0];
  if (activeTool) {
    state.running = true;
    state.activeText = `正在调用工具 ${activeTool.tool}`;
    state.activeStartedAt = activeTool.startedAt;
    return;
  }

  const activeAgent = deepestActiveAgent(state.activeAgents);
  if (activeAgent) {
    state.running = true;
    state.activeText = thinkingText(activeAgent.source, sourceLabel(activeAgent.source));
    state.activeStartedAt = activeAgent.startedAt;
    return;
  }

  state.running = false;
  state.interrupting = false;
  state.activeText = "";
  state.activeStartedAt = null;
}

function clearMatchingActiveRun(state: RunState, runId: string, source: string): void {
  if (!runId) {
    state.activeAgents = {};
    state.activeTools = {};
    return;
  }

  for (const [key, agent] of Object.entries(state.activeAgents)) {
    if (agent.runId === runId || (!agent.runId && agent.source === source)) {
      delete state.activeAgents[key];
    }
  }

  for (const [key, tool] of Object.entries(state.activeTools)) {
    if (tool.runId === runId || (!tool.runId && tool.source === source)) {
      delete state.activeTools[key];
    }
  }
}

function deepestActiveAgent(activeAgents: Record<string, ActiveAgent>): ActiveAgent | undefined {
  const agents = Object.values(activeAgents);
  const agentsByRunId = new Map(agents.filter((agent) => agent.runId).map((agent) => [agent.runId, agent]));

  let deepest: ActiveAgent | undefined;
  let deepestDepth = -1;
  for (const agent of agents) {
    const depth = activeAgentDepth(agent, agentsByRunId);
    if (depth > deepestDepth) {
      deepest = agent;
      deepestDepth = depth;
    }
  }
  return deepest;
}

function activeAgentDepth(agent: ActiveAgent, agentsByRunId: Map<string, ActiveAgent>): number {
  let depth = 0;
  let parentRunId = agent.parentRunId;
  const seen = new Set<string>();

  while (parentRunId && !seen.has(parentRunId)) {
    seen.add(parentRunId);
    const parent = agentsByRunId.get(parentRunId);
    if (!parent) {
      break;
    }
    depth += 1;
    parentRunId = parent.parentRunId;
  }

  return depth;
}

function sourceLabel(source: string): string {
  if (!source || source === "main") {
    return "Main";
  }
  if (isAgentScopeRouterSource(source)) {
    return "DataPilot";
  }
  if (source === "navigation.workflow" || source === "navigation.workflow.resume") {
    return "Workflow";
  }
  if (source === "navigation.plan") {
    return "Plan";
  }
  if (source === "navigation.executor") {
    return "Executor";
  }
  return source;
}

function thinkingText(source: string, label: string): string {
  if (isAgentScopeRouterSource(source)) {
    return "正在思考";
  }
  return `[${label}] 正在思考`;
}

function isAgentScopeRouterSource(source: string): boolean {
  const normalized = source.trim().toLowerCase();
  return normalized === "agentscope" || normalized === "main-router-agent" || normalized === "mainrouteragent";
}

function toolStatus(payload: Record<string, unknown>): string {
  const status = normalizeText(payload.status);
  if (status) {
    return status;
  }
  return payload.ok === false ? "failed" : "completed";
}

function toolCompletionText(status: string, tool: string, elapsed: number): string {
  const elapsedText = `${elapsed.toFixed(1)}s`;
  if (status === "completed") {
    return `已调用工具 ${tool} ${elapsedText}`;
  }
  if (status === "interrupted") {
    return `工具 ${tool} 已中断 ${elapsedText}`;
  }
  return `工具 ${tool} 调用失败 ${elapsedText}`;
}

function elapsedSeconds(startedAt: number | undefined, endedAt: string | null | undefined): number {
  if (startedAt === undefined) {
    return 0;
  }
  return Math.max((timestampMs(endedAt) - startedAt) / 1000, 0);
}

function timestampMs(value: string | null | undefined): number {
  if (!value) {
    return Date.now();
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

function agentKey(runId: string, source: string): string {
  return runId || source || "main";
}

function toolKey(runId: string, callId: string): string {
  return `${runId}\u0000${callId}`;
}

function normalizeNullableText(value: unknown): string | null {
  const text = normalizeText(value);
  return text || null;
}

function normalizeText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}
