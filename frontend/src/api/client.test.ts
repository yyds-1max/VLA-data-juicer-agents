import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSession,
  getSession,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitTurn,
} from "./client";
import type { AgentEvent, SessionDetail, SessionRecord } from "./types";

function session(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    id: "session-1",
    title: "Active",
    status: "active",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    ...session(),
    messages: [],
    ...overrides,
  };
}

function mockFetchJson(body: unknown, ok = true) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 404,
    statusText: ok ? "OK" : "Not Found",
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("creates a session and returns the session record", async () => {
    const record = session();
    const fetchMock = mockFetchJson({ session: record });

    await expect(createSession("hello")).resolves.toEqual(record);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ message: "hello" }),
      headers: { "content-type": "application/json" },
    });
  });

  it("lists sessions from the sessions endpoint", async () => {
    const sessions = [session({ id: "session-1" }), session({ id: "session-2" })];
    const fetchMock = mockFetchJson({ sessions });

    await expect(listSessions()).resolves.toEqual(sessions);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions", {
      headers: { "content-type": "application/json" },
    });
  });

  it("encodes the session id when getting session detail", async () => {
    const sessionDetail = detail({ id: "session/with space" });
    const fetchMock = mockFetchJson({ session: sessionDetail });

    await expect(getSession("session/with space")).resolves.toEqual(sessionDetail);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space", {
      headers: { "content-type": "application/json" },
    });
  });

  it("encodes the session id and posts a submitted turn message", async () => {
    const fetchMock = mockFetchJson({ turn_id: "turn-1" });

    await expect(submitTurn("session/with space", "next")).resolves.toBe("turn-1");
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space/turns", {
      method: "POST",
      body: JSON.stringify({ message: "next" }),
      headers: { "content-type": "application/json" },
    });
  });

  it("encodes the session id and posts an interrupt request", async () => {
    const fetchMock = mockFetchJson({ interrupted: true });

    await expect(interruptTurn("session/with space")).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space/interrupt", {
      method: "POST",
      headers: { "content-type": "application/json" },
    });
  });

  it("throws useful detail from non-ok JSON error responses", async () => {
    mockFetchJson({ detail: "Session not found" }, false);

    await expect(getSession("missing")).rejects.toMatchObject({ message: "Session not found" });
  });

  it("falls back to plain text for non-ok responses", async () => {
    mockFetchJson("Service unavailable", false);

    await expect(getSession("missing")).rejects.toMatchObject({ message: "Service unavailable" });
  });

  it("opens encoded session events and passes parsed websocket messages to the callback", () => {
    const addEventListener = vi.fn();
    const WebSocketMock = vi.fn().mockImplementation(() => ({ addEventListener }));
    vi.stubGlobal("WebSocket", WebSocketMock);
    vi.stubGlobal("location", {
      protocol: "https:",
      host: "example.test",
    });

    const onEvent = vi.fn();
    const socket = openSessionEvents("session/with space", onEvent);

    expect(WebSocketMock).toHaveBeenCalledWith(
      "wss://example.test/api/sessions/session%2Fwith%20space/events",
    );
    expect(addEventListener).toHaveBeenCalledWith("message", expect.any(Function));
    expect(socket).toEqual({ addEventListener });

    const [, onMessage] = addEventListener.mock.calls[0] as [
      string,
      (message: MessageEvent<string>) => void,
    ];
    const event: AgentEvent = { type: "token", payload: { text: "hello" } };
    onMessage(new MessageEvent("message", { data: JSON.stringify(event) }));

    expect(onEvent).toHaveBeenCalledWith(event);
  });
});
