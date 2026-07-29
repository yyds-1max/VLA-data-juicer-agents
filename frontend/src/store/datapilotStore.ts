import { createStore } from "zustand/vanilla";

import type {
  AgentEvent,
  ChatMessageRecord,
  PendingInteraction,
  SessionDetail,
  SessionEntrypoint,
  SessionRecord,
  SessionRequestContext,
  TaskSnapshot,
  TimelineEventRecord,
  TurnRecord,
} from "../api/types";
import { applyAgentEvent, createEmptyRunState, type RunState } from "./eventReducer";

export type SessionMode = "draft_new_session" | "active_session" | "history_session";
export type DataPilotInvocationStatus =
  | "queued"
  | "submitting"
  | "submitted"
  | "failed";

export interface DataPilotInvocation {
  invocationId: string;
  message: string;
  requestContext: SessionRequestContext;
  entrypoint: Exclude<SessionEntrypoint, "chat">;
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
  sessions: SessionRecord[];
  messages: ChatMessageRecord[];
  turns: TurnRecord[];
  tasks: TaskSnapshot[];
  pendingInteraction: PendingInteraction | null;
  lastEventSeq: number;
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
  launchDataPilotRequest: (
    invocationId: string,
    message: string,
    requestContext: SessionRequestContext,
    entrypoint?: Exclude<SessionEntrypoint, "chat">,
  ) => boolean;
  claimDataPilotInvocation: (invocationId: string) => boolean;
  setDataPilotInvocationSession: (invocationId: string, sessionId: string) => void;
  completeDataPilotInvocation: (invocationId: string) => void;
  failDataPilotInvocation: (invocationId: string, error: string) => void;
  retryDataPilotInvocation: (invocationId: string) => boolean;
  clearDataPilotInvocation: (invocationId?: string) => void;
  applyEvent: (event: AgentEvent) => void;
}

export type DataPilotStore = ReturnType<typeof createDataPilotStore>;

export function createDataPilotStore() {
  return createStore<DataPilotStoreState>((set, get) => ({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    sessions: [],
    messages: [],
    turns: [],
    tasks: [],
    pendingInteraction: null,
    lastEventSeq: 0,
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
        tasks: [],
        pendingInteraction: null,
        lastEventSeq: 0,
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
        tasks: [],
        pendingInteraction: null,
        lastEventSeq: 0,
        run: createEmptyRunState(),
      })),

    refreshActiveSession: (session) =>
      set((state) => {
        if (state.mode !== "active_session" || state.currentSessionId !== session.id) {
          return {};
        }

        const turns = mergeTurns(state.turns, session.turns ?? []);
        const run = session.events?.length ? mergeRunFromEvents(state.run, session.events) : state.run;
        const snapshotSeq = sessionSnapshotSeq(session);
        const snapshotCanResolveInteraction = snapshotSeq >= state.lastEventSeq;
        return {
          sessions: upsertSession(state.sessions, session),
          messages: mergeMessages(state.messages, session.messages),
          turns,
          tasks: session.tasks ? mergeTasks(state.tasks, session.tasks) : state.tasks,
          pendingInteraction: session.pending_interaction !== undefined
            ? (
                snapshotCanResolveInteraction
                  ? preferPendingInteraction(state.pendingInteraction, session.pending_interaction)
                  : state.pendingInteraction
              )
            : state.pendingInteraction,
          lastEventSeq: Math.max(state.lastEventSeq, snapshotSeq),
          ...(run !== state.run ? { run } : {}),
        };
      }),

    restoreActiveSession: (session, messages) =>
      set((state) => {
        const turns = "turns" in session ? [...(session.turns ?? [])] : [];
        const run = runFromEvents("events" in session ? (session.events ?? []) : []);
        const lastEventSeq = "events" in session ? sessionSnapshotSeq(session) : 0;
        return {
          mode: "active_session",
          currentSessionId: session.id,
          previousActiveSessionId: null,
          sessions: upsertSession(state.sessions, session),
          messages: [...(messages ?? ("messages" in session ? session.messages : []))],
          turns,
          tasks: "tasks" in session ? [...(session.tasks ?? [])] : [],
          pendingInteraction: "pending_interaction" in session
            ? (session.pending_interaction ?? null)
            : null,
          lastEventSeq,
          run,
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
        tasks: "tasks" in session ? [...(session.tasks ?? [])] : [],
        pendingInteraction: "pending_interaction" in session
          ? (session.pending_interaction ?? null)
          : null,
        lastEventSeq: "events" in session ? sessionSnapshotSeq(session) : 0,
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

    launchDataPilotRequest: (
      invocationId,
      message,
      requestContext,
      entrypoint = "data_management_shortcut",
    ) => {
      const current = get().pendingInvocation;
      if (current?.status === "queued" || current?.status === "submitting") {
        set({ open: true });
        return false;
      }
      set({
        open: true,
        pendingInvocation: {
          invocationId,
          message,
          requestContext,
          entrypoint,
          status: "queued",
        },
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

    retryDataPilotInvocation: (invocationId) => {
      const current = get().pendingInvocation;
      if (
        current?.invocationId !== invocationId ||
        current.status !== "failed"
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

    applyEvent: (event) =>
      set((state) => {
        const reconciled = reconcileOptimisticTurn(state.messages, state.turns, state.run, event);
        const run = cloneRunState(reconciled.run);
        applyLiveEvent(run, event);
        const turns = applyTurnEvent(reconciled.turns, event, state.currentSessionId);
        const tasks = applyTaskEvent(state.tasks, event);
        const pendingInteraction = applyInteractionEvent(state.pendingInteraction, event);
        const eventSeq = typeof event.seq === "number" ? event.seq : 0;
        return {
          run,
          messages: reconciled.messages,
          turns,
          tasks,
          pendingInteraction,
          lastEventSeq: Math.max(state.lastEventSeq, eventSeq),
        };
      }),
  }));
}

export const datapilotStore = createDataPilotStore();

function applyTaskEvent(tasks: TaskSnapshot[], event: AgentEvent): TaskSnapshot[] {
  if (event.type !== "task_state_updated") return tasks;
  const payload = recordValue(event.payload?.task) ?? event.payload ?? {};
  const taskRef = textValue(payload.task_ref) || textValue(payload.taskRef);
  if (!taskRef) return tasks;
  const existing = tasks.find((task) => task.task_ref === taskRef);
  const status = navigationTaskStatus(payload.status) ?? existing?.status;
  const datasetDate = textValue(payload.dataset_date ?? payload.datasetDate) || existing?.dataset_date;
  const selection = navigationClipSelection(payload.selection) ?? existing?.selection;
  if (!status || !datasetDate || !selection) return tasks;
  const now = event.timestamp ?? new Date().toISOString();
  const countRecord = recordValue(payload.count);
  const count = countRecord
    ? {
        done: numberValue(countRecord.done),
        total: numberValue(countRecord.total),
        unit: textValue(countRecord.unit),
      }
    : existing?.count;
  const next: TaskSnapshot = {
    task_ref: taskRef,
    domain: textValue(payload.domain) || existing?.domain || "navigation",
    dataset_date: datasetDate,
    selection,
    scene_mode: optionalText(payload.scene_mode ?? payload.sceneMode, existing?.scene_mode) ?? null,
    status,
    phase: optionalText(payload.phase, existing?.phase),
    waiting_reason: optionalText(payload.waiting_reason ?? payload.waitingReason, existing?.waiting_reason),
    wait_cause: optionalText(payload.wait_cause ?? payload.waitCause, existing?.wait_cause),
    latest_public_update: optionalText(
      payload.latest_public_update ?? payload.latestPublicUpdate,
      existing?.latest_public_update,
    ),
    available_actions: stringArray(payload.available_actions ?? payload.availableActions)
      ?? existing?.available_actions,
    state_revision: numberValue(
      payload.state_revision ?? payload.stateRevision,
      existing?.state_revision ?? 0,
    ),
    started_at: textValue(payload.started_at ?? payload.startedAt) || existing?.started_at || now,
    updated_at: textValue(payload.updated_at ?? payload.updatedAt) || now,
    ...(count && count.total > 0 ? { count } : {}),
  };
  return mergeTasks(tasks, [next]);
}

function applyInteractionEvent(
  pending: PendingInteraction | null,
  event: AgentEvent,
): PendingInteraction | null {
  if (event.type === "interaction_resolved") {
    const interactionId = textValue(
      event.payload?.interaction_id ?? event.payload?.interactionId ??
      event.payload?.interaction_ref ?? event.payload?.interactionRef,
    );
    if (!interactionId) return pending;
    return pending?.interaction_id === interactionId ? null : pending;
  }
  if (event.type !== "interaction_required") return pending;
  const payload = recordValue(event.payload?.interaction) ?? event.payload ?? {};
  const interactionId = textValue(
    payload.interaction_id ?? payload.interactionId ?? payload.interaction_ref ?? payload.interactionRef,
  );
  const taskRef = textValue(payload.task_ref ?? payload.taskRef);
  const kind = interactionKind(payload.kind);
  const risk = interactionRisk(payload.risk);
  const options = interactionOptions(payload.options);
  if (!interactionId || !taskRef || !kind || !risk || options.length === 0) return pending;
  return preferPendingInteraction(pending, {
    interaction_id: interactionId,
    task_ref: taskRef,
    kind,
    blocking: payload.blocking === true,
    risk,
    title: textValue(payload.title) || "需要你的选择",
    summary: textValue(payload.summary),
    options,
    interaction_revision: numberValue(
      payload.interaction_revision ?? payload.interactionRevision,
    ),
    expected_task_revision: numberValue(
      payload.expected_task_revision ?? payload.expectedTaskRevision,
    ),
    expires_at: optionalText(payload.expires_at ?? payload.expiresAt) ?? null,
  });
}

function mergeTasks(existing: TaskSnapshot[], incoming: TaskSnapshot[]): TaskSnapshot[] {
  const byRef = new Map(existing.map((task) => [task.task_ref, task]));
  for (const task of incoming) {
    const current = byRef.get(task.task_ref);
    if (!current || task.state_revision >= current.state_revision) {
      byRef.set(task.task_ref, task);
    }
  }
  return [...byRef.values()].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

function preferPendingInteraction(
  current: PendingInteraction | null,
  incoming: PendingInteraction | null,
): PendingInteraction | null {
  if (!current || !incoming) return incoming;
  if (current.interaction_id === incoming.interaction_id) {
    return incoming.interaction_revision >= current.interaction_revision ? incoming : current;
  }
  if (incoming.expected_task_revision < current.expected_task_revision) {
    return current;
  }
  return incoming;
}

function sessionSnapshotSeq(session: Pick<SessionDetail, "events" | "snapshot_seq">): number {
  if (typeof session.snapshot_seq === "number" && Number.isFinite(session.snapshot_seq)) {
    return session.snapshot_seq;
  }
  return session.events.reduce(
    (maximum, event) => Math.max(maximum, event.seq),
    0,
  );
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function textValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function optionalText(value: unknown, fallback?: string | null): string | null | undefined {
  if (value === null) return null;
  const text = textValue(value);
  return text || fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.map(textValue).filter(Boolean);
}

function navigationTaskStatus(value: unknown): TaskSnapshot["status"] | undefined {
  const status = textValue(value);
  return [
    "active", "waiting_user", "pausing", "paused", "cancelling", "cancelled",
    "completed", "failed", "needs_replan", "superseded",
  ].includes(status) ? status as TaskSnapshot["status"] : undefined;
}

function navigationClipSelection(value: unknown): TaskSnapshot["selection"] | undefined {
  const selection = recordValue(value);
  const kind = textValue(selection?.kind);
  if (kind === "all_clips") return { kind };
  if (kind !== "selected_clips") return undefined;
  const clips = stringArray(selection?.clips);
  return clips && clips.length > 0 ? { kind, clips } : undefined;
}

function interactionKind(value: unknown): PendingInteraction["kind"] | undefined {
  const kind = textValue(value);
  return [
    "high_risk_confirmation", "single_select", "multi_select", "calibration_preview",
    "calibration_confirmation",
  ].includes(kind) ? kind as PendingInteraction["kind"] : undefined;
}

function interactionRisk(value: unknown): PendingInteraction["risk"] | undefined {
  const risk = textValue(value);
  return ["low", "medium", "high"].includes(risk)
    ? risk as PendingInteraction["risk"]
    : undefined;
}

function interactionOptions(value: unknown): PendingInteraction["options"] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((candidate) => {
    const option = recordValue(candidate);
    const optionId = textValue(option?.option_id ?? option?.optionId);
    const label = textValue(option?.label);
    if (!optionId || !label) return [];
    const tone = textValue(option?.tone);
    const description = textValue(option?.description);
    const destructive = option?.destructive === true;
    return [{
      option_id: optionId,
      label,
      ...(description ? { description } : {}),
      ...(destructive ? { destructive: true } : {}),
      ...(["default", "primary", "danger"].includes(tone)
        ? { tone: tone as "default" | "primary" | "danger" }
        : {}),
    }];
  });
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
    timeline: run.timeline.map((item) => ({ ...item })),
    running: run.running,
    interrupting: run.interrupting,
    appliedEventKeys: { ...run.appliedEventKeys },
    terminalProgress: { ...run.terminalProgress },
  };
}

function mergeTurns(existing: TurnRecord[], persisted: TurnRecord[]): TurnRecord[] {
  const byId = new Map(existing.map((turn) => [turn.id, turn]));
  for (const turn of persisted) {
    const current = byId.get(turn.id);
    if (
      current
      && isTerminalTurnStatus(current.status)
      && !isTerminalTurnStatus(turn.status)
    ) {
      continue;
    }
    byId.set(turn.id, turn);
  }
  return [...byId.values()].sort((left, right) => left.started_at.localeCompare(right.started_at));
}

function isTerminalTurnStatus(status: TurnRecord["status"]): boolean {
  return status === "completed" || status === "failed" || status === "interrupted";
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

function reconcileOptimisticTurn(
  messages: ChatMessageRecord[],
  turns: TurnRecord[],
  run: RunState,
  event: AgentEvent,
): { messages: ChatMessageRecord[]; turns: TurnRecord[]; run: RunState } {
  const serverTurnId = typeof event.turn_id === "string" ? event.turn_id : "";
  if (event.type !== "turn_start" || !serverTurnId || serverTurnId.startsWith("local-turn-")) {
    return { messages, turns, run };
  }
  if (turns.some((turn) => turn.id === serverTurnId)) {
    return { messages, turns, run };
  }

  // A WebSocket turn_start can beat the POST /turns response. There can be only
  // one locally submitted user Turn, so adopt it immediately and avoid rendering
  // a second empty ProcessingDisclosure during that race.
  const candidates = turns.filter(
    (turn) => turn.id.startsWith("local-turn-") && turn.origin === "user" && turn.status === "running",
  );
  if (candidates.length !== 1) return { messages, turns, run };
  const localTurnId = candidates[0].id;
  return {
    messages: messages.map((message) =>
      message.turn_id === localTurnId ? { ...message, turn_id: serverTurnId } : message,
    ),
    turns: mergeAdoptedTurn(turns, localTurnId, serverTurnId),
    run: remapRunTurnId(run, localTurnId, serverTurnId),
  };
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
  return next;
}

function removeRunTurn(run: RunState, turnId: string): RunState {
  const next = cloneRunState(run);
  next.timeline = next.timeline.filter((item) => item.turnId !== turnId);
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
    event.turn_id ?? "",
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
