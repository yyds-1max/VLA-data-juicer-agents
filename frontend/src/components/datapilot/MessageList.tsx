import type { ChatMessageRecord } from "../../api/types";
import type { RunState, TimelineItem } from "../../store/eventReducer";
import { cn } from "../../lib/utils";

type MessageListProps = {
  messages: ChatMessageRecord[];
  run: RunState;
};

type OrderedTimelineItem = TimelineItem & {
  createdAt?: string;
  sequence?: number;
};

type MessageEntry = {
  type: "message";
  key: string;
  timestamp: number;
  sequence: number;
  message: ChatMessageRecord;
};

type TimelineEntry = {
  type: "timeline";
  key: string;
  timestamp: number;
  sequence: number;
  item: OrderedTimelineItem;
};

type RenderEntry = MessageEntry | TimelineEntry;

export function MessageList({ messages, run }: MessageListProps) {
  const hasContent = messages.length > 0 || run.timeline.length > 0 || Boolean(run.activeText);
  const entries = chronologicalEntries(messages, run.timeline);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 py-4 sm:px-5">
      {hasContent ? (
        <>
          {entries.map((entry) =>
            entry.type === "message" ? (
              <MessageBubble key={entry.key} message={entry.message} />
            ) : (
              <TimelineBubble key={entry.key} item={entry.item} />
            ),
          )}
          {run.activeText ? (
            <div className="rounded border border-console-cyan/30 bg-console-cyan/10 px-3 py-2 text-xs text-console-cyan">
              {run.activeText}
            </div>
          ) : null}
        </>
      ) : (
        <div className="mt-auto rounded border border-console-line bg-console-bg px-3 py-3 text-sm text-console-muted">
          这个会话还没有消息。
        </div>
      )}
    </div>
  );
}

function chronologicalEntries(messages: ChatMessageRecord[], timeline: TimelineItem[]): RenderEntry[] {
  return [
    ...messages.map((message, index) => ({
      type: "message" as const,
      key: message.id,
      timestamp: timestampMs(message.created_at),
      sequence: index,
      message,
    })),
    ...timeline.map((item, index) => {
      const orderedItem = item as OrderedTimelineItem;
      return {
        type: "timeline" as const,
        key: `${orderedItem.kind}-${orderedItem.runId ?? "run"}-${orderedItem.sequence ?? index}`,
        timestamp: timestampMs(orderedItem.createdAt),
        sequence: messages.length + (orderedItem.sequence ?? index),
        item: orderedItem,
      };
    }),
  ].sort((left, right) => left.timestamp - right.timestamp || left.sequence - right.sequence);
}

function timestampMs(value: string | undefined): number {
  if (!value) {
    return Date.now();
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

function MessageBubble({ message }: { message: ChatMessageRecord }) {
  const isUser = message.role === "user";

  return (
    <article
      className={cn(
        "max-w-[88%] rounded border px-3 py-2 text-sm leading-6",
        isUser
          ? "ml-auto border-console-cyan/40 bg-console-cyan/10 text-console-text"
          : "mr-auto border-console-line bg-console-bg text-console-text",
      )}
    >
      <div className="mb-1 text-[11px] uppercase tracking-[0.16em] text-console-muted">
        {isUser ? "You" : message.role === "assistant" ? "DataPilot" : "System"}
      </div>
      <p className="whitespace-pre-wrap break-words">{message.content}</p>
    </article>
  );
}

function TimelineBubble({ item }: { item: TimelineItem }) {
  return (
    <article className="mr-auto max-w-[92%] rounded border border-console-line bg-console-bg px-3 py-2 text-sm leading-6 text-console-text">
      <div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-[0.16em] text-console-muted">
        <span>{item.kind === "assistant" ? "DataPilot" : item.kind}</span>
        <span className="truncate normal-case tracking-normal">{item.source}</span>
      </div>
      <p className="whitespace-pre-wrap break-words">{item.text}</p>
    </article>
  );
}
