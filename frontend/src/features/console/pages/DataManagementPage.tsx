import { CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, Clock3, Database, Files, Images, Layers3, X, type LucideIcon } from "lucide-react";
import { Fragment, type MouseEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

import { getNavigationDatasetSummary, getSyncImages, getSyncImageUrl } from "../../../api/client";
import type {
  NavigationClipSummary,
  NavigationDatasetStatus,
  NavigationDatasetSummary,
  NavigationDateSummary,
  NavigationSyncImageListing,
} from "../../../api/types";
import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { StatusTag } from "../../../components/console/StatusTag";
import type { StatusTone } from "../consoleTypes";

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

function ClipRows({
  clips,
  onViewSyncImages,
}: {
  clips: NavigationClipSummary[];
  onViewSyncImages: (clip: NavigationClipSummary, opener: HTMLElement) => void;
}) {
  return (
    <tr>
      <td colSpan={9} className="bg-console-panel2/50 px-4 py-4">
        {clips.length === 0 ? (
          <div className="rounded-lg border border-console-line bg-console-panel px-4 py-5 text-sm text-console-muted">该日期暂无 clip 明细。</div>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-console-line bg-console-panel">
            <table className="w-full min-w-[980px] text-left text-sm">
              <thead className="text-xs text-console-muted">
                <tr className="border-b border-console-line bg-console-panel2/70">
                  <th className="py-2 pl-3 pr-3 font-medium">clip 名称</th>
                  <th className="py-2 pr-3 font-medium">时长</th>
                  <th className="py-2 pr-3 font-medium">topic 摘要</th>
                  <th className="py-2 pr-3 font-medium">raw 消息</th>
                  <th className="py-2 pr-3 font-medium">tmp_dir</th>
                  <th className="py-2 pr-3 font-medium">sync_data</th>
                  <th className="py-2 pr-3 font-medium">同步图像帧</th>
                  <th className="py-2 pr-3 font-medium">状态</th>
                  <th className="py-2 pr-3 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {clips.map((clip) => (
                  <tr key={`${clip.date}-${clip.clip}`} className="border-b border-console-line/70 last:border-b-0">
                    <td className="py-3 pl-3 pr-3 font-medium text-console-text">{clip.clip}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatDuration(clip.duration_ns)}</td>
                    <td className="max-w-[18rem] truncate py-3 pr-3 text-console-muted" title={formatTopics(clip.topics)}>
                      {formatTopics(clip.topics)}
                    </td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(clip.raw_message_count)}</td>
                    <td className="py-3 pr-3 text-console-muted">{clip.has_tmp_dir ? "已存在" : "缺失"}</td>
                    <td className="py-3 pr-3 text-console-muted">{clip.has_sync_data ? "已存在" : "缺失"}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(clip.sync_frame_counts.image)}</td>
                    <td className="py-3 pr-3">
                      <StatusCell status={clip.status} />
                    </td>
                    <td className="py-3 pr-3">
                      <ConsoleButton
                        className="h-8 px-2 text-xs"
                        disabled={clip.sync_frame_counts.image === 0}
                        aria-label={`查看 ${clip.clip} 同步图像`}
                        onClick={(event: MouseEvent<HTMLButtonElement>) => onViewSyncImages(clip, event.currentTarget)}
                      >
                        查看同步图像
                      </ConsoleButton>
                    </td>
                  </tr>
                ))}
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
  onToggleDate,
  onViewSyncImages,
}: {
  dates: NavigationDateSummary[];
  expandedDate: string | null;
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
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1040px] text-left text-sm">
          <thead className="text-xs text-console-muted">
            <tr className="border-b border-console-line">
              <th className="py-2 pr-3 font-medium">日期</th>
              <th className="py-2 pr-3 font-medium">clip 数</th>
              <th className="py-2 pr-3 font-medium">总时长</th>
              <th className="py-2 pr-3 font-medium">raw 消息</th>
              <th className="py-2 pr-3 font-medium">已拆解 clip</th>
              <th className="py-2 pr-3 font-medium">同步 clip 数</th>
              <th className="py-2 pr-3 font-medium">同步图像帧</th>
              <th className="py-2 pr-3 font-medium">状态</th>
              <th className="py-2 pr-3 font-medium">展开</th>
            </tr>
          </thead>
          <tbody>
            {dates.map((date) => {
              const isExpanded = expandedDate === date.date;
              const ExpandIcon = isExpanded ? ChevronDown : ChevronRight;

              return (
                <Fragment key={date.date}>
                  <tr className="border-b border-console-line/70">
                    <td className="py-3 pr-3 font-medium text-console-text">{date.date}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(date.clip_count)}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatDuration(date.total_duration_ns)}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(date.raw_message_count)}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(date.extracted_clip_count)}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(date.synced_clip_count)}</td>
                    <td className="py-3 pr-3 text-console-muted">{formatCount(date.sync_frame_counts.image)}</td>
                    <td className="py-3 pr-3">
                      <StatusCell status={date.status} />
                    </td>
                    <td className="py-3 pr-3">
                      <ConsoleButton className="h-8 px-2 text-xs" aria-label={`${isExpanded ? "收起" : "展开"} ${date.date}`} onClick={() => onToggleDate(date.date)}>
                        <ExpandIcon aria-hidden="true" className="h-4 w-4" />
                        {isExpanded ? "收起" : "展开"}
                      </ConsoleButton>
                    </td>
                  </tr>
                  {isExpanded ? <ClipRows clips={date.clips ?? []} onViewSyncImages={onViewSyncImages} /> : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
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
  const [datasetSummary, setDatasetSummary] = useState<NavigationDatasetSummary | null>(null);
  const [expandedDate, setExpandedDate] = useState<string | null>(null);
  const [syncImageClip, setSyncImageClip] = useState<NavigationClipSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const syncImageOpenerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadDatasetSummary() {
      setLoading(true);
      setError(null);

      try {
        const summary = await getNavigationDatasetSummary();
        if (isMounted) {
          setDatasetSummary(summary);
        }
      } catch {
        if (isMounted) {
          setError("导航数据集摘要加载失败");
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    void loadDatasetSummary();

    return () => {
      isMounted = false;
    };
  }, []);

  const totals = datasetSummary?.totals;
  const dates = datasetSummary?.dates ?? [];

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

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
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
      ) : dates.length === 0 ? (
        <ConsoleCard className="py-8 text-center text-sm text-console-muted">
          <Layers3 aria-hidden="true" className="mx-auto mb-3 h-6 w-6 text-console-muted" />
          暂无导航数据集。
        </ConsoleCard>
      ) : (
        <DatasetTable dates={dates} expandedDate={expandedDate} onToggleDate={handleToggleDate} onViewSyncImages={handleViewSyncImages} />
      )}

      <SyncImageDrawer clip={syncImageClip} onClose={handleCloseSyncImages} />
    </section>
  );
}
