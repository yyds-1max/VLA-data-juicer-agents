import type {
  AnnotationCounts,
  AnnotationJobStatus,
  AnnotationJobSummary,
  AnnotationSegmentStatus,
} from "./types";

export type AnnotationJobListFilter =
  | "waiting"
  | "running"
  | "error"
  | "history";

export type AnnotationJobTone =
  | "neutral"
  | "info"
  | "warning"
  | "success"
  | "danger";

export type AnnotationJobStatusModel = {
  label: string;
  tone: AnnotationJobTone;
};

export type AnnotationJobTableProgress = {
  resolved: number;
  total: number;
  value: number;
  stageLabel: string;
};

export type AnnotationJobBreakdownItem = {
  status: AnnotationSegmentStatus;
  label: string;
  count: number;
};

export type AnnotationJobPopoverModel = {
  status: AnnotationJobStatusModel;
  ringValue: number;
  ringLabel: string;
  ringCaption: string;
  progress: AnnotationJobTableProgress;
  breakdown: AnnotationJobBreakdownItem[];
  sourceClips: string[];
  calibrationLabel: string;
  updatedAtLabel: string;
  nextStep: string;
  failureMessage: string | null;
};

export type AnnotationJobListMetrics = {
  waitingSegments: number;
  runningJobs: number;
  failedJobs: number;
  annotatedJobs: number;
};

export const ANNOTATION_JOB_LIST_FILTERS: ReadonlyArray<{
  value: AnnotationJobListFilter;
  label: string;
  title: string;
}> = [
  { value: "waiting", label: "待我处理", title: "待我处理" },
  { value: "running", label: "运行中", title: "运行中" },
  { value: "error", label: "异常", title: "异常任务" },
  { value: "history", label: "历史", title: "历史记录" },
];

const SEGMENT_LABELS: Record<AnnotationSegmentStatus, string> = {
  pending_initial_annotation: "待首帧标注",
  draft: "草稿",
  submitted: "已提交首帧",
  skipped: "已跳过",
  tracking: "Tracking 中",
  tracked: "Tracking 已完成",
  postprocessing: "后处理中",
  annotated: "已标注",
  postprocessing_failed: "后处理失败",
};

const SEGMENT_ORDER = Object.keys(SEGMENT_LABELS) as AnnotationSegmentStatus[];

function toSafeCount(value: number | undefined): number {
  return Number.isFinite(value) ? Math.max(0, Math.trunc(value ?? 0)) : 0;
}

function countOf(counts: AnnotationCounts, status: AnnotationSegmentStatus): number {
  return toSafeCount(counts[status]);
}

function clampPercent(resolved: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, (resolved / total) * 100));
}

function makeProgress(
  resolved: number,
  total: number,
  stageLabel: string,
): AnnotationJobTableProgress {
  const safeTotal = toSafeCount(total);
  const safeResolved = Math.min(safeTotal, toSafeCount(resolved));
  return {
    resolved: safeResolved,
    total: safeTotal,
    value: clampPercent(safeResolved, safeTotal),
    stageLabel,
  };
}

function inferredProgress(job: AnnotationJobSummary): AnnotationJobTableProgress {
  const { counts } = job;
  const total = countOf(counts, "pending_initial_annotation") +
    countOf(counts, "draft") +
    countOf(counts, "submitted") +
    countOf(counts, "skipped") +
    countOf(counts, "tracking") +
    countOf(counts, "tracked") +
    countOf(counts, "postprocessing") +
    countOf(counts, "annotated") +
    countOf(counts, "postprocessing_failed");
  const declaredTotal = toSafeCount(counts.total) || total;
  const skipped = countOf(counts, "skipped");

  if (
    countOf(counts, "postprocessing") > 0 ||
    countOf(counts, "annotated") > 0 ||
    countOf(counts, "postprocessing_failed") > 0
  ) {
    return makeProgress(
      countOf(counts, "annotated") + skipped,
      declaredTotal,
      "后处理完成",
    );
  }
  if (countOf(counts, "tracking") > 0 || countOf(counts, "tracked") > 0) {
    return makeProgress(
      countOf(counts, "tracked") + skipped,
      declaredTotal,
      "Tracking 完成",
    );
  }
  return makeProgress(
    countOf(counts, "submitted") + skipped,
    declaredTotal,
    "首帧处理完成",
  );
}

export function classifyAnnotationJob(
  job: AnnotationJobSummary,
): AnnotationJobListFilter {
  switch (job.status) {
    case "waiting_initial_annotation":
      return "waiting";
    case "preparing":
    case "tracking":
    case "tracked":
    case "postprocessing":
      return "running";
    case "failed":
      return "error";
    case "annotated":
    case "cancelled":
      return "history";
  }
}

export function annotationJobStatusPresentation(
  job: AnnotationJobSummary,
): AnnotationJobStatusModel {
  if (job.cancel_requested && job.status !== "cancelled") {
    return { label: "正在取消", tone: "warning" };
  }

  switch (job.status) {
    case "preparing":
      return { label: "准备中", tone: "info" };
    case "waiting_initial_annotation":
      return { label: "待首帧标注", tone: "warning" };
    case "tracking":
      return { label: "Tracking 中", tone: "info" };
    case "tracked":
      return { label: "Tracking 已完成", tone: "info" };
    case "postprocessing":
      return { label: "后处理中", tone: "info" };
    case "annotated":
      return { label: "已标注", tone: "success" };
    case "failed":
      return { label: "处理失败", tone: "danger" };
    case "cancelled":
      return job.completion_outcome === "no_processable_targets"
        ? { label: "无可处理目标", tone: "neutral" }
        : { label: "已取消", tone: "neutral" };
  }
}

export function annotationJobTableProgress(
  job: AnnotationJobSummary,
): AnnotationJobTableProgress {
  const total = toSafeCount(job.counts.total);
  const skipped = countOf(job.counts, "skipped");

  switch (job.status) {
    case "preparing":
      return makeProgress(0, total, "任务准备完成");
    case "waiting_initial_annotation":
      return makeProgress(
        countOf(job.counts, "submitted") + skipped,
        total,
        "首帧处理完成",
      );
    case "tracking":
    case "tracked":
      return makeProgress(
        countOf(job.counts, "tracked") + skipped,
        total,
        "Tracking 完成",
      );
    case "postprocessing":
    case "annotated":
      return makeProgress(
        countOf(job.counts, "annotated") + skipped,
        total,
        "后处理完成",
      );
    case "failed":
      return inferredProgress(job);
    case "cancelled":
      if (job.completion_outcome === "no_processable_targets") {
        return makeProgress(skipped, total, "已确认无可处理目标");
      }
      return inferredProgress(job);
  }
}

export function annotationJobPrimaryActionLabel(job: AnnotationJobSummary): string {
  switch (classifyAnnotationJob(job)) {
    case "waiting":
      return "继续标注";
    case "running":
      return "查看进度";
    case "error":
      return "查看处理";
    case "history":
      return job.status === "annotated" ? "查看结果" : "查看记录";
  }
}

export function annotationJobNextStep(job: AnnotationJobSummary): string {
  if (job.cancel_requested && job.status !== "cancelled") {
    return "取消请求已提交，请等待当前处理安全结束。";
  }

  switch (job.status) {
    case "preparing":
      return "DataPilot 正在准备首帧，暂时无需人工操作。";
    case "waiting_initial_annotation":
      return "继续完成首帧标注并提交，随后将进入 Tracking。";
    case "tracking":
      return "DataPilot 正在执行 Tracking，期间会持续保存检查点。";
    case "tracked":
      return "Tracking 已完成，DataPilot 将继续执行后处理。";
    case "postprocessing":
      return "DataPilot 正在生成可复核结果，暂时无需人工操作。";
    case "failed":
      if (job.failure?.retryable) {
        return "进入任务详情查看失败信息，并在确认后重试。";
      }
      if (job.failure?.code === "recovery_required") {
        return "请先由运维确认旧处理进程已结束，再继续恢复任务。";
      }
      return "查看失败原因，并选择安全的后续处理方式。";
    case "annotated":
      return "标注结果已生成，可进入人工复核。";
    case "cancelled":
      return job.completion_outcome === "no_processable_targets"
        ? "没有发现有效处理目标，本任务已结束。"
        : "任务已取消，已有处理记录仍会保留。";
  }
}

export function formatAnnotationJobUpdatedAt(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value || "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

export function buildAnnotationJobPopoverModel(
  job: AnnotationJobSummary,
): AnnotationJobPopoverModel {
  const progress = annotationJobTableProgress(job);
  const isRunning = classifyAnnotationJob(job) === "running";
  const runningStep: Partial<Record<AnnotationJobStatus, number>> = {
    preparing: 1,
    tracking: 2,
    tracked: 3,
    postprocessing: 4,
  };
  const step = runningStep[job.status];

  return {
    status: annotationJobStatusPresentation(job),
    ringValue: isRunning && step ? step * 25 : progress.value,
    ringLabel: isRunning && step ? `阶段 ${step}/4` : `${progress.resolved}/${progress.total}`,
    ringCaption: isRunning
      ? annotationJobStatusPresentation(job).label
      : progress.stageLabel,
    progress,
    breakdown: SEGMENT_ORDER.map((status) => ({
      status,
      label: SEGMENT_LABELS[status],
      count: countOf(job.counts, status),
    })).filter((item) => item.count > 0),
    sourceClips: job.source_clips.filter(Boolean),
    calibrationLabel: job.calibration?.label || "未提供",
    updatedAtLabel: formatAnnotationJobUpdatedAt(job.updated_at),
    nextStep: annotationJobNextStep(job),
    failureMessage: job.failure?.message || null,
  };
}

export function annotationJobListMetrics(
  jobs: AnnotationJobSummary[],
): AnnotationJobListMetrics {
  return jobs.reduce<AnnotationJobListMetrics>(
    (metrics, job) => {
      const category = classifyAnnotationJob(job);
      if (category === "waiting") {
        metrics.waitingSegments +=
          countOf(job.counts, "pending_initial_annotation") +
          countOf(job.counts, "draft");
      }
      if (category === "running") metrics.runningJobs += 1;
      if (job.status === "failed") metrics.failedJobs += 1;
      if (job.status === "annotated") metrics.annotatedJobs += 1;
      return metrics;
    },
    { waitingSegments: 0, runningJobs: 0, failedJobs: 0, annotatedJobs: 0 },
  );
}

export function annotationJobsForFilter(
  jobs: AnnotationJobSummary[],
  filter: AnnotationJobListFilter,
): AnnotationJobSummary[] {
  return jobs
    .filter((job) => classifyAnnotationJob(job) === filter)
    .sort((left, right) => {
      const timeDifference =
        new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime();
      if (Number.isFinite(timeDifference) && timeDifference !== 0) {
        return timeDifference;
      }
      return left.job_ref.localeCompare(right.job_ref);
    });
}
