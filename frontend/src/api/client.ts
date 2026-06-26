import type { AgentEvent, SessionDetail, SessionRecord } from "./types";

function sessionPath(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}`;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    throw new Error(await response.text());
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

export function openSessionEvents(sessionId: string, onEvent: (event: AgentEvent) => void): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}${sessionPath(sessionId)}/events`);
  socket.addEventListener("message", (message) => onEvent(JSON.parse(message.data) as AgentEvent));
  return socket;
}
