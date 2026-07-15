import { createStore } from "zustand/vanilla";

import type {
  ChatMessageRecord,
  PendingHumanDecision,
  PublicEventEnvelope,
  PublicToolRun,
  SessionDetail,
  SessionRecord,
} from "../api/types";
import {
  appendPersistedMessage,
  applyPublicEvent,
  createAgentConversation,
  markConversationInterrupting,
  restoreAgentConversation,
  type AgentConversationState,
} from "./agentConversation";

export type SessionMode = "draft_new_session" | "active_session";

export interface DataPilotStoreState {
  open: boolean;
  mode: SessionMode;
  currentSessionId: string | null;
  previousActiveSessionId: string | null;
  sessions: SessionRecord[];
  conversation: AgentConversationState;
  floatingOffset: { x: number; y: number };
  setOpen: (open: boolean) => void;
  setFloatingOffset: (offset: { x: number; y: number }) => void;
  enterDraft: () => void;
  setSessions: (sessions: SessionRecord[]) => void;
  setActiveSession: (session: SessionRecord) => void;
  refreshActiveSession: (session: SessionDetail) => void;
  restoreSession: (session: SessionDetail) => void;
  appendUserMessage: (message: ChatMessageRecord) => void;
  removeOptimisticUserMessage: (expectedSessionId: string, messageId: string) => void;
  applyEvent: (event: PublicEventEnvelope) => void;
  markInterrupting: () => void;
  clearPendingHumanDecision: (
    expectedDecision: PendingHumanDecision,
    expectedSessionId: string | null,
  ) => void;
}

export type DataPilotStore = ReturnType<typeof createDataPilotStore>;

export function createDataPilotStore() {
  return createStore<DataPilotStoreState>((set) => ({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    sessions: [],
    conversation: createAgentConversation(),
    floatingOffset: { x: 0, y: 0 },

    setOpen: (open) => set({ open }),

    setFloatingOffset: (floatingOffset) => set({ floatingOffset }),

    setSessions: (sessions) => set({ sessions: [...sessions] }),

    enterDraft: () =>
      set((state) => ({
        mode: "draft_new_session",
        currentSessionId: null,
        previousActiveSessionId:
          state.mode === "active_session" ? state.currentSessionId : state.previousActiveSessionId,
        conversation: createAgentConversation(),
      })),

    setActiveSession: (session) =>
      set((state) => ({
        mode: "active_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        conversation:
          state.currentSessionId === session.id
            ? state.conversation
            : createAgentConversation(),
      })),

    refreshActiveSession: (session) =>
      set((state) => {
        if (state.mode !== "active_session" || state.currentSessionId !== session.id) {
          return {};
        }
        if (session.last_sequence < state.conversation.lastSequence) {
          return { sessions: upsertSession(state.sessions, session) };
        }
        if (session.last_sequence === state.conversation.lastSequence) {
          const conversation = cloneConversation(state.conversation);
          conversation.messages = conversationFromDetailPreservingOptimistic(
            session,
            state.conversation,
          ).messages;
          mergeAuthoritativeToolRuns(conversation, session.tool_runs);
          return {
            sessions: upsertSession(state.sessions, session),
            conversation,
          };
        }
        return {
          sessions: upsertSession(state.sessions, session),
          conversation: conversationFromDetailPreservingOptimistic(
            session,
            state.conversation,
          ),
        };
      }),

    restoreSession: (session) =>
      set((state) => ({
        mode: "active_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        conversation: conversationFromDetail(session),
      })),

    appendUserMessage: (message) =>
      set((state) => {
        const conversation = cloneConversation(state.conversation);
        appendPersistedMessage(conversation, message);
        return { conversation };
      }),

    removeOptimisticUserMessage: (expectedSessionId, messageId) =>
      set((state) => {
        if (
          state.mode !== "active_session" ||
          state.currentSessionId !== expectedSessionId
        ) {
          return {};
        }
        const messageIndex = state.conversation.messages.findIndex(
          (message) => message.id === messageId && message.role === "user",
        );
        if (messageIndex < 0) {
          return {};
        }
        const conversation = cloneConversation(state.conversation);
        conversation.messages.splice(messageIndex, 1);
        return { conversation };
      }),

    applyEvent: (event) =>
      set((state) => {
        if (
          !state.open ||
          state.mode !== "active_session" ||
          !state.currentSessionId ||
          event.session_id !== state.currentSessionId
        ) {
          return {};
        }
        const conversation = cloneConversation(state.conversation);
        applyPublicEvent(conversation, event);
        return { conversation };
      }),

    markInterrupting: () =>
      set((state) => {
        const conversation = cloneConversation(state.conversation);
        markConversationInterrupting(conversation);
        return { conversation };
      }),

    clearPendingHumanDecision: (expectedDecision, expectedSessionId) =>
      set((state) => {
        if (
          state.currentSessionId !== expectedSessionId ||
          !samePendingHumanDecision(
            state.conversation.pendingHumanDecision,
            expectedDecision,
          )
        ) {
          return {};
        }
        const conversation = cloneConversation(state.conversation);
        conversation.pendingHumanDecision = null;
        return { conversation };
      }),
  }));
}

export const datapilotStore = createDataPilotStore();

function conversationFromDetail(session: SessionDetail): AgentConversationState {
  return restoreAgentConversation({
    messages: session.messages,
    events: session.events,
    toolRuns: session.tool_runs,
    lastSequence: session.last_sequence,
  });
}

function conversationFromDetailPreservingOptimistic(
  session: SessionDetail,
  current: AgentConversationState,
): AgentConversationState {
  const rebuilt = conversationFromDetail(session);
  const optimistic = current.messages.filter(
    (message) => message.role === "user" && message.id.startsWith("local-"),
  );
  if (optimistic.length === 0) {
    return rebuilt;
  }

  for (const message of optimistic) {
    if (rebuilt.messages.some((candidate) => candidate.id === message.id)) {
      continue;
    }

    const currentIndex = current.messages.findIndex(
      (candidate) => candidate.id === message.id,
    );
    let insertionIndex = 0;
    for (let index = currentIndex - 1; index >= 0; index -= 1) {
      const anchorIndex = rebuilt.messages.findIndex(
        (candidate) => candidate.id === current.messages[index].id,
      );
      if (anchorIndex >= 0) {
        insertionIndex = anchorIndex + 1;
        break;
      }
    }
    rebuilt.messages.splice(insertionIndex, 0, structuredClone(message));
  }
  return rebuilt;
}

function cloneConversation(conversation: AgentConversationState): AgentConversationState {
  return {
    messages: conversation.messages.map((message) => structuredClone(message)),
    phase: conversation.phase,
    currentReplyId: conversation.currentReplyId,
    lastSequence: conversation.lastSequence,
    toolRuns: Object.fromEntries(
      Object.entries(conversation.toolRuns).map(([key, run]) => [key, { ...run }]),
    ),
    pendingHumanDecision: conversation.pendingHumanDecision
      ? {
          ...conversation.pendingHumanDecision,
          options: conversation.pendingHumanDecision.options
            ? [...conversation.pendingHumanDecision.options]
            : undefined,
        }
      : null,
  };
}

function mergeAuthoritativeToolRuns(
  conversation: AgentConversationState,
  toolRuns: PublicToolRun[],
): void {
  for (const run of toolRuns) {
    const existing = conversation.toolRuns[run.tool_call_id];
    if (existing && isTerminalToolStatus(existing.status) && run.status === "running") {
      continue;
    }
    conversation.toolRuns[run.tool_call_id] = { ...run };
  }
}

function isTerminalToolStatus(status: PublicToolRun["status"]): boolean {
  return status === "success" || status === "failure" || status === "stopped";
}

function upsertSession(sessions: SessionRecord[], session: SessionRecord): SessionRecord[] {
  return [session, ...sessions.filter((item) => item.id !== session.id)];
}

function samePendingHumanDecision(
  left: PendingHumanDecision | null,
  right: PendingHumanDecision | null,
): boolean {
  if (!left || !right) {
    return false;
  }
  return (
    left.replyId === right.replyId &&
    left.toolCallId === right.toolCallId &&
    left.requestId === right.requestId &&
    left.decisionType === right.decisionType &&
    left.summary === right.summary &&
    sameStringArray(left.options, right.options) &&
    left.planId === right.planId &&
    left.stepId === right.stepId &&
    left.recoveryRequired === right.recoveryRequired &&
    left.submissionDisabled === right.submissionDisabled &&
    left.recoveryEndpoint === right.recoveryEndpoint
  );
}

function sameStringArray(left?: string[], right?: string[]): boolean {
  if (!left || !right) {
    return left === right;
  }
  return left.length === right.length && left.every((value, index) => value === right[index]);
}
