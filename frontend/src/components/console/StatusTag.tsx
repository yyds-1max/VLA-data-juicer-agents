import type * as React from "react";

import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type StatusTagProps = {
  tone?: StatusTone;
  children: React.ReactNode;
  className?: string;
};

const toneClasses: Record<StatusTone, string> = {
  success: "border-console-cyan/30 bg-console-cyan/10 text-console-cyan",
  info: "border-sky-300/30 bg-sky-300/10 text-sky-300",
  warning: "border-amber-300/30 bg-amber-300/10 text-amber-300",
  danger: "border-rose-400/30 bg-rose-400/10 text-rose-400",
  neutral: "border-console-line bg-console-panel2 text-console-muted",
  purple: "border-violet-400/30 bg-violet-400/10 text-violet-400",
};

export function StatusTag({ tone = "neutral", children, className }: StatusTagProps) {
  return (
    <span className={cn("inline-flex w-fit items-center rounded border px-2 py-0.5 text-xs font-medium", toneClasses[tone], className)}>
      {children}
    </span>
  );
}
