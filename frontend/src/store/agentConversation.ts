import {
  EventType,
  type AgentEvent,
  type CustomEvent,
  type ReplyStartEvent,
  type RequireExternalExecutionEvent,
  type ToolCallStartEvent,
} from "@agentscope-ai/agentscope/event";
import {
  AssistantMsg,
  SystemMsg,
  UserMsg,
  appendEvent,
  type Msg,
  type ToolCallBlock,
} from "@agentscope-ai/agentscope/message";

import type {
  ChatMessageRecord,
  PendingHumanDecision,
  PublicEventEnvelope,
  PublicToolRun,
  PublicToolStatus,
} from "../api/types";

export type ReplyPhase = "idle" | "streaming" | "interrupting";

export interface AgentConversationState {
  messages: Msg[];
  phase: ReplyPhase;
  currentReplyId: string | null;
  lastSequence: number;
  toolRuns: Record<string, PublicToolRun>;
  pendingHumanDecision: PendingHumanDecision | null;
}

export interface AgentConversationSnapshot {
  messages: ChatMessageRecord[];
  events: PublicEventEnvelope[];
  toolRuns: PublicToolRun[];
  lastSequence: number;
}

export function createAgentConversation(): AgentConversationState {
  return {
    messages: [],
    phase: "idle",
    currentReplyId: null,
    lastSequence: 0,
    toolRuns: {},
    pendingHumanDecision: null,
  };
}

export function restoreAgentConversation(
  snapshot: AgentConversationSnapshot,
): AgentConversationState {
  const state = createAgentConversation();

  for (const entry of chronologicalSnapshotEntries(snapshot.messages, snapshot.events)) {
    if (entry.kind === "message") {
      appendPersistedMessage(state, entry.message);
    } else {
      applyPublicEvent(state, entry.envelope);
    }
  }
  for (const toolRun of snapshot.toolRuns) {
    state.toolRuns[toolRun.tool_call_id] = { ...toolRun };
  }
  state.lastSequence = Math.max(state.lastSequence, snapshot.lastSequence);
  return state;
}

type SnapshotEntry =
  | {
      kind: "message";
      message: ChatMessageRecord;
      createdAt: string;
      order: number;
    }
  | {
      kind: "event";
      envelope: PublicEventEnvelope;
      createdAt: string;
      order: number;
    };

function chronologicalSnapshotEntries(
  messages: ChatMessageRecord[],
  events: PublicEventEnvelope[],
): SnapshotEntry[] {
  return [
    ...messages.map((message, index): SnapshotEntry => ({
      kind: "message",
      message,
      createdAt: message.created_at,
      order: index,
    })),
    ...events.map((envelope): SnapshotEntry => ({
      kind: "event",
      envelope,
      createdAt: envelope.created_at,
      order: envelope.sequence,
    })),
  ].sort((left, right) => {
    const byTime = compareCreatedAt(left.createdAt, right.createdAt);
    if (byTime !== 0) {
      return byTime;
    }
    if (left.kind !== right.kind) {
      return left.kind === "message" ? -1 : 1;
    }
    return left.order - right.order;
  });
}

function compareCreatedAt(left: string, right: string): number {
  const leftTime = Date.parse(left);
  const rightTime = Date.parse(right);
  if (!Number.isNaN(leftTime) && !Number.isNaN(rightTime) && leftTime !== rightTime) {
    return leftTime - rightTime;
  }
  return left.localeCompare(right);
}

export function applyPublicEvent(
  state: AgentConversationState,
  envelope: PublicEventEnvelope,
): void {
  if (envelope.sequence <= state.lastSequence) {
    return;
  }

  const event = normalizePublicAgentEvent(envelope);
  if (event.type === EventType.REPLY_START) {
    startReply(state, event as ReplyStartEvent);
  } else if (event.type === EventType.REPLY_END) {
    const message = currentReply(state);
    if (message) {
      appendEvent(message, event);
    }
    state.currentReplyId = null;
    state.phase = "idle";
  } else if (event.type === EventType.CUSTOM) {
    applyDataPilotCustomEvent(state, envelope, event as CustomEvent);
  } else {
    const message = currentReply(state);
    if (message) {
      appendEvent(message, event);
    }
    if (event.type === EventType.TOOL_CALL_START) {
      projectRunningTool(state, envelope, event as ToolCallStartEvent);
    } else if (event.type === EventType.REQUIRE_EXTERNAL_EXECUTION) {
      projectExternalDecision(state, event as RequireExternalExecutionEvent);
    }
  }

  state.lastSequence = envelope.sequence;
}

export function markConversationInterrupting(state: AgentConversationState): void {
  if (state.phase === "streaming") {
    state.phase = "interrupting";
  }
}

export function appendPersistedMessage(
  state: AgentConversationState,
  message: ChatMessageRecord,
): void {
  const sdkMessage = persistedMessage(message);
  if (sdkMessage) {
    state.messages.push(sdkMessage);
  }
}

export function normalizePublicAgentEvent(envelope: PublicEventEnvelope): AgentEvent {
  const event = envelope.event;
  if (event.type === EventType.REPLY_START || event.type === EventType.REPLY_END) {
    return { ...event, session_id: envelope.session_id } as AgentEvent;
  }
  return event;
}

function startReply(state: AgentConversationState, event: ReplyStartEvent): void {
  state.messages.push(
    AssistantMsg({
      id: event.reply_id,
      name: "DataPilot",
      content: [],
      created_at: event.created_at,
    }),
  );
  state.currentReplyId = event.reply_id;
  state.phase = "streaming";
}

function currentReply(state: AgentConversationState): Msg | undefined {
  if (!state.currentReplyId) {
    return undefined;
  }
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (message.role === "assistant" && message.id === state.currentReplyId) {
      return message;
    }
  }
  return undefined;
}

function persistedMessage(message: ChatMessageRecord): Msg | null {
  const common = {
    id: message.id,
    content: message.content,
    created_at: message.created_at,
  };
  if (message.role === "user") {
    return UserMsg({ ...common, name: "You" });
  }
  if (message.role === "assistant") {
    return AssistantMsg({ ...common, name: "DataPilot" });
  }
  if (message.role === "system") {
    return SystemMsg({ ...common, name: "System" });
  }
  return null;
}

function projectRunningTool(
  state: AgentConversationState,
  envelope: PublicEventEnvelope,
  event: ToolCallStartEvent,
): void {
  const existing = state.toolRuns[event.tool_call_id];
  state.toolRuns[event.tool_call_id] = {
    session_id: envelope.session_id,
    tool_call_id: event.tool_call_id,
    tool_name: event.tool_call_name,
    status: "running",
    summary: existing?.summary ?? "",
    error_type: null,
    started_at: existing?.started_at ?? event.created_at,
    finished_at: null,
  };
}

function applyDataPilotCustomEvent(
  state: AgentConversationState,
  envelope: PublicEventEnvelope,
  event: CustomEvent,
): void {
  if (event.name === "datapilot_tool_terminal") {
    projectTerminalTool(state, envelope, event);
    return;
  }
  if (
    event.name === "human_decision_required" ||
    event.name === "datapilot_human_decision_required"
  ) {
    state.pendingHumanDecision = pendingDecision(event.value);
  }
}

function projectTerminalTool(
  state: AgentConversationState,
  envelope: PublicEventEnvelope,
  event: CustomEvent,
): void {
  const toolCallId = stringValue(event.value.tool_call_id);
  const status = toolStatus(event.value.status);
  if (!toolCallId || !status) {
    return;
  }

  const existing = state.toolRuns[toolCallId];
  state.toolRuns[toolCallId] = {
    session_id: existing?.session_id ?? envelope.session_id,
    tool_call_id: toolCallId,
    tool_name: existing?.tool_name ?? toolNameFromMessages(state.messages, toolCallId),
    status,
    summary: stringValue(event.value.summary),
    error_type: nullableString(event.value.error_type),
    started_at: existing?.started_at ?? event.created_at,
    finished_at: event.created_at,
  };
}

function projectExternalDecision(
  state: AgentConversationState,
  event: RequireExternalExecutionEvent,
): void {
  const decisionCall = event.tool_calls.find(
    (toolCall) => toolCall.name === "request_human_decision",
  );
  if (!decisionCall) {
    return;
  }
  const value = parseToolInput(decisionCall);
  state.pendingHumanDecision = pendingDecision({
    ...value,
    reply_id: event.reply_id,
    tool_call_id: decisionCall.id,
  });
}

function parseToolInput(toolCall: ToolCallBlock): Record<string, unknown> {
  try {
    const parsed = JSON.parse(toolCall.input) as unknown;
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function pendingDecision(value: Record<string, unknown>): PendingHumanDecision {
  const planId = stringValue(value.plan_id) || stringValue(value.planId);
  const stepId = stringValue(value.step_id) || stringValue(value.stepId);
  const recoveryEndpoint =
    stringValue(value.recovery_endpoint) || stringValue(value.recoveryEndpoint);
  return {
    replyId: stringValue(value.reply_id) || stringValue(value.replyId),
    toolCallId: stringValue(value.tool_call_id) || stringValue(value.toolCallId),
    requestId: stringValue(value.request_id) || stringValue(value.requestId),
    decisionType:
      stringValue(value.decision_type) || stringValue(value.decisionType) || "other",
    summary: stringValue(value.summary),
    ...(planId ? { planId } : {}),
    ...(stepId ? { stepId } : {}),
    ...(value.recovery_required === true || value.recoveryRequired === true
      ? { recoveryRequired: true }
      : {}),
    ...(value.submission_disabled === true || value.submissionDisabled === true
      ? { submissionDisabled: true }
      : {}),
    ...(recoveryEndpoint ? { recoveryEndpoint } : {}),
  };
}

function toolNameFromMessages(messages: Msg[], toolCallId: string): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const toolCall = messages[index].content.find(
      (block): block is ToolCallBlock => block.type === "tool_call" && block.id === toolCallId,
    );
    if (toolCall) {
      return toolCall.name;
    }
  }
  return "unknown_tool";
}

function toolStatus(value: unknown): PublicToolStatus | null {
  return value === "running" ||
    value === "success" ||
    value === "failure" ||
    value === "stopped"
    ? value
    : null;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function nullableString(value: unknown): string | null {
  const normalized = stringValue(value);
  return normalized || null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
