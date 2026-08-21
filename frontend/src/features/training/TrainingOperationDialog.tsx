import { useEffect } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import { TrainingOperationFeedback, type TrainingOperationState } from "./TrainingOperationFeedback";

export function TrainingOperationDialog({
  open,
  operation,
  onOpenChange,
  autoCloseMs = 1400,
}: {
  open: boolean;
  operation: TrainingOperationState | null;
  onOpenChange: (open: boolean) => void;
  autoCloseMs?: number | null;
}) {
  useEffect(() => {
    if (!open || operation?.status !== "success" || autoCloseMs == null || autoCloseMs <= 0) return;
    const timer = window.setTimeout(() => onOpenChange(false), autoCloseMs);
    return () => window.clearTimeout(timer);
  }, [autoCloseMs, onOpenChange, open, operation?.status]);

  return (
    <Dialog open={open && Boolean(operation)} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" aria-busy={operation?.status === "loading"}>
        <DialogHeader className="sr-only">
          <DialogTitle>操作进度</DialogTitle>
          <DialogDescription>显示当前操作的执行进度和结果。</DialogDescription>
        </DialogHeader>
        <TrainingOperationFeedback operation={operation} className="border-0 bg-transparent p-1" />
        {operation?.status === "success" && autoCloseMs != null && autoCloseMs > 0 ? <p className="text-center text-xs text-console-muted">窗口即将自动关闭</p> : null}
      </DialogContent>
    </Dialog>
  );
}
