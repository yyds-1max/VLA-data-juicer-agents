import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type ProgressBarProps = {
  value: number;
  tone?: Extract<StatusTone, "success" | "info" | "warning" | "danger" | "purple" | "neutral">;
  label?: string;
  className?: string;
};

const fillClasses: Record<NonNullable<ProgressBarProps["tone"]>, string> = {
  success: "bg-emerald-600",
  info: "bg-console-cyan",
  warning: "bg-amber-500",
  danger: "bg-rose-600",
  purple: "bg-violet-600",
  neutral: "bg-console-muted",
};

export function ProgressBar({ value, tone = "success", label, className }: ProgressBarProps) {
  const normalizedValue = Math.min(100, Math.max(0, value));

  return (
    <div className={cn("space-y-1.5", className)}>
      {label ? <div className="text-xs text-console-muted">{label}</div> : null}
      <div
        className="h-2 overflow-hidden rounded-full bg-slate-100"
        role="progressbar"
        aria-valuenow={normalizedValue}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div className={cn("h-full rounded-full transition-[width]", fillClasses[tone])} style={{ width: `${normalizedValue}%` }} />
      </div>
    </div>
  );
}
