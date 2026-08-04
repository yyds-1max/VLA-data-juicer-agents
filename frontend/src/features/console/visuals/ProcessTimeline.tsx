import {
  AlertCircle,
  Ban,
  Check,
  CircleDot,
  LoaderCircle,
} from "lucide-react";
import type { ReactNode } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "../../../components/ui/tooltip";
import { cn } from "../../../lib/utils";

export type ProcessTimelineStepState =
  | "completed"
  | "current"
  | "waiting"
  | "pending"
  | "error"
  | "stopped";

export interface ProcessTimelineStep {
  id: string;
  label: string;
  state: ProcessTimelineStepState;
  description?: ReactNode;
  statusLabel?: string;
}

export interface ProcessTimelineProps {
  steps: readonly ProcessTimelineStep[];
  ariaLabel: string;
  className?: string;
  minWidthClassName?: string;
  showSweep?: boolean;
  testIdPrefix?: string;
}

const defaultStateLabels: Record<ProcessTimelineStepState, string> = {
  completed: "已完成",
  current: "进行中",
  waiting: "等待中",
  pending: "未开始",
  error: "处理失败",
  stopped: "已停止",
};

function isCurrentStep(state: ProcessTimelineStepState): boolean {
  return state === "current"
    || state === "waiting"
    || state === "error"
    || state === "stopped";
}

function reachedStepIndex(steps: readonly ProcessTimelineStep[]): number {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (steps[index].state !== "pending") return index;
  }
  return -1;
}

function markerContent(state: ProcessTimelineStepState) {
  if (state === "completed") {
    return <Check className="size-3" strokeWidth={2.75} />;
  }
  if (state === "current") {
    return (
      <LoaderCircle
        className="dashboard-flow-current-spinner absolute size-5"
        strokeWidth={2.25}
      />
    );
  }
  if (state === "waiting") {
    return <CircleDot className="size-3.5" strokeWidth={2.25} />;
  }
  if (state === "error") {
    return <AlertCircle className="size-3.5" strokeWidth={2.25} />;
  }
  if (state === "stopped") {
    return <Ban className="size-3.5" strokeWidth={2.25} />;
  }
  return null;
}

function StepMarker({ step }: { step: ProcessTimelineStep }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "relative flex size-5 shrink-0 items-center justify-center rounded-full border-2 bg-white ring-4 ring-white transition-[border-color,background-color,box-shadow] duration-200 motion-reduce:transition-none",
        step.description && "group-hover:ring-[#3156C8]/15 group-active:ring-[#3156C8]/25 group-data-[state=delayed-open]:ring-[#3156C8]/20 group-data-[state=instant-open]:ring-[#3156C8]/20",
        step.state === "completed" && "border-[#3156C8] bg-[#3156C8] text-white",
        step.state === "current" && "border-transparent text-[#3156C8]",
        step.state === "waiting" && "border-[#7890DD] text-[#3156C8]",
        step.state === "pending" && "border-[#AAB5D8]",
        step.state === "error" && "border-[#E8798E] bg-[#FFF7F8] text-[#D85F78]",
        step.state === "stopped" && "border-[#9AA3B5] bg-[#F6F7F9] text-[#737D90]",
      )}
    >
      {markerContent(step.state)}
    </span>
  );
}

function TimelineStep({
  step,
  index,
}: {
  step: ProcessTimelineStep;
  index: number;
}) {
  const statusLabel = step.statusLabel ?? defaultStateLabels[step.state];
  const showStatus = step.state !== "completed" && step.state !== "pending";
  const markerClassName = cn(
    "group relative mt-1.5 flex size-8 shrink-0 items-center justify-center rounded-full bg-transparent transition-[background-color] duration-200 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[#3156C8] motion-reduce:transition-none",
    step.description && "cursor-help hover:bg-[#F2F5FF] active:bg-[#E8EEFF] data-[state=delayed-open]:bg-[#F2F5FF] data-[state=instant-open]:bg-[#F2F5FF]",
  );

  return (
    <li
      aria-current={isCurrentStep(step.state) ? "step" : undefined}
      aria-label={`${step.label}，${statusLabel}`}
      className="relative flex min-w-0 flex-col items-center px-1 text-center"
      data-flow-state={step.state}
      data-process-state={step.state}
    >
      <span className="flex h-6 min-w-0 max-w-full items-baseline justify-center gap-1.5 whitespace-nowrap">
        <span
          className={cn(
            "text-[0.9375rem] font-semibold tabular-nums tracking-[-0.02em]",
            step.state === "pending" ? "text-[#9AA3B5]" : "text-[#202431]",
          )}
        >
          {String(index + 1).padStart(2, "0")}
        </span>
        <span
          className={cn(
            "min-w-0 truncate text-xs font-medium",
            step.state === "pending" ? "text-[#9AA3B5]" : "text-[#4F586C]",
          )}
        >
          {step.label}
        </span>
      </span>

      {step.description ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label={`${step.label}节点说明`}
              className={markerClassName}
            >
              <StepMarker step={step} />
            </button>
          </TooltipTrigger>
          <TooltipContent
            side="top"
            sideOffset={10}
            className="w-72 max-w-[calc(100vw-2rem)] flex-col items-start gap-1 rounded-xl border border-[#E1E5EE] bg-white px-3.5 py-3 text-left text-[#30374A] shadow-[0_10px_30px_rgba(31,42,68,0.14)] motion-reduce:animate-none [&>svg]:bg-white [&>svg]:fill-white"
          >
            <span className="text-xs font-semibold text-[#202431]">
              {String(index + 1).padStart(2, "0")} · {step.label}
            </span>
            <span className="text-xs leading-5 text-[#626B7D]">
              {step.description}
            </span>
          </TooltipContent>
        </Tooltip>
      ) : (
        <span className={markerClassName}>
          <StepMarker step={step} />
        </span>
      )}

      <span
        className={cn(
          "mt-1 block h-4 text-xs font-medium",
          step.state === "current" && "text-[#3156C8]",
          step.state === "waiting" && "text-[#667BC7]",
          step.state === "error" && "text-[#D85F78]",
          step.state === "stopped" && "text-[#737D90]",
          !showStatus && "text-transparent",
        )}
        aria-hidden={!showStatus}
      >
        {showStatus ? statusLabel : "占位"}
      </span>
    </li>
  );
}

export function ProcessTimeline({
  steps,
  ariaLabel,
  className,
  minWidthClassName = "min-w-[40rem]",
  showSweep = false,
  testIdPrefix = "process-timeline",
}: ProcessTimelineProps) {
  const reachedIndex = reachedStepIndex(steps);
  const progressPercent = reachedIndex < 0
    ? 0
    : steps.length <= 1
      ? 100
      : (reachedIndex / (steps.length - 1)) * 100;
  const edgeInset = steps.length > 0 ? `${50 / steps.length}%` : "0%";

  if (steps.length === 0) {
    return (
      <div
        className={cn(
          "flex min-h-24 items-center justify-center rounded-xl border border-dashed border-[#DDE2EE] bg-[#FAFBFD] px-4 text-sm text-[#626B7D]",
          className,
        )}
        role="status"
      >
        暂无流程信息
      </div>
    );
  }

  return (
    <div
      data-testid={`${testIdPrefix}-scroll-region`}
      className={cn(
        "console-soft-scrollbar overflow-x-auto overscroll-x-contain pb-1",
        className,
      )}
      tabIndex={0}
      role="region"
      aria-label={ariaLabel}
    >
      <div className={cn("relative px-1 pb-1 pt-2", minWidthClassName)}>
        <div
          aria-hidden="true"
          className="absolute top-[3.3125rem] z-0 h-0.5 overflow-hidden rounded-full bg-[#DCE2F0]"
          data-testid={`${testIdPrefix}-track`}
          style={{ left: edgeInset, right: edgeInset }}
        >
          <span
            className="dashboard-flow-progress absolute inset-y-0 left-0 overflow-hidden rounded-full bg-[#3156C8]/80 transition-[width] duration-300 ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none"
            data-testid={`${testIdPrefix}-progress`}
            style={{ width: `${progressPercent}%` }}
          >
            {showSweep && reachedIndex > 0 ? (
              <span className="dashboard-flow-sweep motion-reduce:hidden absolute inset-0" />
            ) : null}
          </span>
        </div>

        <TooltipProvider delayDuration={220} skipDelayDuration={80}>
          <ol
            className="relative z-10 grid"
            style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}
          >
            {steps.map((step, index) => (
              <TimelineStep key={step.id} step={step} index={index} />
            ))}
          </ol>
        </TooltipProvider>
      </div>
    </div>
  );
}
