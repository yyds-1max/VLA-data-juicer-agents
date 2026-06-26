import { afterEach, describe, expect, it, vi } from "vitest";

import { createSession, openSessionEvents } from "./client";

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("creates a session and returns the session record", async () => {
    const session = {
      id: "session-1",
      title: "Draft",
      status: "draft" as const,
      created_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ session }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createSession("hello")).resolves.toEqual(session);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ message: "hello" }),
      headers: { "content-type": "application/json" },
    });
  });

  it("opens encoded session events over the matching websocket protocol", () => {
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
  });
});
