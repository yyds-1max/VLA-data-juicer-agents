import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check } from "lucide-react";

import type { PendingInteraction } from "../../api/types";
import { cn, withoutPercentages } from "../../lib/utils";

type InteractionPanelProps = {
  interaction: PendingInteraction;
  submitting?: boolean;
  error?: string;
  onSubmit: (optionIds: string[]) => void;
};

export function InteractionPanel({
  interaction,
  submitting = false,
  error,
  onSubmit,
}: InteractionPanelProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const multiple = interaction.kind === "multi_select";
  const calibration = interaction.kind === "calibration_preview";
  const calibrationChoice =
    calibration &&
    interaction.options.some((option) =>
      option.option_id.startsWith("calibration_"),
    );
  useEffect(() => {
    setSelected([]);
  }, [interaction.interaction_id, interaction.interaction_revision]);
  const expired = useMemo(() => {
    if (!interaction.expires_at) return false;
    const timestamp = Date.parse(interaction.expires_at);
    return !Number.isNaN(timestamp) && timestamp <= Date.now();
  }, [interaction.expires_at]);

  const choose = (optionId: string) => {
    if (submitting || expired) return;
    if (calibrationChoice && optionId !== "reject") {
      setSelected([optionId]);
      return;
    }
    if (!multiple) {
      onSubmit([optionId]);
      return;
    }
    setSelected((current) =>
      current.includes(optionId)
        ? current.filter((candidate) => candidate !== optionId)
        : [...current, optionId],
    );
  };

  return (
    <section
      aria-labelledby={`interaction-title-${interaction.interaction_id}`}
      className="relative z-30 shrink-0 bg-transparent px-3 pb-3 pt-2 sm:px-4 sm:pb-4"
      data-interaction-id={interaction.interaction_id}
      data-blocking={interaction.blocking ? "true" : "false"}
    >
      <div
        data-interaction-surface="content"
        className="rounded-2xl border border-console-line/90 bg-console-panel/95 p-3 shadow-[0_2px_10px_rgba(23,32,46,0.06)] backdrop-blur-xs"
      >
        <div className="flex items-start gap-2.5">
          {interaction.risk === "high" ? (
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-amber-50 text-amber-700">
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            </span>
          ) : null}
          <div className="min-w-0 flex-1">
            <h2
              id={`interaction-title-${interaction.interaction_id}`}
              className="text-sm font-semibold text-console-text"
            >
              {withoutPercentages(interaction.title)}
            </h2>
            {interaction.summary ? (
              <p className="mt-1 whitespace-pre-wrap wrap-break-word text-xs leading-5 text-console-muted">
                {withoutPercentages(interaction.summary)}
              </p>
            ) : null}
          </div>
        </div>
      </div>

      <div
        data-interaction-surface="options"
        className="mt-2 flex flex-wrap gap-2"
        aria-label="可选操作"
      >
        {interaction.options.map((option) => {
          const checked = selected.includes(option.option_id);
          if (calibrationChoice && option.option_id !== "reject") {
            return (
              <label
                key={option.option_id}
                className={cn(
                  "flex min-h-14 min-w-48 flex-1 basis-56 cursor-pointer items-center gap-3 rounded-xl border bg-white px-4 py-3 text-sm shadow-[0_1px_7px_rgba(23,32,46,0.045)] transition-[border-color,background-color,box-shadow] duration-150 motion-reduce:transition-none",
                  checked
                    ? "border-console-cyan bg-blue-50/70 ring-1 ring-console-cyan/20"
                    : "border-console-line/90 hover:border-console-cyan/30 hover:bg-slate-50/80",
                  (submitting || expired) && "cursor-not-allowed opacity-60",
                )}
              >
                <input
                  type="radio"
                  name={`calibration-${interaction.interaction_id}`}
                  value={option.option_id}
                  checked={checked}
                  disabled={submitting || expired}
                  onChange={() => choose(option.option_id)}
                  className="h-4 w-4 shrink-0 accent-console-cyan"
                />
                <span className="min-w-0">
                  <span className="block font-medium text-console-text">
                    {withoutPercentages(option.label)}
                  </span>
                  {option.description ? (
                    <span className="mt-0.5 block text-xs font-normal text-console-muted">
                      {withoutPercentages(option.description)}
                    </span>
                  ) : null}
                </span>
              </label>
            );
          }
          return (
            <button
              key={option.option_id}
              type="button"
              aria-pressed={multiple ? checked : undefined}
              disabled={submitting || expired}
              onClick={() => choose(option.option_id)}
              className={cn(
                "flex min-h-11 min-w-32 flex-1 basis-34 items-center justify-center gap-2 rounded-xl border px-3 py-2 text-sm font-medium shadow-[0_1px_7px_rgba(23,32,46,0.045)] transition-[border-color,background-color,box-shadow,transform] duration-150 motion-reduce:transition-none focus:outline-hidden focus-visible:ring-2 focus-visible:ring-console-cyan/60 disabled:cursor-not-allowed disabled:opacity-60",
                option.tone === "danger" || option.destructive
                  ? "border-rose-200 bg-white text-rose-700 hover:border-rose-300 hover:bg-rose-50/70"
                  : option.tone === "primary"
                  ? "border-console-cyan bg-console-cyan text-white hover:brightness-95"
                  : checked
                  ? "border-console-cyan/60 bg-blue-50 text-console-text"
                  : "border-console-line/90 bg-white text-console-text hover:border-console-cyan/30 hover:bg-slate-50/80",
              )}
            >
              {multiple && checked ? <Check className="h-4 w-4" aria-hidden="true" /> : null}
              <span>
                <span className="block">{withoutPercentages(option.label)}</span>
                {option.description ? (
                  <span className="mt-0.5 block text-xs font-normal opacity-80">
                    {withoutPercentages(option.description)}
                  </span>
                ) : null}
              </span>
            </button>
          );
        })}
      </div>

      {calibrationChoice ? (
        <button
          type="button"
          disabled={selected.length !== 1 || submitting || expired}
          onClick={() => onSubmit(selected)}
          className="mt-2 min-h-11 w-full rounded-xl bg-console-text px-3 py-2 text-sm font-medium text-white shadow-[0_1px_7px_rgba(23,32,46,0.06)] transition motion-reduce:transition-none hover:bg-slate-800 focus:outline-hidden focus-visible:ring-2 focus-visible:ring-console-cyan/60 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "正在提交…" : "确认所选标定并继续"}
        </button>
      ) : null}

      {multiple ? (
        <button
          type="button"
          disabled={selected.length === 0 || submitting || expired}
          onClick={() => onSubmit(selected)}
          className="mt-2 min-h-11 w-full rounded-xl bg-console-text px-3 py-2 text-sm font-medium text-white shadow-[0_1px_7px_rgba(23,32,46,0.06)] transition motion-reduce:transition-none hover:bg-slate-800 focus:outline-hidden focus-visible:ring-2 focus-visible:ring-console-cyan/60 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "正在提交…" : "提交选择"}
        </button>
      ) : null}

      {expired ? (
        <p className="mt-2 rounded-xl border border-rose-200 bg-white px-3 py-2 text-xs text-rose-700 shadow-[0_2px_9px_rgba(23,32,46,0.05)]">
          此选择已过期，请等待状态刷新。
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-2 rounded-xl border border-rose-200 bg-white px-3 py-2 text-xs text-rose-700 shadow-[0_2px_9px_rgba(23,32,46,0.05)]">
          {error}
        </p>
      ) : null}
    </section>
  );
}
