import {
  EventType,
  type AgentEvent,
  type CustomEvent,
  type ReplyStartEvent,
  type ExternalExecutionResultEvent,
  type RequireExternalExecutionEvent,
  type ToolCallStartEvent,
} from "@agentscope-ai/agentscope/event";
import {
  AssistantMsg,
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
  if (envelope.sequence > state.lastSequence + 1) {
    return;
  }

  const event = normalizePublicAgentEvent(envelope);
  if (isThinkingEvent(event)) {
    // Public backends suppress these events. Consume their sequence before
    // reply ownership checks as a defense against legacy/malicious streams;
    // never feed private reasoning content into the SDK message.
    state.lastSequence = envelope.sequence;
    return;
  }
  if (event.type === EventType.REPLY_START) {
    const start = event as ReplyStartEvent;
    if (!state.currentReplyId && stringValue(start.reply_id)) {
      startReply(state, start);
    }
  } else if (isReplyScopedEvent(event) && !state.currentReplyId) {
    return;
  } else if (!eventBelongsToCurrentReply(state, event)) {
    state.lastSequence = envelope.sequence;
    return;
  } else if (event.type === EventType.REPLY_END) {
    const message = currentReply(state);
    if (message) {
      appendEvent(message, event);
    }
    state.currentReplyId = null;
    state.phase = state.phase === "interrupting" && hasRunningTool(state)
      ? "interrupting"
      : "idle";
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
    } else if (event.type === EventType.EXTERNAL_EXECUTION_RESULT) {
      clearExternalDecision(state, event as ExternalExecutionResultEvent);
    }
  }

  state.lastSequence = envelope.sequence;
}

function isReplyScopedEvent(event: AgentEvent): boolean {
  return "reply_id" in event;
}

function eventBelongsToCurrentReply(
  state: AgentConversationState,
  event: AgentEvent,
): boolean {
  if (!("reply_id" in event)) {
    return true;
  }
  return Boolean(
    state.currentReplyId &&
      stringValue(event.reply_id) &&
      event.reply_id === state.currentReplyId,
  );
}

export function markConversationInterrupting(state: AgentConversationState): void {
  if (state.phase === "streaming" || hasRunningTool(state)) {
    state.phase = "interrupting";
  }
}

export function hasActiveExecution(state: AgentConversationState): boolean {
  return state.phase !== "idle" || hasRunningTool(state);
}

function hasRunningTool(state: AgentConversationState): boolean {
  return Object.values(state.toolRuns).some((run) => run.status === "running");
}

function isThinkingEvent(event: AgentEvent): boolean {
  return event.type === EventType.THINKING_BLOCK_START ||
    event.type === EventType.THINKING_BLOCK_DELTA ||
    event.type === EventType.THINKING_BLOCK_END;
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
  if (event.name === "datapilot_run_terminal") {
    if (state.currentReplyId) {
      return;
    }
    state.phase = state.phase === "interrupting" && hasRunningTool(state)
      ? "interrupting"
      : "idle";
    return;
  }
  if (event.name === "datapilot_tool_terminal") {
    projectTerminalTool(state, envelope, event);
    return;
  }
  if (
    event.name === "human_decision_required" ||
    event.name === "datapilot_human_decision_required"
  ) {
    const decision = customPendingDecision(event.value);
    if (decision) {
      state.pendingHumanDecision = decision;
    }
    return;
  }
  if (event.name === "datapilot_human_decision_resolved") {
    projectHumanDecisionResolution(state, event.value);
  }
}

function projectHumanDecisionResolution(
  state: AgentConversationState,
  value: Record<string, unknown>,
): void {
  const pending = state.pendingHumanDecision;
  if (!pending) {
    return;
  }
  const reason = stringValue(value.reason);
  if (value.all === true && reason === "stopped") {
    state.pendingHumanDecision = null;
    return;
  }
  const requestId = stringValue(value.request_id) || stringValue(value.requestId);
  if (reason === "submitted" && requestId && requestId === pending.requestId) {
    state.pendingHumanDecision = null;
  }
}

function projectTerminalTool(
  state: AgentConversationState,
  envelope: PublicEventEnvelope,
  event: CustomEvent,
): void {
  const toolCallId = stringValue(event.value.tool_call_id);
  const status = terminalToolStatus(event.value.status);
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
  if (
    state.phase === "interrupting" &&
    !state.currentReplyId &&
    !hasRunningTool(state)
  ) {
    state.phase = "idle";
  }
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
  const decision = nativePendingDecision({
    ...value,
    reply_id: event.reply_id,
    tool_call_id: decisionCall.id,
  });
  if (decision) {
    state.pendingHumanDecision = decision;
  }
}

function parseToolInput(toolCall: ToolCallBlock): Record<string, unknown> {
  try {
    const parsed = JSON.parse(toolCall.input) as unknown;
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function nativePendingDecision(
  value: Record<string, unknown>,
): PendingHumanDecision | null {
  const replyId = stringValue(value.reply_id) || stringValue(value.replyId);
  const toolCallId = stringValue(value.tool_call_id) || stringValue(value.toolCallId);
  const requestId = stringValue(value.request_id) || stringValue(value.requestId);
  const decisionType = stringValue(value.decision_type) || stringValue(value.decisionType);
  const summary = stringValue(value.summary);
  if (!replyId || !toolCallId || !requestId || !decisionType || !summary) {
    return null;
  }
  return buildPendingDecision(value, {
    replyId,
    toolCallId,
    requestId,
    decisionType,
    summary,
  });
}

function customPendingDecision(
  value: Record<string, unknown>,
): PendingHumanDecision | null {
  const replyId = stringValue(value.reply_id) || stringValue(value.replyId);
  const toolCallId = stringValue(value.tool_call_id) || stringValue(value.toolCallId);
  const requestId = stringValue(value.request_id) || stringValue(value.requestId);
  const decisionType = stringValue(value.decision_type) || stringValue(value.decisionType);
  const prompt = stringValue(value.prompt);
  const options = stringArray(value.options);
  if (!replyId || !toolCallId || !requestId || !decisionType || !prompt || !options) {
    return null;
  }
  return buildPendingDecision(value, {
    replyId,
    toolCallId,
    requestId,
    decisionType,
    summary: prompt,
    options,
  });
}

function buildPendingDecision(
  value: Record<string, unknown>,
  required: Pick<
    PendingHumanDecision,
    "replyId" | "toolCallId" | "requestId" | "decisionType" | "summary"
  > & { options?: string[] },
): PendingHumanDecision {
  const planId = stringValue(value.plan_id) || stringValue(value.planId);
  const stepId = stringValue(value.step_id) || stringValue(value.stepId);
  const recoveryEndpoint =
    stringValue(value.recovery_endpoint) || stringValue(value.recoveryEndpoint);
  return {
    ...required,
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

function clearExternalDecision(
  state: AgentConversationState,
  event: ExternalExecutionResultEvent,
): void {
  const pending = state.pendingHumanDecision;
  if (!pending || pending.replyId !== event.reply_id) {
    return;
  }
  const matchingResult = event.execution_results.find(
    (result) => result.id === pending.toolCallId,
  );
  if (!matchingResult) {
    return;
  }
  const requestId = toolResultRequestId(matchingResult.output);
  if (requestId && requestId === pending.requestId) {
    state.pendingHumanDecision = null;
  }
}

function toolResultRequestId(
  output: string | Array<{ type: string; text?: string }>,
): string {
  const candidates =
    typeof output === "string"
      ? [output]
      : output.flatMap((block) => (block.type === "text" && block.text ? [block.text] : []));
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as unknown;
      if (isRecord(parsed)) {
        const requestId = stringValue(parsed.request_id) || stringValue(parsed.requestId);
        if (requestId) {
          return requestId;
        }
      }
    } catch {
      continue;
    }
  }
  return "";
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

function terminalToolStatus(value: unknown): PublicToolStatus | null {
  return value === "success" || value === "failure" || value === "stopped"
    ? value
    : null;
}

function stringArray(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }
  const items = value.map(stringValue);
  return items.every(Boolean) ? items : null;
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
