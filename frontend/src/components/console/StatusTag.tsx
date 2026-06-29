import type * as React from "react";

import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type StatusTagProps = {
  tone?: StatusTone;
  children: React.ReactNode;
  className?: string;
};

const toneClasses: Record<StatusTone, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-700",
  info: "border-sky-200 bg-sky-50 text-sky-700",
  warning: "border-amber-200 bg-amber-50 text-amber-700",
  danger: "border-rose-200 bg-rose-50 text-rose-700",
  neutral: "border-console-line bg-console-panel2 text-console-muted",
  purple: "border-violet-200 bg-violet-50 text-violet-700",
};

export function StatusTag({ tone = "neutral", children, className }: StatusTagProps) {
  return (
    <span className={cn("inline-flex w-fit items-center rounded-md border px-2 py-0.5 text-xs font-medium", toneClasses[tone], className)}>
      {children}
    </span>
  );
}
