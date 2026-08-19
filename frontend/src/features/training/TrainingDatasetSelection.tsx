import { Check, ChevronDown, Database, Search, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";

import type { TrainingDatasetReplica } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Popover, PopoverClose, PopoverContent, PopoverTrigger } from "../../components/ui/popover";
import { cn } from "../../lib/utils";
import { formatTransferBytes } from "./TrainingDatasetTransferDialog";

type Props = {
  replicas: TrainingDatasetReplica[];
  trainReplicaRefs: string[];
  testReplicaRefs: string[];
  testSetEnabled: boolean;
  canManage: boolean;
  managementOpen: boolean;
  onManagementOpenChange: (open: boolean) => void;
  onToggleReplica: (replicaRef: string, selected: boolean) => void;
  onSetReplicaSplit: (replicaRef: string, split: "train" | "test") => void;
  onRemoveReplica: (replica: TrainingDatasetReplica) => Promise<string | null>;
};

function formatReplicaTime(value: string | null | undefined) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function DatasetReplicaMultiSelect({
  replicas,
  selectedReplicaRefs,
  disabled,
  onToggle,
}: {
  replicas: TrainingDatasetReplica[];
  selectedReplicaRefs: Set<string>;
  disabled: boolean;
  onToggle: (replicaRef: string, selected: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selectedReplicas = replicas.filter((replica) => selectedReplicaRefs.has(replica.replica_ref));
  const filteredReplicas = useMemo(() => {
    const normalized = query.trim().replace(/-/g, "").toLowerCase();
    if (!normalized) return replicas;
    return replicas.filter((replica) => replica.dataset_date.replace(/-/g, "").toLowerCase().includes(normalized));
  }, [query, replicas]);
  const summary = selectedReplicas.length === 0
    ? "选择训练数据"
    : selectedReplicas.length === 1
      ? selectedReplicas[0].dataset_date
      : `已选择 ${selectedReplicas.length} 个日期`;

  return <Popover open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) setQuery(""); }}>
    <PopoverTrigger asChild>
      <button
        type="button"
        aria-label="选择训练数据"
        aria-expanded={open}
        disabled={disabled}
        className="flex min-h-11 w-full items-center gap-3 rounded-lg border border-console-line bg-console-panel px-3 py-2 text-left shadow-sm transition-colors hover:border-console-cyan/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <Database className="h-4 w-4 shrink-0 text-console-cyan" aria-hidden="true" />
        <span className={cn("min-w-0 flex-1 truncate text-sm", selectedReplicas.length ? "font-medium text-console-text" : "text-console-muted")}>{summary}</span>
        {selectedReplicas.length ? <span className="shrink-0 rounded-full bg-sky-50 px-2 py-0.5 text-xs font-medium text-console-cyan">{selectedReplicas.length} 项</span> : null}
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-console-muted transition-transform", open && "rotate-180")} aria-hidden="true" />
      </button>
    </PopoverTrigger>
    <PopoverContent align="start" sideOffset={6} aria-label="训练数据选项" className="z-[100] w-[var(--radix-popover-trigger-width)] min-w-80 p-0">
      <div className="border-b border-console-line p-3">
        <label className="relative block">
          <span className="sr-only">搜索训练数据日期</span>
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" aria-hidden="true" />
          <Input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索数据日期" className="pl-8" />
        </label>
        <div className="mt-2 flex items-center justify-between text-xs text-console-muted">
          <span>{filteredReplicas.length} 个可用日期</span>
          {selectedReplicas.length ? <button type="button" className="font-medium hover:text-console-text hover:underline" onClick={() => selectedReplicas.forEach((replica) => onToggle(replica.replica_ref, false))}>清空选择</button> : null}
        </div>
      </div>
      <div className="max-h-64 overflow-y-auto p-2">
        {filteredReplicas.length ? filteredReplicas.map((replica) => {
          const checked = selectedReplicaRefs.has(replica.replica_ref);
          return <label key={replica.replica_ref} className={cn("flex cursor-pointer items-center gap-3 rounded-md px-3 py-2.5 transition-colors", checked ? "bg-sky-50" : "hover:bg-slate-50")}>
            <input aria-label={`使用 ${replica.dataset_date} 数据`} type="checkbox" className="sr-only" checked={checked} onChange={(event) => onToggle(replica.replica_ref, event.target.checked)} />
            <span aria-hidden="true" className={cn("flex h-4 w-4 shrink-0 items-center justify-center rounded border", checked ? "border-console-cyan bg-console-cyan text-white" : "border-console-line bg-white")}><Check className={cn("h-3 w-3", checked ? "opacity-100" : "opacity-0")} /></span>
            <span className="min-w-0 flex-1"><b className="block text-sm text-console-text">{replica.dataset_date}</b><span className="mt-0.5 block text-xs text-console-muted">{replica.file_count != null ? `${replica.file_count} 个文件 · ` : ""}{formatTransferBytes(replica.total_bytes)}</span></span>
          </label>;
        }) : <p className="p-4 text-center text-sm text-console-muted">{replicas.length ? "没有匹配的数据日期。" : "该训练节点暂无可用数据。"}</p>}
      </div>
      <div className="flex items-center justify-between border-t border-console-line px-3 py-2.5">
        <span className="text-xs text-console-muted">已选择 {selectedReplicas.length} 个日期</span>
        <PopoverClose asChild><ConsoleButton variant="ghost">完成</ConsoleButton></PopoverClose>
      </div>
    </PopoverContent>
  </Popover>;
}

function NodeDatasetManagementDialog({
  open,
  replicas,
  canManage,
  onOpenChange,
  onRemoveReplica,
}: {
  open: boolean;
  replicas: TrainingDatasetReplica[];
  canManage: boolean;
  onOpenChange: (open: boolean) => void;
  onRemoveReplica: (replica: TrainingDatasetReplica) => Promise<string | null>;
}) {
  const [pendingRemoval, setPendingRemoval] = useState<TrainingDatasetReplica | null>(null);
  const [removingRef, setRemovingRef] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);

  const confirmRemoval = async () => {
    if (!pendingRemoval) return;
    const replica = pendingRemoval;
    setRemovingRef(replica.replica_ref);
    setFeedback(null);
    const error = await onRemoveReplica(replica);
    setRemovingRef(null);
    setPendingRemoval(null);
    setFeedback(error
      ? { tone: "danger", message: error }
      : { tone: "success", message: `${replica.dataset_date} 的删除请求已提交，Worker 正在清理训练节点中的数据。` });
  };

  return <>
    <Dialog open={open} onOpenChange={(nextOpen) => { onOpenChange(nextOpen); if (!nextOpen) setFeedback(null); }}>
      <DialogContent className="max-h-[88vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>管理节点数据</DialogTitle>
          <DialogDescription>查看该训练节点已传输完成的数据。删除只影响训练节点中的托管副本，不会删除中心服务器中的已发布数据。</DialogDescription>
        </DialogHeader>
        {feedback ? <p role="status" className={cn("rounded-md border px-3 py-2 text-sm", feedback.tone === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-rose-200 bg-rose-50 text-rose-700")}>{feedback.message}</p> : null}
        <div className="space-y-2">
          {replicas.length ? replicas.map((replica) => <article key={replica.replica_ref} className="rounded-md border border-console-line bg-console-panel2 px-3 py-2.5">
            <div className="flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
                  <div className="flex items-center gap-2"><h3 className="font-semibold text-console-text">{replica.dataset_date}</h3><span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">可用于训练</span></div>
                  <dl className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                    <div className="flex items-center gap-1"><dt className="text-console-muted">数据量</dt><dd className="font-medium text-console-text">{formatTransferBytes(replica.total_bytes)}</dd></div>
                    <div className="flex items-center gap-1"><dt className="text-console-muted">文件数</dt><dd className="font-medium text-console-text">{replica.file_count?.toLocaleString("zh-CN") ?? "--"}</dd></div>
                    <div className="flex items-center gap-1"><dt className="text-console-muted">完成于</dt><dd className="font-medium text-console-text">{formatReplicaTime(replica.created_at)}</dd></div>
                  </dl>
                </div>
                <p className="mt-1.5 truncate font-mono text-xs text-console-muted" title={replica.local_root}>{replica.local_root}</p>
              </div>
              <button type="button" aria-label={`删除节点中的 ${replica.dataset_date} 数据`} disabled={!canManage || removingRef === replica.replica_ref} className="shrink-0 rounded-md p-1.5 text-console-muted transition-colors hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-300 disabled:cursor-not-allowed disabled:opacity-50" onClick={() => { setFeedback(null); setPendingRemoval(replica); }}><Trash2 className="h-4 w-4" /></button>
            </div>
          </article>) : <div className="rounded-md border border-dashed border-console-line p-8 text-center"><p className="font-medium text-console-text">该训练节点暂无托管数据</p><p className="mt-1 text-sm text-console-muted">从中心服务器传输完成后，数据会显示在这里。</p></div>}
        </div>
        <DialogFooter><ConsoleButton onClick={() => onOpenChange(false)}>关闭</ConsoleButton></DialogFooter>
      </DialogContent>
    </Dialog>
    <AlertDialog open={Boolean(pendingRemoval)} onOpenChange={(nextOpen) => { if (!nextOpen && !removingRef) setPendingRemoval(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>删除训练节点中的数据？</AlertDialogTitle>
          <AlertDialogDescription>将删除 {pendingRemoval?.dataset_date} 在该训练节点中的全部托管数据。之后若要再次训练，需要重新从中心服务器传输；中心服务器中的已发布数据不会受影响。</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={Boolean(removingRef)}>取消</AlertDialogCancel>
          <AlertDialogAction variant="destructive" disabled={Boolean(removingRef)} onClick={(event) => { event.preventDefault(); void confirmRemoval(); }}>{removingRef ? "正在提交…" : "确认删除"}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </>;
}

export function TrainingDatasetSelection({
  replicas,
  trainReplicaRefs,
  testReplicaRefs,
  testSetEnabled,
  canManage,
  managementOpen,
  onManagementOpenChange,
  onToggleReplica,
  onSetReplicaSplit,
  onRemoveReplica,
}: Props) {
  const selectedReplicaRefs = new Set([...trainReplicaRefs, ...testReplicaRefs]);
  const selectedReplicas = replicas.filter((replica) => selectedReplicaRefs.has(replica.replica_ref));

  return <>
    <p className="mb-2 text-sm font-medium text-console-text">本次使用的数据</p>
    <DatasetReplicaMultiSelect replicas={replicas} selectedReplicaRefs={selectedReplicaRefs} disabled={!canManage || !replicas.length} onToggle={onToggleReplica} />
    {selectedReplicas.length ? <div className="mt-2 flex flex-wrap gap-2">
      {selectedReplicas.map((replica) => {
        const split = testReplicaRefs.includes(replica.replica_ref) ? "test" : "train";
        return <article key={replica.replica_ref} className="inline-flex min-h-8 max-w-full items-center gap-2 rounded-md border border-console-cyan/35 bg-sky-50/60 px-2.5 py-1.5">
          <p className="shrink-0 text-sm font-medium text-console-text">{replica.dataset_date}</p>
          {testSetEnabled ? <div className="flex items-center gap-1 text-xs"><label className={cn("inline-flex cursor-pointer items-center rounded px-1.5 py-0.5 font-medium", split === "train" ? "bg-white text-console-cyan shadow-sm" : "text-console-muted hover:text-console-text")}><input aria-label={`${replica.dataset_date} 训练集`} type="radio" className="sr-only" checked={split === "train"} onChange={() => onSetReplicaSplit(replica.replica_ref, "train")} />训练集</label><label className={cn("inline-flex cursor-pointer items-center rounded px-1.5 py-0.5 font-medium", split === "test" ? "bg-white text-amber-700 shadow-sm" : "text-console-muted hover:text-console-text")}><input aria-label={`${replica.dataset_date} 测试集`} type="radio" className="sr-only" checked={split === "test"} onChange={() => onSetReplicaSplit(replica.replica_ref, "test")} />测试集</label></div> : <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-xs font-medium text-emerald-700">训练集</span>}
          <button type="button" aria-label={`取消选择 ${replica.dataset_date} 数据`} className="shrink-0 rounded p-0.5 text-console-muted transition-colors hover:bg-white hover:text-console-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => onToggleReplica(replica.replica_ref, false)}><X className="h-3.5 w-3.5" /></button>
        </article>;
      })}
    </div> : <p className="mt-2 text-xs text-console-muted">从下拉框选择本次训练要使用的日期。</p>}
    <NodeDatasetManagementDialog open={managementOpen} replicas={replicas} canManage={canManage} onOpenChange={onManagementOpenChange} onRemoveReplica={onRemoveReplica} />
  </>;
}
