import { createStore } from "zustand/vanilla";

import type {
  ChatMessageRecord,
  PendingHumanDecision,
  PublicEventEnvelope,
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
        return {
          sessions: upsertSession(state.sessions, session),
          conversation: conversationFromDetail(session),
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

    applyEvent: (event) =>
      set((state) => {
        if (state.currentSessionId && event.session_id !== state.currentSessionId) {
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
      ? { ...conversation.pendingHumanDecision }
      : null,
  };
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
    left.planId === right.planId &&
    left.stepId === right.stepId &&
    left.recoveryRequired === right.recoveryRequired &&
    left.submissionDisabled === right.submissionDisabled &&
    left.recoveryEndpoint === right.recoveryEndpoint
  );
}
