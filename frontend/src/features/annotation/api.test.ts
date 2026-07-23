import {
  AnnotationApiError,
  createAnnotationJob,
  mutateAnnotationJob,
  saveAnnotationDraft,
} from "./api";
import type { AnnotationJobDetail, AnnotationSegmentDetail } from "./types";

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
