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
