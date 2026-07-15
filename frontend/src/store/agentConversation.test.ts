import { EventType, type AgentEvent } from "@agentscope-ai/agentscope/event";

import type {
  ChatMessageRecord,
  PublicEventEnvelope,
  PublicToolRun,
} from "../api/types";
import {
  applyPublicEvent,
  createAgentConversation,
  hasActiveExecution,
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

function toolCallStart(replyId: string, toolCallId: string, toolName: string): AgentEvent {
  return {
    id: `call-start-${toolCallId}`,
    created_at: CREATED_AT,
    type: EventType.TOOL_CALL_START,
    reply_id: replyId,
    tool_call_id: toolCallId,
    tool_call_name: toolName,
  };
}

function validDecisionInput(): Record<string, unknown> {
  return {
    request_id: "request-1",
    decision_type: "camera_params",
    summary: "请确认参数",
  };
}

function requireDecision(
  replyId: string,
  toolCallId: string,
  input: Record<string, unknown>,
): AgentEvent {
  return {
    id: `external-${toolCallId}`,
    created_at: CREATED_AT,
    type: EventType.REQUIRE_EXTERNAL_EXECUTION,
    reply_id: replyId,
    tool_calls: [
      {
        type: "tool_call",
        id: toolCallId,
        name: "request_human_decision",
        input: JSON.stringify(input),
        state: "pending",
      },
    ],
  };
}

function externalResult(replyId: string, toolCallId: string, requestId: string): AgentEvent {
  return {
    id: `result-${replyId}-${toolCallId}-${requestId}`,
    created_at: CREATED_AT,
    type: EventType.EXTERNAL_EXECUTION_RESULT,
    reply_id: replyId,
    execution_results: [
      {
        type: "tool_result",
        id: toolCallId,
        name: "request_human_decision",
        output: JSON.stringify({ request_id: requestId }),
        state: "success",
      },
    ],
  };
}

function decisionResolved(value: Record<string, unknown>): AgentEvent {
  return custom("datapilot_human_decision_resolved", value);
}

function conversationAwaitingDecision() {
  const state = createAgentConversation();
  applyPublicEvent(state, envelope(1, replyStart("reply-1")));
  applyPublicEvent(
    state,
    envelope(2, toolCallStart("reply-1", "decision-1", "request_human_decision")),
  );
  applyPublicEvent(
    state,
    envelope(3, requireDecision("reply-1", "decision-1", validDecisionInput())),
  );
  return state;
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

  it("keeps the active reply when a different reply ends and advances sequence", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));

    applyPublicEvent(state, envelope(2, replyEnd("reply-other")));

    expect(state.currentReplyId).toBe("reply-1");
    expect(state.phase).toBe("streaming");
    expect(state.messages[0].finished_at).toBeUndefined();
    expect(state.lastSequence).toBe(2);
  });

  it("rejects a second reply start while a different reply is active", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));

    applyPublicEvent(state, envelope(2, replyStart("reply-2")));

    expect(state.messages.map((message) => message.id)).toEqual(["reply-1"]);
    expect(state.currentReplyId).toBe("reply-1");
    expect(state.lastSequence).toBe(2);
  });

  it("rejects tool and HITL projections owned by another reply", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(state, envelope(2, toolCallStart("reply-other", "call-other", "extract")));
    applyPublicEvent(
      state,
      envelope(3, requireDecision("reply-other", "decision-other", validDecisionInput())),
    );

    expect(state.messages[0].content).toEqual([]);
    expect(state.toolRuns).toEqual({});
    expect(state.pendingHumanDecision).toBeNull();
    expect(state.lastSequence).toBe(3);
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

  it("keeps one tool row when native and terminal IDs contain private identity text", () => {
    const privateIdentity = "internal-nav-session";
    const replyId = `reply-${privateIdentity}`;
    const toolCallId = `call-${privateIdentity}`;
    const restored = restoreAgentConversation({
      messages: [],
      events: [
        envelope(1, replyStart(replyId)),
        envelope(2, toolCallStart(replyId, toolCallId, "extract")),
        envelope(
          3,
          custom("datapilot_tool_terminal", {
            tool_call_id: toolCallId,
            status: "success",
            summary: "done",
            error_type: null,
          }),
        ),
        envelope(4, replyEnd(replyId)),
      ],
      toolRuns: [
        toolRun({
          tool_call_id: toolCallId,
          status: "success",
          summary: "done",
          finished_at: CREATED_AT,
        }),
      ],
      lastSequence: 4,
    });

    expect(Object.keys(restored.toolRuns)).toEqual([toolCallId]);
    expect(restored.toolRuns[toolCallId]).toMatchObject({
      tool_call_id: toolCallId,
      status: "success",
    });
    expect(restored.messages).toHaveLength(1);
    expect(restored.messages[0]).toMatchObject({ id: replyId });
  });

  it.each([
    { tool_call_id: "call-1", status: "running" },
    { tool_call_id: "call-1", status: "unknown" },
    { tool_call_id: "", status: "failure" },
  ])("rejects non-terminal or malformed tool terminal payload $status", (value) => {
    const state = createAgentConversation();
    state.toolRuns["call-1"] = toolRun({ status: "running" });

    applyPublicEvent(
      state,
      envelope(1, custom("datapilot_tool_terminal", { ...value, summary: "ignored" })),
    );

    expect(state.toolRuns).toEqual({ "call-1": toolRun({ status: "running" }) });
    expect(state.lastSequence).toBe(1);
  });

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

  it("ignores persisted assistant and system rows so native replies are not duplicated", () => {
    const restored = restoreAgentConversation({
      messages: [
        userRecord("user-1", "question", "2026-07-15T08:00:00.000Z"),
        {
          ...userRecord("assistant-copy", "native answer", "2026-07-15T08:00:03.000Z"),
          role: "assistant",
        },
        {
          ...userRecord("system-copy", "internal system", "2026-07-15T08:00:04.000Z"),
          role: "system",
        },
      ],
      events: [
        timedEnvelope(1, replyStart("reply-1"), "2026-07-15T08:00:01.000Z"),
        timedEnvelope(2, textStart("reply-1", "block-1"), "2026-07-15T08:00:01.100Z"),
        timedEnvelope(
          3,
          textDelta("reply-1", "block-1", "native answer"),
          "2026-07-15T08:00:02.000Z",
        ),
        timedEnvelope(4, replyEnd("reply-1"), "2026-07-15T08:00:02.100Z"),
      ],
      toolRuns: [],
      lastSequence: 4,
    });

    expect(restored.messages.map((message) => message.id)).toEqual(["user-1", "reply-1"]);
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

  it("consumes thinking event sequences without retaining private reasoning", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(
      state,
      envelope(2, {
        id: "thinking-start",
        created_at: CREATED_AT,
        type: "THINKING_BLOCK_START",
        reply_id: "reply-1",
        block_id: "thought-1",
      } as AgentEvent),
    );
    applyPublicEvent(
      state,
      envelope(3, {
        id: "thinking-delta",
        created_at: CREATED_AT,
        type: "THINKING_BLOCK_DELTA",
        reply_id: "reply-1",
        block_id: "thought-1",
        delta: "private chain of thought",
      } as AgentEvent),
    );

    expect(state.lastSequence).toBe(3);
    expect(JSON.stringify(state.messages)).not.toContain("private chain of thought");
    expect(state.messages[0].content).toEqual([]);
  });

  it("consumes ownerless thinking before accepting the next reply", () => {
    const state = createAgentConversation();
    applyPublicEvent(
      state,
      envelope(1, {
        id: "orphan-thinking",
        created_at: CREATED_AT,
        type: "THINKING_BLOCK_DELTA",
        reply_id: "stale-reply",
        block_id: "stale-thought",
        delta: "private stale reasoning",
      } as AgentEvent),
    );
    applyPublicEvent(state, envelope(2, replyStart("reply-2")));
    applyPublicEvent(state, envelope(3, textStart("reply-2", "block-2")));
    applyPublicEvent(state, envelope(4, textDelta("reply-2", "block-2", "visible")));

    expect(state.lastSequence).toBe(4);
    expect(state.currentReplyId).toBe("reply-2");
    expect(JSON.stringify(state.messages)).not.toContain("private stale reasoning");
    expect(JSON.stringify(state.messages)).toContain("visible");
  });

  it("treats a restored running tool as active execution while reply is idle", () => {
    const restored = restoreAgentConversation({
      messages: [],
      events: [],
      toolRuns: [toolRun({ tool_call_id: "call-background", status: "running" })],
      lastSequence: 0,
    });

    expect(restored.phase).toBe("idle");
    expect(hasActiveExecution(restored)).toBe(true);
    markConversationInterrupting(restored);
    expect(restored.phase).toBe("interrupting");
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

  it("clears matching pending HITL after native external execution result reduction", () => {
    const state = conversationAwaitingDecision();

    applyPublicEvent(
      state,
      envelope(4, externalResult("reply-1", "decision-1", "request-1")),
    );

    expect(state.pendingHumanDecision).toBeNull();
    expect(state.messages[0].content).toContainEqual(
      expect.objectContaining({ type: "tool_result", id: "decision-1" }),
    );
  });

  it("does not clear pending HITL for a wrong reply, tool, or request result", () => {
    const state = conversationAwaitingDecision();

    applyPublicEvent(
      state,
      envelope(4, externalResult("reply-other", "decision-1", "request-1")),
    );
    applyPublicEvent(
      state,
      envelope(5, externalResult("reply-1", "decision-other", "request-1")),
    );
    applyPublicEvent(
      state,
      envelope(6, externalResult("reply-1", "decision-1", "request-other")),
    );

    expect(state.pendingHumanDecision).toMatchObject({
      replyId: "reply-1",
      toolCallId: "decision-1",
      requestId: "request-1",
    });
    expect(state.lastSequence).toBe(6);
  });

  it("does not resurrect a HITL dialog when snapshot replay includes its result", () => {
    const restored = restoreAgentConversation({
      messages: [],
      events: [
        envelope(1, replyStart("reply-1")),
        envelope(2, toolCallStart("reply-1", "decision-1", "request_human_decision")),
        envelope(3, requireDecision("reply-1", "decision-1", validDecisionInput())),
        envelope(4, externalResult("reply-1", "decision-1", "request-1")),
      ],
      toolRuns: [],
      lastSequence: 4,
    });

    expect(restored.pendingHumanDecision).toBeNull();
  });

  it("clears matching HITL when replay contains the durable backend resolution event", () => {
    const restored = restoreAgentConversation({
      messages: [],
      events: [
        envelope(1, replyStart("reply-1")),
        envelope(2, toolCallStart("reply-1", "decision-1", "request_human_decision")),
        envelope(3, requireDecision("reply-1", "decision-1", validDecisionInput())),
        envelope(
          4,
          decisionResolved({ request_id: "request-1", reason: "submitted" }),
        ),
        envelope(5, replyEnd("reply-1")),
      ],
      toolRuns: [],
      lastSequence: 5,
    });

    expect(restored.pendingHumanDecision).toBeNull();
  });

  it("keeps parked HITL pending when a reply ends without a resolution event", () => {
    const restored = restoreAgentConversation({
      messages: [],
      events: [
        envelope(1, replyStart("reply-1")),
        envelope(2, toolCallStart("reply-1", "decision-1", "request_human_decision")),
        envelope(3, requireDecision("reply-1", "decision-1", validDecisionInput())),
        envelope(4, replyEnd("reply-1")),
      ],
      toolRuns: [],
      lastSequence: 4,
    });

    expect(restored.pendingHumanDecision).toMatchObject({
      requestId: "request-1",
      toolCallId: "decision-1",
    });
  });

  it("clears any pending HITL for a durable explicit-stop resolution", () => {
    const state = conversationAwaitingDecision();

    applyPublicEvent(
      state,
      envelope(4, decisionResolved({ all: true, reason: "stopped" })),
    );

    expect(state.pendingHumanDecision).toBeNull();
  });

  it.each([
    { request_id: "wrong-request", reason: "submitted" },
    { request_id: "request-1" },
    { all: true },
    { all: false, reason: "stopped" },
    { all: true, reason: "submitted" },
  ])("does not clear HITL for wrong or malformed durable resolution %#", (value) => {
    const state = conversationAwaitingDecision();

    applyPublicEvent(state, envelope(4, decisionResolved(value)));

    expect(state.pendingHumanDecision).toMatchObject({ requestId: "request-1" });
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

  it.each([
    { request_id: "", summary: "prompt" },
    { request_id: "request-1", summary: "" },
    { request_id: "request-1", summary: "prompt", decision_type: "" },
  ])("does not open native HITL for malformed payload %#", (input) => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));
    applyPublicEvent(
      state,
      envelope(2, requireDecision("reply-1", "decision-1", input)),
    );

    expect(state.pendingHumanDecision).toBeNull();
  });

  it("accepts a complete custom prompt/options HITL contract", () => {
    const state = createAgentConversation();
    applyPublicEvent(
      state,
      envelope(
        1,
        custom("datapilot_human_decision_required", {
          reply_id: "reply-1",
          tool_call_id: "decision-1",
          request_id: "request-1",
          decision_type: "choice",
          prompt: "Choose an action",
          options: ["confirm", "stop"],
        }),
      ),
    );

    expect(state.pendingHumanDecision).toMatchObject({
      replyId: "reply-1",
      toolCallId: "decision-1",
      requestId: "request-1",
      summary: "Choose an action",
      options: ["confirm", "stop"],
    });
  });

  it.each([
    { reply_id: "", tool_call_id: "decision-1", request_id: "request-1", prompt: "p", options: ["a"] },
    { reply_id: "reply-1", tool_call_id: "", request_id: "request-1", prompt: "p", options: ["a"] },
    { reply_id: "reply-1", tool_call_id: "decision-1", request_id: "", prompt: "p", options: ["a"] },
    { reply_id: "reply-1", tool_call_id: "decision-1", request_id: "request-1", prompt: "", options: ["a"] },
    { reply_id: "reply-1", tool_call_id: "decision-1", request_id: "request-1", prompt: "p", options: [] },
  ])("does not open custom HITL for malformed prompt/options %#", (value) => {
    const state = createAgentConversation();
    applyPublicEvent(
      state,
      envelope(1, custom("datapilot_human_decision_required", value)),
    );

    expect(state.pendingHumanDecision).toBeNull();
  });

  it("does not consume a sequence gap while a reply is active", () => {
    const state = createAgentConversation();
    applyPublicEvent(state, envelope(1, replyStart("reply-1")));

    applyPublicEvent(state, envelope(3, textDelta("reply-1", "block-1", "late")));

    expect(state.lastSequence).toBe(1);
    expect(state.messages[0].content).toEqual([]);
    expect(state.currentReplyId).toBe("reply-1");
  });

  it("does not consume an ownerless reply continuation even at the next sequence", () => {
    const state = createAgentConversation();

    applyPublicEvent(state, envelope(1, textStart("reply-1", "block-1")));

    expect(state.lastSequence).toBe(0);
    expect(state.messages).toEqual([]);
  });
});

describe("DataPilot conversation store", () => {
  it.each([
    {
      name: "draft mode",
      state: { open: true, mode: "draft_new_session" as const, currentSessionId: null },
      eventSessionId: "session-1",
    },
    {
      name: "closed active session",
      state: { open: false, mode: "active_session" as const, currentSessionId: "session-1" },
      eventSessionId: "session-1",
    },
    {
      name: "another active session",
      state: { open: true, mode: "active_session" as const, currentSessionId: "session-1" },
      eventSessionId: "session-other",
    },
  ])("ignores late events for $name", ({ state, eventSessionId }) => {
    const store = createDataPilotStore();
    store.setState(state);

    store.getState().applyEvent({
      ...envelope(1, replyStart("reply-1")),
      session_id: eventSessionId,
    });

    expect(store.getState().conversation).toEqual(createAgentConversation());
  });

  it("does not mutate a previous SDK message snapshot while reducing nested data blocks", () => {
    const store = createDataPilotStore();
    store.setState({
      open: true,
      mode: "active_session",
      currentSessionId: "session-1",
    });
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

  it("merges equal-sequence authoritative messages and tools while preserving optimistic rows and cleared HITL", () => {
    const store = createDataPilotStore();
    const decisionEvent = {
      ...envelope(
        5,
        custom("datapilot_human_decision_required", {
          reply_id: "reply-1",
          tool_call_id: "decision-1",
          request_id: "request-1",
          decision_type: "choice",
          prompt: "Choose",
          options: ["confirm", "stop"],
        }),
      ),
      created_at: "2026-07-15T08:00:05.000Z",
    };
    store.getState().restoreSession(
      sessionDetail({
        events: [decisionEvent],
        tool_runs: [
          toolRun({ status: "failure", summary: "failed", finished_at: CREATED_AT }),
          toolRun({ tool_call_id: "call-3", status: "running" }),
        ],
        last_sequence: 5,
      }),
    );
    const pending = store.getState().conversation.pendingHumanDecision!;
    store.getState().clearPendingHumanDecision(pending, "session-1");
    store.getState().appendUserMessage(
      userRecord("local-user", "local draft", "2026-07-15T08:00:06.000Z"),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [userRecord("server-user", "server copy", CREATED_AT)],
        events: [decisionEvent],
        tool_runs: [
          toolRun({ status: "running" }),
          toolRun({ tool_call_id: "call-2", status: "running" }),
          toolRun({
            tool_call_id: "call-3",
            status: "success",
            summary: "done",
            finished_at: CREATED_AT,
          }),
        ],
        last_sequence: 5,
      }),
    );

    const conversation = store.getState().conversation;
    expect(conversation.messages.map((message) => message.id)).toEqual([
      "local-user",
      "server-user",
    ]);
    expect(conversation.pendingHumanDecision).toBeNull();
    expect(conversation.toolRuns["call-1"]).toMatchObject({
      status: "failure",
      summary: "failed",
    });
    expect(conversation.toolRuns["call-2"]).toMatchObject({ status: "running" });
    expect(conversation.toolRuns["call-3"]).toMatchObject({ status: "success" });
    expect(conversation.lastSequence).toBe(5);
  });

  it("shows another tab's exact user message at the same event cursor", () => {
    const store = createDataPilotStore();
    store.getState().restoreSession(sessionDetail({ last_sequence: 0 }));

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [
          userRecord(
            "local-other-tab-message",
            "submitted elsewhere",
            "2026-07-15T08:00:06.000Z",
          ),
        ],
        last_sequence: 0,
      }),
    );

    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "local-other-tab-message",
    ]);
  });

  it("does not let an uncorrelated late run terminal clear a newly started reply", () => {
    const restored = restoreAgentConversation({
      messages: [],
      events: [
        envelope(1, {
          id: "reply-start",
          created_at: CREATED_AT,
          type: EventType.REPLY_START,
          session_id: "session-1",
            reply_id: "new-wakeup-reply",
          name: "DataPilot",
          role: "assistant",
        }),
        envelope(
          2,
          custom("datapilot_run_terminal", {
            turn_id: "turn-crashed",
            message_id: "local-crashed",
            status: "failure",
          }),
        ),
      ],
      toolRuns: [],
      lastSequence: 2,
    });

    expect(restored.currentReplyId).toBe("new-wakeup-reply");
    expect(restored.phase).toBe("streaming");
  });

  it("does not roll back conversation state from a lower-sequence snapshot", () => {
    const store = createDataPilotStore();
    store.getState().restoreSession(sessionDetail({ last_sequence: 5 }));
    store.getState().appendUserMessage(
      userRecord("local-user", "keep me", "2026-07-15T08:00:06.000Z"),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [userRecord("stale-user", "stale", CREATED_AT)],
        tool_runs: [toolRun({ status: "running" })],
        last_sequence: 4,
      }),
    );

    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "local-user",
    ]);
    expect(store.getState().conversation.lastSequence).toBe(5);
    expect(store.getState().conversation.toolRuns).toEqual({});
  });

  it("fully rebuilds the conversation when restoring a different session", () => {
    const store = createDataPilotStore();
    store.getState().restoreSession(sessionDetail({ last_sequence: 5 }));
    store.getState().appendUserMessage(userRecord("local-user", "old", CREATED_AT));

    store.getState().restoreSession(
      sessionDetail({
        id: "session-2",
        messages: [
          {
            ...userRecord("server-user", "new", CREATED_AT),
            session_id: "session-2",
          },
        ],
        last_sequence: 2,
      }),
    );

    expect(store.getState().currentSessionId).toBe("session-2");
    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "server-user",
    ]);
    expect(store.getState().conversation.lastSequence).toBe(2);
  });

  it("rebuilds a complete snapshot after live events expose a cursor gap", () => {
    const store = createDataPilotStore();
    store.setState({
      open: true,
      mode: "active_session",
      currentSessionId: "session-1",
    });

    store
      .getState()
      .applyEvent(envelope(2, textDelta("reply-1", "block-1", "gap delta")));
    expect(store.getState().conversation.lastSequence).toBe(0);

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [userRecord("user-1", "local request", CREATED_AT)],
        events: [
          timedEnvelope(1, replyStart("reply-1"), "2026-07-15T08:00:01.000Z"),
          timedEnvelope(2, textStart("reply-1", "block-1"), "2026-07-15T08:00:02.000Z"),
          timedEnvelope(
            3,
            textDelta("reply-1", "block-1", "complete reply"),
            "2026-07-15T08:00:03.000Z",
          ),
        ],
        last_sequence: 3,
      }),
    );

    expect(store.getState().conversation.lastSequence).toBe(3);
    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "user-1",
      "reply-1",
    ]);
    expect(store.getState().conversation.messages[1].content).toContainEqual(
      expect.objectContaining({ type: "text", text: "complete reply" }),
    );
  });

  it("preserves an unreconciled optimistic user across a higher-sequence snapshot", () => {
    const store = createDataPilotStore();
    store.getState().restoreSession(sessionDetail({ last_sequence: 0 }));
    store.getState().appendUserMessage(
      userRecord(
        "local-optimistic",
        "first draft request",
        "2026-07-15T08:00:01.000Z",
      ),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [],
        events: [
          timedEnvelope(1, replyStart("reply-fast"), "2026-07-15T08:00:02.000Z"),
        ],
        last_sequence: 1,
      }),
    );

    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "local-optimistic",
      "reply-fast",
    ]);
  });

  it("reconciles an optimistic user only with its exact authoritative message id", () => {
    const store = createDataPilotStore();
    store.getState().restoreSession(sessionDetail({ last_sequence: 0 }));
    store.getState().appendUserMessage(
      userRecord("local-exact", "same request", "2026-07-15T08:00:01.000Z"),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [
          userRecord("local-exact", "same request", "2026-07-15T08:00:02.000Z"),
        ],
        last_sequence: 1,
      }),
    );

    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "local-exact",
    ]);
  });

  it("does not reconcile another tab's same-text message with the local optimistic id", () => {
    const store = createDataPilotStore();
    store.getState().restoreSession(sessionDetail({ last_sequence: 0 }));
    store.getState().appendUserMessage(
      userRecord("local-this-tab", "identical text", "2026-07-15T08:00:01.000Z"),
    );

    store.getState().refreshActiveSession(
      sessionDetail({
        messages: [
          userRecord("local-other-tab", "identical text", "2026-07-15T08:00:02.000Z"),
        ],
        events: [
          timedEnvelope(1, replyStart("reply-other-tab"), "2026-07-15T08:00:03.000Z"),
        ],
        last_sequence: 1,
      }),
    );

    expect(store.getState().conversation.messages.map((message) => message.id)).toEqual([
      "local-this-tab",
      "local-other-tab",
      "reply-other-tab",
    ]);
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

function sessionDetail(
  overrides: Partial<import("../api/types").SessionDetail> = {},
): import("../api/types").SessionDetail {
  return {
    id: "session-1",
    title: "session",
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: 0,
    ...overrides,
  };
}
