import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type WheelEvent } from "react";

import { cn } from "../../lib/utils";
import type { AnnotationSegmentStatus, AnnotationSegmentSummary } from "./types";

const MAX_VISIBLE_TICKS = 21;
const EDGE_FADE_TICKS = 5;
const JUMP_BUFFER_TIMEOUT_MS = 900;

const STATUS_META: Record<
  AnnotationSegmentStatus,
  { label: string; color: string }
> = {
  pending_initial_annotation: { label: "待标注", color: "bg-[#a78bfa]" },
  draft: { label: "草稿", color: "bg-[#f2b84b]" },
  submitted: { label: "已提交", color: "bg-[#4fc58a]" },
  skipped: { label: "已跳过", color: "bg-[#8b95a7]" },
  tracking: { label: "Tracking 中", color: "bg-[#39b9d6]" },
  tracked: { label: "Tracking 完成", color: "bg-[#5c83df]" },
  postprocessing: { label: "后处理中", color: "bg-[#586bd6]" },
  annotated: { label: "已标注", color: "bg-[#35b779]" },
  postprocessing_failed: { label: "后处理失败", color: "bg-[#ee6b7a]" },
};

function canUseGlobalShortcut(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return true;
  return !target.closest("input, textarea, select, [contenteditable='true']");
}

export type SegmentRulerProps = {
  segments: AnnotationSegmentSummary[];
  currentSegmentRef: string;
  onNavigate: (segmentRef: string) => void | Promise<void>;
  disabled?: boolean;
  className?: string;
};

export function SegmentRuler({
  segments,
  currentSegmentRef,
  onNavigate,
  disabled = false,
  className,
}: SegmentRulerProps) {
  const sorted = useMemo(
    () => [...segments].sort((left, right) => (
      left.ordinal - right.ordinal || left.segment_ref.localeCompare(right.segment_ref)
    )),
    [segments],
  );
  const currentIndex = Math.max(0, sorted.findIndex((item) => item.segment_ref === currentSegmentRef));
  const current = sorted[currentIndex];
  const resolvedCount = sorted.filter((item) => (
    item.status === "submitted"
    || item.status === "skipped"
    || item.status === "tracking"
    || item.status === "tracked"
    || item.status === "postprocessing"
    || item.status === "annotated"
  )).length;
  const windowStart = Math.max(
    0,
    Math.min(
      currentIndex - Math.floor(MAX_VISIBLE_TICKS / 2),
      Math.max(0, sorted.length - MAX_VISIBLE_TICKS),
    ),
  );
  const visible = sorted.slice(windowStart, windowStart + MAX_VISIBLE_TICKS);
  const wheelDeltaRef = useRef(0);
  const jumpBufferRef = useRef("");
  const jumpTimeoutRef = useRef<number | null>(null);
  const [jumpAnnouncement, setJumpAnnouncement] = useState("");

  const navigateToIndex = (index: number) => {
    if (disabled || index < 0 || index >= sorted.length) return;
    const next = sorted[index];
    if (next && next.segment_ref !== currentSegmentRef) {
      void onNavigate(next.segment_ref);
    }
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (disabled || !canUseGlobalShortcut(event.target)) return;
      if (/^\d$/.test(event.key)) {
        jumpBufferRef.current = `${jumpBufferRef.current}${event.key}`.slice(-4);
        setJumpAnnouncement(`已输入 Segment 序号 ${jumpBufferRef.current}，按回车跳转`);
        if (jumpTimeoutRef.current !== null) window.clearTimeout(jumpTimeoutRef.current);
        jumpTimeoutRef.current = window.setTimeout(() => {
          jumpBufferRef.current = "";
          setJumpAnnouncement("");
          jumpTimeoutRef.current = null;
        }, JUMP_BUFFER_TIMEOUT_MS);
        return;
      }
      if (event.key !== "Enter" || jumpBufferRef.current.length === 0) return;
      const ordinal = Number(jumpBufferRef.current);
      jumpBufferRef.current = "";
      setJumpAnnouncement("");
      if (jumpTimeoutRef.current !== null) window.clearTimeout(jumpTimeoutRef.current);
      jumpTimeoutRef.current = null;
      const index = sorted.findIndex((item) => item.ordinal === ordinal);
      if (index >= 0) {
        event.preventDefault();
        navigateToIndex(index);
      } else {
        setJumpAnnouncement(`未找到 Segment ${ordinal}`);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      if (jumpTimeoutRef.current !== null) window.clearTimeout(jumpTimeoutRef.current);
    };
  }, [currentSegmentRef, disabled, sorted]);

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (disabled || sorted.length < 2) return;
    wheelDeltaRef.current += event.deltaY || event.deltaX;
    if (Math.abs(wheelDeltaRef.current) < 36) return;
    event.preventDefault();
    const direction = wheelDeltaRef.current > 0 ? 1 : -1;
    wheelDeltaRef.current = 0;
    navigateToIndex(currentIndex + direction);
  };

  if (!current || sorted.length === 0) return null;

  return (
    <div
      data-testid="segment-ruler"
      className={cn(
        "flex min-h-14 max-w-[min(46rem,calc(100vw-2rem))] items-center gap-3 rounded-[14px] border border-white/10 bg-[#141a26]/93 px-3.5 py-2 text-white opacity-60 shadow-[0_10px_28px_rgba(15,23,42,0.28)] backdrop-blur-md transition-[opacity,background-color] duration-160 hover:opacity-100 focus-within:opacity-100 motion-reduce:transition-none",
        className,
      )}
      onWheel={onWheel}
    >
      <div className="flex shrink-0 items-center gap-2">
        <span className={cn("size-2 rounded-full", STATUS_META[current.status].color)} aria-hidden="true" />
        <strong className="text-base font-semibold tabular-nums text-[#f0f2f7]">{String(current.ordinal).padStart(2, "0")}</strong>
        <span className="text-[11px] tabular-nums text-[#8b95ab]">/ {sorted.length}</span>
        <span className="hidden border-l border-white/12 pl-2 text-[11px] text-[#a7b0c4] sm:inline">
          {STATUS_META[current.status].label}
        </span>
      </div>

      <button
        type="button"
        aria-label="上一个 Segment"
        disabled={disabled || currentIndex === 0}
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white/65 transition-[color,background-color,opacity] duration-150 hover:bg-white/10 hover:text-white active:bg-white/15 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-30 motion-reduce:transition-none"
        onClick={() => navigateToIndex(currentIndex - 1)}
      >
        <ChevronLeft className="size-4" aria-hidden="true" />
      </button>

      <div className="flex h-8 min-w-0 flex-1 items-end justify-center border-b border-white/16 px-0.5 pb-1" aria-label="Segment 状态刻度">
        {visible.map((item, visibleIndex) => {
          const isCurrent = item.segment_ref === currentSegmentRef;
          const meta = STATUS_META[item.status];
          const hasMoreBefore = windowStart > 0;
          const hasMoreAfter = windowStart + visible.length < sorted.length;
          let edgeOpacity = 1;
          if (hasMoreBefore) {
            edgeOpacity = Math.min(
              edgeOpacity,
              0.12 + 0.88 * Math.min(1, visibleIndex / EDGE_FADE_TICKS),
            );
          }
          if (hasMoreAfter) {
            edgeOpacity = Math.min(
              edgeOpacity,
              0.12 + 0.88 * Math.min(1, (visible.length - 1 - visibleIndex) / EDGE_FADE_TICKS),
            );
          }
          return (
            <button
              key={item.segment_ref}
              type="button"
              aria-label={`Segment ${String(item.ordinal).padStart(2, "0")}，${meta.label}`}
              aria-current={isCurrent ? "step" : undefined}
              title={`Segment ${item.ordinal} · ${meta.label}`}
              disabled={disabled}
              className="group flex h-8 w-[9px] shrink-0 items-end justify-center rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed"
              style={{ opacity: isCurrent ? 1 : edgeOpacity }}
              onClick={() => {
                if (item.segment_ref !== currentSegmentRef) void onNavigate(item.segment_ref);
              }}
            >
              <span
                className={cn(
                  "block h-[11px] w-[3px] rounded-sm transition-[height,width,box-shadow] duration-150 group-hover:h-[22px] motion-reduce:transition-none",
                  meta.color,
                  (windowStart + visibleIndex) % 5 === 0 && "h-[17px]",
                  isCurrent && "h-[27px] w-1 shadow-[0_0_0_1.5px_rgba(255,255,255,0.45)] group-hover:h-[27px]",
                )}
              />
            </button>
          );
        })}
      </div>

      <button
        type="button"
        aria-label="下一个 Segment"
        disabled={disabled || currentIndex >= sorted.length - 1}
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white/65 transition-[color,background-color,opacity] duration-150 hover:bg-white/10 hover:text-white active:bg-white/15 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-30 motion-reduce:transition-none"
        onClick={() => navigateToIndex(currentIndex + 1)}
      >
        <ChevronRight className="size-4" aria-hidden="true" />
      </button>

      <div className="hidden shrink-0 border-l border-white/15 pl-3 text-[11px] text-white/60 md:block">
        <span className="tabular-nums text-white/80">{resolvedCount}/{sorted.length}</span> 完成
      </div>
      <span className="sr-only" aria-live="polite">{jumpAnnouncement}</span>
    </div>
  );
}
