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
  expect(within(emptySvg).getByText("0%")).toBeVisible();
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
  expect(within(zeroSvg).getByText("0%")).toBeVisible();
  expect(donutSegments("Zero donut")).toHaveLength(0);
  expect(zeroSvg.innerHTML).not.toContain("NaN");
  expect(zeroSvg.innerHTML).not.toContain("Infinity");
  expect(screen.getByText("图像数据")).toBeVisible();
  expect(screen.getByText("点云数据")).toBeVisible();
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
});
