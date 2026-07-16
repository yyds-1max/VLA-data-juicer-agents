import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { TurnRecord } from "../../api/types";
import type { TimelineItem } from "../../store/eventReducer";
import { ToolStatusDot } from "./AgentRunSummary";

type ProcessingDisclosureProps = {
  turn: TurnRecord;
  items: TimelineItem[];
};

export function ProcessingDisclosure({ turn, items }: ProcessingDisclosureProps) {
  const active = turn.status === "running" || turn.status === "waiting";
  const visibleItems = visibleProcessingItems(items);
  const [expanded, setExpanded] = useState(active);
  const previousStatusRef = useRef(turn.status);
  const terminalAutoCollapsedRef = useRef(!active);
  const now = useNow(active || items.some(isActiveTool));

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
  }, [turn.id]);

  const duration = formatDuration(turnDurationMs(turn, now));
  const contentId = `processing-${turn.id}`;

  return (
    <section className="mr-auto w-full max-w-[94%] text-console-text" data-turn-id={turn.id}>
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-1.5 border-b border-console-line/80 py-2 text-left text-sm text-console-muted transition hover:text-console-text focus:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/60"
      >
        <span>{turnTitle(turn.status, duration)}</span>
        {expanded ? (
          <ChevronDown className="h-4 w-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        )}
      </button>

      <div
        id={contentId}
        hidden={!expanded}
        className="space-y-3 py-3 text-sm leading-6"
      >
        {visibleItems.map((item, index) =>
          item.kind === "tool" ? (
            <ToolProgressLine key={toolItemKey(item, index)} item={item} now={now} />
          ) : item.kind === "progress" ? (
            <ProgressParagraph
              key={item.progressId
                ? `progress-${item.runId ?? ""}-${item.progressId}`
                : `progress-${index}`}
              item={item}
              animate={active}
            />
          ) : item.kind === "activity" ? (
            legacyActivityParagraphs(item).map((text, textIndex) => (
              <p key={`legacy-${index}-${textIndex}`} className="whitespace-pre-wrap break-words">
                {text}
              </p>
            ))
          ) : null,
        )}
      </div>
    </section>
  );
}

function ProgressParagraph({ item, animate }: { item: TimelineItem; animate: boolean }) {
  const characters = Array.from(item.text);
  const animateOnMountRef = useRef(
    animate && Boolean(item.progressId) && isRecentProgress(item.createdAt),
  );
  const [visibleCount, setVisibleCount] = useState(
    animateOnMountRef.current ? 0 : characters.length,
  );

  useEffect(() => {
    const reducedMotion = typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!animate || !animateOnMountRef.current || reducedMotion) {
      setVisibleCount(characters.length);
      return undefined;
    }
    if (visibleCount >= characters.length) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      setVisibleCount((count) => Math.min(count + 2, characters.length));
    }, 25);
    return () => window.clearInterval(timer);
  }, [animate, characters.length, visibleCount]);

  const visibleText = characters.slice(0, visibleCount).join("");
  return (
    <p
      className="whitespace-pre-wrap break-words"
      data-progress-id={item.progressId}
    >
      <span
        aria-hidden={visibleCount < characters.length ? "true" : undefined}
        role={visibleCount >= characters.length ? "status" : undefined}
        aria-live={visibleCount >= characters.length ? "polite" : undefined}
      >
        {visibleText}
      </span>
    </p>
  );
}

function isRecentProgress(createdAt: string | undefined): boolean {
  if (!createdAt) return true;
  const timestamp = Date.parse(createdAt);
  if (Number.isNaN(timestamp)) return false;
  const age = Date.now() - timestamp;
  return age >= -60_000 && age <= 5_000;
}

function ToolProgressLine({ item, now }: { item: TimelineItem; now: number }) {
  const phase = item.toolPhase ?? normalizedToolPhase(item.status);
  const tool = item.tool ?? item.text;
  const startedAt = item.startedAt ?? now;
  const finishedAt = item.finishedAt ?? now;
  const elapsedMs = Math.max((phase === "running" || phase === "background" ? now : finishedAt) - startedAt, 0);
  const elapsed = phase === "running" || phase === "background"
    ? `+${Math.floor(elapsedMs / 1000)}s`
    : formatPreciseDuration(elapsedMs);
  const text = phase === "running" || phase === "background"
    ? `正在调用 ${tool} ${elapsed}`
    : phase === "completed"
    ? `已调用 ${tool} ${elapsed}`
    : phase === "interrupted"
    ? `已停止调用 ${tool} ${elapsed}`
    : `调用 ${tool} 失败 ${elapsed}`;

  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-console-muted" data-tool-call={item.callId}>
      <ToolStatusDot status={phase === "background" ? "running" : phase} />
      <span className="min-w-0 break-words">{text}</span>
    </div>
  );
}

function turnTitle(status: TurnRecord["status"], duration: string): string {
  if (status === "waiting") return `等待确认 ${duration}`;
  if (status === "completed") return `已处理 ${duration}`;
  if (status === "interrupted") return `已停止 ${duration}`;
  if (status === "failed") return `处理异常 ${duration}`;
  return `正在处理 ${duration}`;
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

function formatPreciseDuration(milliseconds: number): string {
  return `${(milliseconds / 1000).toFixed(1)}s`;
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

function isActiveTool(item: TimelineItem): boolean {
  return item.kind === "tool" && (item.toolPhase === "running" || item.toolPhase === "background");
}

function visibleProcessingItems(items: TimelineItem[]): TimelineItem[] {
  const hasSubstantiveUpdate = items.some((item) => !isInitialProgress(item));
  if (hasSubstantiveUpdate) {
    return items.filter((item) => !isInitialProgress(item));
  }
  const initialProgress = items.find(isInitialProgress);
  return initialProgress ? [initialProgress] : [];
}

function isInitialProgress(item: TimelineItem): boolean {
  return item.kind === "progress" && item.text === "正在理解你的请求";
}

function normalizedToolPhase(status: string | undefined): NonNullable<TimelineItem["toolPhase"]> {
  if (status === "completed" || status === "interrupted" || status === "background") return status;
  if (status === "running") return "running";
  return "failed";
}

function legacyActivityParagraphs(item: TimelineItem): string[] {
  return (item.activitySteps ?? [])
    .map((step) => [step.observation, step.analysis, step.action].filter(Boolean).join(" "))
    .filter(Boolean);
}

function toolItemKey(item: TimelineItem, index: number): string {
  return `${item.turnId ?? "legacy"}:${item.runId ?? "run"}:${item.callId ?? index}`;
}
