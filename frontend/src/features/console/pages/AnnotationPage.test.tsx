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

beforeAll(() => {
  vi.stubGlobal("Request", DataRouterTestRequest as unknown as typeof Request);
});

afterAll(() => {
  vi.stubGlobal("Request", nativeRequest);
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
  expect(screen.queryByText("已取消")).not.toBeInTheDocument();
  await waitFor(() => expect(apiMocks.listAnnotationJobs).toHaveBeenCalled());
});

test("does not render technical capability details from the public response", async () => {
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
  expect(screen.getByRole("heading", { name: "Segment 队列" }).closest("section")).toHaveClass(
    "hidden",
    "xl:block",
  );
  fireEvent.change(compactSelector, { target: { value: secondRef } });
  await waitFor(() => expect(router.state.location.pathname).toContain(secondRef));
  expect(await screen.findByRole("heading", { name: "Segment 02" })).toBeVisible();

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
  expect(await screen.findByRole("heading", { name: "Segment 02" })).toBeVisible();
  expect(screen.getByLabelText("master bbox x")).toHaveValue(30);

  await act(async () => {
    firstResponse.resolve(firstSegment);
    await Promise.resolve();
  });
  expect(router.state.location.pathname).toContain(secondRef);
  expect(screen.getByRole("heading", { name: "Segment 02" })).toBeVisible();
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

test("a late stale poll cannot hide a newer persisted cancellation", async () => {
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
      <Routes>
        <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("button", { name: "取消任务" })).toBeVisible();
  await waitFor(() => expect(apiMocks.getAnnotationJob).toHaveBeenCalledTimes(2), {
    timeout: 4_000,
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

test("explicit refresh invalidates the dataset summary cache and reloads selected-date clips", async () => {
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

  fireEvent.click(screen.getByRole("button", { name: "刷新自动标注任务" }));
  await waitFor(() => expect(apiMocks.getNavigationDatasetSummary).toHaveBeenCalledTimes(2));
  expect(await screen.findByText(refreshedClip.clip)).toBeVisible();
  expect(screen.queryByText(firstClip.clip)).not.toBeInTheDocument();
  expect(apiMocks.getNavigationDatasetDate).toHaveBeenCalledTimes(2);
  expect(apiMocks.getAnnotationCapabilities).toHaveBeenCalledTimes(2);
  expect(apiMocks.getCalibrationProfiles).toHaveBeenCalledTimes(2);
});

test("changing the data date requires a fresh processing calibration choice", async () => {
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

test("ignores a late clip response after the user switches from date A to date B", async () => {
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
