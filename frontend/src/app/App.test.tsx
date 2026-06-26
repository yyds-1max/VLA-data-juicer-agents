import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { Composer } from "../components/datapilot/Composer";
import { datapilotStore } from "../store/datapilotStore";
import { App } from "./App";

beforeEach(() => {
  datapilotStore.setState({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    sessions: [],
    messages: [],
  });
});

test("renders the DataLoop console shell", () => {
  render(<App />);

  expect(screen.getByText("DataLoop")).toBeVisible();
  expect(screen.getByText("数据接入")).toBeVisible();
  expect(screen.getByText("处理任务")).toBeVisible();
  expect(screen.getByText("质量检查")).toBeVisible();
  expect(screen.getByText("Agent 状态")).toBeVisible();
  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
});

test("renders inert shell navigation without fake controls", () => {
  const { container } = render(<App />);

  expect(screen.getAllByRole("button")).toHaveLength(1);
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

test("opens DataPilot draft window from the floating button", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  expect(screen.queryByRole("button", { name: "Open DataPilot" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
  expect(screen.getByText("开始一个任务")).toBeVisible();
  expect(screen.getByText("描述你的 VLA 数据处理目标，DataPilot 会接入主智能体执行。")).toBeVisible();
  expect(screen.getByPlaceholderText("我们要做什么？")).toBeVisible();
  expect(screen.queryByText("继续任务")).not.toBeInTheDocument();
  expect(screen.queryByText(/示例|标签|Example/i)).not.toBeInTheDocument();
  expect(screen.queryByText("VLA 主智能体")).not.toBeInTheDocument();
});

test("close hides the window and restores the floating button", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));

  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
});

test("new session enters draft mode without creating a session", () => {
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    previousActiveSessionId: null,
    sessions: [
      {
        id: "session-1",
        title: "Existing session",
        created_at: "2026-06-26T00:00:00Z",
        updated_at: "2026-06-26T00:00:00Z",
        status: "active",
      },
    ],
  });

  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "New session" }));

  const state = datapilotStore.getState();
  expect(state.mode).toBe("draft_new_session");
  expect(state.currentSessionId).toBeNull();
  expect(state.previousActiveSessionId).toBe("session-1");
  expect(state.sessions).toHaveLength(1);
  expect(screen.getByText("开始一个任务")).toBeVisible();
});

test("running Composer shows a square stop button", () => {
  const onInterrupt = vi.fn();

  render(<Composer placeholder="我们要做什么？" running onSubmit={vi.fn()} onInterrupt={onInterrupt} />);

  const stopButton = screen.getByRole("button", { name: "Stop current run" });
  expect(stopButton.querySelector("svg")).toBeInTheDocument();
  expect(screen.queryByText(/停止|Stop current run/)).not.toBeInTheDocument();

  fireEvent.click(stopButton);
  expect(onInterrupt).toHaveBeenCalledTimes(1);
});

test("Composer trims messages, clears after submit, and ignores empty input", () => {
  const onSubmit = vi.fn();

  render(<Composer placeholder="我们要做什么？" onSubmit={onSubmit} />);

  const input = screen.getByPlaceholderText("我们要做什么？");
  fireEvent.change(input, { target: { value: "   " } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(onSubmit).not.toHaveBeenCalled();

  fireEvent.change(input, { target: { value: "  清洗 VLA 数据  " } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  expect(onSubmit).toHaveBeenCalledWith("清洗 VLA 数据");
  expect(input).toHaveValue("");
});
