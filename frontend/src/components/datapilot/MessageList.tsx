import { useEffect, useState } from "react";

import type { ChatMessageRecord } from "../../api/types";
import type { ActiveAgent, ActiveTool, RunState, TimelineItem } from "../../store/eventReducer";
import { cn } from "../../lib/utils";
import { AgentRunSummary, ToolStatusDot, type AgentRunTimelineItem } from "./AgentRunSummary";

type MessageListProps = {
  messages: ChatMessageRecord[];
  run: RunState;
};

type OrderedTimelineItem = AgentRunTimelineItem;

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

type AgentRunSummaryEntry = {
  type: "agent-run-summary";
  key: string;
  timestamp: number;
  sequence: number;
  source: string;
  items: OrderedTimelineItem[];
};

type ChronologicalEntry = MessageEntry | TimelineEntry;
type RenderEntry = ChronologicalEntry | AgentRunSummaryEntry;

export function MessageList({ messages, run }: MessageListProps) {
  const hasContent = messages.length > 0 || run.timeline.length > 0 || Boolean(run.activeText);
  const entries = renderEntries(messages, run);
  const now = useActiveNow(run.activeText ? run.activeStartedAt : null);
  const activeText = formatActiveText(run.activeText, run.activeStartedAt, now);

  return (
    <div
      data-datapilot-scroll-area="true"
      className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain bg-console-panel2/45 px-4 py-4 sm:px-5"
    >
      {hasContent ? (
        <>
          {entries.map((entry) =>
            entry.type === "message" ? (
              <MessageBubble key={entry.key} message={entry.message} />
            ) : entry.type === "agent-run-summary" ? (
              <AgentRunSummary key={entry.key} source={entry.source} items={entry.items} />
            ) : (
              <TimelineBubble key={entry.key} item={entry.item} />
            ),
          )}
          {activeText ? (
            <div className="rounded-lg border border-console-cyan/20 bg-blue-50 px-3 py-2 text-xs text-console-cyan">
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

function renderEntries(messages: ChatMessageRecord[], run: RunState): RenderEntry[] {
  const entries = chronologicalEntries(messages, run.timeline);
  const childGroups = completedChildGroups(entries, run);
  const emittedGroups = new Set<string>();

  return entries.flatMap((entry): RenderEntry[] => {
    if (entry.type !== "timeline" || !isChildTimelineItem(entry.item)) {
      return [entry];
    }

    const groupKey = childRunKey(entry.item);
    const group = childGroups.get(groupKey);
    if (!group) {
      return [entry];
    }
    if (emittedGroups.has(groupKey)) {
      return [];
    }

    emittedGroups.add(groupKey);
    return [
      {
        type: "agent-run-summary",
        key: `agent-run-summary-${groupKey}`,
        timestamp: entry.timestamp,
        sequence: entry.sequence,
        source: group[0]?.item.source ?? entry.item.source,
        items: group.map((item) => item.item),
      },
    ];
  });
}

function completedChildGroups(entries: ChronologicalEntry[], run: RunState): Map<string, TimelineEntry[]> {
  const groups = new Map<string, TimelineEntry[]>();

  for (const entry of entries) {
    if (entry.type !== "timeline" || !isChildTimelineItem(entry.item) || isChildRunActive(entry.item, run)) {
      continue;
    }

    const groupKey = childRunKey(entry.item);
    const group = groups.get(groupKey) ?? [];
    group.push(entry);
    groups.set(groupKey, group);
  }

  return groups;
}

function chronologicalEntries(messages: ChatMessageRecord[], timeline: TimelineItem[]): ChronologicalEntry[] {
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

function isChildTimelineItem(item: TimelineItem): boolean {
  return item.source !== "main";
}

function isChildRunActive(item: TimelineItem, run: RunState): boolean {
  return (
    Object.values(run.activeAgents).some((agent) => activeMatchesItem(agent, item)) ||
    Object.values(run.activeTools).some((tool) => activeMatchesItem(tool, item))
  );
}

function activeMatchesItem(active: ActiveAgent | ActiveTool, item: TimelineItem): boolean {
  if (item.runId && active.runId === item.runId) {
    return true;
  }
  return active.source === item.source;
}

function childRunKey(item: TimelineItem): string {
  return item.runId ? `run:${item.runId}` : `source:${item.source}`;
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
        "max-w-[88%] rounded-lg border px-3 py-2 text-sm leading-6 shadow-sm",
        isUser
          ? "ml-auto border-console-cyan/25 bg-blue-50 text-console-text"
          : "mr-auto border-console-line bg-console-panel text-console-text",
      )}
    >
      <div className="mb-1 text-[11px] font-medium text-console-muted">
        {isUser ? "You" : message.role === "assistant" ? "DataPilot" : "System"}
      </div>
      <p className="whitespace-pre-wrap break-words">{message.content}</p>
    </article>
  );
}

function TimelineBubble({ item }: { item: TimelineItem }) {
  return (
    <article className="mr-auto max-w-[92%] rounded-lg border border-console-line bg-console-panel px-3 py-2 text-sm leading-6 text-console-text shadow-sm">
      <div className="mb-1 flex items-center gap-2 text-[11px] font-medium text-console-muted">
        <span>{item.kind === "assistant" ? "DataPilot" : item.kind}</span>
        <span className="truncate font-normal">{item.source}</span>
      </div>
      {item.kind === "tool" ? (
        <div className="flex min-w-0 items-center gap-2 whitespace-pre-wrap break-words">
          <ToolStatusDot status={item.status} />
          <span className="min-w-0 break-words">{item.text}</span>
        </div>
      ) : (
        <p className="whitespace-pre-wrap break-words">{item.text}</p>
      )}
    </article>
  );
}
