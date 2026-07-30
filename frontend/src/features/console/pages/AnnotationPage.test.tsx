import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  createMemoryRouter,
  MemoryRouter,
  Route,
  RouterProvider,
  Routes,
  useLocation,
} from "react-router-dom";

import { getNavigationDatasetDate, getNavigationDatasetSummary } from "../../../api/client";
import type {
  NavigationClipSummary,
  NavigationDatasetSummary,
  NavigationDateSummary,
} from "../../../api/types";
import {
  AnnotationApiError,
  getAnnotationCapabilities,
  getAnnotationJob,
  getAnnotationSegment,
  getCalibrationProfiles,
  listAnnotationJobs,
  mutateAnnotationJob,
  mutateAnnotationSegment,
  saveAnnotationDraft,
  skipAnnotationSegment,
  submitInitialAnnotation,
} from "../../annotation/api";
import { AnnotationDomainEventBridge } from "../../annotation/AnnotationDomainEventBridge";
import type { AnnotationDomainEvent } from "../../annotation/events";
import { resetAnnotationProjectionStore } from "../../annotation/projectionStore";
import type { AnnotationJobDetail, AnnotationSegmentDetail } from "../../annotation/types";
import { resetNavigationDatasetSummaryCache } from "../navigationDatasetSummaryCache";
import { AnnotationPage } from "./AnnotationPage";

vi.mock("../../../api/client", () => ({
  getNavigationDatasetDate: vi.fn(),
  getNavigationDatasetSummary: vi.fn(),
}));

vi.mock("../../annotation/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../annotation/api")>();
  return {
    ...actual,
    getAnnotationCapabilities: vi.fn(),
    getAnnotationJob: vi.fn(),
    getAnnotationSegment: vi.fn(),
    getCalibrationProfiles: vi.fn(),
    listAnnotationJobs: vi.fn(),
    mutateAnnotationJob: vi.fn(),
    mutateAnnotationSegment: vi.fn(),
    saveAnnotationDraft: vi.fn(),
    skipAnnotationSegment: vi.fn(),
    submitInitialAnnotation: vi.fn(),
  };
});

const apiMocks = vi.mocked({
  getAnnotationCapabilities,
  getAnnotationJob,
  getAnnotationSegment,
  getCalibrationProfiles,
  listAnnotationJobs,
  mutateAnnotationJob,
  mutateAnnotationSegment,
  saveAnnotationDraft,
  skipAnnotationSegment,
  submitInitialAnnotation,
  getNavigationDatasetDate,
  getNavigationDatasetSummary,
});

const nativeRequest = globalThis.Request;
const nativeEventSource = globalThis.EventSource;

class DataRouterTestRequest {
  readonly url: string;
  readonly method: string;
  readonly signal: AbortSignal;
  readonly headers: Headers;

  constructor(input: RequestInfo | URL, init: RequestInit = {}) {
    this.url = input instanceof DataRouterTestRequest
      ? input.url
      : typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    this.method = init.method ?? "GET";
    this.signal = init.signal ?? new AbortController().signal;
    this.headers = new Headers(init.headers);
  }
}

class AnnotationTestEventSource {
  static instances: AnnotationTestEventSource[] = [];

  onopen: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent<string>) => unknown) | null = null;

  constructor(readonly url: string) {
    AnnotationTestEventSource.instances.push(this);
  }

  close() {}

  emit(event: AnnotationDomainEvent) {
    this.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify(event),
    }));
  }
}

beforeAll(() => {
  vi.stubGlobal("Request", DataRouterTestRequest as unknown as typeof Request);
  vi.stubGlobal("EventSource", AnnotationTestEventSource);
});

afterAll(() => {
  vi.stubGlobal("Request", nativeRequest);
  vi.stubGlobal("EventSource", nativeEventSource);
});

function segmentFixture(
  segmentRef: string,
  ordinal: number,
  overrides: Partial<AnnotationSegmentDetail> = {},
): AnnotationSegmentDetail {
  return {
    segment_ref: segmentRef,
    ordinal,
    source_clip: "20260605_160904",
    status: "draft",
    state_revision: 2,
    draft_revision: 1,
    submitted_revision: null,
    first_frame: {
      url: `/api/annotation/segments/${segmentRef}/first-frame`,
      width: 100,
      height: 80,
      sha256: "b".repeat(64),
      etag: `"${"b".repeat(64)}"`,
    },
    draft: {
      revision: 1,
      targets: [{
        target_ref: `target_${String(ordinal).repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
    skip_reason: null,
    ...overrides,
  };
}

function LocationProbe() {
  return <output data-testid="route-location">{useLocation().pathname}</output>;
}

function renderSegmentRouter(initialPath: string) {
  const router = createMemoryRouter([
    {
      path: "/annotation/jobs/:jobRef",
      element: (
        <>
          <LocationProbe />
          <AnnotationPage />
        </>
      ),
    },
    {
      path: "/annotation/jobs/:jobRef/segments/:segmentRef",
      element: (
        <>
          <LocationProbe />
          <AnnotationPage />
        </>
      ),
    },
  ], {
    initialEntries: [initialPath],
  });
  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);
  return router;
}

function loadPageFirstFrame(width = 100, height = 80): void {
  const image = screen.getByRole("img", { name: /resize 后首帧/ });
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

function jobFixture(overrides: Partial<AnnotationJobDetail> = {}): AnnotationJobDetail {
  return {
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20270605",
    source_clips: ["20260605_160904"],
    status: "waiting_initial_annotation",
    cancel_requested: false,
    completion_outcome: null,
    state_revision: 2,
    calibration: {
      profile_ref: "calibration_0123456789abcdef0123456789abcdef",
      label: "20260529_go2w",
      content_sha256: "a".repeat(64),
    },
    counts: {
      total: 0,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    ready_for_tracking: false,
    ready_for_no_processable_targets: false,
    failure: null,
    created_at: "2026-07-23T00:00:00Z",
    updated_at: "2026-07-23T00:00:00Z",
    segments: [],
    ...overrides,
  };
}

function clipFixture(date: string, clip: string): NavigationClipSummary {
  return {
    date,
    clip,
    duration_ns: 1,
    raw_message_count: 1,
    topics: [],
    has_tmp_dir: true,
    has_sync_data: true,
    sequences: [],
    sync_frame_counts: { image: 1, pointcloud: 1, odom: 1, grid_map: 0 },
    status: "synced",
    errors: [],
  };
}

function dateFixture(date: string, clips: NavigationClipSummary[]): NavigationDateSummary {
  return {
    date,
    clip_count: clips.length,
    total_duration_ns: clips.length,
    raw_message_count: clips.length,
    extracted_clip_count: clips.length,
    synced_clip_count: clips.length,
    sync_frame_counts: { image: clips.length, pointcloud: clips.length, odom: clips.length, grid_map: 0 },
    status: "synced",
    clips,
  };
}

function datasetFixture(dates: NavigationDateSummary[]): NavigationDatasetSummary {
  const clipCount = dates.reduce((total, date) => total + date.clip_count, 0);
  return {
    totals: {
      date_count: dates.length,
      clip_count: clipCount,
      total_duration_ns: clipCount,
      raw_message_count: clipCount,
      extracted_clip_count: clipCount,
      synced_clip_count: clipCount,
    },
    sync_distribution: { image: clipCount, pointcloud: clipCount, odom: clipCount, grid_map: 0 },
    dates,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  AnnotationTestEventSource.instances = [];
  resetAnnotationProjectionStore();
  resetNavigationDatasetSummaryCache();
  apiMocks.getAnnotationCapabilities.mockResolvedValue({
    available: true,
    runtime_id: "navigation_odom_v1",
    reason: null,
  });
  apiMocks.getCalibrationProfiles.mockResolvedValue([]);
  apiMocks.getNavigationDatasetSummary.mockResolvedValue({
    totals: {
      date_count: 0,
      clip_count: 0,
      total_duration_ns: 0,
      raw_message_count: 0,
      extracted_clip_count: 0,
      synced_clip_count: 0,
    },
    sync_distribution: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
    dates: [],
  });
});

test("restores a job detail directly from its URL", async () => {
  const job = jobFixture();
  apiMocks.getAnnotationJob.mockResolvedValue(job);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${job.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "20270605" })).toBeVisible();
  expect(screen.getByText("20260605_160904")).toBeVisible();
  expect(screen.getByText("20260529_go2w")).toBeVisible();
  expect(apiMocks.getAnnotationJob).toHaveBeenCalledWith(job.job_ref);
});

test("does not flash the previous job after the URL switches to another job", async () => {
  const first = jobFixture();
  const second = jobFixture({
    job_ref: "job_11111111111111111111111111111111",
    dataset_date: "20270623",
    source_clips: ["20260623_145550"],
  });
  const secondResponse = deferred<AnnotationJobDetail>();
  apiMocks.getAnnotationJob.mockImplementation((jobRef) => (
    jobRef === first.job_ref ? Promise.resolve(first) : secondResponse.promise
  ));
  const router = createMemoryRouter([
    {
      path: "/annotation/jobs/:jobRef",
      element: <AnnotationPage />,
    },
  ], {
    initialEntries: [`/annotation/jobs/${first.job_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByRole("heading", { name: first.dataset_date })).toBeVisible();
  await act(async () => {
    await router.navigate(`/annotation/jobs/${second.job_ref}`);
  });

  expect(screen.queryByRole("heading", { name: first.dataset_date })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "正在读取任务…" })).toBeVisible();

  await act(async () => {
    secondResponse.resolve(second);
  });
  expect(await screen.findByRole("heading", { name: second.dataset_date })).toBeVisible();
  expect(screen.getByText("20260623_145550")).toBeVisible();
});

test("keeps the task-list projection across a list-detail-list route round trip", async () => {
  const tracked = jobFixture({ status: "tracked", state_revision: 8 });
  apiMocks.listAnnotationJobs.mockResolvedValue([tracked]);
  apiMocks.getAnnotationJob.mockResolvedValue(tracked);
  const router = createMemoryRouter([
    {
      path: "/annotation/jobs",
      element: <AnnotationPage />,
    },
    {
      path: "/annotation/jobs/:jobRef",
      element: <AnnotationPage />,
    },
  ], {
    initialEntries: ["/annotation/jobs"],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  fireEvent.click(await screen.findByRole("button", {
    name: "查看任务 20270605",
  }));
  expect(await screen.findByRole("heading", { name: "Tracking 已完成" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "返回自动标注任务列表" }));
  expect(await screen.findByRole("button", { name: "查看任务 20270605" })).toBeVisible();
  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(1);
});

test("the explicit task refresh bypasses the SPA projection cache", async () => {
  const tracked = jobFixture({ status: "tracked", state_revision: 8 });
  apiMocks.listAnnotationJobs.mockResolvedValue([tracked]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  await screen.findByRole("button", { name: "查看任务 20270605" });
  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "刷新标注任务" }));
  await waitFor(() => expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(2));
  expect(apiMocks.getAnnotationCapabilities).toHaveBeenCalledTimes(2);
  expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalledTimes(2);
});

test("a simulated full browser reload starts with an empty projection cache", async () => {
  const tracked = jobFixture({ status: "tracked", state_revision: 8 });
  apiMocks.listAnnotationJobs.mockResolvedValue([tracked]);
  const renderJobs = () => render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  const first = renderJobs();
  await screen.findByRole("button", { name: "查看任务 20270605" });
  first.unmount();
  resetAnnotationProjectionStore();

  renderJobs();
  await screen.findByRole("button", { name: "查看任务 20270605" });
  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(2);
});

test("resolved initial annotations hand control back to DataPilot without a Web Tracking button", async () => {
  const job = jobFixture({
    ready_for_tracking: true,
    counts: {
      total: 1,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 1,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [
      segmentFixture("segment_11111111111111111111111111111111", 0, {
        status: "submitted",
        submitted_revision: 1,
      }),
    ],
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${job.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "首帧标注已全部提交" })).toBeVisible();
  expect(screen.getByText(/DataPilot 将从原任务继续执行 Tracking 和后处理/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "开始 Tracking" })).not.toBeInTheDocument();
});

test("projects an all-skip completion as no processable targets instead of ordinary cancellation", async () => {
  const job = jobFixture({
    status: "cancelled",
    completion_outcome: "no_processable_targets",
  });
  apiMocks.listAnnotationJobs.mockResolvedValue([job]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("无可处理目标")).toBeVisible();
  expect(screen.queryByText("已取消", { selector: "span" })).not.toBeInTheDocument();
  await waitFor(() => expect(apiMocks.listAnnotationJobs).toHaveBeenCalled());
});

test.skip("M1 list layout is superseded by the M2 DataPilot-owned workspace", async () => {
  const waiting = jobFixture({
    job_ref: "job_11111111111111111111111111111111",
    source_clips: ["waiting_clip"],
    counts: {
      total: 3,
      pending_initial_annotation: 2,
      draft: 1,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
  });
  const running = jobFixture({
    job_ref: "job_22222222222222222222222222222222",
    source_clips: ["running_clip"],
    status: "tracking",
    counts: {
      total: 3,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 0,
      skipped: 0,
      tracking: 3,
      tracked: 0,
    },
  });
  const failed = jobFixture({
    job_ref: "job_33333333333333333333333333333333",
    source_clips: ["failed_clip"],
    status: "failed",
  });
  const tracked = jobFixture({
    job_ref: "job_44444444444444444444444444444444",
    source_clips: ["archived_clip"],
    status: "tracked",
  });
  apiMocks.listAnnotationJobs.mockResolvedValue([waiting, running, failed, tracked]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "需要我处理" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "DataPilot 处理中" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "等待 DataPilot 继续" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "异常任务" })).toBeVisible();
  expect(screen.getByText("waiting_clip")).toBeVisible();
  expect(screen.getByText("running_clip")).toBeVisible();
  expect(screen.getByText("running_clip").closest("[data-testid='annotation-job-row']")).toHaveTextContent("3/3");
  expect(screen.getByText("failed_clip")).toBeVisible();
  expect(screen.getByText("archived_clip")).toBeVisible();
  expect(screen.getAllByText("更新时间")).toHaveLength(5);
  expect(screen.getAllByText(/2026-07-23/)).toHaveLength(4);
  const firstHeader = screen.getAllByTestId("annotation-job-table-header")[0];
  expect(firstHeader).toHaveClass("bg-slate-100/80");
  const firstTable = firstHeader.parentElement?.parentElement;
  expect(firstTable).toHaveClass("mx-4", "border-y");
  expect(firstTable).not.toHaveClass("rounded-lg", "border");
  expect(screen.getByRole("heading", { name: "需要我处理" }).closest("section")).toHaveClass("rounded-lg");
  const firstRow = screen.getAllByTestId("annotation-job-row")[0];
  expect(firstRow).toHaveClass("lg:min-h-11", "py-2");
  expect(firstRow.children[0]).toHaveClass("text-xs", "font-normal", "text-console-muted");
  expect(firstRow.children[1].querySelector("p")).toHaveClass("text-xs", "font-normal", "text-console-muted");
  expect(screen.queryByText(/个 segment 已处理/)).not.toBeInTheDocument();
  const continueAction = screen.getByRole("button", { name: "继续标注 20270605" });
  expect(continueAction).toHaveClass("justify-self-start");
  expect(continueAction).not.toHaveClass("justify-self-end");

  expect(screen.queryByRole("button", { name: /历史任务$/ })).not.toBeInTheDocument();
  const clearFilters = screen.getByRole("button", { name: "清空筛选" });
  expect(clearFilters).toBeDisabled();
  fireEvent.change(screen.getByLabelText("搜索历史任务"), { target: { value: "not-found" } });
  expect(clearFilters).toBeEnabled();
  expect(screen.getByText("没有符合当前筛选条件的历史任务。")).toBeVisible();
  fireEvent.click(clearFilters);
  expect(screen.getByText("archived_clip")).toBeVisible();
  fireEvent.change(screen.getByLabelText("历史任务开始日期"), { target: { value: "2027-07-01" } });
  expect(screen.getByText("没有符合当前筛选条件的历史任务。")).toBeVisible();
  fireEvent.click(clearFilters);
  expect(screen.getByText("archived_clip")).toBeVisible();
});

test.skip("M1 history pagination is superseded by M3 cross-page lifecycle integration", async () => {
  const historyJobs = Array.from({ length: 6 }, (_, index) => jobFixture({
    job_ref: `job_${String(index + 1).padStart(32, "0")}`,
    source_clips: [`history_clip_${index + 1}`],
    status: "tracked",
    updated_at: `2026-07-${String(20 + index).padStart(2, "0")}T00:00:00Z`,
  }));
  apiMocks.listAnnotationJobs.mockResolvedValue(historyJobs);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("history_clip_6")).toBeVisible();
  expect(screen.getByText("history_clip_2")).toBeVisible();
  expect(screen.queryByText("history_clip_1")).not.toBeInTheDocument();
  expect(screen.getByText("共 6 条")).toBeVisible();
  expect(screen.getByText("5 条/页")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "下一页历史任务" }));
  expect(screen.getByText("history_clip_1")).toBeVisible();
  expect(screen.queryByText("history_clip_6")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "前往历史任务第 2 页" })).toHaveAttribute("aria-current", "page");
});

test.skip("M1 history empty-state is superseded by the M2 grouped workspace", async () => {
  apiMocks.listAnnotationJobs.mockResolvedValue([jobFixture()]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("暂无历史任务")).toBeVisible();
  expect(screen.queryByLabelText("搜索历史任务")).not.toBeInTheDocument();
});

test.skip("M1 direct job creation is intentionally hidden from the M2 product UI", async () => {
  const date = dateFixture("20270605", [clipFixture("20270605", "20260605_160904")]);
  apiMocks.listAnnotationJobs.mockResolvedValue([]);
  apiMocks.getNavigationDatasetSummary.mockResolvedValue(datasetFixture([date]));
  apiMocks.getNavigationDatasetDate.mockResolvedValue(date);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  await screen.findByText("还没有自动标注任务");
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  const dialog = screen.getByRole("dialog", { name: "创建导航自动标注任务" });
  expect(dialog).toBeVisible();
  const closeButton = screen.getByRole("button", { name: "关闭创建任务" });
  expect(closeButton.className).not.toContain("ring-console-cyan");
  expect(document.activeElement).not.toBe(closeButton);

  fireEvent.mouseDown(screen.getByTestId("create-annotation-job-overlay"), { button: 0 });
  expect(screen.getByRole("dialog", { name: "创建导航自动标注任务" })).toBeVisible();
  expect(screen.getByTestId("create-annotation-job-dialog").className).toContain("navigation-dialog-attention-a");

  fireEvent.change(screen.getByLabelText("自动标注数据日期"), { target: { value: date.date } });
  expect(await screen.findByText("20260605_160904")).toBeVisible();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.getByRole("dialog", { name: "创建导航自动标注任务" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

test.skip("M1 create-runtime banner is superseded by the DataPilot handoff banner", async () => {
  apiMocks.listAnnotationJobs.mockResolvedValue([]);
  apiMocks.getAnnotationCapabilities.mockResolvedValue({
    available: false,
    runtime_id: "navigation_odom_v1",
    reason: {
      code: "unexpected_internal_code",
      message: "Xvfb and sandbox failed at /private/runtime/path.",
      error_ref: "annotation_error_1234567890abcdef",
    },
  });

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText(/尚未通过部署预检/)).toBeVisible();
  expect(screen.getByText(/annotation_error_1234567890abcdef/)).toBeVisible();
  expect(screen.queryByText(/Xvfb|sandbox|private\/runtime/i)).not.toBeInTheDocument();
});

test("waits for the current draft save before changing the segment URL", async () => {
  const firstRef = "segment_11111111111111111111111111111111";
  const secondRef = "segment_22222222222222222222222222222222";
  const firstSegment = segmentFixture(firstRef, 1);
  const secondSegment = segmentFixture(secondRef, 2);
  const job = jobFixture({
    counts: {
      total: 2,
      pending_initial_annotation: 0,
      draft: 2,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [firstSegment, secondSegment],
  });
  const save = deferred<AnnotationSegmentDetail>();
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockImplementation(async (_jobRef, segmentRef) => (
    segmentRef === firstRef ? firstSegment : secondSegment
  ));
  apiMocks.saveAnnotationDraft.mockReturnValue(save.promise);

  renderSegmentRouter(`/annotation/jobs/${job.job_ref}/segments/${firstRef}`);
  await screen.findByRole("application", { name: "首帧标注画布" });
  loadPageFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: /Segment 02/ }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  expect(screen.getByTestId("route-location")).toHaveTextContent(firstRef);

  save.resolve(segmentFixture(firstRef, 1, {
    state_revision: 3,
    draft_revision: 2,
    draft: {
      revision: 2,
      targets: apiMocks.saveAnnotationDraft.mock.calls[0][2].targets,
    },
  }));
  await waitFor(() => expect(screen.getByTestId("route-location")).toHaveTextContent(secondRef));
});

test("skips with the revision produced by this page's flush instead of adopting unseen server changes", async () => {
  const segmentRef = "segment_12121212121212121212121212121212";
  const initial = segmentFixture(segmentRef, 1);
  const saved = segmentFixture(segmentRef, 1, {
    state_revision: 3,
    draft_revision: 2,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"1".repeat(32)}`,
        bbox: [11, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });
  const unseenServerChange = segmentFixture(segmentRef, 1, {
    state_revision: 99,
    draft_revision: 42,
  });
  const skipped = segmentFixture(segmentRef, 1, {
    status: "skipped",
    state_revision: 4,
    draft_revision: 2,
    skip_reason: { reason_code: "no_valid_target", note: null },
  });
  const job = jobFixture({
    counts: {
      total: 1,
      pending_initial_annotation: 0,
      draft: 1,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [initial],
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment
    .mockResolvedValueOnce(initial)
    .mockResolvedValue(unseenServerChange);
  apiMocks.saveAnnotationDraft.mockResolvedValue(saved);
  apiMocks.skipAnnotationSegment.mockResolvedValue(skipped);

  renderSegmentRouter(`/annotation/jobs/${job.job_ref}/segments/${segmentRef}`);
  await screen.findByRole("application", { name: "首帧标注画布" });
  loadPageFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "跳过此 Segment" }));
  fireEvent.click(screen.getByRole("button", { name: "确认跳过" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(apiMocks.skipAnnotationSegment).toHaveBeenCalledWith(
    job.job_ref,
    segmentRef,
    3,
    "no_valid_target",
    "",
  ));
  expect(apiMocks.getAnnotationSegment).toHaveBeenCalledTimes(1);
});

test("shows no-processable-targets on the durable detail URL", async () => {
  const job = jobFixture({
    status: "cancelled",
    completion_outcome: "no_processable_targets",
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${job.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("无可处理目标")).toBeVisible();
  expect(screen.queryByText("已取消")).not.toBeInTheDocument();
});

test("keeps the latest segment CAS revisions after submit and reopen on the same URL", async () => {
  const segmentRef = "segment_33333333333333333333333333333333";
  const initial = segmentFixture(segmentRef, 1);
  const submitted = segmentFixture(segmentRef, 1, {
    status: "submitted",
    state_revision: 3,
    draft_revision: 1,
    submitted_revision: 1,
  });
  const reopened = segmentFixture(segmentRef, 1, {
    status: "draft",
    state_revision: 4,
    draft_revision: 1,
    submitted_revision: 1,
  });
  const job = jobFixture({
    counts: {
      total: 1,
      pending_initial_annotation: 0,
      draft: 1,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [initial],
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockResolvedValue(initial);
  apiMocks.submitInitialAnnotation.mockResolvedValue(submitted);
  apiMocks.mutateAnnotationSegment.mockResolvedValue(reopened);
  apiMocks.saveAnnotationDraft.mockImplementation(async (_jobRef, _segmentRef, body) => (
    segmentFixture(segmentRef, 1, {
      status: "draft",
      state_revision: body.expected_segment_revision + 1,
      draft_revision: (body.expected_draft_revision ?? 0) + 1,
      submitted_revision: 1,
      draft: {
        revision: (body.expected_draft_revision ?? 0) + 1,
        targets: body.targets,
      },
    })
  ));

  renderSegmentRouter(`/annotation/jobs/${job.job_ref}/segments/${segmentRef}`);

  await screen.findByRole("application", { name: "首帧标注画布" });
  loadPageFirstFrame();
  fireEvent.click(screen.getByRole("button", { name: "提交首帧标注" }));
  await waitFor(() => expect(apiMocks.submitInitialAnnotation).toHaveBeenCalledWith(
    job.job_ref,
    segmentRef,
    2,
    1,
  ));
  expect(await screen.findByRole("button", { name: "重新编辑" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "重新编辑" }));
  await waitFor(() => expect(apiMocks.mutateAnnotationSegment).toHaveBeenCalledWith(
    job.job_ref,
    segmentRef,
    "reopen",
    3,
  ));
  loadPageFirstFrame();
  await waitFor(() => expect(screen.getByLabelText("master bbox x")).toBeEnabled());

  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "12" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  expect(apiMocks.saveAnnotationDraft.mock.calls[0][2]).toMatchObject({
    expected_segment_revision: 4,
    expected_draft_revision: 1,
  });
});

test("keeps the external-submit notice visible after the readonly workbench remounts", async () => {
  const segmentRef = "segment_34343434343434343434343434343434";
  const initial = segmentFixture(segmentRef, 1, {
    draft: {
      revision: 1,
      targets: [{
        target_ref: `target_${"3".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "black", lower: "black", shoes: "black" },
      }],
    },
  });
  const submitted = segmentFixture(segmentRef, 1, {
    status: "submitted",
    state_revision: 3,
    draft_revision: 2,
    submitted_revision: 1,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"3".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "black", lower: "gray", shoes: "black" },
      }],
    },
  });
  const initialJob = jobFixture({
    counts: {
      total: 1,
      pending_initial_annotation: 0,
      draft: 1,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [initial],
  });
  const submittedJob = jobFixture({
    state_revision: 3,
    counts: {
      total: 1,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 1,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [submitted],
  });
  apiMocks.getAnnotationJob
    .mockResolvedValueOnce(initialJob)
    .mockResolvedValue(submittedJob);
  apiMocks.getAnnotationSegment.mockResolvedValue(initial);
  apiMocks.submitInitialAnnotation.mockRejectedValue(new AnnotationApiError(
    "The annotation segment changed; refresh before retrying.",
    409,
    {
      code: "segment_revision_conflict",
      message: "The annotation segment changed; refresh before retrying.",
      current: submitted,
    },
  ));

  renderSegmentRouter(`/annotation/jobs/${initialJob.job_ref}/segments/${segmentRef}`);

  await screen.findByRole("application", { name: "首帧标注画布" });
  loadPageFirstFrame();
  fireEvent.click(screen.getByRole("button", { name: "提交首帧标注" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "已在其他页面完成提交。本页内容未再次提交，现已切换到服务器版本。",
  );
  expect(screen.getByLabelText("master 裤子颜色")).toHaveValue("gray");
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
  expect(apiMocks.submitInitialAnnotation).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("The annotation segment changed; refresh before retrying.")).not.toBeInTheDocument();
});

test("submitted segments treat a readonly flush as successful for queue and return navigation", async () => {
  const firstRef = "segment_44444444444444444444444444444444";
  const secondRef = "segment_55555555555555555555555555555555";
  const firstSegment = segmentFixture(firstRef, 1, {
    status: "submitted",
    state_revision: 3,
    submitted_revision: 1,
  });
  const secondSegment = segmentFixture(secondRef, 2, {
    status: "submitted",
    state_revision: 5,
    draft_revision: 2,
    submitted_revision: 2,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"5".repeat(32)}`,
        bbox: [30, 10, 20, 20],
        point: [35, 15],
        colors: { upper: "blue", lower: "black", shoes: "white" },
      }],
    },
  });
  const job = jobFixture({
    counts: {
      total: 2,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 2,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [firstSegment, secondSegment],
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockImplementation(async (_jobRef, segmentRef) => (
    segmentRef === firstRef ? firstSegment : secondSegment
  ));
  const router = renderSegmentRouter(
    `/annotation/jobs/${job.job_ref}/segments/${firstRef}`,
  );

  await screen.findByRole("application", { name: "首帧标注画布" });
  const compactSelector = screen.getByLabelText("切换 Segment");
  expect(compactSelector).toHaveValue(firstRef);
  expect(screen.getByRole("heading", { name: "Segment 队列" }).closest("aside")).toHaveClass(
    "hidden",
    "xl:flex",
  );
  expect(screen.getByTestId("annotation-studio-shell")).toHaveClass(
    "xl:grid",
    "xl:grid-cols-[15rem_minmax(0,1fr)]",
  );
  fireEvent.change(compactSelector, { target: { value: secondRef } });
  await waitFor(() => expect(router.state.location.pathname).toContain(secondRef));
  expect(await screen.findByText("Segment 02 / 2")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "返回自动标注任务" }));
  await waitFor(() => expect(router.state.location.pathname).toBe(
    `/annotation/jobs/${job.job_ref}`,
  ));
  expect(await screen.findByRole("heading", { name: "20270605" })).toBeVisible();
  expect(apiMocks.saveAnnotationDraft).not.toHaveBeenCalled();
});

test("ignores a late segment response after the URL has switched to another segment", async () => {
  const firstRef = "segment_66666666666666666666666666666666";
  const secondRef = "segment_77777777777777777777777777777777";
  const firstSegment = segmentFixture(firstRef, 1);
  const secondSegment = segmentFixture(secondRef, 2, {
    state_revision: 4,
    draft_revision: 3,
    draft: {
      revision: 3,
      targets: [{
        target_ref: `target_${"7".repeat(32)}`,
        bbox: [30, 10, 20, 20],
        point: [35, 15],
        colors: { upper: "blue", lower: "black", shoes: "white" },
      }],
    },
  });
  const firstResponse = deferred<AnnotationSegmentDetail>();
  const secondResponse = deferred<AnnotationSegmentDetail>();
  const job = jobFixture({
    counts: {
      total: 2,
      pending_initial_annotation: 0,
      draft: 2,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
    },
    segments: [firstSegment, secondSegment],
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockImplementation((_jobRef, segmentRef) => (
    segmentRef === firstRef ? firstResponse.promise : secondResponse.promise
  ));
  const router = renderSegmentRouter(
    `/annotation/jobs/${job.job_ref}/segments/${firstRef}`,
  );

  await act(async () => {
    await router.navigate(`/annotation/jobs/${job.job_ref}/segments/${secondRef}`);
  });
  await act(async () => {
    secondResponse.resolve(secondSegment);
  });
  expect(await screen.findByText("Segment 02 / 2")).toBeVisible();
  expect(screen.getByLabelText("master bbox x")).toHaveValue(30);

  await act(async () => {
    firstResponse.resolve(firstSegment);
    await Promise.resolve();
  });
  expect(router.state.location.pathname).toContain(secondRef);
  expect(screen.getByText("Segment 02 / 2")).toBeVisible();
  expect(screen.getByLabelText("master bbox x")).toHaveValue(30);
});

test("failed jobs can be explicitly abandoned to release their source scope", async () => {
  const failed = jobFixture({
    status: "failed",
    state_revision: 7,
    failure: {
      code: "runtime_failed",
      message: "Runtime failed.",
      retryable: false,
      error_ref: "error_public_ref",
    },
  });
  const cancelled = jobFixture({
    status: "cancelled",
    state_revision: 8,
  });
  apiMocks.getAnnotationJob.mockResolvedValue(failed);
  apiMocks.mutateAnnotationJob.mockResolvedValue(cancelled);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${failed.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "放弃任务" }));
  await waitFor(() => expect(apiMocks.mutateAnnotationJob).toHaveBeenCalledWith(
    failed.job_ref,
    "cancel",
    7,
  ));
  expect(await screen.findByText("已取消")).toBeVisible();
});

test("recovery-required jobs stay quarantined until an operator confirms safety", async () => {
  const failed = jobFixture({
    status: "failed",
    state_revision: 7,
    failure: {
      code: "recovery_required",
      message: "Runtime recovery requires an operator safety check.",
      retryable: false,
      error_ref: "error_recovery_ref",
    },
  });
  apiMocks.getAnnotationJob.mockResolvedValue(failed);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${failed.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("任务处于恢复隔离状态")).toBeVisible();
  expect(screen.getByText(/错误参考：error_recovery_ref/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "放弃任务" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试失败阶段" })).not.toBeInTheDocument();
  expect(apiMocks.mutateAnnotationJob).not.toHaveBeenCalled();
});

test("a persisted running cancellation is visible and blocks repeated mutations", async () => {
  const cancelling = jobFixture({
    status: "tracking",
    cancel_requested: true,
    state_revision: 8,
  });
  apiMocks.getAnnotationJob.mockResolvedValue(cancelling);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${cancelling.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("status")).toHaveTextContent("正在取消任务");
  expect(screen.getByText(/不会释放该任务的数据范围/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "取消任务" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "开始 Tracking" })).not.toBeInTheDocument();
  expect(apiMocks.mutateAnnotationJob).not.toHaveBeenCalled();
});

test("a late stale event refresh cannot hide a newer persisted cancellation", async () => {
  const tracking = jobFixture({
    status: "tracking",
    cancel_requested: false,
    state_revision: 7,
  });
  const cancelling = jobFixture({
    status: "tracking",
    cancel_requested: true,
    state_revision: 8,
  });
  const stalePoll = deferred<AnnotationJobDetail>();
  apiMocks.getAnnotationJob
    .mockResolvedValueOnce(tracking)
    .mockReturnValue(stalePoll.promise);
  apiMocks.mutateAnnotationJob.mockResolvedValue(cancelling);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${tracking.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <AnnotationDomainEventBridge />
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("button", { name: "取消任务" })).toBeVisible();
  await waitFor(() => expect(AnnotationTestEventSource.instances).toHaveLength(1));
  act(() => {
    AnnotationTestEventSource.instances[0].emit({
      seq: 1,
      event_ref: "annotation_event_1",
      event_kind: "annotation.job.changed",
      aggregate_kind: "job",
      job_ref: tracking.job_ref,
      state_revision: 8,
      status: "tracking",
      occurred_at: "2026-07-29T00:00:00Z",
    });
  });
  await waitFor(() => expect(apiMocks.getAnnotationJob).toHaveBeenCalledTimes(2), {
    timeout: 1_000,
  });
  fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
  expect(await screen.findByRole("status")).toHaveTextContent("正在取消任务");

  await act(async () => {
    stalePoll.resolve(tracking);
    await Promise.resolve();
  });

  expect(screen.getByRole("status")).toHaveTextContent("正在取消任务");
  expect(screen.queryByRole("button", { name: "取消任务" })).not.toBeInTheDocument();
});

test.each([
  ["cancelled", false],
  ["failed", false],
  ["tracking", true],
] as const)("a %s job with cancel=%s keeps a draft segment read-only", async (
  status,
  cancelRequested,
) => {
  const segmentRef = "segment_89898989898989898989898989898989";
  const segment = segmentFixture(segmentRef, 1);
  const job = jobFixture({
    status,
    cancel_requested: cancelRequested,
    failure: status === "failed"
      ? {
          code: "runtime_failed",
          message: "Runtime failed.",
          retryable: false,
          error_ref: "error_public_ref",
        }
      : null,
    segments: [segment],
  });
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockResolvedValue(segment);

  renderSegmentRouter(`/annotation/jobs/${job.job_ref}/segments/${segmentRef}`);

  expect(await screen.findByRole("application", { name: "首帧标注画布" })).toBeVisible();
  loadPageFirstFrame();
  expect(screen.getByLabelText("master bbox x")).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "跳过此 Segment" })).not.toBeInTheDocument();
});

test("tracked jobs cannot be cancelled", async () => {
  const tracked = jobFixture({ status: "tracked" });
  apiMocks.getAnnotationJob.mockResolvedValue(tracked);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${tracked.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Tracking 已完成" })).toBeVisible();
  expect(screen.queryByRole("button", { name: /取消任务|放弃任务/ })).not.toBeInTheDocument();
});

test("a tracked job refreshes into postprocessing when the tab returns to the foreground", async () => {
  const tracked = jobFixture({ status: "tracked", state_revision: 8 });
  const postprocessing = jobFixture({ status: "postprocessing", state_revision: 9 });
  apiMocks.getAnnotationJob
    .mockResolvedValueOnce(tracked)
    .mockResolvedValue(postprocessing);

  render(
    <MemoryRouter
      initialEntries={[`/annotation/jobs/${tracked.job_ref}`]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <AnnotationDomainEventBridge />
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Tracking 已完成" })).toBeVisible();
  fireEvent.focus(window);

  expect(await screen.findByRole("heading", { name: "DataPilot 正在执行后处理" })).toBeVisible();
});

test.skip("M1 direct-create refresh is superseded by the shared DataPilot selection dialog", async () => {
  const firstClip = clipFixture("20270605", "20270605_160904");
  const refreshedClip = clipFixture("20270605", "20270605_152930");
  const initialDate = dateFixture("20270605", [firstClip]);
  const refreshedDate = dateFixture("20270605", [refreshedClip]);
  apiMocks.listAnnotationJobs.mockResolvedValue([]);
  apiMocks.getNavigationDatasetSummary
    .mockResolvedValueOnce(datasetFixture([initialDate]))
    .mockResolvedValueOnce(datasetFixture([refreshedDate]));
  apiMocks.getNavigationDatasetDate
    .mockResolvedValueOnce(initialDate)
    .mockResolvedValueOnce(refreshedDate);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  await screen.findByText("还没有自动标注任务");
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  fireEvent.change(screen.getByLabelText("自动标注数据日期"), { target: { value: "20270605" } });
  expect(await screen.findByText(firstClip.clip)).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  fireEvent.click(screen.getByRole("button", { name: "刷新自动标注任务" }));
  await waitFor(() => expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  expect(await screen.findByText(refreshedClip.clip)).toBeVisible();
  expect(screen.queryByText(firstClip.clip)).not.toBeInTheDocument();
  expect(apiMocks.getNavigationDatasetDate).toHaveBeenCalledTimes(2);
  expect(apiMocks.getAnnotationCapabilities).toHaveBeenCalledTimes(2);
  expect(apiMocks.getCalibrationProfiles).toHaveBeenCalledTimes(2);
});

test.skip("M2 removes processing calibration selection from the ordinary Web UI", async () => {
  const dateA = dateFixture("20270605", [clipFixture("20270605", "20260605_160904")]);
  const dateB = dateFixture("20270623", [clipFixture("20270623", "20260623_145550")]);
  apiMocks.listAnnotationJobs.mockResolvedValue([]);
  apiMocks.getNavigationDatasetSummary.mockResolvedValue(datasetFixture([dateA, dateB]));
  apiMocks.getNavigationDatasetDate.mockImplementation(async (date) => (
    date === dateA.date ? dateA : dateB
  ));
  apiMocks.getCalibrationProfiles.mockResolvedValue([{
    profile_ref: "20260529_go2w",
    label: "20260529_go2w",
    content_sha256: "a".repeat(64),
  }]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  await screen.findByText("还没有自动标注任务");
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  fireEvent.change(screen.getByLabelText("自动标注数据日期"), {
    target: { value: dateA.date },
  });
  await screen.findByText("20260605_160904");
  fireEvent.change(screen.getByLabelText("当天处理标定"), {
    target: { value: "20260529_go2w" },
  });
  expect(screen.getByLabelText("当天处理标定")).toHaveValue("20260529_go2w");

  fireEvent.change(screen.getByLabelText("自动标注数据日期"), {
    target: { value: dateB.date },
  });

  expect(screen.getByLabelText("当天处理标定")).toHaveValue("");
  expect(await screen.findByText("20260623_145550")).toBeVisible();
});

test.skip("M2 delegates clip switching to the shared DataPilot selection dialog", async () => {
  const dateA = dateFixture("20270605", [clipFixture("20270605", "20270605_160904")]);
  const dateB = dateFixture("20270623", [clipFixture("20270623", "20270623_145550")]);
  const firstResponse = deferred<NavigationDateSummary>();
  const secondResponse = deferred<NavigationDateSummary>();
  apiMocks.listAnnotationJobs.mockResolvedValue([]);
  apiMocks.getNavigationDatasetSummary.mockResolvedValue(datasetFixture([dateA, dateB]));
  apiMocks.getNavigationDatasetDate
    .mockImplementationOnce(() => firstResponse.promise)
    .mockImplementationOnce(() => secondResponse.promise);

  render(
    <MemoryRouter
      initialEntries={["/annotation/jobs"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/jobs" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  await screen.findByText("还没有自动标注任务");
  fireEvent.click(screen.getByRole("button", { name: "新建任务" }));
  fireEvent.change(screen.getByLabelText("自动标注数据日期"), { target: { value: dateA.date } });
  fireEvent.change(screen.getByLabelText("自动标注数据日期"), { target: { value: dateB.date } });

  await act(async () => {
    secondResponse.resolve(dateB);
  });
  expect(await screen.findByText("20270623_145550")).toBeVisible();
  await act(async () => {
    firstResponse.resolve(dateA);
    await Promise.resolve();
  });
  expect(screen.getByText("20270623_145550")).toBeVisible();
  expect(screen.queryByText("20270605_160904")).not.toBeInTheDocument();
  expect(screen.getByLabelText("自动标注数据日期")).toHaveValue(dateB.date);
});
