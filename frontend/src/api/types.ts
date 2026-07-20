export type SessionStatus = "draft" | "active" | "historical";
export type SessionEntrypoint = "chat" | "data_management_shortcut";
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
  contract_version?: 0 | 1;
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
  events?: TimelineEventRecord[];
  turns?: TurnRecord[];
  tasks?: TaskSnapshot[];
  pending_interaction?: PendingInteraction | null;
}

export interface AgentEvent {
  type: string;
  contract_version?: 0 | 1;
  source?: string | null;
  run_id?: string | null;
  parent_run_id?: string | null;
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

export type HumanDecisionAction = "confirm" | "stop" | "guide";

export interface PendingHumanDecision {
  replyId: string;
  toolCallId: string;
  requestId: string;
  decisionType: string;
  summary: string;
  planId?: string;
  stepId?: string;
  recoveryRequired?: boolean;
  submissionDisabled?: boolean;
  recoveryEndpoint?: string;
}

export interface HumanDecisionPayload {
  action: HumanDecisionAction;
  request_id: string;
  tool_call_id: string;
  reply_id: string;
  plan_id?: string;
  step_id?: string;
  text?: string;
}

export interface HumanDecisionRecoveryRequest {
  action: "quarantine_and_replan";
  plan_id: string;
  step_id: string;
  reason: string;
}

export interface HumanDecisionRecoveryResponse {
  recovered: true;
  plan_id: string;
  step_id: string;
  handoff_status: "quarantined";
  task_status: "needs_replan";
  next_action: "submit_complete_plan";
}

export type NavigationDatasetStatus = "raw_only" | "extracted" | "synced" | "error";

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
