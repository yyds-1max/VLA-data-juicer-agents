import {
  getAnnotationCapabilities,
  getAnnotationJob,
  getAnnotationSegment,
  getTrajectoryReview,
  listAnnotationJobs,
  listTrajectoryReviews,
} from "./api";
import {
  annotationProjectionStore,
  loadAnnotationCapability,
  loadAnnotationJob,
  loadAnnotationJobs,
  loadAnnotationSegment,
  loadTrajectoryReview,
  loadTrajectoryReviews,
  reconcileLoadedAnnotationProjections,
  refreshAnnotationProjectionForEvent,
  resetAnnotationProjectionStore,
  retainAnnotationJobProjection,
  retainAnnotationSegmentProjection,
  retainTrajectoryReviewProjection,
} from "./projectionStore";
import type {
  AnnotationJobDetail,
  AnnotationSegmentDetail,
  TrajectoryReview,
} from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getAnnotationCapabilities: vi.fn(),
    getAnnotationJob: vi.fn(),
    getAnnotationSegment: vi.fn(),
    getTrajectoryReview: vi.fn(),
    listAnnotationJobs: vi.fn(),
    listTrajectoryReviews: vi.fn(),
  };
});

const apiMocks = vi.mocked({
  getAnnotationCapabilities,
  getAnnotationJob,
  getAnnotationSegment,
  getTrajectoryReview,
  listAnnotationJobs,
  listTrajectoryReviews,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function jobFixture(
  jobRef: string,
  stateRevision: number,
  status: AnnotationJobDetail["status"] = "tracking",
): AnnotationJobDetail {
  return {
    job_ref: jobRef,
    dataset_date: "20270623",
    source_clips: ["20260623_145550"],
    status,
    cancel_requested: false,
    completion_outcome: null,
    state_revision: stateRevision,
    calibration: {
      profile_ref: "calibration_0123456789abcdef0123456789abcdef",
      label: "20260529_go2w",
      content_sha256: "a".repeat(64),
    },
    counts: {
      total: 1,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 0,
      skipped: 0,
      tracking: status === "tracking" ? 1 : 0,
      tracked: status === "tracked" ? 1 : 0,
    },
    ready_for_tracking: false,
    ready_for_no_processable_targets: false,
    failure: null,
    segments: [],
    created_at: "2026-07-30T00:00:00Z",
    updated_at: `2026-07-30T00:00:0${stateRevision}Z`,
  };
}

function segmentFixture(
  segmentRef: string,
  stateRevision: number,
): AnnotationSegmentDetail {
  return {
    segment_ref: segmentRef,
    ordinal: 1,
    source_clip: "20260623_145550",
    status: "tracking",
    state_revision: stateRevision,
    draft_revision: 1,
    submitted_revision: 1,
    first_frame: null,
    draft: null,
    skip_reason: null,
  };
}

function reviewFixture(reviewRef: string, stateRevision: number): TrajectoryReview {
  return {
    review_ref: reviewRef,
    status: "pending",
    state_revision: stateRevision,
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20270623",
    source_clip: "20260623_145550",
    segment_ref: "segment_0123456789abcdef0123456789abcdef",
    segment_ordinal: 1,
    trajectory_revision: {
      revision_ref: "trajectory_revision_0123456789abcdef0123456789abcdef",
      content_sha256: "b".repeat(64),
    },
    processing_calibration: {
      profile_ref: "calibration_0123456789abcdef0123456789abcdef",
      label: "20260529_go2w",
      content_sha256: "a".repeat(64),
    },
    fix_draft: null,
    fix_revisions: [],
    active_fix_run: null,
    fix_failure: null,
    latest_publication: null,
    created_at: "2026-07-30T00:00:00Z",
    updated_at: `2026-07-30T00:00:0${stateRevision}Z`,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  resetAnnotationProjectionStore();
});

test("reuses list projections and never regresses a newer aggregate revision", async () => {
  const current = jobFixture("job_0123456789abcdef0123456789abcdef", 2);
  const newer = jobFixture(current.job_ref, 3, "tracked");
  apiMocks.listAnnotationJobs
    .mockResolvedValueOnce([current])
    .mockResolvedValueOnce([current]);
  apiMocks.getAnnotationJob.mockResolvedValue(newer);

  await loadAnnotationJobs();
  await loadAnnotationJobs();
  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(1);

  await loadAnnotationJob(current.job_ref, { force: true });
  await loadAnnotationJobs({ force: true });
  expect(annotationProjectionStore.getState().jobs[0]).toMatchObject({
    state_revision: 3,
    status: "tracked",
  });
});

test("domain events do not materialize detail projections that the SPA never loaded", async () => {
  const job = jobFixture("job_0123456789abcdef0123456789abcdef", 2);
  const segment = segmentFixture(
    "segment_0123456789abcdef0123456789abcdef",
    2,
  );
  const review = reviewFixture(
    "review_0123456789abcdef0123456789abcdef",
    2,
  );
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockResolvedValue(segment);
  apiMocks.getTrajectoryReview.mockResolvedValue(review);

  await refreshAnnotationProjectionForEvent({
    seq: 1,
    event_ref: "annotation_event_1",
    event_kind: "annotation.segment.changed",
    aggregate_kind: "segment",
    job_ref: job.job_ref,
    segment_ref: segment.segment_ref,
    state_revision: 2,
    status: "tracking",
    occurred_at: "2026-07-30T00:00:00Z",
  });
  await refreshAnnotationProjectionForEvent({
    seq: 2,
    event_ref: "annotation_event_2",
    event_kind: "annotation.review.changed",
    aggregate_kind: "review",
    review_ref: review.review_ref,
    state_revision: 2,
    status: "pending",
    occurred_at: "2026-07-30T00:00:01Z",
  });

  expect(apiMocks.getAnnotationJob).not.toHaveBeenCalled();
  expect(apiMocks.getAnnotationSegment).not.toHaveBeenCalled();
  expect(apiMocks.getTrajectoryReview).not.toHaveBeenCalled();
  expect(annotationProjectionStore.getState().jobDetails).toEqual({});
  expect(annotationProjectionStore.getState().segmentDetails).toEqual({});
  expect(annotationProjectionStore.getState().reviewDetails).toEqual({});
});

test("domain events refresh active aggregate projections without rereading lists", async () => {
  const job = jobFixture("job_0123456789abcdef0123456789abcdef", 2);
  const segment = segmentFixture(
    "segment_0123456789abcdef0123456789abcdef",
    2,
  );
  const review = reviewFixture(
    "review_0123456789abcdef0123456789abcdef",
    2,
  );
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockResolvedValue(segment);
  apiMocks.getTrajectoryReview.mockResolvedValue(review);
  const releaseJob = retainAnnotationJobProjection(job.job_ref);
  const releaseSegment = retainAnnotationSegmentProjection(
    job.job_ref,
    segment.segment_ref,
  );
  const releaseReview = retainTrajectoryReviewProjection(review.review_ref);

  try {
    await refreshAnnotationProjectionForEvent({
      seq: 1,
      event_ref: "annotation_event_1",
      event_kind: "annotation.segment.changed",
      aggregate_kind: "segment",
      job_ref: job.job_ref,
      segment_ref: segment.segment_ref,
      state_revision: 2,
      status: "tracking",
      occurred_at: "2026-07-30T00:00:00Z",
    });
    await refreshAnnotationProjectionForEvent({
      seq: 2,
      event_ref: "annotation_event_2",
      event_kind: "annotation.review.changed",
      aggregate_kind: "review",
      review_ref: review.review_ref,
      state_revision: 2,
      status: "pending",
      occurred_at: "2026-07-30T00:00:01Z",
    });
  } finally {
    releaseJob();
    releaseSegment();
    releaseReview();
  }

  expect(apiMocks.getAnnotationJob).toHaveBeenCalledWith(job.job_ref);
  expect(apiMocks.getAnnotationSegment).toHaveBeenCalledWith(
    job.job_ref,
    segment.segment_ref,
  );
  expect(apiMocks.getTrajectoryReview).toHaveBeenCalledWith(review.review_ref);
  expect(apiMocks.listAnnotationJobs).not.toHaveBeenCalled();
  expect(apiMocks.listTrajectoryReviews).not.toHaveBeenCalled();
});

test("forced reads that arrive in flight always perform one trailing revalidation", async () => {
  const jobRef = "job_0123456789abcdef0123456789abcdef";
  const segmentRef = "segment_0123456789abcdef0123456789abcdef";
  const reviewRef = "review_0123456789abcdef0123456789abcdef";

  const jobsFirst = deferred<AnnotationJobDetail[]>();
  apiMocks.listAnnotationJobs
    .mockReturnValueOnce(jobsFirst.promise)
    .mockResolvedValueOnce([jobFixture(jobRef, 2, "tracked")]);
  const jobsPending = loadAnnotationJobs({ force: true });
  const jobsTrailing = loadAnnotationJobs({ force: true });
  jobsFirst.resolve([jobFixture(jobRef, 1)]);
  await Promise.all([jobsPending, jobsTrailing]);
  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(2);
  expect(annotationProjectionStore.getState().jobs[0].state_revision).toBe(2);

  const jobFirst = deferred<AnnotationJobDetail>();
  apiMocks.getAnnotationJob
    .mockReturnValueOnce(jobFirst.promise)
    .mockResolvedValueOnce(jobFixture(jobRef, 4, "tracked"));
  const jobPending = loadAnnotationJob(jobRef, { force: true });
  const jobTrailing = loadAnnotationJob(jobRef, { force: true });
  jobFirst.resolve(jobFixture(jobRef, 3));
  await Promise.all([jobPending, jobTrailing]);
  expect(apiMocks.getAnnotationJob).toHaveBeenCalledTimes(2);
  expect(annotationProjectionStore.getState().jobDetails[jobRef].state_revision).toBe(4);

  const segmentFirst = deferred<AnnotationSegmentDetail>();
  apiMocks.getAnnotationSegment
    .mockReturnValueOnce(segmentFirst.promise)
    .mockResolvedValueOnce(segmentFixture(segmentRef, 3));
  const segmentPending = loadAnnotationSegment(jobRef, segmentRef, { force: true });
  const segmentTrailing = loadAnnotationSegment(jobRef, segmentRef, { force: true });
  segmentFirst.resolve(segmentFixture(segmentRef, 2));
  await Promise.all([segmentPending, segmentTrailing]);
  expect(apiMocks.getAnnotationSegment).toHaveBeenCalledTimes(2);
  expect(
    annotationProjectionStore.getState().segmentDetails[`${jobRef}:${segmentRef}`]
      .state_revision,
  ).toBe(3);

  const reviewsFirst = deferred<TrajectoryReview[]>();
  apiMocks.listTrajectoryReviews
    .mockReturnValueOnce(reviewsFirst.promise)
    .mockResolvedValueOnce([reviewFixture(reviewRef, 2)]);
  const reviewsPending = loadTrajectoryReviews({ force: true });
  const reviewsTrailing = loadTrajectoryReviews({ force: true });
  reviewsFirst.resolve([reviewFixture(reviewRef, 1)]);
  await Promise.all([reviewsPending, reviewsTrailing]);
  expect(apiMocks.listTrajectoryReviews).toHaveBeenCalledTimes(2);
  expect(annotationProjectionStore.getState().reviews[0].state_revision).toBe(2);

  const reviewFirst = deferred<TrajectoryReview>();
  apiMocks.getTrajectoryReview
    .mockReturnValueOnce(reviewFirst.promise)
    .mockResolvedValueOnce(reviewFixture(reviewRef, 4));
  const reviewPending = loadTrajectoryReview(reviewRef, { force: true });
  const reviewTrailing = loadTrajectoryReview(reviewRef, { force: true });
  reviewFirst.resolve(reviewFixture(reviewRef, 3));
  await Promise.all([reviewPending, reviewTrailing]);
  expect(apiMocks.getTrajectoryReview).toHaveBeenCalledTimes(2);
  expect(annotationProjectionStore.getState().reviewDetails[reviewRef].state_revision).toBe(4);
});

test("reconciliation rereads loaded lists, capability, and only active details", async () => {
  const job = jobFixture("job_0123456789abcdef0123456789abcdef", 2);
  const segment = segmentFixture(
    "segment_0123456789abcdef0123456789abcdef",
    2,
  );
  const review = reviewFixture(
    "review_0123456789abcdef0123456789abcdef",
    2,
  );
  apiMocks.listAnnotationJobs.mockResolvedValue([job]);
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockResolvedValue(segment);
  apiMocks.listTrajectoryReviews.mockResolvedValue([review]);
  apiMocks.getTrajectoryReview.mockResolvedValue(review);
  apiMocks.getAnnotationCapabilities.mockResolvedValue({
    available: true,
    runtime_id: "navigation_odom_v1",
    reason: null,
  });

  await Promise.all([
    loadAnnotationJobs(),
    loadAnnotationCapability(),
    loadAnnotationJob(job.job_ref),
    loadAnnotationSegment(job.job_ref, segment.segment_ref),
    loadTrajectoryReviews(),
    loadTrajectoryReview(review.review_ref),
  ]);
  const releaseJob = retainAnnotationJobProjection(job.job_ref);
  const releaseSegment = retainAnnotationSegmentProjection(
    job.job_ref,
    segment.segment_ref,
  );
  const releaseReview = retainTrajectoryReviewProjection(review.review_ref);
  vi.clearAllMocks();
  apiMocks.listAnnotationJobs.mockResolvedValue([job]);
  apiMocks.getAnnotationJob.mockResolvedValue(job);
  apiMocks.getAnnotationSegment.mockResolvedValue(segment);
  apiMocks.listTrajectoryReviews.mockResolvedValue([review]);
  apiMocks.getTrajectoryReview.mockResolvedValue(review);
  apiMocks.getAnnotationCapabilities.mockResolvedValue({
    available: true,
    runtime_id: "navigation_odom_v1",
    reason: null,
  });

  await reconcileLoadedAnnotationProjections();

  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(1);
  expect(apiMocks.getAnnotationJob).toHaveBeenCalledWith(job.job_ref);
  expect(apiMocks.getAnnotationSegment).toHaveBeenCalledWith(
    job.job_ref,
    segment.segment_ref,
  );
  expect(apiMocks.listTrajectoryReviews).toHaveBeenCalledTimes(1);
  expect(apiMocks.getTrajectoryReview).toHaveBeenCalledWith(review.review_ref);
  expect(apiMocks.getAnnotationCapabilities).toHaveBeenCalledTimes(1);

  releaseJob();
  releaseSegment();
  releaseReview();
  vi.clearAllMocks();
  apiMocks.listAnnotationJobs.mockResolvedValue([job]);
  apiMocks.listTrajectoryReviews.mockResolvedValue([review]);
  apiMocks.getAnnotationCapabilities.mockResolvedValue({
    available: true,
    runtime_id: "navigation_odom_v1",
    reason: null,
  });

  await reconcileLoadedAnnotationProjections();

  expect(apiMocks.listAnnotationJobs).toHaveBeenCalledTimes(1);
  expect(apiMocks.listTrajectoryReviews).toHaveBeenCalledTimes(1);
  expect(apiMocks.getAnnotationCapabilities).toHaveBeenCalledTimes(1);
  expect(apiMocks.getAnnotationJob).not.toHaveBeenCalled();
  expect(apiMocks.getAnnotationSegment).not.toHaveBeenCalled();
  expect(apiMocks.getTrajectoryReview).not.toHaveBeenCalled();
});

test("reconciliation recovers a capability probe that previously failed", async () => {
  apiMocks.getAnnotationCapabilities
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce({
      available: true,
      runtime_id: "navigation_odom_v1",
      reason: null,
    });

  await expect(loadAnnotationCapability()).rejects.toThrow("offline");
  expect(annotationProjectionStore.getState().capability).toMatchObject({
    available: false,
  });

  await reconcileLoadedAnnotationProjections();

  expect(apiMocks.getAnnotationCapabilities).toHaveBeenCalledTimes(2);
  expect(annotationProjectionStore.getState().capability).toMatchObject({
    available: true,
  });
});
