import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";

import type {
  AnnotationCounts,
  AnnotationJobStatus,
  AnnotationJobSummary,
} from "./types";
import {
  AnnotationJobProgress,
  buildAnnotationJobProgressSteps,
} from "./AnnotationJobProgress";

function counts(overrides: Partial<AnnotationCounts> = {}): AnnotationCounts {
  return {
    total: 2,
    pending_initial_annotation: 2,
    draft: 0,
    submitted: 0,
    skipped: 0,
    tracking: 0,
    tracked: 0,
    postprocessing: 0,
    annotated: 0,
    postprocessing_failed: 0,
    ...overrides,
  };
}

function jobFixture(overrides: Partial<AnnotationJobSummary> = {}): AnnotationJobSummary {
  return {
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20270623",
    source_clips: ["20260623_145550"],
    status: "preparing",
    cancel_requested: false,
    completion_outcome: null,
    state_revision: 1,
    calibration: {
      profile_ref: "calibration_0123456789abcdef0123456789abcdef",
      label: "20260529_go2w",
      content_sha256: "a".repeat(64),
    },
    counts: counts(),
    ready_for_tracking: false,
    ready_for_no_processable_targets: false,
    failure: null,
    created_at: "2026-08-03T00:00:00Z",
    updated_at: "2026-08-03T00:00:00Z",
    ...overrides,
  };
}

function statesFor(job: AnnotationJobSummary) {
  return buildAnnotationJobProgressSteps(job).map((step) => step.state);
}

test.each([
  ["preparing", false, ["current", "pending", "pending", "pending", "pending"]],
  ["waiting_initial_annotation", false, ["completed", "waiting", "pending", "pending", "pending"]],
  ["tracking", false, ["completed", "completed", "current", "pending", "pending"]],
  ["tracked", false, ["completed", "completed", "completed", "waiting", "pending"]],
  ["postprocessing", false, ["completed", "completed", "completed", "current", "pending"]],
  ["annotated", false, ["completed", "completed", "completed", "completed", "waiting"]],
] satisfies Array<[AnnotationJobStatus, boolean, string[]]>) (
  "maps %s to the five-stage projection",
  (status, readyForTracking, expected) => {
    expect(statesFor(jobFixture({ status, ready_for_tracking: readyForTracking }))).toEqual(expected);
  },
);

test("moves a fully submitted waiting job to the Tracking waiting boundary", () => {
  const job = jobFixture({
    status: "waiting_initial_annotation",
    ready_for_tracking: true,
    counts: counts({ pending_initial_annotation: 0, submitted: 2 }),
  });

  const steps = buildAnnotationJobProgressSteps(job);

  expect(steps.map((step) => step.state)).toEqual([
    "completed",
    "completed",
    "waiting",
    "pending",
    "pending",
  ]);
  expect(steps[2].statusLabel).toBe("等待开始");
});

test("keeps an annotated job at human review waiting rather than claiming approval", () => {
  render(<AnnotationJobProgress job={jobFixture({ status: "annotated" })} />);

  expect(screen.getAllByRole("listitem")).toHaveLength(5);
  expect(screen.getByRole("listitem", { name: "人工复核，待复核" })).toHaveAttribute(
    "aria-current",
    "step",
  );
  expect(screen.getByRole("listitem", { name: "人工复核，待复核" })).toHaveAttribute(
    "data-process-state",
    "waiting",
  );
  expect(screen.queryByText("已批准")).not.toBeInTheDocument();
});

test.each([
  [
    "postprocessing failure",
    jobFixture({
      status: "failed",
      counts: counts({ pending_initial_annotation: 0, postprocessing_failed: 2 }),
    }),
    ["completed", "completed", "completed", "error", "pending"],
  ],
  [
    "tracking failure",
    jobFixture({
      status: "failed",
      counts: counts({ pending_initial_annotation: 0, tracking: 2 }),
    }),
    ["completed", "completed", "error", "pending", "pending"],
  ],
  [
    "running cancellation",
    jobFixture({ status: "tracking", cancel_requested: true }),
    ["completed", "completed", "stopped", "pending", "pending"],
  ],
  [
    "no processable targets",
    jobFixture({
      status: "cancelled",
      completion_outcome: "no_processable_targets",
      counts: counts({ pending_initial_annotation: 0, skipped: 2 }),
    }),
    ["completed", "stopped", "pending", "pending", "pending"],
  ],
] as const)("projects %s without advancing later stages", (_name, job, expected) => {
  expect(statesFor(job)).toEqual(expected);
});

test("labels an all-skipped job honestly", () => {
  const steps = buildAnnotationJobProgressSteps(jobFixture({
    status: "cancelled",
    completion_outcome: "no_processable_targets",
    counts: counts({ pending_initial_annotation: 0, skipped: 2 }),
  }));

  expect(steps[1]).toMatchObject({ state: "stopped", statusLabel: "无可处理目标" });
  expect(steps[2].state).toBe("pending");
});
