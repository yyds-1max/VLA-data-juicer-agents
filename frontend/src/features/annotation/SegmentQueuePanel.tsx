import { Collapsible } from "radix-ui";
import { ChevronDown, ChevronRight, Layers3 } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { StatusTag } from "../../components/console/StatusTag";
import { cn } from "../../lib/utils";
import type {
  AnnotationJobDetail,
  AnnotationSegmentStatus,
} from "./types";

const segmentStatusMeta: Record<
  AnnotationSegmentStatus,
  {
    label: string;
    tone: "success" | "info" | "warning" | "danger" | "neutral";
  }
> = {
  pending_initial_annotation: { label: "待标注", tone: "warning" },
  draft: { label: "草稿", tone: "info" },
  submitted: { label: "已提交", tone: "success" },
  skipped: { label: "已跳过", tone: "neutral" },
  tracking: { label: "Tracking 中", tone: "info" },
  tracked: { label: "已完成", tone: "success" },
  postprocessing: { label: "后处理中", tone: "info" },
  annotated: { label: "已标注", tone: "success" },
  postprocessing_failed: { label: "后处理失败", tone: "danger" },
};

const segmentStatusDot: Record<AnnotationSegmentStatus, string> = {
  pending_initial_annotation: "bg-[#E59A18]",
  draft: "bg-[#E59A18]",
  submitted: "bg-[#2FA66A]",
  skipped: "bg-[#7B8496]",
  tracking: "bg-[#3156C8]",
  tracked: "bg-[#536FD7]",
  postprocessing: "bg-[#3156C8]",
  annotated: "bg-[#2FA66A]",
  postprocessing_failed: "bg-[#D84A5B]",
};

const breakdownOrder: AnnotationSegmentStatus[] = [
  "submitted",
  "draft",
  "pending_initial_annotation",
  "skipped",
  "tracking",
  "tracked",
  "postprocessing",
  "annotated",
  "postprocessing_failed",
];

function resolvedSegmentCount(job: Pick<AnnotationJobDetail, "counts">) {
  return job.counts.submitted
    + job.counts.skipped
    + job.counts.tracking
    + job.counts.tracked
    + (job.counts.postprocessing ?? 0)
    + (job.counts.annotated ?? 0);
}

function displayOrdinal(ordinal: number): string {
  if (!Number.isSafeInteger(ordinal) || ordinal < 0) return "—";
  return String(ordinal).padStart(2, "0");
}

function clipStatusSummary(segments: AnnotationJobDetail["segments"]) {
  const failed = segments.filter((segment) => segment.status === "postprocessing_failed").length;
  const waiting = segments.filter((segment) => (
    segment.status === "pending_initial_annotation" || segment.status === "draft"
  )).length;
  const processing = segments.filter((segment) => (
    segment.status === "tracking" || segment.status === "postprocessing"
  )).length;
  const allAnnotated = segments.length > 0 && segments.every((segment) => segment.status === "annotated");

  if (failed > 0) return `${failed} 个异常`;
  if (waiting > 0) return `${waiting} 个待标注`;
  if (processing > 0) return `${processing} 个处理中`;
  if (allAnnotated) return "全部已标注";
  return "全部已处理";
}

export interface SegmentQueuePanelProps {
  job: Pick<AnnotationJobDetail, "segments" | "counts">;
  currentSegmentRef?: string;
  className?: string;
  onNavigate: (segmentRef: string) => void | Promise<void>;
}

export function SegmentQueuePanel({
  job,
  currentSegmentRef,
  className,
  onNavigate,
}: SegmentQueuePanelProps) {
  const headingId = useId();
  const grouped = useMemo(() => {
    const result = new Map<string, typeof job.segments>();
    job.segments.forEach((segment) => {
      const items = result.get(segment.source_clip) ?? [];
      items.push(segment);
      result.set(segment.source_clip, items);
    });
    return [...result.entries()].map(([sourceClip, segments]) => [
      sourceClip,
      [...segments].sort((left, right) => (
        left.ordinal - right.ordinal
        || left.segment_ref.localeCompare(right.segment_ref)
      )),
    ] as const);
  }, [job.segments]);
  const currentSourceClip = useMemo(() => job.segments.find(
    (segment) => segment.segment_ref === currentSegmentRef,
  )?.source_clip, [currentSegmentRef, job.segments]);
  const [openClips, setOpenClips] = useState<Set<string>>(() => {
    const preferred = currentSourceClip ?? grouped[0]?.[0];
    return preferred ? new Set([preferred]) : new Set();
  });
  const clipKey = grouped.map(([sourceClip]) => sourceClip).join("\u0000");
  const previousClipKey = useRef(clipKey);
  const previousCurrentSourceClip = useRef(currentSourceClip);
  const resolved = resolvedSegmentCount(job);
  const breakdown = useMemo(() => breakdownOrder.map((status) => ({
    status,
    count: job.segments.filter((segment) => segment.status === status).length,
  })).filter((item) => item.count > 0), [job.segments]);

  useEffect(() => {
    const availableClips = new Set(grouped.map(([sourceClip]) => sourceClip));
    const preferred = currentSourceClip ?? grouped[0]?.[0];
    const groupsChanged = previousClipKey.current !== clipKey;
    const currentChanged = previousCurrentSourceClip.current !== currentSourceClip;
    previousClipKey.current = clipKey;
    previousCurrentSourceClip.current = currentSourceClip;
    setOpenClips((current) => {
      const next = new Set(
        [...current].filter((sourceClip) => availableClips.has(sourceClip)),
      );
      if (currentChanged && currentSourceClip) next.add(currentSourceClip);
      else if (groupsChanged && next.size === 0 && preferred) next.add(preferred);
      if (
        next.size === current.size
        && [...next].every((sourceClip) => current.has(sourceClip))
      ) {
        return current;
      }
      return next;
    });
  }, [clipKey, currentSourceClip, grouped]);

  return (
    <aside
      aria-labelledby={headingId}
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-xl border border-[#E1E5EE] bg-white shadow-[0_4px_16px_rgba(31,42,68,0.04)]",
        className,
      )}
    >
      <div className="shrink-0 border-b border-[#E7EAF0] px-4 py-3.5">
        <div className="flex items-center justify-between gap-4">
          <h3 id={headingId} className="text-sm font-semibold text-[#202431]">
            Segment 队列
          </h3>
          <p className="shrink-0 text-xs font-medium tabular-nums text-[#657087]">
            {resolved} / {job.counts.total} 完成
          </p>
        </div>
      </div>

      {job.segments.length === 0 ? (
        <p className="px-4 py-6 text-sm leading-6 text-[#7B8496]">
          准备完成后显示内部 Segment 队列。
        </p>
      ) : (
        <nav
          aria-label="Segment 分组队列"
          className="console-soft-scrollbar flex min-h-0 flex-1 flex-col overflow-y-auto bg-white px-4"
          data-testid="segment-queue-scroll"
        >
          {grouped.map(([sourceClip, segments]) => {
            const open = openClips.has(sourceClip);
            const summary = clipStatusSummary(segments);

            return (
              <Collapsible.Root
                key={sourceClip}
                asChild
                open={open}
                onOpenChange={(nextOpen) => {
                  setOpenClips((current) => {
                    const next = new Set(current);
                    if (nextOpen) next.add(sourceClip);
                    else next.delete(sourceClip);
                    return next;
                  });
                }}
              >
                <section
                  aria-label={sourceClip}
                  className="my-2 overflow-hidden rounded-xl border border-[#E1E6F0] bg-white shadow-[0_1px_3px_rgba(31,42,68,0.035)] transition-[border-color,box-shadow] duration-180 data-[state=open]:border-[#CFD8EF] data-[state=open]:shadow-[0_4px_12px_rgba(49,86,200,0.06)] motion-reduce:transition-none"
                >
                  <Collapsible.Trigger asChild>
                    <button
                      type="button"
                      className="flex min-h-15 w-full items-center gap-3 bg-white px-3 py-2.5 text-left transition-[background-color] duration-150 hover:bg-[#F8FAFF] active:bg-[#F1F5FF] focus-visible:relative focus-visible:z-10 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#3156C8] data-[state=open]:bg-[#F8FAFF] motion-reduce:transition-none"
                      aria-label={`${open ? "收起" : "展开"}外层 clip ${sourceClip}`}
                    >
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-[#DCE5FA] bg-[#F1F5FF] text-[#3156C8]">
                        <Layers3 aria-hidden="true" className="h-4 w-4" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span
                          className="block truncate text-xs font-semibold text-[#343C4D]"
                          title={sourceClip}
                        >
                          {sourceClip}
                        </span>
                        <span className="mt-0.5 block truncate text-[11px] text-[#7B8496]">
                          <span className="tabular-nums">{segments.length} 个 Segment</span>
                          <span aria-hidden="true"> · </span>
                          {summary}
                        </span>
                      </span>
                      <span
                        className={cn(
                          "flex h-7 w-7 shrink-0 items-center justify-center rounded-full border transition-[color,background-color,border-color] duration-150 motion-reduce:transition-none",
                          open
                            ? "border-[#C8D5F5] bg-white text-[#3156C8]"
                            : "border-transparent bg-[#F1F3F7] text-[#8B94A6]",
                        )}
                      >
                        <ChevronDown
                          aria-hidden="true"
                          className={cn(
                            "h-4 w-4 transition-transform duration-180 motion-reduce:transition-none",
                            open && "rotate-180",
                          )}
                        />
                      </span>
                    </button>
                  </Collapsible.Trigger>
                  <Collapsible.Content className="segment-clip-content overflow-hidden">
                    <div className="divide-y divide-[#ECEFF4] border-t border-[#E7EAF0] px-2 py-1">
                      {segments.map((segment) => {
                        const status = segmentStatusMeta[segment.status];
                        const ordinal = displayOrdinal(segment.ordinal);
                        const active = segment.segment_ref === currentSegmentRef;

                        return (
                          <button
                            key={segment.segment_ref}
                            type="button"
                            aria-current={active ? "page" : undefined}
                            aria-label={`${active ? "当前" : "打开"} Segment ${ordinal}，${status.label}`}
                            className={cn(
                              "relative -mx-1 flex min-h-13 w-[calc(100%+0.5rem)] items-center gap-2.5 rounded-lg border px-2 py-2.5 text-left transition-[color,background-color,border-color] duration-150 focus-visible:z-10 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[#3156C8] motion-reduce:transition-none",
                              active
                                ? "border-[#7C97E7] bg-[#F4F7FF]"
                                : "border-transparent bg-white hover:border-[#D8DEEC] hover:bg-[#F8FAFF] active:bg-[#EEF3FF]",
                            )}
                            onClick={() => {
                              if (!active) void onNavigate(segment.segment_ref);
                            }}
                          >
                            <span
                              aria-hidden="true"
                              className={cn("h-2 w-2 shrink-0 rounded-full", segmentStatusDot[segment.status])}
                            />
                            <span
                              aria-hidden="true"
                              className={cn(
                                "w-7 shrink-0 text-center text-xs font-medium tabular-nums",
                                active ? "text-[#3156C8]" : "text-[#657087]",
                              )}
                            >
                              {ordinal}
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-xs font-medium text-[#343C4D]">
                                Segment {ordinal}
                              </span>
                            </span>
                            <StatusTag className="shrink-0" tone={status.tone}>
                              {status.label}
                            </StatusTag>
                            <ChevronRight
                              aria-hidden="true"
                              className={cn(
                                "h-4 w-4 shrink-0",
                                active ? "text-[#3156C8]" : "text-[#A1A9B8]",
                              )}
                            />
                          </button>
                        );
                      })}
                    </div>
                  </Collapsible.Content>
                </section>
              </Collapsible.Root>
            );
          })}
          {openClips.size === 0 ? (
            <div className="flex min-h-40 flex-1 flex-col items-center justify-center px-6 py-8 text-center">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-[#E1E6F0] bg-[#F7F9FC] text-[#8B94A6]">
                <Layers3 aria-hidden="true" className="h-4 w-4" />
              </span>
              <p className="mt-3 text-xs font-medium text-[#657087]">选择一个外层 clip</p>
              <p className="mt-1 text-[11px] leading-5 text-[#8B94A6]">展开后查看内部 Segment 队列</p>
            </div>
          ) : null}
        </nav>
      )}

      {job.segments.length > 0 ? (
        <footer className="shrink-0 border-t border-[#E7EAF0] bg-white px-4 py-3">
          <p className="text-xs text-[#657087]">
            总计 <span className="font-semibold tabular-nums text-[#343C4D]">{job.counts.total}</span> 个 Segment
          </p>
          <ul aria-label="Segment 状态汇总" className="mt-2 flex flex-wrap gap-x-3 gap-y-1.5">
            {breakdown.map(({ status, count }) => (
              <li key={status} className="flex items-center gap-1.5 text-[11px] text-[#657087]">
                <span aria-hidden="true" className={cn("h-2 w-2 rounded-full", segmentStatusDot[status])} />
                <span>{segmentStatusMeta[status].label}</span>
                <span className="font-semibold tabular-nums text-[#343C4D]">{count}</span>
              </li>
            ))}
          </ul>
        </footer>
      ) : null}
    </aside>
  );
}
