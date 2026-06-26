import { useStore } from "zustand";

import { datapilotStore } from "../../store/datapilotStore";
import { DraftNewSessionView } from "./DraftNewSessionView";
import { SessionHeader } from "./SessionHeader";

export function DataPilotWindow() {
  const open = useStore(datapilotStore, (state) => state.open);
  const mode = useStore(datapilotStore, (state) => state.mode);
  const running = useStore(datapilotStore, (state) => state.run.running);

  if (!open) {
    return null;
  }

  return (
    <section
      role="dialog"
      aria-label="DataPilot"
      className="fixed bottom-3 right-3 z-40 flex h-[min(640px,calc(100vh-1.5rem))] w-[calc(100vw-1.5rem)] max-w-[460px] flex-col overflow-hidden rounded border border-console-line bg-console-panel shadow-[0_22px_70px_rgba(0,0,0,0.42)] sm:bottom-5 sm:right-5 sm:h-[min(680px,calc(100vh-2.5rem))] sm:w-[min(460px,calc(100vw-2.5rem))]"
    >
      <SessionHeader />
      {mode === "draft_new_session" ? (
        <DraftNewSessionView running={running} onSubmit={() => undefined} onInterrupt={() => undefined} />
      ) : (
        <div className="flex-1 bg-console-bg" aria-hidden="true" />
      )}
    </section>
  );
}
