import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import {
  AnnotationJobActivity,
  AnnotationJobNextStep,
  buildAnnotationJobNextStep,
} from "./AnnotationJobDetailPanels";
import type {
  AnnotationJobDetail,
  AnnotationSegmentStatus,
} from "./types";

function segment(ordinal: number, status: AnnotationSegmentStatus) {
  return {
    segment_ref: `segment_${String(ordinal).padStart(32, "0")}`,
    ordinal,
    source_clip: "20260623_145550",
    status,
    state_revision: 1,
    draft_revision: status === "draft" ? 1 : null,
    submitted_revision: status === "submitted" ? 1 : null,
    first_frame: status === "draft" ? {
      url: "/api/annotation/frames/segment-02",
      width: 1920,
      height: 1080,
      sha256: "a".repeat(64),
      etag: "frame-02",
    } : null,
  };
}

function jobFixture(overrides: Partial<AnnotationJobDetail> = {}): AnnotationJobDetail {
  return {
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20260623",
    source_clips: ["20260623_145550"],
    status: "waiting_initial_annotation",
    cancel_requested: false,
    completion_outcome: null,
    state_revision: 2,
    calibration: {
      profile_ref: "calibration_0123456789abcdef0123456789abcdef",
      label: "v46",
      content_sha256: "b".repeat(64),
    },
    counts: {
      total: 3,
      pending_initial_annotation: 1,
      draft: 1,
      submitted: 1,
      skipped: 0,
      tracking: 0,
      tracked: 0,
      postprocessing: 0,
      annotated: 0,
      postprocessing_failed: 0,
    },
    ready_for_tracking: false,
    ready_for_no_processable_targets: false,
    failure: null,
    created_at: "2026-08-03T02:12:00Z",
    updated_at: "2026-08-03T02:31:00Z",
    segments: [
      segment(1, "submitted"),
      segment(2, "draft"),
      segment(3, "pending_initial_annotation"),
    ],
    ...overrides,
  };
}

test("prioritizes a saved draft and keeps the next-step preview tied to real segment data", () => {
  const job = jobFixture();
  const onOpenSegment = vi.fn();

  render(
    <AnnotationJobNextStep
      job={job}
      onOpenReviews={vi.fn()}
      onOpenSegment={onOpenSegment}
    />,
  );

  expect(screen.getByRole("heading", { name: "下一步" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Segment 02" })).toBeVisible();
  expect(screen.getByRole("img", { name: "Segment 02 首帧预览" })).toHaveAttribute(
    "src",
    "/api/annotation/frames/segment-02",
  );
  fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(onOpenSegment).toHaveBeenCalledWith(job.segments[1].segment_ref);
});

test.each([
  ["preparing", "正在准备 Web 首帧标注"],
  ["tracking", "Tracking 正在串行执行"],
  ["tracked", "Tracking 已完成"],
  ["postprocessing", "DataPilot 正在执行后处理"],
] as const)("renders an honest next step for %s", (status, title) => {
  const model = buildAnnotationJobNextStep(jobFixture({ status }));
  expect(model.title).toBe(title);
  expect(model.action).toBeNull();
});

test("opens human review only after an annotated job", () => {
  const onOpenReviews = vi.fn();
  render(
    <AnnotationJobNextStep
      job={jobFixture({
        status: "annotated",
        counts: {
          total: 3,
          pending_initial_annotation: 0,
          draft: 0,
          submitted: 0,
          skipped: 0,
          tracking: 0,
          tracked: 0,
          postprocessing: 0,
          annotated: 3,
          postprocessing_failed: 0,
        },
      })}
      onOpenReviews={onOpenReviews}
      onOpenSegment={vi.fn()}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "进入人工复核" }));
  expect(onOpenReviews).toHaveBeenCalledTimes(1);
});

test("labels derived activity rows as stage records instead of inventing event times", () => {
  render(
    <AnnotationJobActivity
      guidance="等待进入人工复核。"
      job={jobFixture({
        status: "annotated",
        counts: {
          total: 3,
          pending_initial_annotation: 0,
          draft: 0,
          submitted: 0,
          skipped: 0,
          tracking: 0,
          tracked: 0,
          postprocessing: 0,
          annotated: 3,
          postprocessing_failed: 0,
        },
      })}
    />,
  );

  expect(screen.getByRole("heading", { name: "任务动态" })).toBeVisible();
  expect(screen.getByText("后处理已完成")).toBeVisible();
  expect(screen.getAllByText("阶段记录").length).toBeGreaterThan(0);
  expect(screen.getByText("等待进入人工复核。")).toBeVisible();
});
