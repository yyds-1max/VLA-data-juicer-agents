import { beforeEach, describe, expect, it } from "vitest";

import {
  TURN_OUTBOX_TTL_MS,
  readSessionCreationOutbox,
  readTurnOutbox,
  removeSessionCreationOutbox,
  removeTurnOutbox,
  writeSessionCreationOutbox,
  writeTurnOutbox,
} from "./turnOutbox";

const sessionId = "session-1";
const messageId = "local-message-1";
const retry = {
  content: "continue exactly once",
  userMessage: {
    id: messageId,
    session_id: sessionId,
    role: "user" as const,
    content: "continue exactly once",
    created_at: "2026-07-15T00:00:00.000Z",
  },
  draftRevision: 3,
};

describe("turn outbox", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("stores a retry only for the current tab with a bounded TTL", () => {
    writeTurnOutbox(sessionId, retry, 1_000);

    expect(window.localStorage.length).toBe(0);
    expect(readTurnOutbox(sessionId, 1_000 + TURN_OUTBOX_TTL_MS - 1)).toEqual(retry);
    expect(readTurnOutbox(sessionId, 1_000 + TURN_OUTBOX_TTL_MS)).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it.each([
    ["mismatched session", { sessionId: "session-other" }],
    ["unsafe session", { sessionId: "../session-1" }],
    ["unsafe message id", { retry: { ...retry, userMessage: { ...retry.userMessage, id: "message-1" } } }],
    ["mismatched content", { retry: { ...retry, content: "different" } }],
    ["unbounded expiry", { expiresAt: 1_000 + TURN_OUTBOX_TTL_MS + 1 }],
  ])("rejects and removes an invalid %s payload", (_name, override) => {
    window.sessionStorage.setItem(
      `datapilot-turn-outbox:${sessionId}`,
      JSON.stringify({
        version: 1,
        sessionId,
        expiresAt: 1_000 + TURN_OUTBOX_TTL_MS,
        retry,
        ...override,
      }),
    );

    expect(readTurnOutbox(sessionId, 1_000)).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it("does not let an old completion remove a newer exact retry", () => {
    writeTurnOutbox(sessionId, retry, 1_000);
    const replacement = {
      ...retry,
      content: "new intent",
      userMessage: {
        ...retry.userMessage,
        id: "local-message-2",
        content: "new intent",
      },
    };
    writeTurnOutbox(sessionId, replacement, 1_001);

    removeTurnOutbox(sessionId, messageId);

    expect(readTurnOutbox(sessionId, 1_001)).toEqual(replacement);
  });

  it("persists a first-session creation id with its exact intent and TTL", () => {
    const creation = { content: "create exactly once", creationId: "local-create-1" };
    writeSessionCreationOutbox(creation, 1_000);

    expect(readSessionCreationOutbox(1_000)).toEqual(creation);
    expect(readSessionCreationOutbox(1_000 + TURN_OUTBOX_TTL_MS)).toBeNull();
  });

  it("does not let an old creation completion remove a replacement intent", () => {
    writeSessionCreationOutbox(
      { content: "old", creationId: "local-create-old" },
      1_000,
    );
    writeSessionCreationOutbox(
      { content: "new", creationId: "local-create-new" },
      1_001,
    );

    removeSessionCreationOutbox("local-create-old");

    expect(readSessionCreationOutbox(1_001)).toEqual({
      content: "new",
      creationId: "local-create-new",
    });
  });
});
