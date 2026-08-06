import {
  buildReviewGroups,
  filterReviewGroups,
  reviewListMetrics,
} from "./reviewListPresentation";
import type {
  CompatibilityPublicationSummary,
  TrajectoryReview,
  TrajectoryReviewStatus,
} from "./types";

function reviewFixture(
  suffix: string,
  status: TrajectoryReviewStatus,
  overrides: Partial<TrajectoryReview> = {},
): TrajectoryReview {
  return {
    review_ref: `review_${suffix.padStart(32, "0")}`,
    status,
    state_revision: 1,
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20260804",
    source_clip: "outer-clip-01",
    segment_ref: `segment_${suffix.padStart(32, "0")}`,
    segment_ordinal: Number(suffix),
    trajectory_revision: {
      revision_ref: `trajectory_revision_${suffix.padStart(32, "0")}`,
      content_sha256: suffix.repeat(64).slice(0, 64),
    },
    processing_calibration: {
      profile_ref: "processing-calibration",
      label: "处理标定",
      content_sha256: "a".repeat(64),
    },
    fix_draft: null,
    fix_revisions: [],
    active_fix_run: null,
    fix_failure: null,
    latest_publication: null,
    created_at: "2026-08-04T08:00:00Z",
    updated_at: `2026-08-04T08:${suffix.padStart(2, "0")}:00Z`,
    ...overrides,
  };
}

function publication(
  suffix: string,
  status: CompatibilityPublicationSummary["status"],
): CompatibilityPublicationSummary {
  return {
    fix_revision_ref: `fix_revision_${suffix.padStart(32, "0")}`,
    attempt: 1,
    status,
    content_sha256: status === "published" ? suffix.repeat(64).slice(0, 64) : null,
    failure: status === "failed" ? { code: "publication_failed", error_ref: null } : null,
    created_at: "2026-08-04T09:00:00Z",
  };
}

describe("trajectory review list presentation", () => {
  test("builds complete outer-clip groups with real completion and all eight status presentations", () => {
    const reviews = [
      reviewFixture("1", "pending"),
      reviewFixture("2", "in_progress", {
        fix_draft: {
          revision: 1,
          content_sha256: "b".repeat(64),
          calibration: {
            profile_ref: "fix-calibration-a",
            label: "修整标定 A",
            content_sha256: "c".repeat(64),
            differs_from_processing: true,
            difference_reason: "人工复核需要",
          },
        },
      }),
      reviewFixture("3", "returned", {
        fix_draft: {
          revision: 2,
          content_sha256: "d".repeat(64),
          calibration: {
            profile_ref: "fix-calibration-a",
            label: "修整标定 A",
            content_sha256: "c".repeat(64),
            differs_from_processing: true,
            difference_reason: "人工复核需要",
          },
        },
      }),
      reviewFixture("4", "approved"),
      reviewFixture("5", "approved", { latest_publication: publication("5", "publishing") }),
      reviewFixture("6", "approved", { latest_publication: publication("6", "failed") }),
      reviewFixture("7", "approved", { latest_publication: publication("7", "published") }),
      reviewFixture("8", "discarded", {
        updated_at: "2026-08-04T10:30:00Z",
      }),
    ];

    const [group] = buildReviewGroups(reviews);

    expect(group.progress).toEqual({ resolved: 2, total: 8, value: 25 });
    expect(group.statusBreakdown.map((item) => [item.key, item.count])).toEqual([
      ["pending", 1],
      ["in_progress", 1],
      ["returned", 1],
      ["approved_waiting", 1],
      ["approved_publishing", 1],
      ["approved_failed", 1],
      ["verified", 1],
      ["discarded", 1],
    ]);
    expect(group.calibrationBreakdown).toEqual([
      { profileRef: "fix-calibration-a", label: "修整标定 A", count: 2 },
      { profileRef: null, label: "尚未选择", count: 6 },
    ]);
    expect(group.updatedAt).toBe("2026-08-04T10:30:00Z");
    expect(group.actionableReview.status).toBe("in_progress");
    expect(group.status.label).toBe("已退回");
  });

  test("filters group visibility after grouping without truncating its progress or breakdown", () => {
    const pending = reviewFixture("1", "pending");
    const verified = reviewFixture("2", "approved", {
      latest_publication: publication("2", "published"),
    });
    const otherDate = reviewFixture("3", "pending", {
      dataset_date: "20260731",
      source_clip: "outer-clip-02",
    });
    const groups = buildReviewGroups([pending, verified, otherDate]);

    const visible = filterReviewGroups(groups, {
      status: "pending",
      query: "clip-01",
      dateRange: { from: "2026-08-01", to: "2026-08-05" },
    });

    expect(visible).toHaveLength(1);
    expect(visible[0].progress).toMatchObject({ resolved: 1, total: 2 });
    expect(visible[0].statusBreakdown.map((item) => [item.key, item.count])).toEqual([
      ["pending", 1],
      ["in_progress", 0],
      ["returned", 0],
      ["approved_waiting", 0],
      ["approved_publishing", 0],
      ["approved_failed", 0],
      ["verified", 1],
      ["discarded", 0],
    ]);
  });

  test("opens a review matching the active status filter while preserving complete group metrics", () => {
    const inProgress = reviewFixture("1", "in_progress", { segment_ordinal: 1 });
    const verified = reviewFixture("2", "approved", {
      segment_ordinal: 2,
      latest_publication: publication("2", "published"),
    });
    const [group] = filterReviewGroups(buildReviewGroups([inProgress, verified]), {
      status: "verified",
      query: "",
      dateRange: { from: "", to: "" },
    });

    expect(group.reviews).toHaveLength(2);
    expect(group.progress).toEqual({ resolved: 1, total: 2, value: 50 });
    expect(group.actionableReview.review_ref).toBe(verified.review_ref);
  });

  test("keeps metric counts at review-unit granularity and verifies only published approvals", () => {
    const reviews = [
      reviewFixture("1", "pending"),
      reviewFixture("2", "in_progress"),
      reviewFixture("3", "returned"),
      reviewFixture("4", "approved", { latest_publication: publication("4", "published") }),
      reviewFixture("5", "approved", { latest_publication: publication("5", "publishing") }),
      reviewFixture("6", "discarded"),
    ];

    expect(reviewListMetrics(reviews)).toEqual({
      pending: 1,
      inProgress: 1,
      returned: 1,
      verified: 1,
      discarded: 1,
    });
  });

  test("chooses in-progress, returned, and pending entry reviews in explicit priority order", () => {
    const groups = buildReviewGroups([
      reviewFixture("1", "returned", { segment_ordinal: 1 }),
      reviewFixture("2", "pending", { segment_ordinal: 2 }),
      reviewFixture("3", "in_progress", { segment_ordinal: 3 }),
    ]);

    expect(groups[0].actionableReview.status).toBe("in_progress");
    expect(groups[0].actionableReview.segment_ordinal).toBe(3);
  });
});
