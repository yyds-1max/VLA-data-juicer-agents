import {
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Database,
  Files,
  Images,
  Layers3,
  RefreshCw,
  Search,
  X,
  type LucideIcon,
} from "lucide-react";
import { Fragment, type MouseEvent, type KeyboardEvent, type PointerEvent as ReactPointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "zustand";

import { getSyncImages, getSyncImageUrl } from "../../../api/client";
import type {
  AnnotationLifecycleProjection,
  AnnotationLifecycleStatus,
  NavigationClipSummary,
  NavigationDatasetStatus,
  NavigationDateSummary,
  NavigationSyncImageListing,
} from "../../../api/types";
import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { ConsoleSlidingTabs } from "../../../components/console/ConsoleSlidingTabs";
import { StatusTag } from "../../../components/console/StatusTag";
import { Input } from "../../../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select";
import { datapilotStore } from "../../../store/datapilotStore";
import type { StatusTone } from "../consoleTypes";
import { NavigationDataPilotDialog } from "../components/NavigationDataPilotDialog";
import {
  buildNavigationDatasetRequestContext,
  buildNavigationDatasetRequest,
  type NavigationDatasetSelection,
} from "../navigationDataPilotRequest";
import { useNavigationDatasetSummary } from "../navigationDatasetSummaryCache";

type DataManagementPageProps = {
  onPlaceholderAction?: (message?: string) => void;
};

const statusLabels: Record<NavigationDatasetStatus, string> = {
  raw_only: "待处理",
  extracted: "已拆解",
  synced: "已同步",
  error: "异常",
};

const statusTones: Record<NavigationDatasetStatus, StatusTone> = {
  raw_only: "neutral",
  extracted: "warning",
  synced: "success",
  error: "danger",
};

type DataSurface = "navigation" | "robotic_arm";
type StatusFilter = "all" | NavigationDatasetStatus;
type AnnotationStatusFilter = "all" | AnnotationLifecycleStatus;

const dataSurfaces = [
  { value: "navigation", label: "导航数据" },
  { value: "robotic_arm", label: "机械臂数据" },
] satisfies Array<{ value: DataSurface; label: string }>;

const statusOptions = [
  { value: "all", label: "全部状态" },
  { value: "raw_only", label: "待处理" },
  { value: "extracted", label: "已拆解" },
  { value: "synced", label: "已同步" },
  { value: "error", label: "异常" },
] satisfies Array<{ value: StatusFilter; label: string }>;

const annotationStatusLabels: Record<AnnotationLifecycleStatus, string> = {
  not_started: "尚未标注",
  processing: "处理中",
  waiting_initial_annotation: "待首帧标注",
  annotated_pending_review: "已标注 / 待复核",
  verified: "已验证",
  returned: "已退回",
  discarded: "已废弃",
  failed: "处理失败",
  partial: "部分完成",
};

const annotationStatusTones: Record<AnnotationLifecycleStatus, StatusTone> = {
  not_started: "neutral",
  processing: "info",
  waiting_initial_annotation: "warning",
  annotated_pending_review: "warning",
  verified: "success",
  returned: "warning",
  discarded: "neutral",
  failed: "danger",
  partial: "info",
};

const annotationStatusOptions = [
  { value: "all", label: "全部标注状态" },
  ...Object.entries(annotationStatusLabels).map(([value, label]) => ({
    value: value as AnnotationLifecycleStatus,
    label,
  })),
] satisfies Array<{ value: AnnotationStatusFilter; label: string }>;

function formatCount(value: number) {
  return value.toLocaleString();
}

function formatDuration(durationNs: number) {
  const seconds = durationNs / 1_000_000_000;

  if (seconds < 60) {
    return `${seconds.toLocaleString(undefined, { maximumFractionDigits: 1 })} 秒`;
  }

  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);

  return `${minutes} 分 ${remainingSeconds} 秒`;
}

function formatTopics(topics: NavigationClipSummary["topics"]) {
  if (topics.length === 0) {
    return "无 topic";
  }

  return topics
    .slice(0, 2)
    .map((topic) => `${topic.name} (${formatCount(topic.message_count)})`)
    .join(" / ");
}

function SummaryStat({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2 py-1 pr-4 text-sm sm:border-r sm:border-console-line/70 sm:last:border-r-0">
      <Icon aria-hidden="true" className="h-4 w-4 shrink-0 text-console-cyan" />
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="shrink-0 text-xs text-console-muted">{label}</span>
        <span className="truncate text-sm font-semibold text-console-text">{value}</span>
      </div>
    </div>
  );
}

function ProcessOverview() {
  const steps = [
    { name: "raw_data", state: "已采集", icon: Database },
    { name: "tmp_dir", state: "已拆解", icon: Files },
    { name: "sync_data", state: "已同步", icon: CheckCircle2 },
  ];

  return (
    <section
      className="border-b border-console-line bg-console-panel px-3 py-4 sm:px-4"
      data-testid="navigation-process-overview"
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <h2 className="shrink-0 text-sm font-semibold text-console-text">处理流程</h2>
        <ol
          aria-label="导航数据处理流程"
          className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-2"
          data-testid="navigation-process-stepper"
        >
          {steps.map((step, index) => {
            const Icon = step.icon;

            return (
              <Fragment key={step.name}>
                <li
                  className="flex min-w-0 items-center gap-2 text-sm"
                  data-testid="navigation-process-step"
                >
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-console-cyan/25 bg-blue-50/70 text-console-cyan">
                    <Icon aria-hidden="true" className="h-4 w-4" />
                  </span>
                  <span className="font-medium text-console-text">{step.name}</span>
                  <span className="text-console-muted">{step.state}</span>
                </li>
                {index < steps.length - 1 ? (
                  <ChevronRight aria-hidden="true" className="h-4 w-4 shrink-0 text-console-muted/70" />
                ) : null}
              </Fragment>
            );
          })}
        </ol>
      </div>
    </section>
  );
}

function StatusCell({ status }: { status: NavigationDatasetStatus }) {
  return <StatusTag tone={statusTones[status]}>{statusLabels[status]}</StatusTag>;
}

function AnnotationStatusCell({
  projection,
}: {
  projection: AnnotationLifecycleProjection | null | undefined;
}) {
  const status = projection?.status ?? "not_started";
  const total = projection?.counts.total ?? 0;
  const annotated = projection?.annotated_unit_count ?? 0;
  return (
    <div className="flex flex-col items-start gap-1">
      <StatusTag tone={annotationStatusTones[status]}>
        {annotationStatusLabels[status]}
      </StatusTag>
      {total > 0 ? (
        <span className="text-[11px] tabular-nums text-slate-400">
          已标注 {annotated}/{total}
        </span>
      ) : null}
    </div>
  );
}

function annotationDeepLink(
  projection: AnnotationLifecycleProjection | null | undefined,
): { href: string; label: string } | null {
  if (!projection) return null;
  if (projection.status === "verified" && projection.verified_review_ref) {
    return {
      href: `/annotation/reviews/${encodeURIComponent(projection.verified_review_ref)}`,
      label: "查看已验证版本",
    };
  }
  if (projection.historical_asset_ref) {
    return {
      href: `/annotation/verified/${encodeURIComponent(projection.historical_asset_ref)}`,
      label: "查看已验证版本",
    };
  }
  if (projection.review_ref) {
    return {
      href: `/annotation/reviews/${encodeURIComponent(projection.review_ref)}`,
      label: projection.status === "annotated_pending_review" ? "进入人工复核" : "查看复核",
    };
  }
  if (projection.job_ref) {
    return {
      href: `/annotation/jobs/${encodeURIComponent(projection.job_ref)}`,
      label: projection.status === "waiting_initial_annotation" ? "进入标注" : "查看任务",
    };
  }
  return null;
}

function getScrollbarProximity(element: HTMLElement, clientX: number, clientY: number) {
  const rect = element.getBoundingClientRect();
  const proximityPx = 28;
  const hasVerticalScrollbar = element.scrollHeight > element.clientHeight;
  const hasHorizontalScrollbar = element.scrollWidth > element.clientWidth;
  const isNearVertical = hasVerticalScrollbar && clientX >= rect.right - proximityPx;
  const isNearHorizontal = hasHorizontalScrollbar && clientY >= rect.bottom - proximityPx;

  return { horizontal: isNearHorizontal, vertical: isNearVertical };
}

function useScrollbarProximity() {
  const [scrollbarProximity, setScrollbarProximity] = useState({ horizontal: false, vertical: false });
  const [activeScrollbarProximity, setActiveScrollbarProximity] = useState({ horizontal: false, vertical: false });
  const isScrollbarActive = activeScrollbarProximity.horizontal || activeScrollbarProximity.vertical;

  useEffect(() => {
    if (!isScrollbarActive) {
      return;
    }

    function handlePointerUp() {
      setActiveScrollbarProximity({ horizontal: false, vertical: false });
      setScrollbarProximity({ horizontal: false, vertical: false });
    }

    document.addEventListener("pointerup", handlePointerUp);

    return () => {
      document.removeEventListener("pointerup", handlePointerUp);
    };
  }, [isScrollbarActive]);

  function handlePointerMove(event: ReactPointerEvent<HTMLElement>) {
    setScrollbarProximity(getScrollbarProximity(event.currentTarget, event.clientX, event.clientY));
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLElement>) {
    const nextProximity = getScrollbarProximity(event.currentTarget, event.clientX, event.clientY);
    setScrollbarProximity(nextProximity);
    setActiveScrollbarProximity(nextProximity);
  }

  function handlePointerLeave() {
    if (!isScrollbarActive) {
      setScrollbarProximity({ horizontal: false, vertical: false });
    }
  }

  return {
    isHorizontalScrollbarNear: scrollbarProximity.horizontal || activeScrollbarProximity.horizontal,
    isVerticalScrollbarNear: scrollbarProximity.vertical || activeScrollbarProximity.vertical,
    onPointerDown: handlePointerDown,
    onPointerLeave: handlePointerLeave,
    onPointerMove: handlePointerMove,
  };
}

function ClipRows({
  clips,
  highlightedClip,
  onOpenAnnotation,
  onViewSyncImages,
}: {
  clips: NavigationClipSummary[];
  highlightedClip: string | null;
  onOpenAnnotation: (href: string) => void;
  onViewSyncImages: (clip: NavigationClipSummary, opener: HTMLElement) => void;
}) {
  const clipScrollbar = useScrollbarProximity();

  return (
    <tr className="border-b border-slate-200 bg-slate-50/55">
      <td colSpan={10} className="px-0 py-0">
        {clips.length === 0 ? (
          <div className="border-l-2 border-blue-200 px-16 py-6 text-sm text-slate-500">该日期暂无 clip 明细。</div>
        ) : (
          <div
            className={`console-soft-scrollbar max-h-80 overflow-auto border-l-2 border-blue-200 ${
              clipScrollbar.isVerticalScrollbarNear ? "is-scrollbar-vertical-near" : ""
            } ${clipScrollbar.isHorizontalScrollbarNear ? "is-scrollbar-horizontal-near" : ""}`}
            data-testid="navigation-clip-scroll"
            onPointerDown={clipScrollbar.onPointerDown}
            onPointerLeave={clipScrollbar.onPointerLeave}
            onPointerMove={clipScrollbar.onPointerMove}
          >
            <table className="w-full min-w-[1240px] text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr className="h-10 border-b border-slate-200/90 bg-slate-50/80">
                  <th className="pl-16 pr-3 font-medium">clip 名称</th>
                  <th className="pr-3 font-medium">时长</th>
                  <th className="pr-3 font-medium">topic 摘要</th>
                  <th className="pr-3 font-medium">raw 消息</th>
                  <th className="pr-3 font-medium">tmp_dir</th>
                  <th className="pr-3 font-medium">sync_data</th>
                  <th className="pr-3 font-medium">同步图像帧</th>
                  <th className="pr-3 font-medium">数据状态</th>
                  <th className="pr-3 font-medium">标注状态</th>
                  <th className="pr-4 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {clips.map((clip) => {
                  const highlightedQuery = highlightedClip?.toLowerCase() ?? "";
                  const isHighlighted = highlightedQuery.length > 0 && clip.clip.toLowerCase().includes(highlightedQuery);

                  return (
                    <tr
                      key={`${clip.date}-${clip.clip}`}
                      className={`h-14 border-b border-slate-200/80 bg-white/70 transition-[background-color,box-shadow] duration-150 last:border-b-0 hover:bg-blue-50/45 motion-reduce:transition-none ${
                        isHighlighted ? "bg-console-cyan/10 bg-blue-50/80 ring-1 ring-inset ring-blue-300/60" : ""
                      }`}
                    >
                      <td className="pl-16 pr-3 font-medium text-slate-800">
                        {clip.clip}
                      </td>
                      <td className="pr-3 text-slate-500">{formatDuration(clip.duration_ns)}</td>
                      <td className="max-w-[18rem] truncate pr-3 text-slate-500" title={formatTopics(clip.topics)}>
                        {formatTopics(clip.topics)}
                      </td>
                      <td className="pr-3 text-slate-500">{formatCount(clip.raw_message_count)}</td>
                      <td className="pr-3 text-slate-500">{clip.has_tmp_dir ? "已存在" : "缺失"}</td>
                      <td className="pr-3 text-slate-500">{clip.has_sync_data ? "已存在" : "缺失"}</td>
                      <td className="pr-3 text-slate-500">{formatCount(clip.sync_frame_counts.image)}</td>
                      <td className="pr-3">
                        <StatusCell status={clip.status} />
                      </td>
                      <td className="pr-3">
                        <AnnotationStatusCell projection={clip.annotation} />
                      </td>
                      <td className="pr-4 text-right">
                        <div className="flex justify-end gap-1">
                          <button
                            className="inline-flex h-8 items-center rounded-md px-2 text-xs font-medium text-blue-600 transition-[color,background-color] duration-150 hover:bg-blue-50 hover:text-blue-700 active:bg-blue-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-45 motion-reduce:transition-none"
                            disabled={clip.sync_frame_counts.image === 0}
                            aria-label={`查看 ${clip.clip} 同步图像`}
                            onClick={(event: MouseEvent<HTMLButtonElement>) => onViewSyncImages(clip, event.currentTarget)}
                            type="button"
                          >
                            同步图像
                          </button>
                          {annotationDeepLink(clip.annotation) ? (
                            <button
                              className="inline-flex h-8 items-center rounded-md px-2 text-xs font-medium text-blue-600 transition-colors hover:bg-blue-50 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
                              onClick={() => {
                                const link = annotationDeepLink(clip.annotation);
                                if (link) onOpenAnnotation(link.href);
                              }}
                              type="button"
                            >
                              {annotationDeepLink(clip.annotation)?.label}
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </td>
    </tr>
  );
}

function DatasetTable({
  dates,
  expandedDate,
  highlightedClip,
  onOpenAnnotation,
  onToggleDate,
  onViewSyncImages,
}: {
  dates: NavigationDateSummary[];
  expandedDate: string | null;
  highlightedClip: string | null;
  onOpenAnnotation: (href: string) => void;
  onToggleDate: (date: string) => void;
  onViewSyncImages: (clip: NavigationClipSummary, opener: HTMLElement) => void;
}) {
  const datasetScrollbar = useScrollbarProximity();

  return (
    <div>
      <div className="flex flex-col items-start justify-between gap-1 border-b border-slate-200 px-4 py-3 sm:flex-row sm:items-center sm:px-5">
        <p className="text-sm text-console-muted">
          共 <span className="font-semibold text-console-text">{formatCount(dates.length)}</span> 个日期批次
        </p>
        <p className="text-xs text-slate-400">展开日期可查看对应 clip 明细</p>
      </div>
      <div
        className={`console-soft-scrollbar max-h-[60vh] overflow-auto ${
          datasetScrollbar.isVerticalScrollbarNear ? "is-scrollbar-vertical-near" : ""
        } ${datasetScrollbar.isHorizontalScrollbarNear ? "is-scrollbar-horizontal-near" : ""}`}
        data-testid="navigation-dataset-scroll"
        onPointerDown={datasetScrollbar.onPointerDown}
        onPointerLeave={datasetScrollbar.onPointerLeave}
        onPointerMove={datasetScrollbar.onPointerMove}
      >
        <table className="w-full min-w-[1220px] text-left text-sm">
          <thead className="text-xs text-slate-500">
            <tr className="h-11 border-b border-slate-200 bg-white">
              <th className="pl-4 pr-3 font-medium sm:pl-5">日期</th>
              <th className="pr-3 font-medium">clip 数</th>
              <th className="pr-3 font-medium">总时长</th>
              <th className="pr-3 font-medium">raw 消息</th>
              <th className="pr-3 font-medium">已拆解 clip</th>
              <th className="pr-3 font-medium">同步 clip 数</th>
              <th className="pr-3 font-medium">同步图像帧</th>
              <th className="pr-3 font-medium">数据状态</th>
              <th className="pr-3 font-medium">标注状态</th>
              <th className="pr-5 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {dates.length === 0 ? (
              <tr>
                <td colSpan={10} className="h-[19rem] px-4 text-center text-sm text-slate-500">
                  <span className="mx-auto mb-3 flex size-11 items-center justify-center rounded-xl border border-slate-200 bg-slate-50 text-slate-400">
                    <Layers3 aria-hidden="true" className="size-5" />
                  </span>
                  <span className="block font-medium text-slate-700">暂无匹配的导航数据</span>
                  <span className="mt-1 block text-xs text-slate-400">请调整搜索词或状态筛选。</span>
                </td>
              </tr>
            ) : dates.map((date) => {
              const isExpanded = expandedDate === date.date;
              const ExpandIcon = isExpanded ? ChevronDown : ChevronRight;

              return (
                <Fragment key={date.date}>
                  <tr className={`h-[68px] border-b border-slate-100 transition-[background-color] duration-150 hover:bg-blue-50/35 motion-reduce:transition-none ${isExpanded ? "bg-blue-50/55" : ""}`}>
                    <td className="pl-3 pr-3 font-medium text-slate-800 sm:pl-4">
                      <div className="flex items-center gap-2">
                        <button
                          aria-expanded={isExpanded}
                          aria-label={`${isExpanded ? "收起" : "展开"} ${date.date}`}
                          className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-transparent text-slate-500 transition-[color,background-color,border-color] duration-150 hover:border-blue-100 hover:bg-blue-50 hover:text-blue-600 active:bg-blue-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 motion-reduce:transition-none"
                          onClick={() => onToggleDate(date.date)}
                          type="button"
                        >
                          <ExpandIcon aria-hidden="true" className="h-4 w-4" />
                        </button>
                        <span>{date.date}</span>
                      </div>
                    </td>
                    <td className="pr-3 text-slate-500">{formatCount(date.clip_count)}</td>
                    <td className="pr-3 text-slate-500">{formatDuration(date.total_duration_ns)}</td>
                    <td className="pr-3 text-slate-500">{formatCount(date.raw_message_count)}</td>
                    <td className="pr-3 text-slate-500">{formatCount(date.extracted_clip_count)}</td>
                    <td className="pr-3 text-slate-500">{formatCount(date.synced_clip_count)}</td>
                    <td className="pr-3 text-slate-500">{formatCount(date.sync_frame_counts.image)}</td>
                    <td className="pr-3">
                      <StatusCell status={date.status} />
                    </td>
                    <td className="pr-3">
                      <AnnotationStatusCell projection={date.annotation} />
                    </td>
                    <td className="pr-5 text-right">
                      {annotationDeepLink(date.annotation) ? (
                        <button
                          className="inline-flex h-8 items-center rounded-md px-2 text-xs font-medium text-blue-600 transition-colors hover:bg-blue-50 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
                          onClick={() => {
                            const link = annotationDeepLink(date.annotation);
                            if (link) onOpenAnnotation(link.href);
                          }}
                          type="button"
                        >
                          {annotationDeepLink(date.annotation)?.label}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                  {isExpanded ? <ClipRows clips={date.clips ?? []} highlightedClip={highlightedClip} onOpenAnnotation={onOpenAnnotation} onViewSyncImages={onViewSyncImages} /> : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DataSurfaceSwitch({
  activeSurface,
  onChange,
}: {
  activeSurface: DataSurface;
  onChange: (surface: DataSurface) => void;
}) {
  return (
    <ConsoleSlidingTabs
      aria-label="数据类型"
      value={activeSurface}
      items={dataSurfaces}
      listClassName="sm:min-w-60"
      onValueChange={onChange}
    />
  );
}

function SearchSuggestions({
  query,
  dates,
  visible,
  onSelectDate,
  onSelectClip,
}: {
  query: string;
  dates: NavigationDateSummary[];
  visible: boolean;
  onSelectDate: (date: string) => void;
  onSelectClip: (date: string, clip: string) => void;
}) {
  const trimmedQuery = query.trim();
  if (!visible || !trimmedQuery) {
    return null;
  }

  const suggestions =
    trimmedQuery.length <= 8
      ? dates
          .filter((date) => date.date.includes(trimmedQuery))
          .slice(0, 8)
          .map((date) => ({ type: "date" as const, date: date.date, label: date.date }))
      : dates
          .flatMap((date) =>
            (date.clips ?? [])
              .filter((clip) => clip.clip.toLowerCase().includes(trimmedQuery.toLowerCase()))
              .map((clip) => ({ type: "clip" as const, date: date.date, clip: clip.clip, label: clip.clip })),
          )
          .slice(0, 8);

  if (!suggestions.length) {
    return null;
  }

  return (
    <div className="absolute left-0 right-0 top-11 z-30 rounded-lg border border-slate-200 bg-white p-1 shadow-lg" role="listbox" aria-label="搜索建议">
      {suggestions.map((suggestion) => (
        <button
          key={`${suggestion.type}-${suggestion.label}`}
          type="button"
          role="option"
          className="block w-full rounded-md px-3 py-2 text-left text-sm text-slate-600 transition-[color,background-color] duration-150 hover:bg-blue-50/70 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-blue-500 motion-reduce:transition-none"
          onClick={() => {
            if (suggestion.type === "date") {
              onSelectDate(suggestion.date);
            } else {
              onSelectClip(suggestion.date, suggestion.clip);
            }
          }}
        >
          {suggestion.label}
        </button>
      ))}
    </div>
  );
}

function NavigationListToolbar({
  annotationStatus,
  dates,
  refreshing,
  query,
  showSuggestions,
  status,
  onChangeQuery,
  onClearQuery,
  onFocusQuery,
  onOpenDataPilot,
  onRefresh,
  onSelectClip,
  onSelectDate,
  onStatusChange,
  onAnnotationStatusChange,
}: {
  annotationStatus: AnnotationStatusFilter;
  dates: NavigationDateSummary[];
  refreshing: boolean;
  query: string;
  showSuggestions: boolean;
  status: StatusFilter;
  onChangeQuery: (query: string) => void;
  onClearQuery: () => void;
  onFocusQuery: () => void;
  onOpenDataPilot: () => void;
  onRefresh: () => void;
  onSelectClip: (date: string, clip: string) => void;
  onSelectDate: (date: string) => void;
  onStatusChange: (status: StatusFilter) => void;
  onAnnotationStatusChange: (status: AnnotationStatusFilter) => void;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-slate-200 px-4 py-4 sm:px-5 lg:flex-row lg:items-center">
      <div className="relative min-w-0 flex-1 lg:max-w-sm">
        <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 z-10 size-4 -translate-y-1/2 text-slate-400" />
        <Input
          aria-label="搜索导航数据"
          className="h-10 bg-white pl-9 pr-9 shadow-none"
          placeholder="按日期或 clip 搜索"
          value={query}
          onChange={(event) => onChangeQuery(event.target.value)}
          onFocus={onFocusQuery}
        />
        {query ? (
          <button
            type="button"
            aria-label="清空搜索"
            className="absolute right-2 top-1/2 z-10 flex size-7 -translate-y-1/2 items-center justify-center rounded-md text-slate-400 transition-[color,background-color] duration-150 hover:bg-slate-100 hover:text-slate-700 active:bg-slate-200 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-blue-500 motion-reduce:transition-none"
            onClick={onClearQuery}
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        ) : null}
        <SearchSuggestions
          query={query}
          dates={dates}
          visible={showSuggestions}
          onSelectDate={onSelectDate}
          onSelectClip={onSelectClip}
        />
      </div>

      <Select value={status} onValueChange={(value) => onStatusChange(value as StatusFilter)}>
        <SelectTrigger aria-label="导航数据状态筛选" className="h-10 w-full bg-white shadow-none lg:w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent align="start" position="popper">
          {statusOptions.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={annotationStatus}
        onValueChange={(value) => onAnnotationStatusChange(value as AnnotationStatusFilter)}
      >
        <SelectTrigger aria-label="标注状态筛选" className="h-10 w-full bg-white shadow-none lg:w-48">
          <SelectValue />
        </SelectTrigger>
        <SelectContent align="start" position="popper">
          {annotationStatusOptions.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <ConsoleButton
        aria-label={refreshing ? "正在刷新数据资产" : "刷新数据资产"}
        className="h-10"
        disabled={refreshing}
        onClick={onRefresh}
      >
        <RefreshCw
          aria-hidden="true"
          className={`size-4 ${refreshing ? "animate-spin motion-reduce:animate-none" : ""}`}
        />
        {refreshing ? "刷新中" : "刷新"}
      </ConsoleButton>

      <ConsoleButton className="h-10 lg:ml-auto" variant="primary" onClick={onOpenDataPilot}>
        <Bot aria-hidden="true" className="size-4" />
        交给 DataPilot
      </ConsoleButton>
    </div>
  );
}

function RoboticArmPlaceholder() {
  return (
    <ConsoleCard className="py-10">
      <div className="mx-auto max-w-2xl text-center">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl border border-console-line bg-console-panel2">
          <Bot aria-hidden="true" className="h-6 w-6 text-console-cyan" />
        </div>
        <h2 className="text-lg font-semibold text-console-text">机械臂数据接入中</h2>
        <p className="mt-2 text-sm leading-6 text-console-muted">
          这里会用于展示机械臂采集、拆解、同步和标注前的数据资产。当前版本先保留入口，后续接入真实机械臂数据扫描后启用。
        </p>
      </div>
    </ConsoleCard>
  );
}

function ImageStepButton({
  direction,
  disabled,
  onClick,
}: {
  direction: "previous" | "next";
  disabled: boolean;
  onClick: () => void;
}) {
  const isPrevious = direction === "previous";
  const Icon = isPrevious ? ChevronLeft : ChevronRight;
  const label = isPrevious ? "上一张" : "下一张";

  return (
    <button
      aria-label={label}
      className="inline-flex h-9 min-w-20 items-center justify-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 text-sm font-medium text-slate-700 shadow-xs transition-[color,background-color,border-color,box-shadow] duration-150 hover:border-blue-200 hover:bg-blue-50/60 hover:text-blue-700 active:border-blue-300 active:bg-blue-100 disabled:cursor-not-allowed disabled:border-slate-100 disabled:bg-slate-50 disabled:text-slate-300 disabled:shadow-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 data-[pointer-focus=true]:outline-none motion-reduce:transition-none"
      disabled={disabled}
      onBlur={(event) => {
        delete event.currentTarget.dataset.pointerFocus;
      }}
      onClick={onClick}
      onPointerDown={(event) => {
        // 鼠标点击不保留焦点环；键盘进入时仍由 focus-visible 提供可访问焦点。
        event.currentTarget.dataset.pointerFocus = "true";
      }}
      type="button"
    >
      {isPrevious ? <Icon aria-hidden="true" className="size-4" /> : null}
      {label}
      {!isPrevious ? <Icon aria-hidden="true" className="size-4" /> : null}
    </button>
  );
}

function SyncImageDrawer({
  clip,
  onClose,
}: {
  clip: NavigationClipSummary | null;
  onClose: () => void;
}) {
  const [listing, setListing] = useState<NavigationSyncImageListing | null>(null);
  const [activeSequence, setActiveSequence] = useState<string | null>(null);
  const [selectedImageIndex, setSelectedImageIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const sequenceButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const activeImageButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!clip) {
      return;
    }

    const openedClip = clip;
    let isMounted = true;
    setLoading(true);
    setError(null);
    setListing(null);
    setActiveSequence(null);
    setSelectedImageIndex(0);

    async function loadSyncImages() {
      try {
        const nextListing = await getSyncImages(openedClip.date, openedClip.clip);
        // 抽屉可能在请求完成前切换到另一个 clip；只提交当前仍有效的请求结果。
        if (isMounted) {
          setListing(nextListing);
          setActiveSequence(nextListing.sequences[0]?.sequence ?? null);
          setSelectedImageIndex(0);
        }
      } catch {
        if (isMounted) {
          setError("同步图像加载失败");
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    void loadSyncImages();

    return () => {
      isMounted = false;
    };
  }, [clip]);

  useEffect(() => {
    if (clip) {
      closeButtonRef.current?.focus();
    }
  }, [clip]);

  useEffect(() => {
    // 序列很多时保持单行横向队列，并让当前项自动进入可视区域。
    const activeButton = activeSequence ? sequenceButtonRefs.current.get(activeSequence) : null;
    if (activeButton && typeof activeButton.scrollIntoView === "function") {
      activeButton.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [activeSequence]);

  useEffect(() => {
    // 上一张/下一张切换后同步滚动左侧文件队列，长序列中也能定位当前帧。
    const activeButton = activeImageButtonRef.current;
    if (activeButton && typeof activeButton.scrollIntoView === "function") {
      activeButton.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [activeSequence, selectedImageIndex]);

  if (!clip) {
    return null;
  }

  const activeListing = listing && listing.date === clip.date && listing.clip === clip.clip ? listing : null;
  const sequences = activeListing?.sequences ?? [];
  const currentSequence = sequences.find((sequence) => sequence.sequence === activeSequence) ?? sequences[0];
  const images = currentSequence?.images ?? [];
  const selectedImage = images[selectedImageIndex] ?? null;
  const totalImages = images.length;
  const previewUrl =
    currentSequence && selectedImage ? getSyncImageUrl(clip.date, clip.clip, currentSequence.sequence, selectedImage) : null;

  function handleSelectSequence(sequence: string) {
    setActiveSequence(sequence);
    setSelectedImageIndex(0);
  }

  function handleSequenceKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex = index;
    if (event.key === "ArrowRight") {
      nextIndex = Math.min(index + 1, sequences.length - 1);
    } else if (event.key === "ArrowLeft") {
      nextIndex = Math.max(index - 1, 0);
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = sequences.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    const nextSequence = sequences[nextIndex]?.sequence;
    if (nextSequence) {
      handleSelectSequence(nextSequence);
      sequenceButtonRefs.current.get(nextSequence)?.focus();
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-slate-950/20" role="presentation">
      <aside
        aria-labelledby="sync-image-drawer-title"
        aria-modal="true"
        className="ml-auto flex h-full w-full max-w-5xl flex-col border-l border-console-line bg-console-panel shadow-2xl md:w-[76vw]"
        onKeyDown={handleKeyDown}
        role="dialog"
      >
        <div className="flex items-start justify-between gap-3 border-b border-console-line px-5 py-4">
          <div className="min-w-0">
            <h2 id="sync-image-drawer-title" className="text-base font-semibold text-console-text">
              同步图像浏览
            </h2>
            <p className="mt-1 truncate text-sm text-console-muted">
              {clip.date} / {clip.clip}
            </p>
          </div>
          <button
            aria-label="关闭同步图像浏览"
            className="inline-flex size-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 shadow-xs transition-[color,background-color,border-color] duration-150 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 active:bg-blue-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 motion-reduce:transition-none"
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {loading || (listing !== null && activeListing === null) ? (
            <div className="rounded-lg border border-console-line bg-console-panel2/70 px-4 py-5 text-sm text-console-muted">正在加载同步图像...</div>
          ) : error ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50/70 px-4 py-5 text-sm text-rose-700">{error}</div>
          ) : sequences.length === 0 || totalImages === 0 ? (
            <div className="rounded-lg border border-console-line bg-console-panel2/70 px-4 py-5 text-sm text-console-muted">暂无同步图像。</div>
          ) : (
            <div className="flex min-h-0 flex-col gap-4">
              {sequences.length > 1 ? (
                <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-3 border-b border-slate-200 pb-1">
                  <p className="whitespace-nowrap pb-2 text-xs font-medium text-slate-500">
                    同步序列
                    <span className="ml-1 font-semibold text-slate-800">
                      {Math.max(0, sequences.findIndex((sequence) => sequence.sequence === currentSequence?.sequence)) + 1}
                      <span className="mx-1 text-slate-300">/</span>
                      {sequences.length}
                    </span>
                  </p>
                  <div
                    className="console-soft-scrollbar flex min-w-0 flex-nowrap gap-2 overflow-x-auto pb-2"
                    data-testid="sync-sequence-scroll"
                    role="tablist"
                    aria-label="同步图像序列"
                  >
                    {sequences.map((sequence, index) => {
                      const isActive = sequence.sequence === currentSequence?.sequence;
                      return (
                        <button
                          aria-selected={isActive}
                          className={`max-w-64 shrink-0 truncate rounded-lg border px-3 py-1.5 text-sm transition-[color,background-color,border-color] duration-150 active:bg-blue-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 motion-reduce:transition-none ${
                            isActive
                              ? "border-blue-500 bg-blue-50/80 font-medium text-slate-800"
                              : "border-slate-200 bg-slate-50 text-slate-500 hover:border-blue-200 hover:bg-blue-50/55 hover:text-slate-800"
                          }`}
                          key={sequence.sequence}
                          onClick={() => handleSelectSequence(sequence.sequence)}
                          onKeyDown={(event) => handleSequenceKeyDown(event, index)}
                          ref={(node) => {
                            if (node) {
                              sequenceButtonRefs.current.set(sequence.sequence, node);
                            } else {
                              sequenceButtonRefs.current.delete(sequence.sequence);
                            }
                          }}
                          role="tab"
                          tabIndex={isActive ? 0 : -1}
                          title={sequence.sequence}
                          type="button"
                        >
                          {sequence.sequence}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null}

              <div className="grid min-h-0 gap-4 lg:grid-cols-[16rem_1fr]">
                <div className="rounded-lg border border-console-line bg-console-panel2/60 p-3">
                  <div className="mb-2 text-xs font-medium text-console-muted">图像文件</div>
                  <div className="max-h-136 space-y-1 overflow-y-auto">
                    {images.map((image, index) => {
                      const isActive = index === selectedImageIndex;
                      return (
                        <button
                          aria-current={isActive ? "true" : undefined}
                          aria-pressed={isActive}
                          className={`block w-full truncate rounded-md border px-2 py-1.5 text-left text-sm transition-[color,background-color,border-color,box-shadow] duration-150 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-blue-500 motion-reduce:transition-none ${
                            isActive
                              ? "border-blue-500 bg-white text-slate-800 shadow-xs"
                              : "border-transparent bg-transparent text-slate-500 hover:bg-white hover:text-slate-800"
                          }`}
                          key={image}
                          onClick={() => setSelectedImageIndex(index)}
                          ref={isActive ? activeImageButtonRef : undefined}
                          title={image}
                          type="button"
                        >
                          {image}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div className="min-w-0 rounded-lg border border-console-line bg-white p-4 shadow-xs">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-console-text">{selectedImage}</p>
                      <p className="text-xs text-console-muted">
                        {selectedImageIndex + 1} / {totalImages}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <ImageStepButton
                        direction="previous"
                        disabled={selectedImageIndex === 0}
                        onClick={() => setSelectedImageIndex((index) => Math.max(index - 1, 0))}
                      />
                      <ImageStepButton
                        direction="next"
                        disabled={selectedImageIndex >= totalImages - 1}
                        onClick={() => setSelectedImageIndex((index) => Math.min(index + 1, totalImages - 1))}
                      />
                    </div>
                  </div>
                  {previewUrl ? (
                    <img alt={selectedImage ?? "同步图像"} className="max-h-[62vh] w-full rounded-lg border border-console-line object-contain" src={previewUrl} />
                  ) : null}
                </div>
              </div>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

export function DataManagementPage({ onPlaceholderAction }: DataManagementPageProps) {
  void onPlaceholderAction;
  const navigate = useNavigate();
  const [activeSurface, setActiveSurface] = useState<DataSurface>("navigation");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [annotationStatusFilter, setAnnotationStatusFilter] = useState<AnnotationStatusFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [showSearchSuggestions, setShowSearchSuggestions] = useState(false);
  const [highlightedClip, setHighlightedClip] = useState<{ date: string; clip: string } | null>(null);
  const [expandedDate, setExpandedDate] = useState<string | null>(null);
  const [syncImageClip, setSyncImageClip] = useState<NavigationClipSummary | null>(null);
  const [dataPilotDialogOpen, setDataPilotDialogOpen] = useState(false);
  const [activeInvocationId, setActiveInvocationId] = useState<string | null>(null);
  const { summary: datasetSummary, loading, error, reload } = useNavigationDatasetSummary();
  const pendingInvocation = useStore(datapilotStore, (state) => state.pendingInvocation);
  const syncImageOpenerRef = useRef<HTMLElement | null>(null);

  const totals = datasetSummary?.totals;
  const dates = datasetSummary?.dates ?? [];
  const matchingClipDate = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();

    if (highlightedClip) {
      return highlightedClip;
    }

    if (query.length <= 8) {
      return null;
    }

    return (
      dates
        .flatMap((date) => (date.clips ?? []).map((clip) => ({ date: date.date, clip: clip.clip })))
        .find((match) => match.clip.toLowerCase().includes(query)) ?? null
    );
  }, [dates, highlightedClip, searchQuery]);
  const visibleDates = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();

    return dates.filter((date) => {
      if (statusFilter !== "all" && date.status !== statusFilter) {
        return false;
      }

      if (
        annotationStatusFilter !== "all"
        && (date.annotation?.status ?? "not_started") !== annotationStatusFilter
        && !(date.clips ?? []).some(
          (clip) => (clip.annotation?.status ?? "not_started") === annotationStatusFilter,
        )
      ) {
        return false;
      }

      if (!query) {
        return true;
      }

      if (matchingClipDate) {
        return date.date === matchingClipDate.date;
      }

      if (query.length <= 8) {
        return date.date.includes(query);
      }

      return (date.clips ?? []).some((clip) => clip.clip.toLowerCase().includes(query));
    });
  }, [annotationStatusFilter, dates, matchingClipDate, searchQuery, statusFilter]);
  const effectiveExpandedDate = matchingClipDate?.date ?? expandedDate;
  const activeInvocation =
    activeInvocationId && pendingInvocation?.invocationId === activeInvocationId
      ? pendingInvocation
      : null;
  const submittingToDataPilot =
    activeInvocation?.status === "queued" || activeInvocation?.status === "submitting";
  const dataPilotInvocationError =
    activeInvocation?.status === "failed"
      ? activeInvocation.error ?? "提交失败，请重试。"
      : null;

  useEffect(() => {
    if (!activeInvocationId || pendingInvocation?.invocationId !== activeInvocationId) {
      return;
    }
    if (pendingInvocation.status !== "submitted") {
      return;
    }
    setDataPilotDialogOpen(false);
    setActiveInvocationId(null);
    datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
  }, [activeInvocationId, pendingInvocation]);

  function handleToggleDate(date: string) {
    setExpandedDate((currentDate) => (currentDate === date ? null : date));
  }

  function handleViewSyncImages(clip: NavigationClipSummary, opener: HTMLElement) {
    syncImageOpenerRef.current = opener;
    setSyncImageClip(clip);
  }

  function handleCloseSyncImages() {
    setSyncImageClip(null);
    window.setTimeout(() => {
      syncImageOpenerRef.current?.focus();
    }, 0);
  }

  function handleSelectSearchDate(date: string) {
    setSearchQuery(date);
    setShowSearchSuggestions(false);
    setHighlightedClip(null);
    setExpandedDate(null);
  }

  function handleSelectSearchClip(date: string, clip: string) {
    setSearchQuery(clip);
    setShowSearchSuggestions(false);
    setHighlightedClip({ date, clip });
    setExpandedDate(date);
  }

  function handleOpenDataPilotDialog() {
    setDataPilotDialogOpen(true);
  }

  function handleCancelDataPilotDialog() {
    if (activeInvocationId) {
      datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
    }
    setActiveInvocationId(null);
    setDataPilotDialogOpen(false);
  }

  function handleDataPilotSelectionChange() {
    if (
      activeInvocationId &&
      pendingInvocation?.invocationId === activeInvocationId &&
      pendingInvocation.status === "failed"
    ) {
      datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
      setActiveInvocationId(null);
    }
  }

  function handleConfirmDataPilot(selection: NavigationDatasetSelection) {
    const message = buildNavigationDatasetRequest(selection);
    const requestContext = buildNavigationDatasetRequestContext(selection);
    // 同一选择提交失败时沿用原 invocation 重试，保证审计链不被重复任务拆散。
    if (
      activeInvocationId &&
      pendingInvocation?.invocationId === activeInvocationId &&
      pendingInvocation.message === message &&
      pendingInvocation.status === "failed"
    ) {
      datapilotStore.getState().retryDataPilotInvocation(activeInvocationId);
      return;
    }

    if (activeInvocationId) {
      datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
    }
    const invocationId = createInvocationId();
    if (datapilotStore.getState().launchDataPilotRequest(invocationId, message, requestContext)) {
      setActiveInvocationId(invocationId);
    }
  }

  return (
    <section className="mx-auto max-w-360 space-y-4 px-3 pb-28 pt-2 md:px-4 lg:px-5">
      <DataSurfaceSwitch activeSurface={activeSurface} onChange={setActiveSurface} />

      {activeSurface === "navigation" ? (
        <>
          <div
            className="flex flex-wrap items-center gap-x-4 gap-y-2 border-y border-console-line bg-transparent px-1 py-3 text-console-muted"
            data-testid="navigation-summary-strip"
          >
            <SummaryStat icon={Database} label="日期批次" value={formatCount(totals?.date_count ?? 0)} />
            <SummaryStat icon={Files} label="原始 clip" value={formatCount(totals?.clip_count ?? 0)} />
            <SummaryStat icon={Clock3} label="总采集时长" value={formatDuration(totals?.total_duration_ns ?? 0)} />
            <SummaryStat icon={CheckCircle2} label="已同步 clip" value={formatCount(totals?.synced_clip_count ?? 0)} />
            <SummaryStat icon={Images} label="同步图像帧" value={formatCount(datasetSummary?.sync_distribution.image ?? 0)} />
          </div>

          <ProcessOverview />

          <section className="min-h-[31rem] border-y border-slate-200 bg-white" data-testid="navigation-dataset-surface">
            <NavigationListToolbar
              annotationStatus={annotationStatusFilter}
              dates={dates}
              query={searchQuery}
              refreshing={loading}
              showSuggestions={showSearchSuggestions}
              status={statusFilter}
              onChangeQuery={(query) => {
                setSearchQuery(query);
                setShowSearchSuggestions(true);
                setHighlightedClip(null);
              }}
              onClearQuery={() => {
                setSearchQuery("");
                setShowSearchSuggestions(false);
                setHighlightedClip(null);
              }}
              onFocusQuery={() => {
                if (searchQuery.trim()) {
                  setShowSearchSuggestions(true);
                }
              }}
              onOpenDataPilot={handleOpenDataPilotDialog}
              onRefresh={() => void reload()}
              onSelectDate={handleSelectSearchDate}
              onSelectClip={handleSelectSearchClip}
              onStatusChange={(status) => {
                setStatusFilter(status);
                setExpandedDate(null);
                setHighlightedClip(null);
              }}
              onAnnotationStatusChange={(status) => {
                setAnnotationStatusFilter(status);
                setExpandedDate(null);
                setHighlightedClip(null);
              }}
            />

            {loading ? (
              <div aria-live="polite" className="space-y-px" data-testid="navigation-dataset-loading">
                <div className="h-11 animate-pulse border-b border-slate-200 bg-slate-50/70 motion-reduce:animate-none" />
                {Array.from({ length: 4 }, (_, index) => (
                  <div className="h-[68px] animate-pulse border-b border-slate-100 bg-white motion-reduce:animate-none" key={index}>
                    <div className="ml-5 mt-6 h-3 w-2/3 max-w-2xl rounded bg-slate-100" />
                  </div>
                ))}
                <span className="sr-only">正在加载导航数据集</span>
              </div>
            ) : error ? (
              <div className="flex h-[23rem] flex-col items-center justify-center px-5 text-center" role="alert">
                <span className="flex size-11 items-center justify-center rounded-xl border border-rose-200 bg-rose-50 text-rose-500">
                  <Layers3 aria-hidden="true" className="size-5" />
                </span>
                <p className="mt-3 text-sm font-medium text-slate-800">导航数据加载失败</p>
                <p className="mt-1 max-w-md text-xs text-slate-500">{error}</p>
                <ConsoleButton className="mt-4" disabled={loading} onClick={() => void reload()}>
                  重试
                </ConsoleButton>
              </div>
            ) : (
              <DatasetTable
                dates={visibleDates}
                expandedDate={effectiveExpandedDate}
                highlightedClip={highlightedClip?.clip ?? (searchQuery.trim().length > 8 ? searchQuery.trim() : null)}
                onOpenAnnotation={(href) => navigate(href)}
                onToggleDate={handleToggleDate}
                onViewSyncImages={handleViewSyncImages}
              />
            )}
          </section>
        </>
      ) : (
        <RoboticArmPlaceholder />
      )}

      <SyncImageDrawer clip={syncImageClip} onClose={handleCloseSyncImages} />
      <NavigationDataPilotDialog
        dates={dates}
        error={dataPilotInvocationError}
        open={dataPilotDialogOpen}
        submitting={submittingToDataPilot}
        onCancel={handleCancelDataPilotDialog}
        onConfirm={handleConfirmDataPilot}
        onSelectionChange={handleDataPilotSelectionChange}
      />
    </section>
  );
}

function createInvocationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `navigation-${crypto.randomUUID()}`;
  }
  return `navigation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
