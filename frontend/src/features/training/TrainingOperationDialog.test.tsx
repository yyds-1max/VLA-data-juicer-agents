import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TrainingOperationDialog } from "./TrainingOperationDialog";

describe("TrainingOperationDialog", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps loading feedback modal and allows the user to close it", () => {
    const onOpenChange = vi.fn();
    render(
      <TrainingOperationDialog
        open
        operation={{ status: "loading", title: "正在卸载 Worker", detail: "正在连接训练节点。" }}
        onOpenChange={onOpenChange}
      />,
    );

    expect(screen.getByRole("dialog", { name: "操作进度" })).toHaveAttribute("aria-busy", "true");
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("automatically closes a successful operation after a short confirmation", async () => {
    vi.useFakeTimers();
    const onOpenChange = vi.fn();
    render(
      <TrainingOperationDialog
        open
        operation={{ status: "success", title: "Worker 已卸载", detail: "节点记录仍然保留。" }}
        onOpenChange={onOpenChange}
        autoCloseMs={1400}
      />,
    );

    expect(screen.getByText("窗口即将自动关闭")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(1399));
    expect(onOpenChange).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
