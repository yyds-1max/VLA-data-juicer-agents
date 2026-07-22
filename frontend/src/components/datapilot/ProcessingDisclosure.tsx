import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { TurnRecord } from "../../api/types";
import type { TimelineItem } from "../../store/eventReducer";
import { cn, withoutPercentages } from "../../lib/utils";

type ProcessingDisclosureProps = {
  turn: TurnRecord;
  items: TimelineItem[];
  allowEmptyPlaceholder?: boolean;
  hasAnswer?: boolean;
};

export function ProcessingDisclosure({
  turn,
  items,
  allowEmptyPlaceholder = false,
  hasAnswer = false,
}: ProcessingDisclosureProps) {
  const visibleItems = visibleProcessingItems(items);
  const hasForegroundAction = visibleItems.some(
    (item) => item.kind === "action" && item.actionStatus === "running",
  );
  const active = turn.status === "running" || turn.status === "waiting" || hasForegroundAction;
  const meaningful = visibleItems.some((item) => !isInitialProgress(item));
  const [delayElapsed, setDelayElapsed] = useState(false);
  const shouldRender = meaningful || (active && allowEmptyPlaceholder && !hasAnswer && delayElapsed);
  const [expanded, setExpanded] = useState(active);
  const previousStatusRef = useRef(turn.status);
  const terminalAutoCollapsedRef = useRef(!active);
  const now = useNow(active);

  useEffect(() => {
    if (meaningful || !active || !allowEmptyPlaceholder) {
      if (meaningful) setDelayElapsed(true);
      return undefined;
    }
    setDelayElapsed(false);
    const timer = window.setTimeout(() => setDelayElapsed(true), 400);
    return () => window.clearTimeout(timer);
  }, [active, allowEmptyPlaceholder, meaningful, turn.id]);

  useEffect(() => {
    if (previousStatusRef.current !== turn.status && !active && !terminalAutoCollapsedRef.current) {
      setExpanded(false);
      terminalAutoCollapsedRef.current = true;
    }
    previousStatusRef.current = turn.status;
  }, [active, turn.status]);

  useEffect(() => {
    previousStatusRef.current = turn.status;
    terminalAutoCollapsedRef.current = !active;
    setExpanded(active);
  }, [active, turn.id]);

  if (!shouldRender) return null;

  const duration = formatDuration(turnDurationMs(turn, now));
  const contentId = `processing-${turn.id}`;
  const title = turnTitle(active ? "running" : turn.status);
  const animatedProgressIndex = activeProgressIndex(visibleItems, active);

  return (
    <section
      className="mr-auto w-full max-w-[94%] text-console-text"
    >
      <button
        type="button"
        aria-label={title}
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-1.5 border-b border-console-line/80 py-2 text-left text-sm text-console-muted transition motion-reduce:transition-none hover:text-console-text focus:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/60"
      >
        <span>{title}</span>
        <span aria-hidden="true">{duration}</span>
        {expanded ? (
          <ChevronDown className="h-4 w-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        )}
      </button>

      <div id={contentId} hidden={!expanded} className="space-y-3 py-3 text-sm leading-6">
        {visibleItems.map((item, index) => {
          if (item.kind === "action") {
            return <PublicActionLine key={publicActionItemKey(item, index)} item={item} />;
          }
          if (item.kind === "progress") {
            return (
              <p
                key={item.progressId ? `progress-${item.progressId}` : `progress-${index}`}
                className={cn(
                  "whitespace-pre-wrap break-words",
                  index === animatedProgressIndex && "datapilot-progress-wave",
                )}
                data-progress-id={item.progressId}
                data-progress-active={index === animatedProgressIndex ? "true" : undefined}
              >
                {withoutPercentages(item.text)}
              </p>
            );
          }
          if (item.kind === "interaction") {
            return (
              <div key={`interaction-${item.interactionId ?? index}`} className="flex items-center gap-2 text-xs text-console-muted">
                <ToolStatusDot status={item.status === "completed" ? "completed" : "running"} />
                <span>{withoutPercentages(item.text)}</span>
              </div>
            );
          }
          return null;
        })}
      </div>
    </section>
  );
}

function PublicActionLine({ item }: { item: TimelineItem }) {
  const status = item.actionStatus ?? "running";
  const label = withoutPercentages(item.actionDisplayName || "处理数据");
  const prefix = status === "completed"
    ? "已完成"
    : status === "background"
    ? "已转入后台"
    : status === "failed"
    ? "失败"
    : status === "interrupted"
    ? "已停止"
    : "正在";
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-console-muted" data-public-action={item.actionRef}>
      <ToolStatusDot status={status} />
      <span className="min-w-0 break-words">{prefix}{label}</span>
    </div>
  );
}

function ToolStatusDot({ status }: { status?: string }) {
  const tone = status === "completed"
    ? "success"
    : status === "failed"
    ? "failure"
    : status === "interrupted"
    ? "interrupted"
    : "pending";
  return (
    <span
      aria-hidden="true"
      data-status={tone}
      className={cn(
        "shrink-0 text-xs leading-none",
        tone === "success" && "text-emerald-600",
        tone === "failure" && "text-rose-600",
        tone === "interrupted" && "text-amber-600",
        tone === "pending" && "text-console-muted",
      )}
    >
      ●
    </span>
  );
}

function turnTitle(status: TurnRecord["status"]): string {
  if (status === "waiting") return "等待确认";
  if (status === "completed") return "已处理";
  if (status === "interrupted") return "已停止";
  if (status === "failed") return "处理异常";
  return "正在处理";
}

function turnDurationMs(turn: TurnRecord, now: number): number {
  const started = Date.parse(turn.started_at);
  const finished = turn.finished_at ? Date.parse(turn.finished_at) : now;
  if (Number.isNaN(started) || Number.isNaN(finished)) return 0;
  return Math.max(finished - started, 0);
}

function formatDuration(milliseconds: number): string {
  const seconds = Math.max(Math.floor(milliseconds / 1000), 0);
  const minutes = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  return minutes > 0 ? `${minutes}m ${remaining}s` : `${remaining}s`;
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function visibleProcessingItems(items: TimelineItem[]): TimelineItem[] {
  const allowed = items.filter((item) => ["progress", "action", "interaction"].includes(item.kind));
  const hasSubstantiveUpdate = allowed.some((item) => !isInitialProgress(item));
  if (hasSubstantiveUpdate) return allowed.filter((item) => !isInitialProgress(item));
  const initialProgress = allowed.find(isInitialProgress);
  return initialProgress ? [initialProgress] : [];
}

function isInitialProgress(item: TimelineItem): boolean {
  return item.kind === "progress" && item.text === "正在理解你的请求";
}

function activeProgressIndex(items: TimelineItem[], active: boolean): number {
  if (!active) return -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "progress" && item.progressPhase !== "completed") return index;
  }
  return -1;
}

function publicActionItemKey(item: TimelineItem, index: number): string {
  return `${item.turnId ?? "turn"}:${item.actionPhaseInstanceId ?? "phase"}:${item.actionRef ?? index}`;
}
