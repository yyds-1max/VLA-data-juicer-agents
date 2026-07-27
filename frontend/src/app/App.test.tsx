import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import {
  createSession,
  getNavigationDatasetSummary,
  getSession,
  getSyncImages,
  getSyncImageUrl,
  interruptTurn,
  listSessions,
  openSessionEvents,
  submitTurn,
} from "../api/client";
import type { SessionDetail } from "../api/types";
import { Composer } from "../components/datapilot/Composer";
import { MessageList } from "../components/datapilot/MessageList";
import { resetNavigationDatasetSummaryCache } from "../features/console/navigationDatasetSummaryCache";
import { createEmptyRunState } from "../store/eventReducer";
import { datapilotStore } from "../store/datapilotStore";
import { App } from "./App";

vi.mock("../api/client", () => ({
  createSession: vi.fn(),
  getNavigationDatasetSummary: vi.fn(),
  getSyncImages: vi.fn(),
  getSyncImageUrl: vi.fn(),
  listSessions: vi.fn(),
  getSession: vi.fn(),
  submitTurn: vi.fn(),
  interruptTurn: vi.fn(),
  openSessionEvents: vi.fn(),
}));

const apiMocks = vi.mocked({
  createSession,
  getNavigationDatasetSummary,
  getSyncImages,
  getSyncImageUrl,
  listSessions,
  getSession,
  submitTurn,
  interruptTurn,
  openSessionEvents,
});

type TestTimelineItem = ReturnType<typeof createEmptyRunState>["timeline"][number] & {
  createdAt: string;
  sequence: number;
};

function activeSocket(close: () => void = vi.fn()): WebSocket {
  return { close, addEventListener: vi.fn(), readyState: WebSocket.OPEN } as unknown as WebSocket;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function sessionDetailFixture(overrides: Partial<SessionDetail>): SessionDetail {
  return {
    id: "session-1",
    title: "DataPilot session",
    status: "active",
    contract_version: 1,
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    messages: [],
    events: [],
    turns: [],
    tasks: [],
    pending_interaction: null,
    ...overrides,
  };
}

function chooseNavigationDate(date: string) {
  fireEvent.click(screen.getByRole("button", { name: "数据日期" }));
  fireEvent.click(screen.getByRole("option", { name: new RegExp(date) }));
}

function mockScrollableElement(element: HTMLElement) {
  Object.defineProperty(element, "clientHeight", { configurable: true, value: 100 });
  Object.defineProperty(element, "scrollHeight", { configurable: true, value: 220 });
  Object.defineProperty(element, "clientWidth", { configurable: true, value: 100 });
  Object.defineProperty(element, "scrollWidth", { configurable: true, value: 220 });
  element.getBoundingClientRect = vi.fn(
    () =>
      ({
        bottom: 100,
        height: 100,
        left: 0,
        right: 100,
        toJSON: () => ({}),
        top: 0,
        width: 100,
        x: 0,
        y: 0,
      }) as DOMRect,
  );
}

async function renderAppWithDashboardSettled() {
  const result = render(<App routerMode="declarative" />);

  await waitFor(() => expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByText("3.5 秒")).toBeInTheDocument());

  return result;
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  vi.clearAllMocks();
  resetNavigationDatasetSummaryCache();
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1280 });
  Object.defineProperty(window, "innerHeight", { configurable: true, writable: true, value: 900 });
  apiMocks.createSession.mockResolvedValue({
    id: "session-created",
    title: "Clean VLA data",
    created_at: "2026-06-26T01:00:00Z",
    updated_at: "2026-06-26T01:00:00Z",
    status: "active",
    contract_version: 1,
  });
  apiMocks.listSessions.mockResolvedValue([]);
  apiMocks.getSession.mockResolvedValue(sessionDetailFixture({
    id: "history-1",
    title: "历史任务",
    created_at: "2026-06-25T01:00:00Z",
    updated_at: "2026-06-25T02:00:00Z",
    status: "historical",
    messages: [],
  }));
  apiMocks.submitTurn.mockResolvedValue("turn-1");
  apiMocks.interruptTurn.mockResolvedValue(true);
  apiMocks.openSessionEvents.mockReturnValue(activeSocket());
  apiMocks.getNavigationDatasetSummary.mockResolvedValue({
    totals: {
      date_count: 1,
      clip_count: 2,
      total_duration_ns: 3_500_000_000,
      raw_message_count: 40,
      extracted_clip_count: 1,
      synced_clip_count: 1,
    },
    sync_distribution: {
      image: 3,
      pointcloud: 2,
      odom: 2,
      grid_map: 1,
    },
    dates: [
      {
        date: "20270515",
        clip_count: 2,
        total_duration_ns: 3_500_000_000,
        raw_message_count: 40,
        extracted_clip_count: 1,
        synced_clip_count: 1,
        sync_frame_counts: {
          image: 3,
          pointcloud: 2,
          odom: 2,
          grid_map: 1,
        },
        status: "synced",
        clips: [
          {
            date: "20270515",
            clip: "clip_a",
            duration_ns: 1_500_000_000,
            raw_message_count: 18,
            topics: [
              { name: "/camera/front/image_raw", type: "sensor_msgs/msg/Image", message_count: 12 },
              { name: "/odom", type: "nav_msgs/msg/Odometry", message_count: 6 },
            ],
            has_tmp_dir: true,
            has_sync_data: true,
            sequences: [],
            sync_frame_counts: {
              image: 2,
              pointcloud: 0,
              odom: 0,
              grid_map: 0,
            },
            status: "synced",
            errors: [],
          },
          {
            date: "20270515",
            clip: "clip_b",
            duration_ns: 2_000_000_000,
            raw_message_count: 22,
            topics: [{ name: "/camera/front/image_raw", type: "sensor_msgs/msg/Image", message_count: 22 }],
            has_tmp_dir: true,
            has_sync_data: false,
            sequences: [],
            sync_frame_counts: {
              image: 3,
              pointcloud: 2,
              odom: 2,
              grid_map: 1,
            },
            status: "extracted",
            errors: [],
          },
        ],
      },
    ],
  });
  apiMocks.getSyncImages.mockResolvedValue({
    date: "20270515",
    clip: "clip_a",
    sequences: [
      { sequence: "seq_a", images: ["001.jpg", "002.jpg"] },
      { sequence: "seq_b", images: ["010.jpg"] },
    ],
  });
  apiMocks.getSyncImageUrl.mockImplementation(
    (date, clip, sequence, filename) => `/sync-images/${date}/${clip}/${sequence}/${filename}`,
  );

  datapilotStore.setState({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: null,
    knownRunningSessionId: null,
    sessions: [],
    messages: [],
    turns: [],
    tasks: [],
    pendingInteraction: null,
    run: createEmptyRunState(),
    pendingInvocation: null,
    floatingOffset: { x: 0, y: 0 },
  });
});

test("renders the full DataLoop console shell by default", async () => {
  await renderAppWithDashboardSettled();

  expect(screen.getByRole("img", { name: "智瀚星途 logo" })).toHaveAttribute("src", "/brand/wise-explore-favicon.png");
  expect(screen.getByText("智瀚星途")).toBeVisible();
  expect(screen.getByText("WISEXPLORE")).toBeVisible();
  expect(screen.queryByText("智瀚星途 DataLoop")).not.toBeInTheDocument();
  expect(screen.queryByText("Voyager Forge")).not.toBeInTheDocument();
  expect(screen.getByText("智瀚星途数据处理系统")).toBeVisible();
  expect(screen.queryByText("Mock workspace")).not.toBeInTheDocument();
  expect(screen.queryByText("frontend only")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "闭环仪表盘" })).toBeVisible();
  expect(screen.getByPlaceholderText("搜索数据、模型、任务...")).toBeVisible();
  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
});

test("dashboard renders navigation dataset summary metrics and distribution", async () => {
  await renderAppWithDashboardSettled();

  expect(screen.getByText("总数据量")).toBeVisible();
  expect(await screen.findByText("3.5 秒")).toBeVisible();
  expect(screen.getByText("1 个日期 / 2 个 clip / 40 条 ROS 消息")).toBeVisible();
  expect(screen.getByText("数据类型分布")).toBeVisible();
  expect(screen.getByText("同步图像帧")).toBeVisible();
  expect(screen.getByText("同步点云帧")).toBeVisible();
  expect(screen.getByText("总数")).toBeVisible();
  expect(screen.getByText("3")).toBeVisible();
  expect(screen.queryByText("3%")).not.toBeInTheDocument();
  expect(screen.getByText("数据闭环流程")).toBeVisible();
  expect(screen.getByText("最近活动")).toBeVisible();
});

test("dashboard metric chart tabs switch between success and loss", async () => {
  await renderAppWithDashboardSettled();

  expect(screen.getByText("Success Rate (%)")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "损失值" }));
  expect(screen.getByText("Training Loss")).toBeVisible();
});

test("sidebar navigation switches console pages", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Agent 工作流" }));
  expect(await screen.findByRole("heading", { name: "Agent 工作流" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  expect(await screen.findByRole("heading", { name: "测试/仿真" })).toBeVisible();
});

test("direct page deep links keep the console shell eager while the page loads", async () => {
  window.history.replaceState({}, "", "/model");

  render(<App routerMode="declarative" />);

  expect(screen.getByTestId("console-sidebar")).toBeVisible();
  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
  expect(screen.getByRole("button", { name: "模型迭代" })).toHaveAttribute("aria-current", "page");
  expect(window.location.pathname).toBe("/model");
  expect(await screen.findByText("当前部署")).toBeVisible();
});

test("desktop sidebar collapse follows navigation and persists across remounts", async () => {
  const { unmount } = await renderAppWithDashboardSettled();

  const sidebar = screen.getByTestId("console-sidebar");
  const main = screen.getByTestId("console-main");
  const collapseButton = screen.getByRole("button", { name: "收起侧边栏" });
  expect(collapseButton.parentElement).toHaveClass("hidden", "md:flex");
  expect(collapseButton.className).not.toContain("focus:ring");
  expect(collapseButton.className).not.toContain("ring-console-cyan");

  fireEvent.click(collapseButton);

  expect(sidebar).toHaveAttribute("data-collapsed", "true");
  expect(sidebar).toHaveClass("md:w-20");
  expect(main).toHaveClass("md:ml-20");
  expect(window.localStorage.getItem("vla-console-sidebar")).toBe("collapsed");
  expect(screen.getByText("WISEXPLORE").parentElement).toHaveClass("md:hidden");
  expect(screen.getByRole("button", { name: "闭环仪表盘" }).querySelector("span")).toHaveClass("md:sr-only");
  expect(screen.getByText("智瀚星途数据处理系统").parentElement).toHaveClass("md:hidden");

  fireEvent.click(screen.getByRole("button", { name: "Agent 工作流" }));
  expect(await screen.findByRole("heading", { name: "Agent 工作流" })).toBeVisible();
  expect(sidebar).toHaveAttribute("data-collapsed", "true");

  unmount();
  render(<App routerMode="declarative" />);

  expect(screen.getByTestId("console-sidebar")).toHaveAttribute("data-collapsed", "true");
  fireEvent.click(screen.getByRole("button", { name: "展开侧边栏" }));
  expect(screen.getByTestId("console-sidebar")).toHaveAttribute("data-collapsed", "false");
  expect(screen.getByTestId("console-main")).toHaveClass("md:ml-64");
  expect(window.localStorage.getItem("vla-console-sidebar")).toBe("expanded");
});

test("data management renders navigation dataset date and clip details", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));

  expect(await screen.findByText("20270515")).toBeVisible();
  expect(screen.getByText("日期批次")).toBeVisible();
  expect(screen.getByText("原始 clip")).toBeVisible();
  expect(screen.getByText("已同步 clip")).toBeVisible();
  expect(screen.getByTestId("navigation-summary-strip")).toHaveClass("bg-transparent");
  expect(screen.getByTestId("navigation-summary-strip")).not.toHaveClass("rounded-lg", "border", "shadow-xs");
  expect(screen.getByTestId("navigation-summary-strip")).toHaveTextContent("总采集时长3.5 秒");
  expect(screen.getByTestId("navigation-summary-strip")).toHaveTextContent("同步图像帧3");
  expect(screen.getByTestId("navigation-process-overview")).toHaveTextContent("raw_data");
  expect(screen.getByTestId("navigation-process-overview")).toHaveTextContent("sync_data");
  expect(screen.getByTestId("navigation-process-overview").innerHTML).not.toContain("bg-console-panel2/70 p-3");
  expect(screen.getByTestId("navigation-process-stepper")).toBeVisible();
  expect(screen.getAllByTestId("navigation-process-step")).toHaveLength(3);
  expect(screen.getByRole("columnheader", { name: "clip 数" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "raw 消息" })).toBeVisible();
  expect(screen.getByTestId("navigation-dataset-scroll")).toHaveClass("console-soft-scrollbar", "max-h-[62vh]", "overflow-auto", "pb-3");
  const datasetScroll = screen.getByTestId("navigation-dataset-scroll");
  mockScrollableElement(datasetScroll);

  fireEvent.pointerMove(datasetScroll, { clientX: 40, clientY: 40 });
  expect(datasetScroll).not.toHaveClass("is-scrollbar-vertical-near");
  expect(datasetScroll).not.toHaveClass("is-scrollbar-horizontal-near");

  fireEvent.pointerMove(datasetScroll, { clientX: 96, clientY: 40 });
  expect(datasetScroll).toHaveClass("is-scrollbar-vertical-near");
  expect(datasetScroll).not.toHaveClass("is-scrollbar-horizontal-near");

  fireEvent.pointerLeave(datasetScroll);
  expect(datasetScroll).not.toHaveClass("is-scrollbar-vertical-near");
  expect(datasetScroll).not.toHaveClass("is-scrollbar-horizontal-near");

  fireEvent.pointerMove(datasetScroll, { clientX: 40, clientY: 96 });
  expect(datasetScroll).not.toHaveClass("is-scrollbar-vertical-near");
  expect(datasetScroll).toHaveClass("is-scrollbar-horizontal-near");

  fireEvent.click(screen.getByRole("button", { name: "展开 20270515" }));

  expect(screen.getByRole("columnheader", { name: "clip 名称" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "topic 摘要" })).toBeVisible();
  expect(screen.getByTestId("navigation-clip-scroll")).toHaveClass("console-soft-scrollbar", "max-h-80", "overflow-auto");
  expect(screen.getByText("clip_a")).toBeVisible();
  expect(screen.getAllByText("已同步").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "查看 clip_a 同步图像" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
});

test("data management sends selected clips through a new visible DataPilot session", async () => {
  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));

  const confirm = screen.getByRole("button", { name: "确定" });
  expect(confirm).toBeDisabled();
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "clip_a" }));
  fireEvent.click(confirm);

  const message = [
    "请处理导航数据。",
    "",
    "数据日期：20270515",
    "指定 clips：",
    "- clip_a",
  ].join("\n");
  await waitFor(() => expect(apiMocks.createSession).toHaveBeenCalledWith(
    message,
    "data_management_shortcut",
    {
      kind: "navigation_dataset_selection_v1",
      dataset_date: "20270515",
      selection: { kind: "selected_clips", clips: ["clip_a"] },
    },
  ));
  await waitFor(() =>
    expect(apiMocks.submitTurn).toHaveBeenCalledWith(
      "session-created",
      message,
      expect.stringMatching(/^navigation-/),
    ),
  );
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "交给 DataPilot" })).not.toBeInTheDocument());
  expect(datapilotStore.getState().open).toBe(true);
  expect(datapilotStore.getState().messages).toEqual([
    expect.objectContaining({ role: "user", content: message, turn_id: "turn-1" }),
  ]);
  expect(await screen.findByRole("dialog", { name: "DataPilot" })).toBeVisible();
  expect(screen.getByText("请处理导航数据。", { exact: false })).toBeVisible();
});

test("data management shortcut claims a double click only once", async () => {
  const pendingTurn = deferred<string>();
  apiMocks.submitTurn.mockReturnValue(pendingTurn.promise);
  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));

  const confirm = screen.getByRole("button", { name: "确定" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledTimes(1));
  expect(apiMocks.createSession).toHaveBeenCalledTimes(1);
  expect(confirm).toBeDisabled();
  expect(confirm).toHaveTextContent("确定");
  pendingTurn.resolve("turn-1");
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "交给 DataPilot" })).not.toBeInTheDocument());
});

test("data management shortcut opens DataPilot but does not submit while its known task is running", async () => {
  const runningDetail = sessionDetailFixture({
    id: "session-running",
    title: "Running session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:01:00Z",
    status: "active" as const,
    messages: [],
    turns: [
      {
        id: "turn-running",
        web_session_id: "session-running",
        origin: "user" as const,
        status: "running" as const,
        started_at: "2026-06-26T00:01:00Z",
        finished_at: null,
        final_message_id: null,
      },
    ],
  });
  apiMocks.getSession.mockResolvedValue(runningDetail);
  datapilotStore.setState({
    open: false,
    mode: "active_session",
    currentSessionId: "session-running",
    previousActiveSessionId: null,
    knownRunningSessionId: "session-running",
    sessions: [runningDetail],
    turns: runningDetail.turns,
    run: { ...createEmptyRunState(), running: true },
  });

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));
  fireEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(screen.getAllByText("当前任务正在执行，请等待完成或停止后再发起。").length).toBeGreaterThan(0));
  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(apiMocks.submitTurn).not.toHaveBeenCalled();
  expect(datapilotStore.getState().open).toBe(true);
  expect(screen.getByRole("button", { name: "确定" })).toBeEnabled();
});

test("data management shortcut still blocks a running session after viewing history", async () => {
  const runningDetail = sessionDetailFixture({
    id: "session-running",
    title: "Running session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:01:00Z",
    status: "active" as const,
    messages: [],
    turns: [
      {
        id: "turn-running",
        web_session_id: "session-running",
        origin: "user" as const,
        status: "running" as const,
        started_at: "2026-06-26T00:01:00Z",
        finished_at: null,
        final_message_id: null,
      },
    ],
  });
  apiMocks.getSession.mockResolvedValue(runningDetail);
  datapilotStore.setState({
    open: false,
    mode: "history_session",
    currentSessionId: "session-history",
    previousActiveSessionId: null,
    knownRunningSessionId: "session-running",
    sessions: [
      runningDetail,
      {
        id: "session-history",
        title: "History",
        created_at: "2026-06-25T00:00:00Z",
        updated_at: "2026-06-25T00:01:00Z",
        status: "historical",
        contract_version: 1,
      },
    ],
    turns: [],
    run: createEmptyRunState(),
  });

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));
  fireEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(screen.getAllByText("当前任务正在执行，请等待完成或停止后再发起。").length).toBeGreaterThan(0));
  expect(apiMocks.getSession).toHaveBeenCalledWith("session-running");
  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(apiMocks.submitTurn).not.toHaveBeenCalled();
});

test("data management shortcut fails closed when a known running session cannot be refreshed", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  apiMocks.getSession.mockRejectedValue(new Error("network unavailable"));
  datapilotStore.setState({
    open: false,
    mode: "draft_new_session",
    currentSessionId: null,
    previousActiveSessionId: "session-running",
    knownRunningSessionId: "session-running",
    sessions: [],
    turns: [],
    run: createEmptyRunState(),
  });

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));
  fireEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(screen.getAllByText("当前任务正在执行，请等待完成或停止后再发起。").length).toBeGreaterThan(0));
  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(apiMocks.submitTurn).not.toHaveBeenCalled();
  expect(consoleError).toHaveBeenCalledWith(
    "Failed to refresh DataPilot before shortcut submission",
    expect.any(Error),
  );
  consoleError.mockRestore();
});

test("data management shortcut retries submit in the session it already created", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  apiMocks.submitTurn
    .mockRejectedValueOnce(new Error("temporary failure"))
    .mockResolvedValueOnce("turn-retry");
  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "clip_a" }));
  fireEvent.click(screen.getByRole("button", { name: "确定" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("提交失败：temporary failure");
  fireEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledTimes(2));
  expect(apiMocks.submitTurn).toHaveBeenLastCalledWith(
    "session-created",
    expect.stringContaining("- clip_a"),
    expect.stringMatching(/^navigation-/),
  );
  expect(apiMocks.submitTurn.mock.calls[1][2]).toBe(apiMocks.submitTurn.mock.calls[0][2]);
  expect(apiMocks.createSession).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "交给 DataPilot" })).not.toBeInTheDocument());
  consoleError.mockRestore();
});

test("changing selection after a failed shortcut creates a new invocation and session", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  apiMocks.createSession
    .mockResolvedValueOnce(sessionDetailFixture({
      id: "session-failed",
      title: "First request",
      created_at: "2026-06-26T01:00:00Z",
      updated_at: "2026-06-26T01:00:00Z",
      status: "active",
      contract_version: 1,
    }))
    .mockResolvedValueOnce(sessionDetailFixture({
      id: "session-changed",
      title: "Changed request",
      created_at: "2026-06-26T01:01:00Z",
      updated_at: "2026-06-26T01:01:00Z",
      status: "active",
    }));
  apiMocks.submitTurn
    .mockRejectedValueOnce(new Error("temporary failure"))
    .mockResolvedValueOnce("turn-changed");

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "交给 DataPilot" }));
  chooseNavigationDate("20270515");
  fireEvent.click(screen.getByRole("checkbox", { name: "clip_a" }));
  fireEvent.click(screen.getByRole("button", { name: "确定" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("提交失败");

  fireEvent.click(screen.getByRole("checkbox", { name: "clip_b" }));
  fireEvent.click(screen.getByRole("button", { name: "确定" }));

  await waitFor(() => expect(apiMocks.createSession).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(apiMocks.submitTurn).toHaveBeenLastCalledWith(
      "session-changed",
      expect.any(String),
      expect.stringMatching(/^navigation-/),
    ),
  );
  expect(apiMocks.submitTurn.mock.calls[1][2]).not.toBe(apiMocks.submitTurn.mock.calls[0][2]);
  expect(apiMocks.createSession).toHaveBeenLastCalledWith(
    expect.not.stringContaining("指定 clips："),
    "data_management_shortcut",
    {
      kind: "navigation_dataset_selection_v1",
      dataset_date: "20270515",
      selection: { kind: "all_clips" },
    },
  );
  consoleError.mockRestore();
});

test("data management switches between navigation and robotic arm data surfaces", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));

  expect(await screen.findByRole("tab", { name: "导航数据" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "机械臂数据" })).toBeVisible();
  expect(screen.getByRole("button", { name: "全部场景" })).toBeVisible();
  expect(screen.getByRole("button", { name: "全部状态" })).toBeVisible();
  expect(screen.getByPlaceholderText("按日期或 clip 搜索")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "机械臂数据" }));

  expect(screen.getByText("机械臂数据接入中")).toBeVisible();
  expect(screen.getByRole("button", { name: "全部场景" })).toBeVisible();
  expect(screen.getByRole("button", { name: "全部状态" })).toBeVisible();
  expect(screen.getByPlaceholderText("按日期或 clip 搜索")).toBeVisible();
});

test("data management filters navigation dates by status", async () => {
  apiMocks.getNavigationDatasetSummary.mockResolvedValue({
    totals: {
      date_count: 2,
      clip_count: 3,
      total_duration_ns: 3_500_000_000,
      raw_message_count: 90,
      extracted_clip_count: 1,
      synced_clip_count: 1,
    },
    sync_distribution: { image: 2, pointcloud: 1, odom: 1, grid_map: 1 },
    dates: [
      {
        date: "20270515",
        clip_count: 2,
        total_duration_ns: 2_000_000_000,
        raw_message_count: 40,
        extracted_clip_count: 1,
        synced_clip_count: 1,
        sync_frame_counts: { image: 2, pointcloud: 1, odom: 1, grid_map: 1 },
        status: "synced",
        clips: [
          {
            date: "20270515",
            clip: "20260515_102948",
            duration_ns: 1_500_000_000,
            raw_message_count: 18,
            topics: [{ name: "/camera/front/image_raw", type: "sensor_msgs/msg/Image", message_count: 12 }],
            has_tmp_dir: true,
            has_sync_data: true,
            sequences: [],
            sync_frame_counts: { image: 2, pointcloud: 1, odom: 1, grid_map: 1 },
            status: "synced",
            errors: [],
          },
        ],
      },
      {
        date: "20270601",
        clip_count: 1,
        total_duration_ns: 1_500_000_000,
        raw_message_count: 50,
        extracted_clip_count: 0,
        synced_clip_count: 0,
        sync_frame_counts: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
        status: "raw_only",
        clips: [
          {
            date: "20270601",
            clip: "20260601_083000",
            duration_ns: 1_500_000_000,
            raw_message_count: 50,
            topics: [{ name: "/odom", type: "nav_msgs/msg/Odometry", message_count: 50 }],
            has_tmp_dir: false,
            has_sync_data: false,
            sequences: [],
            sync_frame_counts: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
            status: "raw_only",
            errors: [],
          },
        ],
      },
    ],
  });
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  expect(await screen.findByText("20270515")).toBeVisible();
  expect(screen.getByText("20270601")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "全部状态" }));
  fireEvent.click(screen.getByRole("option", { name: "待处理" }));

  expect(screen.getByText("20270601")).toBeVisible();
  expect(screen.queryByText("20270515")).not.toBeInTheDocument();
});

test("data management filter menus close when clicking outside", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  await screen.findByRole("tab", { name: "导航数据" });

  fireEvent.click(screen.getByRole("button", { name: "全部场景" }));
  expect(screen.getByRole("option", { name: "室外导航" })).toBeVisible();

  fireEvent.pointerDown(screen.getByPlaceholderText("按日期或 clip 搜索"));
  expect(screen.queryByRole("option", { name: "室外导航" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "全部状态" }));
  expect(screen.getByRole("option", { name: "待处理" })).toBeVisible();

  fireEvent.pointerDown(screen.getByRole("heading", { name: "数据管理" }));
  expect(screen.queryByRole("option", { name: "待处理" })).not.toBeInTheDocument();
});

test("data management search suggests dates and expands matching clips", async () => {
  apiMocks.getNavigationDatasetSummary.mockResolvedValue({
    totals: {
      date_count: 1,
      clip_count: 1,
      total_duration_ns: 3_500_000_000,
      raw_message_count: 40,
      extracted_clip_count: 1,
      synced_clip_count: 1,
    },
    sync_distribution: { image: 2, pointcloud: 1, odom: 1, grid_map: 1 },
    dates: [
      {
        date: "20270515",
        clip_count: 1,
        total_duration_ns: 3_500_000_000,
        raw_message_count: 40,
        extracted_clip_count: 1,
        synced_clip_count: 1,
        sync_frame_counts: { image: 2, pointcloud: 1, odom: 1, grid_map: 1 },
        status: "synced",
        clips: [
          {
            date: "20270515",
            clip: "20260515_102948",
            duration_ns: 3_500_000_000,
            raw_message_count: 40,
            topics: [{ name: "/camera/front/image_raw", type: "sensor_msgs/msg/Image", message_count: 40 }],
            has_tmp_dir: true,
            has_sync_data: true,
            sequences: [],
            sync_frame_counts: { image: 2, pointcloud: 1, odom: 1, grid_map: 1 },
            status: "synced",
            errors: [],
          },
        ],
      },
    ],
  });
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  const searchInput = await screen.findByPlaceholderText("按日期或 clip 搜索");

  fireEvent.change(searchInput, { target: { value: "2027" } });
  expect(screen.getByRole("option", { name: "20270515" })).toBeVisible();

  fireEvent.change(searchInput, { target: { value: "20260515_" } });
  fireEvent.click(screen.getByRole("option", { name: "20260515_102948" }));

  expect(searchInput).toHaveValue("20260515_102948");
  expect(screen.getByText("20270515")).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "clip 名称" })).toBeVisible();
  expect(screen.getByText("20260515_102948")).toBeVisible();
  expect(screen.getByText("20260515_102948").closest("tr")).toHaveClass("bg-console-cyan/10");
  expect(screen.queryByText("匹配")).not.toBeInTheDocument();
});

test("navigation dataset summary is reused while switching console pages", async () => {
  await renderAppWithDashboardSettled();
  expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "自动标注" }));
  expect(await screen.findByText("自动标注任务")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "闭环仪表盘" }));
  expect(await screen.findByText("3.5 秒")).toBeVisible();
  expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  expect(await screen.findByText("20270515")).toBeVisible();
  expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalledTimes(1);
});

test("data management opens synchronized image drawer and browses sequences", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "展开 20270515" }));
  fireEvent.click(screen.getByRole("button", { name: "查看 clip_a 同步图像" }));

  expect(await screen.findByRole("dialog", { name: "同步图像浏览" })).toBeVisible();
  expect(apiMocks.getSyncImages).toHaveBeenCalledWith("20270515", "clip_a");
  expect(screen.getByRole("button", { name: "001.jpg" })).toBeVisible();
  expect(screen.getByRole("button", { name: "002.jpg" })).toBeVisible();
  expect(screen.getByText("1 / 2")).toBeVisible();
  expect(screen.queryByRole("button", { name: "上一张" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下一张" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "下一张" }));
  expect(screen.getByText("2 / 2")).toBeVisible();
  expect(screen.getByRole("button", { name: "上一张" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "下一张" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "001.jpg" }));
  expect(screen.getByText("1 / 2")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "seq_b" }));
  expect(screen.getByRole("button", { name: "010.jpg" })).toBeVisible();
  expect(screen.getByText("1 / 1")).toBeVisible();
});

test("data management does not show stale image listing when switching clips", async () => {
  const clipBListing = deferred<Awaited<ReturnType<typeof getSyncImages>>>();
  apiMocks.getSyncImages.mockImplementation((date, clip) => {
    if (clip === "clip_b") {
      return clipBListing.promise;
    }
    return Promise.resolve({
      date,
      clip,
      sequences: [{ sequence: "seq_a", images: ["001.jpg", "002.jpg"] }],
    });
  });
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "展开 20270515" }));
  fireEvent.click(screen.getByRole("button", { name: "查看 clip_a 同步图像" }));

  expect(await screen.findByRole("button", { name: "001.jpg" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "关闭同步图像浏览" }));
  fireEvent.click(screen.getByRole("button", { name: "查看 clip_b 同步图像" }));

  expect(await screen.findByRole("dialog", { name: "同步图像浏览" })).toHaveTextContent("20270515 / clip_b");
  expect(screen.queryByRole("button", { name: "001.jpg" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "002.jpg" })).not.toBeInTheDocument();

  clipBListing.resolve({
    date: "20270515",
    clip: "clip_b",
    sequences: [{ sequence: "seq_b", images: ["101.jpg"] }],
  });

  expect(await screen.findByRole("button", { name: "101.jpg" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "001.jpg" })).not.toBeInTheDocument();
});

test("data management image drawer moves focus inside and closes on Escape", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "展开 20270515" }));
  fireEvent.click(screen.getByRole("button", { name: "查看 clip_a 同步图像" }));

  const dialog = await screen.findByRole("dialog", { name: "同步图像浏览" });
  const closeButton = screen.getByRole("button", { name: "关闭同步图像浏览" });
  await waitFor(() => expect(closeButton).toHaveFocus());

  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "同步图像浏览" })).not.toBeInTheDocument();
});

test("data management image drawer ignores listing that resolves after close", async () => {
  const pendingListing = deferred<Awaited<ReturnType<typeof getSyncImages>>>();
  apiMocks.getSyncImages.mockReturnValue(pendingListing.promise);
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(await screen.findByRole("button", { name: "展开 20270515" }));
  fireEvent.click(screen.getByRole("button", { name: "查看 clip_a 同步图像" }));

  expect(await screen.findByRole("dialog", { name: "同步图像浏览" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "关闭同步图像浏览" }));

  pendingListing.resolve({
    date: "20270515",
    clip: "clip_a",
    sequences: [{ sequence: "seq_a", images: ["late.jpg"] }],
  });

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "同步图像浏览" })).not.toBeInTheDocument());
  expect(screen.queryByText("late.jpg")).not.toBeInTheDocument();
});

test("annotation page replaces fixtures with the real M1 task entry", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "自动标注" }));
  expect(await screen.findByText("自动标注任务")).toBeVisible();
  expect(screen.getByText("从已同步数据开始，完成 Web 首帧标注与 Tracking。")).toBeVisible();
  expect(screen.queryByText("视觉检测")).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/annotation/jobs");
  expect(await screen.findByText("当前服务器暂不能创建处理任务")).toBeVisible();
});

test("model iteration page renders versions training and compare tabs", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "模型迭代" }));
  expect(await screen.findByText("当前部署")).toBeVisible();
  expect(screen.getByText("v47")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "训练监控" }));
  expect(screen.getByText("训练损失曲线")).toBeVisible();
  expect(screen.getByText("GPU 监控 (实时)")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "版本对比" }));
  expect(screen.getByText("版本性能对比")).toBeVisible();
});

test("agent workflow page selects nodes and keeps execute action placeholder-only", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Agent 工作流" }));
  expect(await screen.findByText("节点库")).toBeVisible();
  expect(screen.getByText("工作流画布")).toBeVisible();
  expect(screen.getByRole("button", { name: "数据源接入" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "画布节点 数据源接入" })).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(screen.getByRole("button", { name: "预处理管线" }));
  expect(screen.getByRole("button", { name: "预处理管线" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "画布节点 预处理管线" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("heading", { name: "预处理管线" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "数据源接入" }));
  expect(screen.getByRole("button", { name: "数据源接入" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText("从多个数据源拉取原始数据")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "执行流程" }));
  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();
});

test("simulation page switches config running and results views", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  expect(await screen.findByText("仿真场景配置")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "运行监控" }));
  expect(screen.getByText("实时任务日志")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "测试结果" }));
  expect(screen.getByText("详细测试报告")).toBeVisible();
});

test("DataPilot opens only from the floating button after console migration", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  fireEvent.click(await screen.findByRole("button", { name: "启动仿真" }));
  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
});

test("DataPilot window remains above the console content", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  expect(dialog.className).toContain("fixed");
  expect(dialog.className).toContain("z-80");
});

test("DataPilot window can be dragged from its title bar", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  const handle = screen.getByLabelText("Drag DataPilot window");

  fireEvent.pointerDown(handle, { pointerId: 1, clientX: 900, clientY: 620 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 760, clientY: 500 });
  fireEvent.pointerUp(window, { pointerId: 1, clientX: 760, clientY: 500 });

  expect(dialog).toHaveStyle({ left: "auto" });
  expect(dialog.style.transform).toContain("translate3d(-140px, -120px, 0)");
});

test("DataPilot floating button can be dragged without opening the window", async () => {
  await renderAppWithDashboardSettled();

  const button = screen.getByRole("button", { name: "Open DataPilot" });

  fireEvent.pointerDown(button, { pointerId: 1, clientX: 1180, clientY: 760 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 1060, clientY: 680 });
  fireEvent.pointerUp(window, { pointerId: 1, clientX: 1060, clientY: 680 });
  fireEvent.click(button);

  expect(button.style.transform).toContain("translate3d(-120px, -80px, 0)");
  expect(button.className).not.toContain("transform]");
  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();
});

test("DataPilot floating button stays inside the viewport while dragged", async () => {
  await renderAppWithDashboardSettled();

  const button = screen.getByRole("button", { name: "Open DataPilot" });

  fireEvent.pointerDown(button, { pointerId: 1, clientX: 1180, clientY: 760 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 1480, clientY: 1160 });
  fireEvent.pointerUp(window, { pointerId: 1, clientX: 1480, clientY: 1160 });

  expect(button.style.transform).toContain("translate3d(4px, 4px, 0)");
});

test("DataPilot window opens and closes at the dragged floating button position", async () => {
  await renderAppWithDashboardSettled();

  const button = screen.getByRole("button", { name: "Open DataPilot" });
  fireEvent.pointerDown(button, { pointerId: 1, clientX: 1180, clientY: 760 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 1060, clientY: 680 });
  fireEvent.pointerUp(window, { pointerId: 1, clientX: 1060, clientY: 680 });
  fireEvent.click(button);
  fireEvent.click(button);

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  expect(dialog.style.transform).toContain("translate3d(-120px, -80px, 0)");
  expect(dialog.style.getPropertyValue("--datapilot-x")).toBe("-120px");
  expect(dialog.style.getPropertyValue("--datapilot-y")).toBe("-80px");

  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));

  const reopenedButton = screen.getByRole("button", { name: "Open DataPilot" });
  expect(reopenedButton.style.transform).toContain("translate3d(-120px, -80px, 0)");
});

test("DataPilot window keeps itself inside the viewport when opened from a high floating button", async () => {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1280 });
  Object.defineProperty(window, "innerHeight", { configurable: true, writable: true, value: 720 });
  await renderAppWithDashboardSettled();

  const button = screen.getByRole("button", { name: "Open DataPilot" });
  fireEvent.pointerDown(button, { pointerId: 1, clientX: 1180, clientY: 760 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 1060, clientY: 240 });
  fireEvent.pointerUp(window, { pointerId: 1, clientX: 1060, clientY: 240 });
  fireEvent.click(button);
  fireEvent.click(button);

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  expect(button.style.transform).toContain("translate3d(-120px, -520px, 0)");
  expect(dialog.style.transform).toContain("translate3d(-120px, -4px, 0)");
  expect(dialog.style.getPropertyValue("--datapilot-anchor-x")).toBe("-120px");
  expect(dialog.style.getPropertyValue("--datapilot-anchor-y")).toBe("-520px");
});

test("DataPilot header icon buttons do not start window dragging", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  const historyButton = screen.getByRole("button", { name: "History" });
  const historyIcon = historyButton.querySelector("svg");
  expect(historyIcon).not.toBeNull();

  fireEvent.pointerDown(historyIcon as SVGSVGElement, { pointerId: 1, clientX: 1460, clientY: 80 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 1360, clientY: 140 });
  fireEvent.pointerUp(window, { pointerId: 1, clientX: 1360, clientY: 140 });
  fireEvent.click(historyButton);

  expect(dialog.style.transform).toContain("translate3d(0px, 0px, 0)");
  expect(await screen.findByText("历史会话")).toBeVisible();
});

test("DataPilot window keeps wheel scrolling inside the dialog", async () => {
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
        contract_version: 1,
      },
    ],
    messages: Array.from({ length: 12 }, (_, index) => ({
      id: `message-${index}`,
      session_id: "session-1",
      role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
      content: `消息 ${index}`,
      created_at: `2026-06-26T00:${String(index).padStart(2, "0")}:00Z`,
    })),
  });
  await renderAppWithDashboardSettled();

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  const header = screen.getByLabelText("Drag DataPilot window");
  const scrollArea = dialog.querySelector<HTMLElement>("[data-datapilot-scroll-area='true']");
  expect(scrollArea).not.toBeNull();
  Object.defineProperty(scrollArea, "scrollHeight", { configurable: true, value: 1200 });
  Object.defineProperty(scrollArea, "clientHeight", { configurable: true, value: 300 });
  const preventDefault = vi.spyOn(Event.prototype, "preventDefault");

  fireEvent.wheel(header, { deltaY: 120 });
  expect(preventDefault).toHaveBeenCalled();
  expect(scrollArea?.scrollTop).toBe(120);

  preventDefault.mockClear();
  scrollArea!.scrollTop = 900;
  fireEvent.wheel(scrollArea!, { deltaY: 80 });
  expect(preventDefault).toHaveBeenCalled();
  expect(scrollArea?.scrollTop).toBe(900);
  preventDefault.mockRestore();
});

test("opens DataPilot draft window from the floating button", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Open DataPilot" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
  expect(screen.getByText("开始一个任务")).toBeVisible();
  expect(screen.getByText("描述你的目标，DataPilot会帮你完成。")).toBeVisible();
  expect(screen.getByPlaceholderText("我们要做什么？")).toBeVisible();
  expect(screen.queryByText("新任务草稿")).not.toBeInTheDocument();
  expect(screen.queryByText("ready")).not.toBeInTheDocument();
  expect(screen.queryByText("继续任务")).not.toBeInTheDocument();
  expect(screen.queryByText(/示例|标签|Example/i)).not.toBeInTheDocument();
  expect(screen.queryByText("VLA 主智能体")).not.toBeInTheDocument();
});

test("active session renders messages and does not render draft start content", async () => {
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
        contract_version: 1,
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

  await renderAppWithDashboardSettled();

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
      contract_version: 1,
    },
  ]);

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));

  expect(apiMocks.listSessions).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "Add context" })).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: /历史任务/ })).toBeVisible();
  expect(screen.getByText("2026-06-25 02:00")).toBeVisible();
  expect(screen.queryByText(/last message|summary|继续任务|pending/i)).not.toBeInTheDocument();
});

test("close hides the window and restores the floating button", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));

  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument());
});

test("closing the DataPilot window closes the active event stream", async () => {
  const close = vi.fn();
  apiMocks.openSessionEvents.mockReturnValue(activeSocket(close));
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "继续清洗" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledWith("session-1", expect.any(Function)));

  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));

  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
});

test("new session enters draft mode without creating a session", async () => {
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "New session" }));

  const state = datapilotStore.getState();
  expect(state.mode).toBe("draft_new_session");
  expect(state.currentSessionId).toBeNull();
  expect(state.previousActiveSessionId).toBe("session-1");
  expect(state.sessions).toHaveLength(1);
  expect(apiMocks.createSession).not.toHaveBeenCalled();
  expect(screen.getByText("开始一个任务")).toBeVisible();
});

test("new session closes the active event stream", async () => {
  const close = vi.fn();
  apiMocks.openSessionEvents.mockReturnValue(activeSocket(close));
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "继续清洗" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledWith("session-1", expect.any(Function)));

  fireEvent.click(screen.getByRole("button", { name: "New session" }));

  expect(close).toHaveBeenCalledTimes(1);
});

test("submitting the first draft message creates a session, opens events, submits turn, and shows the user message", async () => {
  await renderAppWithDashboardSettled();

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

test("failed draft submit keeps the created session without a local user message", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const close = vi.fn();
  apiMocks.openSessionEvents.mockReturnValue(activeSocket(close));
  apiMocks.submitTurn.mockRejectedValue(new Error("submit failed"));

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.change(screen.getByPlaceholderText("我们要做什么？"), {
    target: { value: "会失败的任务" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-created", "会失败的任务"));
  await waitFor(() => expect(datapilotStore.getState().mode).toBe("active_session"));
  expect(datapilotStore.getState().currentSessionId).toBe("session-created");
  expect(datapilotStore.getState().messages).toEqual([]);
  expect(screen.queryByText("会失败的任务")).not.toBeInTheDocument();
  expect(close).not.toHaveBeenCalled();
  expect(consoleError).toHaveBeenCalledWith("Failed to submit DataPilot new-session turn", expect.any(Error));
  consoleError.mockRestore();
});

test("failed active submit does not append a local user message", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  apiMocks.submitTurn.mockRejectedValue(new Error("submit failed"));
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
        contract_version: 1,
      },
    ],
    messages: [],
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "会失败的继续任务" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-1", "会失败的继续任务"));
  expect(datapilotStore.getState().messages).toEqual([]);
  expect(screen.queryByText("会失败的继续任务")).not.toBeInTheDocument();
  expect(consoleError).toHaveBeenCalledWith("Failed to submit DataPilot active turn", expect.any(Error));
  consoleError.mockRestore();
});

test("reopening an active session opens events before submitting the turn", async () => {
  const calls: string[] = [];
  apiMocks.openSessionEvents.mockImplementation((sessionId) => {
    calls.push(`open:${sessionId}`);
    return activeSocket();
  });
  apiMocks.submitTurn.mockImplementation(async (sessionId) => {
    calls.push(`submit:${sessionId}`);
    return "turn-1";
  });
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "恢复后继续" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-1", "恢复后继续"));
  expect(calls).toEqual(["open:session-1", "open:session-1", "submit:session-1"]);
});

test("reopening an active session refreshes persisted messages from the backend", async () => {
  apiMocks.getSession
    .mockResolvedValueOnce(sessionDetailFixture({
      id: "session-1",
      title: "Existing session",
      created_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:01:00Z",
      status: "active",
      contract_version: 1,
      messages: [
        {
          id: "message-1",
          session_id: "session-1",
          role: "user",
          content: "清洗已有数据",
          created_at: "2026-06-26T00:01:00Z",
        },
      ],
    }))
    .mockResolvedValueOnce(sessionDetailFixture({
      id: "session-1",
      title: "Existing session",
      created_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:03:00Z",
      status: "active",
      contract_version: 1,
      messages: [
        {
          id: "message-1",
          session_id: "session-1",
          role: "user",
          content: "清洗已有数据",
          created_at: "2026-06-26T00:01:00Z",
        },
        {
          id: "message-2",
          session_id: "session-1",
          role: "assistant",
          content: "后台完成后的助手回复",
          created_at: "2026-06-26T00:03:00Z",
        },
      ],
    }));
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
        contract_version: 1,
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

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("session-1"));
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledTimes(2));
  expect(screen.getByText("后台完成后的助手回复")).toBeVisible();
});

test("reopening an active session reopens the event stream before another turn is submitted", async () => {
  const close = vi.fn();
  apiMocks.openSessionEvents.mockReturnValue(activeSocket(close));
  apiMocks.getSession.mockResolvedValue(sessionDetailFixture({
    id: "session-1",
    title: "Existing session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    status: "active",
    contract_version: 1,
    messages: [],
  }));
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledWith("session-1", expect.any(Function)));
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledTimes(2));
  expect(apiMocks.openSessionEvents).toHaveBeenLastCalledWith("session-1", expect.any(Function));
  expect(apiMocks.submitTurn).not.toHaveBeenCalled();
});

test("event stream close refreshes the active session and reconnects", async () => {
  let closeHandler: (() => void) | undefined;
  const addEventListener = vi.fn((type: string, handler: () => void) => {
    if (type === "close") {
      closeHandler = handler;
    }
  });
  apiMocks.openSessionEvents.mockReturnValue({
    close: vi.fn(),
    addEventListener,
    readyState: WebSocket.OPEN,
  } as unknown as WebSocket);
  apiMocks.getSession.mockResolvedValue(sessionDetailFixture({
    id: "session-1",
    title: "Existing session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    status: "active",
    contract_version: 1,
    messages: [
      {
        id: "message-1",
        session_id: "session-1",
        role: "assistant",
        content: "断线期间完成的回复",
        created_at: "2026-06-26T00:01:00Z",
      },
    ],
    events: [],
  }));
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(closeHandler).toBeDefined());

  await act(async () => {
    closeHandler?.();
  });

  await waitFor(() => expect(screen.getByText("断线期间完成的回复")).toBeVisible());
  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledTimes(2));
});

test("opening a history session does not reconnect the event stream", async () => {
  datapilotStore.setState({
    open: false,
    mode: "history_session",
    currentSessionId: "history-1",
    previousActiveSessionId: null,
    sessions: [
      {
        id: "history-1",
        title: "历史任务",
        created_at: "2026-06-25T01:00:00Z",
        updated_at: "2026-06-25T02:00:00Z",
        status: "historical",
        contract_version: 1,
      },
    ],
    messages: [
      {
        id: "history-message-1",
        session_id: "history-1",
        role: "assistant",
        content: "历史助手回复",
        created_at: "2026-06-25T01:02:00Z",
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  expect(apiMocks.getSession).not.toHaveBeenCalled();
  expect(apiMocks.openSessionEvents).not.toHaveBeenCalled();
  expect(screen.getByText("历史助手回复")).toBeVisible();
});

test("stale active session refreshes do not overwrite draft mode", async () => {
  let resolveSession: (value: Awaited<ReturnType<typeof getSession>>) => void = () => undefined;
  apiMocks.getSession.mockReturnValue(
    new Promise((resolve) => {
      resolveSession = resolve;
    }),
  );
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("session-1"));
  fireEvent.click(screen.getByRole("button", { name: "New session" }));
  resolveSession(sessionDetailFixture({
    id: "session-1",
    title: "Existing session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:03:00Z",
    status: "active",
    contract_version: 1,
    messages: [
      {
        id: "message-2",
        session_id: "session-1",
        role: "assistant",
        content: "过期刷新不应出现",
        created_at: "2026-06-26T00:03:00Z",
      },
    ],
  }));

  await waitFor(() => expect(datapilotStore.getState().mode).toBe("draft_new_session"));
  expect(datapilotStore.getState().currentSessionId).toBeNull();
  expect(screen.getByText("开始一个任务")).toBeVisible();
  expect(screen.queryByText("过期刷新不应出现")).not.toBeInTheDocument();
});

test("selecting a history session restores persisted messages and hides active controls", async () => {
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "history-1",
      title: "历史任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
      status: "historical",
      contract_version: 1,
    },
  ]);
  apiMocks.getSession.mockResolvedValue(sessionDetailFixture({
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
  }));

  await renderAppWithDashboardSettled();

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

test("selecting an active session from history restores active controls and can submit another turn", async () => {
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "active-1",
      title: "活跃任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
      status: "active",
      contract_version: 1,
    },
  ]);
  apiMocks.getSession.mockResolvedValue(sessionDetailFixture({
    id: "active-1",
    title: "活跃任务",
    created_at: "2026-06-25T01:00:00Z",
    updated_at: "2026-06-25T02:00:00Z",
    status: "active",
    messages: [
      {
        id: "active-message-1",
        session_id: "active-1",
        role: "user",
        content: "上一轮用户消息",
        created_at: "2026-06-25T01:01:00Z",
      },
    ],
  }));

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("button", { name: /活跃任务/ }));

  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("active-1"));
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(screen.getByText("上一轮用户消息")).toBeVisible();

  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "继续上一轮任务" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("active-1", "继续上一轮任务"));
  expect(screen.getByText("继续上一轮任务")).toBeVisible();
});

test("selecting a history session closes the active event stream before loading details", async () => {
  const close = vi.fn(() => calls.push("close"));
  const calls: string[] = [];
  apiMocks.openSessionEvents.mockReturnValue(activeSocket(close));
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "history-1",
      title: "历史任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
      status: "historical",
      contract_version: 1,
    },
  ]);
  apiMocks.getSession.mockImplementation(async (sessionId) => {
    calls.push(`get:${sessionId}`);
    return sessionDetailFixture({
      id: "history-1",
      title: "历史任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
      status: "historical",
      contract_version: 1,
      messages: [],
    });
  });
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
        contract_version: 1,
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "先打开流" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(apiMocks.openSessionEvents).toHaveBeenCalledWith("session-1", expect.any(Function)));

  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("button", { name: /历史任务/ }));

  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("history-1"));
  expect(calls.slice(-2)).toEqual(["close", "get:history-1"]);
});

test("message list keeps earlier timeline output before later user messages", () => {
  const run = createEmptyRunState();
  run.timeline = [
    {
      kind: "assistant",
      text: "较早的助手输出",
      turnId: "turn-1",
      createdAt: "2026-06-26T00:02:00Z",
      sequence: 1,
    } as typeof run.timeline[number] & { createdAt: string; sequence: number },
  ];

  render(
    <MessageList
      messages={[
        {
          id: "message-1",
          session_id: "session-1",
          role: "user",
          content: "较新的用户消息",
          created_at: "2026-06-26T00:03:00Z",
          turn_id: "turn-2",
        },
      ]}
      turns={[
        {
          id: "turn-1",
          web_session_id: "session-1",
          origin: "system",
          status: "completed",
          started_at: "2026-06-26T00:02:00Z",
          finished_at: "2026-06-26T00:02:01Z",
          final_message_id: null,
        },
        {
          id: "turn-2",
          web_session_id: "session-1",
          origin: "user",
          status: "running",
          started_at: "2026-06-26T00:03:00Z",
          finished_at: null,
          final_message_id: null,
        },
      ]}
      run={run}
    />,
  );

  const text = screen.getByText("较早的助手输出").compareDocumentPosition(screen.getByText("较新的用户消息"));
  expect(text & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("message list follows new content when the user is already at the bottom", () => {
  const firstMessages = [
    {
      id: "message-1",
      session_id: "session-1",
      role: "user" as const,
      content: "第一条消息",
      created_at: "2026-06-26T00:01:00Z",
    },
  ];
  const { container, rerender } = render(<MessageList messages={firstMessages} run={createEmptyRunState()} />);
  const scrollArea = container.querySelector<HTMLElement>("[data-datapilot-scroll-area='true']");
  expect(scrollArea).not.toBeNull();
  Object.defineProperty(scrollArea, "scrollHeight", { configurable: true, value: 900 });
  Object.defineProperty(scrollArea, "clientHeight", { configurable: true, value: 300 });
  scrollArea!.scrollTop = 600;
  fireEvent.scroll(scrollArea!);

  Object.defineProperty(scrollArea, "scrollHeight", { configurable: true, value: 1100 });
  rerender(
    <MessageList
      messages={[
        ...firstMessages,
        {
          id: "message-2",
          session_id: "session-1",
          role: "assistant" as const,
          content: "新的助手回复",
          created_at: "2026-06-26T00:02:00Z",
        },
      ]}
      run={createEmptyRunState()}
    />,
  );

  expect(scrollArea?.scrollTop).toBe(1100);
});

test("message list does not jump down when the user is reading older content", () => {
  const firstMessages = [
    {
      id: "message-1",
      session_id: "session-1",
      role: "user" as const,
      content: "第一条消息",
      created_at: "2026-06-26T00:01:00Z",
    },
  ];
  const { container, rerender } = render(<MessageList messages={firstMessages} run={createEmptyRunState()} />);
  const scrollArea = container.querySelector<HTMLElement>("[data-datapilot-scroll-area='true']");
  expect(scrollArea).not.toBeNull();
  Object.defineProperty(scrollArea, "scrollHeight", { configurable: true, value: 900 });
  Object.defineProperty(scrollArea, "clientHeight", { configurable: true, value: 300 });
  scrollArea!.scrollTop = 200;
  fireEvent.scroll(scrollArea!);

  Object.defineProperty(scrollArea, "scrollHeight", { configurable: true, value: 1100 });
  rerender(
    <MessageList
      messages={[
        ...firstMessages,
        {
          id: "message-2",
          session_id: "session-1",
          role: "assistant" as const,
          content: "新的助手回复",
          created_at: "2026-06-26T00:02:00Z",
        },
      ]}
      run={createEmptyRunState()}
    />,
  );

  expect(scrollArea?.scrollTop).toBe(200);
});

test("timeline assistant output does not hide unmatched persisted assistant messages", () => {
  const run = createEmptyRunState();
  run.timeline = [
    {
      kind: "assistant",
      text: "实时流式回复",
      turnId: "turn-1",
      createdAt: "2026-06-26T00:02:00Z",
      sequence: 1,
    },
  ] as TestTimelineItem[];

  render(
    <MessageList
      messages={[
        {
          id: "message-1",
          session_id: "session-1",
          role: "assistant",
          content: "只存在于持久化消息里的旧回复",
          created_at: "2026-06-26T00:01:00Z",
        },
      ]}
      turns={[{
        id: "turn-1",
        web_session_id: "session-1",
        origin: "system",
        status: "running",
        started_at: "2026-06-26T00:02:00Z",
        finished_at: null,
        final_message_id: null,
      }]}
      run={run}
    />,
  );

  expect(screen.getByText("只存在于持久化消息里的旧回复")).toBeVisible();
  expect(screen.getByText("实时流式回复")).toBeVisible();
});

test("durable turns do not hide legacy messages from before the migration", () => {
  render(
    <MessageList
      messages={[
        {
          id: "legacy-user",
          session_id: "session-1",
          role: "user",
          content: "迁移前的请求",
          created_at: "2026-06-26T00:01:00Z",
        },
        {
          id: "legacy-assistant",
          session_id: "session-1",
          role: "assistant",
          content: "迁移前的回复",
          created_at: "2026-06-26T00:01:05Z",
        },
        {
          id: "new-user",
          session_id: "session-1",
          turn_id: "turn-new",
          role: "user",
          content: "迁移后的请求",
          created_at: "2026-06-26T00:02:00Z",
        },
      ]}
      turns={[
        {
          id: "turn-new",
          web_session_id: "session-1",
          origin: "user",
          status: "running",
          started_at: "2026-06-26T00:02:00Z",
          finished_at: null,
          final_message_id: null,
        },
      ]}
      run={createEmptyRunState()}
    />,
  );

  expect(screen.getByText("迁移前的请求")).toBeVisible();
  expect(screen.getByText("迁移前的回复")).toBeVisible();
  expect(screen.getByText("迁移后的请求")).toBeVisible();
});

test("message with a turn id remains visible while turn metadata is rolling out", () => {
  render(
    <MessageList
      messages={[
        {
          id: "rolling-user",
          session_id: "session-1",
          turn_id: "turn-not-in-snapshot",
          role: "user",
          content: "滚动部署中的请求",
          created_at: "2026-06-26T00:02:00Z",
        },
      ]}
      run={createEmptyRunState()}
    />,
  );

  expect(screen.getByText("滚动部署中的请求")).toBeVisible();
});

test("separate reply streams in one durable turn render as separate DataPilot bubbles", () => {
  const run = createEmptyRunState();
  run.timeline = [
    {
      kind: "assistant",
      text: "第一轮流式回复",
      turnId: "turn-1",
      replyId: "reply-1",
      createdAt: "2026-06-26T00:02:00Z",
      sequence: 1,
    },
    {
      kind: "assistant",
      text: "第二轮流式回复",
      turnId: "turn-1",
      replyId: "reply-2",
      createdAt: "2026-06-26T00:02:01Z",
      sequence: 2,
    },
  ] as TestTimelineItem[];

  render(
    <MessageList
      messages={[]}
      turns={[
        {
          id: "turn-1",
          web_session_id: "session-1",
          origin: "system",
          status: "running",
          started_at: "2026-06-26T00:02:00Z",
          finished_at: null,
          final_message_id: null,
        },
      ]}
      run={run}
    />,
  );

  expect(screen.getByText("第一轮流式回复")).toBeVisible();
  expect(screen.getByText("第二轮流式回复")).toBeVisible();
});

test("persisted final message replaces its live final by message id", () => {
  const run = createEmptyRunState();
  run.timeline = [
    {
      kind: "assistant",
      text: "流式终态副本",
      status: "final",
      turnId: "turn-1",
      replyId: "reply-1",
      finalMessageId: "message-final",
      createdAt: "2026-06-26T00:02:01Z",
      sequence: 1,
    },
  ] as TestTimelineItem[];

  render(
    <MessageList
      messages={[
        {
          id: "message-final",
          session_id: "session-1",
          turn_id: "turn-1",
          role: "assistant",
          content: "持久化最终回复",
          created_at: "2026-06-26T00:02:01Z",
        },
      ]}
      turns={[
        {
          id: "turn-1",
          web_session_id: "session-1",
          origin: "system",
          status: "completed",
          started_at: "2026-06-26T00:02:00Z",
          finished_at: "2026-06-26T00:02:01Z",
          final_message_id: "message-final",
        },
      ]}
      run={run}
    />,
  );

  expect(screen.getByText("持久化最终回复")).toBeVisible();
  expect(screen.queryByText("流式终态副本")).not.toBeInTheDocument();
});

test("contract-v1 Stop immediately releases the Composer while preserving its draft", async () => {
  apiMocks.getSession.mockResolvedValue(sessionDetailFixture({
    id: "session-1",
    title: "V1 session",
    contract_version: 1,
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:02:00Z",
    status: "active",
    messages: [{
      id: "message-stop-final",
      session_id: "session-1",
      role: "assistant",
      content: "当前运行已停止，任务可以稍后继续。",
      created_at: "2026-06-26T00:02:00Z",
      turn_id: "turn-running",
    }],
    events: [],
    turns: [{
      id: "turn-running",
      web_session_id: "session-1",
      origin: "user",
      status: "completed",
      started_at: "2026-06-26T00:01:00Z",
      finished_at: "2026-06-26T00:02:00Z",
      final_message_id: "message-stop-final",
    }],
    tasks: [{
      task_ref: "NAV-A1B2",
      domain: "navigation_data",
      dataset_date: "20270605",
      selection: { kind: "selected_clips", clips: ["20260605_152856"] },
      scene_mode: null,
      status: "paused",
      phase: "数据准备",
      state_revision: 2,
      started_at: "2026-06-26T00:01:00Z",
      updated_at: "2026-06-26T00:02:00Z",
    }],
    pending_interaction: null,
  }));
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    previousActiveSessionId: null,
    sessions: [
      {
        id: "session-1",
        title: "V1 session",
        contract_version: 1,
        created_at: "2026-06-26T00:00:00Z",
        updated_at: "2026-06-26T00:00:00Z",
        status: "active",
      },
    ],
    turns: [{
      id: "turn-running",
      web_session_id: "session-1",
      origin: "user",
      status: "running",
      started_at: "2026-06-26T00:01:00Z",
      finished_at: null,
      final_message_id: null,
    }],
    run: { ...createEmptyRunState(), running: true },
  });
  await renderAppWithDashboardSettled();

  const input = screen.getByPlaceholderText("继续描述任务…");
  fireEvent.change(input, { target: { value: "停止后继续询问" } });
  fireEvent.click(screen.getByRole("button", { name: "Stop current run" }));

  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeVisible());
  expect(input).toHaveValue("停止后继续询问");
  expect(datapilotStore.getState().turns[0].status).toBe("completed");
  expect(datapilotStore.getState().tasks[0].status).toBe("paused");
});

test("contract-v1 blocking interaction replaces the Composer and keeps the task strip visible", async () => {
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    previousActiveSessionId: null,
    sessions: [{
      id: "session-1",
      title: "V1 session",
      contract_version: 1,
      created_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:00:00Z",
      status: "active",
    }],
    tasks: [{
      task_ref: "nav-A7K2",
      domain: "navigation",
      dataset_date: "20270605",
      selection: { kind: "all_clips" },
      scene_mode: null,
      status: "waiting_user",
      phase: "确认标定参数 60%",
      state_revision: 3,
      started_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:01:00Z",
      count: { done: 3, total: 8, unit: "个数据段" },
    }],
    pendingInteraction: {
      interaction_id: "interaction-1",
      task_ref: "nav-A7K2",
      kind: "high_risk_confirmation",
      blocking: true,
      risk: "high",
      title: "确认标定参数",
      summary: "确认后继续执行。",
      options: [
        { option_id: "confirm", label: "确认执行", tone: "danger" },
        { option_id: "reject", label: "返回修改" },
      ],
      interaction_revision: 1,
      expected_task_revision: 3,
      expires_at: "2099-01-01T00:00:00Z",
    },
  });
  await renderAppWithDashboardSettled();

  expect(screen.getByRole("heading", { name: "确认标定参数" })).toBeVisible();
  expect(screen.getByText("导航任务 nav-A7K2")).toBeVisible();
  expect(screen.queryByPlaceholderText("继续描述任务…")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send message" })).not.toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "DataPilot" })).not.toHaveTextContent(/[%％]/);
});

test("durable running turn keeps the composer locked during handoff gaps", async () => {
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
        contract_version: 1,
      },
    ],
    turns: [
      {
        id: "turn-running",
        web_session_id: "session-1",
        origin: "user",
        status: "running",
        started_at: "2026-06-26T00:01:00Z",
        finished_at: null,
        final_message_id: null,
      },
    ],
    run: createEmptyRunState(),
  });

  await renderAppWithDashboardSettled();

  expect(screen.getByRole("button", { name: "Stop current run" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Send message" })).not.toBeInTheDocument();
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

test("running Composer preserves its draft when requesting Stop", () => {
  const onInterrupt = vi.fn();
  render(<Composer placeholder="继续描述任务…" running onSubmit={vi.fn()} onInterrupt={onInterrupt} />);

  const input = screen.getByPlaceholderText("继续描述任务…");
  fireEvent.change(input, { target: { value: "停止后继续问这个问题" } });
  fireEvent.click(screen.getByRole("button", { name: "Stop current run" }));

  expect(onInterrupt).toHaveBeenCalledTimes(1);
  expect(input).toHaveValue("停止后继续问这个问题");
});

test("interrupting Composer shows a spinning circle button without visible text", () => {
  const onInterrupt = vi.fn();

  render(
    <Composer
      placeholder="我们要做什么？"
      running
      interrupting
      onSubmit={vi.fn()}
      onInterrupt={onInterrupt}
    />,
  );

  const stopButton = screen.getByRole("button", { name: "Interrupt requested" });
  expect(stopButton.querySelector("svg")).toHaveClass("motion-safe:animate-spin");
  expect(screen.queryByText(/中断中|Interrupt requested/)).not.toBeInTheDocument();

  fireEvent.click(stopButton);
  expect(onInterrupt).not.toHaveBeenCalled();
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

test("Composer submits with Enter and keeps Shift+Enter for multiline input", () => {
  const onSubmit = vi.fn();
  render(<Composer placeholder="我们要做什么？" onSubmit={onSubmit} />);

  const input = screen.getByPlaceholderText("我们要做什么？");
  fireEvent.change(input, { target: { value: "第一行\n第二行" } });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(onSubmit).not.toHaveBeenCalled();

  fireEvent.keyDown(input, { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith("第一行\n第二行");
  expect(input).toHaveValue("");
});

test("Composer grows with content but caps its visible height", () => {
  render(<Composer placeholder="我们要做什么？" onSubmit={vi.fn()} />);

  const input = screen.getByPlaceholderText("我们要做什么？") as HTMLTextAreaElement;
  Object.defineProperty(input, "scrollHeight", { configurable: true, value: 240 });
  fireEvent.change(input, { target: { value: "多行\n内容\n继续\n增长\n直到上限" } });

  expect(input.style.height).toBe("132px");
  expect(input.style.overflowY).toBe("auto");
});
