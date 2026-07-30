import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { PendingInteraction } from "../../api/types";
import { InteractionPanel } from "./InteractionPanel";

function interaction(overrides: Partial<PendingInteraction> = {}): PendingInteraction {
  return {
    interaction_id: "interaction-1",
    task_ref: "nav-A7K2",
    kind: "high_risk_confirmation",
    blocking: true,
    risk: "high",
    title: "确认高风险操作",
    summary: "必须使用明确按钮确认。",
    options: [
      { option_id: "confirm", label: "确认执行", tone: "danger" },
      { option_id: "reject", label: "拒绝" },
    ],
    interaction_revision: 1,
    expected_task_revision: 3,
    expires_at: null,
    ...overrides,
  };
}

describe("InteractionPanel", () => {
  test("requires an explicit button click and has no implicit form submission", () => {
    const onSubmit = vi.fn();
    render(<InteractionPanel interaction={interaction()} onSubmit={onSubmit} />);

    fireEvent.keyDown(screen.getByRole("heading", { name: "确认高风险操作" }), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onSubmit).toHaveBeenCalledWith(["confirm"]);
    expect(screen.queryByRole("button", { name: /关闭/ })).not.toBeInTheDocument();
  });

  test("renders the explanation and choices as separate floating surfaces", () => {
    const { container } = render(
      <InteractionPanel interaction={interaction()} onSubmit={vi.fn()} />,
    );

    const content = container.querySelector('[data-interaction-surface="content"]');
    const options = container.querySelector('[data-interaction-surface="options"]');
    expect(content).toBeVisible();
    expect(options).toBeVisible();
    expect(content?.contains(options)).toBe(false);
  });

  test("collects multi-select options before explicit submission", () => {
    const onSubmit = vi.fn();
    render(
      <InteractionPanel
        interaction={interaction({
          kind: "multi_select",
          risk: "low",
          options: [
            { option_id: "one", label: "数据段一" },
            { option_id: "two", label: "数据段二" },
          ],
        })}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "数据段一" }));
    fireEvent.click(screen.getByRole("button", { name: "数据段二" }));
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "提交选择" }));
    expect(onSubmit).toHaveBeenCalledWith(["one", "two"]);
  });

  test("requires a calibration radio choice before explicit confirmation", () => {
    const onSubmit = vi.fn();
    render(
      <InteractionPanel
        interaction={interaction({
          kind: "calibration_preview",
          title: "确认标定参数",
          options: [
            {
              option_id: "calibration_a",
              label: "20260320",
              description: "用于本次数据处理",
            },
            {
              option_id: "calibration_b",
              label: "20260529_go2w",
              description: "用于本次数据处理",
            },
            { option_id: "reject", label: "停止任务" },
          ],
        })}
        onSubmit={onSubmit}
      />,
    );

    const confirm = screen.getByRole("button", {
      name: "确认所选标定并继续",
    });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /20260529_go2w/ }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    expect(onSubmit).toHaveBeenCalledWith(["calibration_b"]);
  });
});
