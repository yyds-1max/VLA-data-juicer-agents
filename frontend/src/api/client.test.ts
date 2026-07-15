import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSession,
  deleteSession,
  getSession,
  getNavigationDatasetDate,
  getNavigationDatasetSummary,
  getSyncImageUrl,
  getSyncImages,
  interruptTurn,
  isAmbiguousTurnSubmissionError,
  listSessions,
  recoverHumanDecision,
  submitHumanDecision,
  submitTurn,
  streamSessionEvents,
} from "./client";
import { EventType } from "@agentscope-ai/agentscope/event";

import type {
  HumanDecisionPayload,
  HumanDecisionRecoveryRequest,
  HumanDecisionRecoveryResponse,
  NavigationDatasetSummary,
  NavigationSyncImageListing,
  PublicEventEnvelope,
  SessionDetail,
  SessionRecord,
} from "./types";

function session(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    id: "session-1",
    title: "Active",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    ...session(),
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: 0,
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

    await expect(createSession("hello", "local-create-1")).resolves.toEqual(record);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ message: "hello", creation_id: "local-create-1" }),
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

  it("follows session-history cursors until every page is loaded", async () => {
    const first = Array.from({ length: 20 }, (_, index) => session({ id: `session-${index}` }));
    const oldest = Array.from({ length: 7 }, (_, index) => session({ id: `session-${index + 20}` }));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: first, next_cursor: "opaque cursor" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: oldest, next_cursor: null }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listSessions()).resolves.toEqual([...first, ...oldest]);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/sessions", {
      headers: { "content-type": "application/json" },
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/sessions?cursor=opaque%20cursor",
      { headers: { "content-type": "application/json" } },
    );
  });

  it("deduplicates a session repeated across adjacent cursor pages", async () => {
    const repeated = session({ id: "session-overlap" });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({
          sessions: [session({ id: "session-new" }), repeated],
          next_cursor: "next-page",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({
          sessions: [repeated, session({ id: "session-old" })],
          next_cursor: null,
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listSessions()).resolves.toEqual([
      session({ id: "session-new" }),
      repeated,
      session({ id: "session-old" }),
    ]);
  });

  it("rejects a repeated session-history cursor after a bounded number of requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: "repeat" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: "repeat" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: null }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listSessions()).rejects.toThrow(SyntaxError);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rejects a cyclic session-history cursor after a bounded number of requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: "cursor-a" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: "cursor-b" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: "cursor-a" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.resolve({ sessions: [], next_cursor: null }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listSessions()).rejects.toThrow(SyntaxError);
    expect(fetchMock).toHaveBeenCalledTimes(3);
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
    const fetchMock = mockFetchJson({
      turn_id: "turn-1",
      replayed: false,
      terminal: false,
    });

    await expect(
      submitTurn("session/with space", "next", "local-message-1"),
    ).resolves.toEqual({ turnId: "turn-1", replayed: false, terminal: false });
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space/turns", {
      method: "POST",
      body: JSON.stringify({ message: "next", message_id: "local-message-1" }),
      headers: { "content-type": "application/json" },
    });
  });

  it("classifies an explicit pending exact turn as ambiguous and retryable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        statusText: "Conflict",
        text: () =>
          Promise.resolve(
            JSON.stringify({
              detail: {
                code: "turn_submission_pending",
                message: "turn submission is still pending",
              },
            }),
          ),
      }),
    );

    const error = await submitTurn("session-1", "same", "local-pending-1").catch(
      (caught) => caught,
    );

    expect(error).toMatchObject({
      status: 409,
      code: "turn_submission_pending",
    });
    expect(isAmbiguousTurnSubmissionError(error)).toBe(true);
    expect(isAmbiguousTurnSubmissionError(new TypeError("network lost"))).toBe(true);
  });

  it("classifies transient HTTP and success-body decode failures as ambiguous turns", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        statusText: "Bad Gateway",
        text: () => Promise.resolve("upstream response lost"),
      }),
    );
    const gatewayError = await submitTurn(
      "session-1",
      "same",
      "local-gateway-1",
    ).catch((caught) => caught);
    expect(isAmbiguousTurnSubmissionError(gatewayError)).toBe(true);

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: "OK",
        json: () => Promise.reject(new SyntaxError("truncated JSON")),
      }),
    );
    const decodeError = await submitTurn(
      "session-1",
      "same",
      "local-decode-1",
    ).catch((caught) => caught);
    expect(isAmbiguousTurnSubmissionError(decodeError)).toBe(true);
  });

  it.each([
    ["missing turn id", { replayed: false, terminal: false }],
    ["empty turn id", { turn_id: "", replayed: false, terminal: false }],
    ["non-boolean replayed", { turn_id: "turn-1", replayed: "false", terminal: false }],
    ["missing terminal", { turn_id: "turn-1", replayed: false }],
  ])("rejects a malformed successful turn response as ambiguous: %s", async (_name, body) => {
    mockFetchJson(body);

    const error = await submitTurn("session-1", "same", "local-malformed-1").catch(
      (caught) => caught,
    );

    expect(error).toBeInstanceOf(SyntaxError);
    expect(isAmbiguousTurnSubmissionError(error)).toBe(true);
  });

  it("encodes the session id and posts an interrupt request", async () => {
    const fetchMock = mockFetchJson({ interrupted: true });

    await expect(interruptTurn("session/with space")).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space/interrupt", {
      method: "POST",
      headers: { "content-type": "application/json" },
    });
  });

  it("encodes the session id and posts a human decision payload", async () => {
    const fetchMock = mockFetchJson({ accepted: true });
    const payload: HumanDecisionPayload = {
      action: "guide",
      request_id: "request-1",
      tool_call_id: "tool-call-1",
      reply_id: "reply-1",
      text: "请先汇总风险再继续。",
    };

    await expect(submitHumanDecision("session/with space", payload)).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space/human-decisions", {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "content-type": "application/json" },
    });
  });

  it("posts the exact controlled recovery payload and returns its anchor", async () => {
    const recovered: HumanDecisionRecoveryResponse = {
      recovered: true,
      plan_id: "plan-1",
      step_id: "confirm",
      handoff_status: "quarantined",
      task_status: "needs_replan",
      next_action: "submit_complete_plan",
    };
    const fetchMock = mockFetchJson(recovered);
    const payload: HumanDecisionRecoveryRequest = {
      action: "quarantine_and_replan",
      plan_id: "plan-1",
      step_id: "confirm",
      reason: "operator confirmed abandoned delivery",
    };

    await expect(recoverHumanDecision("session/with space", payload)).resolves.toEqual(recovered);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/session%2Fwith%20space/human-decisions/recovery",
      {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "content-type": "application/json" },
      },
    );
  });

  it("surfaces controlled recovery conflict detail", async () => {
    mockFetchJson({ detail: "handoff is not recovery_required" }, false);
    await expect(
      recoverHumanDecision("session-1", {
        action: "quarantine_and_replan",
        plan_id: "plan-1",
        step_id: "confirm",
        reason: "recover",
      }),
    ).rejects.toMatchObject({ message: "handoff is not recovery_required" });
  });

  it("throws useful detail from non-ok JSON error responses", async () => {
    mockFetchJson({ detail: "Session not found" }, false);

    await expect(getSession("missing")).rejects.toMatchObject({ message: "Session not found" });
  });

  it("falls back to plain text for non-ok responses", async () => {
    mockFetchJson("Service unavailable", false);

    await expect(getSession("missing")).rejects.toMatchObject({ message: "Service unavailable" });
  });

  it("streams encoded SSE events from the requested cursor across arbitrary chunks", async () => {
    const event: PublicEventEnvelope = {
      id: "event-1",
      session_id: "session/with space",
      sequence: 1,
      dedupe_key: "1".padStart(64, "0"),
      event: {
        id: "custom-1",
        created_at: "2026-06-26T00:00:00Z",
        type: EventType.CUSTOM,
        name: "datapilot_progress",
        value: { summary: "处理中" },
      },
      created_at: "2026-06-26T00:00:00Z",
    };
    const encoded = new TextEncoder().encode(
      `: heartbeat\r\n\r\nevent: public_event\r\ndata: ${JSON.stringify(event)}\r\n\r\n`,
    );
    const splitInsideUtf8 = encoded.findIndex((value) => value >= 0x80) + 1;
    const chunks = [
      encoded.slice(0, 7),
      encoded.slice(7, splitInsideUtf8),
      encoded.slice(splitInsideUtf8),
    ];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(chunk);
          controller.close();
        },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const controller = new AbortController();
    const received: PublicEventEnvelope[] = [];
    for await (const envelope of streamSessionEvents("session/with space", 12, controller.signal)) {
      received.push(envelope);
    }

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/session%2Fwith%20space/stream?after_sequence=12",
      { signal: controller.signal, headers: { Accept: "text/event-stream" } },
    );
    expect(received).toEqual([event]);
  });

  it("skips comments, empty frames, unrelated events, and malformed public envelopes", async () => {
    const valid: PublicEventEnvelope = {
      id: "event-2",
      session_id: "session-1",
      sequence: 2,
      dedupe_key: "2".padStart(64, "0"),
      event: {
        id: "custom-2",
        created_at: "2026-06-26T00:00:00Z",
        type: EventType.CUSTOM,
        name: "datapilot_progress",
        value: {},
      },
      created_at: "2026-06-26T00:00:00Z",
    };
    const body = [
      ": heartbeat\n\n",
      "\n",
      "event: ignored\ndata: {}\n\n",
      "event: public_event\ndata: not-json\n\n",
      "event: public_event\ndata: {\"sequence\":2}\n\n",
      `data: ${JSON.stringify(valid)}\n\n`,
    ].join("");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      body: new Response(body).body,
    }));

    const received: PublicEventEnvelope[] = [];
    for await (const envelope of streamSessionEvents("session-1", 1, new AbortController().signal)) {
      received.push(envelope);
    }

    expect(received).toEqual([valid]);
  });

  it("ends cleanly when an SSE request is aborted", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    })));

    const consume = (async () => {
      for await (const _event of streamSessionEvents("session-1", 0, controller.signal)) {
        // no-op
      }
    })();
    controller.abort();

    await expect(consume).resolves.toBeUndefined();
  });

  it("cancels an in-progress SSE body when aborted", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      body,
    }));
    const controller = new AbortController();
    const consume = (async () => {
      for await (const _event of streamSessionEvents("session-1", 0, controller.signal)) {
        // no-op
      }
    })();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());

    controller.abort();

    await expect(consume).resolves.toBeUndefined();
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("surfaces non-abort SSE failures and response detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("network down")));
    await expect(async () => {
      for await (const _event of streamSessionEvents("session-1", 0, new AbortController().signal)) {
        // no-op
      }
    }).rejects.toThrow("network down");

    mockFetchJson({ detail: "stream unavailable" }, false);
    await expect(async () => {
      for await (const _event of streamSessionEvents("session-1", 0, new AbortController().signal)) {
        // no-op
      }
    }).rejects.toThrow("stream unavailable");
  });

  it("deletes an encoded session and accepts a 204 response", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 204,
      statusText: "No Content",
      text: () => Promise.resolve(""),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(deleteSession("session/with space")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/session%2Fwith%20space", {
      method: "DELETE",
      headers: { "content-type": "application/json" },
    });
  });

  it("does not treat a non-204 delete response as completed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(""),
    }));

    await expect(deleteSession("session-1")).rejects.toThrow("Expected 204");
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
