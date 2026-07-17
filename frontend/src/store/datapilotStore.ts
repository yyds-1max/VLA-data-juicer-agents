import { createStore } from "zustand/vanilla";

import type {
  AgentEvent,
  ChatMessageRecord,
  PendingHumanDecision,
  SessionDetail,
  SessionRecord,
  TimelineEventRecord,
  TurnRecord,
} from "../api/types";
import { applyAgentEvent, createEmptyRunState, type RunState } from "./eventReducer";

export type SessionMode = "draft_new_session" | "active_session" | "history_session";
export type DataPilotInvocationStatus =
  | "queued"
  | "submitting"
  | "submitted"
  | "failed"
  | "blocked";

export interface DataPilotInvocation {
  invocationId: string;
  message: string;
  status: DataPilotInvocationStatus;
  sessionId?: string;
  error?: string;
}

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
  knownRunningSessionId: string | null;
  sessions: SessionRecord[];
  messages: ChatMessageRecord[];
  turns: TurnRecord[];
  run: RunState;
  pendingInvocation: DataPilotInvocation | null;
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
  discardLocalMessage: (messageId: string) => void;
  discardLocalTurn: (turnId: string) => void;
  adoptTurnId: (localTurnId: string, turnId: string) => void;
  launchDataPilotRequest: (invocationId: string, message: string) => boolean;
  claimDataPilotInvocation: (invocationId: string) => boolean;
  setDataPilotInvocationSession: (invocationId: string, sessionId: string) => void;
  completeDataPilotInvocation: (invocationId: string) => void;
  failDataPilotInvocation: (invocationId: string, error: string) => void;
  blockDataPilotInvocation: (invocationId: string, error: string) => void;
  retryDataPilotInvocation: (invocationId: string) => boolean;
  clearDataPilotInvocation: (invocationId?: string) => void;
  updateKnownRunningSession: (sessionId: string, running: boolean) => void;
  applyEvent: (event: AgentEvent) => void;
  clearPendingHumanDecision: (
    expectedDecision: PendingHumanDecision,
    expectedSessionId: string | null,
  ) => void;
}

export type DataPilotStore = ReturnType<typeof createDataPilotStore>;

export function createDataPilotStore() {
  return createStore<DataPilotStoreState>((set, get) => ({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    knownRunningSessionId: null,
    sessions: [],
    messages: [],
    turns: [],
    run: createEmptyRunState(),
    pendingInvocation: null,
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
        turns: [],
        run: createEmptyRunState(),
      })),

    setActiveSession: (session) =>
      set((state) => ({
        mode: "active_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        messages: [],
        turns: [],
        run: createEmptyRunState(),
      })),

    refreshActiveSession: (session) =>
      set((state) => {
        if (state.mode !== "active_session" || state.currentSessionId !== session.id) {
          return {};
        }

        const turns = mergeTurns(state.turns, session.turns ?? []);
        const run = session.events?.length ? mergeRunFromEvents(state.run, session.events) : state.run;
        const running = session.turns
          ? hasRunningTurn(session.turns)
          : run.running || hasRunningTurn(turns);
        return {
          sessions: upsertSession(state.sessions, session),
          messages: mergeMessages(state.messages, session.messages),
          turns,
          ...(run !== state.run ? { run } : {}),
          knownRunningSessionId: running
            ? session.id
            : state.knownRunningSessionId === session.id
              ? null
              : state.knownRunningSessionId,
        };
      }),

    restoreActiveSession: (session, messages) =>
      set((state) => {
        const turns = "turns" in session ? [...(session.turns ?? [])] : [];
        const run = runFromEvents("events" in session ? (session.events ?? []) : []);
        const running = hasRunningTurn(turns) || run.running;
        return {
          mode: "active_session",
          currentSessionId: session.id,
          previousActiveSessionId: null,
          sessions: upsertSession(state.sessions, session),
          messages: [...(messages ?? ("messages" in session ? session.messages : []))],
          turns,
          run,
          knownRunningSessionId: running
            ? session.id
            : state.knownRunningSessionId === session.id
              ? null
              : state.knownRunningSessionId,
        };
      }),

    restoreHistory: (session, messages) =>
      set((state) => ({
        mode: "history_session",
        currentSessionId: session.id,
        previousActiveSessionId: null,
        sessions: upsertSession(state.sessions, session),
        messages: [...(messages ?? ("messages" in session ? session.messages : []))],
        turns: "turns" in session ? [...(session.turns ?? [])] : [],
        run: runFromEvents("events" in session ? (session.events ?? []) : []),
      })),

    appendUserMessage: (message) =>
      set((state) => ({
        messages: [...state.messages, message],
      })),

    discardLocalMessage: (messageId) =>
      set((state) => ({
        messages: state.messages.filter(
          (message) => message.id !== messageId || !isLocalMessageId(message.id),
        ),
      })),

    discardLocalTurn: (turnId) =>
      set((state) => ({
        turns: state.turns.filter((turn) => turn.id !== turnId),
        run: removeRunTurn(state.run, turnId),
      })),

    adoptTurnId: (localTurnId, turnId) =>
      set((state) => ({
        messages: state.messages.map((message) =>
          message.turn_id === localTurnId ? { ...message, turn_id: turnId } : message,
        ),
        turns: mergeAdoptedTurn(state.turns, localTurnId, turnId),
        run: remapRunTurnId(state.run, localTurnId, turnId),
      })),

    launchDataPilotRequest: (invocationId, message) => {
      const current = get().pendingInvocation;
      if (current?.status === "queued" || current?.status === "submitting") {
        set({ open: true });
        return false;
      }
      set({
        open: true,
        pendingInvocation: { invocationId, message, status: "queued" },
      });
      return true;
    },

    claimDataPilotInvocation: (invocationId) => {
      const current = get().pendingInvocation;
      if (current?.invocationId !== invocationId || current.status !== "queued") {
        return false;
      }
      set({ pendingInvocation: { ...current, status: "submitting", error: undefined } });
      return true;
    },

    setDataPilotInvocationSession: (invocationId, sessionId) =>
      set((state) =>
        state.pendingInvocation?.invocationId === invocationId
          ? { pendingInvocation: { ...state.pendingInvocation, sessionId } }
          : {},
      ),

    completeDataPilotInvocation: (invocationId) =>
      set((state) =>
        state.pendingInvocation?.invocationId === invocationId
          ? {
              pendingInvocation: {
                ...state.pendingInvocation,
                status: "submitted",
                error: undefined,
              },
            }
          : {},
      ),

    failDataPilotInvocation: (invocationId, error) =>
      set((state) =>
        state.pendingInvocation?.invocationId === invocationId
          ? {
              pendingInvocation: {
                ...state.pendingInvocation,
                status: "failed",
                error,
              },
            }
          : {},
      ),

    blockDataPilotInvocation: (invocationId, error) =>
      set((state) =>
        state.pendingInvocation?.invocationId === invocationId
          ? {
              pendingInvocation: {
                ...state.pendingInvocation,
                status: "blocked",
                error,
              },
            }
          : {},
      ),

    retryDataPilotInvocation: (invocationId) => {
      const current = get().pendingInvocation;
      if (
        current?.invocationId !== invocationId ||
        (current.status !== "failed" && current.status !== "blocked")
      ) {
        return false;
      }
      set({
        open: true,
        pendingInvocation: { ...current, status: "queued", error: undefined },
      });
      return true;
    },

    clearDataPilotInvocation: (invocationId) =>
      set((state) => {
        if (invocationId && state.pendingInvocation?.invocationId !== invocationId) {
          return {};
        }
        return { pendingInvocation: null };
      }),

    updateKnownRunningSession: (sessionId, running) =>
      set((state) => ({
        knownRunningSessionId: running
          ? sessionId
          : state.knownRunningSessionId === sessionId
            ? null
            : state.knownRunningSessionId,
      })),

    applyEvent: (event) =>
      set((state) => {
        const run = cloneRunState(state.run);
        applyLiveEvent(run, event);
        const turns = applyTurnEvent(state.turns, event, state.currentSessionId);
        const running = run.running || hasRunningTurn(turns);
        return {
          run,
          turns,
          knownRunningSessionId: state.currentSessionId
            ? running
              ? state.currentSessionId
              : state.knownRunningSessionId === state.currentSessionId
                ? null
                : state.knownRunningSessionId
            : state.knownRunningSessionId,
        };
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

function hasRunningTurn(turns: TurnRecord[]): boolean {
  return turns.some((turn) => turn.status === "running" || turn.status === "waiting");
}

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
    timeline: run.timeline.map((item) => ({
      ...item,
      ...(item.activitySteps
        ? { activitySteps: item.activitySteps.map((step) => ({ ...step })) }
        : {}),
    })),
    activeAgents: Object.fromEntries(
      Object.entries(run.activeAgents).map(([key, agent]) => [key, { ...agent }]),
    ),
    activeTools: Object.fromEntries(
      Object.entries(run.activeTools).map(([key, tool]) => [key, { ...tool }]),
    ),
    pendingHumanDecision: run.pendingHumanDecision ? { ...run.pendingHumanDecision } : null,
    activeText: run.activeText,
    activeStartedAt: run.activeStartedAt,
    running: run.running,
    interrupting: run.interrupting,
    appliedEventKeys: { ...run.appliedEventKeys },
    terminalProgress: { ...run.terminalProgress },
  };
}

function mergeTurns(existing: TurnRecord[], persisted: TurnRecord[]): TurnRecord[] {
  const byId = new Map(existing.map((turn) => [turn.id, turn]));
  for (const turn of persisted) {
    byId.set(turn.id, turn);
  }
  return [...byId.values()].sort((left, right) => left.started_at.localeCompare(right.started_at));
}

function applyTurnEvent(
  turns: TurnRecord[],
  event: AgentEvent,
  sessionId: string | null,
): TurnRecord[] {
  const turnId = typeof event.turn_id === "string" ? event.turn_id : "";
  if (!turnId || (event.type !== "turn_start" && event.type !== "turn_state")) {
    return turns;
  }
  const payload = event.payload ?? {};
  const existing = turns.find((turn) => turn.id === turnId);
  const status = typeof payload.status === "string" ? payload.status as TurnRecord["status"] : existing?.status ?? "running";
  const startedAt = typeof payload.started_at === "string"
    ? payload.started_at
    : existing?.started_at ?? event.timestamp ?? new Date().toISOString();
  const finishedAt = typeof payload.finished_at === "string"
    ? payload.finished_at
    : existing?.finished_at ?? null;
  const next: TurnRecord = {
    id: turnId,
    web_session_id: existing?.web_session_id ?? sessionId ?? "",
    origin: existing?.origin ?? "user",
    status,
    started_at: startedAt,
    finished_at: finishedAt,
    final_message_id: existing?.final_message_id ?? null,
  };
  return mergeTurns(turns, [next]);
}

function mergeAdoptedTurn(turns: TurnRecord[], localId: string, serverId: string): TurnRecord[] {
  const local = turns.find((turn) => turn.id === localId);
  const server = turns.find((turn) => turn.id === serverId);
  const remaining = turns.filter((turn) => turn.id !== localId && turn.id !== serverId);
  if (!local && !server) {
    return turns;
  }
  const merged = {
    ...(local ?? server)!,
    ...(server ?? {}),
    id: serverId,
    started_at: [local?.started_at, server?.started_at].filter(Boolean).sort()[0]!,
  };
  return mergeTurns(remaining, [merged]);
}

function remapRunTurnId(run: RunState, localId: string, serverId: string): RunState {
  const next = cloneRunState(run);
  let keptInitialProgress = false;
  next.timeline = next.timeline
    .map((item) => item.turnId === localId ? { ...item, turnId: serverId } : item)
    .filter((item) => {
      if (item.turnId !== serverId || item.kind !== "progress" || item.text !== "正在理解你的请求") {
        return true;
      }
      if (keptInitialProgress) return false;
      keptInitialProgress = true;
      return true;
    });
  next.activeTools = Object.fromEntries(
    Object.entries(next.activeTools).map(([key, tool]) => [
      key.replace(`${localId}\u0000`, `${serverId}\u0000`),
      tool.turnId === localId ? { ...tool, turnId: serverId } : tool,
    ]),
  );
  return next;
}

function removeRunTurn(run: RunState, turnId: string): RunState {
  const next = cloneRunState(run);
  next.timeline = next.timeline.filter((item) => item.turnId !== turnId);
  next.activeTools = Object.fromEntries(
    Object.entries(next.activeTools).filter(([, tool]) => tool.turnId !== turnId),
  );
  next.terminalProgress = Object.fromEntries(
    Object.entries(next.terminalProgress).filter(
      ([key]) => !key.startsWith(`${turnId}\u0000`),
    ),
  );
  return next;
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
  const recoveryUpgrade = isPendingHumanDecisionRecoveryUpgrade(run, event);
  if (persistedKey && run.appliedEventKeys[persistedKey] && !recoveryUpgrade) {
    return;
  }
  const liveKey = liveEventKey(event);
  if (run.appliedEventKeys[liveKey] && !recoveryUpgrade) {
    if (persistedKey) {
      run.appliedEventKeys[persistedKey] = true;
    }
    return;
  }
  applyEventAndMark(run, event, persistedKey ?? liveKey);
}

function isPendingHumanDecisionRecoveryUpgrade(
  run: RunState,
  event: AgentEvent | TimelineEventRecord,
): boolean {
  const current = run.pendingHumanDecision;
  if (!current || current.recoveryRequired || event.type !== "human_decision_required") {
    return false;
  }
  const payload = event.payload ?? {};
  if (!(payload.recovery_required === true || payload.recoveryRequired === true)) {
    return false;
  }
  const replyId = stringField(payload.reply_id) || stringField(payload.replyId);
  const toolCallId = stringField(payload.tool_call_id) || stringField(payload.toolCallId);
  return current.replyId === replyId && current.toolCallId === toolCallId;
}

function stringField(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function applyLiveEvent(run: RunState, event: AgentEvent): void {
  const record = event as AgentEvent & { id?: string; seq?: number };
  if (record.id || typeof record.seq === "number") {
    applyEventIfNew(run, event);
  } else {
    applyEventAndMark(run, event, liveEventKey(event));
  }
}

function applyEventAndMark(run: RunState, event: AgentEvent | TimelineEventRecord, key: string): void {
  const existingItems = new Set(run.timeline);
  applyAgentEvent(run, event);
  run.appliedEventKeys[key] = true;
  stampNewTimelineItems(run, event, existingItems);
}

function stampNewTimelineItems(
  run: RunState,
  event: AgentEvent | TimelineEventRecord,
  existingItems: Set<RunState["timeline"][number]>,
): void {
  const record = event as Partial<TimelineEventRecord>;
  const createdAt = event.timestamp || record.created_at || new Date().toISOString();
  const sequence = typeof record.seq === "number" ? record.seq : timelineSequence;
  for (const candidate of run.timeline) {
    if (existingItems.has(candidate)) {
      continue;
    }
    const item = candidate as OrderedTimelineItem;
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
