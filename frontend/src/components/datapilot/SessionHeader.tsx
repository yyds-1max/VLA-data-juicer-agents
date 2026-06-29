import type { PointerEvent } from "react";
import { GripHorizontal, History, PanelRightClose, Plus } from "lucide-react";
import { useStore } from "zustand";

import { datapilotStore } from "../../store/datapilotStore";

type SessionHeaderProps = {
  onHistory: () => void;
  onNewSession: () => void;
  onDragStart?: (event: PointerEvent<HTMLElement>) => void;
};

export function SessionHeader({ onHistory, onNewSession, onDragStart }: SessionHeaderProps) {
  const setOpen = useStore(datapilotStore, (state) => state.setOpen);

  return (
    <header
      aria-label="Drag DataPilot window"
      className="flex cursor-grab touch-none items-center justify-between gap-3 border-b border-console-line bg-console-panel px-3 py-3 active:cursor-grabbing sm:px-4"
      onPointerDown={onDragStart}
    >
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-console-text text-white">
          <GripHorizontal className="h-4 w-4" aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-console-text">DataPilot</div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <button
          type="button"
          aria-label="History"
          onClick={onHistory}
          onPointerDown={(event) => event.stopPropagation()}
          className="flex h-9 w-9 items-center justify-center rounded-lg text-console-muted transition-[background-color,color,transform] duration-150 hover:bg-console-panel2 hover:text-console-text active:scale-95 focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <History className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          aria-label="New session"
          onClick={onNewSession}
          onPointerDown={(event) => event.stopPropagation()}
          className="flex h-9 w-9 items-center justify-center rounded-lg text-console-muted transition-[background-color,color,transform] duration-150 hover:bg-console-panel2 hover:text-console-text active:scale-95 focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          aria-label="Close DataPilot"
          onClick={() => setOpen(false)}
          onPointerDown={(event) => event.stopPropagation()}
          className="flex h-9 w-9 items-center justify-center rounded-lg text-console-muted transition-[background-color,color,transform] duration-150 hover:bg-console-panel2 hover:text-console-text active:scale-95 focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <PanelRightClose className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </header>
  );
}
