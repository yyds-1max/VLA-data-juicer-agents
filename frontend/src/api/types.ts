export type SessionStatus = "draft" | "active" | "historical";
export type SessionEntrypoint =
  | "chat"
  | "data_management_shortcut"
  | "annotation_processing_shortcut";

export type NavigationClipSelection =
  | { kind: "all_clips" }
  | { kind: "selected_clips"; clips: string[] };

export type NavigationDatasetSelectionContext = {
  kind: "navigation_dataset_selection_v1";
  dataset_date: string;
  selection: NavigationClipSelection;
};

export type SessionRequestContext = NavigationDatasetSelectionContext;
export type MessageRole = "user" | "assistant" | "system";
export type TurnOrigin = "user" | "system" | "interaction";
export type TurnStatus = "running" | "waiting" | "completed" | "failed" | "interrupted";

export interface TurnRecord {
  id: string;
  web_session_id: string;
  origin: TurnOrigin;
  status: TurnStatus;
  started_at: string;
  finished_at: string | null;
  final_message_id: string | null;
}

export interface SessionRecord {
  id: string;
  title: string;
  status: SessionStatus;
  contract_version: 1;
  created_at: string;
  updated_at: string;
}

export interface ChatMessageRecord {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
  turn_id?: string | null;
}

export interface TimelineEventRecord extends AgentEvent {
  id: string;
  session_id: string;
  seq: number;
  created_at: string;
}

export interface SessionDetail extends SessionRecord {
  messages: ChatMessageRecord[];
  events: TimelineEventRecord[];
  turns: TurnRecord[];
  tasks: TaskSnapshot[];
  pending_interaction: PendingInteraction | null;
  snapshot_seq?: number;
}

export interface AgentEvent {
  id?: string;
  session_id?: string;
  seq?: number;
  created_at?: string;
  type: string;
  contract_version: 1;
  timestamp?: string | null;
  turn_id?: string | null;
  payload: Record<string, unknown>;
}

export type NavigationTaskStatus =
  | "active"
  | "waiting_user"
  | "pausing"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "needs_replan"
  | "superseded";

export interface TaskCount {
  done: number;
  total: number;
  unit: string;
}

/** Public, contract-v1 task projection. Internal task/session/run ids must never be added here. */
export interface TaskSnapshot {
  task_ref: string;
  domain: string;
  dataset_date: string;
  selection: NavigationClipSelection;
  scene_mode: string | null;
  status: NavigationTaskStatus;
  phase?: string | null;
  waiting_reason?: string | null;
  wait_cause?: string | null;
  latest_public_update?: string | null;
  available_actions?: string[];
  state_revision: number;
  started_at: string;
  updated_at: string;
  count?: TaskCount | null;
}

export type InteractionKind =
  | "high_risk_confirmation"
  | "single_select"
  | "multi_select"
  | "calibration_preview"
  /** Transitional alias accepted from early contract-v1 snapshots. */
  | "calibration_confirmation";

export type InteractionRisk = "low" | "medium" | "high";

export interface InteractionOption {
  option_id: string;
  label: string;
  description?: string | null;
  destructive?: boolean;
  tone?: "default" | "primary" | "danger";
}

/** Public, durable contract-v1 interaction projection. */
export interface PendingInteraction {
  interaction_id: string;
  task_ref: string;
  kind: InteractionKind;
  blocking: boolean;
  risk: InteractionRisk;
  title: string;
  summary: string;
  options: InteractionOption[];
  interaction_revision: number;
  expected_task_revision: number;
  expires_at: string | null;
}

export interface InteractionResponsePayload {
  option_id?: string;
  option_ids?: string[];
  interaction_revision: number;
  expected_task_revision: number;
  idempotency_key: string;
}

export interface InteractionResponseResult {
  accepted: boolean;
  turn_id?: string;
  interaction?: PendingInteraction | null;
  session?: SessionDetail;
}

export type NavigationDatasetStatus = "raw_only" | "extracted" | "synced" | "error";

export type AnnotationLifecycleStatus =
  | "not_started"
  | "waiting_initial_annotation"
  | "processing"
  | "annotated"
  | "failed";

export interface AnnotationLifecycleCounts {
  total: number;
  not_started: number;
  waiting_initial_annotation: number;
  processing: number;
  annotated: number;
  failed: number;
}

export interface AnnotationLifecycleProjection {
  status: AnnotationLifecycleStatus;
  counts: AnnotationLifecycleCounts;
  completed_unit_count: number;
  annotated_unit_count: number;
  job_ref: string | null;
  historical_asset_ref: string | null;
  updated_at: string | null;
  source: "none" | "native" | "historical_import" | "mixed";
}

export type ReviewLifecycleStatus =
  | "pending"
  | "in_progress"
  | "returned"
  | "verified"
  | "discarded"
  | "partial"
  | "completed";

export interface ReviewLifecycleCounts {
  total: number;
  pending: number;
  in_progress: number;
  returned: number;
  verified: number;
  discarded: number;
}

export interface ReviewLifecycleProjection {
  status: ReviewLifecycleStatus;
  counts: ReviewLifecycleCounts;
  resolved_unit_count: number;
  verified_unit_count: number;
  publishable_verified_unit_count: number;
  review_ref: string | null;
  verified_review_ref: string | null;
  historical_asset_ref: string | null;
  updated_at: string | null;
  source: "native" | "historical_import" | "mixed";
}

export type DatasetReleaseStatus = "not_ready" | "ready" | "released";

export interface DatasetReleaseProjection {
  status: DatasetReleaseStatus;
  release_ref: string | null;
  source_clip_count: number;
  total_duration_ns: number;
  verified_unit_count: number;
  discarded_unit_count: number;
  scope_manifest_sha256: string | null;
  note: string | null;
  actor_kind: string | null;
  deployment_instance: string | null;
  released_at: string | null;
  updated_at: string | null;
}

export interface NavigationTopicSummary {
  name: string;
  type: string;
  message_count: number;
}

export interface NavigationSyncFrameCounts {
  image: number;
  pointcloud: number;
  odom: number;
  grid_map: number;
}

export interface NavigationSyncSequenceSummary {
  sequence: string;
  frame_counts: NavigationSyncFrameCounts;
}

export interface NavigationClipSummary {
  date: string;
  clip: string;
  duration_ns: number;
  raw_message_count: number;
  topics: NavigationTopicSummary[];
  has_tmp_dir: boolean;
  has_sync_data: boolean;
  sequences: NavigationSyncSequenceSummary[];
  sync_frame_counts: NavigationSyncFrameCounts;
  status: NavigationDatasetStatus;
  errors: string[];
  annotation?: AnnotationLifecycleProjection | null;
  review?: ReviewLifecycleProjection | null;
}

export interface NavigationDateSummary {
  date: string;
  clip_count: number;
  total_duration_ns: number;
  raw_message_count: number;
  extracted_clip_count: number;
  synced_clip_count: number;
  sync_frame_counts: NavigationSyncFrameCounts;
  status: NavigationDatasetStatus;
  clips?: NavigationClipSummary[];
  annotation?: AnnotationLifecycleProjection | null;
  review?: ReviewLifecycleProjection | null;
  release?: DatasetReleaseProjection;
}

export interface NavigationDatasetRelease extends DatasetReleaseProjection {
  dataset_date: string;
}

export interface NavigationDatasetTotals {
  date_count: number;
  clip_count: number;
  total_duration_ns: number;
  raw_message_count: number;
  extracted_clip_count: number;
  synced_clip_count: number;
}

export interface NavigationDatasetSummary {
  totals: NavigationDatasetTotals;
  sync_distribution: NavigationSyncFrameCounts;
  annotation_totals?: {
    annotated_clip_count: number;
    annotated_duration_ns: number;
    verified_clip_count: number;
    annotated_unit_count: number;
    verified_unit_count: number;
  };
  dates: NavigationDateSummary[];
}

export interface NavigationSyncImageSequence {
  sequence: string;
  images: string[];
}

export interface NavigationSyncImageListing {
  date: string;
  clip: string;
  sequences: NavigationSyncImageSequence[];
}

// Training platform API (contract v1).  These projections intentionally never
// expose model working directories or raw command lines to unprivileged views.
export type TrainingPermission = "training:view" | "training:manage_models" | "training:create_runs" | "training:stop_runs";
export type TrainingModelStatus = "draft" | "verified" | "disabled";
export type TrainingRunStatus = "queued" | "preparing" | "running" | "stop_requested" | "succeeded" | "failed" | "cancelled" | "lost";
export type TrainingParameterType = "integer" | "number" | "boolean" | "enum" | "string";
export type TrainingArgumentStyle = "value" | "explicit_boolean" | "flag_when_true";

export interface TrainingCapabilities {
  permissions: TrainingPermission[];
  authentication_mode: "read_only" | "development_admin" | string;
  simulation_enabled: boolean;
  real_execution_enabled: boolean;
  real_execution_disabled_reason: string;
}

export interface TrainingParameterDefinition {
  key: string;
  label: string;
  type: TrainingParameterType;
  default: string | number | boolean;
  description?: string | null;
  minimum?: number | null;
  maximum?: number | null;
  choices?: Array<{ value: string; label: string }>;
  /** Optional length limits for restricted string parameters. */
  string_min_length?: number | null;
  string_max_length?: number | null;
  /** Enable and emit this parameter only while the referenced parameter equals the configured value. */
  visible_when?: { parameter_key: string; equals: string | number | boolean } | null;
  /** Revision-owned layout metadata. Legacy revisions omit these fields and are grouped by known keys. */
  display_group?: string | null;
  display_group_label?: string | null;
  display_group_order?: number | null;
  editable: boolean;
  sensitive?: boolean;
  /** Controls how the value is represented in argv; booleans usually use explicit_boolean for NaVILA. */
  argument_style?: TrainingArgumentStyle;
  /** Defaults to `--${key}` when omitted. */
  cli_flag?: string | null;
}

export interface TrainingModelRevision {
  revision: number;
  created_at: string;
  parameter_definitions: TrainingParameterDefinition[];
  fixed_argv: string[];
  output_preview?: string | null;
  launch_template?: TrainingLaunchTemplate;
}

/** Admin-only configuration used to build a simulated RunSpec. It is never executed by the browser. */
export interface TrainingLaunchTemplate {
  domain: string;
  server_ref: string;
  working_directory: string;
  executable: string;
  entrypoint: string;
  fixed_argv: string[];
  output_root: string;
  /** The training entrypoint output flag. The platform supplies the directory value. */
  output_flag?: string;
}

export interface TrainingModel {
  model_ref: string;
  name: string;
  description?: string | null;
  status: TrainingModelStatus;
  latest_revision: number;
  created_at: string;
  updated_at: string;
  revision?: TrainingModelRevision;
}

export interface TrainingGpuResource {
  gpu_uuid: string;
  index: number;
  name: string;
  total_memory_mib: number;
  used_memory_mib: number;
  utilization_percent: number;
  temperature_c: number;
  externally_occupied: boolean;
  lease_run_ref?: string | null;
}

export interface TrainingServer {
  server_ref: string;
  name: string;
  kind: "simulation" | string;
  gpu_count: number;
}

export interface TrainingServerResources {
  server: TrainingServer;
  sampled_at: string;
  gpus: TrainingGpuResource[];
}

export interface TrainingRunSpec {
  contract_version: 1;
  execution_mode: "simulation";
  server_ref: string;
  gpu_uuids: string[];
  nnodes: 1;
  master_addr: "127.0.0.1";
  master_port: number;
  node_rank: 0;
  nproc_per_node: number;
  environment: Record<string, string>;
  parameters: Record<string, string | number | boolean>;
  argv: string[];
  output_preview?: string | null;
}

export interface TrainingPreflightResult {
  ok: boolean;
  code?: string | null;
  message: string;
}

export interface TrainingRunPreview {
  run_spec: TrainingRunSpec;
  command_preview: string;
  preflight: TrainingPreflightResult[];
}

export interface TrainingMetricSample {
  seq: number;
  created_at: string;
  step: number;
  total_steps: number;
  epoch: number;
  loss: number;
  learning_rate: number;
  grad_norm?: number | null;
  elapsed_seconds?: number | null;
  gpu_utilization_percent?: number | null;
  gpu_memory_mib?: number | null;
}

export interface TrainingRunLog {
  seq: number;
  created_at: string;
  level: "info" | "warning" | "error";
  message: string;
}

export interface TrainingRun {
  run_ref: string;
  model_ref: string;
  model_name: string;
  model_revision: number;
  status: TrainingRunStatus;
  state_revision: number;
  server_ref: string;
  gpu_uuids: string[];
  progress_percent: number;
  current_step: number;
  total_steps: number;
  current_epoch: number;
  total_epochs: number;
  latest_metric?: TrainingMetricSample | null;
  failure_code?: string | null;
  failure_message?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  parameters?: Record<string, string | number | boolean>;
  run_spec?: TrainingRunSpec;
  audit_events?: Array<{ created_at: string; action: string; summary: string }>;
}

export interface TrainingEvent {
  event_id: number;
  type: "run.updated" | "run.log.appended" | "run.metric.appended";
  run_ref: string;
  seq?: number;
}
