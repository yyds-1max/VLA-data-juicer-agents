import type {
  AgentEvent,
  HumanDecisionPayload,
  NavigationDatasetSummary,
  NavigationDateSummary,
  NavigationSyncImageListing,
  SessionDetail,
  SessionRecord,
} from "./types";

function sessionPath(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}`;
}

function navigationDatasetPath(date: string): string {
  return `/api/navigation/datasets/${encodeURIComponent(date)}`;
}

function navigationClipPath(date: string, clip: string): string {
  return `${navigationDatasetPath(date)}/clips/${encodeURIComponent(clip)}`;
}

async function responseErrorMessage(response: Response): Promise<string> {
  const fallback = `${response.status} ${response.statusText}`;
  const text = await response.text();
  if (!text) {
    return fallback;
  }

  try {
    const parsed = JSON.parse(text) as unknown;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    return text;
  }

  return text;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return (await response.json()) as T;
}

export async function createSession(message: string): Promise<SessionRecord> {
  const data = await requestJson<{ session: SessionRecord }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ message }),
  });
  return data.session;
}

export async function listSessions(): Promise<SessionRecord[]> {
  const data = await requestJson<{ sessions: SessionRecord[] }>("/api/sessions");
  return data.sessions;
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const data = await requestJson<{ session: SessionDetail }>(sessionPath(sessionId));
  return data.session;
}

export async function submitTurn(sessionId: string, message: string): Promise<string> {
  const data = await requestJson<{ turn_id: string }>(`${sessionPath(sessionId)}/turns`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
  return data.turn_id;
}

export async function interruptTurn(sessionId: string): Promise<boolean> {
  const data = await requestJson<{ interrupted: boolean }>(`${sessionPath(sessionId)}/interrupt`, {
    method: "POST",
  });
  return data.interrupted;
}

export async function submitHumanDecision(
  sessionId: string,
  payload: HumanDecisionPayload,
): Promise<boolean> {
  const data = await requestJson<{ accepted: boolean }>(`${sessionPath(sessionId)}/human-decisions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return data.accepted;
}

export function openSessionEvents(sessionId: string, onEvent: (event: AgentEvent) => void): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}${sessionPath(sessionId)}/events`);
  socket.addEventListener("message", (message) => onEvent(JSON.parse(message.data) as AgentEvent));
  return socket;
}

export async function getNavigationDatasetSummary(): Promise<NavigationDatasetSummary> {
  return requestJson<NavigationDatasetSummary>("/api/navigation/datasets/summary");
}

export async function getNavigationDatasetDate(date: string): Promise<NavigationDateSummary> {
  return requestJson<NavigationDateSummary>(navigationDatasetPath(date));
}

export async function getSyncImages(
  date: string,
  clip: string,
): Promise<NavigationSyncImageListing> {
  return requestJson<NavigationSyncImageListing>(`${navigationClipPath(date, clip)}/sync-images`);
}

export function getSyncImageUrl(
  date: string,
  clip: string,
  sequence: string,
  filename: string,
): string {
  return `${navigationClipPath(date, clip)}/sync-images/${encodeURIComponent(sequence)}/${encodeURIComponent(filename)}`;
}
