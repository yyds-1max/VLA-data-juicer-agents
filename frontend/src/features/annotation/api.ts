import type {
  AnnotationCapability,
  AnnotationConflictDetail,
  AnnotationJobDetail,
  AnnotationJobSummary,
  AnnotationSegmentDetail,
  AnnotationTarget,
  CalibrationProfile,
  CreateAnnotationJobRequest,
  FixCommand,
  HistoricalVerifiedAsset,
  TrajectoryReview,
  TrajectoryReviewEvidence,
  TrajectoryReviewStatus,
} from "./types";
import { parseTrajectoryReviewEvidence } from "./trajectoryEvidence";

const ROOT = "/api/annotation";

export class AnnotationApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: AnnotationConflictDetail | null,
  ) {
    super(message);
    this.name = "AnnotationApiError";
  }
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `annotation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function apiError(response: Response): Promise<AnnotationApiError> {
  const body = await response.json().catch(() => null) as
    | { detail?: AnnotationConflictDetail | string }
    | null;
  const rawDetail = body?.detail;
  const detail = rawDetail && typeof rawDetail === "object"
    ? rawDetail
    : null;
  const message = typeof rawDetail === "string"
    ? rawDetail
    : detail?.message ?? `${response.status} ${response.statusText}`;
  return new AnnotationApiError(message, response.status, detail);
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      accept: "application/json",
      ...(init?.body ? { "content-type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as T;
}

function mutationInit(body: unknown, idempotencyKey = newIdempotencyKey()): RequestInit {
  return {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
  };
}

function jobPath(jobRef: string): string {
  return `${ROOT}/jobs/${encodeURIComponent(jobRef)}`;
}

function segmentPath(jobRef: string, segmentRef: string): string {
  return `${jobPath(jobRef)}/segments/${encodeURIComponent(segmentRef)}`;
}

function unwrapJob(value: AnnotationJobDetail | { job: AnnotationJobDetail }): AnnotationJobDetail {
  return "job" in value ? value.job : value;
}

function unwrapSegment(
  value: AnnotationSegmentDetail | { segment: AnnotationSegmentDetail },
): AnnotationSegmentDetail {
  return "segment" in value ? value.segment : value;
}

export async function getAnnotationCapabilities(): Promise<AnnotationCapability> {
  return requestJson<AnnotationCapability>(`${ROOT}/capabilities`);
}

export async function getCalibrationProfiles(
  purpose: "processing" | "fix" = "processing",
): Promise<CalibrationProfile[]> {
  const data = await requestJson<{ profiles: CalibrationProfile[] }>(
    `${ROOT}/calibration-profiles?domain=navigation&purpose=${purpose}`,
  );
  return data.profiles;
}

export async function listAnnotationJobs(): Promise<AnnotationJobSummary[]> {
  const data = await requestJson<{ jobs: AnnotationJobSummary[] }>(`${ROOT}/jobs`);
  return data.jobs;
}

export async function createAnnotationJob(
  input: CreateAnnotationJobRequest,
  idempotencyKey?: string,
): Promise<AnnotationJobDetail> {
  const data = await requestJson<AnnotationJobDetail | { job: AnnotationJobDetail }>(
    `${ROOT}/jobs`,
    mutationInit(input, idempotencyKey),
  );
  return unwrapJob(data);
}

export async function getAnnotationJob(jobRef: string): Promise<AnnotationJobDetail> {
  const data = await requestJson<AnnotationJobDetail | { job: AnnotationJobDetail }>(
    jobPath(jobRef),
  );
  return unwrapJob(data);
}

export async function mutateAnnotationJob(
  jobRef: string,
  action: "tracking" | "complete-no-processable-targets" | "cancel" | "retry",
  expectedJobRevision: number,
  idempotencyKey?: string,
): Promise<AnnotationJobDetail> {
  const data = await requestJson<AnnotationJobDetail | { job: AnnotationJobDetail }>(
    `${jobPath(jobRef)}/${action}`,
    mutationInit({ expected_job_revision: expectedJobRevision }, idempotencyKey),
  );
  return unwrapJob(data);
}

export async function getAnnotationSegment(
  jobRef: string,
  segmentRef: string,
): Promise<AnnotationSegmentDetail> {
  const data = await requestJson<AnnotationSegmentDetail | { segment: AnnotationSegmentDetail }>(
    segmentPath(jobRef, segmentRef),
  );
  return unwrapSegment(data);
}
export async function saveAnnotationDraft(
  jobRef: string,
  segmentRef: string,
  body: {
    expected_segment_revision: number;
    expected_draft_revision: number | null;
    targets: AnnotationTarget[];
  },
  idempotencyKey?: string,
): Promise<AnnotationSegmentDetail> {
  const data = await requestJson<AnnotationSegmentDetail | { segment: AnnotationSegmentDetail }>(
    `${segmentPath(jobRef, segmentRef)}/draft`,
    {
      method: "PUT",
      headers: { "Idempotency-Key": idempotencyKey ?? newIdempotencyKey() },
      body: JSON.stringify(body),
    },
  );
  return unwrapSegment(data);
}

export async function submitInitialAnnotation(
  jobRef: string,
  segmentRef: string,
  expectedSegmentRevision: number,
  expectedDraftRevision: number,
  idempotencyKey?: string,
): Promise<AnnotationSegmentDetail> {
  const data = await requestJson<AnnotationSegmentDetail | { segment: AnnotationSegmentDetail }>(
    `${segmentPath(jobRef, segmentRef)}/submit`,
    mutationInit({
      expected_segment_revision: expectedSegmentRevision,
      expected_draft_revision: expectedDraftRevision,
    }, idempotencyKey),
  );
  return unwrapSegment(data);
}

export async function mutateAnnotationSegment(
  jobRef: string,
  segmentRef: string,
  action: "reopen" | "unskip",
  expectedSegmentRevision: number,
  idempotencyKey?: string,
): Promise<AnnotationSegmentDetail> {
  const data = await requestJson<AnnotationSegmentDetail | { segment: AnnotationSegmentDetail }>(
    `${segmentPath(jobRef, segmentRef)}/${action}`,
    mutationInit({ expected_segment_revision: expectedSegmentRevision }, idempotencyKey),
  );
  return unwrapSegment(data);
}

export async function skipAnnotationSegment(
  jobRef: string,
  segmentRef: string,
  expectedSegmentRevision: number,
  reasonCode: "no_valid_target" | "unusable_first_frame" | "other",
  note: string,
  idempotencyKey?: string,
): Promise<AnnotationSegmentDetail> {
  const data = await requestJson<AnnotationSegmentDetail | { segment: AnnotationSegmentDetail }>(
    `${segmentPath(jobRef, segmentRef)}/skip`,
    mutationInit({
      expected_segment_revision: expectedSegmentRevision,
      reason_code: reasonCode,
      ...(note.trim() ? { note: note.trim() } : {}),
    }, idempotencyKey),
  );
  return unwrapSegment(data);
}

function reviewPath(reviewRef: string): string {
  return `${ROOT}/reviews/${encodeURIComponent(reviewRef)}`;
}

function unwrapReview(
  value: TrajectoryReview | { review: TrajectoryReview },
): TrajectoryReview {
  return "review" in value ? value.review : value;
}

export async function listTrajectoryReviews(filters?: {
  status?: TrajectoryReviewStatus;
  datasetDate?: string;
  sourceClip?: string;
}): Promise<TrajectoryReview[]> {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.datasetDate) params.set("dataset_date", filters.datasetDate);
  if (filters?.sourceClip) params.set("source_clip", filters.sourceClip);
  const query = params.size ? `?${params.toString()}` : "";
  const data = await requestJson<
    { reviews: TrajectoryReview[] } | TrajectoryReview[]
  >(`${ROOT}/reviews${query}`);
  return Array.isArray(data) ? data : data.reviews;
}

export async function getTrajectoryReview(reviewRef: string): Promise<TrajectoryReview> {
  const data = await requestJson<TrajectoryReview | { review: TrajectoryReview }>(
    reviewPath(reviewRef),
  );
  return unwrapReview(data);
}

export async function getHistoricalVerifiedAsset(
  assetRef: string,
): Promise<HistoricalVerifiedAsset> {
  const data = await requestJson<
    HistoricalVerifiedAsset | { asset: HistoricalVerifiedAsset }
  >(
    `${ROOT}/verified-assets/${encodeURIComponent(assetRef)}`,
  );
  return "asset" in data ? data.asset : data;
}

export async function getTrajectoryReviewEvidence(
  reviewRef: string,
): Promise<TrajectoryReviewEvidence> {
  const data = await requestJson<unknown>(
    `${reviewPath(reviewRef)}/evidence/trajectory`,
  );
  return parseTrajectoryReviewEvidence(data, reviewRef);
}

export async function createFixSession(
  reviewRef: string,
  body: {
    expected_review_revision: number;
    calibration_profile_ref: string;
    calibration_content_sha256: string;
    calibration_difference_reason?: string;
  },
  idempotencyKey?: string,
): Promise<TrajectoryReview> {
  const data = await requestJson<TrajectoryReview | { review: TrajectoryReview }>(
    `${reviewPath(reviewRef)}/fix-sessions`,
    mutationInit(body, idempotencyKey),
  );
  return unwrapReview(data);
}

export async function applyFixCommand(
  reviewRef: string,
  body: {
    expected_review_revision: number;
    expected_draft_revision: number;
    command: FixCommand;
  },
  idempotencyKey?: string,
): Promise<TrajectoryReview> {
  const data = await requestJson<TrajectoryReview | { review: TrajectoryReview }>(
    `${reviewPath(reviewRef)}/fix-commands`,
    mutationInit(body, idempotencyKey),
  );
  return unwrapReview(data);
}

export async function createFixRevision(
  reviewRef: string,
  body: {
    expected_review_revision: number;
    expected_draft_revision: number;
  },
  idempotencyKey?: string,
): Promise<TrajectoryReview> {
  const data = await requestJson<TrajectoryReview | { review: TrajectoryReview }>(
    `${reviewPath(reviewRef)}/fix-revisions`,
    mutationInit(body, idempotencyKey),
  );
  return unwrapReview(data);
}

export async function decideTrajectoryReview(
  reviewRef: string,
  action: "approve" | "return" | "discard",
  body:
    | { expected_review_revision: number; fix_revision_ref: string }
    | { expected_review_revision: number; reason: string },
  idempotencyKey?: string,
): Promise<TrajectoryReview> {
  const data = await requestJson<TrajectoryReview | { review: TrajectoryReview }>(
    `${reviewPath(reviewRef)}/${action}`,
    mutationInit(body, idempotencyKey),
  );
  return unwrapReview(data);
}

export async function retryReviewPublication(
  reviewRef: string,
  expectedReviewRevision: number,
  idempotencyKey?: string,
): Promise<TrajectoryReview> {
  const data = await requestJson<TrajectoryReview | { review: TrajectoryReview }>(
    `${reviewPath(reviewRef)}/retry-publication`,
    mutationInit(
      { expected_review_revision: expectedReviewRevision },
      idempotencyKey,
    ),
  );
  return unwrapReview(data);
}
