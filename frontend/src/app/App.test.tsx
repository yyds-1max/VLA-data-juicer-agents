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

test("renders inert shell navigation without fake controls", () => {
  const { container } = render(<App />);

  expect(screen.queryAllByRole("button")).toHaveLength(0);
  expect(screen.getAllByText("闭环仪表盘")).toHaveLength(4);
  expect(screen.getAllByText("数据管理")).toHaveLength(2);
  expect(screen.getAllByText("实验验证")).toHaveLength(2);
  expect(screen.getAllByText("工作流")).toHaveLength(2);
  expect(screen.getAllByText("系统设置")).toHaveLength(2);
  expect(container.querySelectorAll('[aria-current="page"]')).toHaveLength(2);
});

test("shows running signal status text", () => {
  render(<App />);

  expect(screen.getByText("采集延迟正常")).toBeVisible();
  expect(screen.getByText("正常")).toBeVisible();
  expect(screen.getByText("清洗规则更新")).toBeVisible();
  expect(screen.getByText("更新")).toBeVisible();
  expect(screen.getByText("质量阈值稳定")).toBeVisible();
  expect(screen.getByText("稳定")).toBeVisible();
});
