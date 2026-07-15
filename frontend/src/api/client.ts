import type {
  HumanDecisionPayload,
  HumanDecisionRecoveryRequest,
  HumanDecisionRecoveryResponse,
  NavigationDatasetSummary,
  NavigationDateSummary,
  NavigationSyncImageListing,
  PublicEventEnvelope,
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

export class ApiResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null = null,
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}

async function responseError(response: Response): Promise<ApiResponseError> {
  const fallback = `${response.status} ${response.statusText}`;
  const text = await response.text();
  if (!text) {
    return new ApiResponseError(fallback, response.status);
  }
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    const detail = parsed?.detail;
    if (typeof detail === "string") {
      return new ApiResponseError(detail, response.status);
    }
    if (detail && typeof detail === "object") {
      const value = detail as { code?: unknown; message?: unknown };
      return new ApiResponseError(
        typeof value.message === "string" ? value.message : JSON.stringify(detail),
        response.status,
        typeof value.code === "string" ? value.code : null,
      );
    }
  } catch {
    return new ApiResponseError(text, response.status);
  }
  return new ApiResponseError(text, response.status);
}

export function isAmbiguousTurnSubmissionError(error: unknown): boolean {
  return (
    error instanceof TypeError ||
    error instanceof SyntaxError ||
    (error instanceof ApiResponseError &&
      (error.code === "turn_submission_pending" ||
        error.status === 408 ||
        error.status === 429 ||
        error.status >= 500))
  );
}

export type SubmitTurnResult = {
  turnId: string;
  replayed: boolean;
  terminal: boolean;
};

type SessionHistoryPage = {
  sessions: SessionRecord[];
  next_cursor?: string | null;
};

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

export async function createSession(message: string, creationId: string): Promise<SessionRecord> {
  const data = await requestJson<{ session: SessionRecord }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ message, creation_id: creationId }),
  });
  return data.session;
}

export async function listSessions(): Promise<SessionRecord[]> {
  const sessions: SessionRecord[] = [];
  const seenSessionIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const path: string = cursor === null
      ? "/api/sessions"
      : `/api/sessions?cursor=${encodeURIComponent(cursor)}`;
    const data: SessionHistoryPage = await requestJson<SessionHistoryPage>(path);
    if (!Array.isArray(data.sessions)) {
      throw new SyntaxError("Invalid session-history response");
    }
    for (const session of data.sessions) {
      if (!seenSessionIds.has(session.id)) {
        seenSessionIds.add(session.id);
        sessions.push(session);
      }
    }
    const nextCursor: string | null = data.next_cursor ?? null;
    if (nextCursor !== null && typeof nextCursor !== "string") {
      throw new SyntaxError("Invalid session-history cursor");
    }
    if (nextCursor !== null && seenCursors.has(nextCursor)) {
      throw new SyntaxError("Repeated session-history cursor");
    }
    if (nextCursor !== null) seenCursors.add(nextCursor);
    cursor = nextCursor;
  } while (cursor !== null);
  return sessions;
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const data = await requestJson<{ session: SessionDetail }>(sessionPath(sessionId));
  return data.session;
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(sessionPath(sessionId), {
    method: "DELETE",
    headers: { "content-type": "application/json" },
  });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  if (response.status !== 204) {
    throw new Error(`Expected 204 No Content, received ${response.status} ${response.statusText}`);
  }
}

export async function submitTurn(
  sessionId: string,
  message: string,
  messageId: string,
): Promise<SubmitTurnResult | string> {
  const data = await requestJson<unknown>(`${sessionPath(sessionId)}/turns`, {
    method: "POST",
    body: JSON.stringify({ message, message_id: messageId }),
  });
  if (
    !data ||
    typeof data !== "object" ||
    typeof (data as Record<string, unknown>).turn_id !== "string" ||
    !(data as Record<string, unknown>).turn_id ||
    typeof (data as Record<string, unknown>).replayed !== "boolean" ||
    typeof (data as Record<string, unknown>).terminal !== "boolean"
  ) {
    throw new SyntaxError("Malformed successful turn submission response");
  }
  const response = data as { turn_id: string; replayed: boolean; terminal: boolean };
  return {
    turnId: response.turn_id,
    replayed: response.replayed,
    terminal: response.terminal,
  };
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

export async function recoverHumanDecision(
  sessionId: string,
  payload: HumanDecisionRecoveryRequest,
): Promise<HumanDecisionRecoveryResponse> {
  return requestJson<HumanDecisionRecoveryResponse>(
    `${sessionPath(sessionId)}/human-decisions/recovery`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function* streamSessionEvents(
  sessionId: string,
  afterSequence: number,
  signal: AbortSignal,
): AsyncGenerator<PublicEventEnvelope> {
  try {
    const response = await fetch(
      `${sessionPath(sessionId)}/stream?after_sequence=${afterSequence}`,
      { signal, headers: { Accept: "text/event-stream" } },
    );
    if (!response.ok || !response.body) {
      throw new Error(await responseErrorMessage(response));
    }

    yield* parseSse(response.body, signal);
  } catch (error) {
    if (!signal.aborted && !isAbortError(error)) {
      throw error;
    }
  }
}

async function* parseSse(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): AsyncGenerator<PublicEventEnvelope> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal.addEventListener("abort", cancel, { once: true });

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const { frames, remainder } = splitSseFrames(buffer);
      buffer = remainder;
      for (const frame of frames) {
        const envelope = publicEnvelopeFromFrame(frame);
        if (envelope) yield envelope;
      }
    }

    if (!signal.aborted && buffer.trim()) {
      const envelope = publicEnvelopeFromFrame(buffer);
      if (envelope) yield envelope;
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

function splitSseFrames(buffer: string): { frames: string[]; remainder: string } {
  const frames: string[] = [];
  let start = 0;
  const separator = /\r?\n\r?\n/g;
  for (let match = separator.exec(buffer); match; match = separator.exec(buffer)) {
    frames.push(buffer.slice(start, match.index));
    start = match.index + match[0].length;
  }
  return { frames, remainder: buffer.slice(start) };
}

function publicEnvelopeFromFrame(frame: string): PublicEventEnvelope | null {
  let eventName = "message";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    if (field === "data") data.push(value);
  }
  if ((eventName !== "message" && eventName !== "public_event") || data.length === 0) {
    return null;
  }

  try {
    const value = JSON.parse(data.join("\n")) as unknown;
    return isPublicEventEnvelope(value) ? value : null;
  } catch {
    return null;
  }
}

function isPublicEventEnvelope(value: unknown): value is PublicEventEnvelope {
  if (!value || typeof value !== "object") return false;
  const envelope = value as Record<string, unknown>;
  if (!envelope.event || typeof envelope.event !== "object") return false;
  const event = envelope.event as Record<string, unknown>;
  return (
    typeof envelope.id === "string" &&
    typeof envelope.session_id === "string" &&
    Number.isSafeInteger(envelope.sequence) &&
    (envelope.sequence as number) > 0 &&
    typeof envelope.dedupe_key === "string" &&
    typeof envelope.created_at === "string" &&
    typeof event.id === "string" &&
    typeof event.created_at === "string" &&
    typeof event.type === "string"
  );
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
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
