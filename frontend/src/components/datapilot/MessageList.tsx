import { Fragment, useCallback, useLayoutEffect, useRef } from "react";
import type {
  DataBlock,
  HintBlock,
  Msg,
  TextBlock,
  ThinkingBlock,
} from "@agentscope-ai/agentscope/message";

import type { PublicToolRun } from "../../api/types";
import { cn } from "../../lib/utils";
import { ToolStatusDot } from "./AgentRunSummary";

type MessageListProps = {
  messages: Msg[];
  toolRuns: Record<string, PublicToolRun>;
};

const STICKY_BOTTOM_THRESHOLD = 24;

export function MessageList({ messages, toolRuns }: MessageListProps) {
  const runs = Object.values(toolRuns).sort(compareToolRuns);
  const { runsByMessageId, ownedToolCallIds } = groupToolRunsByOwningMessage(
    messages,
    toolRuns,
  );
  const orphanRuns = runs.filter((run) => !ownedToolCallIds.has(run.tool_call_id));
  const hasContent = messages.length > 0 || runs.length > 0;
  const scrollAreaRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);

  const handleScroll = useCallback(() => {
    const scrollArea = scrollAreaRef.current;
    if (scrollArea) {
      shouldStickToBottomRef.current = isScrolledNearBottom(scrollArea);
    }
  }, []);

  useLayoutEffect(() => {
    const scrollArea = scrollAreaRef.current;
    if (scrollArea && shouldStickToBottomRef.current) {
      scrollArea.scrollTop = scrollArea.scrollHeight;
    }
  });

  return (
    <div
      ref={scrollAreaRef}
      data-datapilot-scroll-area="true"
      className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain bg-console-panel2/45 px-4 py-4 sm:px-5"
      onScroll={handleScroll}
    >
      {hasContent ? (
        <>
          {messages.map((message) => (
            <Fragment key={message.id}>
              <MessageBubble message={message} />
              {(runsByMessageId.get(message.id) ?? []).map((run) => (
                <ToolRunLine key={run.tool_call_id} run={run} />
              ))}
            </Fragment>
          ))}
          {orphanRuns.map((run) => (
            <ToolRunLine key={run.tool_call_id} run={run} />
          ))}
        </>
      ) : (
        <div className="mt-auto rounded-lg border border-console-line bg-console-panel px-3 py-3 text-sm text-console-muted shadow-sm">
          这个会话还没有消息。
        </div>
      )}
    </div>
  );
}

function groupToolRunsByOwningMessage(
  messages: Msg[],
  toolRuns: Record<string, PublicToolRun>,
): {
  runsByMessageId: Map<string, PublicToolRun[]>;
  ownedToolCallIds: Set<string>;
} {
  const runsByMessageId = new Map<string, PublicToolRun[]>();
  const ownedToolCallIds = new Set<string>();

  for (const message of messages) {
    const ownedRuns: PublicToolRun[] = [];
    for (const block of message.content) {
      if (block.type !== "tool_call" || ownedToolCallIds.has(block.id)) {
        continue;
      }
      const run = toolRuns[block.id];
      if (run) {
        ownedToolCallIds.add(block.id);
        ownedRuns.push(run);
      }
    }
    if (ownedRuns.length > 0) {
      runsByMessageId.set(message.id, ownedRuns.sort(compareToolRuns));
    }
  }

  return { runsByMessageId, ownedToolCallIds };
}

function MessageBubble({ message }: { message: Msg }) {
  const isUser = message.role === "user";
  const text = visibleMessageText(message);
  if (!text) {
    return null;
  }

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
      <p className="whitespace-pre-wrap break-words">{text}</p>
    </article>
  );
}

function ToolRunLine({ run }: { run: PublicToolRun }) {
  return (
    <div className="mr-auto flex max-w-[92%] items-center gap-2 px-2 py-1 text-xs text-console-muted">
      <ToolStatusDot status={run.status} />
      <span className="min-w-0 break-words">
        {run.tool_name}
        {run.summary ? ` · ${run.summary}` : ""}
      </span>
    </div>
  );
}

function visibleMessageText(message: Msg): string {
  return message.content
    .map((block) => blockText(block))
    .filter(Boolean)
    .join("\n");
}

function blockText(block: Msg["content"][number]): string {
  if (block.type === "text") {
    return (block as TextBlock).text;
  }
  if (block.type === "thinking") {
    return (block as ThinkingBlock).thinking;
  }
  if (block.type === "hint") {
    const hint = (block as HintBlock).hint;
    return typeof hint === "string"
      ? hint
      : hint.map((item) => (item.type === "text" ? item.text : dataLabel(item))).join("\n");
  }
  if (block.type === "data") {
    return dataLabel(block as DataBlock);
  }
  return "";
}

function dataLabel(block: DataBlock): string {
  return block.name ? `[${block.name}]` : `[${block.source.media_type}]`;
}

function compareToolRuns(left: PublicToolRun, right: PublicToolRun): number {
  return left.started_at.localeCompare(right.started_at) ||
    left.tool_call_id.localeCompare(right.tool_call_id);
}

function isScrolledNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= STICKY_BOTTOM_THRESHOLD;
}
