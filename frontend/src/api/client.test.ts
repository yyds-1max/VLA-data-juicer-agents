import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSession,
  getSession,
  getNavigationDatasetDate,
  getNavigationDatasetSummary,
  getSyncImageUrl,
  getSyncImages,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitTurn,
} from "./client";
import type {
  AgentEvent,
  NavigationDatasetSummary,
  NavigationSyncImageListing,
  SessionDetail,
  SessionRecord,
} from "./types";

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

  it("gets the navigation dataset summary", async () => {
    const summary: NavigationDatasetSummary = {
      totals: {
        date_count: 1,
        clip_count: 2,
        total_duration_ns: 1000,
        raw_message_count: 50,
        extracted_clip_count: 1,
        synced_clip_count: 1,
      },
      sync_distribution: {
        image: 10,
        pointcloud: 8,
        odom: 7,
        grid_map: 6,
      },
      dates: [],
    };
    const fetchMock = mockFetchJson(summary);

    await expect(getNavigationDatasetSummary()).resolves.toEqual(summary);
    expect(fetchMock).toHaveBeenCalledWith("/api/navigation/datasets/summary", {
      headers: { "content-type": "application/json" },
    });
  });

  it("encodes the date when getting navigation dataset date detail", async () => {
    const detail = {
      date: "2026/06 29",
      clip_count: 1,
      total_duration_ns: 1000,
      raw_message_count: 50,
      extracted_clip_count: 1,
      synced_clip_count: 1,
      sync_frame_counts: {
        image: 10,
        pointcloud: 8,
        odom: 7,
        grid_map: 6,
      },
      status: "synced",
      clips: [],
    };
    const fetchMock = mockFetchJson(detail);

    await expect(getNavigationDatasetDate("2026/06 29")).resolves.toEqual(detail);
    expect(fetchMock).toHaveBeenCalledWith("/api/navigation/datasets/2026%2F06%2029", {
      headers: { "content-type": "application/json" },
    });
  });

  it("encodes the clip when listing sync images", async () => {
    const listing: NavigationSyncImageListing = {
      date: "2026-06-29",
      clip: "clip/with space",
      sequences: [{ sequence: "seq-1", images: ["000001.jpg"] }],
    };
    const fetchMock = mockFetchJson(listing);

    await expect(getSyncImages("2026-06-29", "clip/with space")).resolves.toEqual(listing);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/navigation/datasets/2026-06-29/clips/clip%2Fwith%20space/sync-images",
      { headers: { "content-type": "application/json" } },
    );
  });

  it("encodes date, clip, sequence, and filename when building a sync image URL", () => {
    expect(getSyncImageUrl("2026/06 29", "clip/1", "seq 1/left", "frame 1.png")).toBe(
      "/api/navigation/datasets/2026%2F06%2029/clips/clip%2F1/sync-images/seq%201%2Fleft/frame%201.png",
    );
  });
});
