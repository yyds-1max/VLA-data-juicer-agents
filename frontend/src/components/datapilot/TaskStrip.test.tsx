import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { TaskSnapshot } from "../../api/types";
import { TaskStrip } from "./TaskStrip";

describe("TaskStrip", () => {
  test("shows a compact phase capsule with full safe details in its hover panel", () => {
    vi.setSystemTime(new Date("2026-07-20T08:00:10Z"));
    const task: TaskSnapshot = {
      task_ref: "nav-A7K2",
      domain: "navigation",
      dataset_date: "20270605",
      selection: { kind: "all_clips" },
      scene_mode: null,
      status: "active",
      phase: "同步进度 50%",
      state_revision: 4,
      started_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T08:00:09Z",
      count: { done: 3, total: 8, unit: "个数据段" },
    };
    const { container } = render(<TaskStrip tasks={[task]} />);

    expect(screen.getByLabelText("导航任务 nav-A7K2，同步进度，处理中")).toBeVisible();
    expect(screen.getByRole("tooltip")).toHaveTextContent("导航任务 nav-A7K2");
    expect(screen.getByRole("tooltip")).toHaveTextContent("20270605 · 全部 clips");
    expect(screen.getByRole("tooltip")).toHaveTextContent("10s");
    expect(screen.getByRole("tooltip")).toHaveTextContent("3/8 个数据段");
    expect(container.querySelector("[tabindex='0']")).toHaveClass(
      "w-max",
      "transition-[width,border-color,box-shadow]",
      "duration-300",
    );
    expect(screen.getByRole("tooltip")).toHaveClass("bg-console-panel");
    expect(screen.getByRole("tooltip")).not.toHaveClass("backdrop-blur-xs");
    expect(container).not.toHaveTextContent(/[%％]/);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /继续|恢复|Resume/i })).not.toBeInTheDocument();
  });

  test("freezes elapsed time while waiting for the user", () => {
    vi.setSystemTime(new Date("2026-07-20T08:01:00Z"));
    const task: TaskSnapshot = {
      task_ref: "DP-WAITING",
      domain: "navigation",
      dataset_date: "20270605",
      selection: { kind: "selected_clips", clips: ["20260605_152856"] },
      scene_mode: null,
      status: "waiting_user",
      phase: "等待确认",
      latest_public_update: "等待你决定是否继续后处理。",
      state_revision: 8,
      started_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T08:00:05Z",
    };

    render(<TaskStrip tasks={[task]} />);

    expect(screen.getByLabelText("导航任务 DP-WAITING，等待确认，等待你的选择")).toBeVisible();
    expect(screen.getByRole("tooltip")).toHaveTextContent("5s");
    expect(screen.getByRole("tooltip")).toHaveTextContent("20260605_152856");
    expect(screen.getByRole("tooltip")).toHaveTextContent("等待你决定是否继续后处理。");
  });

  test("renders a completed task as a stopped terminal capsule", () => {
    const task: TaskSnapshot = {
      task_ref: "DP-DONE",
      domain: "navigation",
      dataset_date: "20270605",
      selection: { kind: "all_clips" },
      scene_mode: null,
      status: "completed",
      phase: "已完成",
      latest_public_update: "已按你的选择完成当前任务。",
      state_revision: 10,
      started_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T08:00:12Z",
    };
    const { container } = render(<TaskStrip tasks={[task]} />);

    expect(screen.getByLabelText("导航任务 DP-DONE，已完成，已完成")).toBeVisible();
    expect(screen.getByRole("tooltip")).toHaveTextContent("12s");
    expect(container.innerHTML).not.toContain("animate-spin");
  });

  test("adapts its width to changing content and keeps a width transition", () => {
    const width = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get")
      .mockImplementation(function measuredWidth(this: HTMLElement) {
        return this.textContent?.includes("正在提取并同步导航数据") ? 312 : 184;
      });
    const bounds = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function measuredBounds(this: HTMLElement) {
        return {
          width: this.tagName === "ASIDE" ? 480 : 0,
          height: 0,
          x: 0,
          y: 0,
          top: 0,
          right: 0,
          bottom: 0,
          left: 0,
          toJSON: () => ({}),
        };
      });
    const task: TaskSnapshot = {
      task_ref: "DP-RESIZE",
      domain: "navigation",
      dataset_date: "20270605",
      selection: { kind: "all_clips" },
      scene_mode: null,
      status: "active",
      phase: "检查数据",
      state_revision: 1,
      started_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T08:00:01Z",
    };
    const { rerender } = render(<TaskStrip tasks={[task]} />);

    const initial = screen.getByLabelText("导航任务 DP-RESIZE，检查数据，处理中");
    expect(initial).toHaveStyle({ width: "184px" });
    expect(initial).toHaveClass("transition-[width,border-color,box-shadow]");

    rerender(
      <TaskStrip
        tasks={[{ ...task, phase: "正在提取并同步导航数据", state_revision: 2 }]}
      />,
    );

    expect(
      screen.getByLabelText("导航任务 DP-RESIZE，正在提取并同步导航数据，处理中"),
    ).toHaveStyle({ width: "240px" });
    width.mockRestore();
    bounds.mockRestore();
  });
});
