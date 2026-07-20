import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { TurnRecord } from "../../api/types";
import type { TimelineItem } from "../../store/eventReducer";
import { withoutPercentages } from "../../lib/utils";
import { ToolStatusDot } from "./AgentRunSummary";

type ProcessingDisclosureProps = {
  turn: TurnRecord;
  items: TimelineItem[];
  contractVersion?: 0 | 1;
  hasAnswer?: boolean;
};

export function ProcessingDisclosure({
  turn,
  items,
  contractVersion = 0,
  hasAnswer = false,
}: ProcessingDisclosureProps) {
  const active = turn.status === "running" || turn.status === "waiting";
  const visibleItems = visibleProcessingItems(items, contractVersion);
  const meaningful = visibleItems.some((item) => !isInitialProgress(item));
  const [delayElapsed, setDelayElapsed] = useState(contractVersion === 0);
  const shouldRender = meaningful || (active && !hasAnswer && delayElapsed);
  const [expanded, setExpanded] = useState(active);
  const previousStatusRef = useRef(turn.status);
  const terminalAutoCollapsedRef = useRef(!active);
  const now = useNow(active || visibleItems.some(isActiveLegacyTool));

  useEffect(() => {
    if (contractVersion === 0 || meaningful || !active) {
      if (contractVersion === 0 || meaningful) setDelayElapsed(true);
      return undefined;
    }
    setDelayElapsed(false);
    const timer = window.setTimeout(() => setDelayElapsed(true), 400);
    return () => window.clearTimeout(timer);
  }, [active, contractVersion, meaningful, turn.id]);

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
  const title = turnTitle(turn.status);

  return (
    <section
      className="mr-auto w-full max-w-[94%] text-console-text"
      data-turn-id={contractVersion === 0 ? turn.id : undefined}
    >
      <button
        type="button"
        aria-label={contractVersion === 1 ? title : `${title} ${duration}`}
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
          if (item.kind === "tool" && contractVersion === 0) {
            return <LegacyToolLine key={legacyToolItemKey(item, index)} item={item} now={now} />;
          }
          if (item.kind === "action") {
            return <PublicActionLine key={publicActionItemKey(item, index)} item={item} />;
          }
          if (item.kind === "progress") {
            return (
              <p
                key={item.progressId ? `progress-${item.progressId}` : `progress-${index}`}
                className="whitespace-pre-wrap break-words"
                data-progress-id={item.progressId}
              >
                {contractVersion === 1 ? withoutPercentages(item.text) : item.text}
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
          if (item.kind === "activity" && contractVersion === 0) {
            return legacyActivityParagraphs(item).map((text, textIndex) => (
              <p key={`legacy-${index}-${textIndex}`} className="whitespace-pre-wrap break-words">
                {text}
              </p>
            ));
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

function LegacyToolLine({ item, now }: { item: TimelineItem; now: number }) {
  const phase = item.toolPhase ?? normalizedLegacyToolPhase(item.status);
  const tool = item.tool ?? item.text;
  const startedAt = item.startedAt ?? now;
  const finishedAt = item.finishedAt ?? now;
  const elapsedMs = Math.max((phase === "running" || phase === "background" ? now : finishedAt) - startedAt, 0);
  const elapsed = phase === "running" || phase === "background"
    ? `+${Math.floor(elapsedMs / 1000)}s`
    : `${(elapsedMs / 1000).toFixed(1)}s`;
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

function isActiveLegacyTool(item: TimelineItem): boolean {
  return item.kind === "tool" && (item.toolPhase === "running" || item.toolPhase === "background");
}

function visibleProcessingItems(items: TimelineItem[], contractVersion: 0 | 1): TimelineItem[] {
  const allowed = contractVersion === 1
    ? items.filter((item) => ["progress", "action", "interaction"].includes(item.kind))
    : items;
  const hasSubstantiveUpdate = allowed.some((item) => !isInitialProgress(item));
  if (hasSubstantiveUpdate) return allowed.filter((item) => !isInitialProgress(item));
  const initialProgress = allowed.find(isInitialProgress);
  return initialProgress ? [initialProgress] : [];
}

function isInitialProgress(item: TimelineItem): boolean {
  return item.kind === "progress" && item.text === "正在理解你的请求";
}

function normalizedLegacyToolPhase(status: string | undefined): NonNullable<TimelineItem["toolPhase"]> {
  if (status === "completed" || status === "interrupted" || status === "background") return status;
  if (status === "running") return "running";
  return "failed";
}

function legacyActivityParagraphs(item: TimelineItem): string[] {
  return (item.activitySteps ?? [])
    .map((step) => [step.observation, step.analysis, step.action].filter(Boolean).join(" "))
    .filter(Boolean);
}

function legacyToolItemKey(item: TimelineItem, index: number): string {
  return `${item.turnId ?? "legacy"}:${item.runId ?? "run"}:${item.callId ?? index}`;
}

function publicActionItemKey(item: TimelineItem, index: number): string {
  return `${item.turnId ?? "turn"}:${item.actionPhaseInstanceId ?? "phase"}:${item.actionRef ?? index}`;
}
