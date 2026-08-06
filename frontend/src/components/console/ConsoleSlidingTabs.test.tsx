import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { TabsContent } from "../ui/tabs";
import { ConsoleSlidingTabs } from "./ConsoleSlidingTabs";

const items = [
  { value: "overview", label: "总览" },
  { value: "running", label: "运行中" },
  { value: "history", label: "历史记录" },
] as const;

function SlidingTabsHarness({ onChange = vi.fn() }: { onChange?: (value: string) => void }) {
  const [value, setValue] = useState<(typeof items)[number]["value"]>("overview");

  return (
    <ConsoleSlidingTabs
      aria-label="任务视图"
      value={value}
      items={items}
      onValueChange={(nextValue) => {
        setValue(nextValue);
        onChange(nextValue);
      }}
    >
      <TabsContent value="overview">总览内容</TabsContent>
      <TabsContent value="running">运行内容</TabsContent>
      <TabsContent value="history">历史内容</TabsContent>
    </ConsoleSlidingTabs>
  );
}

describe("ConsoleSlidingTabs", () => {
  test("moves one shared indicator for any item count and exposes the selected panel", () => {
    const onChange = vi.fn();
    render(<SlidingTabsHarness onChange={onChange} />);

    const indicator = document.querySelector<HTMLElement>(
      '[data-slot="console-sliding-tabs-indicator"]',
    );
    expect(document.querySelector('[data-slot="tabs-list"]')).toHaveClass("isolate");
    expect(indicator).toHaveStyle({
      width: "calc((100% - 0.5rem) / 3)",
      transform: "translate3d(0%, 0, 0)",
    });
    expect(screen.getByRole("tab", { name: "总览" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toHaveTextContent("总览内容");

    fireEvent.mouseDown(screen.getByRole("tab", { name: "历史记录" }), {
      button: 0,
      ctrlKey: false,
    });

    expect(onChange).toHaveBeenLastCalledWith("history");
    expect(screen.getByRole("tab", { name: "历史记录" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(indicator).toHaveAttribute("data-active-index", "2");
    expect(indicator).toHaveStyle({ transform: "translate3d(200%, 0, 0)" });
    expect(screen.getByRole("tabpanel")).toHaveTextContent("历史内容");
  });

  test("retains Radix arrow-key navigation and automatic selection", async () => {
    render(<SlidingTabsHarness />);
    const overview = screen.getByRole("tab", { name: "总览" });

    await act(async () => {
      overview.focus();
      fireEvent.keyDown(overview, { key: "ArrowRight" });
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "运行中" })).toHaveFocus();
      expect(screen.getByRole("tab", { name: "运行中" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
  });

  test("disables individual and global triggers without losing reduced-motion styling", () => {
    const onChange = vi.fn();
    const disabledItems = [
      { value: "overview", label: "总览" },
      { value: "running", label: "运行中", disabled: true },
    ] as const;

    const { rerender } = render(
      <ConsoleSlidingTabs
        aria-label="任务视图"
        value="overview"
        items={disabledItems}
        onValueChange={onChange}
      />,
    );

    const disabledTab = screen.getByRole("tab", { name: "运行中" });
    expect(disabledTab).toBeDisabled();
    fireEvent.click(disabledTab);
    expect(onChange).not.toHaveBeenCalled();
    expect(disabledTab).toHaveClass("motion-reduce:transition-none");

    rerender(
      <ConsoleSlidingTabs
        aria-label="任务视图"
        value="overview"
        items={disabledItems}
        disabled
        onValueChange={onChange}
      />,
    );
    expect(screen.getByRole("tab", { name: "总览" })).toBeDisabled();
  });
});
