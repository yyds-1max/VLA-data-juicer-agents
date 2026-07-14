import type { CSSProperties, PointerEvent, WheelEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";

import {
  createSession,
  deleteSession,
  getSession,
  interruptTurn,
  listSessions,
  recoverHumanDecision,
  submitHumanDecision,
  submitTurn,
  streamSessionEvents,
} from "../../api/client";
import type { PendingHumanDecision, SessionRecord } from "../../api/types";
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

type StreamLease = {
  sessionId: string;
  generation: number;
  controller: AbortController;
};

type ActiveSubmitRequest = {
  id: number;
  sessionId: string;
  generation: number;
  userMessage: ReturnType<typeof localUserMessage>;
  outcome: "pending" | "success" | "failure";
  error?: unknown;
};

export function DataPilotWindow() {
  const open = useStore(datapilotStore, (state) => state.open);
  const mode = useStore(datapilotStore, (state) => state.mode);
  const currentSessionId = useStore(datapilotStore, (state) => state.currentSessionId);
  const sessions = useStore(datapilotStore, (state) => state.sessions);
  const conversation = useStore(datapilotStore, (state) => state.conversation);
  const running = conversation.phase !== "idle";
  const interrupting = conversation.phase === "interrupting";
  const pendingHumanDecision = conversation.pendingHumanDecision;
  const floatingOffset = useStore(datapilotStore, (state) => state.floatingOffset);
  const setFloatingOffset = useStore(datapilotStore, (state) => state.setFloatingOffset);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [rendered, setRendered] = useState(open);
  const [closing, setClosing] = useState(false);
  const [recoveringHumanDecision, setRecoveringHumanDecision] = useState(false);
  const [humanDecisionRecoveryError, setHumanDecisionRecoveryError] = useState("");
  const [stopRequestPending, setStopRequestPending] = useState(false);
  const [viewport, setViewport] = useState(() => ({
    width: typeof window === "undefined" ? 1280 : window.innerWidth,
    height: typeof window === "undefined" ? 900 : window.innerHeight,
  }));
  const streamRef = useRef<StreamLease | null>(null);
  const startEventStreamRef = useRef<(sessionId: string, generation: number) => void>(
    () => undefined,
  );
  const reconnectTimerRef = useRef<number | null>(null);
  const lifecycleGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const selectionGenerationRef = useRef(0);
  const selectionTargetRef = useRef<string | null>(null);
  const draftRequestGenerationRef = useRef(0);
  const activeSubmitRequestIdRef = useRef(0);
  const activeSubmitQueueRef = useRef(new Map<number, ActiveSubmitRequest>());
  const deletedSessionIdsRef = useRef(new Set<string>());
  const reconnectStateRef = useRef<{ sessionId: string | null; attempts: number }>({
    sessionId: null,
    attempts: 0,
  });
  const humanDecisionRecoveryRequestRef = useRef(0);
  const dragRef = useRef<DragState | null>(null);
  const windowOffset = useMemo(() => visibleWindowOffset(floatingOffset, viewport), [floatingOffset, viewport]);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current === null) {
      return;
    }
    window.clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = null;
  }, []);

  const invalidateEventLifecycle = useCallback(() => {
    lifecycleGenerationRef.current += 1;
    activeSubmitQueueRef.current.clear();
    clearReconnectTimer();
    const active = streamRef.current;
    streamRef.current = null;
    active?.controller.abort();
  }, [clearReconnectTimer]);

  const ownsActiveSubmitRequest = useCallback((request: ActiveSubmitRequest) => {
    const state = datapilotStore.getState();
    return (
      mountedRef.current &&
      activeSubmitQueueRef.current.get(request.id) === request &&
      lifecycleGenerationRef.current === request.generation &&
      state.open &&
      state.mode === "active_session" &&
      state.currentSessionId === request.sessionId &&
      !deletedSessionIdsRef.current.has(request.sessionId)
    );
  }, []);

  const flushActiveSubmitQueue = useCallback(() => {
    for (const [requestId, request] of activeSubmitQueueRef.current) {
      if (request.outcome === "pending") {
        break;
      }
      const owned = ownsActiveSubmitRequest(request);
      activeSubmitQueueRef.current.delete(requestId);
      if (!owned) {
        continue;
      }
      if (request.outcome === "success") {
        datapilotStore.getState().appendUserMessage(request.userMessage);
      } else {
        console.error("Failed to submit DataPilot active turn", request.error);
      }
    }
  }, [ownsActiveSubmitRequest]);

  const isCurrentLease = useCallback((lease: StreamLease) => {
    const state = datapilotStore.getState();
    return (
      mountedRef.current &&
      lifecycleGenerationRef.current === lease.generation &&
      streamRef.current === lease &&
      state.open &&
      state.mode === "active_session" &&
      state.currentSessionId === lease.sessionId &&
      !deletedSessionIdsRef.current.has(lease.sessionId)
    );
  }, []);

  const startEventStream = useCallback(
    (sessionId: string, expectedGeneration: number) => {
      const state = datapilotStore.getState();
      if (
        !mountedRef.current ||
        lifecycleGenerationRef.current !== expectedGeneration ||
        !state.open ||
        state.mode !== "active_session" ||
        state.currentSessionId !== sessionId ||
        deletedSessionIdsRef.current.has(sessionId)
      ) {
        return;
      }
      const active = streamRef.current;
      if (active?.sessionId === sessionId) {
        return;
      }

      if (active) {
        invalidateEventLifecycle();
      }
      if (reconnectStateRef.current.sessionId !== sessionId) {
        reconnectStateRef.current = { sessionId, attempts: 0 };
      }
      const controller = new AbortController();
      const lease: StreamLease = {
        sessionId,
        generation: lifecycleGenerationRef.current,
        controller,
      };
      streamRef.current = lease;

      void (async () => {
        const afterSequence = datapilotStore.getState().conversation.lastSequence;
        let replayRequired = false;
        try {
          for await (const event of streamSessionEvents(
            sessionId,
            afterSequence,
            controller.signal,
          )) {
            if (!isCurrentLease(lease)) {
              return;
            }
            const beforeCursor = datapilotStore.getState().conversation.lastSequence;
            datapilotStore.getState().applyEvent(event);
            const afterCursor = datapilotStore.getState().conversation.lastSequence;
            if (event.sequence > beforeCursor && afterCursor === beforeCursor) {
              replayRequired = true;
              controller.abort();
              break;
            }
            if (afterCursor > beforeCursor) {
              reconnectStateRef.current.attempts = 0;
            }
          }
        } catch (error) {
          if (isCurrentLease(lease) && !controller.signal.aborted && !replayRequired) {
            console.error("DataPilot event stream failed", error);
          }
        }

        if (!isCurrentLease(lease)) {
          return;
        }
        const cursorBeforeRefresh = datapilotStore.getState().conversation.lastSequence;
        try {
          const detail = await getSession(sessionId);
          if (!isCurrentLease(lease)) {
            return;
          }
          datapilotStore.getState().refreshActiveSession(detail);
        } catch (error) {
          if (!isCurrentLease(lease)) {
            return;
          }
          console.error("Failed to refresh DataPilot active session", error);
        }
        if (!isCurrentLease(lease)) {
          return;
        }
        if (datapilotStore.getState().conversation.lastSequence > cursorBeforeRefresh) {
          reconnectStateRef.current.attempts = 0;
        }
        reconnectStateRef.current.attempts += 1;
        const reconnectDelay = Math.min(
          250 * 2 ** (reconnectStateRef.current.attempts - 1),
          5_000,
        );
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          if (isCurrentLease(lease)) {
            controller.abort();
            streamRef.current = null;
            startEventStreamRef.current(lease.sessionId, lease.generation);
          }
        }, reconnectDelay);
      })();
    },
    [invalidateEventLifecycle, isCurrentLease],
  );
  startEventStreamRef.current = startEventStream;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      draftRequestGenerationRef.current += 1;
      selectionGenerationRef.current += 1;
      selectionTargetRef.current = null;
      invalidateEventLifecycle();
    };
  }, [invalidateEventLifecycle]);

  useEffect(() => {
    if (!open || mode !== "active_session" || !currentSessionId) {
      invalidateEventLifecycle();
      reconnectStateRef.current = { sessionId: null, attempts: 0 };
      return undefined;
    }

    let cancelled = false;
    const sessionId = currentSessionId;
    startEventStream(sessionId, lifecycleGenerationRef.current);

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
      if (streamRef.current?.sessionId === sessionId) {
        invalidateEventLifecycle();
      }
    };
  }, [currentSessionId, invalidateEventLifecycle, mode, open, startEventStream]);

  useEffect(() => {
    if (conversation.phase === "idle") {
      setStopRequestPending(false);
    }
  }, [conversation.phase]);

  useEffect(() => {
    setStopRequestPending(false);
  }, [currentSessionId]);

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
    draftRequestGenerationRef.current += 1;
    selectionGenerationRef.current += 1;
    selectionTargetRef.current = null;
    invalidateEventLifecycle();
    setHistoryOpen(false);
    datapilotStore.getState().enterDraft();
  };

  const handleSelectHistory = async (session: SessionRecord) => {
    draftRequestGenerationRef.current += 1;
    const previousState = datapilotStore.getState();
    const previousMode = previousState.mode;
    const previousSessionId = previousState.currentSessionId;
    const selectionGeneration = selectionGenerationRef.current + 1;
    selectionGenerationRef.current = selectionGeneration;
    selectionTargetRef.current = session.id;
    invalidateEventLifecycle();
    const lifecycleGeneration = lifecycleGenerationRef.current;
    const ownsSelection = () => {
      const state = datapilotStore.getState();
      return (
        mountedRef.current &&
        selectionGenerationRef.current === selectionGeneration &&
        selectionTargetRef.current === session.id &&
        lifecycleGenerationRef.current === lifecycleGeneration &&
        !deletedSessionIdsRef.current.has(session.id) &&
        state.open &&
        state.mode === previousMode &&
        state.currentSessionId === previousSessionId
      );
    };
    try {
      const detail = await getSession(session.id);
      if (!ownsSelection()) {
        return;
      }
      selectionTargetRef.current = null;
      datapilotStore.getState().restoreSession(detail);
      setHistoryOpen(false);
      startEventStream(session.id, lifecycleGeneration);
    } catch (error) {
      if (!ownsSelection()) {
        return;
      }
      selectionTargetRef.current = null;
      if (previousMode === "active_session" && previousSessionId) {
        startEventStream(previousSessionId, lifecycleGeneration);
      }
      console.error("Failed to restore DataPilot session", error);
    }
  };

  const handleDeleteHistory = async (session: SessionRecord) => {
    try {
      await deleteSession(session.id);
      deletedSessionIdsRef.current.add(session.id);
      const cancelledSelection = selectionTargetRef.current === session.id;
      if (cancelledSelection) {
        selectionGenerationRef.current += 1;
        selectionTargetRef.current = null;
      }
      const store = datapilotStore.getState();
      store.setSessions(store.sessions.filter((item) => item.id !== session.id));
      if (store.currentSessionId === session.id) {
        invalidateEventLifecycle();
        store.enterDraft();
        setHistoryOpen(false);
      } else if (
        cancelledSelection &&
        store.open &&
        store.mode === "active_session" &&
        store.currentSessionId
      ) {
        startEventStream(store.currentSessionId, lifecycleGenerationRef.current);
      }
    } catch (error) {
      console.error("Failed to delete DataPilot session", error);
    }
  };

  const handleDraftSubmit = async (message: string) => {
    const requestGeneration = draftRequestGenerationRef.current + 1;
    draftRequestGenerationRef.current = requestGeneration;
    const lifecycleGeneration = lifecycleGenerationRef.current;
    let createdSessionId: string | null = null;
    const ownsDraftIntent = () => {
      const state = datapilotStore.getState();
      return (
        mountedRef.current &&
        draftRequestGenerationRef.current === requestGeneration &&
        lifecycleGenerationRef.current === lifecycleGeneration &&
        state.open &&
        state.mode === "draft_new_session" &&
        state.currentSessionId === null
      );
    };
    const ownsCreatedSession = () => {
      const state = datapilotStore.getState();
      return Boolean(
        createdSessionId &&
          mountedRef.current &&
          draftRequestGenerationRef.current === requestGeneration &&
          lifecycleGenerationRef.current === lifecycleGeneration &&
          state.open &&
          state.mode === "active_session" &&
          state.currentSessionId === createdSessionId &&
          !deletedSessionIdsRef.current.has(createdSessionId),
      );
    };
    try {
      const session = await createSession(message);
      if (!ownsDraftIntent()) {
        return;
      }
      createdSessionId = session.id;
      const store = datapilotStore.getState();
      store.setActiveSession(session);
      const userMessage = localUserMessage(session.id, message);
      startEventStream(session.id, lifecycleGeneration);
      await submitTurn(session.id, message);
      if (!ownsCreatedSession()) {
        return;
      }
      datapilotStore.getState().appendUserMessage(userMessage);
    } catch (error) {
      if (!(createdSessionId ? ownsCreatedSession() : ownsDraftIntent())) {
        return;
      }
      invalidateEventLifecycle();
      datapilotStore.getState().enterDraft();
      console.error("Failed to submit DataPilot draft turn", error);
    }
  };

  const handleActiveSubmit = async (message: string) => {
    if (!currentSessionId) {
      return;
    }

    const sessionId = currentSessionId;
    startEventStream(sessionId, lifecycleGenerationRef.current);
    const state = datapilotStore.getState();
    if (
      !mountedRef.current ||
      !state.open ||
      state.mode !== "active_session" ||
      state.currentSessionId !== sessionId ||
      deletedSessionIdsRef.current.has(sessionId)
    ) {
      return;
    }
    const request: ActiveSubmitRequest = {
      id: activeSubmitRequestIdRef.current + 1,
      sessionId,
      generation: lifecycleGenerationRef.current,
      userMessage: localUserMessage(sessionId, message),
      outcome: "pending",
    };
    activeSubmitRequestIdRef.current = request.id;
    activeSubmitQueueRef.current.set(request.id, request);
    try {
      await submitTurn(sessionId, message);
      const current = activeSubmitQueueRef.current.get(request.id);
      if (current === request) {
        request.outcome = "success";
        flushActiveSubmitQueue();
      }
    } catch (error) {
      const current = activeSubmitQueueRef.current.get(request.id);
      if (current === request) {
        request.outcome = "failure";
        request.error = error;
        flushActiveSubmitQueue();
      }
    }
  };

  const handleInterrupt = async () => {
    if (!currentSessionId || stopRequestPending || interrupting) {
      return;
    }

    const sessionId = currentSessionId;
    setStopRequestPending(true);
    try {
      const interrupted = await interruptTurn(sessionId);
      const state = datapilotStore.getState();
      if (interrupted && state.currentSessionId === sessionId) {
        state.markInterrupting();
      } else {
        setStopRequestPending(false);
      }
    } catch (error) {
      setStopRequestPending(false);
      console.error("Failed to interrupt DataPilot turn", error);
    }
  };

  const handleHumanDecision = useCallback(
    async (action: "confirm" | "stop" | "guide", text?: string) => {
      if (!currentSessionId || !pendingHumanDecision) {
        return;
      }

      const sessionId = currentSessionId;
      const decision = pendingHumanDecision;
      if (decision.recoveryRequired || decision.submissionDisabled) {
        return;
      }

      try {
        const accepted = await submitHumanDecision(sessionId, {
          action,
          request_id: decision.requestId,
          tool_call_id: decision.toolCallId,
          reply_id: decision.replyId,
          ...(decision.planId ? { plan_id: decision.planId } : {}),
          ...(decision.stepId ? { step_id: decision.stepId } : {}),
          ...(text ? { text } : {}),
        });
        if (accepted) {
          datapilotStore.getState().clearPendingHumanDecision(decision, sessionId);
        }
      } catch (error) {
        console.error("Failed to submit human decision", error);
      }
    },
    [currentSessionId, pendingHumanDecision],
  );

  useEffect(() => {
    humanDecisionRecoveryRequestRef.current += 1;
    setRecoveringHumanDecision(false);
    setHumanDecisionRecoveryError("");
  }, [
    currentSessionId,
    pendingHumanDecision?.replyId,
    pendingHumanDecision?.toolCallId,
    pendingHumanDecision?.recoveryRequired,
  ]);

  const handleHumanDecisionRecovery = useCallback(
    async (reason: string) => {
      const planId = pendingHumanDecision?.planId;
      const stepId = pendingHumanDecision?.stepId;
      if (
        !currentSessionId ||
        !pendingHumanDecision?.recoveryRequired ||
        !planId ||
        !stepId ||
        !reason.trim()
      ) {
        return;
      }
      const sessionId = currentSessionId;
      const decision = pendingHumanDecision;
      const requestToken = humanDecisionRecoveryRequestRef.current + 1;
      humanDecisionRecoveryRequestRef.current = requestToken;
      const isCurrentRequest = () => {
        const state = datapilotStore.getState();
        return (
          humanDecisionRecoveryRequestRef.current === requestToken &&
          state.currentSessionId === sessionId &&
          samePendingHumanDecision(state.conversation.pendingHumanDecision, decision)
        );
      };
      setRecoveringHumanDecision(true);
      setHumanDecisionRecoveryError("");
      try {
        const result = await recoverHumanDecision(sessionId, {
          action: "quarantine_and_replan",
          plan_id: planId,
          step_id: stepId,
          reason: reason.trim(),
        });
        if (result.recovered) {
          datapilotStore.getState().clearPendingHumanDecision(decision, sessionId);
        }
      } catch (error) {
        if (isCurrentRequest()) {
          setHumanDecisionRecoveryError(
            error instanceof Error ? error.message : "受控恢复失败，请重试。",
          );
        }
        console.error("Failed to recover human decision", error);
      } finally {
        if (isCurrentRequest()) {
          setRecoveringHumanDecision(false);
        }
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
          onDelete={handleDeleteHistory}
          onClose={() => setHistoryOpen(false)}
        />
      ) : null}
      {mode === "draft_new_session" ? (
        <DraftNewSessionView running={running} onSubmit={handleDraftSubmit} onInterrupt={handleInterrupt} />
      ) : mode === "active_session" ? (
        <div className="flex min-h-0 flex-1 flex-col bg-console-panel">
          <MessageList messages={conversation.messages} toolRuns={conversation.toolRuns} />
          <HumanDecisionDialog
            key={`${currentSessionId ?? ""}:${pendingHumanDecision?.replyId ?? ""}:${pendingHumanDecision?.toolCallId ?? ""}`}
            decision={pendingHumanDecision}
            onConfirm={() => handleHumanDecision("confirm")}
            onStop={() => handleHumanDecision("stop")}
            onGuide={(text) => handleHumanDecision("guide", text)}
            onRecover={handleHumanDecisionRecovery}
            recovering={recoveringHumanDecision}
            recoveryError={humanDecisionRecoveryError || undefined}
          />
          {pendingHumanDecision ? null : (
            <div className="border-t border-console-line p-3 sm:p-4">
              <Composer
                placeholder="继续描述任务…"
                running={running}
                interrupting={interrupting || stopRequestPending}
                onSubmit={handleActiveSubmit}
                onInterrupt={handleInterrupt}
              />
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}

function samePendingHumanDecision(
  left: PendingHumanDecision | null,
  right: PendingHumanDecision,
): boolean {
  return Boolean(
    left &&
      left.replyId === right.replyId &&
      left.toolCallId === right.toolCallId &&
      left.requestId === right.requestId &&
      left.planId === right.planId &&
      left.stepId === right.stepId &&
      left.recoveryRequired === right.recoveryRequired &&
      left.submissionDisabled === right.submissionDisabled &&
      left.recoveryEndpoint === right.recoveryEndpoint,
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
