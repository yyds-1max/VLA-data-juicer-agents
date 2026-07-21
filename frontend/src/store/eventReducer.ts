import type { AgentEvent } from "../api/types";

export type TimelineKind = "progress" | "action" | "interaction" | "assistant";

export interface TimelineItem {
  kind: TimelineKind;
  text: string;
  status?: string;
  progressId?: string;
  progressPhase?: "streaming" | "completed";
  progressPhaseName?: string;
  actionRef?: string;
  actionCode?: string;
  actionDisplayName?: string;
  actionPhase?: string;
  actionPhaseInstanceId?: string;
  actionStatus?: "running" | "background" | "completed" | "failed" | "interrupted";
  interactionId?: string;
  turnId?: string | null;
  replyId?: string;
  replyKey?: string;
  finalMessageId?: string;
  createdAt?: string;
  sequence?: number;
}

export interface RunState {
  timeline: TimelineItem[];
  running: boolean;
  interrupting: boolean;
  appliedEventKeys: Record<string, true>;
  terminalProgress: Record<string, true>;
}

const PUBLIC_EVENT_TYPES = new Set([
  "turn_start",
  "turn_state",
  "progress_start",
  "progress_delta",
  "progress_end",
  "action_start",
  "action_end",
  "interaction_required",
  "interaction_resolved",
  "task_state_updated",
  "artifact_ready",
  "answer_delta",
  "answer_reset",
  "final",
  "warning",
  "error",
]);

const CLIENT_LOCAL_EVENT_TYPES = new Set(["turn_pending", "turn_submission_failed"]);

export function createEmptyRunState(): RunState {
  return {
    timeline: [],
    running: false,
    interrupting: false,
    appliedEventKeys: {},
    terminalProgress: {},
  };
}

export function applyAgentEvent(state: RunState, event: AgentEvent): void {
  const type = normalizeText(event.type);
  if (!PUBLIC_EVENT_TYPES.has(type) && !CLIENT_LOCAL_EVENT_TYPES.has(type)) return;

  const payload = event.payload ?? {};
  const turnId = normalizeNullableText(event.turn_id);
  const replyId = normalizeText(payload.reply_id) || normalizeText(payload.replyId);
  const replyKey = replyId ? streamReplyKey(turnId, replyId) : "";

  if (type === "turn_start") {
    if (!state.timeline.some((item) => isInitialProgress(item, turnId))) {
      state.timeline.push({ kind: "progress", text: "正在理解你的请求", turnId });
    }
    state.running = true;
    state.interrupting = false;
    return;
  }

  if (type === "turn_pending") {
    state.running = true;
    state.interrupting = false;
    return;
  }

  if (type === "turn_submission_failed") {
    state.running = hasForegroundActivity(state);
    state.interrupting = false;
    return;
  }

  if (type === "turn_state") {
    const status = normalizeText(payload.status);
    state.running = status === "running" || status === "waiting";
    if (!state.running) state.interrupting = false;
    return;
  }

  if (type === "progress_start") {
    const summary = normalizeText(payload.summary);
    if (!summary) return;
    const phase = normalizeText(payload.phase);
    const existing = findProgressByPhaseAndText(state, turnId, phase, summary);
    const terminal = state.terminalProgress[publicProgressKey(turnId, phase)] === true;
    if (existing) {
      if (terminal) existing.progressPhase = "completed";
      return;
    }
    state.timeline.push({
      kind: "progress",
      text: summary,
      turnId,
      progressPhaseName: phase,
      progressPhase: terminal ? "completed" : "streaming",
    });
    return;
  }

  if (type === "progress_delta") {
    const progressId = normalizeText(payload.progress_id) || normalizeText(payload.progressId);
    const summary = normalizeText(payload.summary);
    const phase = normalizeText(payload.phase);
    if (!progressId) {
      if (!summary || findProgressByPhaseAndText(state, turnId, phase, summary)) return;
      state.timeline.push({
        kind: "progress",
        text: summary,
        turnId,
        progressPhaseName: phase,
        progressPhase: state.terminalProgress[publicProgressKey(turnId, phase)]
          ? "completed"
          : "streaming",
      });
      return;
    }
    const delta = typeof payload.delta === "string" ? payload.delta : "";
    if (!delta) return;
    if (replyKey) removeAssistantDraft(state, replyKey);
    const key = progressKey(turnId, progressId);
    const terminal = state.terminalProgress[key] === true;
    const existing = findProgressItem(state, turnId, progressId);
    if (existing) {
      existing.text += delta;
      if (!terminal) existing.progressPhase = "streaming";
    } else {
      state.timeline.push({
        kind: "progress",
        text: delta,
        turnId,
        progressId,
        progressPhase: terminal ? "completed" : "streaming",
      });
    }
    return;
  }

  if (type === "progress_end") {
    const progressId = normalizeText(payload.progress_id) || normalizeText(payload.progressId);
    if (progressId) {
      state.terminalProgress[progressKey(turnId, progressId)] = true;
      const existing = findProgressItem(state, turnId, progressId);
      if (existing) existing.progressPhase = "completed";
      return;
    }
    const phase = normalizeText(payload.phase);
    state.terminalProgress[publicProgressKey(turnId, phase)] = true;
    for (const item of state.timeline) {
      if (
        item.kind === "progress" &&
        item.turnId === turnId &&
        (!phase || item.progressPhaseName === phase)
      ) {
        item.progressPhase = "completed";
      }
    }
    return;
  }

  if (type === "action_start" || type === "action_end") {
    const actionRef = normalizeText(payload.action_ref) || normalizeText(payload.actionRef);
    if (!actionRef) return;
    const phaseInstanceId =
      normalizeText(payload.phase_instance_id) || normalizeText(payload.phaseInstanceId);
    const displayName =
      normalizeText(payload.display_name) || normalizeText(payload.displayName) || "处理数据";
    const actionStatus = publicActionStatus(type, payload);
    const next: TimelineItem = {
      kind: "action",
      text: displayName,
      turnId,
      actionRef,
      actionCode: normalizeText(payload.action_code) || normalizeText(payload.actionCode),
      actionDisplayName: displayName,
      actionPhase: normalizeText(payload.phase),
      actionPhaseInstanceId: phaseInstanceId,
      actionStatus,
      status: actionStatus,
    };
    const existing = findPublicActionItem(state, turnId, actionRef, phaseInstanceId);
    if (existing) Object.assign(existing, next);
    else state.timeline.push(next);
    if (type === "action_start") {
      state.running = true;
      state.interrupting = false;
    }
    return;
  }

  if (type === "interaction_required") {
    const interactionId = interactionIdentity(payload);
    if (!interactionId) return;
    const next: TimelineItem = {
      kind: "interaction",
      text: normalizeText(payload.summary) || normalizeText(payload.title) || "等待你的选择",
      status: "waiting",
      interactionId,
      turnId,
    };
    const existing = state.timeline.find(
      (item) => item.kind === "interaction" && item.interactionId === interactionId,
    );
    if (existing) Object.assign(existing, next);
    else state.timeline.push(next);
    state.running = false;
    state.interrupting = false;
    return;
  }

  if (type === "interaction_resolved") {
    const interactionId = interactionIdentity(payload);
    if (!interactionId) return;
    const text =
      normalizeText(payload.result_label) || normalizeText(payload.resultLabel) || "已提交选择";
    const existing = state.timeline.find(
      (item) => item.kind === "interaction" && item.interactionId === interactionId,
    );
    if (existing) {
      existing.text = text;
      existing.status = "completed";
    } else {
      state.timeline.push({
        kind: "interaction",
        text,
        status: "completed",
        interactionId,
        turnId,
      });
    }
    return;
  }

  if (type === "warning" || type === "error") {
    const text = normalizeText(payload.text) || normalizeText(payload.message);
    if (text) state.timeline.push({ kind: "progress", text, status: type, turnId });
    return;
  }

  if (type === "answer_reset") {
    if (replyKey) removeAssistantDraft(state, replyKey);
    return;
  }

  if (type === "answer_delta") {
    const delta = typeof payload.delta === "string" ? payload.delta : "";
    if (!delta) return;
    const candidate = findAssistantItem(state, turnId, replyId);
    if (candidate?.status === "final") return;
    if (candidate) candidate.text += delta;
    else {
      state.timeline.push({
        kind: "assistant",
        text: delta,
        turnId,
        ...(replyId ? { replyId, replyKey } : {}),
      });
    }
    state.running = true;
    state.interrupting = false;
    return;
  }

  if (type === "final") {
    const text = typeof payload.text === "string" ? payload.text : "";
    const finalMessageId = normalizeText(payload.message_id) || normalizeText(payload.messageId);
    if (text.trim()) {
      const existing = findAssistantItem(state, turnId, replyId);
      if (existing) {
        existing.text = text;
        existing.status = "final";
        if (replyId) {
          existing.replyId = replyId;
          existing.replyKey = replyKey;
        }
        if (finalMessageId) existing.finalMessageId = finalMessageId;
      } else {
        state.timeline.push({
          kind: "assistant",
          text,
          turnId,
          status: "final",
          ...(replyId ? { replyId, replyKey } : {}),
          ...(finalMessageId ? { finalMessageId } : {}),
        });
      }
    }
    state.running = false;
    state.interrupting = false;
  }
}

function isInitialProgress(item: TimelineItem, turnId: string | null): boolean {
  return item.kind === "progress" && item.turnId === turnId && item.text === "正在理解你的请求";
}

function findAssistantItem(
  state: RunState,
  turnId: string | null,
  replyId: string,
): TimelineItem | undefined {
  if (replyId) {
    const key = streamReplyKey(turnId, replyId);
    for (let index = state.timeline.length - 1; index >= 0; index -= 1) {
      const item = state.timeline[index];
      if (item.kind === "assistant" && timelineReplyKey(item) === key) return item;
    }
    return undefined;
  }
  for (let index = state.timeline.length - 1; index >= 0; index -= 1) {
    const item = state.timeline[index];
    if (item.kind === "assistant" && item.turnId === turnId) return item;
  }
  return undefined;
}

function findProgressItem(
  state: RunState,
  turnId: string | null,
  progressId: string,
): TimelineItem | undefined {
  return state.timeline.find(
    (item) =>
      item.kind === "progress" && item.turnId === turnId && item.progressId === progressId,
  );
}

function findProgressByPhaseAndText(
  state: RunState,
  turnId: string | null,
  phase: string,
  text: string,
): TimelineItem | undefined {
  return state.timeline.find(
    (item) =>
      item.kind === "progress" &&
      item.turnId === turnId &&
      (item.progressPhaseName ?? "") === phase &&
      item.text === text,
  );
}

function findPublicActionItem(
  state: RunState,
  turnId: string | null,
  actionRef: string,
  phaseInstanceId: string,
): TimelineItem | undefined {
  for (let index = state.timeline.length - 1; index >= 0; index -= 1) {
    const item = state.timeline[index];
    if (item.kind !== "action" || item.turnId !== turnId || item.actionRef !== actionRef) continue;
    if (!phaseInstanceId || (item.actionPhaseInstanceId ?? "") === phaseInstanceId) return item;
  }
  return undefined;
}

function publicActionStatus(
  type: "action_start" | "action_end",
  payload: Record<string, unknown>,
): NonNullable<TimelineItem["actionStatus"]> {
  const status = normalizeText(payload.status);
  if (status === "background") return "background";
  if (type === "action_start") return "running";
  if (status === "failed" || status === "interrupted") return status;
  return "completed";
}

function interactionIdentity(payload: Record<string, unknown>): string {
  return (
    normalizeText(payload.interaction_id) ||
    normalizeText(payload.interactionId) ||
    normalizeText(payload.interaction_ref) ||
    normalizeText(payload.interactionRef)
  );
}

function hasForegroundActivity(state: RunState): boolean {
  return state.timeline.some(
    (item) => item.kind === "action" && item.actionStatus === "running",
  );
}

function progressKey(turnId: string | null | undefined, progressId: string): string {
  return `${turnId ?? ""}\u0000progress\u0000${progressId}`;
}

function publicProgressKey(turnId: string | null | undefined, phase: string): string {
  return `${turnId ?? ""}\u0000public-phase\u0000${phase || "generic"}`;
}

function removeAssistantDraft(state: RunState, replyKey: string): void {
  state.timeline = state.timeline.filter(
    (item) =>
      item.kind !== "assistant" || timelineReplyKey(item) !== replyKey || item.status === "final",
  );
}

function streamReplyKey(turnId: string | null | undefined, replyId: string): string {
  return `${turnId ?? ""}\u0000${replyId}`;
}

function timelineReplyKey(item: TimelineItem): string {
  if (item.replyKey) return item.replyKey;
  return item.replyId ? streamReplyKey(item.turnId, item.replyId) : "";
}

function normalizeNullableText(value: unknown): string | null {
  return normalizeText(value) || null;
}

function normalizeText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}
