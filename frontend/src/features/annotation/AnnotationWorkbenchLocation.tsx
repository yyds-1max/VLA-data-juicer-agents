import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";

import { ConsoleButton } from "../../components/console/ConsoleButton";
import { StatusTag } from "../../components/console/StatusTag";
import type { StatusTone } from "../console/consoleTypes";
import { cn } from "../../lib/utils";

type AnnotationWorkbenchLocationProps = {
  datasetDate: string;
  sourceClip: string;
  segmentOrdinal: number;
  segmentCount?: number;
  statusLabel: string;
  statusTone: StatusTone;
  backLabel: string;
  navigationLabel: string;
  onBack: () => void;
  className?: string;
  actions?: ReactNode;
};

export function AnnotationWorkbenchLocation({
  datasetDate,
  sourceClip,
  segmentOrdinal,
  segmentCount,
  statusLabel,
  statusTone,
  backLabel,
  navigationLabel,
  onBack,
  className,
  actions,
}: AnnotationWorkbenchLocationProps) {
  return (
    <div className={cn(
      "flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between",
      className,
    )}>
      <div className="flex min-w-0 items-start gap-3">
        <ConsoleButton
          className="size-9 shrink-0 rounded-xl px-0"
          aria-label={backLabel}
          onClick={onBack}
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
        </ConsoleButton>
        <nav aria-label={navigationLabel} className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1">
            <h2 className="shrink-0 text-lg font-semibold leading-6 text-[#202938]">
              {datasetDate}
            </h2>
            <span className="text-[#a0a7b5]" aria-hidden="true">·</span>
            <strong
              aria-label={`外层 clip：${sourceClip}`}
              className="max-w-28 truncate text-lg font-semibold leading-6 text-[#202938] sm:max-w-52 lg:max-w-80"
              title={`外层 clip：${sourceClip}`}
            >
              {sourceClip}
            </strong>
            <StatusTag tone={statusTone}>{statusLabel}</StatusTag>
          </div>
          <p className="mt-0.5 flex min-w-0 items-center gap-1 text-sm leading-5 text-[#6f7a8e]">
            <span className="max-w-28 truncate sm:max-w-52 lg:max-w-80" title={sourceClip}>
              {sourceClip}
            </span>
            <span aria-hidden="true">·</span>
            <span className="shrink-0">Segment {String(segmentOrdinal).padStart(2, "0")}</span>
            {segmentCount !== undefined && (
              <span className="sr-only">，当前外层 clip 共 {segmentCount} 个 Segment</span>
            )}
          </p>
        </nav>
      </div>
      {actions && (
        <div className="flex min-w-0 items-center gap-2 self-stretch sm:max-w-[42%] sm:shrink-0 sm:self-start">
          {actions}
        </div>
      )}
    </div>
  );
}
