import "@testing-library/jest-dom/vitest";
import { render, screen, within } from "@testing-library/react";

import { MiniChart } from "./MiniChart";

function donutSegments(title: string) {
  return within(screen.getByRole("img", { name: title })).queryAllByTestId("donut-segment");
}

test("donut chart renders empty and zero totals without invalid segment paths", () => {
  const { rerender } = render(<MiniChart type="donut" title="Empty donut" data={[]} />);
  const emptySvg = screen.getByRole("img", { name: "Empty donut" });

  expect(emptySvg).toBeVisible();
  expect(within(emptySvg).getByText("0")).toBeVisible();
  expect(within(emptySvg).getByText("总数")).toBeVisible();
  expect(donutSegments("Empty donut")).toHaveLength(0);

  rerender(
    <MiniChart
      type="donut"
      title="Zero donut"
      data={[
        { label: "图像数据", value: 0, color: "#15d1d8" },
        { label: "点云数据", value: 0, color: "#34d399" },
      ]}
    />,
  );

  const zeroSvg = screen.getByRole("img", { name: "Zero donut" });

  expect(zeroSvg).toBeVisible();
  expect(within(zeroSvg).getByText("0")).toBeVisible();
  expect(donutSegments("Zero donut")).toHaveLength(0);
  expect(zeroSvg.innerHTML).not.toContain("NaN");
  expect(zeroSvg.innerHTML).not.toContain("Infinity");
  expect(screen.getByText("图像数据")).toBeVisible();
  expect(screen.getByText("点云数据")).toBeVisible();
  expect(screen.queryByText("0%")).not.toBeInTheDocument();
});

test("donut chart renders a full slice as a visible circle instead of a coincident arc", () => {
  render(<MiniChart type="donut" title="Full donut" data={[{ label: "多模态数据", value: 100, color: "#a78bfa" }]} />);

  const svg = screen.getByRole("img", { name: "Full donut" });
  const segments = donutSegments("Full donut");

  expect(segments).toHaveLength(1);
  expect(segments[0].tagName.toLowerCase()).toBe("circle");
  expect(segments[0]).toHaveAttribute("stroke", "#a78bfa");
  expect(svg.innerHTML).not.toContain("NaN");
  expect(svg.innerHTML).not.toContain("Infinity");
  expect(within(svg).getByText("100")).toBeVisible();
  expect(screen.queryByText("100%")).not.toBeInTheDocument();
});

test("donut chart displays raw counts while using counts for segment proportions", () => {
  render(
    <MiniChart
      type="donut"
      title="Count donut"
      data={[
        { label: "同步图像帧", value: 40604, color: "#2d6cdf" },
        { label: "同步点云帧", value: 36998, color: "#16845b" },
      ]}
    />,
  );

  const svg = screen.getByRole("img", { name: "Count donut" });

  expect(within(svg).getByText("77,602")).toBeVisible();
  expect(screen.getByText("40,604")).toBeVisible();
  expect(screen.getByText("36,998")).toBeVisible();
  expect(screen.queryByText("40604%")).not.toBeInTheDocument();
});

test("donut chart animates displayed counts and sweeps segment angles by progress", () => {
  const { rerender } = render(
    <MiniChart
      type="donut"
      title="Animated donut"
      progress={0}
      data={[
        { label: "同步图像帧", value: 100, color: "#2d6cdf" },
        { label: "同步点云帧", value: 300, color: "#16845b" },
      ]}
    />,
  );

  expect(donutSegments("Animated donut")).toHaveLength(0);
  expect(screen.getAllByText("1")).toHaveLength(3);

  rerender(
    <MiniChart
      type="donut"
      title="Animated donut"
      progress={0.5}
      data={[
        { label: "同步图像帧", value: 100, color: "#2d6cdf" },
        { label: "同步点云帧", value: 300, color: "#16845b" },
      ]}
    />,
  );

  const segments = donutSegments("Animated donut");

  expect(within(screen.getByRole("img", { name: "Animated donut" })).getByText("200")).toBeVisible();
  expect(screen.getByText("50")).toBeVisible();
  expect(screen.getByText("150")).toBeVisible();
  expect(segments).toHaveLength(2);
  expect(segments[0].tagName.toLowerCase()).toBe("path");
  expect(segments[0]).toHaveAttribute("d", expect.stringContaining("A 58 58 0 0 0"));
  expect(segments[1]).toHaveAttribute("d", expect.stringContaining("A 58 58 0 0 0"));
});

test("radar chart renders rings axes labels and value polygon", () => {
  render(
    <MiniChart
      type="radar"
      title="版本性能对比"
      data={{
        labels: ["成功率", "稳定性", "泛化", "延迟"],
        data: [94, 89, 86, 78],
        label: "v47 候选",
        color: "#15d1d8",
      }}
    />,
  );

  const svg = screen.getByRole("img", { name: "版本性能对比" });

  expect(svg).toBeVisible();
  expect(within(svg).getByText("成功率")).toBeVisible();
  expect(within(svg).getByText("稳定性")).toBeVisible();
  expect(within(svg).getByTestId("radar-polygon")).toHaveAttribute("fill", "#15d1d8");
  expect(svg.innerHTML).not.toContain("NaN");
  expect(svg.innerHTML).not.toContain("Infinity");
});
