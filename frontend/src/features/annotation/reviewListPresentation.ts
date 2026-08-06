import {
  isVerifiedReview,
  REVIEW_PRESENTATIONS,
  trajectoryReviewPresentation,
  type ReviewPresentation,
  type ReviewPresentationKey,
  type ReviewStatusTone,
} from "./reviewPresentation";
import type {
  TrajectoryReview,
  TrajectoryReviewStatus,
} from "./types";

export type ReviewStatusFilter =
  | "active"
  | "history"
  | "all"
  | "verified"
  | TrajectoryReviewStatus;

export type ReviewDateRange = {
  from: string;
  to: string;
};

export type ReviewStatusBreakdownItem = {
  key: ReviewPresentationKey;
  presentation: ReviewPresentation;
  count: number;
};

export type ReviewCalibrationBreakdownItem = {
  profileRef: string | null;
  label: string;
  count: number;
};

export type ReviewGroupPresentation = {
  key: string;
  datasetDate: string;
  sourceClips: string[];
  reviews: TrajectoryReview[];
  updatedAt: string;
  progress: {
    resolved: number;
    total: number;
    value: number;
  };
  status: {
    label: string;
    tone: ReviewStatusTone;
  };
  statusBreakdown: ReviewStatusBreakdownItem[];
  calibrationBreakdown: ReviewCalibrationBreakdownItem[];
  actionableReview: TrajectoryReview;
};

export type ReviewListMetrics = {
  pending: number;
  inProgress: number;
  returned: number;
  verified: number;
  discarded: number;
};

export const REVIEW_PRESENTATION_ORDER: ReviewPresentationKey[] = [
  "pending",
  "in_progress",
  "returned",
  "approved_waiting",
  "approved_publishing",
  "approved_failed",
  "verified",
  "discarded",
];

// 未指定筛选时，优先把仍需人工处理的 Review 作为分组主操作入口。
const ACTION_PRIORITY: Record<TrajectoryReviewStatus, number> = {
  in_progress: 0,
  returned: 1,
  pending: 2,
  approved: 3,
  discarded: 4,
};

// 分组行只显示一个汇总状态；异常与需要人工介入的状态必须优先于普通进行态。
const GROUP_STATUS_PRIORITY: Record<ReviewPresentationKey, number> = {
  returned: 0,
  approved_failed: 1,
  in_progress: 2,
  pending: 3,
  approved_publishing: 4,
  approved_waiting: 5,
  verified: 6,
  discarded: 7,
};

function normalizedDatasetDate(value: string): string {
  return value.replace(/-/g, "");
}

function statusBreakdown(
  reviews: TrajectoryReview[],
): ReviewStatusBreakdownItem[] {
  const counts = new Map<ReviewPresentationKey, ReviewStatusBreakdownItem>();
  for (const review of reviews) {
    const presentation = trajectoryReviewPresentation(review);
    const current = counts.get(presentation.key);
    counts.set(presentation.key, {
      key: presentation.key,
      presentation,
      count: (current?.count ?? 0) + 1,
    });
  }
  return REVIEW_PRESENTATION_ORDER
    .map((key) => counts.get(key) ?? {
      key,
      presentation: REVIEW_PRESENTATIONS[key],
      count: 0,
    });
}

function calibrationBreakdown(
  reviews: TrajectoryReview[],
): ReviewCalibrationBreakdownItem[] {
  // “使用修整标定”取 Fix 草稿实际选择的标定，而不是任务最初的处理标定。
  const selected = new Map<string, ReviewCalibrationBreakdownItem>();
  let unselected = 0;
  for (const review of reviews) {
    const calibration = review.fix_draft?.calibration;
    if (!calibration) {
      unselected += 1;
      continue;
    }
    const current = selected.get(calibration.profile_ref);
    selected.set(calibration.profile_ref, {
      profileRef: calibration.profile_ref,
      label: calibration.label,
      count: (current?.count ?? 0) + 1,
    });
  }
  const result = [...selected.values()].sort((left, right) => (
    left.label.localeCompare(right.label, "zh-CN")
  ));
  if (unselected > 0) {
    result.push({ profileRef: null, label: "尚未选择", count: unselected });
  }
  return result;
}

function groupStatus(
  breakdown: ReviewStatusBreakdownItem[],
  resolved: number,
  total: number,
): { label: string; tone: ReviewStatusTone } {
  if (total > 0 && resolved === total) {
    const verified = breakdown.find((item) => item.key === "verified")?.count ?? 0;
    const discarded = breakdown.find((item) => item.key === "discarded")?.count ?? 0;
    if (verified === total) return { label: "已验证", tone: "success" };
    if (discarded === total) return { label: "已废弃", tone: "neutral" };
    return { label: "已完成", tone: "success" };
  }
  const primary = breakdown
    .filter((item) => item.count > 0)
    .sort((left, right) => (
      GROUP_STATUS_PRIORITY[left.key] - GROUP_STATUS_PRIORITY[right.key]
    ))[0];
  return primary
    ? { label: primary.presentation.label, tone: primary.presentation.tone }
    : { label: "暂无状态", tone: "neutral" };
}

export function matchesReviewStatus(
  review: TrajectoryReview,
  filter: ReviewStatusFilter,
): boolean {
  if (filter === "all") return true;
  if (filter === "verified") return isVerifiedReview(review);
  if (filter === "active") {
    return review.status === "pending"
      || review.status === "in_progress"
      || review.status === "returned"
      || trajectoryReviewPresentation(review).key === "approved_failed";
  }
  if (filter === "history") {
    return review.status === "approved" || review.status === "discarded";
  }
  return review.status === filter;
}

export function selectActionableReview(
  reviews: TrajectoryReview[],
  filter: ReviewStatusFilter = "all",
): TrajectoryReview {
  // 行操作优先打开符合当前筛选条件的 Segment，避免“筛选已验证却进入修正中任务”。
  // 若调用方传入的筛选在该组内没有匹配项，才回退到完整分组的业务优先级。
  const matchingReviews = filter === "all"
    ? reviews
    : reviews.filter((review) => matchesReviewStatus(review, filter));
  const candidates = matchingReviews.length > 0 ? matchingReviews : reviews;
  const [selected] = [...candidates].sort((left, right) => (
    ACTION_PRIORITY[left.status] - ACTION_PRIORITY[right.status]
    || left.segment_ordinal - right.segment_ordinal
    || left.review_ref.localeCompare(right.review_ref)
  ));
  if (!selected) {
    throw new Error("复核分组中没有可打开的 Review");
  }
  return selected;
}

export function buildReviewGroups(
  reviews: TrajectoryReview[],
): ReviewGroupPresentation[] {
  // 列表的一行代表同一数据日期、同一外层 clip 下的完整 Segment 复核组。
  // 所有进度、状态分布和标定统计都基于完整组，不能被界面筛选条件裁剪。
  const grouped = new Map<string, TrajectoryReview[]>();
  for (const review of reviews) {
    const key = `${review.dataset_date}:${review.source_clip}`;
    const current = grouped.get(key) ?? [];
    current.push(review);
    grouped.set(key, current);
  }

  return [...grouped.entries()]
    .map(([key, groupedReviews]) => {
      const orderedReviews = [...groupedReviews].sort((left, right) => (
        left.segment_ordinal - right.segment_ordinal
        || left.review_ref.localeCompare(right.review_ref)
      ));
      const breakdown = statusBreakdown(orderedReviews);
      const resolved = orderedReviews.filter((review) => (
        isVerifiedReview(review) || review.status === "discarded"
      )).length;
      const total = orderedReviews.length;
      const actionableReview = selectActionableReview(orderedReviews);
      return {
        key,
        datasetDate: orderedReviews[0].dataset_date,
        sourceClips: [...new Set(orderedReviews.map((review) => review.source_clip))],
        reviews: orderedReviews,
        updatedAt: orderedReviews.reduce(
          (latest, review) => review.updated_at > latest ? review.updated_at : latest,
          orderedReviews[0].updated_at,
        ),
        progress: {
          resolved,
          total,
          value: total > 0 ? (resolved / total) * 100 : 0,
        },
        status: groupStatus(breakdown, resolved, total),
        statusBreakdown: breakdown,
        calibrationBreakdown: calibrationBreakdown(orderedReviews),
        actionableReview,
      } satisfies ReviewGroupPresentation;
    })
    .sort((left, right) => (
      right.updatedAt.localeCompare(left.updatedAt) || left.key.localeCompare(right.key)
    ));
}

export function filterReviewGroups(
  groups: ReviewGroupPresentation[],
  options: {
    status: ReviewStatusFilter;
    query: string;
    dateRange: ReviewDateRange;
  },
): ReviewGroupPresentation[] {
  const normalizedQuery = options.query.trim().toLocaleLowerCase();
  const from = normalizedDatasetDate(options.dateRange.from);
  const to = normalizedDatasetDate(options.dateRange.to);
  return groups
    .filter((group) => {
      // 状态筛选只决定整组是否出现，不改变详情浮窗中的完整批次统计。
      if (!group.reviews.some((review) => matchesReviewStatus(review, options.status))) {
        return false;
      }
      if (from && group.datasetDate < from) return false;
      if (to && group.datasetDate > to) return false;
      if (
        normalizedQuery
        && !group.datasetDate.includes(normalizedQuery)
        && !group.sourceClips.some((clip) => (
          clip.toLocaleLowerCase().includes(normalizedQuery)
        ))
      ) {
        return false;
      }
      return true;
    })
    .map((group) => ({
      ...group,
      // 组保留完整数据，但入口目标随当前筛选重新选择，保证点击结果符合用户预期。
      actionableReview: selectActionableReview(group.reviews, options.status),
    }));
}

export function reviewListMetrics(
  reviews: TrajectoryReview[],
): ReviewListMetrics {
  // “已验证”必须满足发布完成语义；仅 approved 但仍在发布或发布失败不能计入。
  return reviews.reduce<ReviewListMetrics>((metrics, review) => {
    if (review.status === "pending") metrics.pending += 1;
    if (review.status === "in_progress") metrics.inProgress += 1;
    if (review.status === "returned") metrics.returned += 1;
    if (isVerifiedReview(review)) metrics.verified += 1;
    if (review.status === "discarded") metrics.discarded += 1;
    return metrics;
  }, {
    pending: 0,
    inProgress: 0,
    returned: 0,
    verified: 0,
    discarded: 0,
  });
}
