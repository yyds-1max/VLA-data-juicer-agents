import type {
  AgentEvent,
  InteractionResponsePayload,
  InteractionResponseResult,
  NavigationDatasetSummary,
  NavigationDateSummary,
  NavigationSyncImageListing,
  SessionDetail,
  SessionEntrypoint,
  SessionRecord,
  SessionRequestContext,
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

export class ApiResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}

async function responseError(response: Response): Promise<ApiResponseError> {
  const fallback = `${response.status} ${response.statusText}`;
  const text = await response.text();
  if (!text) return new ApiResponseError(fallback, response.status, null);
  try {
    const parsed = JSON.parse(text) as unknown;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      const message = typeof detail === "string" ? detail : JSON.stringify(detail);
      return new ApiResponseError(message, response.status, parsed);
    }
    return new ApiResponseError(text, response.status, parsed);
  } catch {
    return new ApiResponseError(text, response.status, text);
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as T;
}

export async function createSession(
  message: string,
  entrypoint?: SessionEntrypoint,
  requestContext?: SessionRequestContext,
): Promise<SessionRecord> {
  const data = await requestJson<{ session: SessionRecord }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      message,
      ...(entrypoint ? { entrypoint } : {}),
      ...(requestContext ? { request_context: requestContext } : {}),
    }),
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

export async function submitTurn(
  sessionId: string,
  message: string,
  invocationId?: string,
): Promise<string> {
  const data = await requestJson<{ turn_id: string }>(`${sessionPath(sessionId)}/turns`, {
    method: "POST",
    body: JSON.stringify({
      message,
      ...(invocationId ? { invocation_id: invocationId } : {}),
    }),
  });
  return data.turn_id;
}

export async function interruptTurn(sessionId: string): Promise<boolean> {
  const data = await requestJson<{ interrupted: boolean }>(`${sessionPath(sessionId)}/interrupt`, {
    method: "POST",
  });
  return data.interrupted;
}

export async function submitInteractionResponse(
  sessionId: string,
  interactionId: string,
  payload: InteractionResponsePayload,
): Promise<InteractionResponseResult> {
  return requestJson<InteractionResponseResult>(
    `${sessionPath(sessionId)}/interactions/${encodeURIComponent(interactionId)}/responses`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function openSessionEvents(sessionId: string, onEvent: (event: AgentEvent) => void): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}${sessionPath(sessionId)}/events`);
  socket.addEventListener("message", (message) => {
    try {
      onEvent(JSON.parse(message.data) as AgentEvent);
    } catch (error) {
      console.error("Failed to parse DataPilot event", error);
    }
  });
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
