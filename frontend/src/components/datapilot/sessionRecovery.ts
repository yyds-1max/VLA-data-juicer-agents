import type { SessionMode } from "../../store/datapilotStore";

const RECOVERY_KEY = "datapilot.session-view.v1";

export type DataPilotSessionRecovery = {
  sessionId: string;
  mode: Extract<SessionMode, "active_session" | "history_session">;
};

export function readSessionRecovery(
  storage: Pick<Storage, "getItem"> = window.sessionStorage,
): DataPilotSessionRecovery | null {
  try {
    const encoded = storage.getItem(RECOVERY_KEY);
    if (!encoded) return null;
    const value = JSON.parse(encoded) as Partial<DataPilotSessionRecovery>;
    if (
      typeof value.sessionId !== "string"
      || !value.sessionId.trim()
      || (value.mode !== "active_session" && value.mode !== "history_session")
    ) {
      return null;
    }
    return {
      sessionId: value.sessionId,
      mode: value.mode,
    };
  } catch {
    return null;
  }
}

export function writeSessionRecovery(
  value: DataPilotSessionRecovery,
  storage: Pick<Storage, "setItem"> = window.sessionStorage,
): void {
  storage.setItem(RECOVERY_KEY, JSON.stringify(value));
}

export function clearSessionRecovery(
  storage: Pick<Storage, "removeItem"> = window.sessionStorage,
): void {
  storage.removeItem(RECOVERY_KEY);
}
