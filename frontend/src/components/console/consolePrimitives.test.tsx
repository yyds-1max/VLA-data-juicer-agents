import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { ConsoleButton } from "./ConsoleButton";
import { ProgressBar } from "./ProgressBar";
import { SegmentedTabs } from "./SegmentedTabs";
import { StatusTag } from "./StatusTag";

describe("console primitives", () => {
  test("ConsoleButton renders a real button and delegates clicks to the caller", () => {
    const onClick = vi.fn();

    render(<ConsoleButton onClick={onClick}>启动标注</ConsoleButton>);
    fireEvent.click(screen.getByRole("button", { name: "启动标注" }));

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  test("SegmentedTabs exposes accessible tabs", () => {
    const onChange = vi.fn();

    render(
      <SegmentedTabs
        value="image"
        tabs={[
          { id: "image", label: "图像数据" },
          { id: "pointcloud", label: "点云数据" },
        ]}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "点云数据" }));

    expect(screen.getByRole("tab", { name: "点云数据" })).toHaveAttribute("aria-selected", "true");
    expect(onChange).toHaveBeenCalledWith("pointcloud");
  });

  test("StatusTag and ProgressBar render readable status details", () => {
    render(
      <>
        <StatusTag tone="success">已解锁</StatusTag>
        <ProgressBar value={73} tone="info" label="进行中 73%" />
      </>,
    );

    expect(screen.getByText("已解锁")).toBeVisible();
    expect(screen.getByText("进行中 73%")).toBeVisible();
  });
});
