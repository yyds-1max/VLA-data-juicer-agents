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
    events: [],
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
  it("streams public progress into one timeline paragraph by progress id", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "navigation-session", turn_id: "turn-1" };

    applyAgentEvent(state, event("progress_start", "agentscope", { progress_id: "progress-1" }, metadata));
    applyAgentEvent(
      state,
      event(
        "progress_delta",
        "agentscope",
        { progress_id: "progress-1", delta: "已确认原始数据，" },
        metadata,
      ),
    );
    applyAgentEvent(
      state,
      event(
        "progress_delta",
        "agentscope",
        { progress_id: "progress-1", delta: "接下来开始提取。" },
        metadata,
      ),
    );

    expect(state.timeline.filter((item) => item.kind === "progress")).toMatchObject([
      {
        text: "已确认原始数据，接下来开始提取。",
        progressId: "progress-1",
        progressPhase: "streaming",
        turnId: "turn-1",
        runId: "navigation-session",
      },
    ]);

    applyAgentEvent(state, event("progress_end", "agentscope", { progress_id: "progress-1" }, metadata));
    expect(state.timeline[0]).toMatchObject({
      text: "已确认原始数据，接下来开始提取。",
      progressPhase: "completed",
    });
  });

  it("ignores duplicate progress starts and accepts a recovered delta without start", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "navigation-session", turn_id: "turn-1" };

    applyAgentEvent(state, event("progress_start", "agentscope", { progress_id: "progress-1" }, metadata));
    applyAgentEvent(state, event("progress_start", "agentscope", { progress_id: "progress-1" }, metadata));
    applyAgentEvent(
      state,
      event(
        "progress_delta",
        "agentscope",
        { progress_id: "progress-2", delta: "恢复后的公开进展。" },
        metadata,
      ),
    );

    expect(state.timeline.filter((item) => item.kind === "progress")).toHaveLength(1);
    expect(state.timeline.at(-1)).toMatchObject({
      progressId: "progress-2",
      text: "恢复后的公开进展。",
    });
  });

  it("keeps progress terminal when end arrives before start or an older delta arrives late", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "navigation-session", turn_id: "turn-1" };

    applyAgentEvent(state, event("progress_end", "agentscope", { progress_id: "progress-1" }, metadata));
    applyAgentEvent(state, event("progress_start", "agentscope", { progress_id: "progress-1" }, metadata));
    applyAgentEvent(
      state,
      event(
        "progress_delta",
        "agentscope",
        { progress_id: "progress-1", delta: "第一段。" },
        metadata,
      ),
    );
    applyAgentEvent(
      state,
      event(
        "progress_delta",
        "agentscope",
        { progress_id: "progress-1", delta: "迟到的第二段。" },
        metadata,
      ),
    );

    expect(state.timeline).toMatchObject([
      {
        text: "第一段。迟到的第二段。",
        progressPhase: "completed",
      },
    ]);
  });

  it("does not let late progress reopen a completed turn", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "navigation-session", turn_id: "turn-1" };

    applyAgentEvent(state, event("turn_state", "agentscope", { status: "completed" }, metadata));
    applyAgentEvent(state, event("progress_start", "agentscope", { progress_id: "progress-1" }, metadata));
    applyAgentEvent(
      state,
      event(
        "progress_delta",
        "agentscope",
        { progress_id: "progress-1", delta: "较早的进展。" },
        metadata,
      ),
    );

    expect(state.running).toBe(false);
  });

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

  it("ignores duplicate pending human decision events with the same identity", () => {
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
    const firstDecision = state.pendingHumanDecision;

    applyAgentEvent(
      state,
      event(
        "human_decision_required",
        "navigation.workflow",
        {
          reply_id: "reply-1",
          tool_call_id: "tool-call-1",
          request_id: "request-1",
          summary: "请确认下一步。",
        },
        { timestamp: "2026-06-26T00:00:01.000Z" },
      ),
    );

    expect(state.pendingHumanDecision).toBe(firstDecision);
    expect(state.pendingHumanDecision?.summary).toBe("请确认下一步。");
  });

  it("replaces a non-equivalent normal event that fills plan ownership fields", () => {
    const state = createEmptyRunState();
    applyAgentEvent(
      state,
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "request-1",
        summary: "请确认。",
      }),
    );
    const first = state.pendingHumanDecision;

    applyAgentEvent(
      state,
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "request-1",
        plan_id: "plan-1",
        step_id: "confirm",
        summary: "请确认。",
      }),
    );

    expect(state.pendingHumanDecision).not.toBe(first);
    expect(state.pendingHumanDecision).toMatchObject({
      planId: "plan-1",
      stepId: "confirm",
    });
  });

  it("preserves plan-bound recovery fields from snake and camel case events", () => {
    const state = createEmptyRunState();

    applyAgentEvent(
      state,
      event("human_decision_required", "navigation.workflow", {
        reply_id: "reply-1",
        toolCallId: "tool-call-1",
        request_id: "request-1",
        plan_id: "plan-1",
        stepId: "confirm",
        recovery_required: true,
        submissionDisabled: true,
        recovery_endpoint: "/api/sessions/session-1/human-decisions/recovery",
        summary: "交付状态不明确，需要受控恢复。",
      }),
    );

    expect(state.pendingHumanDecision).toEqual({
      replyId: "reply-1",
      toolCallId: "tool-call-1",
      requestId: "request-1",
      decisionType: "other",
      summary: "交付状态不明确，需要受控恢复。",
      planId: "plan-1",
      stepId: "confirm",
      recoveryRequired: true,
      submissionDisabled: true,
      recoveryEndpoint: "/api/sessions/session-1/human-decisions/recovery",
    });
  });

  it("upgrades the same decision identity once and keeps exact duplicates stable", () => {
    const state = createEmptyRunState();
    const normal = event("human_decision_required", "navigation.workflow", {
      reply_id: "reply-1",
      tool_call_id: "tool-call-1",
      request_id: "request-1",
      plan_id: "plan-1",
      step_id: "confirm",
      summary: "请确认。",
    });
    const recovery = event("human_decision_required", "navigation.workflow", {
      ...normal.payload,
      recovery_required: true,
      submission_disabled: true,
      recovery_endpoint: "/api/sessions/session-1/human-decisions/recovery",
    });

    applyAgentEvent(state, normal);
    const first = state.pendingHumanDecision;
    applyAgentEvent(state, recovery);
    const upgraded = state.pendingHumanDecision;

    expect(upgraded).not.toBe(first);
    expect(upgraded?.recoveryRequired).toBe(true);
    applyAgentEvent(state, recovery);
    expect(state.pendingHumanDecision).toBe(upgraded);
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

  it("uses generic thinking placeholder for AgentScope router startup", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("agent_start", "agentscope"));

    expect(state.running).toBe(true);
    expect(state.activeText).toBe("正在思考");
  });

  it("merges activity snapshots and deltas into one ReAct timeline card", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("turn_pending", "main"));
    expect(state.activeText).toBe("正在思考");

    applyAgentEvent(
      state,
      event("activity_snapshot", "agentscope", {
        activity_id: "activity-1",
        title: "正在处理导航数据",
        status: "running",
        steps: [
          {
            id: "step-1",
            sequence: 1,
            status: "reasoning",
            observation: "发现已有处理方案。",
            analysis: "需要确认当前步骤。",
            action: "读取当前计划步骤。",
          },
        ],
      }),
    );

    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toMatchObject({
      kind: "activity",
      activityId: "activity-1",
      activityStatus: "running",
      activitySteps: [{ id: "step-1", status: "reasoning" }],
    });
    expect(state.running).toBe(true);
    expect(state.activeText).toBe("");

    applyAgentEvent(
      state,
      event("activity_delta", "agentscope", {
        activity_id: "activity-1",
        status: "running",
        step: {
          id: "step-1",
          sequence: 1,
          status: "completed",
          observation: "已确认当前计划步骤。",
          analysis: "需要确认当前步骤。",
          action: "读取当前计划步骤。",
        },
      }),
    );
    applyAgentEvent(
      state,
      event("activity_delta", "agentscope", {
        activity_id: "activity-1",
        status: "completed",
      }),
    );

    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toMatchObject({
      activityStatus: "completed",
      activitySteps: [{ id: "step-1", status: "completed", observation: "已确认当前计划步骤。" }],
    });
    expect(state.running).toBe(false);
  });

  it("keeps the exact public tool name visible while an activity is running", () => {
    const state = createEmptyRunState();
    applyAgentEvent(
      state,
      event("activity_snapshot", "agentscope", {
        activity_id: "activity-1",
        title: "正在处理导航数据",
        status: "running",
        steps: [],
      }),
    );
    applyAgentEvent(
      state,
      event("tool_start", "agentscope", {
        call_id: "call-1",
        tool: "get_current_plan_step_tool",
      }),
    );

    expect(state.activeText).toBe("正在调用工具 get_current_plan_step_tool");
    expect(state.activeTools["run-1\u0000call-1"]).toMatchObject({
      tool: "get_current_plan_step_tool",
    });
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
      text: "已调用工具 classify_navigation_dataset_tool 1.0s",
    });
    expect(state.timeline[0].text).not.toContain("20270605");
    expect(state.timeline[0].text).not.toContain("{");
  });

  it("never renders ok false as a completed tool even when status says completed", () => {
    const state = createEmptyRunState();
    applyAgentEvent(
      state,
      event("tool_start", "agentscope", {
        call_id: "call-failed",
        tool: "prepare_raw_data_tool",
      }),
    );
    applyAgentEvent(
      state,
      event("tool_end", "agentscope", {
        call_id: "call-failed",
        tool: "prepare_raw_data_tool",
        status: "completed",
        ok: false,
      }),
    );

    expect(state.timeline[0]).toMatchObject({
      kind: "tool",
      status: "failed",
      toolPhase: "failed",
    });
  });

  it("keeps a background tool active until its real terminal event", () => {
    const state = createEmptyRunState();
    applyAgentEvent(
      state,
      event("tool_start", "agentscope", {
        call_id: "call-1",
        tool: "extract_and_sync_navigation_data_tool",
      }),
    );
    applyAgentEvent(
      state,
      event("tool_background", "agentscope", {
        call_id: "call-1",
        tool: "extract_and_sync_navigation_data_tool",
        status: "background",
      }),
    );
    applyAgentEvent(
      state,
      event("final", "agentscope", { text: "任务已转入后台。" }),
    );

    expect(state.activeTools["run-1\u0000call-1"]).toMatchObject({
      tool: "extract_and_sync_navigation_data_tool",
      phase: "background",
    });
    expect(state.activeText).toBe("正在调用工具 extract_and_sync_navigation_data_tool");
    expect(state.timeline.filter((item) => item.kind === "tool")).toMatchObject([
      { tool: "extract_and_sync_navigation_data_tool", toolPhase: "background" },
    ]);

    applyAgentEvent(
      state,
      event(
        "tool_end",
        "agentscope",
        {
          call_id: "call-1",
          tool: "extract_and_sync_navigation_data_tool",
          status: "completed",
        },
        { timestamp: "2026-06-26T00:00:35.000Z" },
      ),
    );

    expect(state.activeTools).toEqual({});
    expect(state.timeline.find((item) => item.kind === "tool")).toMatchObject({
      status: "completed",
      text: "已调用工具 extract_and_sync_navigation_data_tool 35.0s",
    });
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

  it("final only clears the matching run and keeps child tools active", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("agent_start", "main", {}, { run_id: "main-run" }));
    applyAgentEvent(
      state,
      event("agent_start", "navigation.workflow", {}, { run_id: "workflow-run", parent_run_id: "main-run" }),
    );
    applyAgentEvent(
      state,
      event(
        "tool_start",
        "navigation.workflow",
        { call_id: "call-1", tool: "prepare_raw_data" },
        { run_id: "workflow-run", parent_run_id: "main-run" },
      ),
    );

    applyAgentEvent(state, event("final", "main", { text: "已启动导航任务。" }, { run_id: "main-run" }));

    expect(state.running).toBe(true);
    expect(state.activeText).toBe("正在调用工具 prepare_raw_data");
    expect(Object.values(state.activeTools)).toMatchObject([{ runId: "workflow-run", tool: "prepare_raw_data" }]);
    expect(state.activeAgents["workflow-run"]).toMatchObject({ runId: "workflow-run" });
    expect(state.activeAgents["main-run"]).toBeUndefined();
  });

  it("dedupes final events by run id", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("final", "main", { text: "first answer" }, { run_id: "final-run" }));
    applyAgentEvent(state, event("final", "main", { text: "duplicate answer" }, { run_id: "final-run" }));

    expect(state.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
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
    expect(state.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
      {
        kind: "assistant",
        source: "main",
        text: "你好，我是 DataPilot",
        runId: "stream-run",
        parentRunId: null,
      },
    ]);

    applyAgentEvent(state, event("final", "main", { text: "你好，我是 DataPilot。" }, { run_id: "stream-run" }));

    expect(state.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
      {
        kind: "assistant",
        source: "main",
        text: "你好，我是 DataPilot。",
        runId: "stream-run",
        parentRunId: null,
      },
    ]);
  });

  it("starts a new assistant item when a reused run id streams after final", () => {
    const state = createEmptyRunState();

    applyAgentEvent(state, event("assistant_delta", "agentscope", { delta: "第一轮" }, { run_id: "session-run" }));
    applyAgentEvent(state, event("final", "agentscope", { text: "第一轮" }, { run_id: "session-run" }));
    applyAgentEvent(state, event("assistant_delta", "agentscope", { delta: "第二轮" }, { run_id: "session-run" }));

    expect(state.timeline.filter((item) => item.kind === "assistant").map((item) => item.text)).toEqual([
      "第一轮",
      "第二轮",
    ]);

    applyAgentEvent(state, event("final", "agentscope", { text: "第二轮完成" }, { run_id: "session-run" }));

    expect(state.running).toBe(false);
    expect(state.timeline.filter((item) => item.kind === "assistant").map((item) => item.text)).toEqual([
      "第一轮",
      "第二轮完成",
    ]);
  });

  it("preserves answer delta whitespace and groups strictly by reply id", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "session-run", turn_id: "turn-1" };

    applyAgentEvent(state, event("answer_delta", "agentscope", { delta: "你好", reply_id: "reply-1" }, metadata));
    applyAgentEvent(state, event("answer_delta", "agentscope", { delta: " ", reply_id: "reply-1" }, metadata));
    applyAgentEvent(state, event("answer_delta", "agentscope", { delta: "world\n\n- item", reply_id: "reply-1" }, metadata));
    applyAgentEvent(state, event("answer_delta", "agentscope", { delta: "第二轮", reply_id: "reply-2" }, metadata));

    expect(state.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
      { text: "你好 world\n\n- item", replyId: "reply-1", turnId: "turn-1" },
      { text: "第二轮", replyId: "reply-2", turnId: "turn-1" },
    ]);

    applyAgentEvent(
      state,
      event(
        "final",
        "agentscope",
        { text: "第二轮完成", reply_id: "reply-2", message_id: "message-2" },
        metadata,
      ),
    );
    expect(state.timeline.filter((item) => item.kind === "assistant")[1]).toMatchObject({
      text: "第二轮完成",
      status: "final",
      replyId: "reply-2",
      finalMessageId: "message-2",
    });
  });

  it("retracts an intermediate answer when the reply becomes progress", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "session-run", turn_id: "turn-1" };

    applyAgentEvent(state, event("answer_delta", "agentscope", { delta: "临时回答", reply_id: "reply-1" }, metadata));
    applyAgentEvent(state, event("answer_reset", "agentscope", { reply_id: "reply-1" }, metadata));
    applyAgentEvent(state, event("progress_update", "agentscope", { text: "后台处理仍在继续。", reply_id: "reply-1" }, metadata));

    expect(state.timeline.some((item) => item.kind === "assistant")).toBe(false);
    expect(state.timeline.at(-1)).toMatchObject({
      kind: "progress",
      text: "后台处理仍在继续。",
      replyId: "reply-1",
    });
  });

  it("ignores a late delta after the same reply has reached final", () => {
    const state = createEmptyRunState();
    const metadata = { run_id: "router-session", turn_id: "turn-1" };

    applyAgentEvent(
      state,
      event(
        "final",
        "agentscope",
        { text: "权威最终回复", reply_id: "reply-1", message_id: "message-1" },
        metadata,
      ),
    );
    applyAgentEvent(
      state,
      event("answer_delta", "agentscope", { delta: "迟到的旧分片", reply_id: "reply-1" }, metadata),
    );

    expect(state.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
      {
        text: "权威最终回复",
        status: "final",
        replyId: "reply-1",
        finalMessageId: "message-1",
      },
    ]);
  });

  it("keeps identical reply ids isolated across AgentScope runs", () => {
    const state = createEmptyRunState();
    const router = { run_id: "router-session", turn_id: "turn-1" };
    const navigation = { run_id: "navigation-session", turn_id: "turn-1" };

    applyAgentEvent(
      state,
      event("answer_delta", "agentscope", { delta: "路由回复", reply_id: "reply-1" }, router),
    );
    applyAgentEvent(
      state,
      event("answer_delta", "agentscope", { delta: "导航回复", reply_id: "reply-1" }, navigation),
    );
    applyAgentEvent(
      state,
      event("answer_reset", "agentscope", { reply_id: "reply-1" }, router),
    );

    expect(state.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
      {
        text: "导航回复",
        runId: "navigation-session",
        replyId: "reply-1",
      },
    ]);
  });
});

describe("datapilotStore", () => {
  it("stamps replacement progress after retracting a draft in the same reducer event", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    store.getState().applyEvent(
      event(
        "answer_delta",
        "agentscope",
        { delta: "临时回答", reply_id: "reply-1" },
        {
          run_id: "as-session",
          turn_id: "turn-1",
          timestamp: "2026-06-26T00:01:00.000Z",
        },
      ),
    );
    store.getState().applyEvent(
      event(
        "progress_update",
        "agentscope",
        { text: "后台处理仍在继续。", reply_id: "reply-1" },
        {
          run_id: "as-session",
          turn_id: "turn-1",
          timestamp: "2026-06-26T00:02:00.000Z",
        },
      ),
    );

    const progress = store.getState().run.timeline.find((item) => item.kind === "progress");
    expect(progress).toMatchObject({
      text: "后台处理仍在继续。",
      createdAt: "2026-06-26T00:02:00.000Z",
    });
    expect(typeof progress?.sequence).toBe("number");
  });

  it("applies a persisted recovery upgrade even when timeline identity is unchanged", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    const baseEvent = {
      id: "event-1",
      session_id: "session-1",
      seq: 1,
      type: "human_decision_required",
      source: "NavigationDataAgent",
      run_id: "as-session",
      parent_run_id: null,
      timestamp: null,
      payload: {
        reply_id: "reply-1",
        tool_call_id: "tool-call-1",
        request_id: "plan-1:confirm",
        plan_id: "plan-1",
        step_id: "confirm",
        summary: "请确认。",
      },
      created_at: "2026-06-26T00:01:00Z",
    };
    store.getState().refreshActiveSession(sessionDetail({ events: [baseEvent] }));

    store.getState().refreshActiveSession(
      sessionDetail({
        events: [
          {
            ...baseEvent,
            payload: {
              ...baseEvent.payload,
              recovery_required: true,
              submission_disabled: true,
              recovery_endpoint: "/api/sessions/session-1/human-decisions/recovery",
            },
          },
        ],
      }),
    );

    expect(store.getState().run.pendingHumanDecision?.recoveryRequired).toBe(true);
  });

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

  it("does not drop repeated live stream events that have no persisted sequence", () => {
    const store = createDataPilotStore();
    const liveEvent = {
      type: "assistant_delta",
      source: "agentscope",
      run_id: "as-session",
      parent_run_id: null,
      timestamp: null,
      payload: { delta: "哈" },
    };

    store.getState().applyEvent(liveEvent);
    store.getState().applyEvent(liveEvent);

    expect(store.getState().run.timeline).toMatchObject([{ kind: "assistant", text: "哈哈" }]);
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

  it("refreshing an idle active session restores persisted timeline events", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().refreshActiveSession(
      sessionDetail({
        events: [
          {
            id: "event-1",
            session_id: "session-1",
            seq: 1,
            type: "assistant_delta",
            source: "navigation-data-agent",
            run_id: "as-session",
            parent_run_id: null,
            timestamp: "2026-06-26T00:01:00Z",
            payload: { delta: "开始检查。" },
            created_at: "2026-06-26T00:01:00Z",
          },
          {
            id: "event-2",
            session_id: "session-1",
            seq: 2,
            type: "tool_start",
            source: "navigation-data-agent",
            run_id: "as-session",
            parent_run_id: null,
            timestamp: "2026-06-26T00:01:01Z",
            payload: { tool: "prepare_raw_data", call_id: "call-1" },
            created_at: "2026-06-26T00:01:01Z",
          },
        ],
      }),
    );

    expect(store.getState().run.timeline).toMatchObject([
      { kind: "assistant", text: "开始检查。" },
      { kind: "tool", tool: "prepare_raw_data", toolPhase: "running" },
    ]);
    expect(store.getState().run.running).toBe(true);
    expect(store.getState().run.activeText).toBe("正在调用工具 prepare_raw_data");
  });

  it("restores repeated persisted deltas even when their content is identical", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().refreshActiveSession(
      sessionDetail({
        events: [
          {
            id: "event-1",
            session_id: "session-1",
            seq: 1,
            type: "assistant_delta",
            source: "agentscope",
            run_id: "as-session",
            parent_run_id: null,
            timestamp: null,
            payload: { delta: "哈" },
            created_at: "2026-06-26T00:01:00Z",
          },
          {
            id: "event-2",
            session_id: "session-1",
            seq: 2,
            type: "assistant_delta",
            source: "agentscope",
            run_id: "as-session",
            parent_run_id: null,
            timestamp: null,
            payload: { delta: "哈" },
            created_at: "2026-06-26T00:01:01Z",
          },
        ],
      }),
    );

    expect(store.getState().run.timeline).toMatchObject([{ kind: "assistant", text: "哈哈" }]);
  });

  it("refreshing an active session applies persisted events missing from the live stream without duplicating live events", () => {
    const store = createDataPilotStore();

    store.getState().setActiveSession(session());
    store.getState().applyEvent({
      type: "assistant_delta",
      source: "navigation-data-agent",
      run_id: "as-session",
      parent_run_id: null,
      timestamp: "2026-06-26T00:01:00Z",
      payload: { delta: "开始检查。" },
    });

    store.getState().refreshActiveSession(
      sessionDetail({
        events: [
          {
            id: "event-1",
            session_id: "session-1",
            seq: 1,
            type: "assistant_delta",
            source: "navigation-data-agent",
            run_id: "as-session",
            parent_run_id: null,
            timestamp: "2026-06-26T00:01:00Z",
            payload: { delta: "开始检查。" },
            created_at: "2026-06-26T00:01:00Z",
          },
          {
            id: "event-2",
            session_id: "session-1",
            seq: 2,
            type: "tool_start",
            source: "navigation-data-agent",
            run_id: "as-session",
            parent_run_id: null,
            timestamp: "2026-06-26T00:01:01Z",
            payload: { tool: "prepare_raw_data", call_id: "call-1" },
            created_at: "2026-06-26T00:01:01Z",
          },
        ],
      }),
    );

    expect(store.getState().run.timeline.filter((item) => item.kind === "assistant")).toMatchObject([
      { text: "开始检查。" },
    ]);
    expect(store.getState().run.running).toBe(true);
    expect(store.getState().run.activeText).toBe("正在调用工具 prepare_raw_data");
  });
});
