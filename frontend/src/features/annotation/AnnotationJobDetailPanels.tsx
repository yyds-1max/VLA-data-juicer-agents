import {
  AlertCircle,
  Check,
  CircleDot,
  Info,
  LoaderCircle,
} from "lucide-react";

import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { StatusTag } from "../../components/console/StatusTag";
import { cn } from "../../lib/utils";
import { formatAnnotationJobUpdatedAt } from "./annotationJobPresentation";
import type {
  AnnotationJobDetail,
  AnnotationJobStatus,
  AnnotationSegmentSummary,
} from "./types";

type NextStepAction = "segment" | "reviews" | null;

export type AnnotationJobNextStepModel = {
  title: string;
  detail: string;
  action: NextStepAction;
  actionLabel: string | null;
  segment: AnnotationSegmentSummary | null;
  state: "active" | "complete" | "attention" | "idle";
};

function sortedSegments(job: AnnotationJobDetail) {
  return [...job.segments].sort((left, right) => (
    left.ordinal - right.ordinal
    || left.segment_ref.localeCompare(right.segment_ref)
  ));
}

function nextAnnotationSegment(job: AnnotationJobDetail) {
  const segments = sortedSegments(job);
  return segments.find((segment) => segment.status === "draft")
    ?? segments.find((segment) => segment.status === "pending_initial_annotation")
    ?? null;
}

function segmentOrdinal(segment: AnnotationSegmentSummary) {
  return Number.isSafeInteger(segment.ordinal) && segment.ordinal >= 0
    ? String(segment.ordinal).padStart(2, "0")
    : "—";
}

export function buildAnnotationJobNextStep(
  job: AnnotationJobDetail,
): AnnotationJobNextStepModel {
  if (job.cancel_requested) {
    return {
      title: "正在安全终止处理任务",
      detail: "系统正在等待 Runtime 进程确认退出；确认前会保留任务状态和数据范围。",
      action: null,
      actionLabel: null,
      segment: null,
      state: "active",
    };
  }

  switch (job.status) {
    case "preparing":
      return {
        title: "正在准备 Web 首帧标注",
        detail: "系统正在隔离的 staging 中生成 resize 后首帧，此阶段不需要人工操作。",
        action: null,
        actionLabel: null,
        segment: null,
        state: "active",
      };
    case "waiting_initial_annotation": {
      const segment = nextAnnotationSegment(job);
      if (job.ready_for_tracking || !segment) {
        return {
          title: "首帧标注已全部提交",
          detail: "提交结果已持久保存，DataPilot 将从原任务继续执行 Tracking 和后处理。",
          action: null,
          actionLabel: null,
          segment: null,
          state: "complete",
        };
      }
      return {
        title: `Segment ${segmentOrdinal(segment)}`,
        detail: "请完成该 Segment 的首帧目标标注；提交后，它将进入 Tracking 与后处理流程。",
        action: "segment",
        actionLabel: segment.status === "draft" ? "继续编辑" : "继续标注",
        segment,
        state: "attention",
      };
    }
    case "tracking":
      return {
        title: "Tracking 正在串行执行",
        detail: "系统正在根据首帧目标生成连续轨迹，并持续保存可恢复的处理检查点。",
        action: null,
        actionLabel: null,
        segment: null,
        state: "active",
      };
    case "tracked":
      return {
        title: "Tracking 已完成",
        detail: "轨迹已生成，DataPilot 将继续检查数据并启动适合当前任务的后处理。",
        action: null,
        actionLabel: null,
        segment: null,
        state: "complete",
      };
    case "postprocessing":
      return {
        title: "DataPilot 正在执行后处理",
        detail: "系统正在清理、修正并固化轨迹结果；任务事实和恢复点会持续保存。",
        action: null,
        actionLabel: null,
        segment: null,
        state: "active",
      };
    case "annotated":
      return {
        title: "后处理已完成",
        detail: "轨迹复核任务已创建，可在“人工复核”中检查结果并继续 Fix。",
        action: "reviews",
        actionLabel: "进入人工复核",
        segment: null,
        state: "complete",
      };
    case "failed":
      return {
        title: "等待处理失败项",
        detail: "请先查看上方失败信息；只有确认失败原因和恢复条件后，才能安全继续任务。",
        action: null,
        actionLabel: null,
        segment: null,
        state: "attention",
      };
    case "cancelled":
      return {
        title: job.completion_outcome === "no_processable_targets" ? "没有可处理目标" : "任务已取消",
        detail: job.completion_outcome === "no_processable_targets"
          ? "数据范围中没有发现有效目标，本任务已安全结束。"
          : "任务已停止，已持久化的处理记录仍会保留。",
        action: null,
        actionLabel: null,
        segment: null,
        state: "idle",
      };
  }
}

const stateIcon = {
  active: LoaderCircle,
  complete: Check,
  attention: CircleDot,
  idle: AlertCircle,
} as const;

const stateIconClass = {
  active: "text-[#3156C8] motion-safe:animate-spin",
  complete: "text-[#2FA66A]",
  attention: "text-[#E59A18]",
  idle: "text-[#7B8496]",
} as const;

const segmentTag = {
  pending_initial_annotation: { label: "待标注", tone: "warning" },
  draft: { label: "草稿", tone: "info" },
} as const;

export function AnnotationJobNextStep({
  job,
  onOpenSegment,
  onOpenReviews,
}: {
  job: AnnotationJobDetail;
  onOpenSegment: (segmentRef: string) => void;
  onOpenReviews: () => void;
}) {
  const model = buildAnnotationJobNextStep(job);
  const Icon = stateIcon[model.state];
  const firstFrame = model.segment?.first_frame;
  const status = model.segment?.status === "pending_initial_annotation" || model.segment?.status === "draft"
    ? segmentTag[model.segment.status]
    : null;

  const action = model.action === "segment" && model.segment
    ? () => onOpenSegment(model.segment!.segment_ref)
    : model.action === "reviews"
      ? onOpenReviews
      : null;

  return (
    <ConsoleCard className="overflow-hidden p-0">
      <div className="border-b border-console-line px-4 py-3">
        <h3 className="text-sm font-semibold text-console-text">下一步</h3>
      </div>
      <div className={cn(
        "grid min-h-44 items-center gap-4 px-4 py-4 sm:px-5",
        firstFrame ? "md:grid-cols-[11rem_minmax(0,1fr)_minmax(15rem,0.9fr)]" : "md:grid-cols-[minmax(0,1fr)_minmax(17rem,0.8fr)]",
      )}>
        {firstFrame ? (
          <img
            alt={`${model.title} 首帧预览`}
            className="aspect-[16/10] w-full rounded-lg border border-console-line bg-[#F5F6F8] object-cover"
            height={firstFrame.height}
            loading="lazy"
            src={firstFrame.url}
            width={firstFrame.width}
          />
        ) : null}

        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Icon
              aria-hidden="true"
              className={cn("h-5 w-5 shrink-0", stateIconClass[model.state])}
            />
            <h2 className="truncate text-base font-semibold text-console-text">{model.title}</h2>
            {status ? <StatusTag tone={status.tone}>{status.label}</StatusTag> : null}
          </div>
          {model.segment ? (
            <dl className="mt-3 grid gap-x-4 gap-y-1.5 text-xs sm:grid-cols-2">
              <div className="min-w-0">
                <dt className="inline text-console-muted">来源剪辑：</dt>
                <dd className="inline break-all text-console-text">{model.segment.source_clip}</dd>
              </div>
              <div>
                <dt className="inline text-console-muted">队列序号：</dt>
                <dd className="inline tabular-nums text-console-text">{segmentOrdinal(model.segment)}</dd>
              </div>
            </dl>
          ) : null}
        </div>

        <div className="border-t border-console-line pt-4 md:border-l md:border-t-0 md:pl-5 md:pt-0">
          <p className="text-sm leading-6 text-console-muted">{model.detail}</p>
          {action && model.actionLabel ? (
            <ConsoleButton className="mt-3" variant="primary" onClick={action}>
              {model.actionLabel}
            </ConsoleButton>
          ) : null}
        </div>
      </div>
    </ConsoleCard>
  );
}

type ActivityItem = {
  title: string;
  detail: string;
  time: string | null;
  tone: "success" | "active" | "neutral" | "danger";
};

function jobStageRank(status: AnnotationJobStatus) {
  const ranks: Record<AnnotationJobStatus, number> = {
    preparing: 0,
    waiting_initial_annotation: 1,
    tracking: 2,
    tracked: 3,
    postprocessing: 4,
    annotated: 5,
    failed: -1,
    cancelled: -1,
  };
  return ranks[status];
}

function buildAnnotationJobActivities(job: AnnotationJobDetail): ActivityItem[] {
  const items: ActivityItem[] = [];
  const rank = jobStageRank(job.status);
  const resolved = job.counts.submitted + job.counts.skipped;

  if (job.status === "failed") {
    items.push({
      title: "任务处理失败",
      detail: job.failure?.code ? `失败代码：${job.failure.code}` : "任务需要检查后才能继续。",
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "danger",
    });
  } else if (job.status === "cancelled") {
    items.push({
      title: job.completion_outcome === "no_processable_targets" ? "未发现可处理目标" : "任务已取消",
      detail: "任务已结束，已有事实记录仍然保留。",
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "neutral",
    });
  } else if (rank === 0) {
    items.push({
      title: "正在准备首帧数据",
      detail: "系统正在生成可用于 Web 标注的首帧资源。",
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "active",
    });
  } else if (rank === 1) {
    items.push({
      title: job.ready_for_tracking ? "首帧标注已全部提交" : "首帧标注进行中",
      detail: `${resolved} / ${job.counts.total} 个 Segment 已提交或跳过。`,
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: job.ready_for_tracking ? "success" : "active",
    });
  } else if (rank === 2) {
    items.push({
      title: "Tracking 正在执行",
      detail: `${job.counts.tracked} / ${job.counts.total} 个 Segment 已完成 Tracking。`,
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "active",
    });
  } else if (rank === 3) {
    items.push({
      title: "Tracking 已完成",
      detail: `${job.counts.tracked} 个 Segment 已生成轨迹，等待后处理。`,
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "success",
    });
  } else if (rank === 4) {
    items.push({
      title: "后处理正在执行",
      detail: `${job.counts.annotated ?? 0} / ${job.counts.total} 个 Segment 已生成标注结果。`,
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "active",
    });
  } else if (rank === 5) {
    items.push({
      title: "后处理已完成",
      detail: `${job.counts.annotated ?? job.counts.total} 个 Segment 已生成标注结果。`,
      time: formatAnnotationJobUpdatedAt(job.updated_at),
      tone: "success",
    });
  }

  if (rank >= 3) {
    items.push({
      title: "Tracking 阶段完成",
      detail: `${job.counts.tracked || job.counts.total} 个 Segment 的轨迹结果已持久保存。`,
      time: null,
      tone: "success",
    });
  }
  if (rank >= 2) {
    items.push({
      title: "首帧标注阶段完成",
      detail: `${resolved || job.counts.total} 个 Segment 已提交或跳过。`,
      time: null,
      tone: "success",
    });
  }
  if (rank >= 1) {
    items.push({
      title: "首帧数据准备完成",
      detail: `${job.counts.total} 个 Segment 已初始化。`,
      time: null,
      tone: "success",
    });
  }

  items.push({
    title: "任务已创建",
    detail: `${job.source_clips.length} 个外层 clips 已纳入本次处理范围。`,
    time: formatAnnotationJobUpdatedAt(job.created_at),
    tone: "neutral",
  });

  return items.slice(0, 5);
}

const activityDotClass = {
  success: "border-[#BCEAD2] bg-[#2FA66A] text-white",
  active: "border-[#BDD0FF] bg-[#EEF3FF] text-[#3156C8]",
  neutral: "border-[#D8DEEC] bg-[#F5F6F8] text-[#7B8496]",
  danger: "border-[#F5C4CC] bg-[#FFF1F3] text-[#D84A5B]",
} as const;

export function AnnotationJobActivity({
  job,
  guidance,
}: {
  job: AnnotationJobDetail;
  guidance: string;
}) {
  const activities = buildAnnotationJobActivities(job);

  return (
    <ConsoleCard className="overflow-hidden p-0">
      <div className="border-b border-console-line px-4 py-3">
        <h3 className="text-sm font-semibold text-console-text">任务动态</h3>
      </div>
      <ol className="px-4 py-4 sm:px-5">
        {activities.map((item, index) => (
          <li key={`${item.title}-${index}`} className="relative grid grid-cols-[1.5rem_minmax(0,1fr)_auto] gap-x-3 pb-4 last:pb-0">
            {index < activities.length - 1 ? (
              <span aria-hidden="true" className="absolute left-[0.6875rem] top-5 h-[calc(100%-0.5rem)] w-px bg-[#E1E5EE]" />
            ) : null}
            <span
              aria-hidden="true"
              className={cn(
                "relative z-10 mt-0.5 flex h-5 w-5 items-center justify-center rounded-full border",
                activityDotClass[item.tone],
              )}
            >
              {item.tone === "success" ? <Check className="h-3 w-3" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" />}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-medium text-console-text">{item.title}</p>
              <p className="mt-0.5 text-xs leading-5 text-console-muted">{item.detail}</p>
            </div>
            <span className="whitespace-nowrap pt-0.5 text-[11px] tabular-nums text-[#8B94A6]">
              {item.time ?? "阶段记录"}
            </span>
          </li>
        ))}
      </ol>
      <div className="mx-4 mb-4 flex gap-2 rounded-lg border border-[#D9E3FF] bg-[#F4F7FF] px-3 py-2.5 text-xs leading-5 text-[#53627A] sm:mx-5">
        <Info aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-[#3156C8]" />
        <p>{guidance}</p>
      </div>
    </ConsoleCard>
  );
}
