import { useEffect, useRef, useState } from "react";
import {
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDashed,
  Eye,
  Play,
  TriangleAlert,
} from "lucide-react";

import { cn } from "../../lib/utils";
import type { ActivityStep, TimelineItem } from "../../store/eventReducer";

type ReActActivityCardProps = {
  item: TimelineItem;
};

export function ReActActivityCard({ item }: ReActActivityCardProps) {
  const status = item.activityStatus || item.status || "running";
  const steps = item.activitySteps ?? [];
  const running = status === "running";
  const keepOpen = running || status === "waiting";
  const [expanded, setExpanded] = useState(keepOpen);
  const userToggledRef = useRef(false);

  useEffect(() => {
    if (keepOpen) {
      userToggledRef.current = false;
      setExpanded(true);
    } else if (!userToggledRef.current) {
      setExpanded(false);
    }
  }, [keepOpen]);

  const handleToggle = () => {
    userToggledRef.current = true;
    setExpanded((value) => !value);
  };

  return (
    <article className="mr-auto w-full max-w-[92%] overflow-hidden rounded-lg border border-console-line bg-console-panel text-console-text shadow-sm">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={handleToggle}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition hover:bg-console-panel2/60 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-console-cyan"
      >
        <ActivityStatusIcon status={status} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{activitySummary(item, steps, status)}</span>
          {keepOpen && latestStepText(steps) ? (
            <span className="mt-0.5 block truncate text-xs text-console-muted">{latestStepText(steps)}</span>
          ) : null}
        </span>
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-console-muted" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-console-muted" aria-hidden="true" />
        )}
      </button>

      {expanded ? (
        <div className="space-y-2 border-t border-console-line/80 bg-console-bg/35 px-3 py-3">
          {steps.length > 0 ? (
            steps.map((step) => <ActivityStepView key={step.id} step={step} />)
          ) : (
            <div className="flex items-center gap-2 text-xs text-console-muted">
              <CircleDashed className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              正在观察当前环境并整理下一步动作
            </div>
          )}
        </div>
      ) : null}
    </article>
  );
}

function ActivityStepView({ step }: { step: ActivityStep }) {
  return (
    <section className="rounded-md border border-console-line/80 bg-console-panel px-3 py-2.5">
      <div className="mb-2 flex items-center gap-2 text-[11px] text-console-muted">
        <StepStatusIcon status={step.status} />
        <span>步骤 {step.sequence}</span>
        <span className="ml-auto">{stepStatusText(step.status)}</span>
      </div>
      <div className="space-y-2">
        {step.observation ? (
          <ActivityField icon={Eye} label="观察" text={step.observation} />
        ) : null}
        {step.analysis ? (
          <ActivityField icon={BrainCircuit} label="思考" text={step.analysis} />
        ) : null}
        {step.action ? (
          <ActivityField icon={Play} label="行动" text={step.action} />
        ) : null}
      </div>
    </section>
  );
}

function ActivityField({
  icon: Icon,
  label,
  text,
}: {
  icon: typeof Eye;
  label: string;
  text: string;
}) {
  return (
    <div className="grid grid-cols-[52px_1fr] gap-2 text-xs leading-5">
      <span className="flex items-start gap-1.5 pt-0.5 font-medium text-console-muted">
        <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        {label}
      </span>
      <span className="min-w-0 whitespace-pre-wrap break-words text-console-text">{text}</span>
    </div>
  );
}

function ActivityStatusIcon({ status }: { status: string }) {
  if (status === "completed") {
    return <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" aria-hidden="true" />;
  }
  if (status === "failed" || status === "interrupted") {
    return <TriangleAlert className="h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />;
  }
  if (status === "waiting") {
    return <CircleDashed className="h-3.5 w-3.5 text-amber-600" aria-hidden="true" />;
  }
  return (
    <CircleDashed
      className={cn("h-4 w-4 shrink-0 text-console-cyan", status === "running" && "animate-spin")}
      aria-hidden="true"
    />
  );
}

function StepStatusIcon({ status }: { status: string }) {
  if (status === "completed") {
    return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" aria-hidden="true" />;
  }
  if (status === "failed" || status === "interrupted") {
    return <TriangleAlert className="h-3.5 w-3.5 text-amber-600" aria-hidden="true" />;
  }
  if (status === "waiting") {
    return <CircleDashed className="h-3.5 w-3.5 text-amber-600" aria-hidden="true" />;
  }
  return <CircleDashed className="h-3.5 w-3.5 animate-spin text-console-cyan" aria-hidden="true" />;
}

function activitySummary(item: TimelineItem, steps: ActivityStep[], status: string): string {
  if (status === "completed") {
    return `已完成 ${steps.length} 个处理步骤`;
  }
  if (status === "failed") {
    return "处理过程遇到问题";
  }
  if (status === "interrupted") {
    return "处理过程已中断";
  }
  if (status === "waiting") {
    return "等待你的确认";
  }
  return item.activityTitle || item.text || "正在分析并处理请求";
}

function latestStepText(steps: ActivityStep[]): string {
  const step = steps[steps.length - 1];
  return step?.action || step?.analysis || step?.observation || "";
}

function stepStatusText(status: string): string {
  if (status === "completed") {
    return "已完成";
  }
  if (status === "failed") {
    return "未完成";
  }
  if (status === "interrupted") {
    return "已中断";
  }
  if (status === "waiting") {
    return "等待确认";
  }
  if (status === "acting") {
    return "正在行动";
  }
  return "正在思考";
}
