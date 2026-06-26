import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";

import { App } from "./App";

test("renders the DataLoop console shell", () => {
  render(<App />);

  expect(screen.getByText("DataLoop")).toBeVisible();
  expect(screen.getByText("数据接入")).toBeVisible();
  expect(screen.getByText("处理任务")).toBeVisible();
  expect(screen.getByText("质量检查")).toBeVisible();
  expect(screen.getByText("Agent 状态")).toBeVisible();
  expect(screen.queryByRole("button", { name: /datapilot/i })).not.toBeInTheDocument();
});
