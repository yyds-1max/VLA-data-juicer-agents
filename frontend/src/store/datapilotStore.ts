import { createStore } from "zustand/vanilla";

import type {
  AgentEvent,
  ChatMessageRecord,
  PendingHumanDecision,
  SessionDetail,
  SessionRecord,
  TimelineEventRecord,
} from "../api/types";
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
  floatingOffset: { x: number; y: number };
  setOpen: (open: boolean) => void;
  setFloatingOffset: (offset: { x: number; y: number }) => void;
  enterDraft: () => void;
  setSessions: (sessions: SessionRecord[]) => void;
  setActiveSession: (session: SessionRecord) => void;
  refreshActiveSession: (session: SessionDetail) => void;
  restoreActiveSession: (session: SessionDetail | SessionRecord, messages?: ChatMessageRecord[]) => void;
  restoreHistory: (session: SessionDetail | SessionRecord, messages?: ChatMessageRecord[]) => void;
  appendUserMessage: (message: ChatMessageRecord) => void;
  applyEvent: (event: AgentEvent) => void;
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
    messages: [],
    run: createEmptyRunState(),
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
          ...(session.events?.length ? { run: mergeRunFromEvents(state.run, session.events) } : {}),
        };
      }),

    restoreActiveSession: (session, messages) =>
      set((state) => ({
        mode: "active_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        messages: [...(messages ?? ("messages" in session ? session.messages : []))],
        run: runFromEvents("events" in session ? (session.events ?? []) : []),
      })),

    restoreHistory: (session, messages) =>
      set((state) => ({
        mode: "history_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        messages: [...(messages ?? ("messages" in session ? session.messages : []))],
        run: runFromEvents("events" in session ? (session.events ?? []) : []),
      })),

    appendUserMessage: (message) =>
      set((state) => ({
        messages: [...state.messages, message],
      })),

    applyEvent: (event) =>
      set((state) => {
        const run = cloneRunState(state.run);
        applyLiveEvent(run, event);
        return { run };
      }),

    clearPendingHumanDecision: (expectedDecision, expectedSessionId) =>
      set((state) => {
        if (
          state.currentSessionId !== expectedSessionId ||
          !samePendingHumanDecision(state.run.pendingHumanDecision, expectedDecision)
        ) {
          return {};
        }
        const run = cloneRunState(state.run);
        run.pendingHumanDecision = null;
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
    pendingHumanDecision: run.pendingHumanDecision ? { ...run.pendingHumanDecision } : null,
    activeText: run.activeText,
    activeStartedAt: run.activeStartedAt,
    running: run.running,
    interrupting: run.interrupting,
    appliedEventKeys: { ...run.appliedEventKeys },
  };
}

function runFromEvents(events: TimelineEventRecord[]): RunState {
  const run = createEmptyRunState();
  for (const record of events) {
    applyEventIfNew(run, record);
  }
  return run;
}

function mergeRunFromEvents(run: RunState, events: TimelineEventRecord[]): RunState {
  const next = cloneRunState(run);
  for (const record of events) {
    applyEventIfNew(next, record);
  }
  return next;
}

function applyEventIfNew(run: RunState, event: AgentEvent | TimelineEventRecord): void {
  const record = event as Partial<TimelineEventRecord>;
  const persistedKey = persistedEventKey(record);
  if (persistedKey && run.appliedEventKeys[persistedKey]) {
    return;
  }
  const liveKey = liveEventKey(event);
  if (run.appliedEventKeys[liveKey]) {
    if (persistedKey) {
      run.appliedEventKeys[persistedKey] = true;
    }
    return;
  }
  applyEventAndMark(run, event, persistedKey ?? liveKey);
}

function applyLiveEvent(run: RunState, event: AgentEvent): void {
  applyEventAndMark(run, event, liveEventKey(event));
}

function applyEventAndMark(run: RunState, event: AgentEvent | TimelineEventRecord, key: string): void {
  const timelineLength = run.timeline.length;
  applyAgentEvent(run, event);
  run.appliedEventKeys[key] = true;
  stampNewTimelineItems(run, event, timelineLength);
}

function stampNewTimelineItems(
  run: RunState,
  event: AgentEvent | TimelineEventRecord,
  timelineLength: number,
): void {
  const record = event as Partial<TimelineEventRecord>;
  const createdAt = event.timestamp || record.created_at || new Date().toISOString();
  const sequence = typeof record.seq === "number" ? record.seq : timelineSequence;
  for (let index = timelineLength; index < run.timeline.length; index += 1) {
    const item = run.timeline[index] as OrderedTimelineItem;
    item.createdAt = createdAt;
    item.sequence = sequence;
    if (typeof record.seq !== "number") {
      timelineSequence += 1;
    }
  }
}

function liveEventKey(event: AgentEvent | TimelineEventRecord): string {
  return `live:${[
    event.type,
    event.source ?? "",
    event.run_id ?? "",
    event.parent_run_id ?? "",
    event.timestamp ?? "",
    stableStringify(event.payload ?? {}),
  ].join("\u0001")}`;
}

function persistedEventKey(event: Partial<TimelineEventRecord>): string | null {
  if (event.id) {
    return `persisted:${event.id}`;
  }
  if (event.session_id && typeof event.seq === "number") {
    return `persisted:${event.session_id}:${event.seq}`;
  }
  return null;
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
    .join(",")}}`;
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
    left.requestId === right.requestId
  );
}
