import { useId, useMemo, useState } from "react";

import { StatusTag } from "../../components/console/StatusTag";
import { Input } from "../../components/ui/input";
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
  const jumpInputId = useId();
  const jumpErrorId = useId();
  const [jumpValue, setJumpValue] = useState("");
  const [jumpError, setJumpError] = useState("");
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
  const ordinalRange = useMemo(() => {
    const ordinals = job.segments
      .map((segment) => segment.ordinal)
      .filter((ordinal) => Number.isSafeInteger(ordinal) && ordinal >= 0);

    return ordinals.length > 0
      ? { min: Math.min(...ordinals), max: Math.max(...ordinals) }
      : undefined;
  }, [job.segments]);
  const resolved = resolvedSegmentCount(job);

  const submitJump = () => {
    const normalized = jumpValue.trim();
    if (normalized.length === 0) {
      setJumpError("请输入 Segment 序号。");
      return;
    }
    if (!/^\d+$/.test(normalized)) {
      setJumpError("请输入有效的整数序号。");
      return;
    }

    const ordinal = Number(normalized);
    if (!Number.isSafeInteger(ordinal)) {
      setJumpError("请输入有效的整数序号。");
      return;
    }

    const target = job.segments.find((segment) => segment.ordinal === ordinal);
    if (!target) {
      setJumpError("未找到该序号对应的 Segment。");
      return;
    }

    setJumpError("");
    if (target.segment_ref !== currentSegmentRef) {
      void onNavigate(target.segment_ref);
    }
  };

  return (
    <aside
      aria-labelledby={headingId}
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-xl border border-[#E1E5EE] bg-white shadow-[0_4px_16px_rgba(31,42,68,0.04)]",
        className,
      )}
    >
      <div className="shrink-0 border-b border-[#E7EAF0] px-4 py-3.5">
        <h3 id={headingId} className="text-sm font-semibold text-[#202431]">
          Segment 队列
        </h3>
        <p className="mt-1 text-xs text-[#7B8496]">
          {resolved}/{job.counts.total} 个 Segment 已处理
        </p>
        {job.segments.length > 0 ? (
          <form
            className="mt-3"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              submitJump();
            }}
          >
            <label
              className="mb-1.5 block text-[11px] font-medium text-[#657087]"
              htmlFor={jumpInputId}
            >
              跳转至序号
            </label>
            <div className="flex items-center gap-2">
              <Input
                id={jumpInputId}
                type="number"
                inputMode="numeric"
                min={ordinalRange?.min}
                max={ordinalRange?.max}
                step={1}
                value={jumpValue}
                aria-describedby={jumpErrorId}
                aria-invalid={jumpError ? true : undefined}
                className="h-8 min-w-0 flex-1 bg-white text-xs tabular-nums"
                placeholder={ordinalRange
                  ? `${ordinalRange.min}–${ordinalRange.max}`
                  : "输入序号"}
                onChange={(event) => {
                  setJumpValue(event.target.value);
                  if (jumpError) setJumpError("");
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    submitJump();
                  }
                }}
              />
              <button
                type="submit"
                className="inline-flex h-8 shrink-0 items-center justify-center rounded-lg border border-[#D8DEEC] bg-white px-3 text-xs font-medium text-[#3156C8] shadow-xs transition-[color,background-color,border-color,box-shadow] duration-150 hover:border-[#BCC8E6] hover:bg-[#F3F6FF] active:bg-[#E9EEFF] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156C8] motion-reduce:transition-none"
              >
                跳转
              </button>
            </div>
            <p
              id={jumpErrorId}
              aria-live="polite"
              className="mt-1 h-4 truncate text-[11px] leading-4 text-[#D84A5B]"
              title={jumpError || undefined}
            >
              {jumpError}
            </p>
          </form>
        ) : null}
      </div>

      {job.segments.length === 0 ? (
        <p className="px-4 py-6 text-sm leading-6 text-[#7B8496]">
          准备完成后显示内部 Segment 队列。
        </p>
      ) : (
        <nav
          aria-label="Segment 数字序号跳转"
          className="console-soft-scrollbar min-h-0 flex-1 space-y-4 overflow-y-auto bg-[#F5F6F8] p-2.5"
          data-testid="segment-queue-scroll"
        >
          {grouped.map(([sourceClip, segments]) => (
            <section key={sourceClip} aria-label={sourceClip}>
              <p
                className="mb-1.5 truncate px-2 pt-1 text-[11px] font-semibold text-[#7B8496]"
                title={sourceClip}
              >
                {sourceClip}
              </p>
              <div className="space-y-1">
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
                        "flex min-h-12 w-full items-center gap-3 rounded-lg border px-2.5 py-2 text-left transition-[color,background-color,border-color,box-shadow] duration-150 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[#3156C8] motion-reduce:transition-none",
                        active
                          ? "border-[#D8DEEC] bg-white shadow-[0_2px_8px_rgba(31,42,68,0.08)]"
                          : "border-transparent bg-transparent hover:border-[#E2E6EE] hover:bg-white/75 active:bg-white",
                      )}
                      onClick={() => {
                        if (!active) void onNavigate(segment.segment_ref);
                      }}
                    >
                      <span
                        aria-hidden="true"
                        className={cn(
                          "flex min-w-8 shrink-0 items-center justify-center rounded-md px-1.5 py-1 text-sm font-semibold tabular-nums",
                          active
                            ? "bg-[#E9EEFF] text-[#3156C8]"
                            : "bg-[#E9EBEF] text-[#657087]",
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
                    </button>
                  );
                })}
              </div>
            </section>
          ))}
        </nav>
      )}
    </aside>
  );
}
