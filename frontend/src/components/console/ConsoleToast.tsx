import type { StatusTone } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type ConsoleToastProps = {
  toast: { message: string; tone: StatusTone } | null;
};

const toneClasses: Record<StatusTone, string> = {
  success: "border-console-cyan/30 bg-console-cyan/10 text-console-cyan",
  info: "border-sky-300/30 bg-sky-300/10 text-sky-200",
  warning: "border-amber-300/30 bg-amber-300/10 text-amber-200",
  danger: "border-rose-400/30 bg-rose-400/10 text-rose-200",
  neutral: "border-console-line bg-console-panel text-console-text",
  purple: "border-violet-400/30 bg-violet-400/10 text-violet-200",
};

export function ConsoleToast({ toast }: ConsoleToastProps) {
  if (!toast) {
    return null;
  }

  return (
    <div
      role="status"
      className={cn(
        "fixed bottom-5 left-5 z-50 max-w-sm rounded border px-4 py-3 text-sm shadow-[0_18px_42px_rgba(0,0,0,0.32)] backdrop-blur",
        toneClasses[toast.tone],
      )}
    >
      {toast.message}
    </div>
  );
}
