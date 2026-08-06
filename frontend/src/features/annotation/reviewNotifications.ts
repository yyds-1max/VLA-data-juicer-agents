import type { TrajectoryReview } from "./types";

export type ReviewTaskNoticeTone = "info" | "success" | "warning" | "danger";

export type ReviewTaskNoticeDraft = {
  dedupeKey: string;
  source: "annotation";
  tone: ReviewTaskNoticeTone;
  title: string;
  detail: string;
  occurredAt: string;
  reviewRef: string;
  jobRef: string;
  segmentRef: string;
};

function scopeDetail(review: TrajectoryReview): string {
  return `${review.source_clip} · Segment ${String(review.segment_ordinal).padStart(2, "0")}`;
}

function notification(
  review: TrajectoryReview,
  kind: string,
  title: string,
  tone: ReviewTaskNoticeTone,
  occurredAt?: string,
): ReviewTaskNoticeDraft {
  return {
    dedupeKey: `annotation:review:${review.review_ref}:${kind}:${review.state_revision}`,
    source: "annotation",
    tone,
    title,
    detail: scopeDetail(review),
    occurredAt: occurredAt ?? review.updated_at,
    reviewRef: review.review_ref,
    jobRef: review.job_ref,
    segmentRef: review.segment_ref,
  };
}

function latestRevision(review: TrajectoryReview) {
  return review.fix_revisions.at(-1) ?? null;
}

/**
 * 只把对操作员有业务意义的复核状态变化转换为工作台消息。
 * 普通草稿自动保存不会产生消息，保存结果仍由属性栏中的保存状态反馈，
 * 避免高频编辑淹没真正需要关注的 Runtime、发布和复核结论。
 */
export function buildReviewTransitionNotification(
  previous: TrajectoryReview | null | undefined,
  next: TrajectoryReview | null | undefined,
  occurredAt?: string,
): ReviewTaskNoticeDraft | null {
  if (!previous || !next || previous.review_ref !== next.review_ref) return null;
  if (previous.state_revision > next.state_revision) return null;

  if (
    next.fix_failure
    && (
      !previous.fix_failure
      || previous.fix_failure.code !== next.fix_failure.code
      || previous.fix_failure.error_ref !== next.fix_failure.error_ref
    )
  ) {
    return notification(next, "fix-failed", "Fix 预览生成失败", "danger", occurredAt);
  }
  if (
    next.active_fix_run?.status === "failed"
    && previous.active_fix_run?.status !== "failed"
  ) {
    return notification(next, "fix-failed", "Fix 预览生成失败", "danger", occurredAt);
  }

  const previousRevision = latestRevision(previous);
  const nextRevision = latestRevision(next);
  if (
    nextRevision
    && previousRevision?.revision_ref !== nextRevision.revision_ref
  ) {
    return notification(next, "fix-ready", "Fix 预览已生成", "success", occurredAt);
  }

  const previousPublication = previous.latest_publication;
  const nextPublication = next.latest_publication;
  if (
    nextPublication?.status === "published"
    && previousPublication?.status !== "published"
  ) {
    return notification(next, "publication-published", "发布完成，Segment 已验证", "success", occurredAt);
  }
  if (
    nextPublication?.status === "failed"
    && (
      previousPublication?.status !== "failed"
      || previousPublication.attempt !== nextPublication.attempt
    )
  ) {
    return notification(next, "publication-failed", "训练兼容文件发布失败", "danger", occurredAt);
  }
  if (
    nextPublication?.status === "publishing"
    && previousPublication?.status === "failed"
    && nextPublication.attempt > previousPublication.attempt
  ) {
    return notification(next, "publication-retried", "已重新提交发布", "info", occurredAt);
  }

  if (previous.status !== next.status) {
    if (next.status === "in_progress") {
      return notification(
        next,
        previous.status === "returned" ? "fix-resumed" : "fix-created",
        previous.status === "returned" ? "已返回 Fix 工作台" : "Fix 草稿已创建",
        "success",
        occurredAt,
      );
    }
    if (next.status === "returned") {
      return notification(next, "review-returned", "Segment 已退回，可继续修正", "warning", occurredAt);
    }
    if (next.status === "discarded") {
      return notification(next, "review-discarded", "Segment 已废弃", "warning", occurredAt);
    }
    if (next.status === "approved") {
      return notification(
        next,
        "review-approved",
        "复核已通过，训练兼容文件正在发布",
        "info",
        occurredAt,
      );
    }
  }

  if (
    next.active_fix_run?.status === "queued"
    && (
      previous.active_fix_run?.status !== "queued"
      || previous.active_fix_run.created_at !== next.active_fix_run.created_at
    )
  ) {
    return notification(next, "fix-queued", "Fix 预览已提交，正在等待生成", "info", occurredAt);
  }
  if (
    next.active_fix_run?.status === "running"
    && previous.active_fix_run?.status !== "running"
  ) {
    return notification(next, "fix-running", "Fix Runtime 正在生成预览", "info", occurredAt);
  }

  return null;
}
