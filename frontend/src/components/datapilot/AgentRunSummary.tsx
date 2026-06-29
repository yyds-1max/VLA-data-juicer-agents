import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "../../lib/utils";
import type { TimelineItem } from "../../store/eventReducer";

export type AgentRunTimelineItem = TimelineItem & {
  createdAt?: string;
  sequence?: number;
};

type AgentRunSummaryProps = {
  source: string;
  items: AgentRunTimelineItem[];
};

type StatusTone = "success" | "failure" | "interrupted" | "pending";

export function AgentRunSummary({ source, items }: AgentRunSummaryProps) {
  const [expanded, setExpanded] = useState(false);
  const summary = summarizeAgentRun(items);
  const tone = aggregateTone(items);

  return (
    <article className="mr-auto w-full max-w-[92%] text-sm text-console-text">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-2 rounded border border-console-line bg-console-bg px-3 py-2 text-left leading-6 transition hover:border-console-cyan/45 focus:outline-none focus:ring-2 focus:ring-console-cyan"
      >
        <StatusDot tone={tone} />
        <span className="min-w-0 flex-1 truncate">{summary}</span>
        <span className="shrink-0 text-[11px] text-console-muted">{sourceLabel(source)}</span>
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-console-muted" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-console-muted" aria-hidden="true" />
        )}
      </button>
      {expanded ? (
        <div className="ml-4 mt-2 space-y-2 border-l border-console-line/80 pl-3">
          {items.map((item, index) => (
            <TimelineDetail key={`${item.kind}-${item.runId ?? item.source}-${item.sequence ?? index}`} item={item} />
          ))}
        </div>
      ) : null}
    </article>
  );
}

export function ToolStatusDot({ status }: { status?: string }) {
  return <StatusDot tone={statusTone(status)} />;
}

function TimelineDetail({ item }: { item: AgentRunTimelineItem }) {
  if (item.kind === "tool") {
    return (
      <div className="flex min-w-0 items-center gap-2 text-xs leading-5 text-console-text">
        <ToolStatusDot status={item.status} />
        <span className="min-w-0 break-words">{item.text}</span>
      </div>
    );
  }

  return (
    <div className="min-w-0 text-xs leading-5">
      <div className="mb-0.5 flex items-center gap-2 uppercase tracking-[0.14em] text-console-muted">
        <span>{item.kind === "assistant" ? "DataPilot" : item.kind}</span>
        <span className="truncate normal-case tracking-normal">{sourceLabel(item.source)}</span>
      </div>
      <p className="whitespace-pre-wrap break-words text-console-text">{item.text}</p>
    </div>
  );
}

function StatusDot({ tone }: { tone: StatusTone }) {
  return (
    <span
      aria-hidden="true"
      data-status={tone}
      className={cn(
        "shrink-0 text-xs leading-none",
        tone === "success" && "text-emerald-600",
        tone === "failure" && "text-rose-600",
        tone === "interrupted" && "text-amber-600",
        tone === "pending" && "text-console-muted",
      )}
    >
      ●
    </span>
  );
}

function summarizeAgentRun(items: AgentRunTimelineItem[]): string {
  const toolNames = items.filter((item) => item.kind === "tool").map((item) => toolName(item.text));
  const fileCount = toolNames.filter(isFileTool).length;
  const commandCount = toolNames.filter(isCommandTool).length;
  const otherToolCount = toolNames.length - fileCount - commandCount;
  const summaryParts: string[] = [];

  if (fileCount > 0) {
    summaryParts.push(`已读取 ${fileCount} 个文件`);
  }
  if (commandCount > 0) {
    summaryParts.push(`执行了 ${commandCount} 条命令`);
  }
  if (otherToolCount > 0) {
    summaryParts.push(`完成了 ${otherToolCount} 个工具`);
  }

  if (summaryParts.length > 0) {
    return summaryParts.join("，");
  }

  const progressCount = items.filter((item) => item.kind === "reasoning" || item.kind === "agent").length;
  return progressCount > 0 ? `记录了 ${progressCount} 条进展` : "完成了子任务";
}

function aggregateTone(items: AgentRunTimelineItem[]): StatusTone {
  const tones = items.filter((item) => item.status).map((item) => statusTone(item.status));
  if (tones.includes("failure")) {
    return "failure";
  }
  if (tones.includes("interrupted")) {
    return "interrupted";
  }
  if (tones.includes("pending")) {
    return "pending";
  }
  return "success";
}

function statusTone(status: string | undefined): StatusTone {
  const normalized = status?.trim().toLowerCase() ?? "";
  if (normalized.includes("fail") || normalized.includes("error")) {
    return "failure";
  }
  if (normalized.includes("interrupt") || normalized.includes("cancel")) {
    return "interrupted";
  }
  if (normalized === "completed" || normalized === "success" || normalized === "succeeded" || normalized === "ok") {
    return "success";
  }
  return "pending";
}

function toolName(text: string): string {
  const parts = text.trim().split(/\s+/);
  return parts[1] ?? parts[0] ?? "";
}

function isFileTool(name: string): boolean {
  return /(^|_)(read|load|open)_?file|file_?(read|load|open)|list_files?/.test(name);
}

function isCommandTool(name: string): boolean {
  return /(exec|command|shell|bash|terminal|run_command)/.test(name);
}

function sourceLabel(source: string): string {
  if (!source || source === "main") {
    return "DataPilot";
  }
  if (source === "navigation.workflow" || source === "navigation.workflow.resume") {
    return "Workflow";
  }
  if (source === "navigation.plan") {
    return "Plan";
  }
  if (source === "navigation.executor") {
    return "Executor";
  }
  return source;
}
