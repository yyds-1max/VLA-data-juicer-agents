import { Collapsible } from "radix-ui";
import { ChevronDown, ChevronRight, Layers3 } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { StatusTag } from "../../components/console/StatusTag";
import { cn } from "../../lib/utils";
import { trajectoryReviewPresentation } from "./reviewPresentation";
import type { TrajectoryReview } from "./types";

const STATUS_DOT = {
  success: "bg-[#2FA66A]",
  info: "bg-[#3156C8]",
  warning: "bg-[#E59A18]",
  danger: "bg-[#D84A5B]",
  neutral: "bg-[#7B8496]",
} as const;

function displayOrdinal(ordinal: number): string {
  return String(Math.max(ordinal, 0)).padStart(2, "0");
}

function isResolved(review: TrajectoryReview): boolean {
  const key = trajectoryReviewPresentation(review).key;
  return key === "verified" || key === "discarded";
}

export type ReviewSegmentQueuePanelProps = {
  reviews: TrajectoryReview[];
  currentReviewRef: string;
  className?: string;
  disabled?: boolean;
  layout?: "vertical" | "horizontal";
  onNavigate: (reviewRef: string) => void | Promise<void>;
};

export function ReviewSegmentQueuePanel({
  reviews,
  currentReviewRef,
  className,
  disabled = false,
  layout = "vertical",
  onNavigate,
}: ReviewSegmentQueuePanelProps) {
  const headingId = useId();
  const grouped = useMemo(() => {
    // Segment 序号在每个外层 clip 内重新排序；不能使用跨 clip 的全局 ordinal 展示。
    const byClip = new Map<string, TrajectoryReview[]>();
    reviews.forEach((review) => {
      const items = byClip.get(review.source_clip) ?? [];
      items.push(review);
      byClip.set(review.source_clip, items);
    });
    return [...byClip.entries()]
      .map(([sourceClip, items]) => [
        sourceClip,
        [...items].sort((left, right) => (
          left.segment_ordinal - right.segment_ordinal
          || left.review_ref.localeCompare(right.review_ref)
        )),
      ] as const)
      .sort(([left], [right]) => left.localeCompare(right));
  }, [reviews]);
  const currentSourceClip = useMemo(() => reviews.find(
    (review) => review.review_ref === currentReviewRef,
  )?.source_clip, [currentReviewRef, reviews]);
  const [openClips, setOpenClips] = useState<Set<string>>(() => {
    const preferred = currentSourceClip ?? grouped[0]?.[0];
    return preferred ? new Set([preferred]) : new Set();
  });
  const clipKey = grouped.map(([sourceClip]) => sourceClip).join("\u0000");
  const previousClipKey = useRef(clipKey);
  const previousCurrentClip = useRef(currentSourceClip);
  const resolved = reviews.filter(isResolved).length;
  const verified = reviews.filter((review) => (
    trajectoryReviewPresentation(review).key === "verified"
  )).length;

  useEffect(() => {
    // 数据刷新时保留用户手动展开的组；导航到另一个 Segment 时仅补充展开其所属 clip。
    const available = new Set(grouped.map(([sourceClip]) => sourceClip));
    const preferred = currentSourceClip ?? grouped[0]?.[0];
    const groupsChanged = previousClipKey.current !== clipKey;
    const currentChanged = previousCurrentClip.current !== currentSourceClip;
    previousClipKey.current = clipKey;
    previousCurrentClip.current = currentSourceClip;
    setOpenClips((current) => {
      const next = new Set([...current].filter((sourceClip) => available.has(sourceClip)));
      if (currentChanged && currentSourceClip) next.add(currentSourceClip);
      else if (groupsChanged && next.size === 0 && preferred) next.add(preferred);
      if (
        next.size === current.size
        && [...next].every((sourceClip) => current.has(sourceClip))
      ) return current;
      return next;
    });
  }, [clipKey, currentSourceClip, grouped]);

  return (
    <aside
      aria-labelledby={headingId}
      data-layout={layout}
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-xl border border-[#E1E5EE] bg-white shadow-[0_4px_16px_rgba(31,42,68,0.04)]",
        className,
      )}
    >
      <header className="shrink-0 border-b border-[#E7EAF0] px-4 py-3.5">
        <div className="flex items-center justify-between gap-3">
          <h3 id={headingId} className="text-sm font-semibold text-[#202431]">Segment 复核队列</h3>
          <span className="shrink-0 text-xs font-medium tabular-nums text-[#657087]">
            {resolved} / {reviews.length} 已处理
          </span>
        </div>
        <p className="mt-1 text-[11px] leading-4 text-[#7B8496]">
          按外层 clip 分组，Segment 序号在组内独立排序
        </p>
      </header>

      {reviews.length === 0 ? (
        <div className="flex min-h-44 flex-1 flex-col items-center justify-center px-5 text-center">
          <Layers3 aria-hidden="true" className="size-5 text-[#9AA4B7]" />
          <p className="mt-3 text-xs font-medium text-[#657087]">暂无同日期复核任务</p>
        </div>
      ) : (
        <nav
          aria-label="人工复核 Segment 分组队列"
          className={cn(
            "console-soft-scrollbar min-h-0 flex-1 bg-white",
            layout === "horizontal"
              ? "review-queue-horizontal-scroll flex items-start gap-3 overflow-x-scroll overflow-y-hidden px-3 pb-3 pt-2"
              : "overflow-y-auto px-3 py-1",
          )}
          data-testid="review-segment-queue-scroll"
        >
          {grouped.map(([sourceClip, clipReviews]) => {
            const open = openClips.has(sourceClip);
            const clipResolved = clipReviews.filter(isResolved).length;
            return (
              <Collapsible.Root
                key={sourceClip}
                asChild
                open={open}
                onOpenChange={(nextOpen) => setOpenClips((current) => {
                  const next = new Set(current);
                  if (nextOpen) next.add(sourceClip);
                  else next.delete(sourceClip);
                  return next;
                })}
              >
                <section
                  aria-label={`外层 clip ${sourceClip}`}
                  className={cn(
                    "rounded-xl border border-[#E1E6F0] bg-white transition-[border-color,box-shadow] duration-180 data-[state=open]:border-[#CFD8EF] data-[state=open]:shadow-[0_4px_12px_rgba(49,86,200,0.06)] motion-reduce:transition-none",
                    layout === "horizontal"
                      ? "my-0 flex w-max shrink-0 items-stretch overflow-visible"
                      : "my-2 overflow-hidden",
                  )}
                >
                  <Collapsible.Trigger asChild>
                    <button
                      type="button"
                      aria-label={`${open ? "收起" : "展开"}外层 clip ${sourceClip}`}
                      className={cn(
                        "flex items-center gap-2.5 bg-white text-left transition-[background-color] duration-150 hover:bg-[#F8FAFF] active:bg-[#F1F5FF] focus-visible:relative focus-visible:z-10 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#3156C8] data-[state=open]:bg-[#F8FAFF] motion-reduce:transition-none",
                        layout === "horizontal"
                          ? "min-h-14 w-[13rem] shrink-0 rounded-xl px-2.5 py-2"
                          : "min-h-15 w-full px-3 py-2.5",
                      )}
                    >
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-[#DCE5FA] bg-[#F1F5FF] text-[#3156C8]">
                        <Layers3 aria-hidden="true" className="size-4" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-xs font-semibold text-[#343C4D]" title={sourceClip}>
                          {sourceClip}
                        </span>
                        <span className="mt-0.5 block text-[11px] tabular-nums text-[#7B8496]">
                          {clipReviews.length} 个 Segment · {clipResolved} 已处理
                        </span>
                      </span>
                      <span className={cn(
                        "flex size-7 shrink-0 items-center justify-center rounded-full border transition-[color,background-color,border-color] duration-150 motion-reduce:transition-none",
                        open
                          ? "border-[#C8D5F5] bg-white text-[#3156C8]"
                          : "border-transparent bg-[#F1F3F7] text-[#8B94A6]",
                      )}>
                        <ChevronDown
                          aria-hidden="true"
                          className={cn(
                            "size-4 transition-transform duration-180 motion-reduce:transition-none",
                            open && "rotate-180",
                          )}
                        />
                      </span>
                    </button>
                  </Collapsible.Trigger>
                  <Collapsible.Content className={cn(
                    "segment-clip-content overflow-hidden",
                    layout === "horizontal" && "review-segment-horizontal-content shrink-0",
                  )}>
                    <div className={cn(
                      "border-[#E7EAF0]",
                      layout === "horizontal"
                        ? "flex gap-2 border-l px-2 py-1"
                        : "divide-y divide-[#ECEFF4] border-t px-2 py-1",
                    )}>
                      {clipReviews.map((item, index) => {
                        const presentation = trajectoryReviewPresentation(item);
                        const active = item.review_ref === currentReviewRef;
                        const ordinal = displayOrdinal(index + 1);
                        return (
                          <button
                            key={item.review_ref}
                            type="button"
                            aria-current={active ? "page" : undefined}
                            aria-label={`${active ? "当前" : "打开"} Segment ${ordinal}，${presentation.label}`}
                            disabled={!active && disabled}
                            className={cn(
                              "relative flex min-h-13 items-center gap-2 rounded-lg border px-2 py-2 text-left transition-[color,background-color,border-color,opacity] duration-150 focus-visible:z-10 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[#3156C8] disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none",
                              layout === "horizontal"
                                ? "w-[11.75rem] shrink-0"
                                : "-mx-1 w-[calc(100%+0.5rem)]",
                              active
                                ? "border-[#7C97E7] bg-[#F4F7FF]"
                                : "border-transparent bg-white hover:border-[#D8DEEC] hover:bg-[#F8FAFF] active:bg-[#EEF3FF]",
                            )}
                            onClick={() => {
                              if (!active && !disabled) void onNavigate(item.review_ref);
                            }}
                          >
                            <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", STATUS_DOT[presentation.tone])} />
                            <span className={cn(
                              "w-6 shrink-0 text-center text-xs font-medium tabular-nums",
                              active ? "text-[#3156C8]" : "text-[#657087]",
                            )}>
                              {ordinal}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-xs font-medium text-[#343C4D]">
                              Segment {ordinal}
                            </span>
                            <StatusTag className="shrink-0" tone={presentation.tone}>
                              {presentation.label}
                            </StatusTag>
                            <ChevronRight aria-hidden="true" className={cn("size-4 shrink-0", active ? "text-[#3156C8]" : "text-[#A1A9B8]")} />
                          </button>
                        );
                      })}
                    </div>
                  </Collapsible.Content>
                </section>
              </Collapsible.Root>
            );
          })}
        </nav>
      )}

      {reviews.length > 0 && (
        <footer className="shrink-0 border-t border-[#E7EAF0] bg-white px-4 py-3">
          <p className="text-xs text-[#657087]">
            已验证 <strong className="font-semibold tabular-nums text-[#343C4D]">{verified}</strong>
            <span aria-hidden="true"> · </span>
            总计 <strong className="font-semibold tabular-nums text-[#343C4D]">{reviews.length}</strong> 个 Segment
          </p>
        </footer>
      )}
    </aside>
  );
}
