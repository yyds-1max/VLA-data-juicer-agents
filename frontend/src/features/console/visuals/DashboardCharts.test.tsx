import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";

import {
  DataDistributionChart,
  DataDistributionSkeleton,
  ModelMetricsChart,
} from "./DashboardCharts";

const distribution = [
  { label: "同步图像帧", value: 42, color: "#274BC8" },
  { label: "同步点云帧", value: 28, color: "#536FD7" },
  { label: "同步里程计帧", value: 18, color: "#7C8FE3" },
  { label: "同步栅格图", value: 12, color: "#A7B3ED" },
];

describe("dashboard charts", () => {
  test("renders the real distribution values with the approved neighboring blue palette", () => {
    render(<DataDistributionChart data={distribution} />);

    expect(screen.getByRole("img", { name: "数据类型分布，总计 100" })).toBeVisible();
    expect(document.querySelector('[data-slot="distribution-donut-shell"]')).toHaveClass("size-32");
    expect(document.querySelector('[data-slot="distribution-panel-body"]')).toHaveClass("min-h-40");
    expect(document.querySelector('[data-slot="distribution-legend"]')).toHaveClass("gap-x-4");
    expect(screen.getByText("同步图像帧")).toBeVisible();
    expect(screen.getByText("42")).toBeVisible();

    const colors = screen.getAllByTestId("distribution-color").map((element) => element.style.backgroundColor);
    expect(colors).toEqual([
      "rgb(39, 75, 200)",
      "rgb(83, 111, 215)",
      "rgb(124, 143, 227)",
      "rgb(167, 179, 237)",
    ]);
  });

  test("renders an equal-size reduced-motion-safe distribution loading skeleton", () => {
    render(<DataDistributionSkeleton />);

    const loading = screen.getByRole("status", { name: "数据类型分布加载中" });
    expect(loading).toHaveClass("min-h-40");
    expect(document.querySelector('[data-slot="distribution-donut-skeleton"]')).toHaveClass("size-32");
    expect(screen.getAllByTestId("distribution-skeleton-row")).toHaveLength(4);
    for (const skeleton of loading.querySelectorAll('[data-slot="skeleton"]')) {
      expect(skeleton).toHaveClass("motion-reduce:animate-none");
    }
  });

  test("keeps a large total readable inside the enlarged donut", () => {
    render(
      <DataDistributionChart
        data={distribution.map((item, index) => ({
          ...item,
          value: index === 0 ? 1_234_567 : 0,
        }))}
      />,
    );

    expect(screen.getByRole("img", { name: "数据类型分布，总计 1,234,567" })).toBeVisible();
    expect(screen.getByTestId("distribution-total")).toHaveTextContent("1,234,567");
    expect(screen.getByTestId("distribution-total")).toHaveClass("text-sm");
  });

  test("counts values up while the donut sweeps into view", () => {
    render(<DataDistributionChart data={distribution} animationProgress={0.5} />);

    expect(screen.getByTestId("distribution-total")).toHaveTextContent("50");
    expect(screen.getByText("21")).toBeVisible();
    expect(document.querySelector('[data-slot="distribution-donut-shell"]')).toHaveAttribute(
      "data-animation-progress",
      "0.500",
    );
  });

  test("keeps an informative stable donut state when every distribution value is zero", () => {
    render(<DataDistributionChart data={distribution.map((item) => ({ ...item, value: 0 }))} />);

    expect(screen.getByRole("img", { name: "数据类型分布，总计 0，暂无同步帧数据" })).toBeVisible();
    expect(screen.getByText("总数")).toBeVisible();
    expect(screen.getAllByText("0")).toHaveLength(5);
  });

  test("renders an explicit empty state for missing chart data", () => {
    const { rerender } = render(<DataDistributionChart data={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent("暂无同步帧数据");

    rerender(<ModelMetricsChart data={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent("暂无训练指标数据");
  });

  test("exposes the model metric chart with both epoch series", () => {
    const { container } = render(
      <ModelMetricsChart
        data={[
          { epoch: 1, successRate: 42, loss: 0.72 },
          { epoch: 2, successRate: 55, loss: 0.54 },
        ]}
      />,
    );

    expect(screen.getByRole("img", { name: "VLA v47 按 Epoch 展示的成功率和损失值折线图" })).toBeVisible();
    expect(container.querySelectorAll("linearGradient")).toHaveLength(2);
    expect(container.querySelectorAll(".recharts-area-area")).toHaveLength(2);
  });
});
