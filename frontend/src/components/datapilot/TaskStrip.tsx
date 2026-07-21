import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  CircleCheck,
  CirclePause,
  LoaderCircle,
  TriangleAlert,
} from "lucide-react";

import type { TaskSnapshot } from "../../api/types";
import { withoutPercentages } from "../../lib/utils";

type TaskStripProps = {
  tasks: TaskSnapshot[];
};

export function TaskStrip({ tasks }: TaskStripProps) {
  const task = useMemo(() => selectVisibleTask(tasks), [tasks]);
  const clockRunning = task ? isClockRunning(task.status) : false;
  const now = useNow(clockRunning);
  const measurementRef = useRef<HTMLDivElement | null>(null);
  const measurementKey = task
    ? `${task.phase || fallbackPhase(task.status)}:${statusPresentation(task.status).label}`
    : "";
  const capsuleWidth = useAdaptiveCapsuleWidth(measurementRef, measurementKey);
  if (!task) return null;

  const presentation = statusPresentation(task.status);
  const Icon = presentation.icon;
  const phase = withoutPercentages(task.phase || fallbackPhase(task.status));
  const runtime = runtimeText(task.started_at, task.updated_at, clockRunning, now);
  const count = countText(task);
  const detailsId = `datapilot-task-details-${task.task_ref}`;
  const selection = selectionText(task);
  const latestUpdate = withoutPercentages(
    task.latest_public_update || task.wait_cause || task.waiting_reason || "暂无更多状态说明",
  );

  return (
    <aside
      className="group pointer-events-none absolute inset-x-3 bottom-3 z-20 flex justify-center"
      data-task-ref={task.task_ref}
    >
      <div className="pointer-events-auto relative max-w-[50%]">
        <div
          className="flex w-max max-w-full cursor-default items-center gap-2 overflow-hidden whitespace-nowrap rounded-full border border-console-line bg-console-panel/95 px-3 py-2 text-xs text-console-text shadow-[0_6px_18px_rgba(23,32,46,0.09)] backdrop-blur-sm outline-none transition-[width,border-color,box-shadow] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] hover:border-console-cyan/35 hover:shadow-[0_8px_22px_rgba(23,32,46,0.12)] focus-visible:border-console-cyan/50 focus-visible:ring-2 focus-visible:ring-console-cyan/20 motion-reduce:transition-none"
          style={capsuleWidth ? { width: `${capsuleWidth}px` } : undefined}
          tabIndex={0}
          aria-label={`导航任务 ${task.task_ref}，${phase}，${presentation.label}`}
          aria-describedby={detailsId}
        >
          <Icon
            className={`h-3.5 w-3.5 shrink-0 ${presentation.color} ${presentation.spinning ? "motion-safe:animate-spin" : ""}`}
            aria-hidden="true"
          />
          <span className="min-w-0 truncate font-medium">{phase}</span>
          <span className="h-3 w-px shrink-0 bg-console-line" aria-hidden="true" />
          <span className="shrink-0 text-console-muted">{presentation.label}</span>
        </div>

        <div
          ref={measurementRef}
          className="pointer-events-none absolute invisible flex w-max items-center gap-2 whitespace-nowrap rounded-full border px-3 py-2 text-xs"
          aria-hidden="true"
        >
          <Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="font-medium">{phase}</span>
          <span className="h-3 w-px shrink-0" />
          <span>{presentation.label}</span>
        </div>

        <div
          id={detailsId}
          role="tooltip"
          className="pointer-events-none invisible absolute bottom-full left-1/2 mb-2 w-[min(320px,calc(100vw-3.5rem))] -translate-x-1/2 translate-y-1 rounded-xl border border-console-line bg-console-panel p-3 opacity-0 shadow-[0_10px_26px_rgba(23,32,46,0.12)] transition-[opacity,transform,visibility] duration-150 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:visible group-focus-within:translate-y-0 group-focus-within:opacity-100 motion-reduce:transition-none"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-xs font-semibold text-console-text">
                导航任务 {task.task_ref}
              </p>
              <p className="mt-0.5 text-[11px] text-console-muted">{presentation.label}</p>
            </div>
            <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${presentation.color}`} aria-hidden="true" />
          </div>
          <dl className="mt-3 grid grid-cols-[56px_minmax(0,1fr)] gap-x-2 gap-y-1.5 text-[11px] leading-4">
            <dt className="text-console-muted">当前阶段</dt>
            <dd className="min-w-0 break-words text-console-text">{phase}</dd>
            <dt className="text-console-muted">处理范围</dt>
            <dd className="min-w-0 break-words text-console-text">{task.dataset_date} · {selection}</dd>
            {runtime ? (
              <>
                <dt className="text-console-muted">耗时</dt>
                <dd className="text-console-text">{runtime}</dd>
              </>
            ) : null}
            {count ? (
              <>
                <dt className="text-console-muted">可靠计数</dt>
                <dd className="text-console-text">{count}</dd>
              </>
            ) : null}
            <dt className="text-console-muted">最新状态</dt>
            <dd className="min-w-0 break-words text-console-text">{latestUpdate}</dd>
          </dl>
        </div>
      </div>
    </aside>
  );
}

function selectVisibleTask(tasks: TaskSnapshot[]): TaskSnapshot | undefined {
  return tasks.find((task) => isNonterminal(task.status)) ?? tasks[0];
}

function isNonterminal(status: TaskSnapshot["status"]): boolean {
  return !["cancelled", "completed", "failed", "superseded"].includes(status);
}

function isClockRunning(status: TaskSnapshot["status"]): boolean {
  return ["active", "pausing", "cancelling"].includes(status);
}

function statusPresentation(status: TaskSnapshot["status"]) {
  if (status === "waiting_user") {
    return { label: "等待你的选择", icon: CirclePause, color: "text-amber-600", spinning: false };
  }
  if (status === "paused") {
    return { label: "已暂停", icon: CirclePause, color: "text-amber-600", spinning: false };
  }
  if (status === "pausing") {
    return { label: "正在安全停止", icon: LoaderCircle, color: "text-amber-600", spinning: true };
  }
  if (status === "failed" || status === "needs_replan") {
    return { label: status === "failed" ? "处理失败" : "需要调整", icon: TriangleAlert, color: "text-rose-600", spinning: false };
  }
  if (["completed", "cancelled", "superseded"].includes(status)) {
    return { label: status === "completed" ? "已完成" : "已结束", icon: CircleCheck, color: "text-emerald-600", spinning: false };
  }
  return {
    label: status === "cancelling" ? "正在取消" : "处理中",
    icon: LoaderCircle,
    color: "text-console-cyan",
    spinning: true,
  };
}

function fallbackPhase(status: TaskSnapshot["status"]): string {
  if (status === "waiting_user") return "等待确认";
  if (status === "completed") return "导航任务完成";
  if (["cancelled", "superseded"].includes(status)) return "导航任务结束";
  if (status === "failed") return "导航任务异常";
  if (status === "needs_replan") return "调整处理方案";
  if (status === "paused" || status === "pausing") return "导航任务暂停";
  if (status === "cancelling") return "结束导航任务";
  return "准备导航数据";
}

function selectionText(task: TaskSnapshot): string {
  if (task.selection.kind === "all_clips") return "全部 clips";
  return task.selection.clips.map(withoutPercentages).join("、");
}

function runtimeText(startedAt: string, updatedAt: string, active: boolean, now: number): string {
  const started = Date.parse(startedAt);
  const ended = active ? now : Date.parse(updatedAt);
  if (Number.isNaN(started) || Number.isNaN(ended)) return "";
  const seconds = Math.max(Math.floor((ended - started) / 1000), 0);
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${seconds}s`;
}

function countText(task: TaskSnapshot): string {
  const count = task.count;
  if (!count || count.total <= 0 || count.done < 0) return "";
  return `${Math.min(count.done, count.total)}/${count.total} ${withoutPercentages(count.unit)}`.trim();
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function useAdaptiveCapsuleWidth(
  measurementRef: RefObject<HTMLDivElement | null>,
  measurementKey: string,
): number | null {
  const [width, setWidth] = useState<number | null>(null);

  useLayoutEffect(() => {
    const measurement = measurementRef.current;
    if (!measurement) return undefined;
    const taskRegion = measurement.closest("aside");

    const measure = () => {
      const intrinsicWidth = Math.ceil(
        Math.max(measurement.scrollWidth, measurement.getBoundingClientRect().width),
      );
      if (intrinsicWidth <= 0) return;
      const regionWidth = taskRegion?.getBoundingClientRect().width || window.innerWidth - 24;
      const regionLimit = Math.max(1, Math.floor(regionWidth / 2));
      const nextWidth = Math.min(intrinsicWidth, regionLimit);
      setWidth((current) => current === nextWidth ? current : nextWidth);
    };

    measure();
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(measure);
    observer?.observe(measurement);
    if (taskRegion) observer?.observe(taskRegion);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [measurementKey, measurementRef]);

  return width;
}
