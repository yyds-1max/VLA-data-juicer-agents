import type {
  AnnotationCapability,
  AnnotationConflictDetail,
  AnnotationJobDetail,
  AnnotationJobSummary,
  AnnotationSegmentDetail,
  AnnotationTarget,
  CalibrationProfile,
  CreateAnnotationJobRequest,
} from "./types";

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

export async function getCalibrationProfiles(): Promise<CalibrationProfile[]> {
  const data = await requestJson<{ profiles: CalibrationProfile[] }>(
    `${ROOT}/calibration-profiles?domain=navigation&purpose=processing`,
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
