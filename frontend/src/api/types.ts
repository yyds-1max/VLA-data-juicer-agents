import type { AgentEvent } from "@agentscope-ai/agentscope/event";

export type MessageRole = "user" | "assistant" | "system";

export interface SessionRecord {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessageRecord {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}

export interface PublicEventEnvelope {
  id: string;
  session_id: string;
  sequence: number;
  dedupe_key: string;
  event: AgentEvent;
  created_at: string;
}

export type PublicToolStatus = "running" | "success" | "failure" | "stopped";

export interface PublicToolRun {
  session_id: string;
  tool_call_id: string;
  tool_name: string;
  status: PublicToolStatus;
  summary: string;
  error_type: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SessionDetail extends SessionRecord {
  messages: ChatMessageRecord[];
  events: PublicEventEnvelope[];
  tool_runs: PublicToolRun[];
  last_sequence: number;
}

export type HumanDecisionAction = "confirm" | "stop" | "guide";

export interface PendingHumanDecision {
  replyId: string;
  toolCallId: string;
  requestId: string;
  decisionType: string;
  summary: string;
  options?: string[];
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
