import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import {
  AnnotationApiError,
  getAnnotationSegment,
  saveAnnotationDraft,
  submitInitialAnnotation,
} from "./api";
import { InitialAnnotationWorkbench } from "./InitialAnnotationWorkbench";
import type { AnnotationJobDetail, AnnotationSegmentDetail } from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getAnnotationSegment: vi.fn(),
    saveAnnotationDraft: vi.fn(),
    submitInitialAnnotation: vi.fn(),
  };
});

const apiMocks = vi.mocked({
  getAnnotationSegment,
  saveAnnotationDraft,
  submitInitialAnnotation,
});

const job: AnnotationJobDetail = {
  job_ref: "job_0123456789abcdef0123456789abcdef",
  dataset_date: "20270605",
  source_clips: ["20260605_160904"],
  status: "waiting_initial_annotation",
  cancel_requested: false,
  completion_outcome: null,
  state_revision: 1,
  calibration: {
    profile_ref: "calibration_0123456789abcdef0123456789abcdef",
    label: "20260529_go2w",
    content_sha256: "a".repeat(64),
  },
  counts: {
    total: 1,
    pending_initial_annotation: 1,
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
};

function segmentFixture(overrides: Partial<AnnotationSegmentDetail> = {}): AnnotationSegmentDetail {
  return {
    segment_ref: "segment_0123456789abcdef0123456789abcdef",
    ordinal: 1,
    source_clip: "20260605_160904",
    status: "pending_initial_annotation",
    state_revision: 1,
    draft_revision: null,
    submitted_revision: null,
    first_frame: {
      url: "/api/annotation/jobs/job/segments/segment/first-frame",
      width: 100,
      height: 80,
      sha256: "b".repeat(64),
      etag: `"${"b".repeat(64)}"`,
    },
    draft: null,
    skip_reason: null,
    ...overrides,
  };
}

function savedSegmentFromTargets(
  targets: AnnotationSegmentDetail["draft"] extends infer _ ? Parameters<typeof saveAnnotationDraft>[2]["targets"] : never,
  revision: number,
): AnnotationSegmentDetail {
  return segmentFixture({
    status: "draft",
    state_revision: revision + 1,
    draft_revision: revision,
    draft: { revision, targets },
  });
}

function prepareCanvas(): SVGSVGElement {
  loadFirstFrame();
  const canvas = screen.getByRole("application", { name: "首帧标注画布" }) as unknown as SVGSVGElement;
  canvas.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 100,
    bottom: 100,
    width: 100,
    height: 100,
    toJSON: () => ({}),
  });
  Object.defineProperty(canvas, "setPointerCapture", { configurable: true, value: vi.fn() });
  Object.defineProperty(canvas, "hasPointerCapture", { configurable: true, value: vi.fn(() => false) });
  return canvas;
}

function loadFirstFrame(width = 100, height = 80): HTMLImageElement {
  const image = screen.getByRole("img", { name: /resize 后首帧/ }) as HTMLImageElement;
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
  return image;
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

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getAnnotationSegment.mockResolvedValue(segmentFixture());
  apiMocks.saveAnnotationDraft.mockImplementation(async (_jobRef, _segmentRef, body) => (
    savedSegmentFromTargets(body.targets, 1)
  ));
  apiMocks.submitInitialAnnotation.mockResolvedValue(segmentFixture({ status: "submitted" }));
});

test("renders the canvas and inspector as one integrated studio workspace", () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );

  const workbench = screen.getByTestId("annotation-workbench");
  expect(workbench).toContainElement(screen.getByTestId("annotation-canvas-region"));
  expect(workbench).toContainElement(screen.getByTestId("annotation-inspector-region"));
  expect(screen.getByRole("complementary", { name: "目标属性检查器" }))
    .toHaveAttribute("data-layout", "overlay");
  expect(workbench.querySelector("[data-annotation-canvas-scroll]"))
    .toHaveAttribute("data-inspector-reserves-space", "false");
  expect(screen.getByTestId("annotation-zoom-controls"))
    .toHaveAttribute("data-inspector-avoidance", "panel-width");
  expect(screen.getByText("Segment 01")).toBeVisible();
});

test("collapses the target inspector without removing the image workspace", () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "收起目标属性" }));
  expect(screen.getByTestId("annotation-inspector-region")).toHaveAttribute("data-collapsed", "true");
  expect(screen.getByRole("button", { name: "展开目标属性" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByTestId("annotation-zoom-controls"))
    .toHaveAttribute("data-inspector-avoidance", "collapsed-rail");
  expect(screen.getByRole("img", { name: /resize 后首帧/ })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "展开目标属性" }));
  expect(screen.getByTestId("annotation-inspector-region")).toHaveAttribute("data-collapsed", "false");
  expect(screen.getByRole("button", { name: "收起目标属性" })).toHaveAttribute("aria-expanded", "true");
});

test("uses a protected focus mode for box and foreground-point interactions", () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();

  fireEvent.click(screen.getByRole("button", { name: "框选目标" }));
  expect(screen.getByText("专注框选：在画面中拖拽创建目标框")).toBeVisible();
  expect(screen.getByRole("button", { name: "框选目标" })).toHaveAttribute("aria-pressed", "true");

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByText("专注框选：在画面中拖拽创建目标框")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "选择/调整" })).toHaveAttribute("aria-pressed", "true");
});

test("draws an ordered master target with a contract-safe ref and clamps the foreground point", async () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  const canvas = prepareCanvas();

  fireEvent.click(screen.getByRole("button", { name: "框选目标" }));
  fireEvent.pointerDown(canvas, { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
  fireEvent.pointerMove(canvas, { clientX: 90, clientY: 90, pointerId: 1 });
  fireEvent.pointerUp(canvas, { clientX: 90, clientY: 90, pointerId: 1 });

  expect(screen.getAllByText("master").length).toBeGreaterThan(0);
  fireEvent.pointerDown(canvas, { button: 0, clientX: 100, clientY: 100, pointerId: 2 });

  fireEvent.change(screen.getByLabelText("master 上衣颜色"), { target: { value: "green" } });
  fireEvent.change(screen.getByLabelText("master 裤子颜色"), { target: { value: "gray" } });
  fireEvent.change(screen.getByLabelText("master 鞋子颜色"), { target: { value: "white" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalled());
  const body = apiMocks.saveAnnotationDraft.mock.calls.at(-1)![2];
  expect(body.targets).toHaveLength(1);
  expect(body.expected_segment_revision).toBe(1);
  expect(body.expected_draft_revision).toBeNull();
  expect(body.targets[0].target_ref).toMatch(/^target_[0-9a-f]{32}$/);
  expect(body.targets[0].point).toEqual([99, 79]);
  expect(body.targets[0].colors).toEqual({ upper: "green", lower: "gray", shoes: "white" });
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeEnabled();
});

test.each([
  ["0px × 0px", 10, 10, [10, 8, 0, 0]],
  ["1px × 0px", 11, 10, [10, 8, 1, 0]],
  ["2px × 0px", 12, 10, [10, 8, 2, 0]],
  ["0px × 8px", 10, 20, [10, 8, 0, 8]],
] as const)("preserves legacy absolute bbox semantics for a %s draw", async (
  _label,
  endX,
  endY,
  expected,
) => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  const canvas = prepareCanvas();

  fireEvent.click(screen.getByRole("button", { name: "框选目标" }));
  fireEvent.pointerDown(canvas, { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
  fireEvent.pointerMove(canvas, { clientX: endX, clientY: endY, pointerId: 1 });
  fireEvent.pointerUp(canvas, { clientX: endX, clientY: endY, pointerId: 1 });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  expect(apiMocks.saveAnnotationDraft.mock.calls[0][2].targets[0].bbox).toEqual(expected);
});

test("numeric bbox inputs preserve an explicit zero width and height", async () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [{
            target_ref: `target_${"1".repeat(32)}`,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();

  fireEvent.change(screen.getByLabelText("master bbox w"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("master bbox h"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  expect(apiMocks.saveAnnotationDraft.mock.calls[0][2].targets[0].bbox).toEqual([10, 10, 0, 0]);
});

test.each([
  ["nw", 10, 12.5, 30, 37.5, [30, 30, 0, 0]],
  ["n", 20, 12.5, 20, 37.5, [10, 30, 20, 0]],
  ["ne", 30, 12.5, 10, 37.5, [10, 30, 0, 0]],
  ["e", 30, 25, 10, 25, [10, 10, 0, 20]],
  ["se", 30, 37.5, 10, 12.5, [10, 10, 0, 0]],
  ["s", 20, 37.5, 20, 12.5, [10, 10, 20, 0]],
  ["sw", 10, 37.5, 30, 12.5, [30, 10, 0, 0]],
  ["w", 10, 25, 30, 25, [30, 10, 0, 20]],
] as const)("allows the %s resize handle to collapse a bbox edge to zero", async (
  direction,
  startX,
  startY,
  endX,
  endY,
  expected,
) => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [{
            target_ref: `target_${"2".repeat(32)}`,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  const canvas = prepareCanvas();
  const handle = canvas.querySelector(`[data-resize-direction="${direction}"]`);
  expect(handle).not.toBeNull();

  fireEvent.pointerDown(handle!, {
    button: 0,
    clientX: startX,
    clientY: startY,
    pointerId: 1,
  });
  fireEvent.pointerMove(canvas, { clientX: endX, clientY: endY, pointerId: 1 });
  fireEvent.pointerUp(canvas, { clientX: endX, clientY: endY, pointerId: 1 });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  expect(apiMocks.saveAnnotationDraft.mock.calls[0][2].targets[0].bbox).toEqual(expected);
});

test("dragging another target point updates the point bound to that target ref", async () => {
  const firstRef = `target_${"3".repeat(32)}`;
  const secondRef = `target_${"4".repeat(32)}`;
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [
            {
              target_ref: firstRef,
              bbox: [10, 10, 20, 20],
              point: [15, 15],
              colors: { upper: "green", lower: "gray", shoes: "white" },
            },
            {
              target_ref: secondRef,
              bbox: [50, 40, 30, 30],
              point: [60, 60],
              colors: { upper: "blue", lower: "black", shoes: "white" },
            },
          ],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  const canvas = prepareCanvas();
  const secondPoint = canvas.querySelector(`[data-annotation-point-ref="${secondRef}"]`);
  expect(secondPoint).not.toBeNull();

  fireEvent.pointerDown(secondPoint!, {
    button: 0,
    clientX: 60,
    clientY: 75,
    pointerId: 1,
  });
  fireEvent.pointerMove(canvas, { clientX: 75, clientY: 87.5, pointerId: 1 });
  fireEvent.pointerUp(canvas, { clientX: 75, clientY: 87.5, pointerId: 1 });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  const savedTargets = apiMocks.saveAnnotationDraft.mock.calls[0][2].targets;
  expect(savedTargets[0].point).toEqual([15, 15]);
  expect(savedTargets[1].point).toEqual([75, 70]);
});

test("requires bbox point and all three colors before submitting", () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
  expect(screen.getByText(/每个目标都需要 bbox/)).toBeVisible();
});

test("stops autosave on 409 and requires an explicit conflict choice", async () => {
  const conflictSegment = segmentFixture({
    status: "draft",
    state_revision: 3,
    draft_revision: 2,
    draft: { revision: 2, targets: [] },
  });
  apiMocks.saveAnnotationDraft.mockRejectedValue(new AnnotationApiError(
    "版本冲突",
    409,
    { code: "revision_conflict", message: "版本冲突", current: conflictSegment },
  ));

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [{
            target_ref: `target_${"1".repeat(32)}`,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();

  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));

  expect(await screen.findByText("检测到并发修改")).toBeVisible();
  expect(screen.getByRole("button", { name: "使用服务器版本" })).toBeVisible();
  expect(screen.getByRole("button", { name: "保留本地版本" })).toBeVisible();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
});

test("keeps the newest UI snapshot when an older in-flight save conflicts", async () => {
  const first = deferred<AnnotationSegmentDetail>();
  const serverTarget = {
    target_ref: `target_${"7".repeat(32)}`,
    bbox: [20, 10, 20, 20] as [number, number, number, number],
    point: [25, 15] as [number, number],
    colors: { upper: "green" as const, lower: "gray" as const, shoes: "white" as const },
  };
  const serverSegment = segmentFixture({
    status: "draft",
    state_revision: 8,
    draft_revision: 5,
    draft: { revision: 5, targets: [serverTarget] },
  });
  apiMocks.saveAnnotationDraft
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(async (_jobRef, _segmentRef, body) => segmentFixture({
      status: "draft",
      state_revision: 9,
      draft_revision: 6,
      draft: { revision: 6, targets: body.targets },
    }));

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 4,
        draft_revision: 2,
        draft: {
          revision: 2,
          targets: [{
            target_ref: serverTarget.target_ref,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();

  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "12" } });

  first.reject(new AnnotationApiError(
    "版本冲突",
    409,
    { code: "revision_conflict", message: "版本冲突", current: { segment: serverSegment } },
  ));
  expect(await screen.findByText("检测到并发修改")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "保留本地版本" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(2));
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2]).toMatchObject({
    expected_segment_revision: 8,
    expected_draft_revision: 5,
  });
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2].targets[0].bbox).toEqual([12, 10, 20, 20]);
  await waitFor(() => expect(screen.getByText("草稿已保存")).toBeVisible());
});

test("does not send an already queued stale save after an earlier request enters conflict", async () => {
  const first = deferred<AnnotationSegmentDetail>();
  const registerFlush = vi.fn();
  const serverSegment = segmentFixture({
    status: "draft",
    state_revision: 8,
    draft_revision: 5,
    draft: {
      revision: 5,
      targets: [{
        target_ref: `target_${"8".repeat(32)}`,
        bbox: [20, 10, 20, 20],
        point: [25, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });
  apiMocks.saveAnnotationDraft.mockImplementationOnce(() => first.promise);

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 4,
        draft_revision: 2,
        draft: {
          revision: 2,
          targets: [{
            target_ref: `target_${"8".repeat(32)}`,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
      registerFlush={registerFlush}
    />,
  );
  loadFirstFrame();

  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText("master bbox y"), { target: { value: "12" } });
  let queuedFlush!: Promise<boolean>;
  act(() => {
    queuedFlush = registerFlush.mock.calls.at(-1)![0]();
  });

  first.reject(new AnnotationApiError(
    "版本冲突",
    409,
    { code: "revision_conflict", message: "版本冲突", current: { segment: serverSegment } },
  ));
  expect(await screen.findByText("检测到并发修改")).toBeVisible();
  await act(async () => {
    expect(await queuedFlush).toBe(false);
  });
  expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1);
});

test("registers a flush callback so route changes can wait for draft persistence", async () => {
  const registerFlush = vi.fn();
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
      registerFlush={registerFlush}
    />,
  );
  expect(registerFlush).toHaveBeenCalled();
  await act(async () => {
    expect(await registerFlush.mock.calls.at(-1)![0]()).toBe(true);
  });
});

test("serializes saves and advances CAS revisions before sending the next draft", async () => {
  const first = deferred<AnnotationSegmentDetail>();
  const second = deferred<AnnotationSegmentDetail>();
  apiMocks.saveAnnotationDraft
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => second.promise);
  const initial = segmentFixture({
    status: "draft",
    state_revision: 4,
    draft_revision: 2,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"2".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={initial}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));

  fireEvent.change(screen.getByLabelText("master bbox y"), { target: { value: "12" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1);

  first.resolve(segmentFixture({
    status: "draft",
    state_revision: 5,
    draft_revision: 3,
    draft: { revision: 3, targets: apiMocks.saveAnnotationDraft.mock.calls[0][2].targets },
  }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(2));
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2]).toMatchObject({
    expected_segment_revision: 5,
    expected_draft_revision: 3,
  });
  second.resolve(segmentFixture({
    status: "draft",
    state_revision: 6,
    draft_revision: 4,
    draft: { revision: 4, targets: apiMocks.saveAnnotationDraft.mock.calls[1][2].targets },
  }));
  await waitFor(() => expect(screen.getByText("草稿已保存")).toBeVisible());
});

test("persists a revert to the last saved value after a different draft is already pending", async () => {
  const first = deferred<AnnotationSegmentDetail>();
  const second = deferred<AnnotationSegmentDetail>();
  const registerFlush = vi.fn();
  apiMocks.saveAnnotationDraft
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => second.promise);
  const initial = segmentFixture({
    status: "draft",
    state_revision: 4,
    draft_revision: 2,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"5".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={initial}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
      registerFlush={registerFlush}
    />,
  );
  loadFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));

  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "10" } });
  let flushPromise!: Promise<boolean>;
  act(() => {
    flushPromise = registerFlush.mock.calls.at(-1)![0]();
  });
  expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1);

  first.resolve(segmentFixture({
    status: "draft",
    state_revision: 5,
    draft_revision: 3,
    draft: {
      revision: 3,
      targets: apiMocks.saveAnnotationDraft.mock.calls[0][2].targets,
    },
  }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(2));
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2]).toMatchObject({
    expected_segment_revision: 5,
    expected_draft_revision: 3,
  });
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2].targets[0].bbox).toEqual([10, 10, 20, 20]);

  second.resolve(segmentFixture({
    status: "draft",
    state_revision: 6,
    draft_revision: 4,
    draft: {
      revision: 4,
      targets: apiMocks.saveAnnotationDraft.mock.calls[1][2].targets,
    },
  }));
  await act(async () => {
    expect(await flushPromise).toBe(true);
  });
  await waitFor(() => expect(screen.getByText("草稿已保存")).toBeVisible());
});

test("automatically persists an A to B to A revert without requiring navigation or a manual flush", async () => {
  const first = deferred<AnnotationSegmentDetail>();
  const second = deferred<AnnotationSegmentDetail>();
  apiMocks.saveAnnotationDraft
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => second.promise);
  const initial = segmentFixture({
    status: "draft",
    state_revision: 4,
    draft_revision: 2,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"6".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={initial}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "立即保存草稿" }));
  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));

  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "10" } });
  first.resolve(segmentFixture({
    status: "draft",
    state_revision: 5,
    draft_revision: 3,
    draft: {
      revision: 3,
      targets: apiMocks.saveAnnotationDraft.mock.calls[0][2].targets,
    },
  }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(2));
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2]).toMatchObject({
    expected_segment_revision: 5,
    expected_draft_revision: 3,
  });
  expect(apiMocks.saveAnnotationDraft.mock.calls[1][2].targets[0].bbox).toEqual([10, 10, 20, 20]);

  second.resolve(segmentFixture({
    status: "draft",
    state_revision: 6,
    draft_revision: 4,
    draft: {
      revision: 4,
      targets: apiMocks.saveAnnotationDraft.mock.calls[1][2].targets,
    },
  }));
  await waitFor(() => expect(screen.getByText("草稿已保存")).toBeVisible());
});

test("freezes geometry and attributes while submit waits for the final draft save", async () => {
  const pendingSave = deferred<AnnotationSegmentDetail>();
  apiMocks.saveAnnotationDraft.mockReturnValue(pendingSave.promise);
  const initial = segmentFixture({
    status: "draft",
    state_revision: 4,
    draft_revision: 2,
    draft: {
      revision: 2,
      targets: [{
        target_ref: `target_${"9".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={initial}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "提交首帧标注" }));

  await waitFor(() => expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1));
  expect(screen.getByLabelText("master bbox x")).toBeDisabled();
  expect(screen.getByLabelText("master 上衣颜色")).toBeDisabled();
  expect(screen.getByRole("button", { name: "框选目标" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();

  pendingSave.resolve(segmentFixture({
    status: "draft",
    state_revision: 5,
    draft_revision: 3,
    draft: {
      revision: 3,
      targets: apiMocks.saveAnnotationDraft.mock.calls[0][2].targets,
    },
  }));
  await waitFor(() => expect(apiMocks.submitInitialAnnotation).toHaveBeenCalledWith(
    job.job_ref,
    initial.segment_ref,
    5,
    3,
  ));
  expect(apiMocks.saveAnnotationDraft.mock.calls[0][2].targets[0].bbox).toEqual([11, 10, 20, 20]);
});

test("synchronizes a stale submit when another tab already submitted the segment", async () => {
  const target = {
    target_ref: `target_${"c".repeat(32)}`,
    bbox: [10, 10, 20, 20] as [number, number, number, number],
    point: [15, 15] as [number, number],
    colors: { upper: "green" as const, lower: "gray" as const, shoes: "white" as const },
  };
  const submitted = segmentFixture({
    status: "submitted",
    state_revision: 6,
    draft_revision: 2,
    submitted_revision: 1,
    draft: { revision: 2, targets: [target] },
  });
  apiMocks.submitInitialAnnotation.mockRejectedValue(new AnnotationApiError(
    "The annotation segment changed; refresh before retrying.",
    409,
    {
      code: "segment_revision_conflict",
      message: "The annotation segment changed; refresh before retrying.",
      current: submitted,
    },
  ));
  const onSegmentUpdated = vi.fn();
  const onJobRefresh = vi.fn(async () => undefined);
  const onExternalSubmissionResolved = vi.fn();

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 4,
        draft_revision: 2,
        draft: { revision: 2, targets: [target] },
      })}
      onSegmentUpdated={onSegmentUpdated}
      onJobRefresh={onJobRefresh}
      onExternalSubmissionResolved={onExternalSubmissionResolved}
    />,
  );
  loadFirstFrame();
  fireEvent.click(screen.getByRole("button", { name: "提交首帧标注" }));

  await waitFor(() => expect(onExternalSubmissionResolved).toHaveBeenCalledWith(
    "已在其他页面完成提交。本页内容未再次提交，现已切换到服务器版本。",
  ));
  expect(screen.getByText("已载入服务器版本")).toBeVisible();
  expect(screen.queryByText("The annotation segment changed; refresh before retrying.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
  expect(apiMocks.submitInitialAnnotation).toHaveBeenCalledTimes(1);
  expect(onSegmentUpdated).toHaveBeenCalledWith(submitted);
  expect(onJobRefresh).toHaveBeenCalledTimes(1);
});

test("discards dirty local edits when the draft save finds an externally submitted segment", async () => {
  const localTarget = {
    target_ref: `target_${"e".repeat(32)}`,
    bbox: [10, 10, 20, 20] as [number, number, number, number],
    point: [15, 15] as [number, number],
    colors: { upper: "black" as const, lower: "black" as const, shoes: "black" as const },
  };
  const submittedTarget = {
    ...localTarget,
    colors: { upper: "black" as const, lower: "gray" as const, shoes: "black" as const },
  };
  const submitted = segmentFixture({
    status: "submitted",
    state_revision: 6,
    draft_revision: 3,
    submitted_revision: 1,
    draft: { revision: 3, targets: [submittedTarget] },
  });
  apiMocks.saveAnnotationDraft.mockRejectedValue(new AnnotationApiError(
    "The annotation segment changed; refresh before retrying.",
    409,
    {
      code: "segment_revision_conflict",
      message: "The annotation segment changed; refresh before retrying.",
      current: submitted,
    },
  ));
  const onSegmentUpdated = vi.fn();
  const onJobRefresh = vi.fn(async () => {
    throw new Error("temporary refresh failure");
  });
  const onExternalSubmissionResolved = vi.fn();

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 4,
        draft_revision: 2,
        draft: { revision: 2, targets: [localTarget] },
      })}
      onSegmentUpdated={onSegmentUpdated}
      onJobRefresh={onJobRefresh}
      onExternalSubmissionResolved={onExternalSubmissionResolved}
    />,
  );
  loadFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "提交首帧标注" }));

  await waitFor(() => expect(onExternalSubmissionResolved).toHaveBeenCalledWith(
    "已在其他页面完成提交。本页内容未再次提交，现已切换到服务器版本。",
  ));
  expect(apiMocks.saveAnnotationDraft).toHaveBeenCalledTimes(1);
  expect(apiMocks.submitInitialAnnotation).not.toHaveBeenCalled();
  expect(onSegmentUpdated).toHaveBeenCalledWith(submitted);
  expect(onJobRefresh).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("检测到并发修改")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保留本地版本" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("master bbox x")).toHaveValue(10);
  expect(screen.getByLabelText("master 裤子颜色")).toHaveValue("gray");
  expect(screen.getByText("已载入服务器版本")).toBeVisible();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
});

test("offers an explicit version choice when a stale submit finds a newer draft", async () => {
  const target = {
    target_ref: `target_${"d".repeat(32)}`,
    bbox: [10, 10, 20, 20] as [number, number, number, number],
    point: [15, 15] as [number, number],
    colors: { upper: "green" as const, lower: "gray" as const, shoes: "white" as const },
  };
  const latestDraft = segmentFixture({
    status: "draft",
    state_revision: 6,
    draft_revision: 3,
    draft: {
      revision: 3,
      targets: [{ ...target, bbox: [12, 10, 20, 20] }],
    },
  });
  apiMocks.submitInitialAnnotation.mockRejectedValue(new AnnotationApiError(
    "The annotation segment changed; refresh before retrying.",
    409,
    {
      code: "segment_revision_conflict",
      message: "The annotation segment changed; refresh before retrying.",
      current: latestDraft,
    },
  ));

  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 4,
        draft_revision: 2,
        draft: { revision: 2, targets: [target] },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();
  fireEvent.click(screen.getByRole("button", { name: "提交首帧标注" }));

  expect(await screen.findByText("检测到并发修改")).toBeVisible();
  expect(screen.getByRole("button", { name: "使用服务器版本" })).toBeVisible();
  expect(screen.getByRole("button", { name: "保留本地版本" })).toBeVisible();
  expect(screen.queryByText("The annotation segment changed; refresh before retrying.")).not.toBeInTheDocument();
});

test("keeps geometry and submit fail-closed until the decoded image size is verified", () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [{
            target_ref: `target_${"a".repeat(32)}`,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );

  expect(screen.getByRole("button", { name: "框选目标" })).toBeDisabled();
  expect(screen.getByLabelText("master bbox x")).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();

  loadFirstFrame();

  expect(screen.getByRole("button", { name: "框选目标" })).toBeEnabled();
  expect(screen.getByLabelText("master bbox x")).toBeEnabled();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeEnabled();
});

test("keeps an editable-looking segment read-only when its job no longer accepts annotation", () => {
  render(
    <InitialAnnotationWorkbench
      job={{ ...job, status: "cancelled" }}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [{
            target_ref: `target_${"b".repeat(32)}`,
            bbox: [10, 10, 20, 20],
            point: [15, 15],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();

  expect(screen.getByText("只读")).toBeVisible();
  expect(screen.getByRole("button", { name: "框选目标" })).toBeDisabled();
  expect(screen.getByLabelText("master bbox x")).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
});

test("reports a decoded-image failure instead of silently leaving editing disabled", () => {
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture()}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );

  fireEvent.error(screen.getByRole("img", { name: /resize 后首帧/ }));

  expect(screen.getByText("首帧图片加载失败，请刷新页面重试")).toBeVisible();
  expect(screen.getByRole("button", { name: "框选目标" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
});

test("uses larger transparent pointer hit areas for production-size points and resize handles", () => {
  const targetRef = `target_${"c".repeat(32)}`;
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        first_frame: {
          url: "/api/annotation/jobs/job/segments/segment/first-frame",
          width: 1920,
          height: 1536,
          sha256: "b".repeat(64),
          etag: `"${"b".repeat(64)}"`,
        },
        draft: {
          revision: 1,
          targets: [{
            target_ref: targetRef,
            bbox: [100, 100, 500, 800],
            point: [300, 250],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame(1920, 1536);

  const point = document.querySelector(`[data-annotation-point-ref="${targetRef}"]`);
  const pointHit = document.querySelector(`[data-annotation-point-hit-ref="${targetRef}"]`);
  const resize = document.querySelector(`[data-resize-direction="se"]`);
  const resizeHit = document.querySelector(`[data-resize-hit-direction="se"]`);
  expect(point).not.toBeNull();
  expect(pointHit).not.toBeNull();
  expect(resize).not.toBeNull();
  expect(resizeHit).not.toBeNull();
  expect(Number(pointHit!.getAttribute("r"))).toBeGreaterThan(Number(point!.getAttribute("r")));
  expect(Number(resizeHit!.getAttribute("r"))).toBeGreaterThan(Number(resize!.getAttribute("r")));
});

test("micro-adjusts the selected bbox and point by keyboard and clamps both to image bounds", () => {
  const targetRef = `target_${"d".repeat(32)}`;
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={segmentFixture({
        status: "draft",
        state_revision: 2,
        draft_revision: 1,
        draft: {
          revision: 1,
          targets: [{
            target_ref: targetRef,
            bbox: [70, 50, 20, 20],
            point: [95, 75],
            colors: { upper: "green", lower: "gray", shoes: "white" },
          }],
        },
      })}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();
  const canvas = screen.getByRole("application", { name: "首帧标注画布" });

  fireEvent.keyDown(canvas, { key: "ArrowLeft" });
  expect(screen.getByLabelText("master bbox x")).toHaveValue(69);
  expect(screen.getByLabelText("master point x")).toHaveValue(94);

  fireEvent.keyDown(canvas, { key: "ArrowUp", shiftKey: true });
  expect(screen.getByLabelText("master bbox y")).toHaveValue(40);
  expect(screen.getByLabelText("master point y")).toHaveValue(65);

  fireEvent.keyDown(canvas, { key: "ArrowRight", shiftKey: true });
  expect(screen.getByLabelText("master bbox x")).toHaveValue(79);
  expect(screen.getByLabelText("master point x")).toHaveValue(99);
  fireEvent.keyDown(canvas, { key: "ArrowRight", shiftKey: true });
  expect(screen.getByLabelText("master bbox x")).toHaveValue(80);
  expect(screen.getByLabelText("master point x")).toHaveValue(99);

  fireEvent.keyDown(canvas, { key: "ArrowDown", shiftKey: true });
  fireEvent.keyDown(canvas, { key: "ArrowDown", shiftKey: true });
  fireEvent.keyDown(canvas, { key: "ArrowDown", shiftKey: true });
  expect(screen.getByLabelText("master bbox y")).toHaveValue(60);
  expect(screen.getByLabelText("master point y")).toHaveValue(79);
});

test("disables geometry editing when the decoded image size differs from server metadata", () => {
  const initial = segmentFixture({
    status: "draft",
    state_revision: 2,
    draft_revision: 1,
    draft: {
      revision: 1,
      targets: [{
        target_ref: `target_${"3".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={initial}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  const image = screen.getByRole("img", { name: /resize 后首帧/ });
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: 101 });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: 80 });
  fireEvent.load(image);

  expect(screen.getByText("图片尺寸与元数据不一致，已停止编辑")).toBeVisible();
  expect(screen.getByRole("button", { name: "框选目标" })).toBeDisabled();
  expect(screen.getByLabelText("master bbox x")).toBeDisabled();
  expect(screen.getByRole("button", { name: "提交首帧标注" })).toBeDisabled();
});

test("marks browser unload as unsafe while an edited draft is not persisted", () => {
  const initial = segmentFixture({
    status: "draft",
    state_revision: 2,
    draft_revision: 1,
    draft: {
      revision: 1,
      targets: [{
        target_ref: `target_${"4".repeat(32)}`,
        bbox: [10, 10, 20, 20],
        point: [15, 15],
        colors: { upper: "green", lower: "gray", shoes: "white" },
      }],
    },
  });
  render(
    <InitialAnnotationWorkbench
      job={job}
      segment={initial}
      onSegmentUpdated={vi.fn()}
      onJobRefresh={vi.fn(async () => undefined)}
    />,
  );
  loadFirstFrame();
  fireEvent.change(screen.getByLabelText("master bbox x"), { target: { value: "12" } });
  const unload = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
});
