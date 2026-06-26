import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import {
  createSession,
  getSession,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitTurn,
} from "../api/client";
import { Composer } from "../components/datapilot/Composer";
import { createEmptyRunState } from "../store/eventReducer";
import { datapilotStore } from "../store/datapilotStore";
import { App } from "./App";

vi.mock("../api/client", () => ({
  createSession: vi.fn(),
  listSessions: vi.fn(),
  getSession: vi.fn(),
  submitTurn: vi.fn(),
  interruptTurn: vi.fn(),
  openSessionEvents: vi.fn(),
}));

const apiMocks = vi.mocked({
  createSession,
  listSessions,
  getSession,
  submitTurn,
  interruptTurn,
  openSessionEvents,
});

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.createSession.mockResolvedValue({
    id: "session-created",
    title: "Clean VLA data",
    created_at: "2026-06-26T01:00:00Z",
    updated_at: "2026-06-26T01:00:00Z",
    status: "active",
  });
  apiMocks.listSessions.mockResolvedValue([]);
  apiMocks.getSession.mockResolvedValue({
    id: "history-1",
    title: "历史任务",
    created_at: "2026-06-25T01:00:00Z",
    updated_at: "2026-06-25T02:00:00Z",
    status: "historical",
    messages: [],
  });
  apiMocks.submitTurn.mockResolvedValue("turn-1");
  apiMocks.interruptTurn.mockResolvedValue(true);
  apiMocks.openSessionEvents.mockReturnValue({ close: vi.fn() } as unknown as WebSocket);

  datapilotStore.setState({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    sessions: [],
    messages: [],
    run: createEmptyRunState(),
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

  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Open DataPilot" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
  expect(screen.getByText("开始一个任务")).toBeVisible();
  expect(screen.getByText("描述你的 VLA 数据处理目标，DataPilot 会接入主智能体执行。")).toBeVisible();
  expect(screen.getByPlaceholderText("我们要做什么？")).toBeVisible();
  expect(screen.queryByText("继续任务")).not.toBeInTheDocument();
  expect(screen.queryByText(/示例|标签|Example/i)).not.toBeInTheDocument();
  expect(screen.queryByText("VLA 主智能体")).not.toBeInTheDocument();
});

test("active session renders messages and does not render draft start content", () => {
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
    messages: [
      {
        id: "message-1",
        session_id: "session-1",
        role: "user",
        content: "清洗已有数据",
        created_at: "2026-06-26T00:01:00Z",
      },
    ],
  });

  render(<App />);

  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
  expect(screen.getByText("清洗已有数据")).toBeVisible();
  expect(screen.getByPlaceholderText("继续描述任务…")).toBeVisible();
  expect(screen.queryByText("开始一个任务")).not.toBeInTheDocument();
});

test("History button lists sessions in a lightweight panel", async () => {
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "history-1",
      title: "历史任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
      status: "historical",
    },
  ]);

  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));

  expect(apiMocks.listSessions).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "Add context" })).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: /历史任务/ })).toBeVisible();
  expect(screen.getByText("2026-06-25 02:00")).toBeVisible();
  expect(screen.queryByText(/last message|summary|继续任务|pending/i)).not.toBeInTheDocument();
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
  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(screen.getByText("开始一个任务")).toBeVisible();
});

test("submitting the first draft message creates a session, opens events, submits turn, and shows the user message", async () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.change(screen.getByPlaceholderText("我们要做什么？"), {
    target: { value: "  清洗 VLA 数据  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  expect(apiMocks.createSession).toHaveBeenCalledWith("清洗 VLA 数据");
  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-created", "清洗 VLA 数据"));
  expect(apiMocks.openSessionEvents).toHaveBeenCalledWith("session-created", expect.any(Function));
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(screen.getByText("清洗 VLA 数据")).toBeVisible();
  expect(screen.queryByText("开始一个任务")).not.toBeInTheDocument();
});

test("selecting a history session restores persisted messages and hides active controls", async () => {
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "history-1",
      title: "历史任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
      status: "historical",
    },
  ]);
  apiMocks.getSession.mockResolvedValue({
    id: "history-1",
    title: "历史任务",
    created_at: "2026-06-25T01:00:00Z",
    updated_at: "2026-06-25T02:00:00Z",
    status: "historical",
    messages: [
      {
        id: "history-message-1",
        session_id: "history-1",
        role: "user",
        content: "历史用户消息",
        created_at: "2026-06-25T01:01:00Z",
      },
      {
        id: "history-message-2",
        session_id: "history-1",
        role: "assistant",
        content: "历史助手回复",
        created_at: "2026-06-25T01:02:00Z",
      },
    ],
  });

  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("button", { name: /历史任务/ }));

  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("history-1"));
  expect(datapilotStore.getState().mode).toBe("history_session");
  expect(screen.getByText("历史用户消息")).toBeVisible();
  expect(screen.getByText("历史助手回复")).toBeVisible();
  expect(screen.queryByPlaceholderText("继续描述任务…")).not.toBeInTheDocument();
  expect(screen.queryByText("继续任务")).not.toBeInTheDocument();
});

test("running stop interrupts the current turn without leaving active mode", async () => {
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
    run: { ...createEmptyRunState(), running: true, activeText: "[Main] 正在思考" },
  });

  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Stop current run" }));

  await waitFor(() => expect(apiMocks.interruptTurn).toHaveBeenCalledWith("session-1"));
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(datapilotStore.getState().currentSessionId).toBe("session-1");
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
