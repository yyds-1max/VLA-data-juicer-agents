import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test } from "vitest";

import { ConsoleListTabs } from "./ConsoleListTabs";

function Example() {
  const [value, setValue] = useState<"ready" | "released">("ready");
  return (
    <ConsoleListTabs
      aria-label="发布状态"
      idPrefix="release-tab"
      panelId="release-panel"
      value={value}
      items={[
        { value: "ready", label: "待发布" },
        { value: "released", label: "已发布" },
      ]}
      onValueChange={setValue}
    />
  );
}

describe("ConsoleListTabs", () => {
  test("exposes selected state and a center-expanding underline", () => {
    render(<Example />);

    const ready = screen.getByRole("tab", { name: "待发布" });
    const released = screen.getByRole("tab", { name: "已发布" });
    expect(ready).toHaveAttribute("aria-selected", "true");
    expect(ready).toHaveClass("after:scale-x-100", "after:origin-center");
    expect(released).toHaveClass("after:scale-x-0");
    expect(ready).toHaveAttribute("aria-controls", "release-panel");
  });

  test("supports pointer and arrow-key switching", () => {
    render(<Example />);

    fireEvent.click(screen.getByRole("tab", { name: "已发布" }));
    expect(screen.getByRole("tab", { name: "已发布" })).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(screen.getByRole("tab", { name: "已发布" }), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "待发布" })).toHaveAttribute("aria-selected", "true");
  });
});
