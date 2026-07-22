import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { TurnRecord } from "../../api/types";
import type { TimelineItem } from "../../store/eventReducer";
import { ProcessingDisclosure } from "./ProcessingDisclosure";

const startedAt = "2026-07-16T04:00:00.000Z";

function turn(status: TurnRecord["status"], finishedAt: string | null = null): TurnRecord {
  return {
    id: "turn-1",
    web_session_id: "session-1",
    origin: "user",
    status,
    started_at: startedAt,
    finished_at: finishedAt,
    final_message_id: null,
  };
}

describe("ProcessingDisclosure", () => {
  test("delays an empty v1 process for 400ms and suppresses a short direct answer", () => {
    vi.useFakeTimers();
    const items: TimelineItem[] = [
      { kind: "progress", text: "正在理解你的请求", turnId: "turn-1" },
    ];
    const { rerender } = render(
      <ProcessingDisclosure turn={turn("running")} items={items} allowEmptyPlaceholder />,
    );
    expect(screen.queryByRole("button", { name: "正在处理" })).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(399));
    expect(screen.queryByRole("button", { name: "正在处理" })).not.toBeInTheDocument();
    rerender(
      <ProcessingDisclosure
        turn={turn("completed", "2026-07-16T04:00:00.300Z")}
        items={items}
        allowEmptyPlaceholder
      />,
    );
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  test("shows v1 public actions immediately and never renders raw tools or percentages", () => {
    const items: TimelineItem[] = [
      { kind: "progress", text: "已完成 50% 的数据", turnId: "turn-1" },
      {
        kind: "action",
        text: "同步数据",
        actionRef: "action-1",
        actionDisplayName: "同步数据 75%",
        actionStatus: "running",
        turnId: "turn-1",
      },
    ];
    const { container } = render(
      <ProcessingDisclosure turn={turn("running")} items={items} />,
    );
    expect(screen.getByRole("button", { name: "正在处理" })).toBeVisible();
    expect(screen.getByText("已完成的数据")).toBeVisible();
    expect(screen.getByText("正在同步数据")).toBeVisible();
    expect(container).not.toHaveTextContent("internal_sync_tool");
    expect(container).not.toHaveTextContent(/[%％]/);
    expect(container.querySelector("[data-tool-call]")).toBeNull();
  });

  test("renders real streamed progress without a client-side typewriter", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-16T04:00:12.000Z"));
    const text = "已确认原始数据，接下来开始提取。";
    const items: TimelineItem[] = [
      {
        kind: "progress",
        text,
        progressId: "progress-1",
        progressPhase: "completed",
        turnId: "turn-1",
        createdAt: "2026-07-16T04:00:12.000Z",
      },
    ];

    const { container } = render(<ProcessingDisclosure turn={turn("running")} items={items} />);

    const paragraph = container.querySelector<HTMLElement>('[data-progress-id="progress-1"]')!;
    expect(paragraph).toHaveTextContent(text);
    expect(paragraph).not.toHaveClass("datapilot-progress-wave");
    vi.useRealTimers();
  });

  test("animates only the latest active progress copy", () => {
    const items: TimelineItem[] = [
      {
        kind: "progress",
        text: "已完成范围核对。",
        progressId: "progress-completed",
        progressPhase: "completed",
        turnId: "turn-1",
      },
      {
        kind: "progress",
        text: "正在核对数据范围。",
        progressId: "progress-active",
        progressPhase: "streaming",
        turnId: "turn-1",
      },
    ];

    const { container, rerender } = render(
      <ProcessingDisclosure turn={turn("running")} items={items} />,
    );
    const completed = container.querySelector<HTMLElement>('[data-progress-id="progress-completed"]')!;
    const activeProgress = container.querySelector<HTMLElement>('[data-progress-id="progress-active"]')!;

    expect(completed).not.toHaveClass("datapilot-progress-wave");
    expect(activeProgress).toHaveClass("datapilot-progress-wave");
    expect(activeProgress).toHaveAttribute("data-progress-active", "true");

    rerender(
      <ProcessingDisclosure
        turn={turn("completed", "2026-07-16T04:00:12.000Z")}
        items={items}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "已处理" }));
    expect(activeProgress).not.toHaveClass("datapilot-progress-wave");
  });

  test("animates the delayed understanding placeholder while the turn remains active", () => {
    vi.useFakeTimers();
    const items: TimelineItem[] = [
      { kind: "progress", text: "正在理解你的请求", turnId: "turn-1" },
    ];
    render(
      <ProcessingDisclosure turn={turn("running")} items={items} allowEmptyPlaceholder />,
    );

    act(() => vi.advanceTimersByTime(400));
    expect(screen.getByText("正在理解你的请求")).toHaveClass("datapilot-progress-wave");
    vi.useRealTimers();
  });

  test("shows natural progress and labels a public background action", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-16T04:00:12.000Z"));
    const items: TimelineItem[] = [
      { kind: "progress", text: "已确认原始数据，接下来开始提取。", turnId: "turn-1" },
      {
        kind: "action",
        text: "提取并同步导航数据",
        actionRef: "extract-sync",
        actionDisplayName: "提取并同步导航数据",
        actionStatus: "background",
        turnId: "turn-1",
      },
    ];

    render(<ProcessingDisclosure turn={turn("running")} items={items} />);

    expect(screen.getByRole("button", { name: "正在处理" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("已确认原始数据，接下来开始提取。")).toBeVisible();
    expect(screen.getByText("已转入后台提取并同步导航数据")).toBeVisible();
    expect(screen.queryByText(/观察|思考|行动/)).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  test("replaces the initial understanding placeholder after real progress arrives", () => {
    const items: TimelineItem[] = [
      { kind: "progress", text: "正在理解你的请求", turnId: "turn-1" },
      { kind: "progress", text: "已确认任务范围，准备执行。", turnId: "turn-1" },
    ];

    render(<ProcessingDisclosure turn={turn("waiting")} items={items} />);

    expect(screen.getByText("已确认任务范围，准备执行。")).toBeVisible();
    expect(screen.queryByText("正在理解你的请求")).not.toBeInTheDocument();
  });

  test("auto-collapses once when the turn completes and can be reopened", () => {
    const items: TimelineItem[] = [
      { kind: "progress", text: "正在检查产物。", turnId: "turn-1" },
    ];
    const { rerender } = render(<ProcessingDisclosure turn={turn("running")} items={items} />);
    expect(screen.getByText("正在检查产物。")).toBeVisible();

    rerender(
      <ProcessingDisclosure
        turn={turn("completed", "2026-07-16T04:00:35.500Z")}
        items={items}
      />,
    );
    const button = screen.getByRole("button", { name: "已处理" });
    expect(button).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(button);
    expect(screen.getByText("正在检查产物。")).toBeVisible();
    rerender(
      <ProcessingDisclosure
        turn={turn("completed", "2026-07-16T04:00:35.500Z")}
        items={[...items]}
      />,
    );
    expect(screen.getByText("正在检查产物。")).toBeVisible();
  });

  test("does not fold a foreground action just because a stale snapshot marks the turn terminal", () => {
    const items: TimelineItem[] = [{
      kind: "action",
      text: "提取并同步导航数据",
      actionRef: "extract-sync",
      actionDisplayName: "提取并同步导航数据",
      actionStatus: "running",
      turnId: "turn-1",
    }];
    render(<ProcessingDisclosure turn={turn("failed", "2026-07-16T04:00:12.000Z")} items={items} />);

    expect(screen.getByRole("button", { name: "正在处理" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("正在提取并同步导航数据")).toBeVisible();
  });

  test("does not reopen a running process after the user folds it", () => {
    const initial: TimelineItem[] = [
      { kind: "progress", text: "正在确认数据范围。", turnId: "turn-1" },
    ];
    const { rerender } = render(<ProcessingDisclosure turn={turn("running")} items={initial} />);
    const button = screen.getByRole("button", { name: /正在处理/ });
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");

    rerender(
      <ProcessingDisclosure
        turn={turn("running")}
        items={[
          ...initial,
          { kind: "progress", text: "准备调用处理工具。", turnId: "turn-1" },
        ]}
      />,
    );

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("准备调用处理工具。")).not.toBeVisible();
  });

  test("keeps progress intact after folding and reopening", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-16T04:00:12.000Z"));
    const text = "已确认数据范围，接下来生成处理方案。";
    const items: TimelineItem[] = [
      {
        kind: "progress",
        text,
        progressId: "progress-1",
        progressPhase: "completed",
        turnId: "turn-1",
        createdAt: "2026-07-16T04:00:12.000Z",
      },
    ];
    const { container } = render(
      <ProcessingDisclosure turn={turn("running")} items={items} />,
    );
    const button = screen.getByRole("button", { name: /正在处理/ });
    const paragraph = container.querySelector<HTMLElement>('[data-progress-id="progress-1"]')!;
    expect(paragraph).toHaveTextContent(text);

    fireEvent.click(button);
    fireEvent.click(button);

    expect(paragraph).toHaveTextContent(text);
    vi.useRealTimers();
  });
});
