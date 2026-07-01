import { describe, expect, it } from "vitest";

import type { AgentEvent, ChatMessageRecord, SessionDetail, SessionRecord } from "../api/types";
import { createEmptyRunState, applyAgentEvent } from "./eventReducer";
import { createDataPilotStore } from "./datapilotStore";

function event(
  type: string,
  source: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<AgentEvent> = {},
): AgentEvent {
  return {
    type,
    source,
    run_id: "run-1",
    parent_run_id: null,
    timestamp: "2026-06-26T00:00:00.000Z",
    payload,
    ...overrides,
  };
}

function session(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    id: "session-1",
    title: "Active session",
    status: "active",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    ...overrides,
  };
}

function message(overrides: Partial<ChatMessageRecord> = {}): ChatMessageRecord {
  return {
    id: "message-1",
    session_id: "session-1",
    role: "user",
    content: "hello",
    created_at: "2026-06-26T00:00:00Z",
    ...overrides,
  };
}

function sessionDetail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    ...session(overrides),
    messages: [],
    ...overrides,
  };
}

function pendingDecision(overrides: Record<string, unknown> = {}) {
  return {
    replyId: "reply-1",
    toolCallId: "tool-call-1",
    requestId: "request-1",
    decisionType: "other",
    summary: "请确认下一步。",
    ...overrides,
  };
}

describe("eventReducer", () => {
  it("captures a pending human decision and pauses active run text", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("agent_start", "main"));
    applyAgentEvent(
      state,
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "request-1",
        decision_type: "confirmation",
        summary: "发现潜在风险，需要人工确认。",
      }),
    );

    expect(state.pendingHumanDecision).toEqual({
      replyId: "reply-1",
      toolCallId: "tool-call-1",
      requestId: "request-1",
      decisionType: "confirmation",
      summary: "发现潜在风险，需要人工确认。",
    });
    expect(state.running).toBe(false);
    expect(state.activeText).toBe("");
    expect(state.activeStartedAt).toBeNull();
  });

  it("defaults missing decision type to other", () => {
    const state = createEmptyRunState();

    applyAgentEvent(
      state,
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-2",
        tool_call_id: "tool-call-2",
        request_id: "request-2",
        summary: "请确认下一步。",
      }),
    );

    expect(state.pendingHumanDecision).toMatchObject({
      decisionType: "other",
    });
  });

  it("keeps pending human decision after assistant output arrives", () => {
    const state = createEmptyRunState();

    applyAgentEvent(
      state,
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "request-1",
        summary: "请确认下一步。",
      }),
    );
    applyAgentEvent(state, event("assistant_delta", "main", { delta: "收到。" }));
    applyAgentEvent(state, event("final", "main", { text: "收到，请确认。" }, { run_id: "final-run" }));

    expect(state.pendingHumanDecision).toEqual({
      replyId: "reply-1",
      toolCallId: "tool-call-1",
      requestId: "request-1",
      decisionType: "other",
      summary: "请确认下一步。",
    });
  });

  it("localizes main agent_start active text", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("agent_start", "main"));

    expect(state.running).toBe(true);
    expect(state.activeText).toBe("[Main] 正在思考");
    expect(state.activeAgents["run-1"]).toMatchObject({
      source: "main",
      runId: "run-1",
      parentRunId: null,
      startedAt: Date.parse("2026-06-26T00:00:00.000Z"),
    });
    expect(state.activeStartedAt).toBe(Date.parse("2026-06-26T00:00:00.000Z"));
  });

  it("creates compact tool completion text without args JSON", () => {
    const state = createEmptyRunState();

    applyAgentEvent(
      state,
      event(
        "tool_start",
        "navigation.plan",
        { call_id: "call-1", tool: "classify_navigation_dataset_tool", args: '{"date":"20270605"}' },
        { timestamp: "2026-06-26T00:00:00.000Z" },
      ),
    );
    applyAgentEvent(
      state,
      event(
        "tool_end",
        "navigation.plan",
        { call_id: "call-1", tool: "classify_navigation_dataset_tool", ok: true },
        { timestamp: "2026-06-26T00:00:01.000Z" },
      ),
    );

    expect(state.activeTools).toEqual({});
    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toMatchObject({
      kind: "tool",
      source: "navigation.plan",
      status: "completed",
      text: "completed classify_navigation_dataset_tool 1.0s",
    });
    expect(state.timeline[0].text).not.toContain("20270605");
    expect(state.timeline[0].text).not.toContain("{");
  });

  it("preserves child source and summary for folding", () => {
    const state = createEmptyRunState();

    applyAgentEvent(
      state,
      event(
        "reasoning",
        "navigation.plan",
        { summary: "先检查原始片段。" },
        { run_id: "plan-run", parent_run_id: "workflow-run" },
      ),
    );

    expect(state.timeline).toEqual([
      {
        kind: "reasoning",
        source: "navigation.plan",
        text: "先检查原始片段。",
        runId: "plan-run",
        parentRunId: "workflow-run",
      },
    ]);
  });

  it("returns to the deepest active agent after a child tool ends", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("agent_start", "main", {}, { run_id: "main-run" }));
    applyAgentEvent(
      state,
      event("agent_start", "navigation.workflow", {}, { run_id: "workflow-run", parent_run_id: "main-run" }),
    );
    applyAgentEvent(
      state,
      event("agent_start", "navigation.plan", {}, { run_id: "plan-run", parent_run_id: "workflow-run" }),
    );
    applyAgentEvent(
      state,
      event(
        "tool_start",
        "navigation.plan",
        { call_id: "call-1", tool: "classify_navigation_dataset_tool" },
        { run_id: "plan-run", parent_run_id: "workflow-run" },
      ),
    );

    applyAgentEvent(
      state,
      event(
        "tool_end",
        "navigation.plan",
        { call_id: "call-1", tool: "classify_navigation_dataset_tool", ok: true },
        { run_id: "plan-run", parent_run_id: "workflow-run" },
      ),
    );

    expect(state.running).toBe(true);
    expect(state.activeText).toBe("[Plan] 正在思考");
    expect(state.activeStartedAt).toBe(Date.parse("2026-06-26T00:00:00.000Z"));
  });

  it("dedupes final events by run id", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("final", "main", { text: "first answer" }, { run_id: "final-run" }));
    applyAgentEvent(state, event("final", "main", { text: "duplicate answer" }, { run_id: "final-run" }));

    expect(state.timeline.filter((item) => item.kind === "assistant")).toEqual([
      {
        kind: "assistant",
        source: "main",
        text: "first answer",
        runId: "final-run",
        parentRunId: null,
      },
    ]);
  });

  it("does not dedupe final events without run id", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("final", "main", { text: "first answer" }, { run_id: "" }));
    applyAgentEvent(state, event("final", "main", { text: "second answer" }, { run_id: "" }));

    expect(state.timeline.filter((item) => item.kind === "assistant").map((item) => item.text)).toEqual([
      "first answer",
      "second answer",
    ]);
  });

  it("streams assistant delta into one final assistant item", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("agent_start", "main", {}, { run_id: "stream-run" }));
    applyAgentEvent(state, event("assistant_delta", "main", { delta: "你好，" }, { run_id: "stream-run" }));
    applyAgentEvent(state, event("assistant_delta", "main", { delta: "我是 DataPilot" }, { run_id: "stream-run" }));

    expect(state.running).toBe(true);
    expect(state.activeText).toBe("");
    expect(state.activeStartedAt).toBeNull();
    expect(state.timeline.filter((item) => item.kind === "assistant")).toEqual([
      {
        kind: "assistant",
        source: "main",
        text: "你好，我是 DataPilot",
        runId: "stream-run",
        parentRunId: null,
      },
    ]);

    applyAgentEvent(state, event("final", "main", { text: "你好，我是 DataPilot。" }, { run_id: "stream-run" }));

    expect(state.timeline.filter((item) => item.kind === "assistant")).toEqual([
      {
        kind: "assistant",
        source: "main",
        text: "你好，我是 DataPilot。",
        runId: "stream-run",
        parentRunId: null,
      },
    ]);
  });
});

describe("datapilotStore", () => {
  it("clearPendingHumanDecision clears the matching pending human decision from the current run", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().applyEvent(
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "request-1",
        summary: "请确认下一步。",
      }),
    );

    store.getState().clearPendingHumanDecision(pendingDecision(), "session-1");

    expect(store.getState().run.pendingHumanDecision).toBeNull();
  });

  it("clearPendingHumanDecision keeps a newer pending human decision when identities differ", () => {
    const store = createDataPilotStore();

    store.getState().applyEvent(
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-2",
        tool_call_id: "tool-call-2",
        request_id: "request-2",
        summary: "需要确认第二步。",
      }),
    );

    store.getState().clearPendingHumanDecision(pendingDecision(), "session-1");

    expect(store.getState().run.pendingHumanDecision).toEqual({
      replyId: "reply-2",
      toolCallId: "tool-call-2",
      requestId: "request-2",
      decisionType: "other",
      summary: "需要确认第二步。",
    });
  });

  it("clearPendingHumanDecision keeps pending when the current session does not match", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session({ id: "session-b" }));
    store.getState().applyEvent(
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "request-1",
        summary: "B 会话里的确认。",
      }),
    );

    store.getState().clearPendingHumanDecision(pendingDecision(), "session-a");

    expect(store.getState().currentSessionId).toBe("session-b");
    expect(store.getState().run.pendingHumanDecision).toEqual({
      replyId: "reply-1",
      toolCallId: "tool-call-1",
      requestId: "request-1",
      decisionType: "other",
      summary: "B 会话里的确认。",
    });
  });

  it("enterDraft records the active session and clears messages and run", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().appendUserMessage(message());
    store.getState().applyEvent(event("agent_start", "main"));

    store.getState().enterDraft();

    expect(store.getState().mode).toBe("draft_new_session");
    expect(store.getState().previousActiveSessionId).toBe("session-1");
    expect(store.getState().currentSessionId).toBeNull();
    expect(store.getState().messages).toEqual([]);
    expect(store.getState().run).toEqual(createEmptyRunState());
  });

  it("preserves final dedupe state when cloning run state", () => {
    const store = createDataPilotStore();

    store.getState().applyEvent(event("final", "main", { text: "first answer" }, { run_id: "final-run" }));
    store.getState().applyEvent(event("final", "main", { text: "duplicate answer" }, { run_id: "final-run" }));

    expect(store.getState().run.timeline.filter((item) => item.kind === "assistant")).toHaveLength(1);
  });

  it("merges active session refresh messages without dropping newer local messages", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().appendUserMessage(
      message({
        id: "local-message",
        content: "new local turn",
        created_at: "2026-06-26T00:02:00Z",
      }),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [
          message({
            id: "persisted-message",
            role: "assistant",
            content: "persisted answer",
            created_at: "2026-06-26T00:01:00Z",
          }),
        ],
      }),
    );

    expect(store.getState().messages.map((item) => item.content)).toEqual(["persisted answer", "new local turn"]);
  });

  it("replaces a matching local user echo with the persisted user message", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().appendUserMessage(
      message({
        id: "local-user-message",
        content: "你好，你是谁？",
        created_at: "2026-06-26T00:01:01Z",
      }),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [
          message({
            id: "persisted-user-message",
            content: "你好，你是谁？",
            created_at: "2026-06-26T00:01:00Z",
          }),
        ],
      }),
    );

    expect(store.getState().messages).toEqual([
      message({
        id: "persisted-user-message",
        content: "你好，你是谁？",
        created_at: "2026-06-26T00:01:00Z",
      }),
    ]);
  });

  it("refreshing an active session keeps live run state from the event stream", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().applyEvent(event("agent_start", "main"));
    store.getState().applyEvent(event("reasoning", "main", { summary: "live reasoning" }));

    store.getState().refreshActiveSession(sessionDetail({ messages: [message()] }));

    expect(store.getState().run.running).toBe(true);
    expect(store.getState().run.activeText).toBe("[Main] 正在思考");
    expect(store.getState().run.timeline).toMatchObject([{ kind: "reasoning", text: "live reasoning" }]);
  });
});
