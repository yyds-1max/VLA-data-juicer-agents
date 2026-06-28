import { Bell, Search } from "lucide-react";

import { StatusTag } from "./StatusTag";

type ConsoleHeaderProps = {
  title: string;
};

export function ConsoleHeader({ title }: ConsoleHeaderProps) {
  return (
    <header className="sticky top-[6.75rem] z-10 border-b border-console-line bg-console-bg/90 px-4 py-4 backdrop-blur md:top-0 md:px-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="truncate text-xl font-semibold">{title}</h1>
            <StatusTag tone="neutral">v2.4.1</StatusTag>
          </div>
        </div>

        <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center">
          <label className="relative block min-w-0 sm:w-72">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" aria-hidden="true" />
            <input
              type="search"
              placeholder="搜索数据、模型、任务..."
              className="h-10 w-full rounded border border-console-line bg-console-panel2 py-2 pl-9 pr-3 text-sm text-console-text placeholder:text-console-muted focus:border-console-cyan/60 focus:outline-none focus:ring-2 focus:ring-console-cyan/30"
            />
          </label>

          <button
            type="button"
            aria-label="Notifications"
            className="relative flex h-10 w-10 shrink-0 items-center justify-center rounded border border-console-line bg-console-panel2 text-console-muted transition hover:border-console-cyan/40 hover:text-console-cyan focus:outline-none focus:ring-2 focus:ring-console-cyan"
          >
            <Bell className="h-4 w-4" aria-hidden="true" />
            <span className="absolute right-2.5 top-2.5 h-2 w-2 rounded-full bg-rose-400" aria-hidden="true" />
          </button>

          <div className="flex h-10 shrink-0 items-center gap-2 rounded border border-console-line bg-console-panel2 px-3 text-sm text-console-muted">
            <span className="h-2 w-2 rounded-full bg-console-cyan shadow-[0_0_12px_#15d1d8]" aria-hidden="true" />
            系统在线
          </div>
        </div>
      </div>
    </header>
  );
}
