import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { ChatMessageRecord, TurnRecord } from "../../api/types";
import type { RunState, TimelineItem } from "../../store/eventReducer";
import { cn, withoutPercentages } from "../../lib/utils";
import { ProcessingDisclosure } from "./ProcessingDisclosure";

type MessageListProps = {
  messages: ChatMessageRecord[];
  turns?: TurnRecord[];
  run: RunState;
  contractVersion?: 0 | 1;
};

const STICKY_BOTTOM_THRESHOLD = 24;

export function MessageList({ messages, turns = [], run, contractVersion = 0 }: MessageListProps) {
  const durableTurnIds = new Set(turns.map((turn) => turn.id));
  const legacy = synthesizeLegacyTurns(
    messages.filter((message) => !message.turn_id || !durableTurnIds.has(message.turn_id)),
    {
      ...run,
      timeline: run.timeline.filter((item) => !item.turnId || !durableTurnIds.has(item.turnId)),
    },
  );
  const durableTimeline = run.timeline.filter(
    (item) => item.turnId && durableTurnIds.has(item.turnId),
  );
  const displayTurns = mergeDisplayTurns(turns, legacy.turns);
  const remappedLegacyMessages = new Map(legacy.messages.map((message) => [message.id, message]));
  const displayMessages = messages.map((message) => remappedLegacyMessages.get(message.id) ?? message);
  const displayRun = {
    ...run,
    timeline: [...durableTimeline, ...legacy.run.timeline].sort(compareTimelineItems),
  };
  const hasContent = displayMessages.length > 0 || displayRun.timeline.length > 0 || displayTurns.length > 0 || Boolean(run.activeText);
  const now = useActiveNow(run.activeText ? run.activeStartedAt : null);
  const activeText = contractVersion === 1 ? "" : formatActiveText(run.activeText, run.activeStartedAt, now);
  const scrollAreaRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);

  const handleScroll = useCallback(() => {
    const scrollArea = scrollAreaRef.current;
    if (!scrollArea) {
      return;
    }
    shouldStickToBottomRef.current = isScrolledNearBottom(scrollArea);
  }, []);

  useLayoutEffect(() => {
    const scrollArea = scrollAreaRef.current;
    if (!scrollArea || !shouldStickToBottomRef.current) {
      return;
    }
    scrollArea.scrollTop = scrollArea.scrollHeight;
  });

  return (
    <div
      ref={scrollAreaRef}
      data-datapilot-scroll-area="true"
      className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain bg-console-panel2/45 px-4 py-4 sm:px-5"
      onScroll={handleScroll}
    >
      {hasContent ? (
        displayTurns.length > 0 ? (
          <>
            {displayTurns.map((turn) => (
              <TurnConversation
                key={turn.id}
                turn={turn}
                messages={displayMessages}
                run={displayRun}
                contractVersion={contractVersion}
              />
            ))}
          </>
        ) : <>
          {activeText ? (
            <div className="mr-auto flex max-w-[92%] items-center gap-2 px-2 py-1 text-xs text-console-muted">
              {activeText}
            </div>
          ) : null}
        </>
      ) : (
        <div className="mt-auto rounded-lg border border-console-line bg-console-panel px-3 py-3 text-sm text-console-muted shadow-sm">
          这个会话还没有消息。
        </div>
      )}
    </div>
  );
}

function synthesizeLegacyTurns(
  messages: ChatMessageRecord[],
  run: RunState,
): { turns: TurnRecord[]; messages: ChatMessageRecord[]; run: RunState } {
  const turns: TurnRecord[] = [];
  const mappedMessages: ChatMessageRecord[] = [];
  const mappedTimeline: TimelineItem[] = [];
  let currentTurnId = "";

  const createTurn = (origin: TurnRecord["origin"], startedAt: string, id?: string) => {
    currentTurnId = id ?? `legacy:system:${turns.length + 1}`;
    turns.push({
      id: currentTurnId,
      web_session_id: messages[0]?.session_id ?? "legacy",
      origin,
      status: "running",
      started_at: startedAt,
      finished_at: null,
      final_message_id: null,
    });
    return currentTurnId;
  };

  const ensureOpenTurn = (startedAt: string) => {
    const current = turns.find((turn) => turn.id === currentTurnId);
    if (current && (current.status === "running" || current.status === "waiting")) return currentTurnId;
    currentTurnId = `legacy:system:${turns.length + 1}`;
    return createTurn("system", startedAt, currentTurnId);
  };

  const entries = [
    ...messages.map((message, index) => ({
      type: "message" as const,
      timestamp: timestampMs(message.created_at),
      sequence: index,
      message,
    })),
    ...run.timeline.map((item, index) => ({
      type: "timeline" as const,
      timestamp: timestampMs(item.createdAt),
      sequence: messages.length + index,
      item,
    })),
  ].sort((left, right) => left.timestamp - right.timestamp || left.sequence - right.sequence);

  for (const entry of entries) {
    const createdAt = entry.type === "message"
      ? entry.message.created_at
      : entry.item.createdAt ?? new Date(entry.timestamp).toISOString();
    if (entry.type === "message") {
      const message = entry.message;
      if (message.turn_id) {
        currentTurnId = message.turn_id;
        if (!turns.some((turn) => turn.id === currentTurnId)) {
          createTurn(message.role === "user" ? "user" : "system", createdAt, currentTurnId);
        }
        mappedMessages.push(message);
        if (message.role === "assistant") {
          finishLegacyTurn(turns, currentTurnId, "completed", createdAt, message.id);
        }
        continue;
      }
      if (message.role === "user") {
        createTurn("user", createdAt, `legacy:${message.id}`);
      } else {
        ensureOpenTurn(createdAt);
      }
      mappedMessages.push({ ...message, turn_id: currentTurnId });
      if (message.role === "assistant") {
        finishLegacyTurn(turns, currentTurnId, "completed", createdAt, message.id);
      }
      continue;
    }

    const original = entry.item;
    if (original.turnId) {
      currentTurnId = original.turnId;
      if (!turns.some((turn) => turn.id === currentTurnId)) {
        createTurn("system", createdAt, currentTurnId);
      }
      const item = normalizeLegacyTimelineItem(original, currentTurnId, entry.timestamp);
      mappedTimeline.push(item);
      if (item.kind === "assistant") {
        finishLegacyTurn(turns, currentTurnId, "completed", createdAt);
      }
      continue;
    }
    ensureOpenTurn(createdAt);
    const item = normalizeLegacyTimelineItem(original, currentTurnId, entry.timestamp);
    mappedTimeline.push(item);
    if (item.kind === "assistant") {
      finishLegacyTurn(turns, currentTurnId, "completed", createdAt);
    } else if (item.kind === "activity" && item.activityStatus && item.activityStatus !== "running") {
      const status = item.activityStatus === "failed"
        ? "failed"
        : item.activityStatus === "interrupted"
        ? "interrupted"
        : "completed";
      finishLegacyTurn(turns, currentTurnId, status, createdAt);
    }
  }

  const hasActiveLegacyWork = run.running || Object.keys(run.activeAgents).length > 0 || Object.keys(run.activeTools).length > 0;
  for (const turn of turns) {
    if (turn.status !== "running") continue;
    const isLast = turn.id === turns.at(-1)?.id;
    if (!isLast || !hasActiveLegacyWork) {
      const lastItem = [...mappedTimeline].reverse().find((item) => item.turnId === turn.id);
      const lastMessage = [...mappedMessages].reverse().find((message) => message.turn_id === turn.id);
      finishLegacyTurn(
        turns,
        turn.id,
        "completed",
        lastItem?.createdAt ?? lastMessage?.created_at ?? turn.started_at,
      );
    }
  }
  return { turns, messages: mappedMessages, run: { ...run, timeline: mappedTimeline } };
}

function normalizeLegacyTimelineItem(item: TimelineItem, turnId: string, timestamp: number): TimelineItem {
  if (item.kind === "reasoning") {
    return { ...item, kind: "progress", turnId };
  }
  if (item.kind !== "tool") {
    return { ...item, turnId };
  }
  const tool = item.tool ?? legacyToolName(item.text);
  const phase = item.toolPhase ?? legacyToolPhase(item.status, item.text);
  const durationMs = legacyToolDurationMs(item.text);
  const finishedAt = item.finishedAt ?? timestamp;
  return {
    ...item,
    turnId,
    tool,
    toolPhase: phase,
    callId: item.callId ?? `legacy:${item.runId ?? item.source}:${timestamp}`,
    startedAt: item.startedAt ?? Math.max(finishedAt - durationMs, 0),
    finishedAt: phase === "running" || phase === "background" ? undefined : finishedAt,
  };
}

function finishLegacyTurn(
  turns: TurnRecord[],
  turnId: string,
  status: TurnRecord["status"],
  finishedAt: string,
  finalMessageId?: string,
) {
  const turn = turns.find((candidate) => candidate.id === turnId);
  if (!turn) return;
  turn.status = status;
  turn.finished_at = finishedAt;
  if (finalMessageId) turn.final_message_id = finalMessageId;
}

function mergeDisplayTurns(durable: TurnRecord[], legacy: TurnRecord[]): TurnRecord[] {
  const byId = new Map<string, TurnRecord>();
  for (const turn of legacy) byId.set(turn.id, turn);
  for (const turn of durable) byId.set(turn.id, turn);
  return [...byId.values()].sort((left, right) => left.started_at.localeCompare(right.started_at));
}

function compareTimelineItems(left: TimelineItem, right: TimelineItem): number {
  const time = timestampMs(left.createdAt) - timestampMs(right.createdAt);
  if (time !== 0) return time;
  return (left.sequence ?? 0) - (right.sequence ?? 0);
}

function TurnConversation({
  turn,
  messages,
  run,
  contractVersion,
}: {
  turn: TurnRecord;
  messages: ChatMessageRecord[];
  run: RunState;
  contractVersion: 0 | 1;
}) {
  const userMessages = messages.filter(
    (message) => message.role === "user" && message.turn_id === turn.id,
  );
  const assistantMessages = messages.filter(
    (message) => message.turn_id === turn.id && message.role === "assistant",
  );
  const items = run.timeline.filter(
    (item) => item.turnId === turn.id && ["progress", "tool", "activity", "action", "interaction"].includes(item.kind),
  );
  const liveReplies = run.timeline.filter(
    (item) => item.turnId === turn.id && item.kind === "assistant",
  );
  const unpersistedLiveReplies = liveReplies.filter((item) => {
    if (item.finalMessageId) {
      return !assistantMessages.some((message) => message.id === item.finalMessageId);
    }
    if (item.status === "final") {
      return !assistantMessages.some((message) => message.content === item.text);
    }
    return true;
  });
  const showDisclosure = items.length > 0 || turn.origin === "user" || turn.status === "running" || turn.status === "waiting";

  return (
    <div className="contents">
      {userMessages.map((message) => <MessageBubble key={message.id} message={message} contractVersion={contractVersion} />)}
      {showDisclosure ? (
        <ProcessingDisclosure
          turn={turn}
          items={items}
          contractVersion={contractVersion}
          hasAnswer={assistantMessages.length > 0 || liveReplies.length > 0}
        />
      ) : null}
      {assistantMessages.map((message) => <MessageBubble key={message.id} message={message} contractVersion={contractVersion} />)}
      {unpersistedLiveReplies.map((item, index) => (
        <AssistantBubble
          key={item.replyKey ?? `${item.runId ?? item.source}:${item.replyId ?? index}`}
          text={item.text}
          contractVersion={contractVersion}
        />
      ))}
    </div>
  );
}

function AssistantBubble({ text, contractVersion }: { text: string; contractVersion: 0 | 1 }) {
  return (
    <article className="mr-auto max-w-[88%] rounded-lg border border-console-line bg-console-panel px-3 py-2 text-sm leading-6 text-console-text shadow-sm">
      <div className="mb-1 text-[11px] font-medium text-console-muted">DataPilot</div>
      <p className="whitespace-pre-wrap break-words">{contractVersion === 1 ? withoutPercentages(text) : text}</p>
    </article>
  );
}

function isScrolledNearBottom(element: HTMLElement) {
  const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
  return distanceToBottom <= STICKY_BOTTOM_THRESHOLD;
}

export function formatActiveText(activeText: string, startedAt: number | null, now: number): string {
  if (!activeText) {
    return "";
  }
  if (startedAt === null) {
    return activeText;
  }
  const elapsed = Math.max(Math.floor((now - startedAt) / 1000), 0);
  return `${activeText} +${elapsed}s`;
}

function useActiveNow(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (startedAt === null) {
      return undefined;
    }
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  return now;
}

function timestampMs(value: string | undefined): number {
  if (!value) {
    return Date.now();
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

function MessageBubble({ message, contractVersion }: { message: ChatMessageRecord; contractVersion: 0 | 1 }) {
  const isUser = message.role === "user";

  return (
    <article
      className={cn(
        "max-w-[88%] rounded-lg border px-3 py-2 text-sm leading-6 shadow-sm",
        isUser
          ? "ml-auto border-console-cyan/25 bg-blue-50 text-console-text"
          : "mr-auto border-console-line bg-console-panel text-console-text",
      )}
    >
      <div className="mb-1 text-[11px] font-medium text-console-muted">
        {isUser ? "You" : contractVersion === 1 ? "DataPilot" : message.role === "assistant" ? "DataPilot" : "System"}
      </div>
      <p className="whitespace-pre-wrap break-words">
        {contractVersion === 1 && !isUser ? withoutPercentages(message.content) : message.content}
      </p>
    </article>
  );
}

function legacyToolName(text: string): string {
  const match = text.match(/(?:正在调用工具|正在调用|已调用工具|已调用|工具)\s+([^\s+]+)/);
  return match?.[1] ?? text;
}

function legacyToolPhase(status: string | undefined, text: string): NonNullable<TimelineItem["toolPhase"]> {
  if (status === "running" || status === "background" || status === "completed" || status === "failed" || status === "interrupted") {
    return status;
  }
  if (/失败/.test(text)) return "failed";
  if (/停止|中断/.test(text)) return "interrupted";
  if (/正在/.test(text)) return "running";
  return "completed";
}

function legacyToolDurationMs(text: string): number {
  const match = text.match(/(\d+(?:\.\d+)?)s\s*$/);
  return match ? Number.parseFloat(match[1]) * 1000 : 0;
}
