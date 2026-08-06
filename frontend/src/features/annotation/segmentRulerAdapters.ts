import type { StatusTone } from "../console/consoleTypes";
import { trajectoryReviewPresentation } from "./reviewPresentation";
import type {
  AnnotationSegmentStatus,
  AnnotationSegmentSummary,
  TrajectoryReview,
} from "./types";

export type SegmentRulerTone = StatusTone | "mint" | "cyan" | "blue" | "indigo";

export type SegmentRulerItem = {
  id: string;
  ordinal: number;
  label: string;
  tone: SegmentRulerTone;
  resolved: boolean;
};

// 标注工作台和 Fix 工作台共用刻度组件，但两条业务链的状态与“完成”口径不同。
// 适配器在这里收敛差异，避免展示组件自行推断领域状态。
const ANNOTATION_SEGMENT_PRESENTATION: Record<
  AnnotationSegmentStatus,
  Pick<SegmentRulerItem, "label" | "tone" | "resolved">
> = {
  pending_initial_annotation: { label: "待标注", tone: "purple", resolved: false },
  draft: { label: "草稿", tone: "warning", resolved: false },
  submitted: { label: "已提交", tone: "mint", resolved: true },
  skipped: { label: "已跳过", tone: "neutral", resolved: true },
  tracking: { label: "Tracking 中", tone: "cyan", resolved: true },
  tracked: { label: "Tracking 完成", tone: "blue", resolved: true },
  postprocessing: { label: "后处理中", tone: "indigo", resolved: true },
  annotated: { label: "已标注", tone: "success", resolved: true },
  postprocessing_failed: { label: "后处理失败", tone: "danger", resolved: false },
};

export function toAnnotationSegmentRulerItem(
  segment: AnnotationSegmentSummary,
): SegmentRulerItem {
  return {
    id: segment.segment_ref,
    ordinal: segment.ordinal,
    ...ANNOTATION_SEGMENT_PRESENTATION[segment.status],
  };
}

export function annotationSegmentsToRulerItems(
  segments: AnnotationSegmentSummary[],
): SegmentRulerItem[] {
  return segments.map(toAnnotationSegmentRulerItem);
}

export function toTrajectoryReviewRulerItem(
  review: TrajectoryReview,
): SegmentRulerItem {
  const presentation = trajectoryReviewPresentation(review);
  return {
    id: review.review_ref,
    ordinal: review.segment_ordinal,
    label: presentation.label,
    tone: presentation.tone,
    // 人工复核刻度只有真正发布完成的 verified 才计入“已验证”。
    resolved: presentation.key === "verified",
  };
}

export function trajectoryReviewsToRulerItems(
  reviews: TrajectoryReview[],
): SegmentRulerItem[] {
  return reviews.map(toTrajectoryReviewRulerItem);
}
