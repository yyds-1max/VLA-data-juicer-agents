import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type ProgressBarProps = {
  value: number;
  tone?: Extract<StatusTone, "success" | "info" | "warning" | "danger" | "purple" | "neutral">;
  label?: string;
  className?: string;
};

const fillClasses: Record<NonNullable<ProgressBarProps["tone"]>, string> = {
  success: "bg-console-cyan",
  info: "bg-sky-300",
  warning: "bg-amber-300",
  danger: "bg-rose-400",
  purple: "bg-violet-400",
  neutral: "bg-console-muted",
};

export function ProgressBar({ value, tone = "success", label, className }: ProgressBarProps) {
  const normalizedValue = Math.min(100, Math.max(0, value));

  return (
    <div className={cn("space-y-1.5", className)}>
      {label ? <div className="text-xs text-console-muted">{label}</div> : null}
      <div
        className="h-2 overflow-hidden rounded-full bg-console-bg"
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
