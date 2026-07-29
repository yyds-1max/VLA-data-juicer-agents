import { describe, expect, it } from "vitest";

import type {
  AgentEvent,
  ChatMessageRecord,
  SessionDetail,
  SessionRecord,
  TimelineEventRecord,
} from "../api/types";
import { createDataPilotStore } from "./datapilotStore";
import { applyAgentEvent, createEmptyRunState } from "./eventReducer";

function event(
  type: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<AgentEvent> = {},
): AgentEvent {
  return {
    type,
    contract_version: 1,
    timestamp: "2026-06-26T00:00:00.000Z",
    turn_id: "turn-1",
    payload,
    ...overrides,
  };
}

function persistedEvent(
  id: string,
  seq: number,
  type: string,
  payload: Record<string, unknown>,
  overrides: Partial<TimelineEventRecord> = {},
): TimelineEventRecord {
  return {
    id,
    session_id: "session-1",
    seq,
    created_at: `2026-06-26T00:00:0${seq}.000Z`,
    ...event(type, payload, { timestamp: `2026-06-26T00:00:0${seq}.000Z` }),
    ...overrides,
  };
}

function session(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    id: "session-1",
    title: "DataPilot session",
    status: "active",
    contract_version: 1,
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    ...session(overrides),
    messages: [],
    events: [],
    turns: [],
    tasks: [],
    pending_interaction: null,
    ...overrides,
  };
}

function message(overrides: Partial<ChatMessageRecord> = {}): ChatMessageRecord {
  return {
    id: "message-1",
    session_id: "session-1",
    role: "user",
    content: "处理导航数据",
    created_at: "2026-06-26T00:00:00Z",
    ...overrides,
  };
}

describe("contract-v1 event reducer", () => {
  it("streams progress by public progress id", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("progress_delta", {
      progress_id: "progress-1",
      delta: "正在核对",
    }));
    applyAgentEvent(state, event("progress_delta", {
      progress_id: "progress-1",
      delta: "数据范围。",
    }));
    applyAgentEvent(state, event("progress_end", { progress_id: "progress-1" }));

    expect(state.timeline).toEqual([{
      kind: "progress",
      text: "正在核对数据范围。",
      turnId: "turn-1",
      progressId: "progress-1",
      progressPhase: "completed",
    }]);
  });

  it("deduplicates coalesced progress summaries and preserves terminal phase state", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("progress_end", { phase: "prepare" }));
    applyAgentEvent(state, event("progress_start", {
      phase: "prepare",
      summary: "已完成数据准备。",
    }));
    applyAgentEvent(state, event("progress_start", {
      phase: "prepare",
      summary: "已完成数据准备。",
    }));

    expect(state.timeline).toEqual([{
      kind: "progress",
      text: "已完成数据准备。",
      turnId: "turn-1",
      progressPhaseName: "prepare",
      progressPhase: "completed",
    }]);
  });

  it("updates a semantic action in place without internal tool state", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("action_start", {
      action_ref: "extract-sync",
      action_code: "navigation.extract_sync",
      phase_instance_id: "phase-1",
      display_name: "提取并同步导航数据",
    }));
    applyAgentEvent(state, event("action_end", {
      action_ref: "extract-sync",
      action_code: "navigation.extract_sync",
      phase_instance_id: "phase-1",
      display_name: "提取并同步导航数据",
      status: "completed",
      tool: "must_not_project",
      call_id: "must_not_project",
    }));

    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toEqual({
      kind: "action",
      text: "提取并同步导航数据",
      turnId: "turn-1",
      actionRef: "extract-sync",
      actionCode: "navigation.extract_sync",
      actionDisplayName: "提取并同步导航数据",
      actionPhase: "",
      actionPhaseInstanceId: "phase-1",
      actionStatus: "completed",
      status: "completed",
    });
    expect(state.timeline[0]).not.toHaveProperty("tool");
    expect(state.timeline[0]).not.toHaveProperty("callId");
  });

  it("represents a background transition using only the public action", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("action_start", {
      action_ref: "extract-sync",
      display_name: "提取并同步导航数据",
      status: "background",
    }));

    expect(state.timeline[0]).toMatchObject({
      kind: "action",
      actionStatus: "background",
      actionDisplayName: "提取并同步导航数据",
    });
  });

  it("tracks a public interaction lifecycle", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("interaction_required", {
      interaction_ref: "interaction-1",
      title: "确认标定参数",
      summary: "确认后继续。",
    }));
    expect(state.running).toBe(false);
    expect(state.timeline[0]).toMatchObject({
      kind: "interaction",
      interactionId: "interaction-1",
      status: "waiting",
      text: "确认后继续。",
    });

    applyAgentEvent(state, event("interaction_resolved", {
      interaction_ref: "interaction-1",
      result_label: "已确认",
    }));
    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toMatchObject({ status: "completed", text: "已确认" });
  });

  it("keeps answer streams separate by reply id within one turn", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("answer_delta", { reply_id: "reply-1", delta: "第一条" }));
    applyAgentEvent(state, event("answer_delta", { reply_id: "reply-2", delta: "第二条" }));
    applyAgentEvent(state, event("answer_delta", { reply_id: "reply-1", delta: "回复" }));

    expect(state.timeline).toMatchObject([
      { kind: "assistant", text: "第一条回复", replyId: "reply-1" },
      { kind: "assistant", text: "第二条", replyId: "reply-2" },
    ]);
  });

  it("retracts a draft and ignores a late delta after final", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("answer_delta", { reply_id: "reply-1", delta: "草稿" }));
    applyAgentEvent(state, event("answer_reset", { reply_id: "reply-1" }));
    expect(state.timeline).toEqual([]);

    applyAgentEvent(state, event("final", {
      reply_id: "reply-1",
      message_id: "message-final",
      text: "最终答复",
    }));
    applyAgentEvent(state, event("answer_delta", { reply_id: "reply-1", delta: "迟到内容" }));
    expect(state.timeline).toMatchObject([{
      kind: "assistant",
      text: "最终答复",
      status: "final",
      finalMessageId: "message-final",
    }]);
  });

  it("ignores every legacy AgentScope and raw tool event", () => {
    const state = createEmptyRunState();
    for (const type of [
      "agent_start",
      "agent_end",
      "reasoning",
      "activity_snapshot",
      "activity_delta",
      "tool_start",
      "tool_background",
      "tool_end",
      "assistant_delta",
      "human_decision_required",
      "progress_update",
    ]) {
      applyAgentEvent(state, event(type, {
        summary: "private",
        delta: "private",
        tool: "private_tool",
        call_id: "private-call",
      }));
    }
    expect(state).toEqual(createEmptyRunState());
  });

  it("keeps the two client-local optimistic lifecycle events", () => {
    const state = createEmptyRunState();
    applyAgentEvent(state, event("turn_pending"));
    expect(state.running).toBe(true);
    applyAgentEvent(state, event("turn_submission_failed"));
    expect(state.running).toBe(false);
  });
});

describe("contract-v1 DataPilot store", () => {
  const requestContext = {
    kind: "navigation_dataset_selection_v1" as const,
    dataset_date: "20270605",
    selection: { kind: "all_clips" as const },
  };

  it("atomically claims a shortcut invocation once and preserves its context on retry", () => {
    const store = createDataPilotStore();
    expect(store.getState().launchDataPilotRequest(
      "invocation-1",
      "处理导航数据",
      requestContext,
    )).toBe(true);
    expect(store.getState().claimDataPilotInvocation("invocation-1")).toBe(true);
    expect(store.getState().claimDataPilotInvocation("invocation-1")).toBe(false);
    store.getState().setDataPilotInvocationSession("invocation-1", "session-1");
    store.getState().failDataPilotInvocation("invocation-1", "提交失败");
    expect(store.getState().retryDataPilotInvocation("invocation-1")).toBe(true);
    expect(store.getState().pendingInvocation).toMatchObject({
      requestContext,
      sessionId: "session-1",
      status: "queued",
    });
  });

  it("adopts the server turn before a submit response can leave two placeholders", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    store.getState().appendUserMessage(message({
      id: "local-message-1",
      turn_id: "local-turn-1",
    }));
    store.getState().applyEvent(event("turn_start", { status: "running" }, {
      turn_id: "local-turn-1",
    }));
    store.getState().applyEvent(event("turn_start", { status: "running" }, {
      turn_id: "server-turn-1",
    }));

    expect(store.getState().turns.map((turn) => turn.id)).toEqual(["server-turn-1"]);
    expect(store.getState().messages[0].turn_id).toBe("server-turn-1");
    expect(store.getState().run.timeline).toEqual([{
      kind: "progress",
      text: "正在理解你的请求",
      turnId: "server-turn-1",
      createdAt: "2026-06-26T00:00:00.000Z",
      sequence: expect.any(Number),
    }]);
  });

  it("keeps repeated reconnect snapshots idempotent", () => {
    const store = createDataPilotStore();
    const events = [
      persistedEvent("event-progress", 1, "progress_start", {
        phase: "prepare",
        summary: "已核对数据范围。",
      }),
      persistedEvent("event-action", 2, "action_start", {
        action_ref: "extract-sync",
        phase_instance_id: "prepare-1",
        display_name: "提取并同步导航数据",
      }),
    ];
    store.getState().setActiveSession(session());
    store.getState().refreshActiveSession(detail({ events }));
    store.getState().refreshActiveSession(detail({ events }));

    expect(store.getState().run.timeline).toHaveLength(2);
    expect(store.getState().run.timeline).toMatchObject([
      { kind: "progress", text: "已核对数据范围。" },
      { kind: "action", actionRef: "extract-sync", actionStatus: "running" },
    ]);
  });

  it("preserves repeated persisted deltas with distinct event identities", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    store.getState().refreshActiveSession(detail({
      events: [
        persistedEvent("event-1", 1, "answer_delta", { reply_id: "reply-1", delta: "哈" }),
        persistedEvent("event-2", 2, "answer_delta", { reply_id: "reply-1", delta: "哈" }),
      ],
    }));
    expect(store.getState().run.timeline).toMatchObject([{
      kind: "assistant",
      text: "哈哈",
    }]);
  });

  it("deduplicates a live public event when its persisted copy arrives", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    const live = event("answer_delta", { reply_id: "reply-1", delta: "开始检查。" }, {
      timestamp: "2026-06-26T00:00:01.000Z",
    });
    store.getState().applyEvent(live);
    store.getState().refreshActiveSession(detail({
      events: [{
        ...live,
        id: "event-1",
        session_id: "session-1",
        seq: 1,
        created_at: "2026-06-26T00:00:01.000Z",
      }],
    }));
    expect(store.getState().run.timeline).toMatchObject([{
      kind: "assistant",
      text: "开始检查。",
    }]);
  });

  it("restores task and interaction snapshots and applies their public events", () => {
    const store = createDataPilotStore();
    store.getState().restoreActiveSession(detail({
      tasks: [{
        task_ref: "DP-A7K2",
        domain: "navigation",
        dataset_date: "20270605",
        selection: { kind: "all_clips" },
        scene_mode: null,
        status: "active",
        phase: "提取数据",
        state_revision: 2,
        started_at: "2026-06-26T00:00:00Z",
        updated_at: "2026-06-26T00:01:00Z",
      }],
      pending_interaction: null,
    }));
    store.getState().applyEvent(event("task_state_updated", {
      task_ref: "DP-A7K2",
      status: "waiting_user",
      phase: "确认标定参数",
      state_revision: 3,
      count: { done: 3, total: 8, unit: "个数据段" },
    }));
    store.getState().applyEvent(event("interaction_required", {
      interaction_ref: "interaction-1",
      task_ref: "DP-A7K2",
      kind: "calibration_preview",
      blocking: true,
      risk: "high",
      title: "确认标定参数",
      summary: "确认后继续。",
      options: [{ option_id: "confirm", label: "确认", destructive: true }],
      interaction_revision: 1,
      expected_task_revision: 3,
      expires_at: null,
    }));

    expect(store.getState().tasks[0]).toMatchObject({
      task_ref: "DP-A7K2",
      status: "waiting_user",
      count: { done: 3, total: 8, unit: "个数据段" },
    });
    expect(store.getState().pendingInteraction).toMatchObject({
      interaction_id: "interaction-1",
      kind: "calibration_preview",
      blocking: true,
    });
    store.getState().applyEvent(event("interaction_resolved", {
      interaction_ref: "interaction-1",
      result_label: "已确认",
    }));
    expect(store.getState().pendingInteraction).toBeNull();
  });

  it("merges persisted messages without dropping a newer local message", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    store.getState().appendUserMessage(message({
      id: "local-message-2",
      content: "本地新消息",
      created_at: "2026-06-26T00:02:00Z",
    }));
    store.getState().refreshActiveSession(detail({
      messages: [message({
        id: "persisted-message-1",
        role: "assistant",
        content: "已持久化回复",
        created_at: "2026-06-26T00:01:00Z",
      })],
    }));
    expect(store.getState().messages.map((item) => item.content)).toEqual([
      "已持久化回复",
      "本地新消息",
    ]);
  });

  it("does not let an older snapshot clear a newer live interaction", () => {
    const store = createDataPilotStore();
    store.getState().setActiveSession(session());
    store.getState().applyEvent(persistedEvent(
      "event-interaction",
      5,
      "interaction_required",
      {
        interaction_ref: "interaction-1",
        task_ref: "DP-A7K2",
        kind: "calibration_preview",
        blocking: true,
        risk: "high",
        title: "确认标定参数",
        summary: "确认后继续。",
        options: [{ option_id: "confirm", label: "确认" }],
        interaction_revision: 2,
        expected_task_revision: 4,
      },
    ));

    store.getState().refreshActiveSession(detail({
      snapshot_seq: 4,
      events: [persistedEvent("event-old", 4, "task_state_updated", {
        task_ref: "DP-A7K2",
        status: "active",
      })],
      pending_interaction: null,
    }));

    expect(store.getState().pendingInteraction).toMatchObject({
      interaction_id: "interaction-1",
      interaction_revision: 2,
    });
    expect(store.getState().lastEventSeq).toBe(5);
  });

  it("keeps the highest task revision when a stale snapshot arrives", () => {
    const store = createDataPilotStore();
    store.getState().restoreActiveSession(detail({
      tasks: [{
        task_ref: "DP-A7K2",
        domain: "navigation",
        dataset_date: "20270605",
        selection: { kind: "all_clips" },
        scene_mode: null,
        status: "waiting_user",
        phase: "等待首帧标注",
        state_revision: 8,
        started_at: "2026-06-26T00:00:00Z",
        updated_at: "2026-06-26T00:08:00Z",
      }],
    }));

    store.getState().refreshActiveSession(detail({
      tasks: [{
        task_ref: "DP-A7K2",
        domain: "navigation",
        dataset_date: "20270605",
        selection: { kind: "all_clips" },
        scene_mode: null,
        status: "active",
        phase: "准备数据",
        state_revision: 7,
        started_at: "2026-06-26T00:00:00Z",
        updated_at: "2026-06-26T00:07:00Z",
      }],
    }));

    expect(store.getState().tasks[0]).toMatchObject({
      status: "waiting_user",
      phase: "等待首帧标注",
      state_revision: 8,
    });
  });
});
