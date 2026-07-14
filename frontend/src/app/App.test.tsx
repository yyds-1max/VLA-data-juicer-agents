import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { EventType } from "@agentscope-ai/agentscope/event";
import { AssistantMsg, UserMsg } from "@agentscope-ai/agentscope/message";
import { StrictMode } from "react";

import {
  createSession,
  deleteSession,
  getNavigationDatasetSummary,
  getSession,
  getSyncImages,
  getSyncImageUrl,
  interruptTurn,
  listSessions,
  recoverHumanDecision,
  submitHumanDecision,
  submitTurn,
  streamSessionEvents,
} from "../api/client";
import type {
  PendingHumanDecision,
  PublicEventEnvelope,
  PublicToolRun,
  SessionDetail,
} from "../api/types";
import { Composer } from "../components/datapilot/Composer";
import { DataPilotWindow } from "../components/datapilot/DataPilotWindow";
import { MessageList } from "../components/datapilot/MessageList";
import { resetNavigationDatasetSummaryCache } from "../features/console/navigationDatasetSummaryCache";
import { createAgentConversation } from "../store/agentConversation";
import { datapilotStore } from "../store/datapilotStore";
import { App } from "./App";

vi.mock("../api/client", () => ({
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  getNavigationDatasetSummary: vi.fn(),
  getSyncImages: vi.fn(),
  getSyncImageUrl: vi.fn(),
  listSessions: vi.fn(),
  getSession: vi.fn(),
  submitTurn: vi.fn(),
  interruptTurn: vi.fn(),
  submitHumanDecision: vi.fn(),
  recoverHumanDecision: vi.fn(),
  streamSessionEvents: vi.fn(),
}));

const apiMocks = vi.mocked({
  createSession,
  deleteSession,
  getNavigationDatasetSummary,
  getSyncImages,
  getSyncImageUrl,
  listSessions,
  getSession,
  submitTurn,
  interruptTurn,
  submitHumanDecision,
  recoverHumanDecision,
  streamSessionEvents,
});

async function waitForAbort(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return;
  await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
}

function waitingEventStream(signal: AbortSignal): AsyncGenerator<PublicEventEnvelope> {
  return (async function* () {
    await waitForAbort(signal);
  })();
}

function eventStream(
  events: PublicEventEnvelope[],
  signal: AbortSignal,
): AsyncGenerator<PublicEventEnvelope> {
  return (async function* () {
    for (const event of events) yield event;
    await waitForAbort(signal);
  })();
}

function replayedAssistantReply(text: string): PublicEventEnvelope[] {
  const event = (
    sequence: number,
    payload: PublicEventEnvelope["event"],
  ): PublicEventEnvelope => ({
    id: `event-${sequence}`,
    session_id: "session-1",
    sequence,
    dedupe_key: String(sequence).padStart(64, "0"),
    created_at: `2026-06-26T00:00:0${sequence}Z`,
    event: payload,
  });

  return [
    event(1, {
      id: "reply-start",
      created_at: "2026-06-26T00:00:01Z",
      type: EventType.REPLY_START,
      session_id: "session-1",
      reply_id: "reply-1",
      name: "DataPilot",
      role: "assistant",
    }),
    event(2, {
      id: "text-start",
      created_at: "2026-06-26T00:00:02Z",
      type: EventType.TEXT_BLOCK_START,
      reply_id: "reply-1",
      block_id: "block-1",
    }),
    event(3, {
      id: "text-delta",
      created_at: "2026-06-26T00:00:03Z",
      type: EventType.TEXT_BLOCK_DELTA,
      reply_id: "reply-1",
      block_id: "block-1",
      delta: text,
    }),
  ];
}

function publicEnvelope(
  sequence: number,
  event: PublicEventEnvelope["event"],
  sessionId = "session-1",
): PublicEventEnvelope {
  return {
    id: `event-${sequence}`,
    session_id: sessionId,
    sequence,
    dedupe_key: String(sequence).padStart(64, "0"),
    created_at: `2026-06-26T00:00:${String(sequence).padStart(2, "0")}Z`,
    event,
  };
}

function emptySessionDetail(sessionId: string, lastSequence = 0): SessionDetail {
  return {
    id: sessionId,
    title: sessionId,
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: lastSequence,
  };
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

function pendingDecision(overrides: Partial<PendingHumanDecision> = {}): PendingHumanDecision {
  return {
    replyId: "reply-1",
    toolCallId: "tool-call-1",
    requestId: "request-1",
    decisionType: "confirmation",
    summary: "发现潜在风险，需要确认。",
    ...overrides,
  };
}

test("MessageList keeps owned tool rows with their reply and appends only orphans", () => {
  const toolRuns: Record<string, PublicToolRun> = {
    "call-1": {
      session_id: "session-1",
      tool_call_id: "call-1",
      tool_name: "extract_navigation_data",
      status: "success",
      summary: "first reply tool",
      error_type: null,
      started_at: "2026-06-26T00:00:01Z",
      finished_at: "2026-06-26T00:00:02Z",
    },
    orphan: {
      session_id: "session-1",
      tool_call_id: "orphan",
      tool_name: "orphan_tool",
      status: "running",
      summary: "unowned",
      error_type: null,
      started_at: "2026-06-26T00:00:03Z",
      finished_at: null,
    },
  };

  render(
    <MessageList
      messages={[
        AssistantMsg({
          id: "reply-1",
          name: "DataPilot",
          content: [
            { type: "text", id: "text-1", text: "first reply" },
            {
              type: "tool_call",
              id: "call-1",
              name: "extract_navigation_data",
              input: "{}",
              state: "finished",
            },
          ],
        }),
        AssistantMsg({ id: "reply-2", name: "DataPilot", content: "second reply" }),
      ]}
      toolRuns={toolRuns}
    />,
  );

  const firstReply = screen.getByText("first reply");
  const ownedTool = screen.getByText("extract_navigation_data · first reply tool");
  const secondReply = screen.getByText("second reply");
  const orphanTool = screen.getByText("orphan_tool · unowned");

  expect(firstReply.compareDocumentPosition(ownedTool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(ownedTool.compareDocumentPosition(secondReply) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(secondReply.compareDocumentPosition(orphanTool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

function setOpenActiveSessionWithPendingDecision(
  decision: PendingHumanDecision,
  options: { sessionId?: string; title?: string } = {},
) {
  const sessionId = options.sessionId ?? "session-1";
  const title = options.title ?? "Existing session";
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: sessionId,
    previousActiveSessionId: null,
    sessions: [
      {
        id: sessionId,
        title,
        created_at: "2026-06-26T00:00:00Z",
        updated_at: "2026-06-26T00:00:00Z",
      },
    ],
    conversation: {
      ...createAgentConversation(),
      messages: [
        AssistantMsg({
          id: "message-1",
          name: "private-agent-name",
          content: "准备继续。",
          created_at: "2026-06-26T00:01:00Z",
        }),
      ],
      pendingHumanDecision: decision,
    },
  });
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
  const result = render(<App />);

  await waitFor(() => expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByText("3.5 秒")).toBeInTheDocument());

  return result;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetNavigationDatasetSummaryCache();
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1280 });
  Object.defineProperty(window, "innerHeight", { configurable: true, writable: true, value: 900 });
  apiMocks.createSession.mockResolvedValue({
    id: "session-created",
    title: "Clean VLA data",
    created_at: "2026-06-26T01:00:00Z",
    updated_at: "2026-06-26T01:00:00Z",
  });
  apiMocks.listSessions.mockResolvedValue([]);
  apiMocks.getSession.mockResolvedValue({
    id: "history-1",
    title: "历史任务",
    created_at: "2026-06-25T01:00:00Z",
    updated_at: "2026-06-25T02:00:00Z",
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: 0,
  });
  apiMocks.submitTurn.mockResolvedValue("turn-1");
  apiMocks.interruptTurn.mockResolvedValue(true);
  apiMocks.submitHumanDecision.mockResolvedValue(true);
  apiMocks.deleteSession.mockResolvedValue(undefined);
  apiMocks.recoverHumanDecision.mockResolvedValue({
    recovered: true,
    plan_id: "plan-1",
    step_id: "confirm",
    handoff_status: "quarantined",
    task_status: "needs_replan",
    next_action: "submit_complete_plan",
  });
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) =>
    waitingEventStream(signal),
  );
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
    sessions: [],
    conversation: createAgentConversation(),
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
  expect(screen.getByRole("heading", { name: "Agent 工作流" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  expect(screen.getByRole("heading", { name: "测试/仿真" })).toBeVisible();
});

test("data management renders navigation dataset date and clip details", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));

  expect(await screen.findByText("20270515")).toBeVisible();
  expect(screen.getByText("日期批次")).toBeVisible();
  expect(screen.getByText("原始 clip")).toBeVisible();
  expect(screen.getByText("已同步 clip")).toBeVisible();
  expect(screen.getByTestId("navigation-summary-strip")).toHaveClass("bg-transparent");
  expect(screen.getByTestId("navigation-summary-strip")).not.toHaveClass("rounded-lg", "border", "shadow-sm");
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
  expect(screen.getByText("视觉检测")).toBeVisible();

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

test("annotation page switches pipeline results and review views", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "自动标注" }));
  expect(screen.getByText("视觉检测")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "标注结果" }));
  expect(screen.getByText("ANN-82401")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "人工复核" }));
  expect(screen.getByText("待复核样本")).toBeVisible();
});

test("model iteration page renders versions training and compare tabs", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "模型迭代" }));
  expect(screen.getByText("v47")).toBeVisible();
  expect(screen.getByText("当前部署")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "训练监控" }));
  expect(screen.getByText("训练损失曲线")).toBeVisible();
  expect(screen.getByText("GPU 监控 (实时)")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "版本对比" }));
  expect(screen.getByText("版本性能对比")).toBeVisible();
});

test("agent workflow page selects nodes and keeps execute action placeholder-only", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Agent 工作流" }));
  expect(screen.getByText("节点库")).toBeVisible();
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
  expect(screen.getByText("仿真场景配置")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "运行监控" }));
  expect(screen.getByText("实时任务日志")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "测试结果" }));
  expect(screen.getByText("详细测试报告")).toBeVisible();
});

test("DataPilot opens only from the floating button after console migration", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  fireEvent.click(screen.getByRole("button", { name: "启动仿真" }));
  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
});

test("DataPilot window remains above the console content", async () => {
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  expect(dialog.className).toContain("fixed");
  expect(dialog.className).toContain("z-[80]");
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
      },
    ],
    conversation: {
      ...createAgentConversation(),
      messages: Array.from({ length: 12 }, (_, index) =>
        index % 2 === 0
          ? UserMsg({
              id: `message-${index}`,
              name: "private-user-name",
              content: `消息 ${index}`,
              created_at: `2026-06-26T00:${String(index).padStart(2, "0")}:00Z`,
            })
          : AssistantMsg({
              id: `message-${index}`,
              name: "private-agent-name",
              content: `消息 ${index}`,
              created_at: `2026-06-26T00:${String(index).padStart(2, "0")}:00Z`,
            }),
      ),
    },
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

test("active session renders SDK messages with fixed public identities and tool runs", async () => {
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
      },
    ],
    conversation: {
      ...createAgentConversation(),
      messages: [
        UserMsg({ name: "private-user", content: "清洗已有数据" }),
        AssistantMsg({ name: "private-agent", content: "已开始处理" }),
      ],
      toolRuns: {
        "call-1": {
          session_id: "session-1",
          tool_call_id: "call-1",
          tool_name: "extract_navigation_data",
          status: "failure",
          summary: "输入无效",
          error_type: "invalid_input",
          started_at: "2026-06-26T00:00:01Z",
          finished_at: "2026-06-26T00:00:02Z",
        },
      },
    },
  });

  await renderAppWithDashboardSettled();

  expect(screen.getByText("清洗已有数据")).toBeVisible();
  expect(screen.getByText("已开始处理")).toBeVisible();
  expect(screen.getByText("You")).toBeVisible();
  expect(screen.getAllByText("DataPilot").some((element) => element.matches("article div"))).toBe(true);
  expect(screen.queryByText("private-user")).not.toBeInTheDocument();
  expect(screen.queryByText("private-agent")).not.toBeInTheDocument();
  expect(screen.getByText("extract_navigation_data · 输入无效")).toBeVisible();
  expect(screen.getByPlaceholderText("继续描述任务…")).toBeVisible();
});

test("tool cards render only the four public statuses and never a backgrounded label", () => {
  const toolRuns = Object.fromEntries(
    (["running", "success", "failure", "stopped"] as const).map((status, index) => [
      `call-${status}`,
      {
        session_id: "session-1",
        tool_call_id: `call-${status}`,
        tool_name: `tool_${status}`,
        status,
        summary: "",
        error_type: null,
        started_at: `2026-06-26T00:00:0${index}Z`,
        finished_at: status === "running" ? null : `2026-06-26T00:00:1${index}Z`,
      },
    ]),
  );

  render(<MessageList messages={[]} toolRuns={toolRuns} />);

  expect(screen.getByText("正在调用")).toBeVisible();
  expect(screen.getByText("成功")).toBeVisible();
  expect(screen.getByText("失败")).toBeVisible();
  expect(screen.getByText("已停止")).toBeVisible();
  expect(screen.queryByText("已转后台")).not.toBeInTheDocument();
});

test("pending human decision shows dialog, hides Composer, submits confirm payload, and clears only after success", async () => {
  const submitDecision = deferred<boolean>();
  apiMocks.submitHumanDecision.mockReturnValue(submitDecision.promise);
  setOpenActiveSessionWithPendingDecision(pendingDecision());

  await renderAppWithDashboardSettled();

  expect(screen.getByRole("dialog", { name: "需要确认" })).toBeVisible();
  expect(screen.getByText("发现潜在风险，需要确认。")).toBeVisible();
  expect(screen.queryByPlaceholderText("继续描述任务…")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "确认" }));

  expect(apiMocks.submitHumanDecision).toHaveBeenCalledWith("session-1", {
    action: "confirm",
    request_id: "request-1",
    tool_call_id: "tool-call-1",
    reply_id: "reply-1",
  });
  expect(datapilotStore.getState().conversation.pendingHumanDecision).toEqual(pendingDecision());

  submitDecision.resolve(true);

  await waitFor(() => expect(datapilotStore.getState().conversation.pendingHumanDecision).toBeNull());
  expect(screen.queryByRole("dialog", { name: "需要确认" })).not.toBeInTheDocument();
  expect(screen.getByPlaceholderText("继续描述任务…")).toBeVisible();
});

test("guide submission includes text in the human decision payload", async () => {
  setOpenActiveSessionWithPendingDecision(pendingDecision());

  await renderAppWithDashboardSettled();

  fireEvent.change(screen.getByLabelText("引导文本"), {
    target: { value: "  先汇总风险再继续  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));

  await waitFor(() =>
    expect(apiMocks.submitHumanDecision).toHaveBeenCalledWith("session-1", {
      action: "guide",
      request_id: "request-1",
      tool_call_id: "tool-call-1",
      reply_id: "reply-1",
      text: "先汇总风险再继续",
    }),
  );
});

test("plan-bound normal decision includes plan and step ids", async () => {
  setOpenActiveSessionWithPendingDecision(
    pendingDecision({ planId: "plan-1", stepId: "confirm" }),
  );
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "确认" }));

  await waitFor(() =>
    expect(apiMocks.submitHumanDecision).toHaveBeenCalledWith("session-1", {
      action: "confirm",
      request_id: "request-1",
      tool_call_id: "tool-call-1",
      reply_id: "reply-1",
      plan_id: "plan-1",
      step_id: "confirm",
    }),
  );
});

test("controlled recovery never submits normally and clears only after success", async () => {
  const recovery = deferred<Awaited<ReturnType<typeof recoverHumanDecision>>>();
  apiMocks.recoverHumanDecision.mockReturnValue(recovery.promise);
  setOpenActiveSessionWithPendingDecision(
    pendingDecision({
      planId: "plan-1",
      stepId: "confirm",
      recoveryRequired: true,
      submissionDisabled: true,
      recoveryEndpoint: "/api/sessions/session-1/human-decisions/recovery",
    }),
  );
  await renderAppWithDashboardSettled();

  fireEvent.change(screen.getByLabelText("恢复原因"), {
    target: { value: "operator confirmed abandoned delivery" },
  });
  fireEvent.click(screen.getByRole("button", { name: "隔离并重新规划" }));

  expect(apiMocks.submitHumanDecision).not.toHaveBeenCalled();
  expect(apiMocks.recoverHumanDecision).toHaveBeenCalledWith("session-1", {
    action: "quarantine_and_replan",
    plan_id: "plan-1",
    step_id: "confirm",
    reason: "operator confirmed abandoned delivery",
  });
  expect(datapilotStore.getState().conversation.pendingHumanDecision).not.toBeNull();

  recovery.resolve({
    recovered: true,
    plan_id: "plan-1",
    step_id: "confirm",
    handoff_status: "quarantined",
    task_status: "needs_replan",
    next_action: "submit_complete_plan",
  });
  await waitFor(() => expect(datapilotStore.getState().conversation.pendingHumanDecision).toBeNull());
});

test("controlled recovery failure retains the dialog and surfaces backend detail", async () => {
  apiMocks.recoverHumanDecision.mockRejectedValue(
    new Error("only a recovery_required human handoff may be quarantined"),
  );
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  setOpenActiveSessionWithPendingDecision(
    pendingDecision({
      planId: "plan-1",
      stepId: "confirm",
      recoveryRequired: true,
      submissionDisabled: true,
    }),
  );
  await renderAppWithDashboardSettled();

  fireEvent.change(screen.getByLabelText("恢复原因"), { target: { value: "recover" } });
  fireEvent.click(screen.getByRole("button", { name: "隔离并重新规划" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("only a recovery_required");
  expect(datapilotStore.getState().conversation.pendingHumanDecision).not.toBeNull();
  expect(screen.getByRole("button", { name: "隔离并重新规划" })).toBeVisible();
});

test("recovery completion from session A cannot clear session B", async () => {
  const recovery = deferred<Awaited<ReturnType<typeof recoverHumanDecision>>>();
  apiMocks.recoverHumanDecision.mockReturnValue(recovery.promise);
  const decision = pendingDecision({
    planId: "plan-1",
    stepId: "confirm",
    recoveryRequired: true,
    submissionDisabled: true,
  });
  setOpenActiveSessionWithPendingDecision(decision, { sessionId: "session-a" });
  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByLabelText("恢复原因"), { target: { value: "recover A" } });
  fireEvent.click(screen.getByRole("button", { name: "隔离并重新规划" }));

  await act(async () => {
    setOpenActiveSessionWithPendingDecision(
      pendingDecision({ ...decision, summary: "Session B recovery" }),
      { sessionId: "session-b" },
    );
  });
  expect(screen.getByLabelText("恢复原因")).toHaveValue("");
  recovery.resolve({
    recovered: true,
    plan_id: "plan-1",
    step_id: "confirm",
    handoff_status: "quarantined",
    task_status: "needs_replan",
    next_action: "submit_complete_plan",
  });

  await waitFor(() => expect(screen.getByText("Session B recovery")).toBeVisible());
  expect(datapilotStore.getState().currentSessionId).toBe("session-b");
  expect(datapilotStore.getState().conversation.pendingHumanDecision?.summary).toBe("Session B recovery");
});

test("pending human decision remains when submitHumanDecision is rejected or not accepted", async () => {
  apiMocks.submitHumanDecision.mockResolvedValueOnce(false).mockRejectedValueOnce(new Error("network failed"));
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  setOpenActiveSessionWithPendingDecision(pendingDecision());

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "确认" }));

  await waitFor(() => expect(apiMocks.submitHumanDecision).toHaveBeenCalledTimes(1));
  expect(datapilotStore.getState().conversation.pendingHumanDecision).toEqual(pendingDecision());
  expect(screen.getByRole("dialog", { name: "需要确认" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "停止" }));

  await waitFor(() => expect(apiMocks.submitHumanDecision).toHaveBeenCalledTimes(2));
  expect(datapilotStore.getState().conversation.pendingHumanDecision).toEqual(pendingDecision());
  expect(consoleError).toHaveBeenCalledWith("Failed to submit human decision", expect.any(Error));
  consoleError.mockRestore();
});

test("resolving a human decision from session A does not clear same-id pending in session B", async () => {
  const submitDecision = deferred<boolean>();
  apiMocks.submitHumanDecision.mockReturnValue(submitDecision.promise);
  setOpenActiveSessionWithPendingDecision(pendingDecision(), { sessionId: "session-a", title: "Session A" });

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "确认" }));

  expect(apiMocks.submitHumanDecision).toHaveBeenCalledWith("session-a", {
    action: "confirm",
    request_id: "request-1",
    tool_call_id: "tool-call-1",
    reply_id: "reply-1",
  });

  await act(async () => {
    setOpenActiveSessionWithPendingDecision(
      pendingDecision({ summary: "Session B 里的确认。" }),
      { sessionId: "session-b", title: "Session B" },
    );
  });

  await waitFor(() => expect(screen.getByText("Session B 里的确认。")).toBeVisible());
  expect(datapilotStore.getState().currentSessionId).toBe("session-b");
  expect(datapilotStore.getState().conversation.pendingHumanDecision).toEqual(
    pendingDecision({ summary: "Session B 里的确认。" }),
  );

  await act(async () => {
    submitDecision.resolve(true);
    await submitDecision.promise;
  });

  await waitFor(() =>
    expect(datapilotStore.getState().conversation.pendingHumanDecision).toEqual(
      pendingDecision({ summary: "Session B 里的确认。" }),
    ),
  );
  expect(screen.getByRole("dialog", { name: "需要确认" })).toBeVisible();
  expect(screen.queryByPlaceholderText("继续描述任务…")).not.toBeInTheDocument();
});


test("an SSE public envelope reduces into the SDK conversation", async () => {
  const events: PublicEventEnvelope[] = [
    {
      id: "event-1",
      session_id: "session-1",
      sequence: 1,
      dedupe_key: "1".padStart(64, "0"),
      created_at: "2026-06-26T00:00:00Z",
      event: {
        id: "reply-start",
        created_at: "2026-06-26T00:00:00Z",
        type: EventType.REPLY_START,
        session_id: "private-session",
        reply_id: "reply-1",
        name: "private-agent",
        role: "assistant",
      },
    },
    {
      id: "event-2",
      session_id: "session-1",
      sequence: 2,
      dedupe_key: "2".padStart(64, "0"),
      created_at: "2026-06-26T00:00:01Z",
      event: {
        id: "text-start",
        created_at: "2026-06-26T00:00:01Z",
        type: EventType.TEXT_BLOCK_START,
        reply_id: "reply-1",
        block_id: "block-1",
      },
    },
    {
      id: "event-3",
      session_id: "session-1",
      sequence: 3,
      dedupe_key: "3".padStart(64, "0"),
      created_at: "2026-06-26T00:00:02Z",
      event: {
        id: "text-delta",
        created_at: "2026-06-26T00:00:02Z",
        type: EventType.TEXT_BLOCK_DELTA,
        reply_id: "reply-1",
        block_id: "block-1",
        delta: "实时回复",
      },
    },
  ];
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) =>
    eventStream(events, signal),
  );
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    previousActiveSessionId: null,
    sessions: [],
    conversation: createAgentConversation(),
  });
  await renderAppWithDashboardSettled();

  expect(await screen.findByText("实时回复")).toBeVisible();
  await waitFor(() => expect(datapilotStore.getState().conversation.lastSequence).toBe(3));
});

test("History button lists sessions in a lightweight panel", async () => {
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "history-1",
      title: "历史任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
  ]);

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));

  expect(apiMocks.listSessions).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "Add context" })).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Open session 历史任务" })).toBeVisible();
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

test("closing the DataPilot window aborts the active event stream", async () => {
  let activeSignal: AbortSignal | undefined;
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) => {
    activeSignal = signal;
    return waitingEventStream(signal);
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
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "继续清洗" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledWith("session-1", 0, expect.any(AbortSignal)));

  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));

  await waitFor(() => expect(activeSignal?.aborted).toBe(true));
});

test("unmount aborts the selected session stream", async () => {
  let activeSignal: AbortSignal | undefined;
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) => {
    activeSignal = signal;
    return waitingEventStream(signal);
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: createAgentConversation(),
  });

  const rendered = await renderAppWithDashboardSettled();
  await waitFor(() => expect(activeSignal).toBeDefined());
  rendered.unmount();

  expect(activeSignal?.aborted).toBe(true);
});

test("switching selected sessions aborts A before opening B and resumes each current cursor", async () => {
  const order: string[] = [];
  apiMocks.getSession.mockImplementation(async (sessionId) => ({
    id: sessionId,
    title: sessionId,
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: datapilotStore.getState().conversation.lastSequence,
  }));
  apiMocks.streamSessionEvents.mockImplementation((sessionId, afterSequence, signal) => {
    order.push(`open:${sessionId}:${afterSequence}`);
    signal.addEventListener("abort", () => order.push(`abort:${sessionId}`), { once: true });
    return waitingEventStream(signal);
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-a",
    sessions: [],
    conversation: { ...createAgentConversation(), lastSequence: 4 },
  });
  await renderAppWithDashboardSettled();
  await waitFor(() => expect(order).toEqual(["open:session-a:4"]));

  act(() => {
    datapilotStore.setState({
      currentSessionId: "session-b",
      conversation: { ...createAgentConversation(), lastSequence: 7 },
    });
  });

  await waitFor(() =>
    expect(order).toEqual(["open:session-a:4", "abort:session-a", "open:session-b:7"]),
  );
  expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2);
});

test("a sequence gap aborts the stream and reconnects from the unadvanced cursor", async () => {
  const signals: AbortSignal[] = [];
  const gap = publicEnvelope(3, {
    id: "gap-reply-start",
    created_at: "2026-06-26T00:00:03Z",
    type: EventType.REPLY_START,
    session_id: "session-1",
    reply_id: "reply-gap",
    name: "DataPilot",
    role: "assistant",
  });
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, signal) => {
    signals.push(signal);
    return signals.length === 1 ? eventStream([gap], signal) : waitingEventStream(signal);
  });
  apiMocks.getSession.mockResolvedValue(emptySessionDetail("session-1", 1));
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: { ...createAgentConversation(), lastSequence: 1 },
  });

  await renderAppWithDashboardSettled();

  await waitFor(() => expect(signals[0]?.aborted).toBe(true));
  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2));
  expect(apiMocks.streamSessionEvents).toHaveBeenNthCalledWith(
    2,
    "session-1",
    1,
    expect.any(AbortSignal),
  );
  expect(datapilotStore.getState().conversation.lastSequence).toBe(1);
});

test("an ownerless continuation triggers snapshot repair before reconnect", async () => {
  const initialSnapshot = deferred<ReturnType<typeof emptySessionDetail>>();
  const ownerless = publicEnvelope(1, {
    id: "ownerless-delta",
    created_at: "2026-06-26T00:00:01Z",
    type: EventType.TEXT_BLOCK_DELTA,
    reply_id: "missing-reply-start",
    block_id: "block-1",
    delta: "must replay",
  });
  const repaired = {
    ...emptySessionDetail("session-1", 3),
    events: replayedAssistantReply("snapshot repaired"),
  };
  const signals: AbortSignal[] = [];
  apiMocks.getSession
    .mockImplementationOnce(() => initialSnapshot.promise)
    .mockResolvedValue(repaired);
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, signal) => {
    signals.push(signal);
    return signals.length === 1 ? eventStream([ownerless], signal) : waitingEventStream(signal);
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: createAgentConversation(),
  });

  await renderAppWithDashboardSettled();

  await waitFor(() => expect(signals[0]?.aborted).toBe(true));
  expect(await screen.findByText("snapshot repaired")).toBeVisible();
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2));
  expect(apiMocks.streamSessionEvents).toHaveBeenNthCalledWith(
    2,
    "session-1",
    3,
    expect.any(AbortSignal),
  );
  initialSnapshot.resolve(repaired);
});

test("a consumed wrong-owner event advances normally without forcing replay", async () => {
  let signal: AbortSignal | undefined;
  const wrongOwner = publicEnvelope(2, {
    id: "wrong-owner-delta",
    created_at: "2026-06-26T00:00:02Z",
    type: EventType.TEXT_BLOCK_DELTA,
    reply_id: "reply-other",
    block_id: "block-1",
    delta: "ignored",
  });
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, nextSignal) => {
    signal = nextSignal;
    return eventStream([wrongOwner], nextSignal);
  });
  apiMocks.getSession.mockResolvedValue(emptySessionDetail("session-1", 1));
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: {
      ...createAgentConversation(),
      phase: "streaming",
      currentReplyId: "reply-1",
      lastSequence: 1,
    },
  });

  await renderAppWithDashboardSettled();

  await waitFor(() => expect(datapilotStore.getState().conversation.lastSequence).toBe(2));
  expect(signal?.aborted).toBe(false);
  expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(1);
  expect(apiMocks.getSession).toHaveBeenCalledTimes(1);
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

test("new session aborts the active event stream", async () => {
  let activeSignal: AbortSignal | undefined;
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) => {
    activeSignal = signal;
    return waitingEventStream(signal);
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
      },
    ],
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "继续清洗" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledWith("session-1", 0, expect.any(AbortSignal)));

  fireEvent.click(screen.getByRole("button", { name: "New session" }));

  expect(activeSignal?.aborted).toBe(true);
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
  expect(apiMocks.streamSessionEvents).toHaveBeenCalledWith("session-created", 0, expect.any(AbortSignal));
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(screen.getByText("清洗 VLA 数据")).toBeVisible();
  expect(screen.queryByText("开始一个任务")).not.toBeInTheDocument();
});

test("failed draft submit does not append a local user message", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  let activeSignal: AbortSignal | undefined;
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) => {
    activeSignal = signal;
    return waitingEventStream(signal);
  });
  apiMocks.submitTurn.mockRejectedValue(new Error("submit failed"));

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.change(screen.getByPlaceholderText("我们要做什么？"), {
    target: { value: "会失败的任务" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-created", "会失败的任务"));
  await waitFor(() => expect(datapilotStore.getState().mode).toBe("draft_new_session"));
  expect(datapilotStore.getState().conversation.messages).toEqual([]);
  expect(screen.queryByText("会失败的任务")).not.toBeInTheDocument();
  expect(activeSignal?.aborted).toBe(true);
  expect(consoleError).toHaveBeenCalledWith("Failed to submit DataPilot draft turn", expect.any(Error));
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
      },
    ],
    conversation: createAgentConversation(),
  });

  await renderAppWithDashboardSettled();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "会失败的继续任务" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-1", "会失败的继续任务"));
  expect(datapilotStore.getState().conversation.messages).toEqual([]);
  expect(screen.queryByText("会失败的继续任务")).not.toBeInTheDocument();
  expect(consoleError).toHaveBeenCalledWith("Failed to submit DataPilot active turn", expect.any(Error));
  consoleError.mockRestore();
});

test("reopening an active session opens its SSE stream before submitting the turn", async () => {
  const calls: string[] = [];
  apiMocks.streamSessionEvents.mockImplementation((sessionId, afterSequence, signal) => {
    calls.push(`stream:${sessionId}:${afterSequence}`);
    return waitingEventStream(signal);
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
  expect(calls).toEqual(["stream:session-1:0", "stream:session-1:0", "submit:session-1"]);
});

test("reopening an active session refreshes replayed assistant events from the backend", async () => {
  apiMocks.getSession
    .mockResolvedValueOnce({
      id: "session-1",
      title: "Existing session",
      created_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:01:00Z",
      messages: [
        {
          id: "message-1",
          session_id: "session-1",
          role: "user",
          content: "清洗已有数据",
          created_at: "2026-06-26T00:01:00Z",
        },
      ],
      events: [],
      tool_runs: [],
      last_sequence: 0,
    })
    .mockResolvedValueOnce({
      id: "session-1",
      title: "Existing session",
      created_at: "2026-06-26T00:00:00Z",
      updated_at: "2026-06-26T00:03:00Z",
      messages: [
        {
          id: "message-1",
          session_id: "session-1",
          role: "user",
          content: "清洗已有数据",
          created_at: "2026-06-26T00:01:00Z",
        },
      ],
      events: replayedAssistantReply("后台完成后的助手回复"),
      tool_runs: [],
      last_sequence: 3,
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
      },
    ],
    conversation: {
      ...createAgentConversation(),
      messages: [UserMsg({ name: "You", content: "清洗已有数据" })],
    },
  });

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("session-1"));
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledTimes(2));
  expect(screen.getByText("后台完成后的助手回复")).toBeVisible();
});

test("reopening an active session starts a new selected stream without submitting", async () => {
  const signals: AbortSignal[] = [];
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) => {
    signals.push(signal);
    return waitingEventStream(signal);
  });
  apiMocks.getSession.mockResolvedValue({
    id: "session-1",
    title: "Existing session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: 0,
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
      },
    ],
  });

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledWith("session-1", 0, expect.any(AbortSignal)));
  fireEvent.click(screen.getByRole("button", { name: "Close DataPilot" }));
  await waitFor(() => expect(signals[0]?.aborted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2));
  expect(apiMocks.streamSessionEvents).toHaveBeenLastCalledWith("session-1", 0, expect.any(AbortSignal));
  expect(apiMocks.submitTurn).not.toHaveBeenCalled();
});

test("an ended selected stream refreshes the snapshot and reconnects from its current cursor", async () => {
  apiMocks.streamSessionEvents
    .mockImplementationOnce(async function* () {
      return;
    })
    .mockImplementation((_sessionId, _afterSequence, signal) => waitingEventStream(signal));
  apiMocks.getSession.mockResolvedValue({
    id: "session-1",
    title: "Existing session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    messages: [],
    events: replayedAssistantReply("断线期间完成的回复"),
    tool_runs: [],
    last_sequence: 3,
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
      },
    ],
  });

  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByText("断线期间完成的回复")).toBeVisible());
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2));
  expect(apiMocks.streamSessionEvents).toHaveBeenLastCalledWith(
    "session-1",
    3,
    expect.any(AbortSignal),
  );
});

test("a retryable selected-stream error reconnects once from the preserved cursor", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  apiMocks.streamSessionEvents
    .mockImplementationOnce(async function* () {
      throw new Error("temporary disconnect");
    })
    .mockImplementation((_sessionId, _afterSequence, signal) => waitingEventStream(signal));
  apiMocks.getSession.mockResolvedValue({
    id: "session-1",
    title: "Existing session",
    created_at: "2026-06-26T00:00:00Z",
    updated_at: "2026-06-26T00:00:00Z",
    messages: [],
    events: [],
    tool_runs: [],
    last_sequence: 5,
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: { ...createAgentConversation(), lastSequence: 5 },
  });

  await renderAppWithDashboardSettled();

  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2));
  expect(apiMocks.streamSessionEvents).toHaveBeenNthCalledWith(
    2,
    "session-1",
    5,
    expect.any(AbortSignal),
  );
  expect(consoleError).toHaveBeenCalledWith(
    "DataPilot event stream failed",
    expect.any(Error),
  );
  consoleError.mockRestore();
});

test("unmount invalidates a stream while its reconnect snapshot is pending", async () => {
  const reconnectSnapshot = deferred<ReturnType<typeof emptySessionDetail>>();
  let endedSignal: AbortSignal | undefined;
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, signal) => {
    endedSignal = signal;
    return (async function* () {
      return;
    })();
  });
  apiMocks.getSession
    .mockResolvedValueOnce(emptySessionDetail("session-1"))
    .mockReturnValueOnce(reconnectSnapshot.promise);
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: createAgentConversation(),
  });

  const rendered = render(<DataPilotWindow />);
  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledTimes(2));
  rendered.unmount();

  expect(endedSignal?.aborted).toBe(true);
  reconnectSnapshot.resolve(emptySessionDetail("session-1"));
  await act(async () => {
    await reconnectSnapshot.promise;
    await new Promise((resolve) => window.setTimeout(resolve, 350));
  });
  expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(1);
});

test("unmount cancels an already queued reconnect timer and its ended lease", async () => {
  vi.useFakeTimers();
  try {
    let endedSignal: AbortSignal | undefined;
    apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, signal) => {
      endedSignal = signal;
      return (async function* () {
        return;
      })();
    });
    apiMocks.getSession.mockResolvedValue(emptySessionDetail("session-1"));
    datapilotStore.setState({
      open: true,
      mode: "active_session",
      currentSessionId: "session-1",
      conversation: createAgentConversation(),
    });

    const rendered = render(<DataPilotWindow />);
    await act(async () => {
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    rendered.unmount();

    expect(endedSignal?.aborted).toBe(true);
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

test("StrictMode leaves exactly one selected-session lease alive", async () => {
  const signals: AbortSignal[] = [];
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, signal) => {
    signals.push(signal);
    return waitingEventStream(signal);
  });
  apiMocks.getSession.mockResolvedValue(emptySessionDetail("session-1"));
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    conversation: createAgentConversation(),
  });

  const rendered = render(
    <StrictMode>
      <DataPilotWindow />
    </StrictMode>,
  );
  await waitFor(() => expect(signals.length).toBeGreaterThanOrEqual(2));

  expect(signals.filter((signal) => !signal.aborted)).toHaveLength(1);
  expect(signals.at(-1)?.aborted).toBe(false);
  rendered.unmount();
  expect(signals.at(-1)?.aborted).toBe(true);
});


test("selecting any saved session restores it as writable active_session", async () => {
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "saved-1",
      title: "保存任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
  ]);
  apiMocks.getSession.mockResolvedValue({
    id: "saved-1",
    title: "保存任务",
    created_at: "2026-06-25T01:00:00Z",
    updated_at: "2026-06-25T02:00:00Z",
    messages: [
      {
        id: "user-1",
        session_id: "saved-1",
        role: "user",
        content: "上一轮任务",
        created_at: "2026-06-25T01:01:00Z",
      },
    ],
    events: [],
    tool_runs: [],
    last_sequence: 0,
  });
  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("button", { name: "Open session 保存任务" }));

  await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalledWith("saved-1"));
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(screen.getByText("上一轮任务")).toBeVisible();
  fireEvent.change(screen.getByPlaceholderText("继续描述任务…"), {
    target: { value: "继续执行" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("saved-1", "继续执行"));
});

test("reselecting the current history session replaces its stream", async () => {
  const signals: AbortSignal[] = [];
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "session-a",
      title: "Session A",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
  ]);
  apiMocks.getSession.mockResolvedValue(emptySessionDetail("session-a"));
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _cursor, signal) => {
    signals.push(signal);
    return waitingEventStream(signal);
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-a",
    sessions: [],
    conversation: createAgentConversation(),
  });
  await renderAppWithDashboardSettled();
  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(1));

  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("button", { name: "Open session Session A" }));

  await waitFor(() => expect(apiMocks.streamSessionEvents).toHaveBeenCalledTimes(2));
  expect(signals[0]?.aborted).toBe(true);
  expect(signals[1]?.aborted).toBe(false);
  expect(datapilotStore.getState().currentSessionId).toBe("session-a");
});

test("rapid history selections keep only the latest response and stream", async () => {
  const sessionB = deferred<ReturnType<typeof emptySessionDetail>>();
  const sessionC = deferred<ReturnType<typeof emptySessionDetail>>();
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "session-b",
      title: "Session B",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
    {
      id: "session-c",
      title: "Session C",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
  ]);
  apiMocks.getSession.mockImplementation((sessionId) =>
    sessionId === "session-b" ? sessionB.promise : sessionC.promise,
  );
  const opened: string[] = [];
  apiMocks.streamSessionEvents.mockImplementation((sessionId, _cursor, signal) => {
    opened.push(sessionId);
    return waitingEventStream(signal);
  });
  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.click(screen.getByRole("button", { name: "History" }));

  fireEvent.click(await screen.findByRole("button", { name: "Open session Session B" }));
  fireEvent.click(screen.getByRole("button", { name: "Open session Session C" }));
  sessionC.resolve(emptySessionDetail("session-c", 4));

  await waitFor(() => expect(datapilotStore.getState().currentSessionId).toBe("session-c"));
  expect(opened).toEqual(["session-c"]);

  sessionB.resolve(emptySessionDetail("session-b", 2));
  await act(async () => {
    await sessionB.promise;
    await Promise.resolve();
  });

  expect(datapilotStore.getState().currentSessionId).toBe("session-c");
  expect(opened).toEqual(["session-c"]);
});

test("deleting a session while its history snapshot is pending prevents resurrection", async () => {
  const sessionB = deferred<ReturnType<typeof emptySessionDetail>>();
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "session-b",
      title: "Session B",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
  ]);
  apiMocks.getSession.mockImplementation((sessionId) =>
    sessionId === "session-b"
      ? sessionB.promise
      : Promise.resolve(emptySessionDetail("session-a")),
  );
  const opened: string[] = [];
  const signals: AbortSignal[] = [];
  apiMocks.streamSessionEvents.mockImplementation((sessionId, _cursor, signal) => {
    opened.push(sessionId);
    signals.push(signal);
    return waitingEventStream(signal);
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-a",
    sessions: [],
    conversation: createAgentConversation(),
  });
  await renderAppWithDashboardSettled();
  await waitFor(() => expect(opened).toEqual(["session-a"]));
  fireEvent.click(screen.getByRole("button", { name: "History" }));

  fireEvent.click(await screen.findByRole("button", { name: "Open session Session B" }));
  await waitFor(() => expect(signals[0]?.aborted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Delete session Session B" }));
  await waitFor(() => expect(apiMocks.deleteSession).toHaveBeenCalledWith("session-b"));
  await waitFor(() => expect(opened).toEqual(["session-a", "session-a"]));
  sessionB.resolve(emptySessionDetail("session-b"));
  await act(async () => {
    await sessionB.promise;
    await Promise.resolve();
  });

  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(datapilotStore.getState().currentSessionId).toBe("session-a");
  expect(opened).toEqual(["session-a", "session-a"]);
  expect(signals[1]?.aborted).toBe(false);
});

test("deleting history stops propagation and removes it only after a successful 204", async () => {
  const deletion = deferred<void>();
  apiMocks.deleteSession.mockReturnValue(deletion.promise);
  apiMocks.listSessions.mockResolvedValue([
    {
      id: "saved-1",
      title: "保存任务",
      created_at: "2026-06-25T01:00:00Z",
      updated_at: "2026-06-25T02:00:00Z",
    },
  ]);
  let activeSignal: AbortSignal | undefined;
  apiMocks.streamSessionEvents.mockImplementation((_sessionId, _afterSequence, signal) => {
    activeSignal = signal;
    return waitingEventStream(signal);
  });
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "saved-1",
    sessions: [],
    conversation: createAgentConversation(),
  });
  await renderAppWithDashboardSettled();
  await waitFor(() => expect(activeSignal).toBeDefined());
  apiMocks.getSession.mockClear();

  fireEvent.click(screen.getByRole("button", { name: "History" }));
  const deleteButton = await screen.findByRole("button", { name: "Delete session 保存任务" });
  fireEvent.click(deleteButton);

  expect(apiMocks.deleteSession).toHaveBeenCalledWith("saved-1");
  expect(apiMocks.getSession).not.toHaveBeenCalled();
  expect(screen.getByText("保存任务")).toBeVisible();
  expect(datapilotStore.getState().sessions.map((session) => session.id)).toContain("saved-1");
  expect(activeSignal?.aborted).toBe(false);

  deletion.resolve();

  await waitFor(() => expect(screen.queryByText("保存任务")).not.toBeInTheDocument());
  expect(datapilotStore.getState().mode).toBe("draft_new_session");
  expect(datapilotStore.getState().currentSessionId).toBeNull();
  expect(datapilotStore.getState().sessions.map((session) => session.id)).not.toContain("saved-1");
  expect(activeSignal?.aborted).toBe(true);
});

test("submitting the first draft creates a writable SDK conversation", async () => {
  await renderAppWithDashboardSettled();
  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  fireEvent.change(screen.getByPlaceholderText("我们要做什么？"), {
    target: { value: "  清洗 VLA 数据  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() =>
    expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-created", "清洗 VLA 数据"),
  );
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(datapilotStore.getState().conversation.messages[0]).toMatchObject({
    role: "user",
    name: "You",
  });
  expect(screen.getByText("清洗 VLA 数据")).toBeVisible();
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
      },
    ],
    conversation: {
      ...createAgentConversation(),
      phase: "streaming",
      currentReplyId: "reply-1",
    },
  });

  await renderAppWithDashboardSettled();

  fireEvent.click(screen.getByRole("button", { name: "Stop current run" }));

  await waitFor(() => expect(apiMocks.interruptTurn).toHaveBeenCalledWith("session-1"));
  expect(datapilotStore.getState().mode).toBe("active_session");
  expect(datapilotStore.getState().currentSessionId).toBe("session-1");
  expect(datapilotStore.getState().conversation.phase).toBe("interrupting");
});

test("stop keeps the draft editable and submits it after the terminating REPLY_END", async () => {
  const interrupt = deferred<boolean>();
  apiMocks.interruptTurn.mockReturnValue(interrupt.promise);
  datapilotStore.setState({
    open: true,
    mode: "active_session",
    currentSessionId: "session-1",
    sessions: [],
    conversation: {
      ...createAgentConversation(),
      phase: "streaming",
      currentReplyId: "reply-1",
    },
  });
  await renderAppWithDashboardSettled();

  const input = screen.getByPlaceholderText("继续描述任务…");
  fireEvent.change(input, { target: { value: "停止后继续" } });
  fireEvent.click(screen.getByRole("button", { name: "Stop current run" }));

  expect(screen.getByRole("button", { name: "Interrupt requested" })).toBeDisabled();
  expect(input).not.toBeDisabled();
  fireEvent.change(input, { target: { value: "保留并继续" } });
  expect(input).toHaveValue("保留并继续");

  await act(async () => {
    interrupt.resolve(true);
    await interrupt.promise;
  });
  expect(screen.getByRole("button", { name: "Interrupt requested" })).toBeDisabled();

  act(() => {
    datapilotStore.getState().applyEvent({
      id: "event-end",
      session_id: "session-1",
      sequence: 1,
      dedupe_key: "end".padStart(64, "0"),
      created_at: "2026-06-26T00:00:01Z",
      event: {
        id: "reply-end",
        created_at: "2026-06-26T00:00:01Z",
        type: EventType.REPLY_END,
        session_id: "session-1",
        reply_id: "reply-1",
      },
    });
  });

  const send = await screen.findByRole("button", { name: "Send message" });
  expect(input).toHaveValue("保留并继续");
  fireEvent.click(send);
  await waitFor(() => expect(apiMocks.submitTurn).toHaveBeenCalledWith("session-1", "保留并继续"));
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
  expect(stopButton.querySelector("svg")).toHaveClass("animate-spin");
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
