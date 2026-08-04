import { useId } from "react";

import { cn } from "../../lib/utils";
import {
  ProcessTimeline,
  type ProcessTimelineStep,
} from "../console/visuals/ProcessTimeline";
import type { AnnotationJobSummary } from "./types";

export const ANNOTATION_JOB_PROGRESS_STAGES = [
  "准备数据",
  "首帧标注",
  "Tracking",
  "后处理",
  "人工复核",
] as const;

export type AnnotationJobProgressStage =
  (typeof ANNOTATION_JOB_PROGRESS_STAGES)[number];

const baseSteps = (): ProcessTimelineStep[] => (
  ANNOTATION_JOB_PROGRESS_STAGES.map((label) => ({
    id: label,
    label,
    state: "pending",
  }))
);

function setReachedStep(
  steps: ProcessTimelineStep[],
  index: number,
  state: ProcessTimelineStep["state"],
  statusLabel: string,
) {
  steps.forEach((step, stepIndex) => {
    if (stepIndex < index) step.state = "completed";
  });
  steps[index] = { ...steps[index], state, statusLabel };
}

function inferredInterruptedStage(job: AnnotationJobSummary): number {
  if (
    (job.counts.postprocessing_failed ?? 0) > 0
    || (job.counts.postprocessing ?? 0) > 0
    || (job.counts.annotated ?? 0) > 0
  ) {
    return 3;
  }
  if (job.counts.tracking > 0 || job.counts.tracked > 0) return 2;
  if (job.counts.total > 0) return 1;
  return 0;
}

function activeStageForStatus(job: AnnotationJobSummary): number {
  if (job.status === "preparing") return 0;
  if (job.status === "waiting_initial_annotation") return 1;
  if (job.status === "tracking") return 2;
  if (job.status === "tracked" || job.status === "postprocessing") return 3;
  if (job.status === "annotated") return 4;
  return inferredInterruptedStage(job);
}

export function buildAnnotationJobProgressSteps(
  job: AnnotationJobSummary,
): ProcessTimelineStep[] {
  const steps = baseSteps();

  if (job.cancel_requested) {
    setReachedStep(
      steps,
      activeStageForStatus(job),
      "stopped",
      "正在取消",
    );
    return steps;
  }

  if (job.status === "failed") {
    setReachedStep(
      steps,
      inferredInterruptedStage(job),
      "error",
      "处理失败",
    );
    return steps;
  }

  if (job.status === "cancelled") {
    const noProcessableTargets = job.completion_outcome === "no_processable_targets";
    setReachedStep(
      steps,
      noProcessableTargets ? 1 : inferredInterruptedStage(job),
      "stopped",
      noProcessableTargets ? "无可处理目标" : "已取消",
    );
    return steps;
  }

  if (job.status === "preparing") {
    setReachedStep(steps, 0, "current", "准备中");
    return steps;
  }

  if (job.status === "waiting_initial_annotation") {
    if (job.ready_for_tracking) {
      steps[0].state = "completed";
      steps[1].state = "completed";
      steps[2] = { ...steps[2], state: "waiting", statusLabel: "等待开始" };
      return steps;
    }
    setReachedStep(
      steps,
      1,
      "waiting",
      job.ready_for_no_processable_targets ? "待确认无目标" : "待标注",
    );
    return steps;
  }

  if (job.status === "tracking") {
    setReachedStep(steps, 2, "current", "Tracking 中");
    return steps;
  }

  if (job.status === "tracked") {
    steps.slice(0, 3).forEach((step) => {
      step.state = "completed";
    });
    steps[3] = { ...steps[3], state: "waiting", statusLabel: "等待开始" };
    return steps;
  }

  if (job.status === "postprocessing") {
    setReachedStep(steps, 3, "current", "后处理中");
    return steps;
  }

  steps.slice(0, 4).forEach((step) => {
    step.state = "completed";
  });
  steps[4] = { ...steps[4], state: "waiting", statusLabel: "待复核" };
  return steps;
}

export function AnnotationJobProgress({
  job,
  className,
}: {
  job: AnnotationJobSummary;
  className?: string;
}) {
  const headingId = useId();
  const steps = buildAnnotationJobProgressSteps(job);

  return (
    <section className={cn("min-w-0", className)} aria-labelledby={headingId}>
      <h3 id={headingId} className="text-sm font-semibold text-[#202431]">
        任务处理进度
      </h3>
      <ProcessTimeline
        ariaLabel={`${job.dataset_date} 自动标注任务处理进度，可横向滚动`}
        className="mt-3"
        minWidthClassName="min-w-[42rem]"
        steps={steps}
        testIdPrefix="annotation-job-progress"
      />
    </section>
  );
}
