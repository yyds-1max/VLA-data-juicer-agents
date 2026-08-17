import { CheckCircle2, LoaderCircle, TriangleAlert } from "lucide-react";

import { cn } from "../../lib/utils";

export type TrainingOperationState = {
  status: "loading" | "success" | "error";
  title: string;
  detail?: string;
  steps?: string[];
  activeStep?: number;
};

export function TrainingOperationFeedback({ operation, className }: { operation: TrainingOperationState | null; className?: string }) {
  if (!operation) return null;
  const loading = operation.status === "loading";
  const success = operation.status === "success";
  return (
    <div
      role={operation.status === "error" ? "alert" : "status"}
      aria-live="polite"
      aria-busy={loading}
      className={cn(
        "overflow-hidden rounded-lg border px-4 py-3 transition-[border-color,background-color,opacity] duration-180 motion-reduce:transition-none",
        loading && "border-sky-200 bg-sky-50 text-sky-900",
        success && "border-emerald-200 bg-emerald-50 text-emerald-900",
        operation.status === "error" && "border-rose-200 bg-rose-50 text-rose-900",
        className,
      )}
    >
      <div className="flex items-start gap-3">
        {loading ? <LoaderCircle className="mt-0.5 h-5 w-5 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" /> : success ? <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" /> : <TriangleAlert className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">{operation.title}</p>
          {operation.detail ? <p className="mt-1 text-xs leading-5 opacity-80">{operation.detail}</p> : null}
        </div>
      </div>
      {operation.steps?.length ? (
        <ol className="mt-3 grid gap-2 sm:grid-cols-3" aria-label="操作进度">
          {operation.steps.map((step, index) => {
            const current = index === operation.activeStep;
            const done = operation.activeStep != null && index < operation.activeStep;
            return <li key={step} className="flex min-w-0 items-center gap-2 text-xs">
              <span className={cn("flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px] font-semibold", done ? "border-emerald-500 bg-emerald-500 text-white" : current ? "border-sky-600 bg-white text-sky-700" : "border-current/25 bg-white/50 opacity-60")}>{done ? "✓" : index + 1}</span>
              <span className={cn("truncate", !current && !done && "opacity-60")}>{step}</span>
            </li>;
          })}
        </ol>
      ) : null}
    </div>
  );
}
