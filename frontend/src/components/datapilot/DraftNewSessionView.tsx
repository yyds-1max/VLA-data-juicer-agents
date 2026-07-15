import { Composer } from "./Composer";

type DraftNewSessionViewProps = {
  running?: boolean;
  onSubmit: (message: string) => void;
  onInterrupt?: () => void;
};

export function DraftNewSessionView({ running = false, onSubmit, onInterrupt }: DraftNewSessionViewProps) {
  return (
    <div
      data-datapilot-scroll-area="true"
      className="flex min-h-0 flex-1 flex-col justify-end gap-6 overflow-y-auto overscroll-contain bg-console-panel2/45 p-4 sm:p-5"
    >
      <div className="space-y-3 rounded-lg border border-console-line bg-console-panel p-4 shadow-sm">
        <h2 className="text-2xl font-semibold tracking-normal text-console-text sm:text-3xl">开始一个任务</h2>
        <p className="max-w-[34rem] text-sm leading-6 text-console-muted">
          描述你的目标，DataPilot会帮你完成。
        </p>
      </div>
      <Composer
        placeholder="我们要做什么？"
        running={running}
        onSubmit={onSubmit}
        onInterrupt={onInterrupt}
      />
    </div>
  );
}
