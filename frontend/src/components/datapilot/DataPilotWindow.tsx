import type { CSSProperties, PointerEvent, WheelEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";

import {
  createSession,
  ApiResponseError,
  getSession,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitInteractionResponse,
  submitTurn,
} from "../../api/client";
import type { PendingInteraction, SessionDetail, SessionRecord } from "../../api/types";
import { withoutPercentages } from "../../lib/utils";
import { datapilotStore, type DataPilotInvocation } from "../../store/datapilotStore";
import { Composer } from "./Composer";
import { DraftNewSessionView } from "./DraftNewSessionView";
import { InteractionPanel } from "./InteractionPanel";
import { MessageList } from "./MessageList";
import { SessionHeader } from "./SessionHeader";
import { SessionHistoryPanel } from "./SessionHistoryPanel";
import { TaskStrip } from "./TaskStrip";
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
  const turns = useStore(datapilotStore, (state) => state.turns);
  const tasks = useStore(datapilotStore, (state) => state.tasks);
  const pendingInteraction = useStore(datapilotStore, (state) => state.pendingInteraction);
  const run = useStore(datapilotStore, (state) => state.run);
  const pendingInvocation = useStore(datapilotStore, (state) => state.pendingInvocation);
  const runRunning = useStore(datapilotStore, (state) => state.run.running);
  const interrupting = useStore(datapilotStore, (state) => state.run.interrupting);
  const floatingOffset = useStore(datapilotStore, (state) => state.floatingOffset);
  const setFloatingOffset = useStore(datapilotStore, (state) => state.setFloatingOffset);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [rendered, setRendered] = useState(open);
  const [closing, setClosing] = useState(false);
  const [submittingInteraction, setSubmittingInteraction] = useState(false);
  const [interactionError, setInteractionError] = useState("");
  const [viewport, setViewport] = useState(() => ({
    width: typeof window === "undefined" ? 1280 : window.innerWidth,
    height: typeof window === "undefined" ? 900 : window.innerHeight,
  }));
  const socketRef = useRef<{ sessionId: string; socket: WebSocket } | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null);
  const previousInteractionRef = useRef<PendingInteraction | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const windowOffset = useMemo(() => visibleWindowOffset(floatingOffset, viewport), [floatingOffset, viewport]);
  const running = runRunning || turns.some(
    (turn) => turn.status === "running" || turn.status === "waiting",
  );

  useEffect(() => {
    const previous = previousInteractionRef.current;
    previousInteractionRef.current = pendingInteraction;
    if (previous && !pendingInteraction) {
      window.requestAnimationFrame(() => composerInputRef.current?.focus());
    }
    setSubmittingInteraction(false);
    setInteractionError("");
  }, [pendingInteraction]);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current === null) {
      return;
    }
    window.clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = null;
  }, []);

  const refreshSessionSnapshot = useCallback(async (sessionId: string) => {
    try {
      const detail = await getSession(sessionId);
      datapilotStore.getState().refreshActiveSession(detail);
    } catch (error) {
      console.error("Failed to refresh DataPilot active session", error);
    }
  }, []);

  const closeSocket = useCallback(() => {
    clearReconnectTimer();
    const socket = socketRef.current?.socket;
    socketRef.current = null;
    socket?.close();
  }, [clearReconnectTimer]);

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
      clearReconnectTimer();
      const socket = openSessionEvents(sessionId, (event) => datapilotStore.getState().applyEvent(event));
      socketRef.current = {
        sessionId,
        socket,
      };

      const handleDisconnect = () => {
        if (socketRef.current?.socket !== socket) {
          return;
        }
        socketRef.current = null;
        void refreshSessionSnapshot(sessionId);
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          const state = datapilotStore.getState();
          if (state.open && state.mode === "active_session" && state.currentSessionId === sessionId) {
            openEvents(sessionId);
          }
        }, 100);
      };
      socket.addEventListener("close", handleDisconnect);
      socket.addEventListener("error", handleDisconnect);
    },
    [clearReconnectTimer, closeSocket, refreshSessionSnapshot],
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

  const submitNewSessionMessage = useCallback(async (
    message: string,
    options: {
      invocationId?: string;
      sessionId?: string;
      requestContext?: DataPilotInvocation["requestContext"];
      entrypoint?: DataPilotInvocation["entrypoint"];
    } = {},
  ) => {
    let sessionId = options.sessionId;
    let localTurnId: string | null = null;
    let userMessageId: string | null = null;
    try {
      if (sessionId) {
        const state = datapilotStore.getState();
        if (state.mode !== "active_session" || state.currentSessionId !== sessionId) {
          const detail = await getSession(sessionId);
          datapilotStore.getState().restoreActiveSession(detail, detail.messages);
        }
      } else {
        const session = options.invocationId
          ? await createSession(
              message,
              options.entrypoint ?? "data_management_shortcut",
              options.requestContext,
            )
          : await createSession(message);
        sessionId = session.id;
        datapilotStore.getState().setActiveSession(session);
        if (options.invocationId) {
          datapilotStore.getState().setDataPilotInvocationSession(options.invocationId, sessionId);
        }
      }

      const store = datapilotStore.getState();
      localTurnId = createLocalTurnId();
      const userMessage = localUserMessage(sessionId, message, localTurnId);
      userMessageId = userMessage.id;
      store.appendUserMessage(userMessage);
      store.applyEvent(localTurnEvent("turn_start", localTurnId));
      openEvents(sessionId);
      const turnId = options.invocationId
        ? await submitTurn(sessionId, message, options.invocationId)
        : await submitTurn(sessionId, message);
      datapilotStore.getState().adoptTurnId(localTurnId, turnId);
      if (options.invocationId) {
        datapilotStore.getState().completeDataPilotInvocation(options.invocationId);
      }
      return true;
    } catch (error) {
      const store = datapilotStore.getState();
      if (userMessageId) {
        store.discardLocalMessage(userMessageId);
      }
      if (localTurnId) {
        store.discardLocalTurn(localTurnId);
        store.applyEvent(localTurnEvent("turn_submission_failed", localTurnId));
      }
      if (options.invocationId) {
        store.failDataPilotInvocation(
          options.invocationId,
          `${sessionId ? "提交失败" : "创建会话失败"}：${errorMessage(error)}`,
        );
      } else if (!sessionId) {
        closeSocket();
        store.enterDraft();
      }
      console.error("Failed to submit DataPilot new-session turn", error);
      return false;
    }
  }, [closeSocket, openEvents]);

  const refreshKnownRunningSession = useCallback(async () => {
    const state = datapilotStore.getState();
    const candidateSessionId =
      state.knownRunningSessionId ??
      (state.mode === "active_session" ? state.currentSessionId : state.previousActiveSessionId);
    if (!candidateSessionId) {
      return false;
    }

    const localRunning =
      state.knownRunningSessionId === candidateSessionId ||
      (state.currentSessionId === candidateSessionId && (
        state.run.running ||
        state.turns.some((turn) => turn.status === "running" || turn.status === "waiting")
      ));
    try {
      const detail = await getSession(candidateSessionId);
      const detailRunning = (detail.turns ?? []).some(
        (turn) => turn.status === "running" || turn.status === "waiting",
      ) || (detail.tasks ?? []).some(
        (task) => !["cancelled", "completed", "failed", "superseded"].includes(task.status),
      );
      if (detailRunning) {
        datapilotStore.getState().restoreActiveSession(detail, detail.messages);
        openEvents(candidateSessionId);
        return true;
      }
      datapilotStore.getState().updateKnownRunningSession(candidateSessionId, false);
      if (
        datapilotStore.getState().mode === "active_session" &&
        datapilotStore.getState().currentSessionId === candidateSessionId
      ) {
        datapilotStore.getState().refreshActiveSession(detail);
      }
      return false;
    } catch (error) {
      console.error("Failed to refresh DataPilot before shortcut submission", error);
      return localRunning;
    }
  }, [openEvents]);

  const processDataPilotInvocation = useCallback(async (invocation: DataPilotInvocation) => {
    if (!datapilotStore.getState().claimDataPilotInvocation(invocation.invocationId)) {
      return;
    }

    if (!invocation.sessionId && await refreshKnownRunningSession()) {
      datapilotStore.getState().blockDataPilotInvocation(
        invocation.invocationId,
        "当前任务正在执行，请等待完成或停止后再发起。",
      );
      return;
    }

    await submitNewSessionMessage(invocation.message, {
      invocationId: invocation.invocationId,
      sessionId: invocation.sessionId,
      requestContext: invocation.requestContext,
      entrypoint: invocation.entrypoint,
    });
  }, [refreshKnownRunningSession, submitNewSessionMessage]);

  useEffect(() => {
    if (!pendingInvocation || pendingInvocation.status !== "queued") {
      return;
    }
    void processDataPilotInvocation(pendingInvocation);
  }, [pendingInvocation, processDataPilotInvocation]);

  const handleDraftSubmit = async (message: string) => {
    await submitNewSessionMessage(message);
  };

  const handleActiveSubmit = async (message: string) => {
    if (!currentSessionId) {
      return;
    }

    const store = datapilotStore.getState();
    const localTurnId = createLocalTurnId();
    const userMessage = localUserMessage(currentSessionId, message, localTurnId);
    store.appendUserMessage(userMessage);
    store.applyEvent(localTurnEvent("turn_start", localTurnId));
    try {
      openEvents(currentSessionId);
      const turnId = await submitTurn(currentSessionId, message);
      datapilotStore.getState().adoptTurnId(localTurnId, turnId);
    } catch (error) {
      datapilotStore.getState().discardLocalMessage(userMessage.id);
      datapilotStore.getState().discardLocalTurn(localTurnId);
      datapilotStore.getState().applyEvent(
        localTurnEvent("turn_submission_failed", localTurnId),
      );
      console.error("Failed to submit DataPilot active turn", error);
    }
  };

  const handleInterrupt = async () => {
    if (!currentSessionId) {
      return;
    }

    const interrupted = await interruptTurn(currentSessionId);
    if (interrupted) {
      const detail = await getSession(currentSessionId);
      datapilotStore.getState().restoreActiveSession(detail, detail.messages);
    }
  };

  const handleInteractionResponse = useCallback(async (optionIds: string[]) => {
    if (!currentSessionId || !pendingInteraction || optionIds.length === 0) return;
    const sessionId = currentSessionId;
    const interaction = pendingInteraction;
    setSubmittingInteraction(true);
    setInteractionError("");
    try {
      const result = await submitInteractionResponse(sessionId, interaction.interaction_id, {
        ...(optionIds.length === 1 ? { option_id: optionIds[0] } : { option_ids: optionIds }),
        interaction_revision: interaction.interaction_revision,
        expected_task_revision: interaction.expected_task_revision,
        idempotency_key: createIdempotencyKey(),
      });
      if (result.session) {
        datapilotStore.getState().refreshActiveSession(result.session);
      } else if (result.accepted) {
        datapilotStore.getState().applyEvent({
          type: "interaction_resolved",
          contract_version: 1,
          timestamp: new Date().toISOString(),
          turn_id: result.turn_id ?? null,
          payload: {
            interaction_id: interaction.interaction_id,
            result_label: selectedInteractionLabel(interaction, optionIds),
          },
        });
      }
    } catch (error) {
      const snapshot = interactionConflictSnapshot(error);
      if (snapshot) datapilotStore.getState().refreshActiveSession(snapshot);
      setInteractionError(
        error instanceof ApiResponseError && error.status === 409
          ? "任务状态已更新，请根据最新状态重新选择。"
          : errorMessage(error),
      );
    } finally {
      setSubmittingInteraction(false);
    }
  }, [currentSessionId, pendingInteraction]);

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
      className={`fixed bottom-3 right-3 z-80 flex h-[min(640px,calc(100vh-1.5rem))] w-[calc(100vw-1.5rem)] max-w-[500px] origin-bottom-right flex-col overflow-hidden rounded-lg border border-console-line bg-console-panel shadow-[0_24px_70px_rgba(23,32,46,0.20)] motion-reduce:animate-none sm:bottom-5 sm:right-5 sm:h-[min(680px,calc(100vh-2.5rem))] sm:w-[min(500px,calc(100vw-2.5rem))] ${
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
      {pendingInvocation?.error ? (
        <div
          className={`border-b px-4 py-2 text-sm ${
            pendingInvocation.status === "blocked"
              ? "border-amber-200 bg-amber-50 text-amber-800"
              : "border-rose-200 bg-rose-50 text-rose-700"
          }`}
        >
          {pendingInvocation.error}
        </div>
      ) : null}
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
          <div className="sr-only" aria-live="polite" aria-atomic="true">
            {liveAnnouncement(pendingInteraction, tasks)}
          </div>
          <div className="relative flex min-h-0 flex-1">
            <MessageList
              messages={messages}
              turns={turns}
              run={run}
              hasTaskOverlay={tasks.length > 0}
            />
            <TaskStrip tasks={tasks} />
          </div>
          {pendingInteraction ? (
            <InteractionPanel
              key={`${pendingInteraction.interaction_id}:${pendingInteraction.interaction_revision}`}
              interaction={pendingInteraction}
              submitting={submittingInteraction}
              error={interactionError || undefined}
              onSubmit={handleInteractionResponse}
            />
          ) : null}
          {pendingInteraction?.blocking ? null : (
            <div className="relative z-30 shrink-0 bg-transparent px-3 pb-3 pt-2 sm:px-4 sm:pb-4">
              <Composer
                ref={composerInputRef}
                placeholder="继续描述任务…"
                running={running}
                interrupting={interrupting}
                onSubmit={handleActiveSubmit}
                onInterrupt={handleInterrupt}
              />
            </div>
          )}
        </div>
      ) : (
        <MessageList messages={messages} turns={turns} run={run} />
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

function localUserMessage(sessionId: string, content: string, turnId?: string) {
  return {
    id: createLocalId(),
    session_id: sessionId,
    role: "user" as const,
    content,
    created_at: new Date().toISOString(),
    turn_id: turnId ?? null,
  };
}

function localTurnEvent(
  type: "turn_start" | "turn_submission_failed",
  turnId: string,
) {
  const timestamp = new Date().toISOString();
  return {
    type,
    contract_version: 1 as const,
    timestamp,
    turn_id: turnId,
    payload: type === "turn_start" ? { status: "running", started_at: timestamp } : {},
  };
}

function createLocalTurnId(): string {
  return createLocalId().replace(/^local-/, "local-turn-");
}

function createLocalId(): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `local-${suffix}`;
}

function createIdempotencyKey(): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `interaction-${suffix}`;
}

function selectedInteractionLabel(interaction: PendingInteraction, optionIds: string[]): string {
  const labels = optionIds.flatMap((optionId) => {
    const option = interaction.options.find((candidate) => candidate.option_id === optionId);
    return option ? [option.label] : [];
  });
  return labels.length > 0 ? `已选择：${labels.join("、")}` : "已提交选择";
}

function interactionConflictSnapshot(error: unknown): SessionDetail | null {
  if (!(error instanceof ApiResponseError) || error.status !== 409) return null;
  const body = asRecord(error.body);
  const detail = asRecord(body?.detail);
  const session = asRecord(body?.session) ?? asRecord(detail?.session);
  return session && typeof session.id === "string" && Array.isArray(session.messages)
    ? session as unknown as SessionDetail
    : null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function liveAnnouncement(
  interaction: PendingInteraction | null,
  tasks: ReturnType<typeof datapilotStore.getState>["tasks"],
): string {
  if (interaction) return withoutPercentages(interaction.title || interaction.summary);
  const task = tasks.find((candidate) =>
    !["cancelled", "completed", "failed", "superseded"].includes(candidate.status),
  ) ?? tasks[0];
  if (!task) return "";
  const status = task.status === "waiting_user"
    ? "等待输入"
    : task.status === "completed"
    ? "任务已完成"
    : task.status === "failed"
    ? "任务失败"
    : task.status === "paused"
    ? "任务已暂停"
    : "任务状态已更新";
  return task.phase ? `${status}：${withoutPercentages(task.phase)}` : status;
}

function isActiveSocket(socket: WebSocket): boolean {
  return socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN;
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message.trim() ? error.message : "请稍后重试";
}
