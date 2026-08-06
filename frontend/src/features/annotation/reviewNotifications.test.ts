import { describe, expect, test } from "vitest";

import type { TrajectoryReview } from "./types";
import { buildReviewTransitionNotification } from "./reviewNotifications";

function reviewFixture(overrides: Partial<TrajectoryReview> = {}): TrajectoryReview {
  return {
    review_ref: "review_0123456789abcdef0123456789abcdef",
    status: "pending",
    state_revision: 1,
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20260805",
    source_clip: "clip-a",
    segment_ref: "segment_0123456789abcdef0123456789abcdef",
    segment_ordinal: 3,
    trajectory_revision: {
      revision_ref: "trajectory_revision_0123456789abcdef0123456789abcdef",
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
    ...overrides,
  };
}

describe("review notification presentation", () => {
  test("maps review decisions and Fix session entry without leaking internal refs", () => {
    const pending = reviewFixture();
    const inProgress = reviewFixture({
      status: "in_progress",
      state_revision: 2,
      fix_draft: {
        revision: 1,
        content_sha256: "c".repeat(64),
        calibration: {
          ...pending.processing_calibration,
          differs_from_processing: false,
          difference_reason: null,
        },
      },
    });
    expect(buildReviewTransitionNotification(pending, inProgress)).toMatchObject({
      tone: "success",
      title: "Fix 草稿已创建",
      detail: "clip-a · Segment 03",
    });
    expect(buildReviewTransitionNotification(
      inProgress,
      { ...inProgress, status: "returned", state_revision: 3 },
    )).toMatchObject({ title: "Segment 已退回，可继续修正", tone: "warning" });
    expect(buildReviewTransitionNotification(
      inProgress,
      { ...inProgress, status: "discarded", state_revision: 3 },
    )).toMatchObject({ title: "Segment 已废弃", tone: "warning" });
  });

  test("maps asynchronous Fix queue, completion, and failure transitions", () => {
    const inProgress = reviewFixture({ status: "in_progress", state_revision: 2 });
    const queued = reviewFixture({
      status: "in_progress",
      state_revision: 3,
      active_fix_run: {
        status: "queued",
        failure: null,
        created_at: "2026-08-05T00:01:00Z",
        updated_at: "2026-08-05T00:01:00Z",
      },
    });
    expect(buildReviewTransitionNotification(inProgress, queued)).toMatchObject({
      title: "Fix 预览已提交，正在等待生成",
      tone: "info",
    });
    const running = {
      ...queued,
      active_fix_run: {
        ...queued.active_fix_run!,
        status: "running" as const,
        updated_at: "2026-08-05T00:01:30Z",
      },
    };
    expect(buildReviewTransitionNotification(queued, running)).toMatchObject({
      title: "Fix Runtime 正在生成预览",
      tone: "info",
    });

    const completed = reviewFixture({
      status: "in_progress",
      state_revision: 4,
      fix_revisions: [{
        revision_ref: "fix_revision_0123456789abcdef0123456789abcdef",
        revision_number: 1,
        source_draft_revision: 1,
        content_sha256: "d".repeat(64),
        created_at: "2026-08-05T00:02:00Z",
      }],
    });
    expect(buildReviewTransitionNotification(queued, completed)).toMatchObject({
      title: "Fix 预览已生成",
      tone: "success",
    });

    const failed = reviewFixture({
      status: "in_progress",
      state_revision: 4,
      active_fix_run: {
        status: "failed",
        failure: { code: "fix_failed", error_ref: "audit-ref" },
        created_at: "2026-08-05T00:01:00Z",
        updated_at: "2026-08-05T00:02:00Z",
      },
      fix_failure: {
        code: "fix_failed",
        message: "private failure detail",
        error_ref: "audit-ref",
        retryable: true,
      },
    });
    const failureNotification = buildReviewTransitionNotification(queued, failed);
    expect(failureNotification).toMatchObject({ title: "Fix 预览生成失败", tone: "danger" });
    expect(failureNotification?.detail).not.toContain("private failure detail");
  });

  test("maps approval and publication outcomes using the event time", () => {
    const inProgress = reviewFixture({ status: "in_progress", state_revision: 3 });
    const publishing = reviewFixture({
      status: "approved",
      state_revision: 4,
      latest_publication: {
        fix_revision_ref: "fix-revision-a",
        attempt: 1,
        status: "publishing",
        content_sha256: null,
        failure: null,
        created_at: "2026-08-05T00:03:00Z",
      },
    });
    const occurredAt = "2026-08-05T00:03:01Z";
    expect(buildReviewTransitionNotification(inProgress, publishing, occurredAt)).toMatchObject({
      title: "复核已通过，训练兼容文件正在发布",
      tone: "info",
      occurredAt,
    });

    const published = {
      ...publishing,
      state_revision: 5,
      latest_publication: {
        ...publishing.latest_publication!,
        status: "published" as const,
        content_sha256: "e".repeat(64),
      },
    };
    expect(buildReviewTransitionNotification(publishing, published)).toMatchObject({
      title: "发布完成，Segment 已验证",
      tone: "success",
    });

    const publicationFailed = {
      ...publishing,
      state_revision: 5,
      latest_publication: {
        ...publishing.latest_publication!,
        status: "failed" as const,
        failure: { code: "publish_failed", error_ref: null },
      },
    };
    expect(buildReviewTransitionNotification(publishing, publicationFailed)).toMatchObject({
      title: "训练兼容文件发布失败",
      tone: "danger",
    });
  });

  test("suppresses initial hydration, stale events, and ordinary draft autosaves", () => {
    const initial = reviewFixture({ status: "in_progress", state_revision: 2 });
    expect(buildReviewTransitionNotification(null, initial)).toBeNull();
    expect(buildReviewTransitionNotification(initial, initial)).toBeNull();
    expect(buildReviewTransitionNotification(initial, {
      ...initial,
      state_revision: 3,
      fix_draft: {
        revision: 2,
        content_sha256: "f".repeat(64),
        calibration: {
          ...initial.processing_calibration,
          differs_from_processing: false,
          difference_reason: null,
        },
      },
    })).toBeNull();
  });
});
