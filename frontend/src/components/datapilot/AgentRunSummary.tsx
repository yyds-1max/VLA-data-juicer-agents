import type { PublicToolStatus } from "../../api/types";
import { cn } from "../../lib/utils";

type StatusTone = "success" | "failure" | "interrupted" | "pending";

export function ToolStatusDot({ status }: { status?: PublicToolStatus }) {
  const tone = statusTone(status);
  return (
    <span
      aria-hidden="true"
      data-status={tone}
      className={cn(
        "shrink-0 text-xs leading-none",
        tone === "success" && "text-emerald-600",
        tone === "failure" && "text-rose-600",
        tone === "interrupted" && "text-amber-600",
        tone === "pending" && "text-console-muted",
      )}
    >
      ●
    </span>
  );
}

function statusTone(status: PublicToolStatus | undefined): StatusTone {
  if (status === "success") {
    return "success";
  }
  if (status === "failure") {
    return "failure";
  }
  if (status === "stopped") {
    return "interrupted";
  }
  return "pending";
}
