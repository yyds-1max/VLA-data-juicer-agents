import { useCallback, useEffect, useRef, useState } from "react";
import { useStore } from "zustand";

import {
  createSession,
  getSession,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitTurn,
} from "../../api/client";
import type { SessionRecord } from "../../api/types";
import { datapilotStore } from "../../store/datapilotStore";
import { Composer } from "./Composer";
import { DraftNewSessionView } from "./DraftNewSessionView";
import { MessageList } from "./MessageList";
import { SessionHeader } from "./SessionHeader";
import { SessionHistoryPanel } from "./SessionHistoryPanel";

export function DataPilotWindow() {
  const open = useStore(datapilotStore, (state) => state.open);
  const mode = useStore(datapilotStore, (state) => state.mode);
  const currentSessionId = useStore(datapilotStore, (state) => state.currentSessionId);
  const sessions = useStore(datapilotStore, (state) => state.sessions);
  const messages = useStore(datapilotStore, (state) => state.messages);
  const run = useStore(datapilotStore, (state) => state.run);
  const running = useStore(datapilotStore, (state) => state.run.running);
  const [historyOpen, setHistoryOpen] = useState(false);
  const socketRef = useRef<{ sessionId: string; socket: WebSocket } | null>(null);

  const closeSocket = useCallback(() => {
    socketRef.current?.socket.close();
    socketRef.current = null;
  }, []);

  useEffect(() => {
    if (!open || mode !== "active_session") {
      closeSocket();
    }
  }, [closeSocket, mode, open]);

  useEffect(() => closeSocket, [closeSocket]);

  const openEvents = useCallback(
    (sessionId: string) => {
      if (socketRef.current?.sessionId === sessionId && isActiveSocket(socketRef.current.socket)) {
        return;
      }

      closeSocket();
      socketRef.current = {
        sessionId,
        socket: openSessionEvents(sessionId, (event) => datapilotStore.getState().applyEvent(event)),
      };
    },
    [closeSocket],
  );

  useEffect(() => {
    if (!open || mode !== "active_session" || !currentSessionId) {
      return;
    }

    let cancelled = false;
    const sessionId = currentSessionId;
    openEvents(sessionId);

    void getSession(sessionId)
      .then((detail) => {
        if (!cancelled) {
          datapilotStore.getState().refreshActiveSession(detail);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          console.error("Failed to refresh DataPilot active session", error);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [currentSessionId, mode, open, openEvents]);

  const handleHistory = async () => {
    const nextSessions = await listSessions();
    datapilotStore.getState().setSessions(nextSessions);
    setHistoryOpen(true);
  };

  const handleNewSession = () => {
    closeSocket();
    setHistoryOpen(false);
    datapilotStore.getState().enterDraft();
  };

  const handleSelectHistory = async (session: SessionRecord) => {
    closeSocket();
    const detail = await getSession(session.id);
    datapilotStore.getState().restoreHistory(detail, detail.messages);
    setHistoryOpen(false);
  };

  const handleDraftSubmit = async (message: string) => {
    try {
      const session = await createSession(message);
      const store = datapilotStore.getState();
      store.setActiveSession(session);
      const userMessage = localUserMessage(session.id, message);
      openEvents(session.id);
      await submitTurn(session.id, message);
      datapilotStore.getState().appendUserMessage(userMessage);
    } catch (error) {
      closeSocket();
      datapilotStore.getState().enterDraft();
      console.error("Failed to submit DataPilot draft turn", error);
    }
  };

  const handleActiveSubmit = async (message: string) => {
    if (!currentSessionId) {
      return;
    }

    try {
      const userMessage = localUserMessage(currentSessionId, message);
      openEvents(currentSessionId);
      await submitTurn(currentSessionId, message);
      datapilotStore.getState().appendUserMessage(userMessage);
    } catch (error) {
      console.error("Failed to submit DataPilot active turn", error);
    }
  };

  const handleInterrupt = async () => {
    if (!currentSessionId) {
      return;
    }

    await interruptTurn(currentSessionId);
  };

  if (!open) {
    return null;
  }

  return (
    <section
      role="dialog"
      aria-label="DataPilot"
      className="fixed bottom-3 right-3 z-40 flex h-[min(640px,calc(100vh-1.5rem))] w-[calc(100vw-1.5rem)] max-w-[460px] flex-col overflow-hidden rounded border border-console-line bg-console-panel shadow-[0_22px_70px_rgba(0,0,0,0.42)] sm:bottom-5 sm:right-5 sm:h-[min(680px,calc(100vh-2.5rem))] sm:w-[min(460px,calc(100vw-2.5rem))]"
    >
      <SessionHeader onHistory={handleHistory} onNewSession={handleNewSession} />
      {historyOpen ? (
        <SessionHistoryPanel
          sessions={sessions}
          onSelect={handleSelectHistory}
          onClose={() => setHistoryOpen(false)}
        />
      ) : null}
      {mode === "draft_new_session" ? (
        <DraftNewSessionView running={running} onSubmit={handleDraftSubmit} onInterrupt={handleInterrupt} />
      ) : mode === "active_session" ? (
        <div className="flex min-h-0 flex-1 flex-col bg-console-panel">
          <MessageList messages={messages} run={run} />
          <div className="border-t border-console-line p-3 sm:p-4">
            <Composer
              placeholder="继续描述任务…"
              running={running}
              onSubmit={handleActiveSubmit}
              onInterrupt={handleInterrupt}
            />
          </div>
        </div>
      ) : (
        <MessageList messages={messages} run={run} />
      )}
    </section>
  );
}

function localUserMessage(sessionId: string, content: string) {
  return {
    id: createLocalId(),
    session_id: sessionId,
    role: "user" as const,
    content,
    created_at: new Date().toISOString(),
  };
}

function createLocalId(): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `local-${suffix}`;
}

function isActiveSocket(socket: WebSocket): boolean {
  return socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN;
}
