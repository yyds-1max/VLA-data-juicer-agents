import {
  AnnotationApiError,
  applyFixCommand,
  createAnnotationJob,
  createFixSession,
  getTrajectoryReviewEvidence,
  listTrajectoryReviews,
  mutateAnnotationJob,
  saveAnnotationDraft,
} from "./api";
import type {
  AnnotationJobDetail,
  AnnotationSegmentDetail,
  TrajectoryReview,
} from "./types";

const job: AnnotationJobDetail = {
  job_ref: "job_0123456789abcdef0123456789abcdef",
  dataset_date: "20270605",
  source_clips: ["20260605_160904"],
  status: "waiting_initial_annotation",
  cancel_requested: false,
  completion_outcome: null,
  state_revision: 3,
  calibration: {
    profile_ref: "calibration_0123456789abcdef0123456789abcdef",
    label: "20260529_go2w",
    content_sha256: "a".repeat(64),
  },
  counts: {
    total: 1,
    pending_initial_annotation: 1,
    draft: 0,
    submitted: 0,
    skipped: 0,
    tracking: 0,
    tracked: 0,
  },
  ready_for_tracking: false,
  ready_for_no_processable_targets: false,
  failure: null,
  created_at: "2026-07-23T00:00:00Z",
  updated_at: "2026-07-23T00:00:00Z",
  segments: [],
};

const segment: AnnotationSegmentDetail = {
  segment_ref: "segment_0123456789abcdef0123456789abcdef",
  ordinal: 1,
  source_clip: "20260605_160904",
  status: "draft",
  state_revision: 2,
  draft_revision: 1,
  submitted_revision: null,
  first_frame: {
    url: "/api/annotation/jobs/safe/segments/safe/first-frame",
    width: 1920,
    height: 1536,
    sha256: "b".repeat(64),
    etag: `"${"b".repeat(64)}"`,
  },
  draft: { revision: 1, targets: [] },
  skip_reason: null,
};

const review: TrajectoryReview = {
  review_ref: "review_0123456789abcdef0123456789abcdef",
  status: "pending",
  state_revision: 1,
  job_ref: job.job_ref,
  dataset_date: job.dataset_date,
  source_clip: job.source_clips[0],
  segment_ref: segment.segment_ref,
  segment_ordinal: 1,
  trajectory_revision: {
    revision_ref: "trajectory_revision_0123456789abcdef0123456789abcdef",
    content_sha256: "c".repeat(64),
  },
  processing_calibration: job.calibration,
  fix_draft: null,
  fix_revisions: [],
  active_fix_run: null,
  fix_failure: null,
  latest_publication: null,
  created_at: job.created_at,
  updated_at: job.updated_at,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("create job sends an idempotency key and the frozen calibration hash", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ job }));

  await createAnnotationJob({
    dataset_date: "20270605",
    source_clips: ["20260605_160904"],
    calibration_profile_ref: job.calibration.profile_ref,
    calibration_content_sha256: job.calibration.content_sha256,
  }, "idem-create");

  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/annotation/jobs");
  expect(init?.method).toBe("POST");
  expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("idem-create");
  expect(JSON.parse(String(init?.body))).toEqual({
    dataset_date: "20270605",
    source_clips: ["20260605_160904"],
    calibration_profile_ref: job.calibration.profile_ref,
    calibration_content_sha256: job.calibration.content_sha256,
  });
});

test("draft save URL-encodes refs and carries both CAS revisions", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ segment }));

  await saveAnnotationDraft("job/a", "segment b", {
    expected_segment_revision: 4,
    expected_draft_revision: 2,
    targets: [],
  }, "idem-draft");

  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/annotation/jobs/job%2Fa/segments/segment%20b/draft");
  expect(init?.method).toBe("PUT");
  expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("idem-draft");
  expect(JSON.parse(String(init?.body))).toEqual({
    expected_segment_revision: 4,
    expected_draft_revision: 2,
    targets: [],
  });
});

test("job actions send expected_job_revision and preserve structured 409 details", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({
    detail: {
      code: "revision_conflict",
      message: "任务已更新",
      current: { job },
    },
  }, 409));

  await expect(mutateAnnotationJob(job.job_ref, "tracking", 3, "idem-track")).rejects.toMatchObject({
    name: "AnnotationApiError",
    status: 409,
    detail: {
      code: "revision_conflict",
      message: "任务已更新",
    },
  } satisfies Partial<AnnotationApiError>);
});

test("review list preserves server-side filters and unwraps the public projection", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    jsonResponse({ reviews: [review] }),
  );

  await expect(listTrajectoryReviews({
    status: "pending",
    datasetDate: "20270605",
    sourceClip: "clip with space",
  })).resolves.toEqual([review]);

  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/annotation/reviews?status=pending&dataset_date=20270605&source_clip=clip+with+space",
  );
});

test("Fix session and commands carry idempotency and both CAS revisions", async () => {
  const inProgress = {
    ...review,
    status: "in_progress" as const,
    state_revision: 2,
    fix_draft: {
      revision: 1,
      content_sha256: "d".repeat(64),
      calibration: {
        ...job.calibration,
        differs_from_processing: false,
        difference_reason: null,
      },
    },
  };
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse(inProgress, 201))
    .mockResolvedValueOnce(jsonResponse({
      ...inProgress,
      state_revision: 3,
      fix_draft: { ...inProgress.fix_draft, revision: 2 },
    }));

  await createFixSession(review.review_ref, {
    expected_review_revision: 1,
    calibration_profile_ref: job.calibration.profile_ref,
    calibration_content_sha256: job.calibration.content_sha256,
  }, "idem-session");
  await applyFixCommand(review.review_ref, {
    expected_review_revision: 2,
    expected_draft_revision: 1,
    command: {
      kind: "set_position",
      frame_index: 4,
      target_ref: "target_0123456789abcdef0123456789abcdef",
      x: 1.25,
      y: -2.5,
    },
  }, "idem-command");

  expect(fetchMock.mock.calls[0][0]).toBe(
    `/api/annotation/reviews/${review.review_ref}/fix-sessions`,
  );
  expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Idempotency-Key")).toBe(
    "idem-session",
  );
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
    expected_review_revision: 2,
    expected_draft_revision: 1,
    command: {
      kind: "set_position",
      frame_index: 4,
      target_ref: "target_0123456789abcdef0123456789abcdef",
      x: 1.25,
      y: -2.5,
    },
  });
});

test("evidence validates the exact dedicated public review contract", async () => {
  const payload = {
    availability: "available" as const,
    review_ref: review.review_ref,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: review.state_revision,
    draft_revision: null,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: {
        url: `/api/annotation/reviews/${review.review_ref}/evidence/frames/0/camera`,
        width: 1920,
        height: 1536,
      },
      gridmap: {
        url: `/api/annotation/reviews/${review.review_ref}/evidence/frames/0/gridmap`,
        width: 320,
        height: 240,
      },
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: ["black", "black", "black"],
        image_box: [1, 2, 3, 4],
        trajectory_points: [[1, 2], [3, 4, 5]],
      }],
    }],
    draft_commands: [],
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));

  await expect(getTrajectoryReviewEvidence(review.review_ref)).resolves.toEqual(payload);
  expect(fetchMock.mock.calls[0][0]).toBe(
    `/api/annotation/reviews/${review.review_ref}/evidence/trajectory`,
  );
});

test("evidence rejects stale or path-like media projections", async () => {
  const payload = {
    availability: "available",
    review_ref: review.review_ref,
    trajectory_revision_ref: review.trajectory_revision.revision_ref,
    review_state_revision: review.state_revision,
    draft_revision: null,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: {
        url: "/private/work/segment/frame.jpg",
        width: 1920,
        height: 1536,
      },
      gridmap: null,
      targets: [{
        target_ref: "target_0123456789abcdef0123456789abcdef",
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: [],
        image_box: null,
        trajectory_points: [],
      }],
    }],
    draft_commands: [],
  };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload));

  await expect(getTrajectoryReviewEvidence(review.review_ref)).rejects.toThrow(
    "服务器返回的轨迹证据不符合公开契约。",
  );
});
