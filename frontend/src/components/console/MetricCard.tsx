import type * as React from "react";
import { AlertCircle, RotateCcw, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

import { cn } from "../../lib/utils";

type MetricCardProps = {
  title: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
  icon: LucideIcon;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  className?: string;
};

export function MetricCard({ title, value, detail, icon: Icon, loading = false, error, onRetry, className }: MetricCardProps) {
  return (
    <Card
      role="article"
      aria-busy={loading || undefined}
      className={cn(
        "min-h-36 gap-0 overflow-visible rounded-2xl bg-white py-0 ring-1 ring-[#E7EAF1] shadow-[0_8px_26px_rgba(34,48,78,0.055)]",
        className,
      )}
    >
      <CardContent className="flex min-h-36 flex-col justify-between p-5">
          <div className="flex items-start justify-between gap-4">
            <h2 className="min-w-0 truncate text-sm font-medium text-[#687186]">{title}</h2>
            <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-[#F0F2FC] text-[#3156C8]" aria-hidden="true">
              <Icon className="size-[18px]" strokeWidth={1.9} />
            </span>
          </div>

          {loading ? (
            <div className="space-y-2" role="status" aria-label={`${title}加载中`}>
              <Skeleton className="h-8 w-28 rounded-md bg-[#EEF0F5]" />
              <Skeleton className="h-4 w-40 max-w-full rounded bg-[#F2F3F7]" />
            </div>
          ) : error ? (
            <div className="flex min-h-14 items-end justify-between gap-3" role="alert">
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-sm font-medium text-[#BD3E58]">
                  <AlertCircle className="size-4 shrink-0" aria-hidden="true" />
                  <span>加载失败</span>
                </div>
                <p className="mt-1 truncate text-xs text-[#626B7D]" title={error}>无法读取数据汇总</p>
              </div>
              {onRetry ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 shrink-0 gap-1.5 rounded-lg px-2.5 text-xs text-[#3156C8] transition-[color,background-color,box-shadow] duration-150 hover:bg-[#EEF1FB] active:bg-[#E5EAF9] focus-visible:ring-2 focus-visible:ring-[#3156C8]/30 motion-reduce:transition-none"
                  onClick={onRetry}
                >
                  <RotateCcw className="size-3.5" aria-hidden="true" />
                  重试
                </Button>
              ) : null}
            </div>
          ) : (
            <div className="min-w-0">
              <p className="truncate text-[30px] font-semibold leading-none tracking-[-0.035em] tabular-nums text-[#202431]" title={typeof value === "string" ? value : undefined}>
                {value}
              </p>
              {detail ? <p className="mt-2 truncate text-sm text-[#626B7D]" title={typeof detail === "string" ? detail : undefined}>{detail}</p> : null}
            </div>
          )}
      </CardContent>
    </Card>
  );
}
