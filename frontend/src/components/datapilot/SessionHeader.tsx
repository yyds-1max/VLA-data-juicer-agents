import { History, PanelRightClose, Plus } from "lucide-react";
import { useStore } from "zustand";

import { datapilotStore } from "../../store/datapilotStore";

type SessionHeaderProps = {
  onHistory: () => void;
  onNewSession: () => void;
};

export function SessionHeader({ onHistory, onNewSession }: SessionHeaderProps) {
  const mode = useStore(datapilotStore, (state) => state.mode);
  const setOpen = useStore(datapilotStore, (state) => state.setOpen);
  const subtitle =
    mode === "draft_new_session" ? "新任务草稿" : mode === "history_session" ? "历史会话" : "活动会话";

  return (
    <header className="flex items-center justify-between gap-3 border-b border-console-line px-3 py-3 sm:px-4">
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-console-text">DataPilot</div>
        <div className="truncate text-xs text-console-muted">{subtitle}</div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <button
          type="button"
          aria-label="History"
          onClick={onHistory}
          className="flex h-9 w-9 items-center justify-center rounded text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <History className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          aria-label="New session"
          onClick={onNewSession}
          className="flex h-9 w-9 items-center justify-center rounded text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          aria-label="Close DataPilot"
          onClick={() => setOpen(false)}
          className="flex h-9 w-9 items-center justify-center rounded text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <PanelRightClose className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </header>
  );
}
