import type { ChatMessageRecord } from "../../api/types";

export type AmbiguousTurnRetry = {
  content: string;
  userMessage: ChatMessageRecord & { role: "user" };
  draftRevision: number;
};

export type SessionCreationRetry = {
  content: string;
  creationId: string;
};

type TurnOutboxEnvelope = {
  version: 1;
  sessionId: string;
  expiresAt: number;
  retry: AmbiguousTurnRetry;
};

export const TURN_OUTBOX_TTL_MS = 24 * 60 * 60 * 1_000;
const TURN_OUTBOX_PREFIX = "datapilot-turn-outbox:";
const SESSION_CREATION_OUTBOX_KEY = "datapilot-session-creation-outbox";
const SESSION_ID_PATTERN = /^session[-_][A-Za-z0-9-]{1,127}$/;
const MESSAGE_ID_PATTERN = /^local-[A-Za-z0-9-]+$/;
const CREATION_ID_PATTERN = /^local-create-[A-Za-z0-9-]+$/;

export function writeTurnOutbox(
  sessionId: string,
  retry: AmbiguousTurnRetry,
  now = Date.now(),
): void {
  if (!validRetry(sessionId, retry)) return;
  const envelope: TurnOutboxEnvelope = {
    version: 1,
    sessionId,
    expiresAt: now + TURN_OUTBOX_TTL_MS,
    retry,
  };
  try {
    window.sessionStorage.setItem(storageKey(sessionId), JSON.stringify(envelope));
  } catch {
    // Server-side idempotency still protects the live in-memory retry when
    // per-tab storage is unavailable (private mode/quota/security policy).
  }
}

export function readTurnOutbox(
  sessionId: string,
  now = Date.now(),
): AmbiguousTurnRetry | null {
  try {
    const raw = window.sessionStorage.getItem(storageKey(sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!validEnvelope(parsed, sessionId, now)) {
      rawRemoveTurnOutbox(sessionId);
      return null;
    }
    return parsed.retry;
  } catch {
    rawRemoveTurnOutbox(sessionId);
    return null;
  }
}

export function removeTurnOutbox(sessionId: string, expectedMessageId?: string): void {
  if (!expectedMessageId) {
    rawRemoveTurnOutbox(sessionId);
    return;
  }
  try {
    const raw = window.sessionStorage.getItem(storageKey(sessionId));
    if (!raw) return;
    const parsed = JSON.parse(raw) as { retry?: { userMessage?: { id?: unknown } } };
    if (parsed.retry?.userMessage?.id === expectedMessageId) {
      rawRemoveTurnOutbox(sessionId);
    }
  } catch {
    rawRemoveTurnOutbox(sessionId);
  }
}

export function writeSessionCreationOutbox(
  retry: SessionCreationRetry,
  now = Date.now(),
): void {
  if (!validSessionCreationRetry(retry)) return;
  try {
    window.sessionStorage.setItem(
      SESSION_CREATION_OUTBOX_KEY,
      JSON.stringify({
        version: 1,
        expiresAt: now + TURN_OUTBOX_TTL_MS,
        retry,
      }),
    );
  } catch {
    // The component retains the exact id in memory when storage is unavailable.
  }
}

export function readSessionCreationOutbox(
  now = Date.now(),
): SessionCreationRetry | null {
  try {
    const raw = window.sessionStorage.getItem(SESSION_CREATION_OUTBOX_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (
      !isRecord(parsed) ||
      parsed.version !== 1 ||
      typeof parsed.expiresAt !== "number" ||
      !Number.isFinite(parsed.expiresAt) ||
      parsed.expiresAt <= now ||
      parsed.expiresAt > now + TURN_OUTBOX_TTL_MS ||
      !validSessionCreationRetry(parsed.retry)
    ) {
      rawRemoveSessionCreationOutbox();
      return null;
    }
    return parsed.retry;
  } catch {
    rawRemoveSessionCreationOutbox();
    return null;
  }
}

export function removeSessionCreationOutbox(expectedCreationId?: string): void {
  if (!expectedCreationId) {
    rawRemoveSessionCreationOutbox();
    return;
  }
  try {
    const raw = window.sessionStorage.getItem(SESSION_CREATION_OUTBOX_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as { retry?: { creationId?: unknown } };
    if (parsed.retry?.creationId === expectedCreationId) {
      rawRemoveSessionCreationOutbox();
    }
  } catch {
    rawRemoveSessionCreationOutbox();
  }
}

function validEnvelope(
  value: unknown,
  expectedSessionId: string,
  now: number,
): value is TurnOutboxEnvelope {
  if (!isRecord(value)) return false;
  if (
    value.version !== 1 ||
    value.sessionId !== expectedSessionId ||
    !SESSION_ID_PATTERN.test(expectedSessionId) ||
    typeof value.expiresAt !== "number" ||
    !Number.isFinite(value.expiresAt) ||
    value.expiresAt <= now ||
    value.expiresAt > now + TURN_OUTBOX_TTL_MS
  ) {
    return false;
  }
  return validRetry(expectedSessionId, value.retry);
}

function validRetry(sessionId: string, value: unknown): value is AmbiguousTurnRetry {
  if (!isRecord(value) || !isRecord(value.userMessage)) return false;
  const message = value.userMessage;
  return (
    SESSION_ID_PATTERN.test(sessionId) &&
    typeof value.content === "string" &&
    value.content.length > 0 &&
    Number.isInteger(value.draftRevision) &&
    (value.draftRevision as number) >= 0 &&
    typeof message.id === "string" &&
    message.id.length >= 8 &&
    message.id.length <= 128 &&
    MESSAGE_ID_PATTERN.test(message.id) &&
    message.session_id === sessionId &&
    message.role === "user" &&
    message.content === value.content &&
    typeof message.created_at === "string" &&
    Number.isFinite(Date.parse(message.created_at))
  );
}

function validSessionCreationRetry(value: unknown): value is SessionCreationRetry {
  return Boolean(
    isRecord(value) &&
      typeof value.content === "string" &&
      value.content.trim().length > 0 &&
      typeof value.creationId === "string" &&
      value.creationId.length <= 128 &&
      CREATION_ID_PATTERN.test(value.creationId),
  );
}

function storageKey(sessionId: string): string {
  return `${TURN_OUTBOX_PREFIX}${sessionId}`;
}

function rawRemoveTurnOutbox(sessionId: string): void {
  try {
    window.sessionStorage.removeItem(storageKey(sessionId));
  } catch {
    // Best-effort browser cleanup only.
  }
}

function rawRemoveSessionCreationOutbox(): void {
  try {
    window.sessionStorage.removeItem(SESSION_CREATION_OUTBOX_KEY);
  } catch {
    // Best-effort browser cleanup only.
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
