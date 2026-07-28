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

export function trajectoryReviewPresentation(
  review: TrajectoryReview,
): ReviewPresentation {
  if (review.status === "approved") {
    if (review.latest_publication?.status === "published") {
      return { key: "verified", label: "已验证", tone: "success" };
    }
    if (review.latest_publication?.status === "failed") {
      return {
        key: "approved_failed",
        label: "已批准/发布失败",
        tone: "danger",
      };
    }
    if (review.latest_publication?.status === "publishing") {
      return {
        key: "approved_publishing",
        label: "已批准/发布中",
        tone: "info",
      };
    }
    return {
      key: "approved_waiting",
      label: "已批准/待发布",
      tone: "warning",
    };
  }

  const presentations = {
    pending: { key: "pending", label: "待复核", tone: "warning" },
    in_progress: { key: "in_progress", label: "修正中", tone: "info" },
    returned: { key: "returned", label: "已退回", tone: "danger" },
    discarded: { key: "discarded", label: "已废弃", tone: "neutral" },
  } as const;
  return presentations[review.status];
}

export function isVerifiedReview(review: TrajectoryReview): boolean {
  return trajectoryReviewPresentation(review).key === "verified";
}
