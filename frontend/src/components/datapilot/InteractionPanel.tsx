import { useMemo, useState } from "react";
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
  const expired = useMemo(() => {
    if (!interaction.expires_at) return false;
    const timestamp = Date.parse(interaction.expires_at);
    return !Number.isNaN(timestamp) && timestamp <= Date.now();
  }, [interaction.expires_at]);

  const choose = (optionId: string) => {
    if (submitting || expired) return;
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
      className="border-t border-console-line bg-console-panel p-3 sm:p-4"
      data-interaction-id={interaction.interaction_id}
      data-blocking={interaction.blocking ? "true" : "false"}
    >
      <div className="rounded-lg border border-amber-200 bg-amber-50/80 p-3 shadow-sm">
        <div className="flex items-start gap-2.5">
          {interaction.risk === "high" ? (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" aria-hidden="true" />
          ) : null}
          <div className="min-w-0 flex-1">
            <h2
              id={`interaction-title-${interaction.interaction_id}`}
              className="text-sm font-semibold text-console-text"
            >
              {withoutPercentages(interaction.title)}
            </h2>
            {interaction.summary ? (
              <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-console-muted">
                {withoutPercentages(interaction.summary)}
              </p>
            ) : null}
          </div>
        </div>

        <div className="mt-3 grid gap-2">
          {interaction.options.map((option) => {
            const checked = selected.includes(option.option_id);
            return (
              <button
                key={option.option_id}
                type="button"
                aria-pressed={multiple ? checked : undefined}
                disabled={submitting || expired}
                onClick={() => choose(option.option_id)}
                className={cn(
                  "flex min-h-10 w-full items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium transition motion-reduce:transition-none focus:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/60 disabled:cursor-not-allowed disabled:opacity-60",
                  option.tone === "danger" || option.destructive
                    ? "border-rose-300 bg-white text-rose-700 hover:bg-rose-50"
                    : option.tone === "primary"
                    ? "border-console-cyan bg-console-cyan text-white hover:brightness-95"
                    : checked
                    ? "border-console-cyan bg-cyan-50 text-console-text"
                    : "border-console-line bg-white text-console-text hover:bg-slate-50",
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

        {multiple ? (
          <button
            type="button"
            disabled={selected.length === 0 || submitting || expired}
            onClick={() => onSubmit(selected)}
            className="mt-3 min-h-10 w-full rounded-md bg-console-text px-3 py-2 text-sm font-medium text-white transition motion-reduce:transition-none hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/60 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "正在提交…" : "提交选择"}
          </button>
        ) : null}

        {expired ? <p className="mt-2 text-xs text-rose-700">此选择已过期，请等待状态刷新。</p> : null}
        {error ? <p role="alert" className="mt-2 text-xs text-rose-700">{error}</p> : null}
      </div>
    </section>
  );
}
