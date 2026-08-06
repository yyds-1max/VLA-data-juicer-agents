import type { ReactNode } from "react";

import { Tabs, TabsList, TabsTrigger } from "../ui/tabs";
import { cn } from "../../lib/utils";

export type ConsoleSlidingTabItem<T extends string> = {
  value: T;
  label: ReactNode;
  disabled?: boolean;
};

export type ConsoleSlidingTabsProps<T extends string> = {
  value: T;
  items: readonly ConsoleSlidingTabItem<T>[];
  onValueChange: (value: T) => void;
  children?: ReactNode;
  className?: string;
  listClassName?: string;
  disabled?: boolean;
  activationMode?: "automatic" | "manual";
  "aria-label": string;
};

export function ConsoleSlidingTabs<T extends string>({
  value,
  items,
  onValueChange,
  children,
  className,
  listClassName,
  disabled = false,
  activationMode = "automatic",
  "aria-label": ariaLabel,
}: ConsoleSlidingTabsProps<T>) {
  const activeIndex = items.findIndex((item) => item.value === value);
  const itemCount = Math.max(1, items.length);

  return (
    <Tabs
      value={value}
      activationMode={activationMode}
      className={cn("min-w-0 gap-2", className)}
      onValueChange={(nextValue) => onValueChange(nextValue as T)}
    >
      <TabsList
        aria-label={ariaLabel}
        className={cn(
          "relative isolate grid h-10 w-full min-w-0 overflow-hidden rounded-xl bg-[#EEF0F4] p-1 sm:w-fit sm:min-w-64",
          listClassName,
        )}
        style={{ gridTemplateColumns: `repeat(${itemCount}, minmax(0, 1fr))` }}
      >
        <span
          aria-hidden="true"
          data-slot="console-sliding-tabs-indicator"
          data-active-index={activeIndex}
          className={cn(
            "pointer-events-none absolute inset-y-1 left-1 rounded-lg bg-white shadow-[0_2px_8px_rgba(31,42,68,0.10)] ring-1 ring-black/[0.035] transition-[transform,opacity] duration-200 ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none",
            activeIndex < 0 && "opacity-0",
          )}
          style={{
            width: `calc((100% - 0.5rem) / ${itemCount})`,
            transform: `translate3d(${Math.max(0, activeIndex) * 100}%, 0, 0)`,
          }}
        />

        {items.map((item) => (
          <TabsTrigger
            key={item.value}
            value={item.value}
            disabled={disabled || item.disabled}
            className="z-10 h-8 min-w-0 rounded-lg bg-transparent px-3 text-xs font-medium text-[#626B7D] shadow-none transition-[color,opacity] duration-200 hover:text-[#30374A] focus-visible:border-transparent focus-visible:ring-2 focus-visible:ring-[#3156C8]/35 data-[state=active]:bg-transparent data-[state=active]:text-[#202431] data-[state=active]:shadow-none disabled:cursor-not-allowed disabled:opacity-45 motion-reduce:transition-none"
          >
            <span className="truncate">{item.label}</span>
          </TabsTrigger>
        ))}
      </TabsList>
      {children}
    </Tabs>
  );
}
