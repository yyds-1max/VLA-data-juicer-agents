import { EventType, type AgentEvent } from "@agentscope-ai/agentscope/event";

import type {
  ChatMessageRecord,
  PublicEventEnvelope,
  PublicToolRun,
} from "../api/types";
import {
  applyPublicEvent,
  createAgentConversation,
  markConversationInterrupting,
  normalizePublicAgentEvent,
  restoreAgentConversation,
} from "./agentConversation";
import { createDataPilotStore } from "./datapilotStore";

const CREATED_AT = "2026-07-15T08:00:00.000Z";

function envelope(sequence: number, event: AgentEvent): PublicEventEnvelope {
  return {
    id: `event-${sequence}`,
    session_id: "session-1",
    sequence,
    dedupe_key: sequence.toString(16).padStart(64, "0"),
    event,
    created_at: CREATED_AT,
  };
}

function replyStart(replyId: string): AgentEvent {
  return {
    id: `start-${replyId}`,
    created_at: CREATED_AT,
    type: EventType.REPLY_START,
    session_id: "DataPilot",
    reply_id: replyId,
    name: "private-agent-name",
    role: "assistant",
  };
}

function textStart(replyId: string, blockId: string): AgentEvent {
  return {
    id: `text-start-${blockId}`,
    created_at: CREATED_AT,
    type: EventType.TEXT_BLOCK_START,
    reply_id: replyId,
    block_id: blockId,
  };
}

function textDelta(replyId: string, blockId: string, delta: string): AgentEvent {
  return {
    id: `text-delta-${blockId}-${delta}`,
    created_at: CREATED_AT,
    type: EventType.TEXT_BLOCK_DELTA,
    reply_id: replyId,
    block_id: blockId,
    delta,
  };
}

function replyEnd(replyId: string): AgentEvent {
  return {
    id: `end-${replyId}`,
    created_at: CREATED_AT,
    type: EventType.REPLY_END,
    session_id: "DataPilot",
    reply_id: replyId,
  };
}

function custom(name: string, value: Record<string, unknown>): AgentEvent {
  return {
    id: `${name}-${JSON.stringify(value)}`,
    created_at: CREATED_AT,
    type: EventType.CUSTOM,
    name,
    value,
  };
}

describe("AgentScope conversation reduction", () => {
  it("reduces a native reply stream into an SDK AssistantMsg", () => {
    const state = createAgentConversation();

    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(state, envelope(2, textStart("reply-1", "block-1")));
    applyPublicEvent(state, envelope(3, textDelta("reply-1", "block-1", "完成")));
    applyPublicEvent(state, envelope(4, replyEnd("reply-1")));

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      id: "reply-1",
      name: "DataPilot",
      role: "assistant",
      content: [{ type: "text", id: "block-1", text: "完成" }],
      finished_at: CREATED_AT,
    });
    expect(state.phase).toBe("idle");
    expect(state.currentReplyId).toBeNull();
    expect(state.lastSequence).toBe(4);
  });

  it("ignores every duplicate or out-of-order sequence", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(state, envelope(2, textStart("reply-1", "block-1")));
    applyPublicEvent(state, envelope(3, textDelta("reply-1", "block-1", "first")));

    applyPublicEvent(state, envelope(3, textDelta("reply-1", "block-1", " duplicate")));
    applyPublicEvent(state, envelope(2, textDelta("reply-1", "block-1", " stale")));

    expect(state.messages[0].content[0]).toMatchObject({ text: "first" });
    expect(state.lastSequence).toBe(3);
  });

  it("continues a restored in-flight reply only after the snapshot sequence", () => {
    const restored = restoreAgentConversation({
      messages: [],
      events: [
        envelope(1, replyStart("reply-1")),
        envelope(2, textStart("reply-1", "block-1")),
        envelope(3, textDelta("reply-1", "block-1", "已")),
      ],
      toolRuns: [],
      lastSequence: 3,
    });

    applyPublicEvent(restored, envelope(3, textDelta("reply-1", "block-1", "重复")));
    applyPublicEvent(restored, envelope(4, textDelta("reply-1", "block-1", "恢复")));
    applyPublicEvent(restored, envelope(5, replyEnd("reply-1")));

    expect(restored.messages[0].content[0]).toMatchObject({ text: "已恢复" });
    expect(restored.phase).toBe("idle");
    expect(restored.lastSequence).toBe(5);
  });

  it("chronologically interleaves two persisted user turns with native replies", () => {
    const restored = restoreAgentConversation({
      messages: [
        userRecord("user-1", "第一问", "2026-07-15T08:00:00.000Z"),
        userRecord("user-2", "第二问", "2026-07-15T08:02:00.000Z"),
      ],
      events: [
        timedEnvelope(1, replyStart("reply-1"), "2026-07-15T08:00:00.000Z"),
        timedEnvelope(2, textStart("reply-1", "block-1"), "2026-07-15T08:00:01.000Z"),
        timedEnvelope(3, textDelta("reply-1", "block-1", "第一答"), "2026-07-15T08:00:02.000Z"),
        timedEnvelope(4, replyEnd("reply-1"), "2026-07-15T08:00:03.000Z"),
        timedEnvelope(5, replyStart("reply-2"), "2026-07-15T08:02:00.000Z"),
        timedEnvelope(6, textStart("reply-2", "block-2"), "2026-07-15T08:02:01.000Z"),
        timedEnvelope(7, textDelta("reply-2", "block-2", "第二答"), "2026-07-15T08:02:02.000Z"),
        timedEnvelope(8, replyEnd("reply-2"), "2026-07-15T08:02:03.000Z"),
      ],
      toolRuns: [],
      lastSequence: 8,
    });

    expect(restored.messages.map((message) => message.id)).toEqual([
      "user-1",
      "reply-1",
      "user-2",
      "reply-2",
    ]);
    expect(restored.messages.map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
  });

  it("creates a second assistant message for a wakeup reply after reply end", () => {
    const state = createAgentConversation();

    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(state, envelope(2, replyEnd("reply-1")));
    applyPublicEvent(state, envelope(3, replyStart("reply-wakeup")));
    applyPublicEvent(state, envelope(4, textStart("reply-wakeup", "block-2")));
    applyPublicEvent(state, envelope(5, textDelta("reply-wakeup", "block-2", "后台完成")));

    expect(state.messages.map((message) => message.id)).toEqual(["reply-1", "reply-wakeup"]);
    expect(state.messages[1]).toMatchObject({
      name: "DataPilot",
      content: [{ type: "text", text: "后台完成" }],
    });
    expect(state.currentReplyId).toBe("reply-wakeup");
    expect(state.phase).toBe("streaming");
  });

  it.each(["success", "failure", "stopped"] as const)(
    "projects a %s custom terminal event into the public tool ledger",
    (status) => {
      const state = createAgentConversation();
      state.toolRuns["call-1"] = toolRun({ status: "running" });

      applyPublicEvent(
        state,
        envelope(
          1,
          custom("datapilot_tool_terminal", {
            tool_call_id: "call-1",
            status,
            summary: `${status} summary`,
            error_type: status === "failure" ? "tool_failed" : null,
          }),
        ),
      );

      expect(state.toolRuns["call-1"]).toMatchObject({
        tool_call_id: "call-1",
        tool_name: "extract",
        status,
        summary: `${status} summary`,
      });
      if (status === "failure") {
        expect(state.toolRuns["call-1"].error_type).toBe("tool_failed");
      }
    },
  );

  it("restores user messages as SDK UserMsg values without trusting persisted names", () => {
    const messages: ChatMessageRecord[] = [
      {
        id: "message-1",
        session_id: "session-1",
        role: "user",
        content: "继续处理",
        created_at: CREATED_AT,
      },
    ];

    const restored = restoreAgentConversation({
      messages,
      events: [],
      toolRuns: [toolRun()],
      lastSequence: 7,
    });

    expect(restored.messages[0]).toMatchObject({
      id: "message-1",
      name: "You",
      role: "user",
      content: [{ type: "text", text: "继续处理" }],
    });
    expect(restored.toolRuns["call-1"]).toEqual(toolRun());
    expect(restored.lastSequence).toBe(7);
  });

  it("cleans up interrupt state when the native reply ends", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    markConversationInterrupting(state);

    expect(state.phase).toBe("interrupting");
    applyPublicEvent(state, envelope(2, replyEnd("reply-1")));

    expect(state.phase).toBe("idle");
    expect(state.currentReplyId).toBeNull();
  });

  it("projects only recognized DataPilot custom state", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, custom("datapilot_progress", { text: "do not render" })));

    expect(state.messages).toEqual([]);
    expect(state.toolRuns).toEqual({});
    expect(state.pendingHumanDecision).toBeNull();
    expect(state.lastSequence).toBe(1);
  });

  it("normalizes missing or private reply session ids to the public envelope session", () => {
    const withoutSession = {
      ...replyStart("reply-1"),
      session_id: undefined,
    } as unknown as AgentEvent;
    const withPrivateSession = {
      ...replyEnd("reply-1"),
      session_id: "private-agentscope-session",
    } as AgentEvent;

    expect(normalizePublicAgentEvent(envelope(1, withoutSession))).toMatchObject({
      type: EventType.REPLY_START,
      session_id: "session-1",
    });
    expect(normalizePublicAgentEvent(envelope(2, withPrivateSession))).toMatchObject({
      type: EventType.REPLY_END,
      session_id: "session-1",
    });
    expect((withPrivateSession as { session_id: string }).session_id).toBe(
      "private-agentscope-session",
    );
  });

  it("keeps native external execution reduction and derives DataPilot HITL state", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(
      state,
      envelope(2, {
        id: "call-start",
        created_at: CREATED_AT,
        type: EventType.TOOL_CALL_START,
        reply_id: "reply-1",
        tool_call_id: "decision-1",
        tool_call_name: "request_human_decision",
      }),
    );
    applyPublicEvent(
      state,
      envelope(3, {
        id: "external",
        created_at: CREATED_AT,
        type: EventType.REQUIRE_EXTERNAL_EXECUTION,
        reply_id: "reply-1",
        tool_calls: [
          {
            type: "tool_call",
            id: "decision-1",
            name: "request_human_decision",
            input: JSON.stringify({
              request_id: "request-1",
              decision_type: "camera_params",
              summary: "请确认参数",
              plan_id: "plan-1",
              step_id: "step-1",
            }),
            state: "pending",
          },
        ],
      }),
    );

    expect(state.messages[0].content[0]).toMatchObject({
      type: "tool_call",
      id: "decision-1",
      state: "submitted",
    });
    expect(state.pendingHumanDecision).toEqual({
      replyId: "reply-1",
      toolCallId: "decision-1",
      requestId: "request-1",
      decisionType: "camera_params",
      summary: "请确认参数",
      planId: "plan-1",
      stepId: "step-1",
    });
  });

  it("does not open DataPilot HITL for unrelated external tool execution", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(
      state,
      envelope(2, {
        id: "external",
        created_at: CREATED_AT,
        type: EventType.REQUIRE_EXTERNAL_EXECUTION,
        reply_id: "reply-1",
        tool_calls: [
          {
            type: "tool_call",
            id: "external-1",
            name: "unrelated_external_tool",
            input: "{}",
            state: "pending",
          },
        ],
      }),
    );

    expect(state.pendingHumanDecision).toBeNull();
  });
});

describe("DataPilot conversation store", () => {
  it("does not mutate a previous SDK message snapshot while reducing nested data blocks", () => {
    const store = createDataPilotStore();
    store.getState().applyEvent(envelope(1, replyStart("reply-1")));
    store.getState().applyEvent(
      envelope(2, {
        id: "data-start",
        created_at: CREATED_AT,
        type: EventType.DATA_BLOCK_START,
        reply_id: "reply-1",
        block_id: "data-1",
        media_type: "application/octet-stream",
      }),
    );
    const previous = store.getState().conversation;

    store.getState().applyEvent(
      envelope(3, {
        id: "data-delta",
        created_at: CREATED_AT,
        type: EventType.DATA_BLOCK_DELTA,
        reply_id: "reply-1",
        block_id: "data-1",
        media_type: "application/octet-stream",
        data: "AQ==",
      }),
    );

    expect(previous.messages[0].content[0]).toMatchObject({
      type: "data",
      source: { data: "" },
    });
    expect(store.getState().conversation.messages[0].content[0]).toMatchObject({
      type: "data",
      source: { data: "AQ==" },
    });
  });
});

function toolRun(overrides: Partial<PublicToolRun> = {}): PublicToolRun {
  return {
    session_id: "session-1",
    tool_call_id: "call-1",
    tool_name: "extract",
    status: "running",
    summary: "",
    error_type: null,
    started_at: CREATED_AT,
    finished_at: null,
    ...overrides,
  };
}

function userRecord(id: string, content: string, createdAt: string): ChatMessageRecord {
  return {
    id,
    session_id: "session-1",
    role: "user",
    content,
    created_at: createdAt,
  };
}

function timedEnvelope(
  sequence: number,
  event: AgentEvent,
  createdAt: string,
): PublicEventEnvelope {
  return {
    ...envelope(sequence, { ...event, created_at: createdAt }),
    created_at: createdAt,
  };
}
