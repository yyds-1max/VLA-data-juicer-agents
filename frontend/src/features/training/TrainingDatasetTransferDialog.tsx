import { CalendarDays, Check, ChevronDown, ChevronRight, CloudDownload, Folder, FolderOpen, HardDrive, RefreshCw, RotateCcw, Search, Square, UploadCloud, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  createTrainingDatasetTransfers,
  getTrainingDirectoryListing,
  listTrainingDatasetReleases,
  requestTrainingDirectoryListing,
} from "../../api/client";
import type { TrainingDatasetRelease, TrainingDatasetTransfer, TrainingDirectoryListing } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ProgressBar } from "../../components/console/ProgressBar";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Popover, PopoverClose, PopoverContent, PopoverTrigger } from "../../components/ui/popover";
import { cn } from "../../lib/utils";

type Props = {
  open: boolean;
  nodeRef: string;
  unavailableReleaseRefs: Set<string>;
  onOpenChange: (open: boolean) => void;
  onTransfersCreated: (transfers: TrainingDatasetTransfer[]) => void;
};

type TransferMonitorProps = {
  transfers: TrainingDatasetTransfer[];
  error?: string | null;
  onPause: (transfer: TrainingDatasetTransfer) => void;
  onCancel: (transfer: TrainingDatasetTransfer) => void;
  onRetry: (transfer: TrainingDatasetTransfer) => void;
};

type SelectedDirectory = {
  path: string;
  freeBytes: number | null;
};

type DatasetReleaseMultiSelectProps = {
  loading: boolean;
  releases: TrainingDatasetRelease[];
  selectedReleaseRefs: string[];
  onChange: (releaseRefs: string[]) => void;
};

export const activeTransferStatuses = new Set<TrainingDatasetTransfer["status"]>(["preparing", "queued", "running", "pause_requested", "cancel_requested"]);
export const actionableTransferStatuses = new Set<TrainingDatasetTransfer["status"]>(["paused", "failed"]);

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

export function formatTransferBytes(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "--";
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = -1;
  do { amount /= 1024; unit += 1; } while (amount >= 1024 && unit < units.length - 1);
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

export function transferProgress(transfer: TrainingDatasetTransfer) {
  if (typeof transfer.progress_percent === "number") return Math.min(100, Math.max(0, transfer.progress_percent));
  if (transfer.total_bytes && transfer.total_bytes > 0) return Math.min(100, transfer.bytes_transferred / transfer.total_bytes * 100);
  return transfer.status === "succeeded" ? 100 : 0;
}

export function datasetTransferDestination(parentDirectory: string, release: Pick<TrainingDatasetRelease, "dataset_date" | "release_ref">) {
  const parent = parentDirectory === "/" ? "" : parentDirectory.replace(/\/+$/, "");
  const releaseSuffix = release.release_ref.split("_").at(-1)?.slice(0, 8) || release.release_ref.slice(0, 8);
  return `${parent}/datapilot-managed/${release.dataset_date}-${releaseSuffix}`;
}

export function transferLabel(status: TrainingDatasetTransfer["status"]) {
  return {
    preparing: "正在准备文件清单",
    queued: "等待传输",
    running: "正在传输",
    pause_requested: "正在暂停",
    paused: "已暂停",
    cancel_requested: "正在取消",
    succeeded: "传输完成",
    failed: "传输失败",
    cancelled: "已取消",
  }[status];
}

function transferStatusClass(status: TrainingDatasetTransfer["status"]) {
  if (status === "succeeded") return "bg-emerald-50 text-emerald-700";
  if (status === "failed") return "bg-rose-50 text-rose-700";
  if (status === "cancelled") return "bg-slate-100 text-slate-600";
  return "bg-blue-50 text-console-cyan";
}

function DatasetReleaseMultiSelect({ loading, releases, selectedReleaseRefs, onChange }: DatasetReleaseMultiSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selectedReleases = useMemo(() => releases.filter((release) => selectedReleaseRefs.includes(release.release_ref)), [releases, selectedReleaseRefs]);
  const filteredReleases = useMemo(() => {
    const normalized = query.trim().replace(/-/g, "").toLowerCase();
    if (!normalized) return releases;
    return releases.filter((release) => release.dataset_date.replace(/-/g, "").toLowerCase().includes(normalized));
  }, [query, releases]);
  const selectedVisibleCount = filteredReleases.filter((release) => selectedReleaseRefs.includes(release.release_ref)).length;
  const allVisibleSelected = filteredReleases.length > 0 && selectedVisibleCount === filteredReleases.length;
  const summary = selectedReleases.length === 0
    ? "请选择要传输的已发布日期"
    : selectedReleases.length === 1
      ? selectedReleases[0].dataset_date
      : `已选择 ${selectedReleases.length} 个日期`;
  const detail = selectedReleases.length > 1
    ? `${selectedReleases.slice(0, 3).map((release) => release.dataset_date).join("、")}${selectedReleases.length > 3 ? "…" : ""}`
    : selectedReleases.length === 1
      ? `${selectedReleases[0].source_clip_count} 个 clip · ${formatTransferBytes(selectedReleases[0].source_manifest?.total_bytes)}`
      : "支持搜索并同时选择多个日期";

  const toggleRelease = (releaseRef: string) => {
    onChange(selectedReleaseRefs.includes(releaseRef)
      ? selectedReleaseRefs.filter((ref) => ref !== releaseRef)
      : [...selectedReleaseRefs, releaseRef]);
  };

  const toggleVisible = () => {
    if (allVisibleSelected) {
      const visibleRefs = new Set(filteredReleases.map((release) => release.release_ref));
      onChange(selectedReleaseRefs.filter((ref) => !visibleRefs.has(ref)));
      return;
    }
    onChange(Array.from(new Set([...selectedReleaseRefs, ...filteredReleases.map((release) => release.release_ref)])));
  };

  return <Popover open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) setQuery(""); }}>
    <PopoverTrigger asChild>
      <button type="button" aria-label="选择中心已发布数据" aria-expanded={open} className="flex min-h-14 w-full items-center gap-3 rounded-lg border border-console-line bg-console-panel2 px-4 py-2.5 text-left shadow-sm transition-colors hover:border-console-cyan/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30">
        <CalendarDays className="h-5 w-5 shrink-0 text-console-cyan" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          <span className={cn("block truncate text-sm font-medium", selectedReleases.length ? "text-console-text" : "text-console-muted")}>{loading ? "正在读取已发布数据…" : summary}</span>
          {!loading ? <span className="mt-0.5 block truncate text-xs text-console-muted">{detail}</span> : null}
        </span>
        {selectedReleases.length ? <span className="shrink-0 rounded-full bg-sky-50 px-2.5 py-1 text-xs font-medium text-console-cyan">已选 {selectedReleases.length} 项</span> : null}
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-console-muted transition-transform", open && "rotate-180")} aria-hidden="true" />
      </button>
    </PopoverTrigger>
    <PopoverContent align="start" sideOffset={6} aria-label="中心已发布数据选项" className="z-[100] w-[var(--radix-popover-trigger-width)] min-w-80 p-0">
      <div className="border-b border-console-line p-3">
        <label className="relative block">
          <span className="sr-only">搜索已发布日期</span>
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" aria-hidden="true" />
          <Input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索数据日期" className="pl-8" />
        </label>
        <div className="mt-2 flex items-center justify-between gap-3 text-xs">
          <span className="text-console-muted">{filteredReleases.length} 个可选日期</span>
          <div className="flex items-center gap-3">
            {filteredReleases.length ? <button type="button" className="font-medium text-console-cyan hover:underline" onClick={toggleVisible}>{allVisibleSelected ? "取消全选" : "全选当前结果"}</button> : null}
            {selectedReleaseRefs.length ? <button type="button" className="font-medium text-console-muted hover:text-console-text hover:underline" onClick={() => onChange([])}>清空</button> : null}
          </div>
        </div>
      </div>
      <div className="max-h-64 overflow-y-auto p-2">
        {loading ? <p className="p-3 text-sm text-console-muted">正在读取已发布数据…</p> : filteredReleases.length ? filteredReleases.map((release) => {
          const checked = selectedReleaseRefs.includes(release.release_ref);
          return <label key={release.release_ref} className={cn("flex cursor-pointer items-center gap-3 rounded-md px-3 py-2.5 transition-colors", checked ? "bg-sky-50" : "hover:bg-slate-50") }>
            <input type="checkbox" className="sr-only" checked={checked} onChange={() => toggleRelease(release.release_ref)} />
            <span aria-hidden="true" className={cn("flex h-4 w-4 shrink-0 items-center justify-center rounded border", checked ? "border-console-cyan bg-console-cyan text-white" : "border-console-line bg-white")}><Check className={cn("h-3 w-3", checked ? "opacity-100" : "opacity-0")} /></span>
            <span className="min-w-0 flex-1"><b className="block text-sm text-console-text">{release.dataset_date}</b><span className="mt-0.5 block text-xs text-console-muted">{release.source_clip_count} 个 clip · {formatTransferBytes(release.source_manifest?.total_bytes)}</span></span>
          </label>;
        }) : <p className="p-4 text-center text-sm text-console-muted">{releases.length ? "没有匹配的数据日期。" : "没有可传输的已发布日期，或全部日期已存在于该节点。"}</p>}
      </div>
      <div className="flex items-center justify-between border-t border-console-line px-3 py-2.5">
        <span className="text-xs text-console-muted">已选择 {selectedReleaseRefs.length} 个日期</span>
        <PopoverClose asChild><ConsoleButton variant="ghost">完成</ConsoleButton></PopoverClose>
      </div>
    </PopoverContent>
  </Popover>;
}

export function TrainingDatasetTransferMonitor({ transfers, error, onPause, onCancel, onRetry }: TransferMonitorProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [terminalDismissed, setTerminalDismissed] = useState(false);
  const activeTransfers = transfers.filter((transfer) => activeTransferStatuses.has(transfer.status));
  const actionableTransfers = transfers.filter((transfer) => actionableTransferStatuses.has(transfer.status));
  const displayedTransfers = activeTransfers.length || actionableTransfers.length
    ? [...activeTransfers, ...actionableTransfers.filter((transfer) => !activeTransfers.some((active) => active.transfer_ref === transfer.transfer_ref))]
    : transfers.slice(0, 1);
  const attentionSignature = [...activeTransfers, ...actionableTransfers].map((transfer) => transfer.transfer_ref).sort().join(",");

  useEffect(() => {
    if (attentionSignature) {
      setTerminalDismissed(false);
      setCollapsed(false);
    }
  }, [attentionSignature]);

  if ((!displayedTransfers.length && !error) || (terminalDismissed && !activeTransfers.length && !error)) return null;
  const averageProgress = displayedTransfers.length
    ? displayedTransfers.reduce((total, transfer) => total + transferProgress(transfer), 0) / displayedTransfers.length
    : 0;
  const floatingTitle = error
    ? "数据传输需要处理"
    : activeTransfers.length
      ? "数据传输进行中"
      : actionableTransfers.length
        ? "数据传输需要处理"
        : "数据传输已完成";
  const floatingSummary = activeTransfers.length
    ? `${activeTransfers.length} 个任务正在后台运行`
    : actionableTransfers.length
      ? `${actionableTransfers.length} 个任务等待处理`
      : "最近任务已完成";

  if (collapsed) {
    return <aside aria-label="数据传输进度" aria-live="polite" className="pointer-events-none fixed bottom-20 right-3 z-[70] sm:bottom-24 sm:right-5">
      <button type="button" aria-label="展开数据传输进度" onClick={() => setCollapsed(false)} className="pointer-events-auto flex min-h-12 max-w-[calc(100vw-1.5rem)] items-center gap-3 rounded-full border border-white/10 bg-console-text px-3 py-2 text-left text-white shadow-[0_18px_42px_rgba(23,32,46,0.22)] transition hover:bg-slate-800 hover:shadow-[0_22px_48px_rgba(23,32,46,0.26)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan focus-visible:ring-offset-2">
        <span className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-white/12"><CloudDownload className="h-4 w-4" aria-hidden="true" />{activeTransfers.length ? <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-console-text bg-emerald-400" /> : null}</span>
        <span className="min-w-0"><span className="block truncate text-sm font-semibold">{floatingTitle}</span><span className="block truncate text-[11px] text-slate-300">{activeTransfers.length ? `${averageProgress.toFixed(0)}% · ${floatingSummary}` : floatingSummary}</span></span>
        <ChevronDown className="h-4 w-4 shrink-0 rotate-180 text-slate-300" aria-hidden="true" />
      </button>
    </aside>;
  }

  return <aside aria-label="数据传输进度" aria-live="polite" className="pointer-events-none fixed bottom-20 right-3 z-[70] w-[min(23rem,calc(100vw-1.5rem))] sm:bottom-24 sm:right-5">
    <section className="pointer-events-auto overflow-hidden rounded-2xl border border-console-line bg-white shadow-[0_24px_64px_rgba(23,32,46,0.20)]">
      <div className="flex items-center gap-3 bg-console-text px-3.5 py-3 text-white">
        <span className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/12"><CloudDownload className="h-4.5 w-4.5" aria-hidden="true" />{activeTransfers.length ? <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full border-2 border-console-text bg-emerald-400 motion-reduce:animate-none" /> : null}</span>
        <div className="min-w-0 flex-1"><h2 id="dataset-transfer-monitor-title" className="truncate text-sm font-semibold">{floatingTitle}</h2><p className="mt-0.5 truncate text-[11px] text-slate-300">{floatingSummary}</p></div>
        <button type="button" aria-label="收起数据传输进度" title="收起" onClick={() => setCollapsed(true)} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-slate-300 transition hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70"><ChevronDown className="h-4 w-4" /></button>
        {!activeTransfers.length && !actionableTransfers.length ? <button type="button" aria-label="关闭数据传输进度" title="关闭" onClick={() => setTerminalDismissed(true)} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-slate-300 transition hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70"><X className="h-4 w-4" /></button> : null}
      </div>
      {error ? <p role="alert" className="m-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p> : null}
      {displayedTransfers.length ? <div className="max-h-[min(19rem,50vh)] space-y-2 overflow-y-auto p-3">{displayedTransfers.map((transfer) => {
        const progress = transferProgress(transfer);
        const canStop = ["preparing", "queued", "running"].includes(transfer.status);
        const canDiscard = canStop || actionableTransferStatuses.has(transfer.status);
        return <article key={transfer.transfer_ref} className="rounded-xl border border-console-line bg-console-panel2 p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><b className="text-sm text-console-text">{transfer.dataset_date}</b><span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", transferStatusClass(transfer.status))}>{transferLabel(transfer.status)}</span></div><p className="mt-1 text-xs tabular-nums text-console-muted">{formatTransferBytes(transfer.bytes_transferred)} / {formatTransferBytes(transfer.total_bytes)} · {progress.toFixed(0)}%</p></div>
            <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
              {canStop ? <button type="button" aria-label={`暂停 ${transfer.dataset_date} 数据传输`} onClick={() => onPause(transfer)} className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-console-line bg-white px-2.5 text-xs font-medium text-console-text transition hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30"><Square className="h-3 w-3" />暂停</button> : null}
              {actionableTransferStatuses.has(transfer.status) ? <ConsoleButton variant="ghost" onClick={() => onRetry(transfer)}><RotateCcw className="h-3.5 w-3.5" />{transfer.status === "paused" ? "继续传输" : "重试"}</ConsoleButton> : null}
              {canDiscard ? <button type="button" aria-label={`取消 ${transfer.dataset_date} 本次传输并清理`} onClick={() => onCancel(transfer)} className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-2.5 text-xs font-medium text-rose-700 transition hover:border-rose-300 hover:bg-rose-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-300"><X className="h-3 w-3" />取消本次传输</button> : null}
            </div>
          </div>
          <ProgressBar className="mt-2.5" value={progress} tone={transfer.status === "failed" ? "danger" : transfer.status === "succeeded" ? "success" : "info"} label={`${transfer.dataset_date} 传输进度 ${progress.toFixed(0)}%`} />
          {transfer.error_message ? <p className="mt-2 text-xs leading-5 text-rose-700">{transfer.error_message}</p> : null}
        </article>;
      })}</div> : null}
    </section>
  </aside>;
}

export function TrainingDatasetTransferDialog({ open, nodeRef, unavailableReleaseRefs, onOpenChange, onTransfersCreated }: Props) {
  const [releases, setReleases] = useState<TrainingDatasetRelease[]>([]);
  const [selectedReleaseRefs, setSelectedReleaseRefs] = useState<string[]>([]);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [listing, setListing] = useState<TrainingDirectoryListing | null>(null);
  const [selectedDirectory, setSelectedDirectory] = useState<SelectedDirectory | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const browse = useCallback(async (path: string) => {
    if (!nodeRef) return;
    setError(null);
    try {
      setListing(await requestTrainingDirectoryListing(nodeRef, path));
    } catch (caught) {
      setError(errorText(caught));
    }
  }, [nodeRef]);

  useEffect(() => {
    setListing(null);
    setSelectedDirectory(null);
  }, [nodeRef]);

  useEffect(() => {
    if (!open || !nodeRef) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSelectedReleaseRefs([]);
    listTrainingDatasetReleases()
      .then((nextReleases) => { if (!cancelled) setReleases(nextReleases); })
      .catch((caught) => { if (!cancelled) setError(errorText(caught)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [nodeRef, open]);

  useEffect(() => {
    if (!directoryOpen || listing) return;
    void browse(selectedDirectory?.path ?? "/");
  }, [browse, directoryOpen, listing, selectedDirectory?.path]);

  useEffect(() => {
    if (!directoryOpen || !listing || !["queued", "running"].includes(listing.status)) return;
    const timer = window.setTimeout(() => {
      void getTrainingDirectoryListing(listing.listing_ref)
        .then(setListing)
        .catch((caught) => setError(errorText(caught)));
    }, 700);
    return () => window.clearTimeout(timer);
  }, [directoryOpen, listing]);

  const availableReleases = useMemo(() => releases.filter((release) => !unavailableReleaseRefs.has(release.release_ref)), [releases, unavailableReleaseRefs]);
  const selectedDestinations = useMemo(() => {
    if (!selectedDirectory) return [];
    const selected = new Set(selectedReleaseRefs);
    return releases
      .filter((release) => selected.has(release.release_ref))
      .map((release) => datasetTransferDestination(selectedDirectory.path, release));
  }, [releases, selectedDirectory, selectedReleaseRefs]);
  const breadcrumbs = useMemo(() => {
    const path = listing?.path || "/";
    const parts = path.split("/").filter(Boolean);
    return [{ name: "/", path: "/" }, ...parts.map((name, index) => ({ name, path: `/${parts.slice(0, index + 1).join("/")}` }))];
  }, [listing?.path]);

  const chooseDirectory = () => {
    if (listing?.status !== "succeeded" || !listing.writable) return;
    setSelectedDirectory({ path: listing.path, freeBytes: listing.free_bytes ?? null });
    setDirectoryOpen(false);
  };

  const submit = async () => {
    if (!selectedReleaseRefs.length || !selectedDirectory) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await createTrainingDatasetTransfers({ node_ref: nodeRef, release_refs: selectedReleaseRefs, target_parent_directory: selectedDirectory.path });
      onTransfersCreated(created);
      setSelectedReleaseRefs([]);
      onOpenChange(false);
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const changeMainOpen = (nextOpen: boolean) => {
    if (!nextOpen) setDirectoryOpen(false);
    onOpenChange(nextOpen);
  };

  return <>
    <Dialog open={open} onOpenChange={changeMainOpen}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>从中心服务器传输数据</DialogTitle>
          <DialogDescription>选择已发布日期和训练节点中的保存位置。提交后可以关闭窗口，传输任务会继续在后台运行。</DialogDescription>
        </DialogHeader>
        {error ? <p role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p> : null}
        <section aria-labelledby="central-releases-title">
          <h3 id="central-releases-title" className="font-medium text-console-text">1. 选择中心已发布数据</h3>
          <div className="mt-2"><DatasetReleaseMultiSelect loading={loading} releases={availableReleases} selectedReleaseRefs={selectedReleaseRefs} onChange={setSelectedReleaseRefs} /></div>
        </section>
        <section aria-labelledby="selected-directory-title">
          <h3 id="selected-directory-title" className="font-medium text-console-text">2. 选择保存位置</h3>
          <div className="mt-2 flex items-start gap-3 rounded-lg border border-console-line bg-console-panel2 px-4 py-3">
            <FolderOpen className="h-5 w-5 shrink-0 text-amber-500" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <p className="text-xs text-console-muted">实际保存位置</p>
              {selectedDestinations.length ? <div className="mt-1 max-h-24 space-y-1 overflow-y-auto pr-1">
                {selectedDestinations.map((destination) => <p key={destination} className="break-all font-mono text-sm text-console-text" title={destination}>{destination}</p>)}
              </div> : <p className="mt-0.5 text-sm text-console-muted">{selectedDirectory ? "选择数据日期后显示完整位置" : "尚未选择保存位置"}</p>}
              {selectedDirectory ? <p className="mt-1 text-xs text-emerald-700">可写 · 剩余 {formatTransferBytes(selectedDirectory.freeBytes)}</p> : null}
            </div>
            <ConsoleButton className="shrink-0" aria-label="选择保存目录" variant="ghost" onClick={() => setDirectoryOpen(true)}><Folder className="h-4 w-4" />{selectedDirectory ? "更改" : "选择"}</ConsoleButton>
          </div>
          <p className="mt-2 text-xs text-console-muted">系统将在所选目录下创建 <span className="font-mono">datapilot-managed/日期-标识</span>，不会覆盖已有文件。</p>
        </section>
        <DialogFooter>
          <ConsoleButton onClick={() => changeMainOpen(false)}>关闭</ConsoleButton>
          <ConsoleButton variant="primary" disabled={submitting || !selectedReleaseRefs.length || !selectedDirectory} onClick={() => void submit()}>{submitting ? <RefreshCw className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <UploadCloud className="h-4 w-4" />}{submitting ? "正在创建…" : "开始传输"}</ConsoleButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={directoryOpen} onOpenChange={setDirectoryOpen}>
      <DialogContent className="z-[95] max-h-[88vh] overflow-hidden p-0 sm:max-w-2xl">
        <DialogHeader className="px-5 pt-5">
          <DialogTitle>选择保存位置</DialogTitle>
          <DialogDescription>浏览训练节点中的目录。只能选择 Worker 当前运行账号可写的位置。</DialogDescription>
        </DialogHeader>
        {error ? <p role="alert" className="mx-5 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p> : null}
        <div className="mx-5 overflow-hidden rounded-lg border border-console-line">
          <nav aria-label="目录路径" className="flex min-h-11 flex-wrap items-center gap-1 border-b border-console-line bg-console-panel2 px-3 py-2 text-xs">{breadcrumbs.map((item, index) => <span key={item.path} className="flex items-center gap-1">{index ? <ChevronRight className="h-3 w-3 text-console-muted" /> : null}<button type="button" className="rounded-sm font-mono text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => void browse(item.path)}>{item.name}</button></span>)}</nav>
          <div className="max-h-[45vh] min-h-72 overflow-y-auto bg-console-panel p-2">{listing?.status === "failed" ? <p className="p-3 text-sm text-rose-700">{listing.error_message || "目录读取失败。"}</p> : listing?.status === "succeeded" ? listing.directories.length ? listing.directories.map((directory) => <button key={directory.path} type="button" className="flex w-full items-center justify-between gap-3 rounded-md px-3 py-2.5 text-left text-sm hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/25" onClick={() => void browse(directory.path)}><span className="flex min-w-0 items-center gap-2"><Folder className="h-4 w-4 shrink-0 text-amber-500" /><span className="truncate">{directory.name}</span></span><span className={cn("shrink-0 text-xs", directory.writable ? "text-emerald-700" : "text-console-muted")}>{directory.writable ? "可写" : "只读"}</span></button>) : <p className="p-3 text-sm text-console-muted">当前目录没有可进入的子目录。</p> : <div className="flex min-h-72 items-center justify-center gap-2 text-sm text-console-muted"><RefreshCw className="h-4 w-4 animate-spin text-console-cyan motion-reduce:animate-none" />正在读取训练节点目录…</div>}</div>
          <div className="border-t border-console-line bg-console-panel2 px-3 py-2.5"><p className="break-all font-mono text-xs text-console-text">{listing?.path ?? "/"}</p><p className={cn("mt-1 flex items-center gap-1 text-xs", listing?.writable ? "text-emerald-700" : "text-amber-700")}><HardDrive className="h-3.5 w-3.5" />{listing?.status === "succeeded" ? listing.writable ? `当前目录可写 · 剩余 ${formatTransferBytes(listing.free_bytes)}` : "当前目录不可写，请进入其他目录" : "正在确认目录权限"}</p></div>
        </div>
        <DialogFooter className="mx-0 mb-0 px-5">
          <ConsoleButton onClick={() => setDirectoryOpen(false)}>取消</ConsoleButton>
          <ConsoleButton variant="primary" disabled={listing?.status !== "succeeded" || !listing.writable} onClick={chooseDirectory}><FolderOpen className="h-4 w-4" />选择当前目录</ConsoleButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>;
}
