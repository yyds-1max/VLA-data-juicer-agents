import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { TaskSnapshot } from "../../api/types";
import { TaskStrip } from "./TaskStrip";

describe("TaskStrip", () => {
  test("shows public phase and reliable counts without percentages, progress bars, or resume controls", () => {
    vi.setSystemTime(new Date("2026-07-20T08:00:10Z"));
    const task: TaskSnapshot = {
      task_ref: "nav-A7K2",
      domain: "navigation",
      status: "active",
      phase: "同步进度 50%",
      state_revision: 4,
      started_at: "2026-07-20T08:00:00Z",
      updated_at: "2026-07-20T08:00:09Z",
      count: { done: 3, total: 8, unit: "个数据段" },
    };
    const { container } = render(<TaskStrip tasks={[task]} />);

    expect(screen.getByText("导航任务 nav-A7K2")).toBeVisible();
    expect(screen.getByText(/同步进度 · 10s · 3\/8 个数据段/)).toBeVisible();
    expect(container).not.toHaveTextContent(/[%％]/);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /继续|恢复|Resume/i })).not.toBeInTheDocument();
  });
});
