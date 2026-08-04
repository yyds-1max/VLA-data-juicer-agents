import {
  annotationJobListMetrics,
  annotationJobNextStep,
  annotationJobPrimaryActionLabel,
  annotationJobsForFilter,
  annotationJobStatusPresentation,
  annotationJobTableProgress,
  buildAnnotationJobPopoverModel,
  classifyAnnotationJob,
} from "./annotationJobPresentation";
import type { AnnotationJobStatus, AnnotationJobSummary } from "./types";

function jobFixture(
  status: AnnotationJobStatus,
  overrides: Partial<AnnotationJobSummary> = {},
): AnnotationJobSummary {
  return {
    job_ref: `job-${status}`,
    dataset_date: "20260623",
    source_clips: ["clip-001", "clip-002"],
    status,
    cancel_requested: false,
    completion_outcome: null,
    state_revision: 1,
    calibration: {
      profile_ref: "calibration-profile",
      label: "主标定参数",
      content_sha256: "a".repeat(64),
    },
    counts: {
      total: 4,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
      postprocessing: 0,
      annotated: 0,
      postprocessing_failed: 0,
    },
    ready_for_tracking: false,
    ready_for_no_processable_targets: false,
    failure: null,
    created_at: "2026-08-01T08:00:00Z",
    updated_at: "2026-08-01T09:00:00Z",
    ...overrides,
  };
}

describe("annotation job list presentation", () => {
  it("classifies every backend status and keeps tracked in the running list", () => {
    expect(classifyAnnotationJob(jobFixture("waiting_initial_annotation"))).toBe("waiting");
    expect(classifyAnnotationJob(jobFixture("preparing"))).toBe("running");
    expect(classifyAnnotationJob(jobFixture("tracking"))).toBe("running");
    expect(classifyAnnotationJob(jobFixture("tracked"))).toBe("running");
    expect(classifyAnnotationJob(jobFixture("postprocessing"))).toBe("running");
    expect(classifyAnnotationJob(jobFixture("failed"))).toBe("error");
    expect(classifyAnnotationJob(jobFixture("annotated"))).toBe("history");
    expect(classifyAnnotationJob(jobFixture("cancelled"))).toBe("history");
  });

  it("computes the four lightweight metrics from actual job and segment state", () => {
    const jobs = [
      jobFixture("waiting_initial_annotation", {
        counts: {
          ...jobFixture("waiting_initial_annotation").counts,
          total: 5,
          pending_initial_annotation: 2,
          draft: 1,
          submitted: 2,
        },
      }),
      jobFixture("tracked"),
      jobFixture("postprocessing", { job_ref: "job-postprocessing-2" }),
      jobFixture("failed"),
      jobFixture("annotated"),
      jobFixture("cancelled"),
    ];

    expect(annotationJobListMetrics(jobs)).toEqual({
      waitingSegments: 3,
      runningJobs: 2,
      failedJobs: 1,
      annotatedJobs: 1,
    });
  });

  it("uses stage-specific resolved segment counts without inventing a percentage", () => {
    const waiting = jobFixture("waiting_initial_annotation", {
      counts: {
        ...jobFixture("waiting_initial_annotation").counts,
        total: 8,
        pending_initial_annotation: 3,
        draft: 1,
        submitted: 3,
        skipped: 1,
      },
    });
    const tracked = jobFixture("tracked", {
      counts: {
        ...jobFixture("tracked").counts,
        total: 8,
        tracked: 7,
        skipped: 1,
      },
    });
    const postprocessing = jobFixture("postprocessing", {
      counts: {
        ...jobFixture("postprocessing").counts,
        total: 8,
        postprocessing: 5,
        annotated: 2,
        skipped: 1,
      },
    });

    expect(annotationJobTableProgress(waiting)).toMatchObject({
      resolved: 4,
      total: 8,
      stageLabel: "首帧处理完成",
    });
    expect(annotationJobTableProgress(tracked)).toMatchObject({
      resolved: 8,
      total: 8,
      stageLabel: "Tracking 完成",
    });
    expect(annotationJobTableProgress(postprocessing)).toMatchObject({
      resolved: 3,
      total: 8,
      stageLabel: "后处理完成",
    });
  });

  it("treats counts.total as authoritative and only clamps impossible resolved counts", () => {
    const job = jobFixture("tracked", {
      counts: {
        ...jobFixture("tracked").counts,
        total: 2,
        tracked: 9,
        skipped: 3,
      },
    });

    expect(annotationJobTableProgress(job)).toEqual({
      resolved: 2,
      total: 2,
      value: 100,
      stageLabel: "Tracking 完成",
    });
  });

  it.each([
    ["preparing", 25, "阶段 1/4", "准备中"],
    ["tracking", 50, "阶段 2/4", "Tracking 中"],
    ["tracked", 75, "阶段 3/4", "Tracking 已完成"],
    ["postprocessing", 100, "阶段 4/4", "后处理中"],
  ] as const)(
    "uses the four-step coarse ring for %s",
    (status, ringValue, ringLabel, ringCaption) => {
      const model = buildAnnotationJobPopoverModel(jobFixture(status));
      expect(model).toMatchObject({ ringValue, ringLabel, ringCaption });
    },
  );

  it("uses real segment progress in waiting, error, and history popovers", () => {
    const failed = jobFixture("failed", {
      failure: {
        code: "tracking_failed",
        message: "Tracking 进程退出",
        retryable: true,
        error_ref: "error-1",
      },
      counts: {
        ...jobFixture("failed").counts,
        total: 5,
        tracked: 2,
        tracking: 2,
        skipped: 1,
      },
    });

    expect(buildAnnotationJobPopoverModel(failed)).toMatchObject({
      ringValue: 60,
      ringLabel: "3/5",
      failureMessage: "Tracking 进程退出",
    });
  });

  it("exposes status, primary action, and next-step copy for terminal edge cases", () => {
    const noTargets = jobFixture("cancelled", {
      completion_outcome: "no_processable_targets",
    });
    const cancelling = jobFixture("tracking", { cancel_requested: true });
    const retryableFailure = jobFixture("failed", {
      failure: {
        code: "worker_failed",
        message: "worker failed",
        retryable: true,
        error_ref: null,
      },
    });

    expect(annotationJobStatusPresentation(noTargets).label).toBe("无可处理目标");
    expect(annotationJobStatusPresentation(cancelling).label).toBe("正在取消");
    expect(annotationJobPrimaryActionLabel(noTargets)).toBe("查看记录");
    expect(annotationJobPrimaryActionLabel(jobFixture("annotated"))).toBe("查看结果");
    expect(annotationJobNextStep(retryableFailure)).toContain("重试");
    expect(annotationJobNextStep(noTargets)).toContain("没有发现有效处理目标");
  });

  it("filters and orders jobs by latest update without mutating the source", () => {
    const older = jobFixture("tracked", {
      job_ref: "older",
      updated_at: "2026-08-01T08:00:00Z",
    });
    const newer = jobFixture("tracking", {
      job_ref: "newer",
      updated_at: "2026-08-01T10:00:00Z",
    });
    const source = [older, jobFixture("failed"), newer];

    expect(annotationJobsForFilter(source, "running").map((job) => job.job_ref)).toEqual([
      "newer",
      "older",
    ]);
    expect(source.map((job) => job.job_ref)).toEqual(["older", "job-failed", "newer"]);
  });
});
