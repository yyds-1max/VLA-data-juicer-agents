export type SessionStatus = "draft" | "active" | "historical";
export type MessageRole = "user" | "assistant" | "system";

export interface SessionRecord {
  id: string;
  title: string;
  status: SessionStatus;
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

export interface SessionDetail extends SessionRecord {
  messages: ChatMessageRecord[];
}

export interface AgentEvent {
  type: string;
  source?: string | null;
  run_id?: string | null;
  parent_run_id?: string | null;
  timestamp?: string | null;
  payload: Record<string, unknown>;
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
