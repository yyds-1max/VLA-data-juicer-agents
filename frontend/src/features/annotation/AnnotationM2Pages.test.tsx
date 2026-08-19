import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  createMemoryRouter,
  MemoryRouter,
  Route,
  RouterProvider,
  Routes,
} from "react-router-dom";

import {
  applyFixCommand,
  createFixRevision,
  getAnnotationCapabilities,
  getCalibrationProfiles,
  getTrajectoryReview,
  getTrajectoryReviewEvidence,
  listTrajectoryReviews,
} from "./api";
import type { AnnotationDomainEvent } from "./events";
import { AnnotationDomainEventBridge } from "./AnnotationDomainEventBridge";
import { AnnotationReviewsPage } from "./AnnotationReviewsPage";
import { AnnotationWorkspaceLayout } from "./AnnotationWorkspaceLayout";
import {
  cacheTrajectoryReview,
  resetAnnotationProjectionStore,
} from "./projectionStore";
import { TrajectoryFixPage } from "./TrajectoryFixPage";
import type {
  TrajectoryReview,
  TrajectoryReviewEvidence,
} from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    applyFixCommand: vi.fn(),
    createFixRevision: vi.fn(),
    getAnnotationCapabilities: vi.fn(),
    getCalibrationProfiles: vi.fn(),
    getTrajectoryReview: vi.fn(),
    getTrajectoryReviewEvidence: vi.fn(),
    listTrajectoryReviews: vi.fn(),
  };
});

const apiMocks = vi.mocked({
  applyFixCommand,
  createFixRevision,
  getAnnotationCapabilities,
  getCalibrationProfiles,
  getTrajectoryReview,
  getTrajectoryReviewEvidence,
  listTrajectoryReviews,
});

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

const review: TrajectoryReview = {
  review_ref: "review_0123456789abcdef0123456789abcdef",
  status: "pending",
  state_revision: 1,
  job_ref: "job_0123456789abcdef0123456789abcdef",
  dataset_date: "20270605",
  source_clip: "20260605_160904",
  segment_ref: "segment_0123456789abcdef0123456789abcdef",
  segment_ordinal: 1,
  trajectory_revision: {
    revision_ref: "trajectory_revision_0123456789abcdef0123456789abcdef",
    content_sha256: "a".repeat(64),
  },
  processing_calibration: {
    profile_ref: "20260529_go2w",
    label: "20260529_go2w",
    content_sha256: "b".repeat(64),
  },
  fix_draft: null,
  fix_revisions: [],
  active_fix_run: null,
  fix_failure: null,
  latest_publication: null,
  created_at: "2026-07-28T00:00:00Z",
  updated_at: "2026-07-28T00:00:00Z",
};

function evidenceFor(owner: TrajectoryReview): TrajectoryReviewEvidence {
  return {
    availability: "available",
    review_ref: owner.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: owner.trajectory_revision.revision_ref,
    review_state_revision: owner.state_revision,
    draft_revision: owner.fix_draft?.revision ?? null,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: null,
      projection: null,
      gridmap: null,
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: [1, 2],
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    }],
    draft_commands: [],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  AnnotationTestEventSource.instances = [];
  resetAnnotationProjectionStore();
  vi.stubGlobal("EventSource", AnnotationTestEventSource);
  apiMocks.getCalibrationProfiles.mockResolvedValue([]);
  apiMocks.getAnnotationCapabilities.mockResolvedValue({
    available: true,
    runtime_id: "navigation_odom_v1",
    reason: null,
  });
  apiMocks.listTrajectoryReviews.mockResolvedValue([]);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("annotation workspace exposes URL-backed workbench and review tabs", async () => {
  const router = createMemoryRouter([
    {
      path: "/annotation",
      element: <AnnotationWorkspaceLayout />,
      children: [
        { path: "jobs", element: <div>jobs route</div> },
        { path: "reviews", element: <div>reviews route</div> },
      ],
    },
  ], { initialEntries: ["/annotation/jobs"] });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(screen.getByRole("tab", { name: "标注任务" })).toHaveAttribute(
    "data-state",
    "active",
  );
  expect(screen.getByRole("tab", { name: "人工复核" })).toBeVisible();
  expect(await screen.findByText("处理环境可用")).toBeVisible();
  expect(screen.getByTestId("annotation-workspace-switcher")).not.toHaveClass("border-b");
  expect(screen.getByText("jobs route")).toBeVisible();
});

test("annotation workspace hides the list switcher on the Fix workbench route", async () => {
  const router = createMemoryRouter([
    {
      path: "/annotation",
      element: <AnnotationWorkspaceLayout />,
      children: [
        { path: "reviews/:reviewRef", element: <div>fix route</div> },
      ],
    },
  ], { initialEntries: ["/annotation/reviews/review_0123456789abcdef0123456789abcdef"] });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);
  await act(async () => {
    await Promise.resolve();
  });

  expect(screen.getByText("fix route")).toBeVisible();
  expect(screen.queryByTestId("annotation-workspace-switcher")).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "标注任务" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "人工复核" })).not.toBeInTheDocument();
});

test("review list aggregates all clips for a date and moves clip details into the popover", async () => {
  apiMocks.listTrajectoryReviews.mockResolvedValue([
    review,
    {
      ...review,
      review_ref: "review_11111111111111111111111111111111",
      segment_ref: "segment_11111111111111111111111111111111",
      source_clip: "20260605_170102",
      segment_ordinal: 1,
      status: "returned",
    },
  ]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/reviews"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/reviews" element={<AnnotationReviewsPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("0/2")).toBeVisible();
  expect(screen.getAllByRole("columnheader")).toHaveLength(6);
  expect(screen.queryByRole("columnheader", { name: /外层 clip/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "复核任务" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "进入人工 Fix" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", {
    name: /查看 20270605 20260605_160904、20260605_170102 复核详情/,
  }));
  const popover = await screen.findByRole("dialog");
  expect(within(popover).getByText("2 个复核单元")).toBeVisible();
  expect(within(popover).getByText("20260605_160904")).toBeVisible();
  expect(within(popover).getByText("20260605_170102")).toBeVisible();
  expect(within(popover).getByText("尚未选择")).toBeVisible();
  expect(screen.queryByRole("button", { name: /交给 DataPilot 复核/ })).not.toBeInTheDocument();
});

test("Fix workbench provides a clip-grouped Segment queue across active and terminal reviews", async () => {
  const terminalReview: TrajectoryReview = {
    ...review,
    review_ref: "review_11111111111111111111111111111111",
    segment_ref: "segment_11111111111111111111111111111111",
    segment_ordinal: 2,
    status: "approved",
    state_revision: 3,
    latest_publication: {
      fix_revision_ref: "fix_revision_11111111111111111111111111111111",
      attempt: 1,
      status: "published",
      content_sha256: "d".repeat(64),
      failure: null,
      created_at: "2026-07-28T00:01:00Z",
    },
  };
  apiMocks.listTrajectoryReviews.mockResolvedValue([terminalReview, review]);
  apiMocks.getTrajectoryReview.mockImplementation(async (reviewRef) => (
    reviewRef === terminalReview.review_ref ? terminalReview : review
  ));
  apiMocks.getTrajectoryReviewEvidence.mockImplementation(async (reviewRef) => (
    evidenceFor(reviewRef === terminalReview.review_ref ? terminalReview : review)
  ));
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByRole("button", { name: "当前 Segment 01，待复核" })).toBeVisible();
  const secondSegment = screen.getByRole("button", { name: "打开 Segment 02，已验证" });
  expect(secondSegment).toHaveTextContent("已验证");
  expect(screen.queryByText(terminalReview.segment_ref)).not.toBeInTheDocument();
});

test("Fix workbench keeps sibling Segment statuses synchronized with the shared projection", async () => {
  const sibling: TrajectoryReview = {
    ...review,
    review_ref: "review_11111111111111111111111111111111",
    segment_ref: "segment_11111111111111111111111111111111",
    segment_ordinal: 2,
  };
  apiMocks.listTrajectoryReviews.mockResolvedValue([review, sibling]);
  apiMocks.getTrajectoryReview.mockResolvedValue(review);
  apiMocks.getTrajectoryReviewEvidence.mockResolvedValue(evidenceFor(review));
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  const siblingButton = await screen.findByRole("button", {
    name: "打开 Segment 02，待复核",
  });
  expect(siblingButton).toHaveTextContent("待复核");

  act(() => {
    cacheTrajectoryReview({
      ...sibling,
      status: "returned",
      state_revision: 2,
      updated_at: "2026-07-28T00:01:00Z",
    });
  });

  await waitFor(() => expect(siblingButton).toHaveTextContent("已退回"));
});

test("only a successfully published approved review is counted and labelled as verified", async () => {
  const approved = (suffix: string, publicationStatus: "publishing" | "published" | "failed") => ({
    ...review,
    review_ref: `review_${suffix.repeat(32)}`,
    segment_ref: `segment_${suffix.repeat(32)}`,
    source_clip: `20260605_16090${suffix}`,
    segment_ordinal: Number(suffix),
    status: "approved" as const,
    latest_publication: {
      fix_revision_ref: `fix_revision_${suffix.repeat(32)}`,
      attempt: 1,
      status: publicationStatus,
      content_sha256: publicationStatus === "published" ? suffix.repeat(64) : null,
      failure: publicationStatus === "failed"
        ? { code: "publication_failed", error_ref: null }
        : null,
      created_at: "2026-07-28T00:01:00Z",
    },
  });
  apiMocks.listTrajectoryReviews.mockResolvedValue([
    approved("1", "publishing"),
    approved("2", "failed"),
    approved("3", "published"),
  ]);

  render(
    <MemoryRouter
      initialEntries={["/annotation/reviews"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/reviews" element={<AnnotationReviewsPage />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("button", { name: /已验证\s*1/ })).toBeVisible();
  expect(screen.getByText("已批准/发布失败")).toBeVisible();
  expect(screen.queryByText("已验证 3")).not.toBeInTheDocument();
});

test("Fix workbench fails closed when the public evidence API is unavailable", async () => {
  apiMocks.getTrajectoryReview.mockResolvedValue(review);
  apiMocks.getTrajectoryReviewEvidence.mockRejectedValue(new Error("404 Not Found"));
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByRole("heading", { name: "轨迹证据不可用" })).toBeVisible();
  expect(screen.getByText(/不会构造替代数据/)).toBeVisible();
  await waitFor(() => expect(apiMocks.getTrajectoryReviewEvidence).toHaveBeenCalledWith(
    review.review_ref,
  ));
  expect(screen.queryByRole("button", { name: /交给 DataPilot 复核/ })).not.toBeInTheDocument();
});

test("Fix workbench rejects evidence bound to a stale review revision", async () => {
  apiMocks.getTrajectoryReview.mockResolvedValue(review);
  apiMocks.getTrajectoryReviewEvidence.mockResolvedValue({
    availability: "available",
    review_ref: review.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: review.state_revision + 1,
    draft_revision: null,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: null,
      projection: null,
      gridmap: null,
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: [1, 2],
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    }],
    draft_commands: [],
  });
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByRole("heading", { name: "轨迹证据不可用" })).toBeVisible();
  expect(screen.getAllByText(/版本与当前复核任务不一致/).length).toBeGreaterThanOrEqual(1);
});

test("Fix workbench displays the bound Gridmap PNG with declared dimensions", async () => {
  const inProgress: TrajectoryReview = {
    ...review,
    status: "in_progress",
    state_revision: 2,
    fix_draft: {
      revision: 1,
      content_sha256: "c".repeat(64),
      calibration: {
        ...review.processing_calibration,
        differs_from_processing: false,
        difference_reason: null,
      },
    },
  };
  const gridmapUrl =
    `/api/annotation/reviews/${review.review_ref}/evidence/frames/0/gridmap`;
  apiMocks.getTrajectoryReview.mockResolvedValue(inProgress);
  const gridEvidence: TrajectoryReviewEvidence = {
    availability: "available",
    review_ref: review.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: 2,
    draft_revision: 1,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: {
        url: `/api/annotation/reviews/${review.review_ref}/evidence/frames/0/camera`,
        width: 1920,
        height: 1536,
      },
      projection: {
        url: `/api/annotation/reviews/${review.review_ref}/evidence/frames/0/projection`,
        width: 3840,
        height: 1536,
      },
      gridmap: {
        url: gridmapUrl,
        width: 320,
        height: 240,
        resolution: 0.1,
        x_range: [-12, 12],
        y_range: [-12, 12],
      },
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: [1, 2],
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    }],
    draft_commands: [],
  };
  apiMocks.getTrajectoryReviewEvidence.mockResolvedValue(gridEvidence);
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);
  const projection = await screen.findByRole("img", {
    name: "第 1 帧原后处理投影",
  });
  expect(projection).toHaveAttribute(
    "data-evidence-layout",
    "legacy-composite-camera",
  );
  expect(projection).toHaveAttribute("viewBox", "1920 0 1920 1536");
  expect(projection).toHaveAttribute("preserveAspectRatio", "xMidYMid meet");
  expect(projection.querySelector("image")).toHaveAttribute(
    "href",
    `/api/annotation/reviews/${review.review_ref}/evidence/frames/0/projection`,
  );
  expect(screen.getByText(/原后处理投影；生成 Fix 预览后更新/)).toBeVisible();

  const gridmap = await screen.findByRole("img", {
    name: "当前帧 Gridmap 鸟瞰图",
  });
  expect(gridmap).toHaveAttribute("src", gridmapUrl);
  expect(gridmap).toHaveAttribute("width", "320");
  expect(gridmap).toHaveAttribute("height", "240");

  const changedReview: TrajectoryReview = {
    ...inProgress,
    state_revision: 3,
    fix_draft: { ...inProgress.fix_draft!, revision: 2 },
  };
  apiMocks.applyFixCommand.mockResolvedValue(changedReview);
  apiMocks.getTrajectoryReviewEvidence.mockResolvedValue({
    ...gridEvidence,
    review_state_revision: 3,
    draft_revision: 2,
    draft_commands: [{
      kind: "set_position",
      frame_index: 0,
      target_ref: "target_0123456789abcdef0123456789abcdef",
      x: 0,
      y: 0,
    }],
  });
  const overlay = screen.getByLabelText("可拖动的当前帧 Gridmap 与轨迹");
  const directionHandle = screen.getByLabelText("拖动目标方向");
  const positionHandle = screen.getByLabelText("拖动目标位置");
  expect(directionHandle).toHaveAttribute("r", "5");
  expect(
    directionHandle.compareDocumentPosition(positionHandle)
      & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  Object.defineProperty(overlay, "getBoundingClientRect", {
    configurable: true,
    value: () => ({
      left: 0,
      top: 0,
      width: 320,
      height: 240,
      right: 320,
      bottom: 240,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }),
  });
  Object.defineProperty(overlay, "setPointerCapture", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(overlay, "hasPointerCapture", {
    configurable: true,
    value: () => false,
  });
  fireEvent.pointerDown(screen.getByLabelText("拖动目标位置"), {
    pointerId: 1,
    clientX: 147,
    clientY: 110,
  });
  const inspector = document.querySelector<HTMLElement>("aside.fix-inspector-panel");
  expect(inspector).not.toBeNull();
  if (!inspector) {
    throw new Error("轨迹属性栏未渲染");
  }
  expect(inspector).toHaveAttribute("aria-hidden", "true");
  expect(inspector).toHaveAttribute("inert");
  expect(inspector.closest("[data-focus-mode]"))?.toHaveAttribute("data-focus-mode", "true");
  fireEvent.pointerMove(overlay, {
    pointerId: 1,
    clientX: 160,
    clientY: 120,
  });
  fireEvent.pointerUp(overlay, {
    pointerId: 1,
    clientX: 160,
    clientY: 120,
  });
  expect(inspector).not.toHaveAttribute("aria-hidden");
  expect(inspector).not.toHaveAttribute("inert");
  expect(inspector.closest("[data-focus-mode]"))?.toHaveAttribute("data-focus-mode", "false");
  await waitFor(() => expect(apiMocks.applyFixCommand).toHaveBeenCalledWith(
    review.review_ref,
    {
      expected_review_revision: 2,
      expected_draft_revision: 1,
      command: {
        kind: "set_position",
        frame_index: 0,
        target_ref: "target_0123456789abcdef0123456789abcdef",
        x: 0,
        y: 0,
      },
    },
  ), { timeout: 2_500 });
});

test("Fix workbench freezes mutations while a Fix run is active and refreshes on completion event", async () => {
  const inProgress: TrajectoryReview = {
    ...review,
    status: "in_progress",
    state_revision: 3,
    fix_draft: {
      revision: 1,
      content_sha256: "c".repeat(64),
      calibration: {
        ...review.processing_calibration,
        differs_from_processing: false,
        difference_reason: null,
      },
    },
    active_fix_run: {
      status: "queued",
      failure: null,
      created_at: "2026-07-28T00:00:01Z",
      updated_at: "2026-07-28T00:00:01Z",
    },
  };
  const completed: TrajectoryReview = {
    ...inProgress,
    state_revision: 4,
    active_fix_run: null,
    fix_revisions: [{
      revision_ref: "fix_revision_0123456789abcdef0123456789abcdef",
      revision_number: 1,
      source_draft_revision: 1,
      content_sha256: "d".repeat(64),
      created_at: "2026-07-28T00:00:02Z",
    }],
  };
  const evidence = (stateRevision: number): TrajectoryReviewEvidence => ({
    availability: "available",
    review_ref: review.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: stateRevision,
    draft_revision: 1,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: null,
      projection: null,
      gridmap: null,
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: [1, 2],
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    }],
    draft_commands: [],
  });
  apiMocks.getTrajectoryReview
    .mockResolvedValue(completed)
    .mockResolvedValueOnce(inProgress);
  apiMocks.getTrajectoryReviewEvidence
    .mockResolvedValue(evidence(4))
    .mockResolvedValueOnce(evidence(3));
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: (
        <>
          <AnnotationDomainEventBridge />
          <TrajectoryFixPage />
        </>
      ),
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByText("Fix 版本正在等待执行")).toBeVisible();
  expect(screen.getByLabelText("位置 X")).toBeDisabled();
  expect(screen.getByRole("button", { name: "生成 Fix 预览" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "通过" })).toBeDisabled();

  await waitFor(() => expect(AnnotationTestEventSource.instances).toHaveLength(1));
  act(() => {
    AnnotationTestEventSource.instances[0].emit({
      seq: 1,
      event_ref: "annotation_event_fix_completed",
      event_kind: "annotation.review.changed",
      aggregate_kind: "review",
      job_ref: review.job_ref,
      segment_ref: review.segment_ref,
      review_ref: review.review_ref,
      state_revision: completed.state_revision,
      status: completed.status,
      occurred_at: "2026-07-29T00:00:00Z",
    });
  });
  await waitFor(() => {
    expect(screen.queryByText("Fix 版本正在等待执行")).not.toBeInTheDocument();
    expect(screen.getByLabelText("位置 X")).toBeEnabled();
  }, { timeout: 1_000 });
  expect(apiMocks.getTrajectoryReview).toHaveBeenCalledTimes(2);
});

test("Fix workbench refreshes an approved publication event until it is truly verified", async () => {
  const publishing: TrajectoryReview = {
    ...review,
    status: "approved",
    state_revision: 4,
    latest_publication: {
      fix_revision_ref: "fix_revision_0123456789abcdef0123456789abcdef",
      attempt: 1,
      status: "publishing",
      content_sha256: null,
      failure: null,
      created_at: "2026-07-28T00:00:01Z",
    },
  };
  const published: TrajectoryReview = {
    ...publishing,
    state_revision: 5,
    latest_publication: {
      ...publishing.latest_publication!,
      status: "published",
      content_sha256: "d".repeat(64),
    },
  };
  apiMocks.getTrajectoryReview
    .mockResolvedValue(published)
    .mockResolvedValueOnce(publishing);
  apiMocks.getTrajectoryReviewEvidence
    .mockResolvedValue(evidenceFor(published))
    .mockResolvedValueOnce(evidenceFor(publishing));
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: (
        <>
          <AnnotationDomainEventBridge />
          <TrajectoryFixPage />
        </>
      ),
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByText("已批准，训练兼容文件正在发布")).toBeVisible();
  expect(screen.getAllByText("已批准/发布中").length).toBeGreaterThan(0);
  await waitFor(() => expect(AnnotationTestEventSource.instances).toHaveLength(1));
  act(() => {
    AnnotationTestEventSource.instances[0].emit({
      seq: 1,
      event_ref: "annotation_event_publication_completed",
      event_kind: "annotation.review.changed",
      aggregate_kind: "review",
      job_ref: review.job_ref,
      segment_ref: review.segment_ref,
      review_ref: review.review_ref,
      state_revision: published.state_revision,
      status: published.status,
      occurred_at: "2026-07-29T00:00:00Z",
    });
  });
  await waitFor(() => {
    expect(screen.queryByText("已批准，训练兼容文件正在发布")).not.toBeInTheDocument();
    expect(screen.getAllByText("已验证").length).toBeGreaterThan(0);
  }, { timeout: 1_000 });
  expect(apiMocks.getTrajectoryReview).toHaveBeenCalledTimes(2);
});

test("Fix workbench displays a sanitized Fix failure and permits draft correction", async () => {
  const failed: TrajectoryReview = {
    ...review,
    status: "in_progress",
    state_revision: 3,
    fix_draft: {
      revision: 1,
      content_sha256: "c".repeat(64),
      calibration: {
        ...review.processing_calibration,
        differs_from_processing: false,
        difference_reason: null,
      },
    },
    active_fix_run: {
      status: "failed",
      failure: {
        code: "fix_runtime_failed",
        error_ref: "annotation_error_0123456789abcdef0123456789abcdef",
      },
      created_at: "2026-07-28T00:00:01Z",
      updated_at: "2026-07-28T00:00:02Z",
    },
    fix_failure: {
      code: "fix_runtime_failed",
      message: "failed under /private/runtime/fix/segment",
      error_ref: "annotation_error_0123456789abcdef0123456789abcdef",
      retryable: true,
    },
  };
  apiMocks.getTrajectoryReview.mockResolvedValue(failed);
  apiMocks.getTrajectoryReviewEvidence.mockResolvedValue({
    availability: "available",
    review_ref: review.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: 3,
    draft_revision: 1,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: null,
      projection: null,
      gridmap: null,
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: [1, 2],
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    }],
    draft_commands: [],
  });
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);

  expect(await screen.findByText("Fix 版本生成失败")).toBeVisible();
  expect(screen.getByText(/错误代码：fix_runtime_failed/)).toBeVisible();
  expect(screen.getByText(/可以调整草稿后重新提交/)).toBeVisible();
  expect(screen.queryByText(/private\/runtime/)).not.toBeInTheDocument();
  expect(screen.getByLabelText("位置 X")).toBeEnabled();
});

test("Fix workbench autosaves a position change through the CAS command API", async () => {
  const inProgress: TrajectoryReview = {
    ...review,
    status: "in_progress",
    state_revision: 2,
    fix_draft: {
      revision: 1,
      content_sha256: "c".repeat(64),
      calibration: {
        ...review.processing_calibration,
        differs_from_processing: false,
        difference_reason: null,
      },
    },
  };
  apiMocks.getTrajectoryReview.mockResolvedValue(inProgress);
  const baseEvidence: TrajectoryReviewEvidence = {
    availability: "available",
    review_ref: review.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: 2,
    draft_revision: 1,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: null,
      projection: null,
      gridmap: null,
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2] as [number, number],
        direction: 0,
        speed: 1,
        color: ["black", "black", "black"],
        image_box: [1, 2, 3, 4] as [number, number, number, number],
        trajectory_points: [[1, 2] as [number, number]],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: [1, 2],
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    }],
    draft_commands: [],
  };
  apiMocks.getTrajectoryReviewEvidence
    .mockResolvedValueOnce(baseEvidence)
    .mockResolvedValueOnce({
      ...baseEvidence,
      review_state_revision: 3,
      draft_revision: 2,
      draft_commands: [{
        kind: "set_position",
        frame_index: 0,
        target_ref: "target_0123456789abcdef0123456789abcdef",
        x: 4.5,
        y: 2,
      }],
    });
  apiMocks.applyFixCommand.mockResolvedValue({
    ...inProgress,
    state_revision: 3,
    fix_draft: { ...inProgress.fix_draft!, revision: 2 },
  });
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);
  const xInput = await screen.findByLabelText("位置 X");
  expect(xInput).toHaveValue("1");
  fireEvent.change(xInput, { target: { value: "4.5" } });

  await waitFor(() => expect(apiMocks.applyFixCommand).toHaveBeenCalledWith(
    review.review_ref,
    {
      expected_review_revision: 2,
      expected_draft_revision: 1,
      command: {
        kind: "set_position",
        frame_index: 0,
        target_ref: "target_0123456789abcdef0123456789abcdef",
        x: 4.5,
        y: 2,
      },
    },
  ), { timeout: 2500 });
});

test("restoring a missing target automatically queues the authoritative Fix preview", async () => {
  const targetRef = "target_0123456789abcdef0123456789abcdef";
  const inProgress: TrajectoryReview = {
    ...review,
    status: "in_progress",
    state_revision: 2,
    fix_draft: {
      revision: 1,
      content_sha256: "c".repeat(64),
      calibration: {
        ...review.processing_calibration,
        differs_from_processing: false,
        difference_reason: null,
      },
    },
  };
  const baseEvidence: TrajectoryReviewEvidence = {
    availability: "available",
    review_ref: review.review_ref,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: 2,
    draft_revision: 1,
    frame_count: 2,
    frames: [0, 1].map((frameIndex) => ({
      frame_index: frameIndex,
      pass: false,
      camera: null,
      projection: null,
      gridmap: null,
      targets: [{
        target_ref: targetRef,
        label: "Master",
        position: frameIndex === 0 ? [1, 2] : null,
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
        camera_position: null,
        camera_trajectory_points: [],
        base_position: frameIndex === 0 ? [1, 2] : null,
        base_direction: 0,
        base_speed: 1,
        base_trajectory_points: [],
      }],
    })),
    draft_commands: [],
  };
  const commandApplied: TrajectoryReview = {
    ...inProgress,
    state_revision: 3,
    fix_draft: { ...inProgress.fix_draft!, revision: 2 },
  };
  const queued: TrajectoryReview = {
    ...commandApplied,
    state_revision: 4,
    active_fix_run: {
      status: "queued",
      failure: null,
      created_at: "2026-07-31T00:00:01Z",
      updated_at: "2026-07-31T00:00:01Z",
    },
  };
  const pendingEvidence = {
    ...baseEvidence,
    review_state_revision: 3,
    draft_revision: 2,
    draft_commands: [{
      kind: "add_missing_target" as const,
      frame_index: 1,
      target_ref: targetRef,
    }],
  };
  apiMocks.getTrajectoryReview.mockResolvedValue(inProgress);
  apiMocks.getTrajectoryReviewEvidence
    .mockResolvedValueOnce(baseEvidence)
    .mockResolvedValueOnce(pendingEvidence)
    .mockResolvedValueOnce({
      ...pendingEvidence,
      review_state_revision: 4,
    });
  apiMocks.applyFixCommand.mockResolvedValue(commandApplied);
  apiMocks.createFixRevision.mockResolvedValue(queued);
  const router = createMemoryRouter([
    {
      path: "/annotation/reviews/:reviewRef",
      element: <TrajectoryFixPage />,
    },
  ], {
    initialEntries: [`/annotation/reviews/${review.review_ref}`],
  });

  render(<RouterProvider router={router} future={{ v7_startTransition: true }} />);
  const frameInput = await screen.findByRole("textbox", { name: "当前帧" });
  expect(frameInput).toHaveValue("1");
  fireEvent.change(frameInput, { target: { value: "2" } });
  fireEvent.keyDown(frameInput, { key: "Enter" });
  expect(frameInput).toHaveValue("2");
  expect(screen.getByRole("slider", { name: "轨迹帧时间线" })).toHaveValue("1");

  fireEvent.change(frameInput, { target: { value: "3" } });
  fireEvent.keyDown(frameInput, { key: "Enter" });
  expect(frameInput).toHaveValue("2");
  expect(screen.getByText("帧序号范围为 1 至 2")).toBeVisible();

  const collapseInspector = screen.getByRole("button", { name: "收起轨迹属性" });
  fireEvent.click(collapseInspector);
  const expandInspector = screen.getByRole("button", { name: "展开轨迹属性" });
  expect(expandInspector).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByRole("region", { name: "相机投影证据" })).toBeVisible();
  expect(screen.getByRole("region", { name: "Gridmap 与轨迹证据" })).toBeVisible();
  fireEvent.click(expandInspector);

  fireEvent.click(await screen.findByRole("button", { name: "补回目标" }));

  await waitFor(() => expect(apiMocks.applyFixCommand).toHaveBeenCalledWith(
    review.review_ref,
    {
      expected_review_revision: 2,
      expected_draft_revision: 1,
      command: {
        kind: "add_missing_target",
        frame_index: 1,
        target_ref: targetRef,
      },
    },
  ));
  await waitFor(() => expect(apiMocks.createFixRevision).toHaveBeenCalledWith(
    review.review_ref,
    {
      expected_review_revision: 3,
      expected_draft_revision: 2,
    },
  ));
  expect(await screen.findByText("Fix 版本正在等待执行")).toBeVisible();
  expect(await screen.findByText("目标已补回，Fix 预览已进入生成队列")).toBeVisible();
});
