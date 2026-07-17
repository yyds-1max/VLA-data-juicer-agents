import * as Dialog from "@radix-ui/react-dialog";
import { Check, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { NavigationDateSummary } from "../../../api/types";
import { ConsoleButton } from "../../../components/console/ConsoleButton";
import type { NavigationDatasetSelection } from "../navigationDataPilotRequest";

type NavigationDataPilotDialogProps = {
  open: boolean;
  dates: NavigationDateSummary[];
  submitting: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: (selection: NavigationDatasetSelection) => void;
  onSelectionChange: () => void;
};

export function NavigationDataPilotDialog({
  open,
  dates,
  submitting,
  error,
  onCancel,
  onConfirm,
  onSelectionChange,
}: NavigationDataPilotDialogProps) {
  const [selectedDate, setSelectedDate] = useState("");
  const [selectedClips, setSelectedClips] = useState<Set<string>>(() => new Set());
  const [wholeDate, setWholeDate] = useState(false);
  const [attentionPulse, setAttentionPulse] = useState<0 | 1 | 2>(0);
  const wasOpenRef = useRef(false);
  const attentionTimerRef = useRef<number | null>(null);
  const lastAttentionAtRef = useRef(0);

  useEffect(() => {
    if (open && !wasOpenRef.current) {
      setSelectedDate("");
      setSelectedClips(new Set());
      setWholeDate(false);
      setAttentionPulse(0);
      lastAttentionAtRef.current = 0;
    }
    wasOpenRef.current = open;
  }, [open]);

  useEffect(() => () => {
    if (attentionTimerRef.current !== null) {
      window.clearTimeout(attentionTimerRef.current);
    }
  }, []);

  const dateSummary = useMemo(
    () => dates.find((candidate) => candidate.date === selectedDate) ?? null,
    [dates, selectedDate],
  );
  const clips = dateSummary?.clips ?? [];
  const canSubmit = Boolean(selectedDate) && (wholeDate || selectedClips.size > 0);

  function changeSelection(change: () => void) {
    onSelectionChange();
    change();
  }

  function handleDateChange(date: string) {
    changeSelection(() => {
      setSelectedDate(date);
      setSelectedClips(new Set());
      setWholeDate(false);
    });
  }

  function handleWholeDateChange(checked: boolean) {
    changeSelection(() => {
      setWholeDate(checked);
      setSelectedClips(new Set());
    });
  }

  function handleClipChange(clip: string, checked: boolean) {
    changeSelection(() => {
      if (wholeDate) {
        setWholeDate(false);
        setSelectedClips(new Set(clips.filter((candidate) => candidate.clip !== clip).map((candidate) => candidate.clip)));
        return;
      }

      const next = new Set(selectedClips);
      if (checked) {
        next.add(clip);
      } else {
        next.delete(clip);
      }

      if (clips.length > 0 && next.size === clips.length) {
        setWholeDate(true);
        setSelectedClips(new Set());
      } else {
        setSelectedClips(next);
      }
    });
  }

  function handleCancel() {
    if (submitting) {
      return;
    }
    if (attentionTimerRef.current !== null) {
      window.clearTimeout(attentionTimerRef.current);
      attentionTimerRef.current = null;
    }
    setAttentionPulse(0);
    setSelectedDate("");
    setSelectedClips(new Set());
    setWholeDate(false);
    onCancel();
  }

  function handleOutsideAttention() {
    if (submitting) {
      return;
    }
    const now = Date.now();
    if (now - lastAttentionAtRef.current < 100) {
      return;
    }
    lastAttentionAtRef.current = now;
    if (attentionTimerRef.current !== null) {
      window.clearTimeout(attentionTimerRef.current);
    }
    setAttentionPulse((current) => current === 1 ? 2 : 1);
    attentionTimerRef.current = window.setTimeout(() => {
      setAttentionPulse(0);
      attentionTimerRef.current = null;
    }, 1200);
  }

  function handleConfirm() {
    if (!canSubmit || submitting || !selectedDate) {
      return;
    }
    if (wholeDate) {
      onConfirm({ scope: "date", date: selectedDate });
      return;
    }
    const orderedClips = clips
      .map((clip) => clip.clip)
      .filter((clip) => selectedClips.has(clip));
    if (orderedClips.length === 0) {
      return;
    }
    onConfirm({
      scope: "clips",
      date: selectedDate,
      clips: orderedClips as [string, ...string[]],
    });
  }

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) {
          handleCancel();
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay
          className="fixed inset-0 z-[90] bg-slate-950/25"
          data-testid="navigation-datapilot-overlay"
          onMouseDown={handleOutsideAttention}
        />
        <Dialog.Content
          aria-describedby="navigation-datapilot-description"
          className={`fixed left-1/2 top-1/2 z-[91] flex max-h-[min(680px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-xl border border-console-line bg-console-panel shadow-2xl outline outline-2 outline-offset-2 outline-transparent focus:outline-none ${
            attentionPulse === 1
              ? "animate-[navigation-dialog-attention-a_760ms_ease-in-out] motion-reduce:animate-none motion-reduce:outline-slate-400"
              : attentionPulse === 2
                ? "animate-[navigation-dialog-attention-b_760ms_ease-in-out] motion-reduce:animate-none motion-reduce:outline-slate-400"
                : ""
          }`}
          data-testid="navigation-datapilot-dialog"
          onEscapeKeyDown={(event) => {
            if (submitting) {
              event.preventDefault();
            }
          }}
          onInteractOutside={(event) => {
            event.preventDefault();
            handleOutsideAttention();
          }}
        >
          <div className="flex items-start justify-between gap-4 border-b border-console-line px-5 py-4">
            <div>
              <Dialog.Title className="text-base font-semibold text-console-text">交给 DataPilot</Dialog.Title>
              <Dialog.Description id="navigation-datapilot-description" className="mt-1 text-sm text-console-muted">
                选择一个日期，并指定该日期下需要处理的 clips。
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                aria-label="关闭数据选择"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus-visible:bg-console-panel2 focus-visible:text-console-text disabled:cursor-not-allowed disabled:opacity-40"
                disabled={submitting}
                type="button"
              >
                <X aria-hidden="true" className="h-4 w-4" />
              </button>
            </Dialog.Close>
          </div>

          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
            <label className="block space-y-2">
              <span className="text-sm font-semibold text-console-text">数据日期</span>
              <select
                aria-label="数据日期"
                className="h-10 w-full rounded-lg border border-console-line bg-console-panel px-3 text-sm text-console-text outline-none focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/20 disabled:cursor-not-allowed disabled:opacity-60"
                disabled={submitting}
                value={selectedDate}
                onChange={(event) => handleDateChange(event.target.value)}
              >
                <option value="">请选择日期</option>
                {dates.map((date) => (
                  <option key={date.date} value={date.date}>
                    {date.date}（{date.clip_count} 个 clips）
                  </option>
                ))}
              </select>
            </label>

            {selectedDate ? (
              <fieldset className="space-y-3" disabled={submitting}>
                <legend className="text-sm font-semibold text-console-text">选择 clips</legend>
                {clips.length === 0 ? (
                  <div className="rounded-lg border border-console-line bg-console-panel2/60 px-4 py-4 text-sm text-console-muted">
                    该日期暂无可选择的 clip。
                  </div>
                ) : (
                  <div className="overflow-hidden rounded-lg border border-console-line">
                    <label className="flex cursor-pointer items-center gap-3 border-b border-console-line bg-console-panel2/70 px-4 py-3 text-sm font-semibold text-console-text">
                      <span className={`flex h-4 w-4 items-center justify-center rounded border ${wholeDate ? "border-console-cyan bg-console-cyan text-white" : "border-console-line bg-white"}`}>
                        {wholeDate ? <Check aria-hidden="true" className="h-3 w-3" /> : null}
                      </span>
                      <input
                        className="sr-only"
                        type="checkbox"
                        checked={wholeDate}
                        onChange={(event) => handleWholeDateChange(event.target.checked)}
                      />
                      全选
                    </label>
                    <div className="console-soft-scrollbar max-h-72 divide-y divide-console-line overflow-y-auto">
                      {clips.map((clip) => {
                        const checked = wholeDate || selectedClips.has(clip.clip);
                        return (
                          <label key={clip.clip} className="flex cursor-pointer items-center gap-3 px-4 py-3 text-sm text-console-text hover:bg-console-panel2/60">
                            <span className={`flex h-4 w-4 items-center justify-center rounded border ${checked ? "border-console-cyan bg-console-cyan text-white" : "border-console-line bg-white"}`}>
                              {checked ? <Check aria-hidden="true" className="h-3 w-3" /> : null}
                            </span>
                            <input
                              aria-label={clip.clip}
                              className="sr-only"
                              type="checkbox"
                              checked={checked}
                              onChange={(event) => handleClipChange(clip.clip, event.target.checked)}
                            />
                            <span className="min-w-0 flex-1 truncate" title={clip.clip}>{clip.clip}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                )}
              </fieldset>
            ) : null}

            {error ? (
              <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {error}
              </div>
            ) : null}
          </div>

          <div className="flex justify-end gap-2 border-t border-console-line px-5 py-4">
            <ConsoleButton disabled={submitting} onClick={handleCancel}>取消</ConsoleButton>
            <ConsoleButton variant="primary" disabled={!canSubmit || submitting} onClick={handleConfirm}>确定</ConsoleButton>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
