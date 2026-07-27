import { useCallback, useLayoutEffect, useRef } from "react";

import type { ChatMessageRecord, TurnRecord } from "../../api/types";
import type { RunState, TimelineItem } from "../../store/eventReducer";
import { cn, withoutPercentages } from "../../lib/utils";
import { ProcessingDisclosure } from "./ProcessingDisclosure";

type MessageListProps = {
  messages: ChatMessageRecord[];
  turns?: TurnRecord[];
  run: RunState;
  hasTaskOverlay?: boolean;
};

const STICKY_BOTTOM_THRESHOLD = 24;

export function MessageList({ messages, turns = [], run, hasTaskOverlay = false }: MessageListProps) {
  const displayTurns = [...turns].sort((left, right) => left.started_at.localeCompare(right.started_at));
  const turnIds = new Set(displayTurns.map((turn) => turn.id));
  // Contract v1 welcomes may intentionally be session-scoped instead of Turn-scoped.
  // Render them as messages only; never synthesize a legacy Turn around them.
  const sessionMessages = messages.filter((message) => !message.turn_id || !turnIds.has(message.turn_id));
  const placeholderTurnId = latestEmptyUserTurnId(displayTurns, messages, run.timeline);
  const hasContent = messages.length > 0 || run.timeline.length > 0 || displayTurns.length > 0;
  const scrollAreaRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);

  const handleScroll = useCallback(() => {
    const scrollArea = scrollAreaRef.current;
    if (!scrollArea) return;
    shouldStickToBottomRef.current = isScrolledNearBottom(scrollArea);
  }, []);

  useLayoutEffect(() => {
    const scrollArea = scrollAreaRef.current;
    if (!scrollArea || !shouldStickToBottomRef.current) return;
    scrollArea.scrollTop = scrollArea.scrollHeight;
  });

  return (
    <div
      ref={scrollAreaRef}
      data-datapilot-scroll-area="true"
      className={cn(
        "flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain bg-console-panel2/45 px-4 py-4 sm:px-5",
        hasTaskOverlay && "pb-20",
      )}
      onScroll={handleScroll}
    >
      {hasContent ? (
        <>
          {sessionMessages.map((message) => <MessageBubble key={message.id} message={message} />)}
          {displayTurns.map((turn) => (
            <TurnConversation
              key={turn.id}
              turn={turn}
              messages={messages}
              run={run}
              allowEmptyPlaceholder={turn.id === placeholderTurnId}
            />
          ))}
        </>
      ) : (
        <div className="mt-auto rounded-lg border border-console-line bg-console-panel px-3 py-3 text-sm text-console-muted shadow-xs">
          这个会话还没有消息。
        </div>
      )}
    </div>
  );
}

function TurnConversation({
  turn,
  messages,
  run,
  allowEmptyPlaceholder,
}: {
  turn: TurnRecord;
  messages: ChatMessageRecord[];
  run: RunState;
  allowEmptyPlaceholder: boolean;
}) {
  const userMessages = messages.filter(
    (message) => message.role === "user" && message.turn_id === turn.id,
  );
  const assistantMessages = messages.filter(
    (message) => message.turn_id === turn.id && message.role === "assistant",
  );
  const items = processingItems(run.timeline, turn.id);
  const meaningful = items.some((item) => !isInitialProgress(item));
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
  const active = turn.status === "running" || turn.status === "waiting";
  const showDisclosure = meaningful || (active && allowEmptyPlaceholder);

  return (
    <div className="contents">
      {userMessages.map((message) => <MessageBubble key={message.id} message={message} />)}
      {showDisclosure ? (
        <ProcessingDisclosure
          turn={turn}
          items={items}
          allowEmptyPlaceholder={allowEmptyPlaceholder}
          hasAnswer={assistantMessages.length > 0 || liveReplies.length > 0}
        />
      ) : null}
      {assistantMessages.map((message) => <MessageBubble key={message.id} message={message} />)}
      {unpersistedLiveReplies.map((item, index) => (
        <AssistantBubble
          key={item.replyKey ?? `${item.turnId ?? "turn"}:${index}`}
          text={item.text}
        />
      ))}
    </div>
  );
}

function latestEmptyUserTurnId(
  turns: TurnRecord[],
  messages: ChatMessageRecord[],
  timeline: TimelineItem[],
): string | null {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (turn.origin !== "user" || (turn.status !== "running" && turn.status !== "waiting")) continue;
    const hasAnswer = messages.some(
      (message) => message.turn_id === turn.id && message.role === "assistant",
    ) || timeline.some((item) => item.turnId === turn.id && item.kind === "assistant");
    const meaningful = processingItems(timeline, turn.id).some((item) => !isInitialProgress(item));
    if (!hasAnswer && !meaningful) return turn.id;
  }
  return null;
}

function processingItems(timeline: TimelineItem[], turnId: string): TimelineItem[] {
  return timeline.filter(
    (item) => item.turnId === turnId && ["progress", "action", "interaction"].includes(item.kind),
  );
}

function isInitialProgress(item: TimelineItem): boolean {
  return item.kind === "progress" && item.text === "正在理解你的请求";
}

function AssistantBubble({ text }: { text: string }) {
  return (
    <article className="mr-auto max-w-[88%] rounded-lg border border-console-line bg-console-panel px-3 py-2 text-sm leading-6 text-console-text shadow-xs">
      <div className="mb-1 text-[11px] font-medium text-console-muted">DataPilot</div>
      <p className="whitespace-pre-wrap wrap-break-word">{withoutPercentages(text)}</p>
    </article>
  );
}

function MessageBubble({ message }: { message: ChatMessageRecord }) {
  const isUser = message.role === "user";
  return (
    <article
      className={cn(
        "max-w-[88%] rounded-lg border px-3 py-2 text-sm leading-6 shadow-xs",
        isUser
          ? "ml-auto border-console-cyan/25 bg-blue-50 text-console-text"
          : "mr-auto border-console-line bg-console-panel text-console-text",
      )}
    >
      <div className="mb-1 text-[11px] font-medium text-console-muted">
        {isUser ? "You" : "DataPilot"}
      </div>
      <p className="whitespace-pre-wrap wrap-break-word">
        {isUser ? message.content : withoutPercentages(message.content)}
      </p>
    </article>
  );
}

function isScrolledNearBottom(element: HTMLElement) {
  const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
  return distanceToBottom <= STICKY_BOTTOM_THRESHOLD;
}
