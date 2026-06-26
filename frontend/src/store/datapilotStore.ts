import { createStore } from "zustand/vanilla";

import type { AgentEvent, ChatMessageRecord, SessionDetail, SessionRecord } from "../api/types";
import { applyAgentEvent, createEmptyRunState, type RunState } from "./eventReducer";

export type SessionMode = "draft_new_session" | "active_session" | "history_session";

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
  setActiveSession: (session: SessionRecord) => void;
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
        applyAgentEvent(run, event);
        return { run };
      }),
  }));
}

export const datapilotStore = createDataPilotStore();

function upsertSession(sessions: SessionRecord[], session: SessionRecord): SessionRecord[] {
  const next = sessions.filter((item) => item.id !== session.id);
  return [session, ...next];
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
    running: run.running,
  };
}
