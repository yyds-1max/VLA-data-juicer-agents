import type { CSSProperties, PointerEvent, WheelEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";

import {
  createSession,
  getSession,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitHumanDecision,
  submitTurn,
} from "../../api/client";
import type { SessionRecord } from "../../api/types";
import { datapilotStore } from "../../store/datapilotStore";
import { Composer } from "./Composer";
import { DraftNewSessionView } from "./DraftNewSessionView";
import { HumanDecisionDialog } from "./HumanDecisionDialog";
import { MessageList } from "./MessageList";
import { SessionHeader } from "./SessionHeader";
import { SessionHistoryPanel } from "./SessionHistoryPanel";
import { currentViewport, visibleFloatingOffset, visibleWindowOffset } from "./floatingPosition";

type DragState = {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
};

export function DataPilotWindow() {
  const open = useStore(datapilotStore, (state) => state.open);
  const mode = useStore(datapilotStore, (state) => state.mode);
  const currentSessionId = useStore(datapilotStore, (state) => state.currentSessionId);
  const sessions = useStore(datapilotStore, (state) => state.sessions);
  const messages = useStore(datapilotStore, (state) => state.messages);
  const run = useStore(datapilotStore, (state) => state.run);
  const running = useStore(datapilotStore, (state) => state.run.running);
  const pendingHumanDecision = useStore(datapilotStore, (state) => state.run.pendingHumanDecision);
  const floatingOffset = useStore(datapilotStore, (state) => state.floatingOffset);
  const setFloatingOffset = useStore(datapilotStore, (state) => state.setFloatingOffset);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [rendered, setRendered] = useState(open);
  const [closing, setClosing] = useState(false);
  const [viewport, setViewport] = useState(() => ({
    width: typeof window === "undefined" ? 1280 : window.innerWidth,
    height: typeof window === "undefined" ? 900 : window.innerHeight,
  }));
  const socketRef = useRef<{ sessionId: string; socket: WebSocket } | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const windowOffset = useMemo(() => visibleWindowOffset(floatingOffset, viewport), [floatingOffset, viewport]);

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

  useEffect(() => {
    if (open) {
      setRendered(true);
      setClosing(false);
      return undefined;
    }

    if (!rendered) {
      return undefined;
    }

    setClosing(true);
    const timer = window.setTimeout(() => {
      setRendered(false);
      setClosing(false);
    }, 160);

    return () => {
      window.clearTimeout(timer);
    };
  }, [open, rendered]);

  useEffect(() => {
    const updateViewport = () => {
      setViewport({ width: window.innerWidth, height: window.innerHeight });
    };

    updateViewport();
    window.addEventListener("resize", updateViewport);

    return () => {
      window.removeEventListener("resize", updateViewport);
    };
  }, []);

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
    if (detail.status === "active") {
      datapilotStore.getState().restoreActiveSession(detail, detail.messages);
    } else {
      datapilotStore.getState().restoreHistory(detail, detail.messages);
    }
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

  const handleHumanDecision = useCallback(
    async (action: "confirm" | "stop" | "guide", text?: string) => {
      if (!currentSessionId || !pendingHumanDecision) {
        return;
      }

      try {
        const accepted = await submitHumanDecision(currentSessionId, {
          action,
          request_id: pendingHumanDecision.requestId,
          tool_call_id: pendingHumanDecision.toolCallId,
          reply_id: pendingHumanDecision.replyId,
          ...(text ? { text } : {}),
        });
        if (accepted) {
          datapilotStore.getState().clearPendingHumanDecision();
        }
      } catch (error) {
        console.error("Failed to submit human decision", error);
      }
    },
    [currentSessionId, pendingHumanDecision],
  );

  const handleDragStart = useCallback((event: PointerEvent<HTMLElement>) => {
    const target = event.target;
    if (target instanceof Element && target.closest("button")) {
      return;
    }

    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: windowOffset.x,
      originY: windowOffset.y,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }, [windowOffset.x, windowOffset.y]);

  const handleWheelCapture = useCallback((event: WheelEvent<HTMLElement>) => {
    const target = event.target;
    if (!(target instanceof Node)) {
      return;
    }

    const scrollArea = scrollAreaForWheel(target, event.currentTarget);
    if (!scrollArea) {
      blockWheel(event);
      return;
    }

    const maxScrollTop = Math.max(scrollArea.scrollHeight - scrollArea.clientHeight, 0);
    if (maxScrollTop === 0) {
      blockWheel(event);
      return;
    }

    if (!scrollArea.contains(target)) {
      scrollArea.scrollTop = clampScroll(scrollArea.scrollTop + event.deltaY, maxScrollTop);
      scrollArea.dispatchEvent(new Event("scroll"));
      blockWheel(event);
      return;
    }

    const nextScrollTop = scrollArea.scrollTop + event.deltaY;
    if (nextScrollTop < 0 || nextScrollTop > maxScrollTop) {
      blockWheel(event);
      return;
    }

    event.stopPropagation();
  }, []);

  useEffect(() => {
    if (!open) {
      dragRef.current = null;
      return undefined;
    }

    const handlePointerMove = (event: globalThis.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag || event.pointerId !== drag.pointerId) {
        return;
      }

      setFloatingOffset(
        visibleFloatingOffset(
          {
            x: drag.originX + event.clientX - drag.startX,
            y: drag.originY + event.clientY - drag.startY,
          },
          currentViewport(),
        ),
      );
    };

    const handlePointerUp = (event: globalThis.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag || event.pointerId !== drag.pointerId) {
        return;
      }

      dragRef.current = null;
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };
  }, [open, setFloatingOffset]);

  if (!rendered) {
    return null;
  }

  return (
    <section
      role="dialog"
      aria-label="DataPilot"
      className={`fixed bottom-3 right-3 z-[80] flex h-[min(640px,calc(100vh-1.5rem))] w-[calc(100vw-1.5rem)] max-w-[500px] origin-bottom-right flex-col overflow-hidden rounded-lg border border-console-line bg-console-panel shadow-[0_24px_70px_rgba(23,32,46,0.20)] sm:bottom-5 sm:right-5 sm:h-[min(680px,calc(100vh-2.5rem))] sm:w-[min(500px,calc(100vw-2.5rem))] ${
        closing ? "animate-[datapilot-window-out_160ms_ease-in_forwards]" : "animate-[datapilot-window-in_180ms_ease-out]"
      }`}
      style={{
        left: "auto",
        "--datapilot-x": `${windowOffset.x}px`,
        "--datapilot-y": `${windowOffset.y}px`,
        "--datapilot-anchor-x": `${floatingOffset.x}px`,
        "--datapilot-anchor-y": `${floatingOffset.y}px`,
        transform: `translate3d(${windowOffset.x}px, ${windowOffset.y}px, 0)`,
      } as CSSProperties}
      onWheelCapture={handleWheelCapture}
    >
      <SessionHeader onHistory={handleHistory} onNewSession={handleNewSession} onDragStart={handleDragStart} />
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
          <HumanDecisionDialog
            decision={pendingHumanDecision}
            onConfirm={() => handleHumanDecision("confirm")}
            onStop={() => handleHumanDecision("stop")}
            onGuide={(text) => handleHumanDecision("guide", text)}
          />
          {pendingHumanDecision ? null : (
            <div className="border-t border-console-line p-3 sm:p-4">
              <Composer
                placeholder="继续描述任务…"
                running={running}
                onSubmit={handleActiveSubmit}
                onInterrupt={handleInterrupt}
              />
            </div>
          )}
        </div>
      ) : (
        <MessageList messages={messages} run={run} />
      )}
    </section>
  );
}

function blockWheel(event: WheelEvent<HTMLElement>) {
  event.preventDefault();
  event.stopPropagation();
}

function clampScroll(value: number, maxScrollTop: number) {
  return Math.min(maxScrollTop, Math.max(0, value));
}

function scrollAreaForWheel(target: Node, root: HTMLElement) {
  let current: Element | null =
    target instanceof Element ? target : target.parentNode instanceof Element ? target.parentNode : null;

  while (current && root.contains(current)) {
    if (current instanceof HTMLElement) {
      if (current.getAttribute("data-datapilot-scroll-area") === "true") {
        return current;
      }

      const overflowY = window.getComputedStyle(current).overflowY;
      if ((overflowY === "auto" || overflowY === "scroll") && current.scrollHeight > current.clientHeight) {
        return current;
      }
    }

    current = current.parentElement;
  }

  return root.querySelector<HTMLElement>("[data-datapilot-scroll-area='true']");
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
