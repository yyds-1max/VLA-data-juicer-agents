import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
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
  test("shows natural progress and keeps background tools in the calling state", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-16T04:00:12.000Z"));
    const items: TimelineItem[] = [
      { kind: "progress", source: "main", text: "已确认原始数据，接下来开始提取。", turnId: "turn-1" },
      {
        kind: "tool",
        source: "agentscope",
        text: "extract_and_sync_navigation_data_tool",
        tool: "extract_and_sync_navigation_data_tool",
        callId: "call-1",
        toolPhase: "background",
        startedAt: Date.parse("2026-07-16T04:00:02.000Z"),
        turnId: "turn-1",
      },
    ];

    render(<ProcessingDisclosure turn={turn("running")} items={items} />);

    expect(screen.getByRole("button", { name: /正在处理 12s/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("已确认原始数据，接下来开始提取。")).toBeVisible();
    expect(screen.getByText("正在调用 extract_and_sync_navigation_data_tool +10s")).toBeVisible();
    expect(screen.queryByText(/后台执行/)).not.toBeInTheDocument();
    expect(screen.queryByText(/观察|思考|行动/)).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  test("replaces the initial understanding placeholder after real progress arrives", () => {
    const items: TimelineItem[] = [
      { kind: "progress", source: "main", text: "正在理解你的请求", turnId: "turn-1" },
      { kind: "progress", source: "main", text: "已确认任务范围，准备执行。", turnId: "turn-1" },
    ];

    render(<ProcessingDisclosure turn={turn("waiting")} items={items} />);

    expect(screen.getByText("已确认任务范围，准备执行。")).toBeVisible();
    expect(screen.queryByText("正在理解你的请求")).not.toBeInTheDocument();
  });

  test("auto-collapses once when the turn completes and can be reopened", () => {
    const items: TimelineItem[] = [
      { kind: "progress", source: "main", text: "正在检查产物。", turnId: "turn-1" },
    ];
    const { rerender } = render(<ProcessingDisclosure turn={turn("running")} items={items} />);
    expect(screen.getByText("正在检查产物。")).toBeVisible();

    rerender(
      <ProcessingDisclosure
        turn={turn("completed", "2026-07-16T04:00:35.500Z")}
        items={items}
      />,
    );
    const button = screen.getByRole("button", { name: /已处理 35s/ });
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

  test("does not reopen a running process after the user folds it", () => {
    const initial: TimelineItem[] = [
      { kind: "progress", source: "main", text: "正在确认数据范围。", turnId: "turn-1" },
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
          { kind: "progress", source: "main", text: "准备调用处理工具。", turnId: "turn-1" },
        ]}
      />,
    );

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("准备调用处理工具。")).not.toBeInTheDocument();
  });
});
