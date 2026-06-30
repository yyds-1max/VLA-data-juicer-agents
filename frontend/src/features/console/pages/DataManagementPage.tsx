import {
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Database,
  Files,
  Filter,
  Images,
  Layers3,
  Route,
  Search,
  X,
  type LucideIcon,
} from "lucide-react";
import { type MouseEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import { getSyncImages, getSyncImageUrl } from "../../../api/client";
import type {
  NavigationClipSummary,
  NavigationDatasetStatus,
  NavigationDateSummary,
  NavigationSyncImageListing,
} from "../../../api/types";
import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { StatusTag } from "../../../components/console/StatusTag";
import type { StatusTone } from "../consoleTypes";
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
type SceneFilter = "all" | "outdoor" | "indoor";
type StatusFilter = "all" | NavigationDatasetStatus;

const dataSurfaces = [
  { id: "navigation", label: "导航数据", icon: Route },
  { id: "robotic_arm", label: "机械臂数据", icon: Bot },
] satisfies Array<{ id: DataSurface; label: string; icon: LucideIcon }>;

const sceneOptions = [
  { value: "all", label: "全部场景" },
  { value: "outdoor", label: "室外导航" },
  { value: "indoor", label: "室内导航" },
] satisfies Array<{ value: SceneFilter; label: string }>;

const statusOptions = [
  { value: "all", label: "全部状态" },
  { value: "raw_only", label: "待处理" },
  { value: "extracted", label: "已拆解" },
  { value: "synced", label: "已同步" },
  { value: "error", label: "异常" },
] satisfies Array<{ value: StatusFilter; label: string }>;

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

function MetricCard({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <ConsoleCard className="flex items-center gap-3">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-console-line bg-console-panel2">
        <Icon aria-hidden="true" className="h-5 w-5 text-console-cyan" />
      </div>
      <div className="min-w-0">
        <p className="text-xs text-console-muted">{label}</p>
        <p className="truncate text-lg font-semibold text-console-text">{value}</p>
      </div>
    </ConsoleCard>
  );
}

function ProcessOverview() {
  const steps = [
    { name: "raw_data", state: "已采集" },
    { name: "tmp_dir", state: "已拆解" },
    { name: "sync_data", state: "已同步" },
  ];

  return (
    <ConsoleCard>
      <div className="mb-4">
        <h2 className="text-base font-semibold text-console-text">流程概览</h2>
        <p className="mt-1 text-sm text-console-muted">raw_data 已采集 {"->"} tmp_dir 已拆解 {"->"} sync_data 已同步</p>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        {steps.map((step, index) => (
          <div key={step.name} className="flex items-center gap-3 rounded-lg border border-console-line bg-console-panel2/70 p-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-console-line bg-console-panel text-sm font-semibold text-console-muted">
              {index + 1}
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-console-text">{step.name}</p>
              <p className="text-xs text-console-muted">{step.state}</p>
            </div>
          </div>
        ))}
      </div>
    </ConsoleCard>
  );
}

function StatusCell({ status }: { status: NavigationDatasetStatus }) {
  return <StatusTag tone={statusTones[status]}>{statusLabels[status]}</StatusTag>;
}

function ClipList({
  clips,
  highlightedClip,
  onViewSyncImages,
}: {
  clips: NavigationClipSummary[];
  highlightedClip: string | null;
  onViewSyncImages: (clip: NavigationClipSummary, opener: HTMLElement) => void;
}) {
  if (clips.length === 0) {
    return <div className="rounded-lg border border-console-line bg-console-panel px-4 py-5 text-sm text-console-muted">该日期暂无 clip 明细。</div>;
  }

  return (
    <div className="grid gap-3">
      {clips.map((clip) => {
        const highlightedQuery = highlightedClip?.toLowerCase() ?? "";
        const isHighlighted = highlightedQuery.length > 0 && clip.clip.toLowerCase().includes(highlightedQuery);

        return (
          <div
            key={`${clip.date}-${clip.clip}`}
            className={`rounded-lg border bg-console-panel p-3 transition ${
              isHighlighted ? "border-console-cyan shadow-[0_0_0_2px_rgba(45,108,223,0.12)]" : "border-console-line"
            }`}
          >
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold text-console-text">{clip.clip}</h3>
                  <StatusCell status={clip.status} />
                  {isHighlighted ? <StatusTag tone="info">匹配</StatusTag> : null}
                </div>
                <p className="max-w-3xl truncate text-xs text-console-muted" title={formatTopics(clip.topics)}>
                  {formatTopics(clip.topics)}
                </p>
              </div>
              <ConsoleButton
                className="h-8 w-fit px-2 text-xs"
                disabled={clip.sync_frame_counts.image === 0}
                aria-label={`查看 ${clip.clip} 同步图像`}
                onClick={(event: MouseEvent<HTMLButtonElement>) => onViewSyncImages(clip, event.currentTarget)}
              >
                查看同步图像
              </ConsoleButton>
            </div>
            <div className="mt-3 grid gap-2 text-xs text-console-muted sm:grid-cols-2 lg:grid-cols-6">
              <span>时长 {formatDuration(clip.duration_ns)}</span>
              <span>raw {formatCount(clip.raw_message_count)}</span>
              <span>tmp_dir {clip.has_tmp_dir ? "已存在" : "缺失"}</span>
              <span>sync_data {clip.has_sync_data ? "已存在" : "缺失"}</span>
              <span>图像 {formatCount(clip.sync_frame_counts.image)}</span>
              <span>点云 {formatCount(clip.sync_frame_counts.pointcloud)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function DatasetList({
  dates,
  expandedDate,
  highlightedClip,
  onToggleDate,
  onViewSyncImages,
}: {
  dates: NavigationDateSummary[];
  expandedDate: string | null;
  highlightedClip: string | null;
  onToggleDate: (date: string) => void;
  onViewSyncImages: (clip: NavigationClipSummary, opener: HTMLElement) => void;
}) {
  return (
    <ConsoleCard>
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-console-text">导航数据集</h2>
          <p className="mt-1 text-sm text-console-muted">按日期查看 raw_data、tmp_dir 与 sync_data 处理状态</p>
        </div>
        <StatusTag tone="info">{formatCount(dates.length)} 个日期</StatusTag>
      </div>
      <div className="space-y-3">
        {dates.map((date) => {
          const isExpanded = expandedDate === date.date;
          const ExpandIcon = isExpanded ? ChevronDown : ChevronRight;

          return (
            <div key={date.date} className="rounded-lg border border-console-line bg-console-panel2/50 p-4">
              <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
                <div className="flex flex-wrap items-center gap-3">
                  <h3 className="text-lg font-semibold text-console-text">{date.date}</h3>
                  <StatusCell status={date.status} />
                </div>
                <div className="grid flex-1 gap-2 text-xs text-console-muted sm:grid-cols-3 xl:max-w-3xl xl:grid-cols-6">
                  <span>clip {formatCount(date.clip_count)}</span>
                  <span>总时长 {formatDuration(date.total_duration_ns)}</span>
                  <span>raw {formatCount(date.raw_message_count)}</span>
                  <span>已拆解 {formatCount(date.extracted_clip_count)}</span>
                  <span>已同步 {formatCount(date.synced_clip_count)}</span>
                  <span>图像 {formatCount(date.sync_frame_counts.image)}</span>
                </div>
                <ConsoleButton className="h-8 w-fit px-2 text-xs" aria-label={`${isExpanded ? "收起" : "展开"} ${date.date}`} onClick={() => onToggleDate(date.date)}>
                  <ExpandIcon aria-hidden="true" className="h-4 w-4" />
                  {isExpanded ? "收起" : "展开"}
                </ConsoleButton>
              </div>
              {isExpanded ? (
                <div className="mt-4 border-t border-console-line pt-4">
                  <ClipList clips={date.clips ?? []} highlightedClip={highlightedClip} onViewSyncImages={onViewSyncImages} />
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </ConsoleCard>
  );
}

function SelectMenu<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: Array<{ value: T; label: string }>;
  value: T;
  onChange: (value: T) => void;
}) {
  const [open, setOpen] = useState(false);
  const activeLabel = options.find((option) => option.value === value)?.label ?? label;

  return (
    <div className="relative">
      <ConsoleButton aria-expanded={open} aria-haspopup="listbox" onClick={() => setOpen((current) => !current)}>
        <Filter aria-hidden="true" className="h-4 w-4" />
        {activeLabel}
        <ChevronDown aria-hidden="true" className="h-4 w-4" />
      </ConsoleButton>
      {open ? (
        <div className="absolute left-0 top-11 z-20 min-w-36 rounded-lg border border-console-line bg-console-panel p-1 shadow-lg" role="listbox" aria-label={label}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              className={`block w-full rounded-md px-3 py-2 text-left text-sm transition ${
                option.value === value ? "bg-console-panel2 font-semibold text-console-text" : "text-console-muted hover:bg-console-panel2 hover:text-console-text"
              }`}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function DataSurfaceTabs({
  activeSurface,
  onChange,
}: {
  activeSurface: DataSurface;
  onChange: (surface: DataSurface) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2" role="tablist" aria-label="数据类型">
      {dataSurfaces.map((surface) => {
        const Icon = surface.icon;
        const active = activeSurface === surface.id;

        return (
          <button
            key={surface.id}
            type="button"
            role="tab"
            aria-selected={active}
            className={`inline-flex h-10 items-center gap-2 rounded-lg border px-4 text-sm font-semibold transition focus:outline-none focus:ring-2 focus:ring-console-cyan ${
              active
                ? "border-console-cyan bg-blue-50 text-console-cyan shadow-sm"
                : "border-console-line bg-console-panel text-console-muted hover:border-console-cyan/40 hover:bg-console-panel2 hover:text-console-text"
            }`}
            onClick={() => onChange(surface.id)}
          >
            <Icon aria-hidden="true" className="h-4 w-4" />
            {surface.label}
          </button>
        );
      })}
    </div>
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
    <div className="absolute left-0 right-0 top-11 z-20 rounded-lg border border-console-line bg-console-panel p-1 shadow-lg" role="listbox" aria-label="搜索建议">
      {suggestions.map((suggestion) => (
        <button
          key={`${suggestion.type}-${suggestion.label}`}
          type="button"
          role="option"
          className="block w-full rounded-md px-3 py-2 text-left text-sm text-console-muted transition hover:bg-console-panel2 hover:text-console-text"
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
            className="inline-flex h-8 items-center justify-center gap-2 rounded-lg border border-console-line bg-console-panel px-2 text-sm font-medium text-console-text shadow-sm transition hover:border-console-cyan/40 hover:bg-console-panel2 focus:outline-none focus:ring-2 focus:ring-console-cyan"
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
                <div className="flex flex-wrap gap-2" role="tablist" aria-label="同步图像序列">
                  {sequences.map((sequence) => {
                    const isActive = sequence.sequence === currentSequence?.sequence;
                    return (
                      <button
                        aria-selected={isActive}
                        className={`rounded-lg border px-3 py-1.5 text-sm transition focus:outline-none focus:ring-2 focus:ring-console-cyan ${
                          isActive
                            ? "border-console-cyan bg-console-cyan/10 text-console-text"
                            : "border-console-line bg-console-panel2 text-console-muted hover:text-console-text"
                        }`}
                        key={sequence.sequence}
                        onClick={() => handleSelectSequence(sequence.sequence)}
                        role="tab"
                        type="button"
                      >
                        {sequence.sequence}
                      </button>
                    );
                  })}
                </div>
              ) : null}

              <div className="grid min-h-0 gap-4 lg:grid-cols-[16rem_1fr]">
                <div className="rounded-lg border border-console-line bg-console-panel2/60 p-3">
                  <div className="mb-2 text-xs font-medium text-console-muted">图像文件</div>
                  <div className="max-h-[34rem] space-y-1 overflow-y-auto">
                    {images.map((image, index) => {
                      const isActive = index === selectedImageIndex;
                      return (
                        <button
                          aria-pressed={isActive}
                          className={`block w-full truncate rounded-md border px-2 py-1.5 text-left text-sm transition focus:outline-none focus:ring-2 focus:ring-console-cyan ${
                            isActive
                              ? "border-console-cyan bg-white text-console-text shadow-sm"
                              : "border-transparent bg-transparent text-console-muted hover:bg-white hover:text-console-text"
                          }`}
                          key={image}
                          onClick={() => setSelectedImageIndex(index)}
                          title={image}
                          type="button"
                        >
                          {image}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div className="min-w-0 rounded-lg border border-console-line bg-white p-4 shadow-sm">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-console-text">{selectedImage}</p>
                      <p className="text-xs text-console-muted">
                        {selectedImageIndex + 1} / {totalImages}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      {selectedImageIndex > 0 ? (
                        <ConsoleButton aria-label="上一张" className="h-8 px-2" onClick={() => setSelectedImageIndex((index) => Math.max(index - 1, 0))}>
                          <ChevronLeft aria-hidden="true" className="h-4 w-4" />
                          上一张
                        </ConsoleButton>
                      ) : null}
                      {selectedImageIndex < totalImages - 1 ? (
                        <ConsoleButton
                          aria-label="下一张"
                          className="h-8 px-2"
                          onClick={() => setSelectedImageIndex((index) => Math.min(index + 1, totalImages - 1))}
                        >
                          下一张
                          <ChevronRight aria-hidden="true" className="h-4 w-4" />
                        </ConsoleButton>
                      ) : null}
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
  const [activeSurface, setActiveSurface] = useState<DataSurface>("navigation");
  const [sceneFilter, setSceneFilter] = useState<SceneFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [showSearchSuggestions, setShowSearchSuggestions] = useState(false);
  const [highlightedClip, setHighlightedClip] = useState<{ date: string; clip: string } | null>(null);
  const [expandedDate, setExpandedDate] = useState<string | null>(null);
  const [syncImageClip, setSyncImageClip] = useState<NavigationClipSummary | null>(null);
  const { summary: datasetSummary, loading, error } = useNavigationDatasetSummary();
  const syncImageOpenerRef = useRef<HTMLElement | null>(null);

  const totals = datasetSummary?.totals;
  const dates = datasetSummary?.dates ?? [];
  const visibleDates = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const matchingClipDate =
      highlightedClip ??
      (query.length > 8
        ? dates
            .flatMap((date) => (date.clips ?? []).map((clip) => ({ date: date.date, clip: clip.clip })))
            .find((match) => match.clip.toLowerCase().includes(query)) ?? null
        : null);

    return dates.filter((date) => {
      if (statusFilter !== "all" && date.status !== statusFilter) {
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
  }, [dates, highlightedClip, searchQuery, statusFilter]);
  const effectiveExpandedDate = highlightedClip?.date ?? expandedDate;

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

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <ConsoleCard className="space-y-4">
        <DataSurfaceTabs activeSurface={activeSurface} onChange={setActiveSurface} />
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="flex flex-wrap gap-2">
            <SelectMenu label="全部场景" options={sceneOptions} value={sceneFilter} onChange={setSceneFilter} />
            <SelectMenu label="全部状态" options={statusOptions} value={statusFilter} onChange={setStatusFilter} />
          </div>
          <div className="relative min-w-0 flex-1">
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" />
            <input
              className="h-10 w-full rounded-lg border border-console-line bg-console-panel px-9 text-sm text-console-text shadow-sm outline-none transition placeholder:text-console-muted focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/20"
              placeholder="按日期或 clip 搜索"
              value={searchQuery}
              onChange={(event) => {
                setSearchQuery(event.target.value);
                setShowSearchSuggestions(true);
                setHighlightedClip(null);
              }}
              onFocus={() => {
                if (searchQuery.trim()) {
                  setShowSearchSuggestions(true);
                }
              }}
            />
            {searchQuery ? (
              <button
                type="button"
                aria-label="清空搜索"
                className="absolute right-2 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-console-muted hover:bg-console-panel2 hover:text-console-text"
                onClick={() => {
                  setSearchQuery("");
                  setShowSearchSuggestions(false);
                  setHighlightedClip(null);
                }}
              >
                <X aria-hidden="true" className="h-4 w-4" />
              </button>
            ) : null}
            <SearchSuggestions
              query={searchQuery}
              dates={dates}
              visible={showSearchSuggestions}
              onSelectDate={handleSelectSearchDate}
              onSelectClip={handleSelectSearchClip}
            />
          </div>
        </div>
      </ConsoleCard>

      {activeSurface === "navigation" ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
            <MetricCard icon={Database} label="日期批次" value={formatCount(totals?.date_count ?? 0)} />
            <MetricCard icon={Files} label="原始 clip" value={formatCount(totals?.clip_count ?? 0)} />
            <MetricCard icon={Clock3} label="总采集时长" value={formatDuration(totals?.total_duration_ns ?? 0)} />
            <MetricCard icon={CheckCircle2} label="已同步 clip" value={formatCount(totals?.synced_clip_count ?? 0)} />
            <MetricCard icon={Images} label="同步图像帧" value={formatCount(datasetSummary?.sync_distribution.image ?? 0)} />
          </div>

          <ProcessOverview />

          {loading ? (
            <ConsoleCard className="py-8 text-center text-sm text-console-muted">正在加载导航数据集...</ConsoleCard>
          ) : error ? (
            <ConsoleCard className="border-rose-200 bg-rose-50/60 py-8 text-center text-sm text-rose-700">{error}</ConsoleCard>
          ) : visibleDates.length === 0 ? (
            <ConsoleCard className="py-8 text-center text-sm text-console-muted">
              <Layers3 aria-hidden="true" className="mx-auto mb-3 h-6 w-6 text-console-muted" />
              暂无匹配的导航数据集。
            </ConsoleCard>
          ) : (
            <DatasetList
              dates={visibleDates}
              expandedDate={effectiveExpandedDate}
              highlightedClip={highlightedClip?.clip ?? (searchQuery.trim().length > 8 ? searchQuery.trim() : null)}
              onToggleDate={handleToggleDate}
              onViewSyncImages={handleViewSyncImages}
            />
          )}
        </>
      ) : (
        <RoboticArmPlaceholder />
      )}

      <SyncImageDrawer clip={syncImageClip} onClose={handleCloseSyncImages} />
    </section>
  );
}
