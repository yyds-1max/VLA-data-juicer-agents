import "@testing-library/jest-dom/vitest";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { ChatMessageRecord, TurnRecord } from "../../api/types";
import { createEmptyRunState } from "../../store/eventReducer";
import { MessageList } from "./MessageList";

const sessionId = "session-1";

function turn(id: string, origin: TurnRecord["origin"] = "user"): TurnRecord {
  return {
    id,
    web_session_id: sessionId,
    origin,
    status: "running",
    started_at: `2026-07-20T00:00:0${id.endsWith("1") ? "1" : "2"}.000Z`,
    finished_at: null,
    final_message_id: null,
  };
}

function userMessage(id: string, turnId: string): ChatMessageRecord {
  return {
    id,
    session_id: sessionId,
    turn_id: turnId,
    role: "user",
    content: `request ${id}`,
    created_at: "2026-07-20T00:00:00.000Z",
  };
}

function assistantMessage(id: string, turnId: string, content: string): ChatMessageRecord {
  return {
    id,
    session_id: sessionId,
    turn_id: turnId,
    role: "assistant",
    content,
    created_at: "2026-07-20T00:00:04.000Z",
  };
}

function expectBefore(first: HTMLElement, second: HTMLElement) {
  expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

afterEach(() => {
  vi.useRealTimers();
});

describe("MessageList contract v1 processing", () => {
  test("renders at most one delayed empty placeholder while optimistic and server turns overlap", () => {
    vi.useFakeTimers();
    const run = createEmptyRunState();
    run.running = true;
    run.timeline = [
      { kind: "progress", text: "正在理解你的请求", turnId: "turn-1" },
      { kind: "progress", text: "正在理解你的请求", turnId: "turn-2" },
    ];

    render(
      <MessageList
        messages={[userMessage("message-1", "turn-1"), userMessage("message-2", "turn-2")]}
        turns={[turn("turn-1"), turn("turn-2")]}
        run={run}
      />,
    );

    expect(screen.queryByRole("button", { name: "正在处理" })).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(400));
    expect(screen.getAllByRole("button", { name: "正在处理" })).toHaveLength(1);
  });

  test("does not create an empty processing placeholder for a system turn", () => {
    vi.useFakeTimers();
    const run = createEmptyRunState();
    run.running = true;

    render(<MessageList messages={[]} turns={[turn("turn-2", "system")]} run={run} />);
    act(() => vi.advanceTimersByTime(500));

    expect(screen.queryByRole("button", { name: "正在处理" })).not.toBeInTheDocument();
  });

  test("shows natural progress and its semantic action as one ReAct disclosure", () => {
    const run = createEmptyRunState();
    run.running = true;
    run.timeline = [
      {
        kind: "progress",
        text: "已核对现有产物，接下来提取并同步导航数据。",
        turnId: "turn-1",
      },
      {
        kind: "action",
        text: "提取并同步导航数据",
        actionRef: "extract-sync",
        actionDisplayName: "提取并同步导航数据",
        actionStatus: "running",
        turnId: "turn-1",
      },
    ];

    render(
      <MessageList
        messages={[userMessage("message-1", "turn-1")]}
        turns={[turn("turn-1")]}
        run={run}
      />,
    );

    expect(screen.getAllByRole("button", { name: "正在处理" })).toHaveLength(1);
    expect(screen.getByText("已核对现有产物，接下来提取并同步导航数据。")).toBeVisible();
    expect(screen.getByText("正在提取并同步导航数据")).toBeVisible();
  });

  test("restores a durable workflow milestone inside its turn before later processing and final reply", () => {
    const run = createEmptyRunState();
    run.timeline = [
      {
        kind: "progress",
        text: "人工复核状态已更新，DataPilot 将继续处理当前任务。",
        turnId: null,
        progressPhase: "completed",
        createdAt: "2026-07-20T00:00:03.000Z",
        sequence: 7,
      },
      {
        kind: "progress",
        text: "正在核对人工复核结果。",
        turnId: "turn-1",
        createdAt: "2026-07-20T00:00:03.500Z",
        sequence: 8,
      },
    ];

    render(
      <MessageList
        messages={[
          userMessage("message-1", "turn-1"),
          assistantMessage("message-2", "turn-1", "轨迹复核已完成收口。"),
        ]}
        turns={[turn("turn-1")]}
        run={run}
      />,
    );

    const user = screen.getByText("request message-1");
    const milestone = screen.getByText(
      "人工复核状态已更新，DataPilot 将继续处理当前任务。",
    );
    const processing = screen.getByText("正在核对人工复核结果。");
    const finalReply = screen.getByText("轨迹复核已完成收口。");

    expect(screen.getByText("DataPilot · 状态更新")).toBeVisible();
    expectBefore(user, milestone);
    expectBefore(milestone, processing);
    expectBefore(processing, finalReply);
  });

  test("keeps a live workflow milestone ahead of processing details when the turn updates", () => {
    const run = createEmptyRunState();
    run.running = true;
    const messages = [userMessage("message-1", "turn-1")];
    const { rerender } = render(
      <MessageList messages={messages} turns={[turn("turn-1")]} run={run} />,
    );

    const updatedRun = createEmptyRunState();
    updatedRun.running = true;
    updatedRun.timeline = [
      {
        kind: "progress",
        text: "人工复核状态已更新，DataPilot 将继续处理当前任务。",
        turnId: null,
        createdAt: "2026-07-20T00:00:03.000Z",
        sequence: 9,
      },
      {
        kind: "action",
        text: "核对人工复核结果",
        actionRef: "review-result",
        actionDisplayName: "核对人工复核结果",
        actionStatus: "running",
        turnId: "turn-1",
        createdAt: "2026-07-20T00:00:03.500Z",
        sequence: 10,
      },
    ];

    rerender(<MessageList messages={messages} turns={[turn("turn-1")]} run={updatedRun} />);

    const milestone = screen.getByText(
      "人工复核状态已更新，DataPilot 将继续处理当前任务。",
    );
    const action = screen.getByText("正在核对人工复核结果");
    expectBefore(screen.getByText("request message-1"), milestone);
    expectBefore(milestone, action);
  });
});
