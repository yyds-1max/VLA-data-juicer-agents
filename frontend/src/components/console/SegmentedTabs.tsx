import { useEffect, useState } from "react";

import type { TabItem } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type SegmentedTabsProps<T extends string> = {
  value: T;
  tabs: Array<TabItem<T>>;
  onChange: (value: T) => void;
  className?: string;
  "aria-label"?: string;
};

export function SegmentedTabs<T extends string>({ value, tabs, onChange, className, "aria-label": ariaLabel = "Console tabs" }: SegmentedTabsProps<T>) {
  const [selected, setSelected] = useState(value);

  useEffect(() => {
    setSelected(value);
  }, [value]);

  return (
    <div className={cn("inline-flex rounded border border-console-line bg-console-panel2 p-1", className)} role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab) => {
        const active = tab.id === selected;

        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active}
            className={cn(
              "h-8 rounded px-3 text-xs font-medium text-console-muted transition focus:outline-none focus:ring-2 focus:ring-console-cyan",
              active && "bg-console-cyan/10 text-console-cyan shadow-[inset_0_0_0_1px_rgba(21,209,216,0.35)]",
              !active && "hover:bg-console-panel hover:text-console-text",
            )}
            onClick={() => {
              setSelected(tab.id);
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
