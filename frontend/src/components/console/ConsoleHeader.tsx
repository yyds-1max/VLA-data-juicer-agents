import { Bell, Search } from "lucide-react";

type ConsoleHeaderProps = {
  title: string;
  subtitle: string;
  onPlaceholderAction: (message?: string) => void;
};

export function ConsoleHeader({ title, subtitle, onPlaceholderAction }: ConsoleHeaderProps) {
  return (
    <header className="sticky top-[124px] z-30 isolate border-b border-[#e8ebf1] bg-white/92 px-4 py-4 backdrop-blur-md md:top-0 md:px-7 md:py-5">
      <div className="mx-auto flex max-w-[1680px] flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <h1 className="truncate text-[22px] font-semibold tracking-[-0.02em] text-[#1c2433] md:text-2xl">{title}</h1>
          <p className="mt-0.5 truncate text-sm text-[#626b7d]">{subtitle}</p>
        </div>

        <div className="flex min-w-0 items-center gap-2.5 sm:gap-3">
          <button
            type="button"
            aria-label="搜索数据、模型、任务（暂未接入）"
            className="group flex h-10 min-w-0 flex-1 items-center gap-2.5 rounded-xl border border-[#dfe3eb] bg-white px-3 text-left text-sm text-[#626b7d] shadow-[0_2px_7px_rgba(30,41,59,0.04)] transition-[color,background-color,border-color,box-shadow] duration-200 ease-out hover:border-[#cdd5e3] hover:bg-[#fafbfc] hover:text-[#59657a] active:bg-[#f3f5f8] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none sm:w-72 sm:flex-none"
            onClick={() => onPlaceholderAction("搜索功能暂未接入")}
          >
            <Search className="h-4 w-4 shrink-0 text-[#626b7d] transition-colors duration-200 group-hover:text-[#3156c8] motion-reduce:transition-none" aria-hidden="true" />
            <span className="truncate">搜索数据、模型、任务...</span>
          </button>

          <button
            type="button"
            aria-label="通知（暂未接入）"
            className="relative flex size-10 shrink-0 items-center justify-center rounded-xl border border-[#dfe3eb] bg-white text-[#657087] shadow-[0_2px_7px_rgba(30,41,59,0.04)] transition-[color,background-color,border-color,box-shadow] duration-200 ease-out hover:border-[#cdd5e3] hover:bg-[#fafbfc] hover:text-[#3156c8] active:bg-[#f3f5f8] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none"
            onClick={() => onPlaceholderAction("通知功能暂未接入")}
          >
            <Bell aria-hidden="true" className="size-4" />
            <span aria-hidden="true" className="absolute right-2 top-2 size-1.5 rounded-full bg-[#ef4458] ring-2 ring-white" />
          </button>

          <div
            className="hidden h-10 shrink-0 items-center gap-2 rounded-xl border border-[#dfe3eb] bg-white px-3 text-sm text-[#657087] shadow-[0_2px_7px_rgba(30,41,59,0.04)] sm:flex"
            role="status"
            aria-label="系统状态：在线"
          >
            <span className="h-2 w-2 rounded-full bg-[#08a66c]" aria-hidden="true" />
            <span>系统在线</span>
          </div>
        </div>
      </div>
    </header>
  );
}
