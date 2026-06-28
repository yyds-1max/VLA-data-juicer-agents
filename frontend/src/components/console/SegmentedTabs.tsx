import type { TabItem } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type SegmentedTabsProps<T extends string> = {
  value: T;
  tabs: Array<TabItem<T>>;
  onChange: (value: T) => void;
  idPrefix?: string;
  className?: string;
  "aria-label"?: string;
};

export function SegmentedTabs<T extends string>({ value, tabs, onChange, idPrefix, className, "aria-label": ariaLabel = "Console tabs" }: SegmentedTabsProps<T>) {
  return (
    <div className={cn("inline-flex rounded border border-console-line bg-console-panel2 p-1", className)} role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab) => {
        const active = tab.id === value;
        const tabId = idPrefix ? `${idPrefix}-tab-${tab.id}` : undefined;
        const panelId = idPrefix ? `${idPrefix}-panel-${tab.id}` : undefined;

        return (
          <button
            key={tab.id}
            id={tabId}
            type="button"
            role="tab"
            aria-selected={active}
            aria-controls={panelId}
            className={cn(
              "h-8 rounded px-3 text-xs font-medium text-console-muted transition focus:outline-none focus:ring-2 focus:ring-console-cyan",
              active && "bg-console-cyan/10 text-console-cyan shadow-[inset_0_0_0_1px_rgba(21,209,216,0.35)]",
              !active && "hover:bg-console-panel hover:text-console-text",
            )}
            onClick={() => {
              onChange(tab.id);
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
