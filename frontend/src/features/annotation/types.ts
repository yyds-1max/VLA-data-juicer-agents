export const ANNOTATION_COLORS = [
  "black",
  "white",
  "gray",
  "red",
  "yellow",
  "blue",
  "green",
  "pink",
  "purple",
  "brown",
  "orange",
  "camouflage",
  "beige",
  "khaki",
] as const;

export type AnnotationColor = (typeof ANNOTATION_COLORS)[number];

export type AnnotationJobStatus =
  | "preparing"
  | "waiting_initial_annotation"
  | "tracking"
  | "tracked"
  | "failed"
  | "cancelled";

export type AnnotationSegmentStatus =
  | "pending_initial_annotation"
  | "draft"
  | "submitted"
  | "skipped"
  | "tracking"
  | "tracked";

export type AnnotationTarget = {
  target_ref: string;
  bbox: [number, number, number, number] | null;
  point: [number, number] | null;
  colors: {
    upper: AnnotationColor | null;
    lower: AnnotationColor | null;
    shoes: AnnotationColor | null;
  };
};

export type InitialAnnotationDraft = {
  revision: number;
  targets: AnnotationTarget[];
};

export type AnnotationFirstFrame = {
  url: string;
  width: number;
  height: number;
  sha256: string;
  etag: string;
};

export type AnnotationSegmentSummary = {
  segment_ref: string;
  ordinal: number;
  source_clip: string;
  status: AnnotationSegmentStatus;
  state_revision: number;
  draft_revision: number | null;
  submitted_revision: number | null;
  first_frame: AnnotationFirstFrame | null;
};

export type AnnotationSegmentDetail = AnnotationSegmentSummary & {
  draft: InitialAnnotationDraft | null;
  skip_reason: {
    reason_code: "no_valid_target" | "unusable_first_frame" | "other";
    note: string | null;
  } | null;
};

export type AnnotationCounts = {
  total: number;
  pending_initial_annotation: number;
  draft: number;
  submitted: number;
  skipped: number;
  tracking: number;
  tracked: number;
};

export type AnnotationCalibration = {
  profile_ref: string;
  label: string;
  content_sha256: string;
};

export type AnnotationFailure = {
  code: string;
  message: string;
  retryable: boolean;
  error_ref: string | null;
};

export type AnnotationJobSummary = {
  job_ref: string;
  dataset_date: string;
  source_clips: string[];
  status: AnnotationJobStatus;
  cancel_requested: boolean;
  completion_outcome: string | null;
  state_revision: number;
  calibration: AnnotationCalibration;
  counts: AnnotationCounts;
  ready_for_tracking: boolean;
  ready_for_no_processable_targets: boolean;
  failure: AnnotationFailure | null;
  created_at: string;
  updated_at: string;
};

export type AnnotationJobDetail = AnnotationJobSummary & {
  segments: AnnotationSegmentSummary[];
};

export type AnnotationCapability = {
  available: boolean;
  runtime_id: string;
  reason: { code: string; message: string; error_ref?: string } | null;
};

export type CalibrationProfile = AnnotationCalibration;

export type CreateAnnotationJobRequest = {
  dataset_date: string;
  source_clips: string[];
  calibration_profile_ref: string;
  calibration_content_sha256: string;
};

export type AnnotationConflictDetail = {
  code: string;
  message: string;
  current?: unknown;
};
