import { Trash2, X } from "lucide-react";

import type { SessionRecord } from "../../api/types";

type SessionHistoryPanelProps = {
  sessions: SessionRecord[];
  onSelect: (session: SessionRecord) => void;
  onDelete: (session: SessionRecord) => void;
  onClose: () => void;
};

export function SessionHistoryPanel({ sessions, onSelect, onDelete, onClose }: SessionHistoryPanelProps) {
  return (
    <aside className="border-b border-console-line bg-console-panel2 px-3 py-3 sm:px-4">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="text-sm font-medium text-console-text">历史会话</div>
        <button
          type="button"
          aria-label="Close history"
          onClick={onClose}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-console-muted transition hover:bg-console-panel hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
      <div className="max-h-48 space-y-2 overflow-y-auto">
        {sessions.length > 0 ? (
          sessions.map((session) => (
            <div
              key={session.id}
              className="flex w-full items-center gap-2 rounded-lg border border-console-line bg-console-panel p-1 shadow-sm transition hover:border-console-cyan/50"
            >
              <button
                type="button"
                aria-label={`Open session ${session.title}`}
                onClick={() => onSelect(session)}
                className="min-w-0 flex-1 rounded-md px-2 py-1 text-left focus:outline-none focus:ring-2 focus:ring-console-cyan"
              >
                <div className="truncate text-sm font-medium text-console-text">{session.title}</div>
                <time className="mt-1 block text-xs text-console-muted" dateTime={session.updated_at}>
                  {formatUpdatedAt(session.updated_at)}
                </time>
              </button>
              <button
                type="button"
                aria-label={`Delete session ${session.title}`}
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete(session);
                }}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-console-muted transition hover:bg-rose-50 hover:text-rose-600 focus:outline-none focus:ring-2 focus:ring-console-cyan"
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          ))
        ) : (
          <div className="rounded-lg border border-console-line bg-console-panel px-3 py-3 text-sm text-console-muted">
            暂无历史会话。
          </div>
        )}
      </div>
    </aside>
  );
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const day = String(date.getUTCDate()).padStart(2, "0");
  const hours = String(date.getUTCHours()).padStart(2, "0");
  const minutes = String(date.getUTCMinutes()).padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}`;
}
