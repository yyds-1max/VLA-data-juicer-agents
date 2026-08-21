import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  Database,
  FileOutput,
  RefreshCw,
  Square,
  Terminal,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getTrainingRun, getTrainingRunLogs, getTrainingRunMetrics, stopTrainingRun } from "../../api/client";
import type {
  TrainingArtifact,
  TrainingMetricSample,
  TrainingRun,
  TrainingRunLog,
  TrainingStage,
} from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { ProgressBar } from "../../components/console/ProgressBar";
import { StatusTag } from "../../components/console/StatusTag";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import { cn } from "../../lib/utils";
import type { StatusTone } from "../console/consoleTypes";
import { MiniChart } from "../console/visuals/MiniChart";

const activeStatuses = new Set<TrainingRun["status"]>(["queued", "preparing", "running", "stop_requested"]);

const runStatusMeta: Record<TrainingRun["status"], { label: string; tone: StatusTone }> = {
  queued: { label: "排队中", tone: "warning" },
  preparing: { label: "准备中", tone: "warning" },
  running: { label: "训练中", tone: "info" },
  stop_requested: { label: "正在停止", tone: "warning" },
  succeeded: { label: "已完成", tone: "success" },
  failed: { label: "失败", tone: "danger" },
  cancelled: { label: "已取消", tone: "neutral" },
  lost: { label: "状态丢失", tone: "danger" },
};

const stageStatusMeta: Record<TrainingStage["status"], { label: string; tone: StatusTone; dot: string }> = {
  pending: { label: "等待中", tone: "neutral", dot: "bg-slate-400" },
  preparing: { label: "准备中", tone: "warning", dot: "bg-amber-500" },
  running: { label: "训练中", tone: "info", dot: "bg-blue-600" },
  succeeded: { label: "已完成", tone: "success", dot: "bg-emerald-600" },
  failed: { label: "失败", tone: "danger", dot: "bg-rose-600" },
  cancelled: { label: "已取消", tone: "neutral", dot: "bg-slate-400" },
  skipped: { label: "已跳过", tone: "neutral", dot: "bg-slate-400" },
  lost: { label: "状态丢失", tone: "danger", dot: "bg-rose-600" },
};

const commonParameterNames = new Set([
  "learning_rate",
  "max_steps",
  "num_train_epochs",
  "per_device_train_batch_size",
  "gradient_accumulation_steps",
  "num_video_frames",
  "model_name_or_path",
  "data_mixture",
  "save_strategy",
  "save_steps",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function errorText(error: unknown) {
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

function formatNumber(value: number | null | undefined, digits = 4) {
  return value == null || !Number.isFinite(value) ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function formatEpoch(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function formatClock(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDuration(startedAt?: string | null, finishedAt?: string | null) {
  if (!startedAt) return "--";
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "--";
  const seconds = Math.floor((end - start) / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分 ${remainder} 秒`;
}

function usesStepLimit(stage: TrainingStage | undefined) {
  const value = stage?.parameters?.max_steps;
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function metricLabel(item: TrainingMetricSample) {
  return item.step != null ? String(item.step) : formatClock(item.created_at);
}

function chartData(
  metrics: TrainingMetricSample[],
  key: "loss" | "learning_rate" | "grad_norm" | "gpu_utilization_percent" | "gpu_memory_mib",
  label: string,
  color: string,
) {
  const samples = metrics.filter((item) => item[key] != null && Number.isFinite(item[key]));
  if (!samples.length) return null;
  return {
    labels: samples.map(metricLabel),
    data: samples.map((item) => Number(item[key])),
    label,
    color,
  };
}

type DatasetEntry = {
  dataset_date?: string;
  release_ref?: string;
  replica_ref?: string;
  local_root?: string;
  inventory_sha256?: string;
};

function datasetSplits(snapshot: Record<string, unknown> | null | undefined) {
  if (!isRecord(snapshot)) return { train: [] as DatasetEntry[], test: [] as DatasetEntry[], snapshotRef: null as string | null };
  const manifest = isRecord(snapshot.manifest) ? snapshot.manifest : snapshot;
  const splits = isRecord(manifest.splits) ? manifest.splits : {};
  const normalize = (items: unknown): DatasetEntry[] => Array.isArray(items) ? items.filter(isRecord).map((item) => ({
    dataset_date: typeof item.dataset_date === "string" ? item.dataset_date : undefined,
    release_ref: typeof item.release_ref === "string" ? item.release_ref : undefined,
    replica_ref: typeof item.replica_ref === "string" ? item.replica_ref : undefined,
    local_root: typeof item.local_root === "string" ? item.local_root : undefined,
    inventory_sha256: typeof item.inventory_sha256 === "string" ? item.inventory_sha256 : undefined,
  })) : [];
  return {
    train: normalize(splits.train),
    test: normalize(splits.test),
    snapshotRef: typeof snapshot.snapshot_ref === "string" ? snapshot.snapshot_ref : null,
  };
}

function OverviewStat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return <div className="min-w-0 px-4 py-3.5">
    <p className="text-xs text-console-muted">{label}</p>
    <div className="mt-1 truncate text-xl font-semibold tabular-nums text-console-text">{value}</div>
    {hint ? <p className="mt-1 truncate text-[11px] text-console-muted">{hint}</p> : null}
  </div>;
}

function StageTabs({ stages, selectedStageRef, onSelect }: { stages: TrainingStage[]; selectedStageRef: string; onSelect: (stageRef: string) => void }) {
  return <nav className="flex gap-2 overflow-x-auto pb-2" role="tablist" aria-label="任务训练阶段">
    {stages.map((stage) => {
      const active = stage.stage_ref === selectedStageRef;
      const meta = stageStatusMeta[stage.status];
      return <button
        key={stage.stage_ref}
        type="button"
        role="tab"
        aria-selected={active}
        aria-label={`${stage.stage_name} · ${meta.label}`}
        className={cn(
          "min-w-40 shrink-0 rounded-lg border bg-white px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30",
          active ? "border-console-cyan shadow-[0_0_0_1px_rgba(49,86,200,0.12)]" : "border-console-line hover:border-slate-300",
        )}
        onClick={() => onSelect(stage.stage_ref)}
      >
        <span className="flex items-center justify-between gap-3">
          <span className={cn("h-2 w-2 shrink-0 rounded-full", meta.dot, stage.status === "running" && "animate-pulse motion-reduce:animate-none")} />
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-console-text">{stage.stage_name}</span>
          <StatusTag tone={meta.tone} className="shrink-0">{meta.label}</StatusTag>
        </span>
        <span className="mt-1.5 block text-xs tabular-nums text-console-muted">
          {stage.total_steps > 0 ? `${stage.current_step}/${stage.total_steps} Step` : `${(stage.progress_percent ?? (stage.progress ?? 0) * 100).toFixed(1)}%`}
        </span>
      </button>;
    })}
  </nav>;
}

function MetricCard({ title, value, data, emptyText = "当前阶段尚未上报该指标。" }: { title: string; value?: string; data: ReturnType<typeof chartData>; emptyText?: string }) {
  return <ConsoleCard className="min-w-0 shadow-none">
    <div className="mb-3 flex items-baseline justify-between gap-3">
      <h3 className="text-sm font-semibold text-console-text">{title}</h3>
      <span className="font-mono text-sm font-semibold tabular-nums text-console-text">{value ?? "--"}</span>
    </div>
    {data ? <MiniChart type="line" title={title} data={data} /> : <div className="flex h-36 items-center justify-center rounded-md border border-dashed border-console-line bg-white text-sm text-console-muted">{emptyText}</div>}
  </ConsoleCard>;
}

function DatasetSplitCard({ title, entries, emptyText }: { title: string; entries: DatasetEntry[]; emptyText: string }) {
  return <ConsoleCard className="shadow-none">
    <div className="flex items-center justify-between gap-3">
      <div className="flex items-center gap-2"><Database className="h-4 w-4 text-console-cyan" /><h3 className="font-semibold text-console-text">{title}</h3></div>
      <span className="text-xs text-console-muted">{entries.length} 个日期</span>
    </div>
    {entries.length ? <div className="mt-3 flex flex-wrap gap-2">{entries.map((entry, index) => <div key={entry.replica_ref ?? `${entry.dataset_date}-${index}`} className="rounded-md border border-console-line bg-white px-3 py-2">
      <p className="font-mono text-sm font-semibold text-console-text">{entry.dataset_date ?? "未知日期"}</p>
      <p className="mt-0.5 max-w-72 truncate text-xs text-console-muted">{entry.release_ref ?? entry.replica_ref ?? "托管数据副本"}</p>
    </div>)}</div> : <p className="mt-3 rounded-md border border-dashed border-console-line bg-white px-3 py-5 text-center text-sm text-console-muted">{emptyText}</p>}
  </ConsoleCard>;
}

function ParameterTable({ entries }: { entries: Array<[string, string | number | boolean]> }) {
  if (!entries.length) return <p className="py-4 text-sm text-console-muted">无可展示参数。</p>;
  return <div className="overflow-x-auto"><table className="w-full min-w-[36rem] text-left text-sm">
    <thead className="text-xs text-console-muted"><tr><th className="border-b border-console-line px-3 py-2 font-medium">参数字段名</th><th className="border-b border-console-line px-3 py-2 font-medium">本次使用值</th></tr></thead>
    <tbody>{entries.map(([key, value]) => <tr key={key}><td className="border-b border-console-line/70 px-3 py-2 font-mono text-xs text-console-text">{key}</td><td className="border-b border-console-line/70 px-3 py-2 font-mono text-xs text-console-text break-all">{String(value)}</td></tr>)}</tbody>
  </table></div>;
}

function ArtifactTable({ stage, artifacts, versionModel }: { stage: TrainingStage; artifacts: TrainingArtifact[]; versionModel: TrainingRun["version_model"] }) {
  const rows: Array<{ kind: string; step: number | null; path: string; time?: string | null }> = [];
  if (stage.output_directory) rows.push({ kind: "阶段输出", step: null, path: stage.output_directory });
  artifacts.forEach((artifact) => {
    const row = {
      kind: artifact.kind === "checkpoint" ? "Checkpoint" : artifact.kind === "version_model" ? "版本模型" : "阶段输出",
      step: artifact.step ?? null,
      path: artifact.relative_path ?? artifact.output_directory ?? artifact.path ?? "路径待 Worker 上报",
      time: artifact.created_at,
    };
    if (!rows.some((item) => item.kind === row.kind && item.path === row.path && item.step === row.step)) rows.push(row);
  });
  if (versionModel && !rows.some((item) => item.kind === "版本模型" && item.path === versionModel.output_directory)) rows.push({ kind: "版本模型", step: null, path: versionModel.output_directory });
  return <ConsoleCard className="shadow-none">
    <div className="flex items-center gap-2"><FileOutput className="h-4 w-4 text-console-cyan" /><h2 className="font-semibold text-console-text">Checkpoint 与模型产物</h2></div>
    {rows.length ? <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[42rem] text-left text-sm"><thead className="text-xs text-console-muted"><tr><th className="border-b border-console-line px-3 py-2 font-medium">类型</th><th className="border-b border-console-line px-3 py-2 font-medium">Step</th><th className="border-b border-console-line px-3 py-2 font-medium">路径</th><th className="border-b border-console-line px-3 py-2 font-medium">记录时间</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.kind}-${row.path}-${index}`}><td className="border-b border-console-line/70 px-3 py-2"><span className="font-medium text-console-text">{row.kind}</span>{row.kind === "版本模型" ? <StatusTag tone="success" className="ml-2">版本模型</StatusTag> : null}</td><td className="border-b border-console-line/70 px-3 py-2 font-mono text-xs text-console-muted">{row.step ?? "--"}</td><td className="border-b border-console-line/70 px-3 py-2 font-mono text-xs text-console-text break-all">{row.path}</td><td className="border-b border-console-line/70 px-3 py-2 text-xs text-console-muted">{formatDateTime(row.time)}</td></tr>)}</tbody></table></div> : <p className="mt-3 rounded-md border border-dashed border-console-line bg-white px-3 py-5 text-center text-sm text-console-muted">训练程序尚未上报 checkpoint。</p>}
  </ConsoleCard>;
}

export function TrainingRunDetail({ run, canStop, onBack, onRunChange }: { run: TrainingRun; canStop: boolean; onBack: () => void; onRunChange: (run: TrainingRun) => void }) {
  const [logs, setLogs] = useState<TrainingRunLog[]>([]);
  const [metrics, setMetrics] = useState<TrainingMetricSample[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectedStageRef, setSelectedStageRef] = useState("");
  const [stopDialogOpen, setStopDialogOpen] = useState(false);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const [followLogs, setFollowLogs] = useState(true);
  const logViewportRef = useRef<HTMLDivElement>(null);
  const lastLogSeq = useRef(0);
  const lastMetricSeq = useRef(0);

  useEffect(() => {
    setLogs([]);
    setMetrics([]);
    lastLogSeq.current = 0;
    lastMetricSeq.current = 0;
    setLoadError(null);
    setSelectedStageRef("");
    setFollowLogs(true);
  }, [run.run_ref]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [nextRun, nextLogs, nextMetrics] = await Promise.all([
          getTrainingRun(run.run_ref),
          getTrainingRunLogs(run.run_ref, lastLogSeq.current),
          getTrainingRunMetrics(run.run_ref, lastMetricSeq.current),
        ]);
        if (!alive) return;
        onRunChange(nextRun);
        setLoadError(null);
        if (nextLogs.length) {
          lastLogSeq.current = Math.max(lastLogSeq.current, ...nextLogs.map((item) => item.seq));
          setLogs((current) => [...current, ...nextLogs.filter((item) => !current.some((known) => known.seq === item.seq))].sort((a, b) => a.seq - b.seq));
        }
        if (nextMetrics.length) {
          lastMetricSeq.current = Math.max(lastMetricSeq.current, ...nextMetrics.map((item) => item.seq));
          setMetrics((current) => [...current, ...nextMetrics.filter((item) => !current.some((known) => known.seq === item.seq))].sort((a, b) => a.seq - b.seq));
        }
      } catch (caught) {
        if (alive) setLoadError(errorText(caught));
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), activeStatuses.has(run.status) ? 2000 : 8000);
    return () => { alive = false; window.clearInterval(interval); };
  }, [onRunChange, refreshRevision, run.run_ref, run.status]);

  const selectedStage = run.stages.find((stage) => stage.stage_ref === selectedStageRef)
    ?? run.stages.find((stage) => stage.stage_number === run.current_stage_number)
    ?? run.stages[0];
  const effectiveStageRef = selectedStage?.stage_ref ?? "";
  const stageMetrics = useMemo(() => selectedStage ? metrics.filter((item) => !item.stage_ref || item.stage_ref === selectedStage.stage_ref) : metrics, [metrics, selectedStage]);
  const stageLogs = useMemo(() => selectedStage ? logs.filter((item) => !item.stage_ref || item.stage_ref === selectedStage.stage_ref) : logs, [logs, selectedStage]);

  useEffect(() => {
    if (!followLogs) return;
    const viewport = logViewportRef.current;
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [followLogs, stageLogs.length, effectiveStageRef]);

  const stop = useCallback(async () => {
    if (!canStop) return;
    setBusy(true);
    try {
      onRunChange(await stopTrainingRun(run.run_ref, run.state_revision));
      setStopDialogOpen(false);
    } catch (caught) {
      setLoadError(errorText(caught));
    } finally {
      setBusy(false);
    }
  }, [canStop, onRunChange, run.run_ref, run.state_revision]);

  const lossData = chartData(stageMetrics, "loss", "Loss", "#3156c8");
  const learningRateData = chartData(stageMetrics, "learning_rate", "学习率", "#b7791f");
  const gradNormData = chartData(stageMetrics, "grad_norm", "Grad Norm", "#7c3aed");
  const gpuUtilizationData = chartData(stageMetrics, "gpu_utilization_percent", "GPU 利用率", "#0284c7");
  const gpuMemoryData = chartData(stageMetrics, "gpu_memory_mib", "GPU 显存 MiB", "#059669");
  const stageArtifacts = (run.artifacts ?? []).filter((artifact) => !selectedStage || !artifact.stage_ref || artifact.stage_ref === selectedStage.stage_ref);
  const dataset = datasetSplits(run.dataset_snapshot);
  const stepLimited = usesStepLimit(selectedStage);
  const status = runStatusMeta[run.status];
  const parameterEntries = Object.entries(selectedStage?.parameters ?? {});
  const commonParameters = parameterEntries.filter(([key]) => commonParameterNames.has(key));
  const otherParameters = parameterEntries.filter(([key]) => !commonParameterNames.has(key));
  const primaryParameters = commonParameters.length ? commonParameters : parameterEntries.slice(0, 8);
  const secondaryParameters = commonParameters.length ? otherParameters : parameterEntries.slice(8);
  const stageProgress = selectedStage?.progress_percent ?? (selectedStage?.progress ?? 0) * 100;
  const progressKnown = run.total_steps > 0 || run.status === "succeeded";
  const latestLoss = [...stageMetrics].reverse().find((item) => item.loss != null)?.loss ?? run.latest_metric?.loss;
  const latestLearningRate = [...stageMetrics].reverse().find((item) => item.learning_rate != null)?.learning_rate ?? run.latest_metric?.learning_rate;
  const latestGradNorm = [...stageMetrics].reverse().find((item) => item.grad_norm != null)?.grad_norm;

  return <div className="bg-white pb-8">
    <header className="border-b border-console-line pb-4">
      <button type="button" className="inline-flex items-center gap-1.5 text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={onBack}><ArrowLeft className="h-4 w-4" />返回任务列表</button>
      <div className="mt-3 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-semibold tracking-tight text-console-text">{run.family_name}</h2>
            <span className="rounded-md border border-console-line bg-white px-2 py-0.5 font-mono text-xs text-console-text">{run.version_label}</span>
            <StatusTag tone={status.tone}>{status.label}</StatusTag>
            <StatusTag tone="neutral">{run.execution_mode === "real" ? "真实训练" : "模拟训练"}</StatusTag>
            {run.execution_mode === "real" ? <StatusTag tone={run.execution_control_status === "connected" ? "success" : "warning"}>
              {run.execution_control_status === "connected" ? <Wifi className="mr-1 h-3 w-3" /> : <WifiOff className="mr-1 h-3 w-3" />}
              {run.execution_control_status === "connected" ? "Worker 已连接" : run.execution_control_status === "unresolved" ? "状态待确认" : "Worker 连接中断"}
            </StatusTag> : null}
          </div>
          <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-console-muted">
            <span className="font-mono text-console-text">任务 {run.run_ref}</span>
            <span>节点 <span className="text-console-text">{run.server_ref}</span></span>
            <span>GPU <span className="text-console-text">{run.gpu_uuids.length ? `${run.gpu_uuids.length} 张` : "--"}</span></span>
            <span>创建于 <span className="text-console-text">{formatDateTime(run.created_at)}</span></span>
          </div>
        </div>
        {canStop && activeStatuses.has(run.status) ? <ConsoleButton className="shrink-0 border-rose-200 text-rose-700 hover:border-rose-300 hover:bg-rose-50" onClick={() => setStopDialogOpen(true)} disabled={busy}><Square className="h-4 w-4" />{run.status === "stop_requested" ? "正在停止" : "停止训练"}</ConsoleButton> : null}
      </div>
    </header>

    {run.execution_mode === "real" && run.execution_control_status && run.execution_control_status !== "connected" ? <div role="status" className="mt-4 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /><div><p className="font-medium">Worker 状态待确认</p><p className="mt-0.5 text-amber-800">{run.execution_control_status === "unresolved" ? "平台尚未确认训练进程身份，为避免重复占用 GPU，资源租约暂不释放。" : "Worker 连接暂时中断，但训练可能仍在节点继续执行，平台不会将任务误判为失败。"}{run.execution_control_message ? ` ${run.execution_control_message}` : ""}</p>{run.last_execution_heartbeat_at ? <p className="mt-1 text-xs text-amber-700">最后执行心跳：{formatDateTime(run.last_execution_heartbeat_at)}</p> : null}</div></div> : null}

    {loadError ? <div role="alert" className="mt-4 flex items-center justify-between gap-3 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"><span>详情增量更新失败：{loadError}。已有内容不会清空。</span><button type="button" className="inline-flex shrink-0 items-center gap-1 font-medium text-rose-700 hover:underline" onClick={() => setRefreshRevision((value) => value + 1)}><RefreshCw className="h-3.5 w-3.5" />重试</button></div> : null}

    <section className="mt-4 rounded-lg border border-console-line bg-white px-4 py-3">
      <div className="flex flex-wrap items-center gap-2"><p className="text-xs font-medium tracking-wide text-console-muted">本次训练说明</p><span className="text-[11px] text-console-muted">用于记录本次模型版本变化</span></div>
      <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-console-text">{run.version_description?.trim() || "历史任务未填写训练说明。"}</p>
    </section>

    <section className={cn("mt-4 grid overflow-hidden rounded-lg border border-console-line bg-white divide-y divide-console-line sm:grid-cols-2 sm:divide-x sm:divide-y-0", stepLimited ? "lg:grid-cols-5" : "lg:grid-cols-6")} aria-label="训练概览">
      <OverviewStat label="当前阶段" value={selectedStage ? `${selectedStage.stage_number}/${run.stage_count}` : `--/${run.stage_count}`} hint={selectedStage?.stage_name} />
      {!stepLimited ? <OverviewStat label="Epoch" value={`${formatEpoch(selectedStage?.current_epoch ?? run.current_epoch)}/${(selectedStage?.total_epochs ?? run.total_epochs) > 0 ? formatEpoch(selectedStage?.total_epochs ?? run.total_epochs) : "--"}`} /> : null}
      <OverviewStat label="Step" value={`${selectedStage?.current_step ?? run.current_step}/${(selectedStage?.total_steps ?? run.total_steps) || "--"}`} />
      <OverviewStat label="最新 Loss" value={formatNumber(latestLoss)} />
      <OverviewStat label="学习率" value={formatNumber(latestLearningRate, 7)} />
      <OverviewStat label="已运行" value={formatDuration(run.started_at, run.finished_at)} hint={run.started_at ? `开始于 ${formatDateTime(run.started_at)}` : "尚未开始"} />
    </section>
    <div className="mt-3 rounded-lg border border-console-line bg-white px-4 py-3">
      <div className="flex items-center justify-between gap-4 text-xs text-console-muted"><span>总体训练进度</span><span className="font-mono tabular-nums text-console-text">{progressKnown ? `${run.progress_percent.toFixed(1)}%` : "等待总 Step"}</span></div>
      <ProgressBar className="mt-2" value={progressKnown ? run.progress_percent : 0} tone={run.status === "succeeded" ? "success" : run.status === "failed" ? "danger" : "info"} label="总体训练进度" showLabel={false} />
      {!progressKnown ? <p className="mt-2 text-xs text-console-muted">正在等待训练程序上报总 Step。</p> : null}
    </div>

    <section className="mt-6">
      <div className="mb-2 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-console-text">训练阶段</h2><span className="text-xs text-console-muted">当前阶段进度 {stageProgress.toFixed(1)}%</span></div>
      <StageTabs stages={run.stages} selectedStageRef={effectiveStageRef} onSelect={setSelectedStageRef} />
      {selectedStage?.failure_message || selectedStage?.failure?.message ? <div role="alert" className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"><span className="font-medium">{selectedStage.stage_name}失败：</span>{selectedStage.failure_message ?? selectedStage.failure?.message}{selectedStage.failure_code ? <span className="ml-2 font-mono text-xs">({selectedStage.failure_code})</span> : null}</div> : null}
      {run.failure_message && !selectedStage?.failure_message && !selectedStage?.failure?.message ? <div role="alert" className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"><span className="font-medium">任务失败：</span>{run.failure_message}{run.failure_code ? <span className="ml-2 font-mono text-xs">({run.failure_code})</span> : null}</div> : null}
    </section>

    <section className="mt-6">
      <div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-console-text">训练指标</h2><span className="text-xs text-console-muted">{selectedStage?.stage_name} · 横轴优先使用 Step</span></div>
      <div className="grid gap-4 lg:grid-cols-2">
        <MetricCard title="Loss 曲线" value={formatNumber(latestLoss)} data={lossData} />
        <MetricCard title="学习率曲线" value={formatNumber(latestLearningRate, 7)} data={learningRateData} />
      </div>
      <details className="group mt-3 rounded-lg border border-console-line bg-white">
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium text-console-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30"><span>Grad Norm（次要指标）</span><ChevronDown className="h-4 w-4 text-console-muted transition-transform group-open:rotate-180" /></summary>
        <div className="border-t border-console-line p-3"><MetricCard title="Grad Norm" value={formatNumber(latestGradNorm)} data={gradNormData} /></div>
      </details>
    </section>

    <section className="mt-6">
      <div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-console-text">GPU 指标</h2><span className="text-xs text-console-muted">{run.gpu_uuids.length ? `${run.gpu_uuids.length} 张 GPU · 横轴为采样时间或序号` : "未分配 GPU"}</span></div>
      <div className="grid gap-4 lg:grid-cols-2">
        <MetricCard title="GPU 利用率" value={formatNumber([...stageMetrics].reverse().find((item) => item.gpu_utilization_percent != null)?.gpu_utilization_percent, 1) === "--" ? "--" : `${formatNumber([...stageMetrics].reverse().find((item) => item.gpu_utilization_percent != null)?.gpu_utilization_percent, 1)}%`} data={gpuUtilizationData} />
        <MetricCard title="GPU 显存" value={formatNumber([...stageMetrics].reverse().find((item) => item.gpu_memory_mib != null)?.gpu_memory_mib, 0) === "--" ? "--" : `${formatNumber([...stageMetrics].reverse().find((item) => item.gpu_memory_mib != null)?.gpu_memory_mib, 0)} MiB`} data={gpuMemoryData} />
      </div>
    </section>

    <section className="mt-6">
      <div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-console-text">训练日志</h2><span className="text-xs text-console-muted">{activeStatuses.has(run.status) ? "实时增量更新" : "任务日志"} · 敏感参数已遮蔽</span></div>
      <div className="overflow-hidden rounded-lg border border-console-line bg-white">
        <div className="flex items-center justify-between gap-3 border-b border-console-line px-4 py-2.5 text-xs"><div className="flex items-center gap-2"><Terminal className="h-4 w-4 text-console-cyan" /><span className="font-medium text-console-text">{selectedStage?.stage_name}日志</span></div><span className="text-console-muted">{followLogs ? "自动跟随最新" : "已暂停自动滚动"}</span></div>
        <div className="relative">
          <div ref={logViewportRef} aria-label="训练日志" className="h-80 overflow-y-auto bg-slate-950 px-4 py-3 font-mono text-xs leading-6 text-slate-200" onScroll={(event) => { const element = event.currentTarget; setFollowLogs(element.scrollHeight - element.scrollTop - element.clientHeight < 32); }}>
            {stageLogs.length ? stageLogs.map((item) => <p key={item.seq} className={cn(item.level === "error" ? "text-rose-300" : item.level === "warning" ? "text-amber-300" : "text-slate-200")}><span className="mr-2 text-slate-500">{formatClock(item.created_at)}</span><span className="mr-2 inline-block w-12 font-semibold">{item.level.toUpperCase()}</span><span className="whitespace-pre-wrap break-all">{item.message}</span></p>) : <div className="flex h-full items-center justify-center text-slate-500">等待日志…</div>}
          </div>
          {!followLogs ? <button type="button" className="absolute bottom-3 right-3 rounded-full border border-console-line bg-white px-3 py-1.5 text-xs font-medium text-console-text shadow-lg hover:bg-slate-50" onClick={() => { setFollowLogs(true); const viewport = logViewportRef.current; if (viewport) viewport.scrollTop = viewport.scrollHeight; }}>↓ 回到最新</button> : null}
        </div>
      </div>
    </section>

    {run.dataset_snapshot ? <section className="mt-6"><div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-console-text">训练数据</h2><span className="text-xs text-console-muted">托管数据 · 本次任务不可变快照</span></div><div className="grid gap-4 lg:grid-cols-2"><DatasetSplitCard title="训练集" entries={dataset.train} emptyText="本次任务没有训练集记录。" /><DatasetSplitCard title="测试集" entries={dataset.test} emptyText="本次训练未设置测试集。" /></div></section> : null}

    <section className="mt-6">
      <div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-console-text">{selectedStage?.stage_name}参数快照</h2><span className="text-xs text-console-muted">只读 · 本次任务实际使用值</span></div>
      <ConsoleCard className="overflow-hidden p-0 shadow-none">
        <div className="px-4 pt-2"><h3 className="py-2 text-sm font-medium text-console-text">常用参数</h3></div>
        <ParameterTable entries={primaryParameters} />
        {secondaryParameters.length ? <details className="group border-t border-console-line"><summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium text-console-text"><span>其他参数（{secondaryParameters.length}）</span><ChevronDown className="h-4 w-4 text-console-muted transition-transform group-open:rotate-180" /></summary><div className="border-t border-console-line"><ParameterTable entries={secondaryParameters} /></div></details> : null}
      </ConsoleCard>
    </section>

    {selectedStage ? <section className="mt-6"><ArtifactTable stage={selectedStage} artifacts={stageArtifacts} versionModel={run.version_model} /></section> : null}

    <details className="group mt-6 rounded-lg border border-console-line bg-white">
      <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-semibold text-console-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30"><span>技术详情与 RunSpec</span><span className="flex items-center gap-3 text-xs font-normal text-console-muted"><span>Run ID · Worker · GPU · 命令 · 路径</span><ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" /></span></summary>
      <div className="space-y-5 border-t border-console-line px-4 py-4">
        <dl className="grid gap-x-6 gap-y-2 text-sm md:grid-cols-[10rem_1fr_10rem_1fr]">
          <dt className="text-console-muted">Run ID</dt><dd className="break-all font-mono text-xs text-console-text">{run.run_ref}</dd>
          <dt className="text-console-muted">训练节点</dt><dd className="break-all text-console-text">{run.server_ref}</dd>
          <dt className="text-console-muted">GPU UUID</dt><dd className="break-all font-mono text-xs text-console-text">{run.gpu_uuids.join(", ") || "--"}</dd>
          <dt className="text-console-muted">最后执行心跳</dt><dd className="text-console-text">{formatDateTime(run.last_execution_heartbeat_at)}</dd>
          <dt className="text-console-muted">阶段输出目录</dt><dd className="break-all font-mono text-xs text-console-text">{selectedStage?.output_directory ?? "尚未生成"}</dd>
          <dt className="text-console-muted">数据快照</dt><dd className="break-all font-mono text-xs text-console-text">{dataset.snapshotRef ?? "模型自行管理数据"}</dd>
        </dl>
        {selectedStage?.run_spec?.argv?.length ? <div><h3 className="mb-2 text-sm font-medium text-console-text">命令预览</h3><pre className="overflow-x-auto rounded-md border border-console-line bg-slate-50 p-3 font-mono text-xs leading-6 text-slate-800 whitespace-pre-wrap break-all">{selectedStage.run_spec.argv.join(" ")}</pre></div> : null}
        {selectedStage?.run_spec ? <div><h3 className="mb-2 text-sm font-medium text-console-text">RunSpec</h3><pre className="max-h-80 overflow-auto rounded-md border border-console-line bg-slate-50 p-3 font-mono text-xs leading-6 text-slate-800">{JSON.stringify(selectedStage.run_spec, null, 2)}</pre></div> : null}
        <div><h3 className="mb-2 text-sm font-medium text-console-text">审计摘要</h3>{run.audit_events?.length ? <div className="grid gap-2 md:grid-cols-2">{run.audit_events.map((event, index) => <div key={`${event.created_at}-${index}`} className="rounded-md border border-console-line bg-white px-3 py-2 text-sm"><p className="font-medium text-console-text">{event.action}</p><p className="mt-1 text-xs text-console-muted">{event.summary} · {event.created_at}</p></div>)}</div> : <p className="text-sm text-console-muted">暂无审计事件。</p>}</div>
      </div>
    </details>

    <Dialog open={stopDialogOpen} onOpenChange={(open) => { if (!busy) setStopDialogOpen(open); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>确认停止训练？</DialogTitle><DialogDescription>停止训练会终止当前阶段，并取消所有尚未开始的后续阶段。已生成的日志和 checkpoint 会保留。</DialogDescription></DialogHeader>
        <DialogFooter><ConsoleButton onClick={() => setStopDialogOpen(false)} disabled={busy}>取消</ConsoleButton><ConsoleButton className="border-rose-600 bg-rose-600 text-white hover:border-rose-700 hover:bg-rose-700" onClick={() => void stop()} disabled={busy}>{busy ? <RefreshCw className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Square className="h-4 w-4" />}{busy ? "正在停止…" : "确认停止"}</ConsoleButton></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}
