import { useEffect, useMemo, useState } from "react";
import { CircleCheck, CirclePause, LoaderCircle, TriangleAlert } from "lucide-react";

import type { TaskSnapshot } from "../../api/types";
import { withoutPercentages } from "../../lib/utils";

type TaskStripProps = {
  tasks: TaskSnapshot[];
};

export function TaskStrip({ tasks }: TaskStripProps) {
  const task = useMemo(() => selectVisibleTask(tasks), [tasks]);
  const active = task ? isNonterminal(task.status) : false;
  const now = useNow(active);
  if (!task) return null;

  const metadata = [
    task.phase ? withoutPercentages(task.phase) : "准备中",
    runtimeText(task.started_at, task.updated_at, active, now),
    countText(task),
  ].filter(Boolean).join(" · ");
  const presentation = statusPresentation(task.status);
  const Icon = presentation.icon;

  return (
    <aside
      className="border-t border-console-line bg-console-panel px-3 pt-3 sm:px-4"
      aria-label={`任务 ${task.task_ref}，${presentation.label}`}
      data-task-ref={task.task_ref}
    >
      <div className="flex items-center gap-2.5 rounded-lg border border-console-line bg-console-panel2/55 px-3 py-2">
        <Icon
          className={`h-4 w-4 shrink-0 ${presentation.color} ${presentation.spinning ? "motion-safe:animate-spin" : ""}`}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xs">
            <span className="font-medium text-console-text">导航任务 {task.task_ref}</span>
            <span className="text-console-muted">{presentation.label}</span>
          </div>
          <p className="mt-0.5 truncate text-[11px] text-console-muted" aria-hidden="true">
            {metadata}
          </p>
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

function statusPresentation(status: TaskSnapshot["status"]) {
  if (status === "paused" || status === "pausing" || status === "waiting_user") {
    return { label: status === "waiting_user" ? "等待输入" : "已暂停", icon: CirclePause, color: "text-amber-600", spinning: false };
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
