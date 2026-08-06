import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { TrajectoryReview } from "./types";
import { ReviewSegmentQueuePanel } from "./ReviewSegmentQueuePanel";

function reviewFixture(
  reviewRef: string,
  sourceClip: string,
  segmentOrdinal: number,
  status: TrajectoryReview["status"],
): TrajectoryReview {
  return {
    review_ref: reviewRef,
    status,
    state_revision: 1,
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20260805",
    source_clip: sourceClip,
    segment_ref: `segment_${reviewRef.padEnd(32, "0").slice(-32)}`,
    segment_ordinal: segmentOrdinal,
    trajectory_revision: {
      revision_ref: `trajectory_revision_${reviewRef.padEnd(32, "0").slice(-32)}`,
      content_sha256: "a".repeat(64),
    },
    processing_calibration: {
      profile_ref: "calibration-a",
      label: "Calibration A",
      content_sha256: "b".repeat(64),
    },
    fix_draft: null,
    fix_revisions: [],
    active_fix_run: null,
    fix_failure: null,
    latest_publication: null,
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-05T00:00:00Z",
  };
}

test("groups reviews by outer clip and numbers segments independently inside each group", () => {
  const reviews = [
    reviewFixture("review-a-2", "clip-a", 20, "discarded"),
    reviewFixture("review-b-1", "clip-b", 7, "pending"),
    reviewFixture("review-a-1", "clip-a", 10, "approved"),
  ];

  render(
    <ReviewSegmentQueuePanel
      reviews={reviews}
      currentReviewRef="review-a-1"
      onNavigate={vi.fn()}
    />,
  );

  expect(screen.getByRole("button", { name: "收起外层 clip clip-a" })).toBeVisible();
  expect(screen.getByText("2 个 Segment · 1 已处理")).toBeVisible();
  expect(screen.getByRole("button", { name: "当前 Segment 01，已批准/待发布" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(screen.getByRole("button", { name: "打开 Segment 02，已废弃" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "打开 Segment 01，待复核" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "展开外层 clip clip-b" }));
  expect(screen.getByRole("button", { name: "打开 Segment 01，待复核" })).toBeVisible();
});

test("keeps the current clip open and disables cross-segment navigation during protected states", () => {
  const onNavigate = vi.fn();
  render(
    <ReviewSegmentQueuePanel
      reviews={[
        reviewFixture("review-a-1", "clip-a", 1, "in_progress"),
        reviewFixture("review-a-2", "clip-a", 2, "pending"),
      ]}
      currentReviewRef="review-a-1"
      disabled
      onNavigate={onNavigate}
    />,
  );

  const next = screen.getByRole("button", { name: "打开 Segment 02，待复核" });
  expect(next).toBeDisabled();
  fireEvent.click(next);
  expect(onNavigate).not.toHaveBeenCalled();
  expect(screen.getByText(/0 \/ 2 已处理/)).toBeVisible();
});

test("lays out both clip groups and their segments horizontally below the evidence workbench", () => {
  render(
    <ReviewSegmentQueuePanel
      reviews={[
        reviewFixture("review-a-1", "clip-a", 1, "in_progress"),
        reviewFixture("review-a-2", "clip-a", 2, "pending"),
        reviewFixture("review-b-1", "clip-b", 1, "pending"),
      ]}
      currentReviewRef="review-a-1"
      layout="horizontal"
      onNavigate={vi.fn()}
    />,
  );

  const panel = screen.getByRole("complementary", { name: "Segment 复核队列" });
  expect(panel).toHaveAttribute("data-layout", "horizontal");
  expect(screen.getByRole("navigation", { name: "人工复核 Segment 分组队列" }))
    .toHaveClass("overflow-x-scroll");
  expect(screen.getByRole("button", { name: "收起外层 clip clip-a" }))
    .toHaveClass("w-[13rem]");
  expect(screen.getByRole("button", { name: "当前 Segment 01，修正中" }))
    .toHaveClass("w-[11.75rem]");
});
