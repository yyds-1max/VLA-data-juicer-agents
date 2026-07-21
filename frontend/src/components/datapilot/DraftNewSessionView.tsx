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
      className="flex min-h-0 flex-1 flex-col justify-start overflow-y-auto overscroll-contain bg-console-panel2/45 px-4 pb-4 pt-20 sm:px-5 sm:pb-5 sm:pt-24"
    >
      <div className="w-full">
        <div className="text-center">
          <h2 className="text-[1.7rem] font-medium leading-tight tracking-[-0.02em] text-console-text sm:text-[1.9rem]">
            开始一个任务
          </h2>
          <p className="mt-2 text-sm leading-6 text-console-muted">
            描述你的目标，DataPilot会帮你完成。
          </p>
        </div>
        <div className="mt-6">
          <Composer
            placeholder="我们要做什么？"
            running={running}
            onSubmit={onSubmit}
            onInterrupt={onInterrupt}
          />
        </div>
      </div>
    </div>
  );
}
