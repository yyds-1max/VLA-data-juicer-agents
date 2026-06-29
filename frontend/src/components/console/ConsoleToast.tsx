import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type ConsoleToastProps = {
  toast: { message: string; tone: StatusTone } | null;
};

const toneClasses: Record<StatusTone, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-800",
  info: "border-sky-200 bg-sky-50 text-sky-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  danger: "border-rose-200 bg-rose-50 text-rose-800",
  neutral: "border-console-line bg-console-panel text-console-text",
  purple: "border-violet-200 bg-violet-50 text-violet-800",
};

export function ConsoleToast({ toast }: ConsoleToastProps) {
  if (!toast) {
    return null;
  }

  return (
    <div
      role="status"
      className={cn(
        "fixed bottom-5 left-5 z-50 max-w-sm rounded-lg border px-4 py-3 text-sm shadow-lg",
        toneClasses[toast.tone],
      )}
    >
      {toast.message}
    </div>
  );
}
