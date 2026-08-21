import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  CircleHelp,
  FileText,
  Cpu,
  HardDrive,
  Info,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Terminal,
  UploadCloud,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import {
  cancelTrainingDatasetTransfer,
  createTrainingModel,
  createTrainingRun,
  getTrainingCapabilities,
  getTrainingRun,
  getTrainingServerResources,
  listTrainingModels,
  listTrainingDatasetReplicas,
  listTrainingDatasetTransfers,
  removeTrainingDatasetReplica,
  listTrainingNodes,
  listTrainingRuns,
  listTrainingServers,
  openTrainingEvents,
  pauseTrainingDatasetTransfer,
  previewTrainingRun,
  retryTrainingDatasetTransfer,
  updateTrainingModel,
  verifyTrainingModel,
} from "../../api/client";
import type { TrainingCapabilities, TrainingDataAccessMode, TrainingDatasetReplica, TrainingDatasetTransfer, TrainingGpuResource, TrainingModel, TrainingNode, TrainingParameterDefinition, TrainingRun, TrainingRunPreview, TrainingServer, TrainingServerResources, TrainingStageInputSource } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { ProgressBar } from "../../components/console/ProgressBar";
import { StatusTag } from "../../components/console/StatusTag";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { Popover, PopoverContent, PopoverTrigger } from "../../components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../../components/ui/tooltip";
import { cn } from "../../lib/utils";
import type { StatusTone, TabItem } from "../console/consoleTypes";
import { MiniChart } from "../console/visuals/MiniChart";
import { ParameterDefinitionEditor, validateParameterDefinitions } from "./ParameterDefinitionEditor";
import { parameterDependencySummary } from "./ParameterDependencyDialog";
import { navilaTrajectoryLaunchTemplate, navilaTrajectoryParameters } from "./navilaTemplate";
import { trainingParameterGroupFor, usedTrainingParameterGroups } from "./parameterGroups";
import { enabledTrainingParameters } from "./parameterAvailability";
import { TrainingNodesPanel } from "./TrainingNodesPanel";
import { TrainingDatasetSelection } from "./TrainingDatasetSelection";
import { actionableTransferStatuses, activeTransferStatuses, TrainingDatasetTransferDialog, TrainingDatasetTransferMonitor, transferLabel } from "./TrainingDatasetTransferDialog";
import { TrainingDataReviewPanel } from "./TrainingDataReviewPanel";
import { TrainingOperationFeedback, type TrainingOperationState } from "./TrainingOperationFeedback";
import { TrainingOperationDialog } from "./TrainingOperationDialog";
import { TrainingRunDetail } from "./TrainingRunDetail";
import { TrainingModelVersions } from "./TrainingModelVersions";

type TrainingTab = "runs" | "new" | "versions" | "data" | "models" | "nodes" | "resources";
const tabs = [
  { id: "runs", label: "训练任务" },
  { id: "versions", label: "模型版本" },
  { id: "data", label: "训练数据" },
  { id: "nodes", label: "训练节点" },
  { id: "resources", label: "服务器资源" },
] satisfies Array<TabItem<TrainingTab>>;

const emptyLaunchTemplate = {
  ...navilaTrajectoryLaunchTemplate,
  domain: "",
  working_directory: "",
  executable: "",
  entrypoint: "",
  output_root: "",
  output_flag: "",
};

function inferLauncherKind(template: { launcher_kind?: "torchrun" | "direct"; executable: string }): "torchrun" | "direct" {
  if (template.launcher_kind) return template.launcher_kind;
  const executableName = template.executable.trim().split(/[\\/]/).pop();
  return executableName === "torchrun" ? "torchrun" : "direct";
}

function trainingServerStatus(server: TrainingServer) {
  if (server.kind === "simulation") return { label: "模拟模式", className: "text-violet-600" };
  if (server.status === "online") return { label: "在线", className: "text-emerald-600" };
  if (server.status === "degraded") return { label: "状态异常", className: "text-amber-600" };
  if (server.status === "offline") return { label: "离线", className: "text-slate-500" };
  if (server.status === "repair_required") return { label: "需要修复", className: "text-rose-600" };
  if (server.status === "disabled") return { label: "已停用", className: "text-slate-500" };
  return { label: "待接入", className: "text-slate-500" };
}

function TrainingServerLabel({ server }: { server: TrainingServer }) {
  const status = trainingServerStatus(server);
  return <span className="flex min-w-0 items-center gap-2"><span className="truncate text-console-text">{server.name}</span><span className={cn("shrink-0 text-xs font-medium", status.className)}>{status.label}</span></span>;
}

function TrainingServerSelect({ servers, value, disabled, ariaLabel, onValueChange }: { servers: TrainingServer[]; value: string; disabled: boolean; ariaLabel: string; onValueChange: (value: string) => void }) {
  const selected = servers.find((server) => server.server_ref === value);
  return <Select value={value || undefined} disabled={disabled} onValueChange={onValueChange}>
    <SelectTrigger aria-label={ariaLabel} className="mt-1 h-9 w-full rounded-md border-console-line bg-console-panel px-2 text-console-text">
      <SelectValue placeholder="尚未登记训练节点">{selected ? <TrainingServerLabel server={selected} /> : value ? <span className="text-rose-600">未找到：{value}</span> : null}</SelectValue>
    </SelectTrigger>
    <SelectContent position="popper" align="start">
      {servers.map((server) => <SelectItem key={server.server_ref} value={server.server_ref}><TrainingServerLabel server={server} /></SelectItem>)}
    </SelectContent>
  </Select>;
}

const activeStatuses = new Set<TrainingRun["status"]>(["queued", "preparing", "running", "stop_requested"]);
const statusMeta: Record<TrainingRun["status"], { label: string; tone: StatusTone }> = {
  queued: { label: "训练中", tone: "purple" }, preparing: { label: "训练中", tone: "purple" }, running: { label: "训练中", tone: "purple" },
  stop_requested: { label: "训练中", tone: "purple" }, succeeded: { label: "已完成", tone: "success" }, failed: { label: "失败", tone: "danger" }, cancelled: { label: "已取消", tone: "neutral" }, lost: { label: "状态丢失", tone: "danger" },
};
type TrainingRunStatusFilter = "all" | "active" | "cancelled" | "failed" | "succeeded" | "lost";
type DatasetTransferNotice = { id: number; message: string; tone: "info" | "success" | "danger" | "neutral" };
const runStatusFilterOptions: Array<{ value: Exclude<TrainingRunStatusFilter, "all">; label: string }> = [
  { value: "active", label: "训练中" },
  { value: "cancelled", label: "已取消" },
  { value: "failed", label: "失败" },
  { value: "succeeded", label: "已完成" },
  { value: "lost", label: "状态丢失" },
];
function matchesRunStatusFilter(status: TrainingRun["status"], filter: TrainingRunStatusFilter) {
  if (filter === "all") return true;
  if (filter === "active") return activeStatuses.has(status);
  return status === filter;
}
const stageStatusLabels = { pending: "等待中", preparing: "准备中", running: "训练中", succeeded: "已完成", failed: "失败", cancelled: "已取消", skipped: "已跳过", lost: "状态丢失" } as const;

function errorText(error: unknown) {
  // Keep this structural so app-level tests can supply a narrow API mock.
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
function formatNumber(value: number | null | undefined, digits = 4) { return value == null ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: digits }); }
function formatResourceBytes(value: number | undefined) {
  if (value == null || !Number.isFinite(value)) return "--";
  const gib = value / 1024 / 1024 / 1024;
  return `${gib >= 10 ? gib.toFixed(0) : gib.toFixed(1)} GiB`;
}
function availablePercent(available: number, total: number) {
  if (!Number.isFinite(available) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.max(0, available / total * 100));
}
function can(capabilities: TrainingCapabilities | null, permission: TrainingCapabilities["permissions"][number]) { return capabilities?.permissions.includes(permission) ?? false; }
function runModelDisplayName(run: TrainingRun) { return `${run.family_name} ${run.version_label}`; }

function currentRunStage(run: TrainingRun) {
  return run.stages.find((stage) => stage.stage_number === run.current_stage_number) ?? run.stages.at(-1);
}

function positiveParameter(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function usesStepLimit(run: TrainingRun) {
  return positiveParameter(currentRunStage(run)?.parameters?.max_steps);
}

function formatEpoch(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "--";
  return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function runProgressSummary(run: TrainingRun) {
  const stage = currentRunStage(run);
  const stagePrefix = `阶段 ${run.current_stage_number ?? "-"}/${run.stage_count}`;
  if (usesStepLimit(run)) {
    return `${stagePrefix} · Step ${stage?.current_step ?? 0}/${stage?.total_steps ?? 0}`;
  }
  const currentEpoch = stage?.current_epoch ?? run.current_epoch;
  const totalEpochs = stage?.total_epochs ?? run.total_epochs;
  return `${stagePrefix} · Epoch ${formatEpoch(currentEpoch)}/${totalEpochs > 0 ? formatEpoch(totalEpochs) : "--"}`;
}

function formatTrainingTime(value: string | null | undefined) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function TrainingSectionTabs({ value, onChange }: { value: TrainingTab; onChange: (value: TrainingTab) => void }) {
  return (
    <div className="min-w-0 flex-1 overflow-x-auto" role="tablist" aria-label="训练平台视图">
      <div className="flex min-w-max gap-7">
        {tabs.map((item) => {
          const active = item.id === value;
          return (
            <button
              key={item.id}
              id={`training-platform-tab-${item.id}`}
              type="button"
              role="tab"
              aria-selected={active}
              aria-controls={`training-platform-panel-${item.id}`}
              className={cn(
                "relative h-11 px-0.5 text-sm font-medium transition-[color] duration-180 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/35 focus-visible:ring-offset-2 motion-reduce:transition-none",
                active ? "text-console-cyan" : "text-console-muted hover:text-console-text",
              )}
              onClick={() => onChange(item.id)}
            >
              {item.label}
              <span
                aria-hidden="true"
                className={cn(
                  "absolute inset-x-0 bottom-0 h-0.5 origin-center bg-console-cyan transition-[transform,opacity] duration-180 motion-reduce:transition-none",
                  active ? "scale-x-100 opacity-100" : "scale-x-0 opacity-0",
                )}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function LoadingCard() { return <ConsoleCard><div className="py-10 text-center text-sm text-console-muted">正在加载训练平台…</div></ConsoleCard>; }

function trainingParameterValueError(parameter: TrainingParameterDefinition, value: string | number | boolean | undefined): string | null {
  if (parameter.type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) return "请输入整数";
    if (!Number.isInteger(value)) return "请输入整数";
    if (!Number.isSafeInteger(value)) return "整数超出安全范围";
  } else if (parameter.type === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) return "请输入有效数值";
  } else if (parameter.type === "string") {
    if (typeof value !== "string") return "请输入文本";
    const minimumLength = parameter.string_min_length ?? 0;
    const maximumLength = parameter.string_max_length ?? 512;
    if (value.length < minimumLength) return `至少输入 ${minimumLength} 个字符`;
    if (value.length > maximumLength) return `最多输入 ${maximumLength} 个字符`;
    return null;
  }
  if (typeof value === "number" && parameter.minimum != null && value < parameter.minimum) return `不能小于 ${parameter.minimum}`;
  if (typeof value === "number" && parameter.maximum != null && value > parameter.maximum) return `不能大于 ${parameter.maximum}`;
  return null;
}

const sensitiveValueMask = "********";

function isUnchangedSensitiveMask(parameter: TrainingParameterDefinition, value: string | number | boolean | undefined) {
  return Boolean(parameter.sensitive) && value === sensitiveValueMask;
}

function ParameterFields({ definitions, values, onChange, enabledParameterKeys, disabled = false }: { definitions: TrainingParameterDefinition[]; values: Record<string, string | number | boolean>; onChange: (key: string, value: string | number | boolean) => void; enabledParameterKeys: Set<string>; disabled?: boolean }) {
  return <TooltipProvider delayDuration={180} skipDelayDuration={80}><div className="grid gap-3 md:grid-cols-2">{definitions.map((parameter) => {
    const conditionMet = enabledParameterKeys.has(parameter.key);
    const parameterDisabled = disabled || !conditionMet;
    const inputClass = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-sm text-console-text focus:border-console-cyan focus:outline-hidden disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-console-muted";
    const cliFlag = parameter.cli_flag || `--${parameter.key}`;
    const description = parameter.description?.trim() || `${parameter.label}，对应命令参数 ${cliFlag}。`;
    const helpId = `training-parameter-help-${parameter.key}`;
    const conditionId = `training-parameter-condition-${parameter.key}`;
    const inputId = `training-parameter-input-${parameter.key}`;
    const conditionSummary = !conditionMet ? parameterDependencySummary(definitions, parameter) : null;
    const currentValue = values[parameter.key];
    const unchangedSensitiveMask = isUnchangedSensitiveMask(parameter, currentValue);
    const valueError = conditionMet && !unchangedSensitiveMask ? trainingParameterValueError(parameter, currentValue) : null;
    const renderedValue = unchangedSensitiveMask || (typeof currentValue === "number" && !Number.isFinite(currentValue)) ? "" : String(currentValue ?? "");
    return <div key={parameter.key} data-parameter-field={parameter.key} className={cn("block text-sm text-console-muted transition-opacity", !conditionMet && "opacity-50")}><span className="flex min-h-5 items-center gap-1.5"><label htmlFor={inputId} className="font-medium text-console-text">{parameter.label}</label><span className="font-mono text-[11px] text-console-muted">{parameter.key}</span><Tooltip><TooltipTrigger asChild><button type="button" className="inline-flex rounded-sm text-console-muted outline-none transition-[color,box-shadow] duration-150 hover:text-console-text focus-visible:ring-2 focus-visible:ring-console-cyan/35 motion-reduce:transition-none" aria-label={`${parameter.label} 参数说明`}><CircleHelp className="h-3.5 w-3.5" aria-hidden="true" /></button></TooltipTrigger><TooltipContent id={helpId} side="top" align="center" sideOffset={7} collisionPadding={12} className="w-72 max-w-[calc(100vw-1.5rem)] whitespace-normal text-left leading-5">{description}</TooltipContent></Tooltip>{parameter.type === "boolean" ? <input id={inputId} aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} className="ml-auto h-4 w-4 accent-console-cyan disabled:cursor-not-allowed" type="checkbox" checked={Boolean(currentValue)} disabled={parameterDisabled} onChange={(event) => onChange(parameter.key, event.target.checked)} /> : null}</span>
      {parameter.type === "boolean" ? null
      : parameter.type === "enum" ? <select id={inputId} aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} className={inputClass} value={renderedValue} disabled={parameterDisabled} onChange={(event) => onChange(parameter.key, event.target.value)}>{unchangedSensitiveMask ? <option value="">已保存敏感默认值（保持不变）</option> : null}{parameter.choices?.map((choice) => <option key={choice.value} value={choice.value}>{choice.value}</option>)}</select>
      : <input id={inputId} aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} aria-invalid={Boolean(valueError)} className={cn(inputClass, valueError && "border-rose-500")} type={parameter.sensitive ? "password" : parameter.type === "string" ? "text" : "number"} step={parameter.type === "number" ? "any" : "1"} min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} maxLength={parameter.type === "string" ? parameter.string_max_length ?? 512 : undefined} value={renderedValue} placeholder={unchangedSensitiveMask ? "已保存敏感默认值，留空沿用" : undefined} disabled={parameterDisabled} onChange={(event) => {
        const raw = event.target.value;
        onChange(parameter.key, parameter.type === "string" ? raw : raw.trim() === "" ? Number.NaN : Number(raw));
      }} />}
      {valueError ? <span role="alert" className="mt-1 block text-xs text-rose-700">{valueError}</span> : null}
      {conditionSummary ? <span id={conditionId} className="mt-1 block text-xs text-console-muted">{conditionSummary}</span> : null}
    </div>;
  })}</div></TooltipProvider>;
}

function ParameterAccordion({ title, hint, definitions, values, onChange, enabledParameterKeys, disabled }: { title: string; hint: string; definitions: TrainingParameterDefinition[]; values: Record<string, string | number | boolean>; onChange: (key: string, value: string | number | boolean) => void; enabledParameterKeys: Set<string>; disabled: boolean }) {
  if (!definitions.length) return null;
  return <details className="group rounded-md border border-console-line bg-console-panel2 px-3 py-2">
    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 py-1">
      <span><span className="text-sm font-medium text-console-text">{title} <span className="font-normal text-console-muted">({definitions.length})</span></span><span className="ml-2 text-xs text-console-muted">{hint}</span></span>
      <ChevronDown className="h-4 w-4 shrink-0 text-console-muted transition-transform group-open:rotate-180" aria-hidden="true" />
    </summary>
    <div className="border-t border-console-line pt-3"><ParameterFields definitions={definitions} values={values} onChange={onChange} enabledParameterKeys={enabledParameterKeys} disabled={disabled} /></div>
  </details>;
}

function GroupedParameterFields({ definitions, values, onChange, enabledParameterKeys, disabled = false }: { definitions: TrainingParameterDefinition[]; values: Record<string, string | number | boolean>; onChange: (key: string, value: string | number | boolean) => void; enabledParameterKeys: Set<string>; disabled?: boolean }) {
  // Legacy `dataset` parameters such as data_mixture are ordinary model hyperparameters.
  const hyperparameterDefinitions = definitions;
  const groups = usedTrainingParameterGroups(hyperparameterDefinitions);
  const commonGroup = groups.find((group) => group.key === "common");
  const common = commonGroup ? hyperparameterDefinitions.filter((parameter) => trainingParameterGroupFor(parameter).key === commonGroup.key) : [];
  const foldedGroups = groups.filter((group) => group.key !== "common").map((group) => ({ ...group, definitions: hyperparameterDefinitions.filter((parameter) => trainingParameterGroupFor(parameter).key === group.key) }));
  return <div className="space-y-4">
    {common.length ? <section aria-label="常用参数"><div className="mb-3"><h3 className="text-sm font-medium text-console-text">常用参数 <span className="font-normal text-console-muted">({common.length})</span></h3><p className="text-xs text-console-muted">高频训练参数保持常驻；依赖条件未满足时会灰显且不可设置。</p></div><ParameterFields definitions={common} values={values} onChange={onChange} enabledParameterKeys={enabledParameterKeys} disabled={disabled} /></section> : null}
    <div className="space-y-2">
      {foldedGroups.map((group) => <ParameterAccordion key={group.key} title={group.label} hint={group.hint} definitions={group.definitions} values={values} onChange={onChange} enabledParameterKeys={enabledParameterKeys} disabled={disabled} />)}
    </div>
  </div>;
}

function GpuPicker({ gpus, selected, onChange, disabled }: { gpus: TrainingGpuResource[]; selected: string[]; onChange: (ids: string[]) => void; disabled?: boolean }) {
  return <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">{gpus.map((gpu) => {
    const unavailable = gpu.externally_occupied || Boolean(gpu.lease_run_ref);
    const checked = selected.includes(gpu.gpu_uuid);
    const utilizationPercent = Math.min(100, Math.max(0, gpu.utilization_percent));
    const memoryPercent = gpu.total_memory_mib > 0 ? Math.min(100, Math.max(0, (gpu.used_memory_mib / gpu.total_memory_mib) * 100)) : 0;
    return <label key={gpu.gpu_uuid} className={cn("rounded-md border p-3 outline-none transition-[border-color,background-color,box-shadow,opacity] duration-150 focus-within:ring-2 focus-within:ring-console-cyan/30 motion-reduce:transition-none", checked ? "border-console-cyan bg-sky-50" : "border-console-line bg-console-panel2 hover:border-console-cyan/40", unavailable && "opacity-55")}>
      <div className="flex items-center justify-between gap-2"><span className="font-medium text-console-text">GPU {gpu.index}</span><input aria-label={`选择 GPU ${gpu.index}`} type="checkbox" checked={checked} disabled={disabled || unavailable} onChange={() => onChange(checked ? selected.filter((id) => id !== gpu.gpu_uuid) : [...selected, gpu.gpu_uuid])} /></div>
      <p className="mt-1 truncate text-xs text-console-muted" title={gpu.name}>{gpu.name} · {gpu.temperature_c}°C</p>
      <div className="mt-2 space-y-2">
        <div><div className="flex items-center justify-between gap-2 text-[11px] text-console-muted"><span>GPU 利用率</span><span className="tabular-nums">{utilizationPercent.toFixed(0)}%</span></div><ProgressBar className="mt-1" value={utilizationPercent} tone={utilizationPercent >= 90 ? "danger" : utilizationPercent >= 70 ? "warning" : "purple"} label={`GPU ${gpu.index} 利用率`} showLabel={false} /></div>
        <div><div className="flex items-center justify-between gap-2 text-[11px] text-console-muted"><span>显存占用</span><span className="tabular-nums">{Math.round(gpu.used_memory_mib / 1024)}/{Math.round(gpu.total_memory_mib / 1024)} GiB</span></div><ProgressBar className="mt-1" value={memoryPercent} tone={memoryPercent >= 90 ? "danger" : memoryPercent >= 70 ? "warning" : "info"} label={`GPU ${gpu.index} 显存占用`} showLabel={false} /></div>
      </div>
      <p className={cn("mt-2 text-xs font-medium", unavailable ? "text-amber-700" : "text-emerald-700")}>{unavailable ? (gpu.lease_run_ref ? "平台已租用" : "检测到外部占用") : "平台未租用"}</p>
    </label>;
  })}</div>;
}

function NewRunResourcePanel({ resources, selectedGpuIds }: { resources: TrainingServerResources | undefined; selectedGpuIds: string[] }) {
  if (!resources) return <aside className="rounded-xl border border-dashed border-console-line bg-console-panel p-4" aria-label="训练资源概览"><div className="flex items-center gap-2"><Server className="h-4 w-4 text-console-cyan" aria-hidden="true" /><h3 className="font-semibold text-console-text">资源概览</h3></div><p className="mt-3 text-sm text-console-muted">正在等待 Worker 上报资源。</p></aside>;
  const selectedGpus = resources.gpus.filter((gpu) => selectedGpuIds.includes(gpu.gpu_uuid));
  const memoryPercent = resources.memory ? availablePercent(resources.memory.available_bytes, resources.memory.total_bytes) : 0;
  const selectedMemoryGib = selectedGpus.reduce((total, gpu) => total + gpu.total_memory_mib, 0) / 1024;
  const largestDisk = [...(resources.disks ?? [])].sort((left, right) => right.available_bytes - left.available_bytes)[0];
  return <aside className="rounded-xl border border-console-line bg-console-panel p-4 shadow-[0_8px_24px_rgba(31,42,68,0.05)]" aria-label="训练资源概览">
    <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2"><Server className="h-4 w-4 text-console-cyan" aria-hidden="true" /><h3 className="font-semibold text-console-text">资源概览</h3></div><span className="text-[11px] text-console-muted">{formatTrainingTime(resources.sampled_at)} 采样</span></div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
      <div className="rounded-md bg-console-panel2 px-3 py-2.5"><div className="flex items-center justify-between gap-3"><span className="flex items-center gap-2 text-xs text-console-muted"><Cpu className="h-3.5 w-3.5" aria-hidden="true" />CPU</span><b className="text-sm text-console-text">{resources.cpu ? `${resources.cpu.logical_cores} 核` : "--"}</b></div><p className="mt-1 text-xs text-console-muted">1 分钟负载 {resources.cpu?.load_1m?.toFixed(2) ?? "--"}</p></div>
      <div className="rounded-md bg-console-panel2 px-3 py-2.5"><div className="flex items-center justify-between gap-2 text-xs text-console-muted"><span className="flex items-center gap-2"><Activity className="h-3.5 w-3.5" aria-hidden="true" />可用内存</span><b className="text-sm text-console-text">{memoryPercent.toFixed(0)}%</b></div><div className="mt-1 flex items-center justify-between gap-3"><p className="font-semibold text-console-text">{formatResourceBytes(resources.memory?.available_bytes)}</p><span className="text-[11px] text-console-muted">可用</span></div><ProgressBar className="mt-1.5" value={memoryPercent} tone={memoryPercent < 15 ? "danger" : memoryPercent < 30 ? "warning" : "success"} label="可用内存" /></div>
      <div className="rounded-md bg-console-panel2 px-3 py-2.5"><div className="flex items-center justify-between gap-3 text-xs text-console-muted"><span>已选 GPU</span><b className="text-sm text-console-text">{selectedGpus.length}/{resources.gpus.length}</b></div><p className="mt-1 font-semibold text-console-text">{selectedGpus.length ? `${selectedMemoryGib.toFixed(0)} GiB 显存` : "尚未选择"}</p></div>
      <div className="rounded-md bg-console-panel2 px-3 py-2.5"><div className="flex items-center justify-between gap-3"><span className="flex min-w-0 items-center gap-2 text-xs text-console-muted"><HardDrive className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />磁盘最大可用空间</span><b className="shrink-0 text-sm text-console-text">{formatResourceBytes(largestDisk?.available_bytes)}</b></div><p className="mt-1 truncate font-mono text-[11px] text-console-muted" title={largestDisk?.mount}>{largestDisk?.mount ?? "Worker 未上报"}</p></div>
    </div>
  </aside>;
}

type NewRunStage = { parameters: Record<string, string | number | boolean>; stage_input_source: TrainingStageInputSource };
const stageNames = ["第一阶段", "第二阶段", "第三阶段", "第四阶段", "第五阶段", "第六阶段", "第七阶段", "第八阶段", "第九阶段", "第十阶段"];

function NewRunPanel({ models, servers, nodes, resourcesByServer, capabilities, canCreate, datasetEventRevision, datasetTransfers, preferredFamilyRef, onCancel, onCreated, onTransfersCreated, onCreateModel, onEditModel }: { models: TrainingModel[]; servers: TrainingServer[]; nodes: TrainingNode[]; resourcesByServer: Record<string, TrainingServerResources>; capabilities: TrainingCapabilities | null; canCreate: boolean; datasetEventRevision: number; datasetTransfers: TrainingDatasetTransfer[]; preferredFamilyRef: string | null; onCancel: () => void; onCreated: (run: TrainingRun) => void; onTransfersCreated: (transfers: TrainingDatasetTransfer[]) => void; onCreateModel: () => void; onEditModel: (familyRef: string) => void }) {
  const availableModels = useMemo(() => models.filter((item) => item.status !== "disabled"), [models]);
  const duplicateFamilyNames = useMemo(() => new Set(availableModels.filter((item, index, all) => all.findIndex((candidate) => candidate.family_name === item.family_name) !== index).map((item) => item.family_name)), [availableModels]);
  const [familyRef, setFamilyRef] = useState("");
  const [serverRef, setServerRef] = useState("");
  const [gpuIds, setGpuIds] = useState<string[]>([]);
  const [stages, setStages] = useState<NewRunStage[]>([]);
  const [activeStageIndex, setActiveStageIndex] = useState(0);
  const [preview, setPreview] = useState<TrainingRunPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [operation, setOperation] = useState<TrainingOperationState | null>(null);
  const [replicas, setReplicas] = useState<TrainingDatasetReplica[]>([]);
  const [datasetError, setDatasetError] = useState<string | null>(null);
  const [transferOpen, setTransferOpen] = useState(false);
  const [datasetManagementOpen, setDatasetManagementOpen] = useState(false);
  const [trainReplicaRefs, setTrainReplicaRefs] = useState<string[]>([]);
  const [testReplicaRefs, setTestReplicaRefs] = useState<string[]>([]);
  const [testSetEnabled, setTestSetEnabled] = useState(false);
  const [versionDescription, setVersionDescription] = useState("");
  const selectedFamilyRef = availableModels.some((item) => item.family_ref === familyRef) ? familyRef : availableModels[0]?.family_ref ?? "";
  const model = availableModels.find((item) => item.family_ref === selectedFamilyRef);
  const dataAccessMode = model?.configuration?.data_access_mode ?? model?.data_access_mode ?? "self_managed";
  const managedData = dataAccessMode === "datapilot_managed";
  const definitions = model?.configuration?.parameter_definitions ?? [];
  const stageInput = definitions.find((item) => item.semantic_role === "stage_input");
  const modelServerRef = model?.configuration?.launch_template?.server_ref;
  const selectedServer = modelServerRef || serverRef || servers[0]?.server_ref || "";
  const selectedServerRecord = servers.find((server) => server.server_ref === selectedServer);
  const simulationTarget = selectedServerRecord?.kind === "simulation";
  const selectedNode = nodes.find((node) => node.node_ref === selectedServer);
  const workerFeatures = selectedNode?.capabilities?.worker_features;
  const workerSupportsExecution = selectedNode?.capabilities?.training_execution_v1 === true
    || (Array.isArray(workerFeatures) && workerFeatures.includes("training_execution_v1"));
  const realExecutionEnabled = capabilities?.real_execution_enabled === true;
  const realStartDisabledReason = simulationTarget ? null : !canCreate ? "当前账号没有创建训练任务的权限。"
    : !realExecutionEnabled ? (capabilities?.real_execution_disabled_reason || "真实训练尚未启用。")
      : model?.status !== "verified" ? "请先完成当前模型配置验证。"
        : selectedNode?.status !== "online" ? "训练节点当前不在线，暂不能启动真实训练。"
          : !workerSupportsExecution ? "训练节点 Worker 尚未升级到真实训练执行能力。"
            : null;
  const executionMode = simulationTarget ? "simulation" as const : "real" as const;
  const selectedResources = resourcesByServer[selectedServer];
  const selectedServerTransfers = datasetTransfers.filter((transfer) => transfer.node_ref === selectedServer);
  const gpus = selectedResources?.gpus ?? [];
  const defaultValues = useMemo(() => Object.fromEntries(definitions.map((item) => [item.key, item.default])), [definitions]);

  const refreshReplicas = useCallback(async () => {
    if (!selectedServer || !managedData) { setReplicas([]); return; }
    setDatasetError(null);
    try {
      const next = (await listTrainingDatasetReplicas(selectedServer)).filter((item) => item.status === "ready");
      setReplicas(next);
      const readyRefs = new Set(next.map((item) => item.replica_ref));
      setTrainReplicaRefs((current) => current.filter((ref) => readyRefs.has(ref)));
      setTestReplicaRefs((current) => current.filter((ref) => readyRefs.has(ref)));
    } catch (caught) {
      setDatasetError(errorText(caught));
    }
  }, [managedData, selectedServer]);

  useEffect(() => { if (selectedFamilyRef !== familyRef) setFamilyRef(selectedFamilyRef); }, [familyRef, selectedFamilyRef]);
  useEffect(() => {
    if (preferredFamilyRef && availableModels.some((item) => item.family_ref === preferredFamilyRef)) setFamilyRef(preferredFamilyRef);
  }, [availableModels, preferredFamilyRef]);
  useEffect(() => {
    const nextServerRef = modelServerRef || servers[0]?.server_ref || "";
    setServerRef(nextServerRef);
    setGpuIds([]);
    setStages([{ parameters: defaultValues, stage_input_source: "manual" }]);
    setActiveStageIndex(0);
    setPreview(null);
    setTrainReplicaRefs([]);
    setTestReplicaRefs([]);
    setTestSetEnabled(false);
    setDatasetManagementOpen(false);
    setVersionDescription("");
  }, [selectedFamilyRef, model?.edit_revision, modelServerRef]);

  useEffect(() => { void refreshReplicas(); }, [datasetEventRevision, refreshReplicas]);

  const invalidatePreview = () => setPreview(null);
  const updateStage = (index: number, update: (stage: NewRunStage) => NewRunStage) => {
    setStages((current) => current.map((stage, stageIndex) => stageIndex === index ? update(stage) : stage));
    invalidatePreview();
  };
  const addStage = () => {
    if (stages.length >= 10) return;
    const previous = stages.at(-1) ?? { parameters: defaultValues, stage_input_source: "manual" as const };
    setStages((current) => [...current, { parameters: structuredClone(previous.parameters), stage_input_source: stageInput ? "previous_stage_output" : "manual" }]);
    setActiveStageIndex(stages.length);
    invalidatePreview();
  };
  const removeStage = (index: number) => {
    if (index === 0 || !window.confirm(`确定删除${stageNames[index]}吗？后续阶段将自动重新编号，已生成的预览会失效。`)) return;
    setStages((current) => current.filter((_, stageIndex) => stageIndex !== index));
    setActiveStageIndex((current) => Math.max(0, Math.min(current >= index ? current - 1 : current, stages.length - 2)));
    invalidatePreview();
  };
  const stageValidation = stages.map((stage, index) => {
    const enabled = enabledTrainingParameters(definitions, stage.parameters);
    const errors = enabled.filter((parameter) => !(index > 0 && parameter.key === stageInput?.key && stage.stage_input_source === "previous_stage_output") && !isUnchangedSensitiveMask(parameter, stage.parameters[parameter.key])).map((parameter) => trainingParameterValueError(parameter, stage.parameters[parameter.key])).filter(Boolean);
    return { enabled, enabledKeys: new Set(enabled.map((item) => item.key)), errors };
  });
  const hasParameterErrors = stageValidation.some((item) => item.errors.length > 0);
  const payloadBase = () => ({
    family_ref: selectedFamilyRef,
    server_ref: selectedServer,
    gpu_uuids: gpuIds,
    ...(versionDescription.trim() ? { version_description: versionDescription.trim() } : {}),
    ...(managedData ? { dataset_selection: { train_replica_refs: trainReplicaRefs, test_replica_refs: testSetEnabled ? testReplicaRefs : [] } } : {}),
    stages: stages.map((stage, index) => ({
      stage_input_source: index === 0 ? "manual" as const : stage.stage_input_source,
      parameters: Object.fromEntries(stageValidation[index].enabled.filter((item) => !isUnchangedSensitiveMask(item, stage.parameters[item.key])).map((item) => [item.key, stage.parameters[item.key] ?? item.default])),
    })),
  });
  const previewPayload = () => ({ ...payloadBase(), execution_mode: executionMode });
  const runPayload = () => ({ ...payloadBase(), execution_mode: executionMode });
  const doPreview = async () => {
    if (!selectedFamilyRef || !selectedServer || !gpuIds.length) return setMessage("请选择模型、服务器和至少一张可用 GPU。");
    if (hasParameterErrors) return setMessage("请先修正各训练阶段中标红的参数。");
    if (managedData && !trainReplicaRefs.length) return setMessage("请至少选择一个训练集日期。");
    setBusy(true); setMessage(null); setOperation({ status: "loading", title: "正在生成训练预览", detail: `逐项校验 ${stages.length} 个训练阶段并生成安全 argv。`, steps: ["校验资源", "校验参数", "生成 RunSpec"], activeStep: 1 });
    try { setPreview(await previewTrainingRun(previewPayload())); setOperation({ status: "success", title: "训练预览已生成", detail: "请核对各阶段命令和输出目录；预览不会启动任何进程。" }); } catch (error) { const detail = errorText(error); setMessage(detail); setOperation({ status: "error", title: "生成训练预览失败", detail }); } finally { setBusy(false); }
  };
  const start = async () => {
    if (!preview || realStartDisabledReason) return;
    if (!versionDescription.trim()) { setMessage("请填写本次训练说明后再启动训练。"); return; }
    setBusy(true); setMessage(null); setOperation({ status: "loading", title: "正在创建训练任务", detail: `创建一个模型版本并准备顺序执行 ${stages.length} 个阶段。`, steps: ["校验资源", "创建模型版本", "提交训练任务"], activeStep: 1 });
    try { onCreated(await createTrainingRun(runPayload())); } catch (error) { const detail = errorText(error); setMessage(detail); setOperation({ status: "error", title: "创建训练任务失败", detail }); } finally { setBusy(false); }
  };
  const toggleReplica = (replicaRef: string, selected: boolean) => {
    setTrainReplicaRefs((current) => selected ? [...current.filter((ref) => ref !== replicaRef), replicaRef] : current.filter((ref) => ref !== replicaRef));
    setTestReplicaRefs((current) => current.filter((ref) => ref !== replicaRef));
    invalidatePreview();
  };
  const setReplicaSplit = (replicaRef: string, split: "train" | "test") => {
    setTrainReplicaRefs((current) => split === "train" ? [...current.filter((ref) => ref !== replicaRef), replicaRef] : current.filter((ref) => ref !== replicaRef));
    setTestReplicaRefs((current) => split === "test" ? [...current.filter((ref) => ref !== replicaRef), replicaRef] : current.filter((ref) => ref !== replicaRef));
    invalidatePreview();
  };
  const removeReplica = async (replica: TrainingDatasetReplica): Promise<string | null> => {
    setDatasetError(null);
    try {
      await removeTrainingDatasetReplica(replica.replica_ref);
      setReplicas((current) => current.filter((item) => item.replica_ref !== replica.replica_ref));
      toggleReplica(replica.replica_ref, false);
      return null;
    } catch (caught) {
      const detail = errorText(caught);
      setDatasetError(detail);
      return detail;
    }
  };
  const activeStage = stages[activeStageIndex];
  const activeValidation = stageValidation[activeStageIndex];
  const previousOutputPreview = activeStageIndex > 0 ? preview?.stages[activeStageIndex - 1]?.output_directory : null;
  const hasTrainingDraftChanges = Boolean(
    gpuIds.length
    || trainReplicaRefs.length
    || testReplicaRefs.length
    || versionDescription.trim()
    || preview
    || stages.length > 1
    || stages.some((stage) => Object.entries(defaultValues).some(([key, value]) => stage.parameters[key] !== value)),
  );
  const openModelConfiguration = (mode: "create" | "edit") => {
    if (hasTrainingDraftChanges && !window.confirm("当前训练任务已有未保存的设置。保存模型配置后，GPU、数据、阶段参数和旧预览会按最新模型配置重新加载。是否继续？")) return;
    if (mode === "edit" && selectedFamilyRef) onEditModel(selectedFamilyRef);
    else onCreateModel();
  };

  return <div className="space-y-4">
    <header className="flex flex-col gap-3 border-b border-console-line pb-5 lg:flex-row lg:items-end lg:justify-between"><div><button type="button" className="mb-2 text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={onCancel}>← 返回训练任务</button><h2 className="text-xl font-semibold text-console-text">新建训练任务</h2><p className="mt-1 text-sm text-console-muted">一次任务生成一个模型版本；所有训练阶段共用本次选择的数据。</p></div><ol className="flex max-w-full flex-wrap items-center gap-2 text-xs text-console-muted lg:justify-end" aria-label="创建训练步骤">{['模型与资源', '训练数据', '阶段参数', '说明与预览'].map((label, index) => <li key={label} className="flex items-center gap-2"><span className={cn("flex h-6 w-6 items-center justify-center rounded-full border text-[11px] font-semibold", index === 0 ? "border-console-cyan bg-blue-50 text-console-cyan" : "border-console-line bg-console-panel text-console-muted")}>{index + 1}</span><span>{label}</span>{index < 3 ? <ArrowRight className="h-3.5 w-3.5 text-slate-300" aria-hidden="true" /> : null}</li>)}</ol></header>
    {simulationTarget ? <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>当前选择模拟节点，只会创建可重复的多阶段模拟任务。</span></div> : realExecutionEnabled ? <div className="flex items-start gap-3 rounded-md border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-800"><Play className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>真实训练会在 Worker 上启动，并占用所选 GPU 直到全部阶段结束、失败或停止。</span></div> : <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>开发预览模式：{capabilities?.real_execution_disabled_reason || "真实训练未启用。当前可生成预览，但不会启动进程。"}</span></div>}
    <TrainingOperationFeedback operation={operation} />
    <ConsoleCard className="shadow-none"><div className="mb-4 flex flex-wrap items-start justify-between gap-3"><div className="flex items-center gap-2"><Play className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">1. 选择模型和资源</h2><p className="text-sm text-console-muted">模型族的当前训练定义决定节点、入口和可设置参数。</p></div></div><ConsoleButton variant="ghost" onClick={() => openModelConfiguration("create")}><Plus className="h-4 w-4" />登记新模型族</ConsoleButton></div><div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_18rem]"><div><div className="grid gap-3 md:grid-cols-2"><label className="text-sm text-console-muted">模型族<select aria-label="模型族" className="mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={selectedFamilyRef} onChange={(event) => { if (event.target.value === "__register_model_family__") { openModelConfiguration("create"); return; } setFamilyRef(event.target.value); setGpuIds([]); invalidatePreview(); }}>{availableModels.map((item) => <option key={item.family_ref} value={item.family_ref}>{`${item.family_name}${duplicateFamilyNames.has(item.family_name) ? ` · ${item.family_ref.slice(-6)}` : ""}（可用 ${item.available_version_count ?? item.trained_version_count} 个版本）`}</option>)}<option disabled>──────────</option><option value="__register_model_family__">＋ 登记新模型族</option></select></label><label className="text-sm text-console-muted">训练节点<TrainingServerSelect ariaLabel="训练节点" servers={selectedServerRecord ? [selectedServerRecord] : []} value={selectedServer} disabled onValueChange={() => undefined} /></label></div>{!simulationTarget && model?.status !== "verified" ? <p className="mt-3 text-xs text-amber-700">当前模型配置尚未验证，真实训练不可启动。</p> : null}<h3 className="mb-2 mt-5 text-sm font-medium text-console-text">选择 GPU（{gpuIds.length} 张）</h3><GpuPicker gpus={gpus} selected={gpuIds} onChange={(ids) => { setGpuIds(ids); invalidatePreview(); }} disabled={!canCreate} />{!simulationTarget ? <p className="mt-3 text-xs text-console-muted">{realExecutionEnabled ? "开始训练前，Worker 会再次核对 GPU、模型配置、数据副本和输出目录。" : "GPU 选择仅用于生成真实训练预览；当前不会占用或租用 GPU。"}</p> : null}</div><NewRunResourcePanel resources={selectedResources} selectedGpuIds={gpuIds} /></div></ConsoleCard>
    <ConsoleCard><div className="mb-4 flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold text-console-text">2. 选择训练数据</h2><p className="text-sm text-console-muted">所有训练阶段共用同一份日期划分；测试集可选，本阶段只保存供后续测试使用。</p></div>{managedData ? <div className="flex items-center gap-3"><button type="button" disabled={!canCreate || !selectedServer} className="text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30 disabled:cursor-not-allowed disabled:opacity-50" onClick={() => setDatasetManagementOpen(true)}>管理节点数据</button><ConsoleButton variant="ghost" disabled={!canCreate || !selectedServer} onClick={() => setTransferOpen(true)}><UploadCloud className="h-4 w-4" />从中心服务器传输数据</ConsoleButton></div> : null}</div>{managedData ? <>{datasetError ? <p role="alert" className="mb-3 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{datasetError}</p> : null}<label className="mb-3 inline-flex items-center gap-2 text-sm text-console-text"><input type="checkbox" className="accent-console-cyan" checked={testSetEnabled} onChange={(event) => { const enabled = event.target.checked; setTestSetEnabled(enabled); if (!enabled) { setTrainReplicaRefs((current) => [...new Set([...current, ...testReplicaRefs])]); setTestReplicaRefs([]); } invalidatePreview(); }} />设置测试集</label><TrainingDatasetSelection replicas={replicas} trainReplicaRefs={trainReplicaRefs} testReplicaRefs={testReplicaRefs} testSetEnabled={testSetEnabled} canManage={canCreate && Boolean(selectedServer)} managementOpen={datasetManagementOpen} onManagementOpenChange={setDatasetManagementOpen} onToggleReplica={toggleReplica} onSetReplicaSplit={setReplicaSplit} onRemoveReplica={removeReplica} /></> : <div className="rounded-md border border-console-line bg-console-panel2 px-4 py-3"><p className="text-sm font-medium text-console-text">模型自行管理数据</p><p className="mt-1 text-sm text-console-muted">平台不会传输或划分数据，也不会向命令注入 dataset manifest。</p></div>}</ConsoleCard>
    <ConsoleCard><div className="mb-4 flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold text-console-text">3. 配置训练阶段</h2><p className="text-sm text-console-muted">新增阶段会复制前一阶段全部参数；每个阶段仍使用同一份参数定义。</p></div><div className="flex flex-wrap items-center gap-2"><ConsoleButton variant="ghost" disabled={!selectedFamilyRef} onClick={() => openModelConfiguration("edit")}>修改参数配置</ConsoleButton><ConsoleButton variant="ghost" disabled={!canCreate || stages.length >= 10} onClick={addStage}><Plus className="h-4 w-4" />添加训练阶段</ConsoleButton></div></div>{!stageInput && stages.length > 1 ? <div className="mb-4 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-800">模型族未登记“阶段输入参数”，各阶段的加载路径需要手动填写。</div> : null}<div className="mb-4 flex flex-wrap gap-2" role="tablist" aria-label="训练阶段">{stages.map((_, index) => { const active = activeStageIndex === index; return <div key={index} className={cn("inline-flex min-h-8 max-w-full items-center rounded-md border", active ? "border-console-cyan/45 bg-sky-50/60" : "border-console-line bg-console-panel")}><button type="button" role="tab" aria-selected={active} className={cn("px-2.5 py-1.5 text-sm font-medium", active ? "text-console-cyan" : "text-console-muted hover:text-console-text")} onClick={() => setActiveStageIndex(index)}>{stageNames[index]}</button>{index > 0 ? <button type="button" aria-label={`删除${stageNames[index]}`} className="mr-1.5 shrink-0 rounded p-0.5 text-console-muted transition-colors hover:bg-white hover:text-rose-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => removeStage(index)}><X className="h-3.5 w-3.5" /></button> : null}</div>; })}</div>{activeStage && activeValidation ? <section aria-label={`${stageNames[activeStageIndex]}参数`} className="rounded-md border border-console-line p-4"><div className="mb-4 flex items-center justify-between"><div><h3 className="font-medium text-console-text">{stageNames[activeStageIndex]}</h3><p className="text-xs text-console-muted">阶段 {activeStageIndex + 1} / {stages.length}</p></div>{activeValidation.errors.length ? <StatusTag tone="danger">{activeValidation.errors.length} 项待修正</StatusTag> : <StatusTag tone="success">参数有效</StatusTag>}</div>{stageInput && activeStageIndex > 0 ? <div className="mb-4 rounded-md border border-console-line bg-console-panel2 p-3"><p className="text-sm font-medium text-console-text">{stageInput.label} <span className="font-mono text-xs text-console-muted">{stageInput.key}</span></p><div className="mt-2 flex flex-wrap gap-4 text-sm"><label><input type="radio" className="mr-2 accent-console-cyan" checked={activeStage.stage_input_source === "previous_stage_output"} onChange={() => updateStage(activeStageIndex, (stage) => ({ ...stage, stage_input_source: "previous_stage_output" }))} />使用上一阶段输出目录</label><label><input type="radio" className="mr-2 accent-console-cyan" checked={activeStage.stage_input_source === "manual"} onChange={() => updateStage(activeStageIndex, (stage) => ({ ...stage, stage_input_source: "manual" }))} />手动填写</label></div>{activeStage.stage_input_source === "previous_stage_output" ? <input aria-label="上一阶段输出目录" className="mt-2 h-9 w-full cursor-not-allowed rounded-md border border-console-line bg-slate-100 px-2 font-mono text-sm text-console-muted" disabled value={previousOutputPreview ?? "生成预览后显示上一阶段输出目录"} /> : null}</div> : null}<GroupedParameterFields definitions={activeStage.stage_input_source === "previous_stage_output" && activeStageIndex > 0 ? definitions.filter((item) => item.key !== stageInput?.key) : definitions} values={activeStage.parameters} onChange={(key, value) => updateStage(activeStageIndex, (stage) => ({ ...stage, parameters: { ...stage.parameters, [key]: value } }))} enabledParameterKeys={activeValidation.enabledKeys} disabled={!canCreate} /></section> : null}</ConsoleCard>
      <ConsoleCard><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-console-text">4. 填写说明并预览</h2><p className="text-sm text-console-muted">预览不创建模型版本；启动训练前必须填写本次版本的变化说明。</p></div><ConsoleButton variant="ghost" aria-busy={busy} onClick={() => void doPreview()} disabled={!canCreate || busy || !models.length || hasParameterErrors || (managedData && !trainReplicaRefs.length)}>{busy ? <RefreshCw className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <RefreshCw className="h-4 w-4" />}{busy ? "正在生成…" : "生成预览"}</ConsoleButton></div><label className="mt-4 block text-sm text-console-muted">本次训练说明（必填）<span className="float-right text-xs">{versionDescription.length}/500</span><textarea aria-label="本次训练说明" className="mt-1 min-h-24 w-full rounded-md border border-console-line bg-console-panel p-3 text-sm text-console-text placeholder:text-slate-400 focus:border-console-cyan focus:outline-hidden" value={versionDescription} maxLength={500} placeholder="说明本次模型或训练方案的主要变化" onChange={(event) => { setVersionDescription(event.target.value); setPreview(null); setMessage(null); }} /><span className="mt-1 block text-xs leading-5">本次训练会生成一个新的模型版本，该说明用于记录本次模型或训练方案的主要变化。</span></label>{!versionDescription.trim() ? <p className="mt-2 text-xs text-amber-700">可以先生成预览；填写说明后才能启动训练。</p> : null}{message ? <p role="alert" className="mt-3 text-sm text-rose-700">{message}</p> : null}{preview ? <div className="mt-4 space-y-2">{preview.stages.map((stage) => <details key={stage.stage_number} className="rounded-md border border-console-line bg-console-panel2 px-3 py-2" open={stage.stage_number === 1}><summary className="cursor-pointer text-sm font-medium text-console-text">{stage.stage_name} · {stage.output_directory}</summary><div className="mt-3 rounded-md bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100 break-all">{stage.command_preview}</div><div className="mt-3 grid gap-2 md:grid-cols-3"><span className="text-sm text-console-muted">nproc_per_node：<b className="text-console-text">{stage.run_spec.nproc_per_node}</b></span><span className="text-sm text-console-muted">GPU：<b className="text-console-text">{stage.run_spec.gpu_uuids.length}</b></span><span className="text-sm text-console-muted">端口：<b className="text-console-text">{stage.run_spec.master_port ?? "不需要"}</b></span></div>{stage.preflight.map((item, index) => <p key={index} className={cn("mt-2 text-sm", item.ok ? "text-emerald-700" : "text-rose-700")}>{item.ok ? "✓" : "×"} {item.message}</p>)}</details>)}</div> : null}</ConsoleCard>
    <ConsoleCard><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-console-text">{simulationTarget ? "创建模型版本并启动模拟训练" : "创建模型版本并开始训练"}</h2><p className="text-sm text-console-muted">{simulationTarget ? `将创建一个模型版本，并顺序执行 ${stages.length} 个模拟阶段。` : `将创建一个模型版本，并由 Worker 顺序执行 ${stages.length} 个训练阶段；GPU 与端口会覆盖整个任务周期。`}</p>{!simulationTarget && realStartDisabledReason ? <p className="mt-2 text-xs text-amber-700">{realStartDisabledReason}</p> : null}</div><ConsoleButton variant="primary" onClick={() => void start()} disabled={!canCreate || busy || !preview || !versionDescription.trim() || Boolean(realStartDisabledReason)}><Play className="h-4 w-4" />{simulationTarget ? "启动模拟训练" : realExecutionEnabled ? "开始训练" : "真实训练未启用"}</ConsoleButton></div></ConsoleCard>
    {managedData ? <TrainingDatasetTransferDialog open={transferOpen} nodeRef={selectedServer} unavailableReleaseRefs={new Set([...replicas.map((item) => item.release_ref), ...selectedServerTransfers.filter((item) => activeTransferStatuses.has(item.status) || actionableTransferStatuses.has(item.status)).map((item) => item.release_ref)])} onOpenChange={setTransferOpen} onTransfersCreated={onTransfersCreated} /> : null}
  </div>;
}

function TrainingRunDescriptionPopover({ run }: { run: TrainingRun }) {
  return <Popover>
    <PopoverTrigger asChild>
      <button type="button" aria-label={`${runModelDisplayName(run)} 版本说明`} className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-xs font-medium text-console-muted transition-colors hover:bg-blue-50 hover:text-console-cyan focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30">
        <Info className="h-3.5 w-3.5" aria-hidden="true" />说明
      </button>
    </PopoverTrigger>
    <PopoverContent align="center" sideOffset={8} className="w-[min(22rem,calc(100vw-2rem))] rounded-xl border-console-line p-0 shadow-xl shadow-slate-950/8">
      <div className="border-b border-console-line px-4 py-3">
        <p className="truncate text-sm font-semibold text-console-text">{runModelDisplayName(run)}</p>
        <p className="mt-0.5 text-xs text-console-muted">版本说明</p>
      </div>
      <p className="whitespace-pre-wrap break-words px-4 py-4 text-sm leading-6 text-console-text">{run.version_description?.trim() || "历史任务未填写版本说明。"}</p>
    </PopoverContent>
  </Popover>;
}

function RunsPanel({ runs, selectedRun, canStop, canCreate, query, statusFilter, page, hasPreviousPage, hasNextPage, onQueryChange, onStatusFilterChange, onPreviousPage, onNextPage, onCreate, onSelect, onRunChange }: { runs: TrainingRun[]; selectedRun: TrainingRun | null; canStop: boolean; canCreate: boolean; query: string; statusFilter: TrainingRunStatusFilter; page: number; hasPreviousPage: boolean; hasNextPage: boolean; onQueryChange: (value: string) => void; onStatusFilterChange: (value: TrainingRunStatusFilter) => void; onPreviousPage: () => void; onNextPage: () => void; onCreate: () => void; onSelect: (run: TrainingRun | null) => void; onRunChange: (run: TrainingRun) => void }) {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  // 服务端负责完整查询；本地同步过滤让输入和筛选在下一次请求完成前立即响应。
  const filteredRuns = runs.filter((run) => matchesRunStatusFilter(run.status, statusFilter) && (!normalizedQuery || [runModelDisplayName(run), run.family_name, run.version_ref, run.run_ref, run.server_name ?? "", run.server_ref].some((value) => value.toLocaleLowerCase().includes(normalizedQuery))));

  if (selectedRun) {
    return (
      <TrainingRunDetail run={selectedRun} canStop={canStop} onBack={() => onSelect(null)} onRunChange={onRunChange} />
    );
  }

  return (
    <section aria-labelledby="training-runs-heading" className="border-b border-console-line bg-console-panel">
      <header className="flex flex-col gap-4 py-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="h-5 w-5 text-console-cyan" aria-hidden="true" />
            <h2 id="training-runs-heading" className="text-lg font-semibold text-console-text">训练任务</h2>
          </div>
          <p className="mt-1 text-sm text-console-muted">统一查看任务调度、训练进度与最近指标；状态会自动刷新。</p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <ConsoleButton variant="primary" disabled={!canCreate} onClick={onCreate}><Plus className="h-4 w-4" />新建训练任务</ConsoleButton>
          <label className="relative min-w-0 sm:w-64">
            <span className="sr-only">搜索训练任务</span>
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" aria-hidden="true" />
            <input
              type="search"
              aria-label="搜索训练任务"
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              placeholder="搜索任务、模型或服务器"
              className="h-9 w-full rounded-md border border-console-line bg-console-panel pl-9 pr-3 text-sm text-console-text outline-none transition-[border-color,box-shadow] duration-180 placeholder:text-console-muted/70 focus-visible:border-console-cyan focus-visible:ring-2 focus-visible:ring-console-cyan/15 motion-reduce:transition-none"
            />
          </label>
          <label>
            <span className="sr-only">状态筛选</span>
            <select
              aria-label="状态筛选"
              className="h-9 w-full rounded-md border border-console-line bg-console-panel px-3 text-sm text-console-text outline-none focus-visible:border-console-cyan focus-visible:ring-2 focus-visible:ring-console-cyan/15 sm:w-36"
              value={statusFilter}
              onChange={(event) => onStatusFilterChange(event.target.value as TrainingRunStatusFilter)}
            >
              <option value="all">全部状态</option>
              {runStatusFilterOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
        </div>
      </header>

      <div className="overflow-x-auto border-t border-console-line">
        <table className="w-full min-w-[1080px] table-fixed text-left text-sm">
          <thead className="bg-console-panel2 text-xs font-medium text-console-muted">
            <tr>
              <th className="w-[21%] px-4 py-3">任务 / 模型</th>
              <th className="w-[12%] px-4 py-3">服务器 / GPU</th>
              <th className="w-[23%] px-4 py-3">训练进度</th>
              <th className="w-[9%] px-4 py-3">版本说明</th>
              <th className="w-[9%] px-4 py-3">最新 Loss</th>
              <th className="w-[9%] px-4 py-3">状态</th>
              <th className="w-[12%] px-4 py-3">更新时间</th>
              <th className="w-[5%] px-4 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {filteredRuns.map((run) => (
              <tr key={run.run_ref} className="border-t border-console-line transition-[background-color] duration-150 hover:bg-console-panel2/70 motion-reduce:transition-none">
                <td className="px-4 py-3.5 align-middle">
                  <button type="button" className="max-w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => onSelect(run)}>
                    <span className="block truncate font-medium text-console-text hover:text-console-cyan">{runModelDisplayName(run)}</span>
                    <span className="mt-1 block truncate font-mono text-[11px] text-console-muted">{run.run_ref}</span>
                  </button>
                </td>
                <td className="px-4 py-3.5 align-middle text-console-muted">
                  <span className="block truncate text-console-text" title={run.server_name ?? run.server_ref}>{run.server_name ?? run.server_ref}</span>
                  <span className="mt-1 block text-xs">GPU {run.gpu_uuids.length} 张</span>
                </td>
                <td className="px-4 py-3.5 align-middle">
                  <div className="flex items-center justify-between gap-3 text-xs">
                    <span className="text-console-muted">{runProgressSummary(run)}</span>
                    <span className="tabular-nums text-console-text">{run.progress_percent.toFixed(1)}%</span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-label={`${runModelDisplayName(run)} 训练进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={run.progress_percent}>
                    <span className="block h-full rounded-full bg-console-cyan transition-[width] duration-200 motion-reduce:transition-none" style={{ width: `${Math.max(0, Math.min(100, run.progress_percent))}%` }} />
                  </div>
                </td>
                <td className="px-4 py-3.5 align-middle"><TrainingRunDescriptionPopover run={run} /></td>
                <td className="px-4 py-3.5 align-middle font-mono text-xs tabular-nums text-console-text">{formatNumber(run.latest_metric?.loss)}</td>
                <td className="px-4 py-3.5 align-middle"><StatusTag tone={statusMeta[run.status].tone}>{statusMeta[run.status].label}</StatusTag></td>
                <td className="px-4 py-3.5 align-middle text-xs text-console-muted">{formatTrainingTime(run.finished_at ?? run.started_at ?? run.created_at)}</td>
                <td className="px-4 py-3.5 text-right align-middle">
                  <button type="button" className="inline-flex items-center gap-1 rounded px-1 py-1 text-xs font-medium text-console-cyan hover:bg-blue-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => onSelect(run)}>
                    详情 <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!filteredRuns.length ? (
        <div className="border-t border-console-line py-16 text-center">
          <FileText className="mx-auto h-8 w-8 text-console-muted" aria-hidden="true" />
          <p className="mt-3 text-sm font-medium text-console-text">{query.trim() || statusFilter !== "all" ? "没有符合筛选条件的任务" : "还没有训练任务"}</p>
          {query.trim() || statusFilter !== "all" ? <p className="mt-1 text-sm text-console-muted">请调整搜索内容或状态筛选。</p> : <><p className="mt-1 text-sm text-console-muted">选择已登记模型和 GPU，创建第一项训练。</p>{canCreate ? <ConsoleButton className="mt-4" variant="primary" onClick={onCreate}><Plus className="h-4 w-4" />新建训练任务</ConsoleButton> : null}</>}
        </div>
      ) : null}
      {filteredRuns.length || hasPreviousPage ? <footer className="flex items-center justify-between border-t border-console-line px-4 py-3 text-sm">
        <span className="text-console-muted">第 {page} 页 · 每页最多 20 项</span>
        <div className="flex gap-2"><ConsoleButton disabled={!hasPreviousPage} onClick={onPreviousPage}>上一页</ConsoleButton><ConsoleButton disabled={!hasNextPage} onClick={onNextPage}>下一页</ConsoleButton></div>
      </footer> : null}
    </section>
  );
}

type ModelEditorRequest = { requestId: number; mode: "create" | "edit"; familyRef?: string };

function ModelsPanel({ models, servers, canManage, active, request, onSaved, onCancel, onVerified }: { models: TrainingModel[]; servers: TrainingServer[]; canManage: boolean; active: boolean; request: ModelEditorRequest | null; onSaved: (model: TrainingModel) => void; onCancel: () => void; onVerified: (model: TrainingModel) => void }) {
  const [editingFamilyRef, setEditingFamilyRef] = useState<string | null>(null);
  const [familyName, setFamilyName] = useState("");
  const [domain, setDomain] = useState(emptyLaunchTemplate.domain); const [serverRef, setServerRef] = useState(servers[0]?.server_ref ?? "");
  const [workingDirectory, setWorkingDirectory] = useState(emptyLaunchTemplate.working_directory); const [executable, setExecutable] = useState(emptyLaunchTemplate.executable);
  const [launcherKind, setLauncherKind] = useState<"torchrun" | "direct">(emptyLaunchTemplate.launcher_kind);
  const [entrypoint, setEntrypoint] = useState(emptyLaunchTemplate.entrypoint); const [fixedArgv, setFixedArgv] = useState(""); const [outputRoot, setOutputRoot] = useState(emptyLaunchTemplate.output_root); const [outputFlag, setOutputFlag] = useState(emptyLaunchTemplate.output_flag);
  const [runtimeKind, setRuntimeKind] = useState<"system" | "conda">(emptyLaunchTemplate.runtime_environment.kind);
  const [condaEnvironment, setCondaEnvironment] = useState("");
  const [monitoringFormat, setMonitoringFormat] = useState<"plain" | "transformers" | "jsonl">(emptyLaunchTemplate.monitoring.format);
  const [dataAccessMode, setDataAccessMode] = useState<TrainingDataAccessMode>("self_managed");
  const [parameterDefinitions, setParameterDefinitions] = useState<TrainingParameterDefinition[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const [verificationTarget, setVerificationTarget] = useState<{ familyRef: string; verificationRef: string } | null>(null);
  const [operation, setOperation] = useState<TrainingOperationState | null>(null);
  const [operationOpen, setOperationOpen] = useState(false);
  const editorHeadingRef = useRef<HTMLHeadingElement>(null);
  const textInput = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text placeholder:text-slate-400 placeholder:opacity-100";
  useEffect(() => {
    if (!editingFamilyRef && servers.length && !servers.some((server) => server.server_ref === serverRef)) {
      setServerRef(servers[0].server_ref);
    }
  }, [editingFamilyRef, serverRef, servers]);
  useEffect(() => {
    if (!active) return;
    const frame = window.requestAnimationFrame(() => editorHeadingRef.current?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, [active, request?.requestId]);
  const resetCreateMode = () => {
    setEditingFamilyRef(null); setFamilyName("");
    setDomain(emptyLaunchTemplate.domain); setServerRef(servers[0]?.server_ref ?? ""); setWorkingDirectory(emptyLaunchTemplate.working_directory); setLauncherKind(emptyLaunchTemplate.launcher_kind); setExecutable(emptyLaunchTemplate.executable);
    setEntrypoint(emptyLaunchTemplate.entrypoint); setFixedArgv(""); setOutputRoot(emptyLaunchTemplate.output_root); setOutputFlag(emptyLaunchTemplate.output_flag);
    setRuntimeKind(emptyLaunchTemplate.runtime_environment.kind); setCondaEnvironment(""); setMonitoringFormat(emptyLaunchTemplate.monitoring.format); setDataAccessMode("self_managed"); setParameterDefinitions([]); setError(null);
  };
  const showOperation = (next: TrainingOperationState) => {
    setOperation(next);
    setOperationOpen(true);
  };
  const openCreateMode = () => { resetCreateMode(); setOperation(null); setOperationOpen(false); setVerificationTarget(null); };
  const populateFromModel = (model: TrainingModel) => {
    const template = model.configuration?.launch_template;
    if (!template || !model.configuration) { setError("该模型族缺少可编辑的 launch template，请重新加载管理员投影。"); return false; }
    setFamilyName(model.family_name);
    setDomain(template.domain); setServerRef(template.server_ref); setWorkingDirectory(template.working_directory); setLauncherKind(inferLauncherKind(template)); setExecutable(template.executable);
    setEntrypoint(template.entrypoint); setFixedArgv(template.fixed_argv.join("\n")); setOutputRoot(template.output_root); setOutputFlag(template.output_flag ?? "--output_dir");
    setRuntimeKind(template.runtime_environment?.kind ?? "system"); setCondaEnvironment(template.runtime_environment?.conda_environment ?? ""); setMonitoringFormat(template.monitoring?.format ?? "plain");
    setDataAccessMode(model.configuration.data_access_mode ?? model.data_access_mode ?? "self_managed");
    setParameterDefinitions(model.configuration.parameter_definitions.map((parameter) => ({ ...structuredClone(parameter), editable: true }))); setError(null);
    return true;
  };
  const edit = (model: TrainingModel) => {
    if (!populateFromModel(model)) return;
    setEditingFamilyRef(model.family_ref);
    setOperation(null);
    setOperationOpen(false);
    setVerificationTarget(null);
  };
  useEffect(() => {
    if (!active || !request) return;
    if (request.mode === "create") { openCreateMode(); return; }
    const requestedModel = models.find((model) => model.family_ref === request.familyRef);
    if (requestedModel) edit(requestedModel);
    else setError("没有找到要修改的模型族，请返回新建训练页重新选择。");
    // A request id represents an explicit user navigation; model polling must not reopen the editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, request?.requestId]);
  useEffect(() => {
    if (!verificationTarget) return;
    const current = models.find((model) => model.family_ref === verificationTarget.familyRef);
    const verification = current?.verification;
    if (!current || !verification || verification.verification_ref !== verificationTarget.verificationRef) return;
    if (verification.status === "queued" || verification.status === "running") {
      setOperation({
        status: "loading",
        title: verification.status === "queued" ? "正在等待 Worker 验证" : "Worker 正在验证模型配置",
        detail: "正在检查训练节点、工程目录、训练入口、运行环境和输出目录。",
        steps: ["保存配置", "请求 Worker 验证", "等待验证结果"],
        activeStep: 2,
        checks: verification.checks,
      });
      setOperationOpen(true);
      return;
    }
    setOperation({
      status: verification.status === "succeeded" ? "success" : "error",
      title: verification.status === "succeeded" ? "模型配置验证通过" : "模型配置验证未通过",
      detail: verification.status === "succeeded" ? "关闭窗口后将返回新建训练任务，并加载这份最新配置。" : "请关闭窗口并根据失败项修改配置后重新保存。",
      steps: ["保存配置", "请求 Worker 验证", "等待验证结果"],
      activeStep: 3,
      checks: verification.checks,
    });
    setOperationOpen(true);
  }, [models, verificationTarget]);
  const handleOperationOpenChange = (open: boolean) => {
    // Verification is part of saving the model configuration. Keep its progress
    // visible until Worker returns a terminal result, then let the user close it.
    if (!open && operation?.status === "loading") return;
    setOperationOpen(open);
    if (open || !verificationTarget) return;
    const current = models.find((model) => model.family_ref === verificationTarget.familyRef);
    const succeeded = current?.verification?.verification_ref === verificationTarget.verificationRef && current.verification.status === "succeeded";
    setVerificationTarget(null);
    if (succeeded && current) {
      resetCreateMode();
      onVerified(current);
    }
  };
  const save = async () => {
    if (!workingDirectory.trim().startsWith("/")) { setError("工作目录必须填写训练节点上的绝对路径。"); return; }
    if (entrypoint.trim().startsWith("/") || entrypoint.split("/").includes("..")) { setError("训练入口必须填写工作目录内的相对路径，不能使用绝对路径或跳出工作目录。"); return; }
    if (!outputRoot.trim().startsWith("/")) { setError("输出根目录必须填写训练节点上的绝对路径。"); return; }
    let normalizedDefinitions: TrainingParameterDefinition[];
    try { normalizedDefinitions = validateParameterDefinitions(parameterDefinitions); } catch (caught) { setError(errorText(caught)); return; }
    const fixedTokens = fixedArgv.split("\n").map((item) => item.trim()).filter(Boolean);
    const fixedTokenFlags = fixedTokens.map((token) => token.startsWith("--") ? token.split("=", 1)[0] : token);
    if (!/^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(outputFlag)) { setError("产物输出参数格式无效。"); return; }
    if (parameterDefinitions.some((parameter) => (parameter.cli_flag || `--${parameter.key}`) === outputFlag)) { setError("产物输出参数由平台管理，不能与训练参数重复。"); return; }
    if (fixedTokenFlags.includes(outputFlag)) { setError("额外固定 argv 不能重复声明平台管理的产物输出参数。"); return; }
    if (dataAccessMode === "datapilot_managed" && (fixedTokenFlags.includes("--dataset_manifest") || normalizedDefinitions.some((parameter) => (parameter.cli_flag || `--${parameter.key}`) === "--dataset_manifest"))) { setError("--dataset_manifest 由 DataPilot 托管数据自动注入，不能在固定 argv 或训练参数中重复登记。"); return; }
    const parameterFlags = new Set(normalizedDefinitions.map((parameter) => parameter.cli_flag || `--${parameter.key}`));
    const duplicateFixedFlag = fixedTokenFlags.find((token) => parameterFlags.has(token));
    if (duplicateFixedFlag) { setError(`额外固定 argv 与训练参数重复声明了 ${duplicateFixedFlag}。`); return; }
    const normalizedCondaEnvironment = condaEnvironment.trim();
    if (runtimeKind === "conda" && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(normalizedCondaEnvironment)) { setError("Conda 环境名只能包含字母、数字、点、下划线和短横线，且不能为空。"); return; }
    setBusy(true); setError(null); showOperation({ status: "loading", title: editingFamilyRef ? "正在保存模型配置" : "正在登记模型", detail: "保存训练入口、运行环境和参数定义。", steps: ["保存配置", "请求 Worker 验证", "等待验证结果"], activeStep: 0 });
    try {
      const launchTemplate = {
        domain, server_ref: serverRef, working_directory: workingDirectory, launcher_kind: launcherKind, executable, entrypoint,
        // One argv token per line makes this a structured argv list, never a shell command.
        fixed_argv: fixedTokens, output_root: outputRoot, output_flag: outputFlag,
        runtime_environment: runtimeKind === "conda" ? { kind: "conda" as const, conda_environment: normalizedCondaEnvironment } : { kind: "system" as const },
        monitoring: { source: "stdout" as const, format: monitoringFormat },
      };
      const configuration = { data_access_mode: dataAccessMode, parameter_definitions: normalizedDefinitions, launch_template: launchTemplate };
      const editingModel = models.find((model) => model.family_ref === editingFamilyRef);
      const saved = editingModel
        ? await updateTrainingModel(editingModel.family_ref, { expected_revision: editingModel.edit_revision ?? 0, configuration })
        : await createTrainingModel({ family_name: familyName.trim(), configuration });
      onSaved(saved);
      setEditingFamilyRef(saved.family_ref);
      showOperation({ status: "loading", title: "配置已保存，正在请求 Worker 验证", detail: "系统会在模型绑定的训练节点上检查目录、入口和运行环境。", steps: ["保存配置", "请求 Worker 验证", "等待验证结果"], activeStep: 1 });
      try {
        const verifying = await verifyTrainingModel(saved.family_ref, saved.edit_revision ?? 0);
        const submitted = verifying ?? saved;
        if (verifying) onSaved(verifying);
        const verificationRef = submitted.verification?.verification_ref;
        if (verificationRef) {
          setVerificationTarget({ familyRef: submitted.family_ref, verificationRef });
          showOperation({ status: "loading", title: "Worker 正在验证模型配置", detail: "正在检查训练节点、工程目录、训练入口、运行环境和输出目录。", steps: ["保存配置", "请求 Worker 验证", "等待验证结果"], activeStep: 2, checks: submitted.verification?.checks });
        } else {
          showOperation({ status: "error", title: "配置已保存，未收到验证状态", detail: "请关闭窗口后重新保存配置以发起验证。" });
        }
      } catch (verifyError) {
        const detail = errorText(verifyError);
        setError(`模型配置已保存，但验证未能启动：${detail}`);
        showOperation({ status: "error", title: "配置已保存，验证未完成", detail: "模型仍保留为草稿，请修改后重新保存以发起验证。" });
      }
    } catch (caught) { const detail = errorText(caught); setError(detail); showOperation({ status: "error", title: editingFamilyRef ? "保存模型配置失败" : "登记模型失败", detail }); } finally { setBusy(false); }
  };
  const loadNavilaPreset = () => {
    setFamilyName("NaVILA 轨迹训练");
    setDomain(navilaTrajectoryLaunchTemplate.domain); setServerRef(servers[0]?.server_ref ?? ""); setWorkingDirectory(navilaTrajectoryLaunchTemplate.working_directory);
    setLauncherKind(navilaTrajectoryLaunchTemplate.launcher_kind); setExecutable(navilaTrajectoryLaunchTemplate.executable); setEntrypoint(navilaTrajectoryLaunchTemplate.entrypoint); setFixedArgv("");
    setOutputRoot(navilaTrajectoryLaunchTemplate.output_root); setOutputFlag(navilaTrajectoryLaunchTemplate.output_flag);
    setRuntimeKind(navilaTrajectoryLaunchTemplate.runtime_environment.kind); setCondaEnvironment(navilaTrajectoryLaunchTemplate.runtime_environment.conda_environment ?? ""); setMonitoringFormat(navilaTrajectoryLaunchTemplate.monitoring.format);
    setDataAccessMode("datapilot_managed"); setParameterDefinitions(structuredClone(navilaTrajectoryParameters)); setError(null);
  };
  const defaultParameterValues = Object.fromEntries(parameterDefinitions.map((parameter) => [parameter.key, parameter.default]));
  const defaultEnabledParameters = enabledTrainingParameters(parameterDefinitions, defaultParameterValues);
  const commandSummaryReady = Boolean(executable.trim() && entrypoint.trim() && outputFlag.trim());
  const renderCommandToken = (token: string) => /\s/.test(token) ? JSON.stringify(token) : token;
  const summaryFixedTokens = fixedArgv.split("\n").map((item) => item.trim()).filter(Boolean);
  const summaryFixedLines = summaryFixedTokens.reduce<string[]>((lines, token, index, all) => {
    if (!token.startsWith("--") && index > 0 && all[index - 1]?.startsWith("--")) return lines;
    const next = token.startsWith("--") && all[index + 1] && !all[index + 1].startsWith("--") ? ` ${renderCommandToken(all[index + 1])}` : "";
    lines.push(`${renderCommandToken(token)}${next}`);
    return lines;
  }, []);
  const commandLines = commandSummaryReady ? [
    renderCommandToken(executable.trim()),
    ...(launcherKind === "torchrun" ? ["--nnodes=1", "--nproc_per_node=<所选 GPU 数>", "--master_port=<自动分配>", "--master_addr=127.0.0.1", "--node_rank=0"] : []),
    entrypoint.trim(),
    ...summaryFixedLines,
    ...defaultEnabledParameters.flatMap((parameter) => {
      const flag = parameter.cli_flag || `--${parameter.key}`;
      if (parameter.argument_style === "flag_when_true") return parameter.default ? [flag] : [];
      const rendered = parameter.sensitive ? "********" : parameter.type === "boolean" ? (parameter.default ? "True" : "False") : String(parameter.default);
      return [`${renderCommandToken(flag)} ${renderCommandToken(rendered)}`];
    }),
    ...(dataAccessMode === "datapilot_managed" ? ["--dataset_manifest <平台生成数据清单>"] : []),
    `${renderCommandToken(outputFlag.trim())} <平台生成输出目录>`,
  ].filter(Boolean) : [];
  const formTitle = editingFamilyRef ? `修改 ${models.find((model) => model.family_ref === editingFamilyRef)?.family_name ?? (familyName || "模型")} 的参数配置` : "登记新模型族";
  return <div className="mx-auto max-w-[1520px] space-y-5">
    <header className="flex flex-col gap-3 border-b border-console-line pb-5 sm:flex-row sm:items-start sm:justify-between">
      <div><button type="button" className="mb-2 text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => { setError(null); onCancel(); }}>← 返回新建训练任务</button><h2 ref={editorHeadingRef} tabIndex={-1} className="text-xl font-semibold text-console-text outline-none">{formTitle}</h2><p className="mt-1 text-sm text-console-muted">{editingFamilyRef ? "修改只影响之后创建的训练，历史模型版本保留原配置快照；保存后系统会自动验证最新配置。" : "登记训练入口、参数定义和输出规则；保存后系统会自动验证配置。"}</p></div>
      {!editingFamilyRef ? <ConsoleButton variant="ghost" disabled={!canManage || busy} onClick={loadNavilaPreset}>一键载入 NaVILA 轨迹训练模板</ConsoleButton> : null}
    </header>
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(320px,420px)]">
    <ConsoleCard className="min-w-0 shadow-none">
      <section aria-labelledby="model-basic-config-title"><h3 id="model-basic-config-title" className="font-semibold text-console-text">基础配置</h3><p className="mt-1 text-xs text-console-muted">灰色文字仅为填写示例，不会作为真实配置保存。</p>
        <label className="mt-4 block text-sm text-console-muted">模型族名称<input className={textInput} value={familyName} placeholder="例如 NaVILA 轨迹训练" disabled={!canManage || Boolean(editingFamilyRef)} onChange={(event) => setFamilyName(event.target.value)} /></label>
        <label className="mt-3 block text-sm text-console-muted">训练数据管理方式<select aria-label="训练数据管理方式" className={textInput} value={dataAccessMode} disabled={!canManage} onChange={(event) => setDataAccessMode(event.target.value as TrainingDataAccessMode)}><option value="datapilot_managed">DataPilot 托管数据</option><option value="self_managed">模型自行管理数据</option></select></label>
        {dataAccessMode === "datapilot_managed" ? <p className="mt-2 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-800">平台负责将已发布数据传输到训练节点，并在训练时通过 <span className="font-mono">--dataset_manifest</span> 提供本次训练集和测试集。模型项目负责读取 manifest 并转换为自身需要的样本格式。</p> : <p className="mt-2 text-xs leading-5 text-console-muted">模型项目自行管理数据路径和划分，平台不会传输数据或注入 dataset manifest。</p>}
        <div className="mt-3 grid gap-3 md:grid-cols-2 md:items-end">
          <label className="text-sm text-console-muted">领域 · Domain<input aria-label="领域 · Domain" className={textInput} value={domain} placeholder="例如 vla" disabled={!canManage} onChange={(e) => setDomain(e.target.value)} /></label>
          <label className="text-sm text-console-muted">训练节点 · Server<TrainingServerSelect ariaLabel="训练节点 · Server" servers={servers} value={serverRef} disabled={!canManage} onValueChange={setServerRef} /></label>
          <label className="text-sm text-console-muted">工作目录 · Working directory<span className="block text-[11px] leading-4">绝对路径。填写训练节点上的模型工程目录。</span><input aria-label="工作目录 · Working directory" className={textInput} value={workingDirectory} placeholder="例如 /data/project/NaVILA" disabled={!canManage} onChange={(e) => setWorkingDirectory(e.target.value)} /></label>
          <label className="text-sm text-console-muted">启动方式 · Launcher<select aria-label="启动方式 · Launcher" className={textInput} value={launcherKind} disabled={!canManage} onChange={(e) => setLauncherKind(e.target.value as "torchrun" | "direct")}><option value="torchrun">PyTorch Torchrun（多 GPU）</option><option value="direct">单进程启动（不使用 Torchrun）</option></select></label>
          <label className="text-sm text-console-muted">启动程序 · Executable<input aria-label="启动程序 · Executable" className={textInput} value={executable} placeholder={launcherKind === "torchrun" ? "例如 torchrun" : "例如 python"} disabled={!canManage} onChange={(e) => setExecutable(e.target.value)} /></label>
          <label className="text-sm text-console-muted">训练入口 · Entrypoint<span className="block text-[11px] leading-4">相对路径。以工作目录为起点，且必须位于工作目录内。</span><input aria-label="训练入口 · Entrypoint" className={textInput} value={entrypoint} placeholder="例如 llava/train/train_mem.py" disabled={!canManage} onChange={(e) => setEntrypoint(e.target.value)} /></label>
          <label className="text-sm text-console-muted">输出根目录 · Output root<span className="block text-[11px] leading-4">绝对路径。平台会在此目录下生成模型版本和训练阶段目录。</span><input aria-label="输出根目录 · Output root" className={textInput} value={outputRoot} placeholder="例如 /data/training_outputs" disabled={!canManage} onChange={(e) => setOutputRoot(e.target.value)} /></label>
          <label className="text-sm text-console-muted">产物输出参数 · Output flag<span className="block text-[11px] leading-4">训练脚本接收输出目录的 CLI flag。</span><input aria-label="产物输出参数 · Output flag" className={textInput} value={outputFlag} placeholder="例如 --output_dir" disabled={!canManage} onChange={(e) => setOutputFlag(e.target.value)} /></label>
          <label className="text-sm text-console-muted">运行环境 · Runtime environment<select aria-label="运行环境 · Runtime environment" className={textInput} value={runtimeKind} disabled={!canManage} onChange={(e) => setRuntimeKind(e.target.value as "system" | "conda")}><option value="system">Worker 系统环境</option><option value="conda">Conda 环境</option></select></label>
          {runtimeKind === "conda" ? <label className="text-sm text-console-muted">Conda 环境名<input className={textInput} value={condaEnvironment} disabled={!canManage} maxLength={128} placeholder="例如 navila" onChange={(e) => setCondaEnvironment(e.target.value)} /></label> : null}
          <label className="text-sm text-console-muted">指标日志格式 · Metrics format<select aria-label="指标日志格式 · Metrics format" className={textInput} value={monitoringFormat} disabled={!canManage} onChange={(e) => setMonitoringFormat(e.target.value as "plain" | "transformers" | "jsonl")}><option value="plain">普通文本（仅日志）</option><option value="transformers">Transformers Trainer 日志</option><option value="jsonl">JSON Lines 指标</option></select></label>
        </div>
        <label className="mt-3 block text-sm text-console-muted">额外固定 argv（每行一个 token）<textarea className="mt-1 min-h-20 w-full rounded-md border border-console-line bg-console-panel p-2 font-mono text-xs text-console-text placeholder:text-slate-400" value={fixedArgv} placeholder={"例如：\n--deepspeed\n./scripts/zero3.json"} disabled={!canManage} onChange={(e) => setFixedArgv(e.target.value)} /></label>
      </section>
      <div className="mt-6 border-t border-console-line pt-6"><ParameterDefinitionEditor definitions={parameterDefinitions} disabled={!canManage} onChange={setParameterDefinitions} /></div>
      <p className="mt-3 text-xs text-console-muted">{launcherKind === "torchrun" ? "GPU、分布式参数和产物输出目录由平台管理，不注册为普通参数。" : "GPU 和产物输出目录由平台管理；单进程启动不会注入 Torchrun 分布式参数。"}</p>
      {error ? <p role="alert" className="mt-4 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p> : null}
      <div className="sticky bottom-3 mt-5 flex flex-wrap justify-end gap-2 rounded-lg border border-console-line bg-white/95 p-3 shadow-lg backdrop-blur"><ConsoleButton disabled={busy} onClick={() => { setError(null); onCancel(); }}>取消</ConsoleButton><ConsoleButton variant="primary" aria-busy={busy} disabled={!canManage || busy || !familyName.trim() || !domain.trim() || !serverRef.trim() || !workingDirectory.trim() || !executable.trim() || !entrypoint.trim() || !outputRoot.trim() || !outputFlag.trim()} onClick={() => void save()}>{busy ? <RefreshCw className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Plus className="h-4 w-4" />}{busy ? "正在保存并验证…" : editingFamilyRef ? "保存参数配置" : "登记模型族"}</ConsoleButton></div>
    </ConsoleCard>
    <aside className="min-w-0 xl:sticky xl:top-4" aria-label="实时命令摘要">
      <section className="rounded-xl border border-slate-800 bg-slate-950 p-4 shadow-[0_14px_38px_rgba(15,23,42,0.16)]" aria-labelledby="command-summary-title"><div className="flex items-start gap-3"><Terminal className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" aria-hidden="true" /><div><h3 id="command-summary-title" className="text-sm font-semibold text-slate-100">实时结构化命令摘要（默认值）</h3><p className="mt-1 text-xs leading-5 text-slate-400">按当前默认值生成，仅用于核对，不会执行。</p></div></div>{commandSummaryReady ? <pre className="console-soft-scrollbar mt-4 max-h-[calc(100vh-12rem)] min-h-48 overflow-auto whitespace-pre rounded-md bg-slate-900/70 p-3 font-mono text-xs leading-6 text-slate-100"><code>{commandLines.join("\n")}</code></pre> : <div className="mt-4 rounded-md border border-dashed border-slate-700 px-3 py-8 text-center text-xs leading-5 text-slate-400">填写启动程序、训练入口和产物输出参数后显示命令摘要。</div>}</section>
      <p className="mt-3 px-1 text-xs leading-5 text-console-muted">宽屏下摘要会固定在视口内；参数较多时仅滚动摘要区域。</p>
    </aside>
    </div>
    <TrainingOperationDialog open={operationOpen} operation={operation} onOpenChange={handleOperationOpenChange} autoCloseMs={null} />
  </div>;
}

function ResourcesPanel({ servers, resourcesByServer, resourceErrors, onRefresh }: { servers: TrainingServer[]; resourcesByServer: Record<string, TrainingServerResources>; resourceErrors: Record<string, string>; onRefresh: () => void }) {
  const [selectedServerRef, setSelectedServerRef] = useState(servers[0]?.server_ref ?? "");
  useEffect(() => {
    if (!servers.some((server) => server.server_ref === selectedServerRef)) setSelectedServerRef(servers[0]?.server_ref ?? "");
  }, [selectedServerRef, servers]);

  const selectedServer = servers.find((server) => server.server_ref === selectedServerRef) ?? servers[0] ?? null;
  const selectedResources = selectedServer ? resourcesByServer[selectedServer.server_ref] : undefined;
  const selectedError = selectedServer ? resourceErrors[selectedServer.server_ref] : undefined;
  const gpus = selectedResources?.gpus ?? [];
  const cpu = selectedResources?.cpu;
  const memory = selectedResources?.memory;
  const disks = selectedResources?.disks ?? [];
  const memoryAvailablePercent = memory ? availablePercent(memory.available_bytes, memory.total_bytes) : 0;
  // 可用资源与创建训练任务时的 GPU 选择规则保持一致：外部占用或已有租约均不可用。
  const availableCount = gpus.filter((gpu) => !gpu.externally_occupied && !gpu.lease_run_ref).length;
  const averageUtilization = gpus.length ? gpus.reduce((sum, gpu) => sum + gpu.utilization_percent, 0) / gpus.length : 0;

  return (
    <section aria-labelledby="training-resources-heading" className="space-y-5">
      <header>
        <h2 id="training-resources-heading" className="text-xl font-semibold text-console-text">服务器资源</h2>
        <p className="mt-1 text-sm text-console-muted">集中查看已部署 Worker 的真实 CPU、内存、磁盘和 GPU 快照；所有已纳管服务器每 2 秒独立刷新。</p>
      </header>

      {servers.length ? (
        <div className="console-soft-scrollbar overflow-x-auto pb-1" role="group" aria-label="选择训练服务器">
          <div className="flex min-w-max gap-2">
            {servers.map((server) => {
              const active = server.server_ref === selectedServer?.server_ref;
              const serverError = resourceErrors[server.server_ref];
              const ready = Boolean(resourcesByServer[server.server_ref]);
              return (
                <button
                  key={server.server_ref}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setSelectedServerRef(server.server_ref)}
                  className={cn(
                    "flex min-w-52 items-center gap-3 rounded-lg border px-3 py-2.5 text-left outline-none transition-[border-color,background-color,box-shadow] duration-180 focus-visible:ring-2 focus-visible:ring-console-cyan/30 motion-reduce:transition-none",
                    active ? "border-console-cyan bg-blue-50/60 shadow-sm" : "border-console-line bg-console-panel hover:border-console-cyan/35 hover:bg-console-panel2",
                  )}
                >
                  <span className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-md", active ? "bg-white text-console-cyan" : "bg-console-panel2 text-console-muted")}><Server className="h-4 w-4" aria-hidden="true" /></span>
                  <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium text-console-text">{server.name}</span><span className="mt-0.5 block truncate font-mono text-[11px] text-console-muted">{server.server_ref} · {server.gpu_count} GPU</span></span>
                  <span className={cn("h-2 w-2 shrink-0 rounded-full", serverError ? "bg-rose-500" : ready ? "bg-emerald-500" : "bg-slate-300")} aria-label={serverError ? "资源读取失败" : ready ? "资源连接正常" : "正在读取资源"} />
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      {selectedServer ? (
        <div className="flex flex-col gap-3 border-y border-console-line py-3 sm:flex-row sm:items-center sm:justify-between">
          <div><p className="font-medium text-console-text">{selectedServer.name}</p><p className="mt-0.5 text-xs text-console-muted">{selectedServer.kind} · 最近采样 {formatTrainingTime(selectedResources?.sampled_at)}</p></div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm"><span className="text-console-muted">纳管 <b className="tabular-nums text-console-text">{gpus.length}</b></span><span className="text-console-muted">可用 <b className="tabular-nums text-emerald-700">{availableCount}</b></span><span className="text-console-muted">平均利用率 <b className="tabular-nums text-console-text">{averageUtilization.toFixed(0)}%</b></span></div>
        </div>
      ) : null}

      {selectedError ? <div className="flex flex-col gap-3 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><p role="alert" className="text-sm text-rose-700">{selectedServer?.name} 的资源读取失败：{selectedError}</p><ConsoleButton onClick={onRefresh}><RefreshCw className="h-4 w-4" />重新加载</ConsoleButton></div> : null}

      {cpu || memory || disks.length ? <div className="space-y-3">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {cpu ? <div className="rounded-xl border border-console-line bg-console-panel p-4"><Cpu className="h-4 w-4 text-console-muted" /><p className="mt-2 text-xs text-console-muted">CPU</p><p className="mt-1 font-semibold text-console-text">{cpu.logical_cores} 核 · load {cpu.load_1m?.toFixed(2) ?? "--"}</p></div> : null}
          {memory ? <div className="rounded-xl border border-console-line bg-console-panel p-4"><div className="flex items-start justify-between gap-3"><div><Activity className="h-4 w-4 text-console-muted" /><p className="mt-2 text-xs text-console-muted">内存可用 / 总容量</p></div><p className="text-2xl font-semibold leading-none text-console-text" aria-label={`内存可用百分比 ${memoryAvailablePercent.toFixed(0)}%`}>{memoryAvailablePercent.toFixed(0)}<span className="text-sm">%</span></p></div><p className="mt-1 font-semibold text-console-text">{formatResourceBytes(memory.available_bytes)} / {formatResourceBytes(memory.total_bytes)}</p><ProgressBar className="mt-2" value={memoryAvailablePercent} tone="success" label="可用内存" /></div> : null}
          <div className="rounded-xl border border-console-line bg-console-panel p-4"><Server className="h-4 w-4 text-console-muted" /><p className="mt-2 text-xs text-console-muted">GPU</p><p className="mt-1 font-semibold text-console-text">{gpus.length} 张 · {Math.round(gpus.reduce((sum, gpu) => sum + gpu.total_memory_mib, 0) / 1024)} GiB</p></div>
        </div>
        <section className="rounded-xl border border-console-line bg-console-panel p-4" aria-labelledby="server-disks-title"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><HardDrive className="h-4 w-4 text-console-cyan" /><h3 id="server-disks-title" className="font-medium text-console-text">磁盘空间</h3></div><span className="text-xs text-console-muted">Worker 自动发现 {disks.length} 个存储挂载点</span></div>{disks.length ? <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{disks.map((disk) => { const percent = availablePercent(disk.available_bytes, disk.total_bytes); return <article key={disk.mount} className="rounded-md bg-console-panel2 p-3"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="truncate font-mono text-sm font-medium text-console-text" title={disk.mount}>{disk.mount}</p><p className="mt-1 text-xs text-console-muted">可用 / 总容量</p></div><p className="shrink-0 text-2xl font-semibold leading-none text-console-text" aria-label={`${disk.mount} 可用 ${percent.toFixed(0)}%`}>{percent.toFixed(0)}<span className="text-sm">%</span></p></div><p className="mt-2 font-semibold text-console-text">{formatResourceBytes(disk.available_bytes)} / {formatResourceBytes(disk.total_bytes)}</p><ProgressBar className="mt-2" value={percent} tone={percent < 10 ? "danger" : percent < 20 ? "warning" : "success"} label="可用空间" /></article>; })}</div> : <p className="mt-3 text-sm text-console-muted">Worker 尚未上报可用存储挂载点。</p>}</section>
      </div> : null}

      {gpus.length ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" role="region" aria-label={`${selectedServer?.name ?? "当前服务器"} GPU 资源`}>
          {gpus.map((gpu) => {
            const memoryPercent = gpu.total_memory_mib ? gpu.used_memory_mib / gpu.total_memory_mib * 100 : 0;
            const occupied = gpu.externally_occupied || Boolean(gpu.lease_run_ref);
            return (
              <article key={gpu.gpu_uuid} className="rounded-xl border border-console-line bg-console-panel p-4 shadow-[0_6px_20px_rgba(31,42,68,0.04)]">
                <div className="flex items-start justify-between gap-3"><div><p className="font-medium text-console-text">GPU {gpu.index}</p><p className="mt-1 text-xs text-console-muted">{gpu.name} · {gpu.temperature_c}°C</p></div><StatusTag tone={occupied ? "warning" : "success"}>{gpu.lease_run_ref ? "平台已租用" : gpu.externally_occupied ? "外部占用" : selectedServer?.kind === "training_node" ? "平台未租用" : "可用"}</StatusTag></div>
                <ProgressBar className="mt-4" value={gpu.utilization_percent} tone="purple" label={`利用率 ${gpu.utilization_percent}%`} />
                <ProgressBar className="mt-4" value={memoryPercent} tone="info" label={`显存 ${Math.round(gpu.used_memory_mib / 1024)} / ${Math.round(gpu.total_memory_mib / 1024)} GiB`} />
                <div className="mt-4 border-t border-console-line pt-3"><p className="truncate font-mono text-[11px] text-console-muted" title={gpu.gpu_uuid}>{gpu.gpu_uuid}</p><p className="mt-1 truncate text-xs text-console-muted" title={gpu.lease_run_ref ?? undefined}>{gpu.lease_run_ref ? `租约任务：${gpu.lease_run_ref}` : occupied ? "由平台外部进程占用" : selectedServer?.kind === "training_node" ? "平台未租用；请结合利用率和显存判断" : "可分配给新的训练任务"}</p></div>
              </article>
            );
          })}
        </div>
      ) : !selectedError ? (
        <div className="rounded-xl border border-dashed border-console-line py-16 text-center"><Server className="mx-auto h-8 w-8 text-console-muted" aria-hidden="true" /><p className="mt-3 text-sm font-medium text-console-text">{selectedServer ? "该服务器暂无可展示的 GPU" : "尚未发现训练服务器"}</p><p className="mt-1 text-sm text-console-muted">{selectedServer ? "资源刷新后会自动显示在这里。" : "服务器接入后可在此切换查看资源。"}</p></div>
      ) : null}
    </section>
  );
}

export function TrainingPlatform() {
  const location = useLocation(); const navigate = useNavigate();
  const deepRunRef = useMemo(() => /^\/model\/runs\/([^/]+)\/?$/.exec(location.pathname)?.[1], [location.pathname]);
  const deepVersionRef = useMemo(() => /^\/model\/versions\/([^/]+)\/?$/.exec(location.pathname)?.[1], [location.pathname]);
  const [tab, setTab] = useState<TrainingTab>("runs"); const [capabilities, setCapabilities] = useState<TrainingCapabilities | null>(null); const [models, setModels] = useState<TrainingModel[]>([]); const [nodes, setNodes] = useState<TrainingNode[]>([]); const [servers, setServers] = useState<TrainingServer[]>([]); const [resourcesByServer, setResourcesByServer] = useState<Record<string, TrainingServerResources>>({}); const [resourceErrors, setResourceErrors] = useState<Record<string, string>>({}); const [runs, setRuns] = useState<TrainingRun[]>([]); const [selectedRun, setSelectedRun] = useState<TrainingRun | null>(null); const [error, setError] = useState<string | null>(null); const [eventStreamDisconnected, setEventStreamDisconnected] = useState(false); const [datasetEventRevision, setDatasetEventRevision] = useState(0);
  const [runQuery, setRunQuery] = useState("");
  const [runQueryForRequest, setRunQueryForRequest] = useState("");
  const [runStatusFilter, setRunStatusFilter] = useState<TrainingRunStatusFilter>("all");
  const [runCursorHistory, setRunCursorHistory] = useState<string[]>([""]);
  const [runNextAfter, setRunNextAfter] = useState<string | null>(null);
  const [datasetTransfers, setDatasetTransfers] = useState<TrainingDatasetTransfer[]>([]);
  const [trackedTransferRefs, setTrackedTransferRefs] = useState<Set<string>>(new Set());
  const [datasetTransferError, setDatasetTransferError] = useState<string | null>(null);
  const [datasetTransferNotice, setDatasetTransferNotice] = useState<DatasetTransferNotice | null>(null);
  const [datasetTransferNoticeClosing, setDatasetTransferNoticeClosing] = useState(false);
  const [modelVersionEventRevision, setModelVersionEventRevision] = useState(0);
  const [modelEditorRequest, setModelEditorRequest] = useState<ModelEditorRequest | null>(null);
  const [preferredFamilyRef, setPreferredFamilyRef] = useState<string | null>(null);
  const pendingNavigationTab = useRef<TrainingTab | null>(null);
  const modelEditorSequence = useRef(0);
  const transferStatuses = useRef(new Map<string, TrainingDatasetTransfer["status"]>());
  const transferStatusInitialized = useRef(false);
  const transferNoticeSequence = useRef(0);
  const runAfter = runCursorHistory[runCursorHistory.length - 1] || undefined;

  useEffect(() => {
    const timer = window.setTimeout(() => setRunQueryForRequest(runQuery.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [runQuery]);

  useEffect(() => {
    setRunCursorHistory([""]);
  }, [runQueryForRequest, runStatusFilter]);

  const showDatasetTransferNotice = useCallback((message: string, tone: DatasetTransferNotice["tone"] = "info") => {
    transferNoticeSequence.current += 1;
    setDatasetTransferNotice({ id: transferNoticeSequence.current, message, tone });
    setDatasetTransferNoticeClosing(false);
  }, []);

  const refreshDatasetTransfers = useCallback(async (announceChanges = true) => {
    try {
      const next = await listTrainingDatasetTransfers();
      if (transferStatusInitialized.current && announceChanges) {
        for (const transfer of next) {
          const previousStatus = transferStatuses.current.get(transfer.transfer_ref);
          if (!previousStatus || previousStatus === transfer.status) continue;
          const tone: DatasetTransferNotice["tone"] = transfer.status === "succeeded" ? "success" : transfer.status === "failed" ? "danger" : transfer.status === "cancelled" ? "neutral" : "info";
          showDatasetTransferNotice(`${transfer.dataset_date}：${transferLabel(transfer.status)}`, tone);
        }
      }
      transferStatuses.current = new Map(next.map((transfer) => [transfer.transfer_ref, transfer.status]));
      transferStatusInitialized.current = true;
      setDatasetTransfers(next);
      setTrackedTransferRefs((current) => {
        const updated = new Set(current);
        next.filter((transfer) => activeTransferStatuses.has(transfer.status) || actionableTransferStatuses.has(transfer.status)).forEach((transfer) => updated.add(transfer.transfer_ref));
        return updated;
      });
      setDatasetTransferError(null);
    } catch (caught) {
      setDatasetTransferError(errorText(caught));
    }
  }, [showDatasetTransferNotice]);

  const mergeDatasetTransfers = useCallback((updates: TrainingDatasetTransfer[]) => {
    setDatasetTransfers((current) => [...updates, ...current.filter((item) => !updates.some((update) => update.transfer_ref === item.transfer_ref))]);
    setTrackedTransferRefs((current) => new Set([...current, ...updates.map((item) => item.transfer_ref)]));
    updates.forEach((transfer) => transferStatuses.current.set(transfer.transfer_ref, transfer.status));
  }, []);

  const handleTransfersCreated = useCallback((created: TrainingDatasetTransfer[]) => {
    mergeDatasetTransfers(created);
    showDatasetTransferNotice(created.length === 1 ? `${created[0].dataset_date} 已加入后台传输` : `${created.length} 个日期已加入后台传输`, "info");
  }, [mergeDatasetTransfers, showDatasetTransferNotice]);

  const handlePauseDatasetTransfer = useCallback(async (transfer: TrainingDatasetTransfer) => {
    try {
      const updated = await pauseTrainingDatasetTransfer(transfer.transfer_ref);
      mergeDatasetTransfers([updated]);
      showDatasetTransferNotice(`${updated.dataset_date}：${transferLabel(updated.status)}`, "neutral");
    } catch (caught) {
      setDatasetTransferError(errorText(caught));
    }
  }, [mergeDatasetTransfers, showDatasetTransferNotice]);

  const handleCancelDatasetTransfer = useCallback(async (transfer: TrainingDatasetTransfer) => {
    if (!window.confirm(`确定取消 ${transfer.dataset_date} 的本次传输吗？\n\n训练节点中已下载的临时数据也会被删除，之后需要重新传输。`)) return;
    try {
      const updated = await cancelTrainingDatasetTransfer(transfer.transfer_ref);
      mergeDatasetTransfers([updated]);
      showDatasetTransferNotice(`${updated.dataset_date}：${transferLabel(updated.status)}`, "neutral");
    } catch (caught) {
      setDatasetTransferError(errorText(caught));
    }
  }, [mergeDatasetTransfers, showDatasetTransferNotice]);

  const handleRetryDatasetTransfer = useCallback(async (transfer: TrainingDatasetTransfer) => {
    try {
      const updated = await retryTrainingDatasetTransfer(transfer.transfer_ref);
      mergeDatasetTransfers([updated]);
      showDatasetTransferNotice(`${updated.dataset_date} 已重新加入后台传输`, "info");
    } catch (caught) {
      setDatasetTransferError(errorText(caught));
    }
  }, [mergeDatasetTransfers, showDatasetTransferNotice]);
  const load = useCallback(async () => {
    try {
      const [nextCapabilities, nextModels, nextNodes, nextServers, nextRuns] = await Promise.all([getTrainingCapabilities(), listTrainingModels(), listTrainingNodes(), listTrainingServers(), listTrainingRuns({ status: runStatusFilter, query: runQueryForRequest, after: runAfter, limit: 20 })]);
      // 各服务器资源独立请求，单台服务器不可达时仍保留其他服务器的监控数据。
      const resourceResults = await Promise.allSettled(nextServers.map(async (server) => ({ serverRef: server.server_ref, resources: await getTrainingServerResources(server.server_ref) })));
      const nextResources: Record<string, TrainingServerResources> = {};
      const nextResourceErrors: Record<string, string> = {};
      resourceResults.forEach((result, index) => {
        const serverRef = nextServers[index].server_ref;
        if (result.status === "fulfilled") nextResources[serverRef] = result.value.resources;
        else nextResourceErrors[serverRef] = errorText(result.reason);
      });
      setCapabilities(nextCapabilities); setModels(nextModels); setNodes((current) => nextNodes.map((node) => {
        const local = current.find((item) => item.node_ref === node.node_ref);
        return local && local.state_revision > node.state_revision ? local : node;
      })); setServers(nextServers); setResourcesByServer(nextResources); setResourceErrors(nextResourceErrors); setRuns(nextRuns); setRunNextAfter(nextRuns.next_after ?? null);
      setSelectedRun((current) => current ? nextRuns.find((item) => item.run_ref === current.run_ref) ?? current : null);
      setError(null);
    } catch (caught) { setError(errorText(caught)); }
  }, [runAfter, runQueryForRequest, runStatusFilter]);
  useEffect(() => { void load(); const interval = window.setInterval(() => void load(), 2000); return () => window.clearInterval(interval); }, [load]);
  const hasActiveDatasetTransfers = datasetTransfers.some((transfer) => activeTransferStatuses.has(transfer.status));
  useEffect(() => {
    void refreshDatasetTransfers(false);
    const interval = window.setInterval(() => void refreshDatasetTransfers(true), hasActiveDatasetTransfers ? 2000 : 10000);
    return () => window.clearInterval(interval);
  }, [hasActiveDatasetTransfers, refreshDatasetTransfers]);
  useEffect(() => {
    if (!datasetTransferNotice) return;
    setDatasetTransferNoticeClosing(false);
    const closingTimer = window.setTimeout(() => setDatasetTransferNoticeClosing(true), 3600);
    const removeTimer = window.setTimeout(() => setDatasetTransferNotice(null), 3900);
    return () => { window.clearTimeout(closingTimer); window.clearTimeout(removeTimer); };
  }, [datasetTransferNotice]);
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = openTrainingEvents((event) => {
      setEventStreamDisconnected(false);
      if (event.type.startsWith("dataset.")) {
        setDatasetEventRevision((current) => current + 1);
        void refreshDatasetTransfers(true);
      }
      if (event.type === "model.version.artifact.updated") setModelVersionEventRevision((current) => current + 1);
      void load();
    }, 0, () => setEventStreamDisconnected(true));
    return () => source.close();
  }, [load, refreshDatasetTransfers]);
  useEffect(() => {
    if (!deepRunRef) {
      setSelectedRun(null);
      if (pendingNavigationTab.current) {
        setTab(pendingNavigationTab.current);
        pendingNavigationTab.current = null;
      }
      return;
    }
    if (pendingNavigationTab.current) return;
    setTab("runs");
    const listed = runs.find((run) => run.run_ref === deepRunRef);
    if (listed) { setSelectedRun(listed); return; }
    let alive = true;
    void getTrainingRun(deepRunRef).then((run) => { if (alive) { setRuns((current) => [run, ...current.filter((item) => item.run_ref !== run.run_ref)]); setSelectedRun(run); } }).catch((caught) => { if (alive) setError(errorText(caught)); });
    return () => { alive = false; };
  }, [deepRunRef, runs]);
  useEffect(() => {
    if (deepVersionRef) setTab("versions");
  }, [deepVersionRef]);
  const updateRun = useCallback((run: TrainingRun) => { setRuns((current) => [run, ...current.filter((item) => item.run_ref !== run.run_ref)]); setSelectedRun((current) => current?.run_ref === run.run_ref ? run : current); }, []);
  const selectRun = useCallback((run: TrainingRun | null) => { setSelectedRun(run); navigate(run ? `/model/runs/${encodeURIComponent(run.run_ref)}` : "/model"); }, [navigate]);
  const changeTab = useCallback((nextTab: TrainingTab) => {
    setTab(nextTab);
    if (nextTab !== "runs" && deepRunRef) {
      pendingNavigationTab.current = nextTab;
      setSelectedRun(null);
      navigate("/model");
    }
    if (nextTab !== "versions" && deepVersionRef) navigate("/model");
  }, [deepRunRef, deepVersionRef, navigate]);
  const openModelEditor = useCallback((mode: ModelEditorRequest["mode"], familyRef?: string) => {
    modelEditorSequence.current += 1;
    setModelEditorRequest({ requestId: modelEditorSequence.current, mode, familyRef });
    setTab("models");
    if (deepRunRef || deepVersionRef) navigate("/model");
  }, [deepRunRef, deepVersionRef, navigate]);
  const visibleDatasetTransfers = datasetTransfers
    .filter((transfer) => trackedTransferRefs.has(transfer.transfer_ref))
    .sort((left, right) => Number(activeTransferStatuses.has(right.status)) - Number(activeTransferStatuses.has(left.status)) || (right.updated_at ?? right.created_at ?? "").localeCompare(left.updated_at ?? left.created_at ?? ""));
  if (!capabilities && !error) return <LoadingCard />;
  return (
    <section className="w-full space-y-5 px-4 py-3 md:px-6 xl:px-8">
      <div className="border-b border-console-line">
        <TrainingSectionTabs value={tab === "new" || tab === "models" ? "runs" : tab} onChange={changeTab} />
      </div>

      {eventStreamDisconnected ? <div role="status" className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800"><AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />事件流已断开，正在使用轮询恢复。</div> : null}
      {error ? <div className="flex flex-col gap-3 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><p role="alert" className="text-sm text-rose-700">{error}</p><ConsoleButton className="shrink-0" onClick={() => void load()}><RefreshCw className="h-4 w-4" />重新加载</ConsoleButton></div> : null}
      {datasetTransferNotice ? <div key={datasetTransferNotice.id} role="status" aria-live="polite" data-phase={datasetTransferNoticeClosing ? "closing" : "open"} className={cn("training-data-toast fixed left-1/2 top-4 z-[100] flex w-[min(34rem,calc(100vw-2rem))] items-center gap-2 rounded-lg border px-4 py-3 text-sm shadow-lg", datasetTransferNotice.tone === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : datasetTransferNotice.tone === "danger" ? "border-rose-200 bg-rose-50 text-rose-800" : datasetTransferNotice.tone === "neutral" ? "border-slate-200 bg-white text-slate-700" : "border-sky-200 bg-sky-50 text-sky-800")}><UploadCloud className="h-4 w-4 shrink-0" aria-hidden="true" /><span>{datasetTransferNotice.message}</span></div> : null}
      <TrainingDatasetTransferMonitor transfers={visibleDatasetTransfers} error={datasetTransferError} onPause={(transfer) => void handlePauseDatasetTransfer(transfer)} onCancel={(transfer) => void handleCancelDatasetTransfer(transfer)} onRetry={(transfer) => void handleRetryDatasetTransfer(transfer)} />

      <div id="training-platform-panel-runs" role="tabpanel" aria-labelledby="training-platform-tab-runs" hidden={tab !== "runs"}>
        <RunsPanel runs={runs} selectedRun={selectedRun} canStop={can(capabilities, "training:stop_runs")} canCreate={can(capabilities, "training:create_runs")} query={runQuery} statusFilter={runStatusFilter} page={runCursorHistory.length} hasPreviousPage={runCursorHistory.length > 1} hasNextPage={Boolean(runNextAfter)} onQueryChange={setRunQuery} onStatusFilterChange={setRunStatusFilter} onPreviousPage={() => setRunCursorHistory((current) => current.length > 1 ? current.slice(0, -1) : current)} onNextPage={() => { if (runNextAfter) setRunCursorHistory((current) => [...current, runNextAfter]); }} onCreate={() => changeTab("new")} onSelect={selectRun} onRunChange={updateRun} />
      </div>
      <div id="training-platform-panel-new" role="tabpanel" aria-labelledby="training-platform-tab-new" hidden={tab !== "new"}>
        <NewRunPanel models={models} servers={servers} nodes={nodes} resourcesByServer={resourcesByServer} capabilities={capabilities} canCreate={can(capabilities, "training:create_runs")} datasetEventRevision={datasetEventRevision} datasetTransfers={datasetTransfers} preferredFamilyRef={preferredFamilyRef} onCancel={() => changeTab("runs")} onCreateModel={() => openModelEditor("create")} onEditModel={(familyRef) => openModelEditor("edit", familyRef)} onTransfersCreated={handleTransfersCreated} onCreated={(run) => { setModels((current) => current.map((model) => model.family_ref === run.family_ref ? { ...model, trained_version_count: model.trained_version_count + 1 } : model)); updateRun(run); setTab("runs"); selectRun(run); }} />
      </div>
      <div id="training-platform-panel-versions" role="tabpanel" aria-labelledby="training-platform-tab-versions" hidden={tab !== "versions"}>
        <TrainingModelVersions
          active={tab === "versions"}
          versionRef={deepVersionRef}
          canInspect={can(capabilities, "training:create_runs")}
          eventRevision={modelVersionEventRevision}
          onOpenVersion={(versionRef) => navigate(`/model/versions/${encodeURIComponent(versionRef)}`)}
          onBack={() => navigate("/model")}
          onOpenRun={(runRef) => { setTab("runs"); navigate(`/model/runs/${encodeURIComponent(runRef)}`); }}
        />
      </div>
      <div id="training-platform-panel-data" role="tabpanel" aria-labelledby="training-platform-tab-data" hidden={tab !== "data"}>
        <TrainingDataReviewPanel active={tab === "data"} />
      </div>
      <div id="training-platform-panel-models" role="region" aria-label="模型族参数配置" hidden={tab !== "models"}>
        <ModelsPanel models={models} servers={servers} canManage={can(capabilities, "training:manage_models")} active={tab === "models"} request={modelEditorRequest} onCancel={() => setTab("new")} onSaved={(model) => setModels((current) => [model, ...current.filter((item) => item.family_ref !== model.family_ref)])} onVerified={(model) => { setModels((current) => [model, ...current.filter((item) => item.family_ref !== model.family_ref)]); setPreferredFamilyRef(model.family_ref); setTab("new"); }} />
      </div>
      <div id="training-platform-panel-nodes" role="tabpanel" aria-labelledby="training-platform-tab-nodes" hidden={tab !== "nodes"}>
        <TrainingNodesPanel nodes={nodes} canManage={can(capabilities, "training:manage_nodes")} deploymentEnabled={capabilities?.node_deployment_enabled ?? false} deploymentDisabledReason={capabilities?.node_deployment_disabled_reason} onChanged={(node) => setNodes((current) => [node, ...current.filter((item) => item.node_ref !== node.node_ref)])} onDeleted={(nodeRef) => setNodes((current) => current.filter((item) => item.node_ref !== nodeRef))} onViewResources={() => changeTab("resources")} />
      </div>
      <div id="training-platform-panel-resources" role="tabpanel" aria-labelledby="training-platform-tab-resources" hidden={tab !== "resources"}>
        <ResourcesPanel servers={servers} resourcesByServer={resourcesByServer} resourceErrors={resourceErrors} onRefresh={() => void load()} />
      </div>
    </section>
  );
}
