import type { TrajectoryReview } from "./types";

export type ReviewStatusTone =
  | "success"
  | "info"
  | "warning"
  | "danger"
  | "neutral";

export type ReviewPresentationKey =
  | "pending"
  | "in_progress"
  | "returned"
  | "approved_waiting"
  | "approved_publishing"
  | "approved_failed"
  | "verified"
  | "discarded";

export type ReviewPresentation = {
  key: ReviewPresentationKey;
  label: string;
  tone: ReviewStatusTone;
};

export const REVIEW_PRESENTATIONS: Record<
  ReviewPresentationKey,
  ReviewPresentation
> = {
  pending: { key: "pending", label: "待复核", tone: "warning" },
  in_progress: { key: "in_progress", label: "修正中", tone: "info" },
  returned: { key: "returned", label: "已退回", tone: "danger" },
  approved_waiting: {
    key: "approved_waiting",
    label: "已批准/待发布",
    tone: "warning",
  },
  approved_publishing: {
    key: "approved_publishing",
    label: "已批准/发布中",
    tone: "info",
  },
  approved_failed: {
    key: "approved_failed",
    label: "已批准/发布失败",
    tone: "danger",
  },
  verified: { key: "verified", label: "已验证", tone: "success" },
  discarded: { key: "discarded", label: "已废弃", tone: "neutral" },
};

export function trajectoryReviewPresentation(
  review: TrajectoryReview,
): ReviewPresentation {
  // approved 只是人工结论，只有训练兼容文件发布成功后才具有“已验证”业务语义。
  // 列表、指标、队列和详情必须统一通过此函数判断，不能直接把 approved 当作完成。
  if (review.status === "approved") {
    if (review.latest_publication?.status === "published") {
      return REVIEW_PRESENTATIONS.verified;
    }
    if (review.latest_publication?.status === "failed") {
      return REVIEW_PRESENTATIONS.approved_failed;
    }
    if (review.latest_publication?.status === "publishing") {
      return REVIEW_PRESENTATIONS.approved_publishing;
    }
    return REVIEW_PRESENTATIONS.approved_waiting;
  }

  return REVIEW_PRESENTATIONS[review.status];
}

export function isVerifiedReview(review: TrajectoryReview): boolean {
  return trajectoryReviewPresentation(review).key === "verified";
}
