import { useRef, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "../../lib/utils";

export type ConsoleListTabItem<T extends string> = {
  value: T;
  label: ReactNode;
  disabled?: boolean;
};

type ConsoleListTabsProps<T extends string> = {
  value: T;
  items: readonly ConsoleListTabItem<T>[];
  onValueChange: (value: T) => void;
  className?: string;
  listClassName?: string;
  disabled?: boolean;
  panelId?: string;
  idPrefix?: string;
  "aria-label": string;
};

/**
 * 列表内容之间的轻量切换器。
 *
 * 与页面级的 ConsoleSlidingTabs 不同，本组件只改变当前列表视图，使用从中心展开的
 * 下划线表达选中状态。组件自行处理方向键、Home/End 与焦点移动，便于各业务列表
 * 复用相同的键盘和视觉反馈。
 */
export function ConsoleListTabs<T extends string>({
  value,
  items,
  onValueChange,
  className,
  listClassName,
  disabled = false,
  panelId,
  idPrefix = "console-list-tab",
  "aria-label": ariaLabel,
}: ConsoleListTabsProps<T>) {
  const listRef = useRef<HTMLDivElement>(null);

  function selectAndFocus(index: number) {
    const item = items[index];
    if (!item || disabled || item.disabled) return;

    onValueChange(item.value);
    window.requestAnimationFrame(() => {
      listRef.current
        ?.querySelectorAll<HTMLButtonElement>("[role='tab']")
        .item(index)
        ?.focus();
    });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) {
    let direction = 0;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") direction = 1;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") direction = -1;

    if (event.key === "Home") {
      event.preventDefault();
      const firstEnabled = items.findIndex((item) => !item.disabled);
      if (firstEnabled >= 0) selectAndFocus(firstEnabled);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      for (let index = items.length - 1; index >= 0; index -= 1) {
        if (!items[index]?.disabled) {
          selectAndFocus(index);
          break;
        }
      }
      return;
    }
    if (direction === 0) return;

    event.preventDefault();
    for (let offset = 1; offset <= items.length; offset += 1) {
      const nextIndex = (currentIndex + direction * offset + items.length) % items.length;
      if (!items[nextIndex]?.disabled) {
        selectAndFocus(nextIndex);
        return;
      }
    }
  }

  return (
    <div className={cn("console-soft-scrollbar overflow-x-auto", className)}>
      <div
        ref={listRef}
        role="tablist"
        aria-label={ariaLabel}
        className={cn("flex min-w-max items-center gap-6", listClassName)}
      >
        {items.map((item, index) => {
          const selected = item.value === value;
          const itemDisabled = disabled || item.disabled;
          return (
            <button
              key={item.value}
              type="button"
              role="tab"
              id={`${idPrefix}-${item.value}`}
              data-console-list-tab={item.value}
              aria-selected={selected}
              aria-controls={panelId}
              disabled={itemDisabled}
              tabIndex={selected ? 0 : -1}
              onClick={() => onValueChange(item.value)}
              onKeyDown={(event) => handleKeyDown(event, index)}
              className={cn(
                "relative inline-flex min-h-11 items-center justify-center px-1 text-sm font-medium outline-none transition-[color,opacity,transform] duration-150 after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:origin-center after:bg-[#3157cf] after:transition-transform after:duration-[180ms] hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 active:translate-y-px disabled:cursor-not-allowed disabled:opacity-45 motion-reduce:transition-none motion-reduce:after:transition-none",
                selected
                  ? "text-[#3157cf] after:scale-x-100"
                  : "text-slate-500 after:scale-x-0",
              )}
            >
              {item.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
