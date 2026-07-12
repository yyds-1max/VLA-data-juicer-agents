import { useEffect, useState } from "react";

import type { PendingHumanDecision } from "../../api/types";

type HumanDecisionDialogProps = {
  decision: PendingHumanDecision | null;
  onConfirm: () => void | Promise<void>;
  onStop: () => void | Promise<void>;
  onGuide: (text: string) => void | Promise<void>;
  onRecover: (reason: string) => void | Promise<void>;
  recovering?: boolean;
  recoveryError?: string;
};

export function HumanDecisionDialog({
  decision,
  onConfirm,
  onStop,
  onGuide,
  onRecover,
  recovering = false,
  recoveryError,
}: HumanDecisionDialogProps) {
  const [guidance, setGuidance] = useState("");
  const [recoveryReason, setRecoveryReason] = useState("");

  useEffect(() => {
    setGuidance("");
    setRecoveryReason("");
  }, [decision?.replyId, decision?.toolCallId, decision?.recoveryRequired]);

  if (!decision) {
    return null;
  }

  const trimmedGuidance = guidance.trim();
  const trimmedRecoveryReason = recoveryReason.trim();

  const handleGuideSubmit = () => {
    if (!trimmedGuidance) {
      return;
    }
    void onGuide(trimmedGuidance);
    setGuidance("");
  };

  return (
    <section
      role="dialog"
      aria-label="需要确认"
      className="border-t border-console-line bg-console-panel2/45 px-3 py-3 sm:px-4"
    >
      <div className="space-y-3 rounded-lg border border-console-line bg-console-panel px-3 py-3 shadow-sm">
        <p className="text-sm leading-6 text-console-text">
          {decision.summary || "请确认是否继续。"}
        </p>
        {decision.recoveryRequired || decision.submissionDisabled ? (
          <div className="space-y-2">
            <label className="block text-sm text-console-text">
              <span className="mb-1 block">恢复原因</span>
              <textarea
                value={recoveryReason}
                onChange={(event) => setRecoveryReason(event.target.value)}
                aria-label="恢复原因"
                maxLength={4000}
                rows={3}
                disabled={recovering}
                className="w-full rounded-lg border border-console-line bg-console-panel px-3 py-2 text-sm text-console-text outline-none focus:ring-2 focus:ring-console-cyan disabled:opacity-60"
              />
            </label>
            {recoveryError ? (
              <p role="alert" className="text-sm text-red-700">
                {recoveryError}
              </p>
            ) : null}
            <button
              type="button"
              onClick={() => void onRecover(trimmedRecoveryReason)}
              disabled={
                !trimmedRecoveryReason ||
                !decision.planId ||
                !decision.stepId ||
                recovering
              }
              className="rounded-lg bg-console-text px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
            >
              隔离并重新规划
            </button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void onConfirm()}
                className="rounded-lg bg-console-text px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
              >
                确认
              </button>
              <button
                type="button"
                onClick={() => void onStop()}
                className="rounded-lg border border-console-line px-3 py-2 text-sm font-medium text-console-text transition hover:bg-console-panel2 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
              >
                停止
              </button>
            </div>
            <div className="flex items-center gap-2 rounded-lg border border-console-line bg-console-panel px-2 py-2">
              <input
                value={guidance}
                onChange={(event) => setGuidance(event.target.value)}
                aria-label="引导文本"
                placeholder="补充一段引导文本…"
                className="min-w-0 flex-1 bg-transparent text-sm text-console-text outline-none placeholder:text-console-muted"
              />
              <button
                type="button"
                onClick={handleGuideSubmit}
                disabled={!trimmedGuidance}
                className="rounded-lg bg-console-text px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
              >
                发送
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
