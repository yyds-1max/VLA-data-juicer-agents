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
  | "postprocessing"
  | "annotated"
  | "failed"
  | "cancelled";

export type AnnotationSegmentStatus =
  | "pending_initial_annotation"
  | "draft"
  | "submitted"
  | "skipped"
  | "tracking"
  | "tracked"
  | "postprocessing"
  | "annotated"
  | "postprocessing_failed";

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
  postprocessing?: number;
  annotated?: number;
  postprocessing_failed?: number;
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

export type TrajectoryReviewStatus =
  | "pending"
  | "in_progress"
  | "returned"
  | "approved"
  | "discarded";

export type TrajectoryRevisionSummary = {
  revision_ref: string;
  content_sha256: string;
};

export type FixCalibrationSummary = AnnotationCalibration & {
  differs_from_processing: boolean;
  difference_reason: string | null;
};

export type FixDraftSummary = {
  revision: number;
  content_sha256: string;
  calibration: FixCalibrationSummary;
};

export type FixRevisionSummary = {
  revision_ref: string;
  revision_number: number;
  source_draft_revision: number;
  content_sha256: string;
  created_at: string;
};

export type CompatibilityPublicationSummary = {
  fix_revision_ref: string;
  attempt: number;
  status: "publishing" | "published" | "failed";
  content_sha256: string | null;
  failure: {
    code: string;
    error_ref: string | null;
  } | null;
  created_at: string;
};

export type FixRuntimeRunSummary = {
  status: "queued" | "running" | "failed";
  failure: {
    code: string;
    error_ref: string | null;
  } | null;
  created_at: string;
  updated_at: string;
};

export type FixFailureSummary = {
  code: string;
  message: string;
  error_ref: string | null;
  retryable: boolean;
};

export type TrajectoryReview = {
  review_ref: string;
  status: TrajectoryReviewStatus;
  state_revision: number;
  job_ref: string;
  dataset_date: string;
  source_clip: string;
  segment_ref: string;
  segment_ordinal: number;
  trajectory_revision: TrajectoryRevisionSummary;
  processing_calibration: AnnotationCalibration;
  fix_draft: FixDraftSummary | null;
  fix_revisions: FixRevisionSummary[];
  active_fix_run: FixRuntimeRunSummary | null;
  fix_failure: FixFailureSummary | null;
  latest_publication: CompatibilityPublicationSummary | null;
  submitted_fix_revision_ref?: string;
  created_at: string;
  updated_at: string;
};

export type TrajectoryPoint = {
  x: number;
  y: number;
};

export type TrajectoryCoordinate =
  | [number, number]
  | [number, number, number];

export type TrajectoryEvidenceTarget = {
  target_ref: string;
  label: string;
  position: [number, number] | null;
  direction: number | null;
  speed: number | null;
  color: string[];
  image_box: [number, number, number, number] | null;
  trajectory_points: TrajectoryCoordinate[];
};

export type TrajectoryEvidenceCamera = {
  url: string;
  width: number | null;
  height: number | null;
};

export type TrajectoryEvidenceGridmap = {
  url: string;
  width: number;
  height: number;
};

export type TrajectoryEvidenceFrame = {
  frame_index: number;
  pass: boolean;
  camera: TrajectoryEvidenceCamera | null;
  gridmap: TrajectoryEvidenceGridmap | null;
  targets: TrajectoryEvidenceTarget[];
};

export type TrajectoryReviewEvidence = {
  availability: "available";
  review_ref: string;
  trajectory_revision_ref: string;
  review_state_revision: number;
  draft_revision: number | null;
  frame_count: number;
  frames: TrajectoryEvidenceFrame[];
  draft_commands: FixCommand[];
};

export type FixCommand =
  | {
      kind: "set_position";
      frame_index: number;
      target_ref: string;
      x: number;
      y: number;
    }
  | {
      kind: "set_direction";
      frame_index: number;
      target_ref: string;
      direction: number;
    }
  | {
      kind: "set_speed";
      frame_index: number;
      target_ref: string;
      speed: number;
    }
  | {
      kind: "delete_target";
      frame_index: number;
      target_ref: string;
    }
  | {
      kind: "add_missing_target";
      frame_index: number;
      target_ref: string;
    }
  | {
      kind: "restore_frame";
      frame_index: number;
    }
  | {
      kind: "toggle_pass";
      frame_index: number;
      value: boolean;
    };
