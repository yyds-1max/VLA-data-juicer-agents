import { describe, expect, test } from "vitest";

import {
  annotationSegmentsToRulerItems,
  toTrajectoryReviewRulerItem,
  trajectoryReviewsToRulerItems,
} from "./segmentRulerAdapters";
import type {
  AnnotationSegmentStatus,
  AnnotationSegmentSummary,
  CompatibilityPublicationSummary,
  TrajectoryReview,
  TrajectoryReviewStatus,
} from "./types";

function annotationSegment(
  ordinal: number,
  status: AnnotationSegmentStatus,
): AnnotationSegmentSummary {
  return {
    segment_ref: `segment_${ordinal}`,
    ordinal,
    source_clip: "clip",
    status,
    state_revision: 1,
    draft_revision: null,
    submitted_revision: null,
    first_frame: null,
  };
}

function publication(
  status: CompatibilityPublicationSummary["status"],
): CompatibilityPublicationSummary {
  return {
    fix_revision_ref: "fix_revision_1",
    attempt: 1,
    status,
    content_sha256: null,
    failure: null,
    created_at: "2026-08-05T00:00:00Z",
  };
}

function review(
  status: TrajectoryReviewStatus,
  latestPublication: CompatibilityPublicationSummary | null = null,
): TrajectoryReview {
  return {
    review_ref: `review_${status}_${latestPublication?.status ?? "none"}`,
    status,
    state_revision: 1,
    job_ref: "job_1",
    dataset_date: "2026-08-05",
    source_clip: "clip",
    segment_ref: "segment_1",
    segment_ordinal: 7,
    trajectory_revision: {
      revision_ref: "trajectory_revision_1",
      content_sha256: "a".repeat(64),
    },
    processing_calibration: {} as TrajectoryReview["processing_calibration"],
    fix_draft: null,
    fix_revisions: [],
    active_fix_run: null,
    fix_failure: null,
    latest_publication: latestPublication,
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-05T00:00:00Z",
  };
}

describe("annotationSegmentsToRulerItems", () => {
  test("keeps initial-annotation state labels and completion semantics in its adapter", () => {
    const items = annotationSegmentsToRulerItems([
      annotationSegment(1, "pending_initial_annotation"),
      annotationSegment(2, "draft"),
      annotationSegment(3, "submitted"),
      annotationSegment(4, "skipped"),
      annotationSegment(5, "tracking"),
      annotationSegment(6, "tracked"),
      annotationSegment(7, "postprocessing"),
      annotationSegment(8, "annotated"),
      annotationSegment(9, "postprocessing_failed"),
    ]);

    expect(items.map(({ label, resolved }) => ({ label, resolved }))).toEqual([
      { label: "待标注", resolved: false },
      { label: "草稿", resolved: false },
      { label: "已提交", resolved: true },
      { label: "已跳过", resolved: true },
      { label: "Tracking 中", resolved: true },
      { label: "Tracking 完成", resolved: true },
      { label: "后处理中", resolved: true },
      { label: "已标注", resolved: true },
      { label: "后处理失败", resolved: false },
    ]);
  });
});

describe("trajectoryReviewsToRulerItems", () => {
  test("uses review_ref navigation and real review/publication presentation", () => {
    const reviews = [
      review("pending"),
      review("in_progress"),
      review("returned"),
      review("discarded"),
      review("approved"),
      review("approved", publication("publishing")),
      review("approved", publication("failed")),
      review("approved", publication("published")),
    ];

    expect(trajectoryReviewsToRulerItems(reviews)).toEqual([
      { id: reviews[0].review_ref, ordinal: 7, label: "待复核", tone: "warning", resolved: false },
      { id: reviews[1].review_ref, ordinal: 7, label: "修正中", tone: "info", resolved: false },
      { id: reviews[2].review_ref, ordinal: 7, label: "已退回", tone: "danger", resolved: false },
      { id: reviews[3].review_ref, ordinal: 7, label: "已废弃", tone: "neutral", resolved: false },
      { id: reviews[4].review_ref, ordinal: 7, label: "已批准/待发布", tone: "warning", resolved: false },
      { id: reviews[5].review_ref, ordinal: 7, label: "已批准/发布中", tone: "info", resolved: false },
      { id: reviews[6].review_ref, ordinal: 7, label: "已批准/发布失败", tone: "danger", resolved: false },
      { id: reviews[7].review_ref, ordinal: 7, label: "已验证", tone: "success", resolved: true },
    ]);
  });

  test("maps a single review for incremental projections", () => {
    const item = toTrajectoryReviewRulerItem(review("approved", publication("published")));
    expect(item.resolved).toBe(true);
    expect(item.id).toMatch(/^review_/);
  });
});
