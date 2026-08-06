import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";

import { ProcessTimeline, type ProcessTimelineStep } from "./ProcessTimeline";

describe("ProcessTimeline", () => {
  test("renders dynamic steps and advances the track through a waiting step", () => {
    const steps: ProcessTimelineStep[] = [
      { id: "one", label: "第一步", state: "completed" },
      { id: "two", label: "第二步", state: "completed" },
      { id: "three", label: "第三步", state: "waiting", statusLabel: "待复核" },
      { id: "four", label: "第四步", state: "pending" },
      { id: "five", label: "第五步", state: "pending" },
    ];

    render(
      <ProcessTimeline
        ariaLabel="测试流程"
        steps={steps}
        testIdPrefix="test-process"
      />,
    );

    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByRole("listitem", { name: "第三步，待复核" })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByTestId("test-process-progress")).toHaveStyle({ width: "50%" });
    expect(screen.getByRole("region", { name: "测试流程" })).toHaveAttribute("tabindex", "0");
  });

  test.each([
    ["error", "处理失败"],
    ["stopped", "已停止"],
  ] as const)("exposes the %s terminal state without advancing pending steps", (state, label) => {
    render(
      <ProcessTimeline
        ariaLabel={`${state} 流程`}
        steps={[
          { id: "one", label: "第一步", state: "completed" },
          { id: "two", label: "第二步", state, statusLabel: label },
          { id: "three", label: "第三步", state: "pending" },
        ]}
        testIdPrefix={state}
      />,
    );

    expect(screen.getByRole("listitem", { name: `第二步，${label}` })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByRole("listitem", { name: "第三步，未开始" })).toHaveAttribute(
      "data-process-state",
      "pending",
    );
    expect(screen.getByTestId(`${state}-progress`)).toHaveStyle({ width: "50%" });
  });

  test("renders a stable empty state", () => {
    render(<ProcessTimeline ariaLabel="空流程" steps={[]} />);

    expect(screen.getByRole("status")).toHaveTextContent("暂无流程信息");
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });
});
