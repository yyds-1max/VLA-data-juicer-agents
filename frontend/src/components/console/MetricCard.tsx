import type * as React from "react";

import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";
import { StatusTag } from "./StatusTag";

type MetricCardProps = {
  title: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
  meta?: React.ReactNode;
  tone?: StatusTone;
  tag?: React.ReactNode;
  className?: string;
};

export function MetricCard({ title, value, detail, meta, tone = "success", tag, className }: MetricCardProps) {
  return (
    <article className={cn("min-h-36 rounded-lg border border-console-line bg-console-panel p-4 shadow-sm", className)}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-medium text-console-muted">{title}</h2>
          <p className="mt-2 text-2xl font-semibold tracking-normal text-console-text">{value}</p>
        </div>
        {tag ? <StatusTag tone={tone}>{tag}</StatusTag> : null}
      </div>
      {detail ? <p className="mt-2 text-sm text-console-text">{detail}</p> : null}
      {meta ? <p className="mt-4 truncate text-xs text-console-muted">{meta}</p> : null}
    </article>
  );
}
