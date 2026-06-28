import { Bot } from "lucide-react";
import { useStore } from "zustand";

import { datapilotStore } from "../../store/datapilotStore";

export function DataPilotButton() {
  const open = useStore(datapilotStore, (state) => state.open);
  const setOpen = useStore(datapilotStore, (state) => state.setOpen);

  if (open) {
    return null;
  }

  return (
    <button
      type="button"
      aria-label="Open DataPilot"
      onClick={() => setOpen(true)}
      className="fixed bottom-5 right-5 z-[80] flex h-14 w-14 items-center justify-center rounded-full border border-console-cyan/45 bg-console-cyan text-console-bg shadow-[0_18px_38px_rgba(21,209,216,0.28)] transition hover:bg-cyan-200 focus:outline-none focus:ring-2 focus:ring-console-cyan focus:ring-offset-2 focus:ring-offset-console-bg"
    >
      <Bot className="h-6 w-6" aria-hidden="true" />
    </button>
  );
}
