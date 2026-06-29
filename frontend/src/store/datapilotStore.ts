import { createStore } from "zustand/vanilla";

import type { AgentEvent, ChatMessageRecord, SessionDetail, SessionRecord } from "../api/types";
import { applyAgentEvent, createEmptyRunState, type RunState } from "./eventReducer";

export type SessionMode = "draft_new_session" | "active_session" | "history_session";
type OrderedTimelineItem = RunState["timeline"][number] & {
  createdAt?: string;
  sequence?: number;
};

let timelineSequence = 0;

export interface DataPilotStoreState {
  open: boolean;
  mode: SessionMode;
  currentSessionId: string | null;
  previousActiveSessionId: string | null;
  sessions: SessionRecord[];
  messages: ChatMessageRecord[];
  run: RunState;
  setOpen: (open: boolean) => void;
  enterDraft: () => void;
  setSessions: (sessions: SessionRecord[]) => void;
  setActiveSession: (session: SessionRecord) => void;
  refreshActiveSession: (session: SessionDetail) => void;
  restoreHistory: (session: SessionDetail | SessionRecord, messages?: ChatMessageRecord[]) => void;
  appendUserMessage: (message: ChatMessageRecord) => void;
  applyEvent: (event: AgentEvent) => void;
}

export type DataPilotStore = ReturnType<typeof createDataPilotStore>;

export function createDataPilotStore() {
  return createStore<DataPilotStoreState>((set) => ({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    sessions: [],
    messages: [],
    run: createEmptyRunState(),

    setOpen: (open) => set({ open }),

    setSessions: (sessions) => set({ sessions: [...sessions] }),

    enterDraft: () =>
      set((state) => ({
        mode: "draft_new_session",
        currentSessionId: null,
        previousActiveSessionId:
          state.mode === "active_session" ? state.currentSessionId : state.previousActiveSessionId,
        messages: [],
        run: createEmptyRunState(),
      })),

    setActiveSession: (session) =>
      set((state) => ({
        mode: "active_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
      })),

    refreshActiveSession: (session) =>
      set((state) => {
        if (state.mode !== "active_session" || state.currentSessionId !== session.id) {
          return {};
        }

        return {
          sessions: upsertSession(state.sessions, session),
          messages: mergeMessages(state.messages, session.messages),
        };
      }),

    restoreHistory: (session, messages) =>
      set((state) => ({
        mode: "history_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        messages: [...(messages ?? ("messages" in session ? session.messages : []))],
        run: createEmptyRunState(),
      })),

    appendUserMessage: (message) =>
      set((state) => ({
        messages: [...state.messages, message],
      })),

    applyEvent: (event) =>
      set((state) => {
        const run = cloneRunState(state.run);
        const timelineLength = run.timeline.length;
        applyAgentEvent(run, event);
        const createdAt = event.timestamp || new Date().toISOString();
        for (let index = timelineLength; index < run.timeline.length; index += 1) {
          const item = run.timeline[index] as OrderedTimelineItem;
          item.createdAt = createdAt;
          item.sequence = timelineSequence;
          timelineSequence += 1;
        }
        return { run };
      }),
  }));
}

export const datapilotStore = createDataPilotStore();

function upsertSession(sessions: SessionRecord[], session: SessionRecord): SessionRecord[] {
  const next = sessions.filter((item) => item.id !== session.id);
  return [session, ...next];
}

function mergeMessages(existing: ChatMessageRecord[], persisted: ChatMessageRecord[]): ChatMessageRecord[] {
  const orderById = new Map<string, number>();
  let nextOrder = 0;
  const existingWithoutPersistedEchoes = existing.filter(
    (message) => !isLocalUserEchoOfPersistedMessage(message, persisted),
  );

  for (const message of [...existingWithoutPersistedEchoes, ...persisted]) {
    if (!orderById.has(message.id)) {
      orderById.set(message.id, nextOrder);
      nextOrder += 1;
    }
  }

  const byId = new Map<string, ChatMessageRecord>();
  for (const message of existingWithoutPersistedEchoes) {
    byId.set(message.id, message);
  }
  for (const message of persisted) {
    byId.set(message.id, message);
  }

  return [...byId.values()].sort((left, right) => compareMessages(left, right, orderById));
}

function isLocalUserEchoOfPersistedMessage(
  message: ChatMessageRecord,
  persisted: ChatMessageRecord[],
): boolean {
  if (!isLocalMessageId(message.id) || message.role !== "user") {
    return false;
  }
  return persisted.some(
    (candidate) =>
      candidate.role === message.role &&
      candidate.session_id === message.session_id &&
      candidate.content === message.content,
  );
}

function isLocalMessageId(messageId: string): boolean {
  return messageId.startsWith("local-");
}

function compareMessages(
  left: ChatMessageRecord,
  right: ChatMessageRecord,
  orderById: Map<string, number>,
): number {
  const leftTime = Date.parse(left.created_at);
  const rightTime = Date.parse(right.created_at);
  if (!Number.isNaN(leftTime) && !Number.isNaN(rightTime) && leftTime !== rightTime) {
    return leftTime - rightTime;
  }

  const createdAtOrder = left.created_at.localeCompare(right.created_at);
  if (createdAtOrder !== 0) {
    return createdAtOrder;
  }

  return (orderById.get(left.id) ?? 0) - (orderById.get(right.id) ?? 0);
}

function cloneRunState(run: RunState): RunState {
  return {
    timeline: run.timeline.map((item) => ({ ...item })),
    activeAgents: Object.fromEntries(
      Object.entries(run.activeAgents).map(([key, agent]) => [key, { ...agent }]),
    ),
    activeTools: Object.fromEntries(
      Object.entries(run.activeTools).map(([key, tool]) => [key, { ...tool }]),
    ),
    finalRunIds: { ...run.finalRunIds },
    activeText: run.activeText,
    activeStartedAt: run.activeStartedAt,
    running: run.running,
  };
}
