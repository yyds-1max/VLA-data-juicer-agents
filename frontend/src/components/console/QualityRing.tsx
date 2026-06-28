import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type QualityRingProps = {
  value: number;
  label?: string;
  tone?: StatusTone;
  className?: string;
};

const strokeClasses: Record<StatusTone, string> = {
  success: "stroke-console-cyan text-console-cyan",
  info: "stroke-sky-300 text-sky-300",
  warning: "stroke-amber-300 text-amber-300",
  danger: "stroke-rose-400 text-rose-400",
  neutral: "stroke-console-muted text-console-muted",
  purple: "stroke-violet-400 text-violet-400",
};

export function QualityRing({ value, label, tone = "success", className }: QualityRingProps) {
  const normalizedValue = Math.min(100, Math.max(0, value));
  const radius = 28;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference - (normalizedValue / 100) * circumference;

  return (
    <div className={cn("inline-flex items-center gap-3", className)}>
      <div className="relative h-16 w-16" aria-label={label} role="img">
        <svg className="h-16 w-16 -rotate-90" viewBox="0 0 72 72" aria-hidden="true">
          <circle cx="36" cy="36" r={radius} className="fill-none stroke-console-bg" strokeWidth="7" />
          <circle
            cx="36"
            cy="36"
            r={radius}
            className={cn("fill-none transition-[stroke-dashoffset]", strokeClasses[tone])}
            strokeWidth="7"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
          />
        </svg>
        <span className="absolute inset-0 flex items-center justify-center text-sm font-semibold text-console-text">{normalizedValue}%</span>
      </div>
      {label ? <span className="text-xs text-console-muted">{label}</span> : null}
    </div>
  );
}
