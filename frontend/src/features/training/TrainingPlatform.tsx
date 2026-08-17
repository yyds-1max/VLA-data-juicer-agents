import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BookOpen,
  ChevronDown,
  CircleHelp,
  FileText,
  Cpu,
  HardDrive,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import {
  createTrainingModel,
  createTrainingRun,
  getTrainingCapabilities,
  getTrainingRun,
  getTrainingRunLogs,
  getTrainingRunMetrics,
  getTrainingServerResources,
  listTrainingModels,
  listTrainingNodes,
  listTrainingRuns,
  listTrainingServers,
  openTrainingEvents,
  previewTrainingRun,
  stopTrainingRun,
  updateTrainingModel,
  verifyTrainingModel,
} from "../../api/client";
import type { TrainingCapabilities, TrainingGpuResource, TrainingMetricSample, TrainingModel, TrainingNode, TrainingParameterDefinition, TrainingRun, TrainingRunLog, TrainingRunPreview, TrainingServer, TrainingServerResources, TrainingStageInputSource } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { ProgressBar } from "../../components/console/ProgressBar";
import { StatusTag } from "../../components/console/StatusTag";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
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
import { TrainingOperationFeedback, type TrainingOperationState } from "./TrainingOperationFeedback";
import { TrainingOperationDialog } from "./TrainingOperationDialog";

type TrainingTab = "runs" | "new" | "models" | "nodes" | "resources";
const tabs = [
  { id: "runs", label: "训练任务" },
  { id: "models", label: "模型注册" }, { id: "nodes", label: "训练节点" }, { id: "resources", label: "服务器资源" },
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
  queued: { label: "排队中", tone: "neutral" }, preparing: { label: "准备中", tone: "info" }, running: { label: "模拟训练中", tone: "purple" },
  stop_requested: { label: "停止中", tone: "warning" }, succeeded: { label: "已完成", tone: "success" }, failed: { label: "失败", tone: "danger" }, cancelled: { label: "已取消", tone: "neutral" }, lost: { label: "状态丢失", tone: "danger" },
};
const modelStatusMeta: Record<TrainingModel["status"], { label: string; tone: StatusTone }> = {
  draft: { label: "草稿", tone: "warning" },
  verified: { label: "已验证", tone: "success" },
  disabled: { label: "已停用", tone: "neutral" },
};
const stageStatusLabels = { pending: "等待中", preparing: "准备中", running: "训练中", succeeded: "已完成", failed: "失败", cancelled: "已取消", skipped: "已跳过", lost: "状态丢失" } as const;

function errorText(error: unknown) {
  // Keep this structural so app-level tests can supply a narrow API mock.
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
function formatNumber(value: number | undefined, digits = 4) { return value === undefined ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: digits }); }
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

function TrainingOverviewMetrics({ runs, gpus }: { runs: TrainingRun[]; gpus: TrainingGpuResource[] }) {
  // 总览只从现有任务与资源投影派生，避免展示层维护另一套训练状态口径。
  const metrics = [
    { label: "运行中", value: runs.filter((run) => activeStatuses.has(run.status)).length, hint: "含排队与准备任务" },
    { label: "排队中", value: runs.filter((run) => run.status === "queued").length, hint: "等待资源调度" },
    { label: "已完成", value: runs.filter((run) => run.status === "succeeded").length, hint: "当前任务总览" },
    { label: "可用 GPU", value: gpus.filter((gpu) => !gpu.externally_occupied && !gpu.lease_run_ref).length, hint: `共 ${gpus.length} 张已纳管` },
  ];

  return (
    <section aria-label="训练概览" className="grid border-y border-console-line sm:grid-cols-2 xl:grid-cols-4">
      {metrics.map((metric, index) => (
        <article key={metric.label} className={cn("min-w-0 px-5 py-4", index > 0 && "sm:border-l sm:border-console-line", index === 2 && "sm:border-l-0 xl:border-l", index > 1 && "border-t border-console-line xl:border-t-0")}>
          <p className="text-xs font-medium text-console-muted">{metric.label}</p>
          <div className="mt-1 flex items-end gap-2">
            <strong className="text-2xl font-semibold tabular-nums text-console-text">{metric.value}</strong>
            <span className="pb-0.5 text-xs text-console-muted">{metric.hint}</span>
          </div>
        </article>
      ))}
    </section>
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
  const datasetDefinitions = definitions.filter((parameter) => parameter.semantic_role === "dataset");
  const hyperparameterDefinitions = definitions.filter((parameter) => parameter.semantic_role !== "dataset");
  const groups = usedTrainingParameterGroups(hyperparameterDefinitions);
  const commonGroup = groups.find((group) => group.key === "common");
  const common = commonGroup ? hyperparameterDefinitions.filter((parameter) => trainingParameterGroupFor(parameter).key === commonGroup.key) : [];
  const foldedGroups = groups.filter((group) => group.key !== "common").map((group) => ({ ...group, definitions: hyperparameterDefinitions.filter((parameter) => trainingParameterGroupFor(parameter).key === group.key) }));
  return <div className="space-y-4">
    {datasetDefinitions.length ? <section aria-label="训练数据集" className="rounded-md border border-sky-200 bg-sky-50/60 p-3"><div className="mb-3"><h3 className="text-sm font-medium text-console-text">训练数据集</h3><p className="text-xs text-console-muted">该输入由模型注册时标记，当前填写数据集标识；后续将接入已发布数据版本选择器。</p></div><ParameterFields definitions={datasetDefinitions} values={values} onChange={onChange} enabledParameterKeys={enabledParameterKeys} disabled={disabled} /></section> : null}
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

function NewRunPanel({ models, servers, resourcesByServer, canCreate, onCancel, onCreated }: { models: TrainingModel[]; servers: TrainingServer[]; resourcesByServer: Record<string, TrainingServerResources>; canCreate: boolean; onCancel: () => void; onCreated: (run: TrainingRun) => void }) {
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
  const selectedFamilyRef = availableModels.some((item) => item.family_ref === familyRef) ? familyRef : availableModels[0]?.family_ref ?? "";
  const model = availableModels.find((item) => item.family_ref === selectedFamilyRef);
  const definitions = model?.configuration?.parameter_definitions ?? [];
  const stageInput = definitions.find((item) => item.semantic_role === "stage_input");
  const modelServerRef = model?.configuration?.launch_template?.server_ref;
  const selectedServer = modelServerRef || serverRef || servers[0]?.server_ref || "";
  const selectedServerRecord = servers.find((server) => server.server_ref === selectedServer);
  const simulationTarget = selectedServerRecord?.kind === "simulation";
  const selectedResources = resourcesByServer[selectedServer];
  const gpus = selectedResources?.gpus ?? [];
  const defaultValues = useMemo(() => Object.fromEntries(definitions.map((item) => [item.key, item.default])), [definitions]);

  useEffect(() => { if (selectedFamilyRef !== familyRef) setFamilyRef(selectedFamilyRef); }, [familyRef, selectedFamilyRef]);
  useEffect(() => {
    const nextServerRef = modelServerRef || servers[0]?.server_ref || "";
    setServerRef(nextServerRef);
    setGpuIds([]);
    setStages([{ parameters: defaultValues, stage_input_source: "manual" }]);
    setActiveStageIndex(0);
    setPreview(null);
  }, [selectedFamilyRef, model?.edit_revision, modelServerRef]);

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
    stages: stages.map((stage, index) => ({
      stage_input_source: index === 0 ? "manual" as const : stage.stage_input_source,
      parameters: Object.fromEntries(stageValidation[index].enabled.filter((item) => !isUnchangedSensitiveMask(item, stage.parameters[item.key])).map((item) => [item.key, stage.parameters[item.key] ?? item.default])),
    })),
  });
  const previewPayload = () => ({ ...payloadBase(), execution_mode: simulationTarget ? "simulation" as const : "real" as const });
  const simulationPayload = () => ({ ...payloadBase(), execution_mode: "simulation" as const });
  const doPreview = async () => {
    if (!selectedFamilyRef || !selectedServer || !gpuIds.length) return setMessage("请选择模型、服务器和至少一张可用 GPU。");
    if (hasParameterErrors) return setMessage("请先修正各训练阶段中标红的参数。");
    setBusy(true); setMessage(null); setOperation({ status: "loading", title: "正在生成训练预览", detail: `逐项校验 ${stages.length} 个训练阶段并生成安全 argv。`, steps: ["校验资源", "校验参数", "生成 RunSpec"], activeStep: 1 });
    try { setPreview(await previewTrainingRun(previewPayload())); setOperation({ status: "success", title: "训练预览已生成", detail: "请核对各阶段命令和输出目录；预览不会启动任何进程。" }); } catch (error) { const detail = errorText(error); setMessage(detail); setOperation({ status: "error", title: "生成训练预览失败", detail }); } finally { setBusy(false); }
  };
  const start = async () => {
    if (!preview || !simulationTarget) return;
    setBusy(true); setMessage(null); setOperation({ status: "loading", title: "正在创建训练任务", detail: `创建一个模型版本并准备顺序执行 ${stages.length} 个阶段。`, steps: ["校验资源", "创建模型版本", "提交训练任务"], activeStep: 1 });
    try { onCreated(await createTrainingRun(simulationPayload())); } catch (error) { const detail = errorText(error); setMessage(detail); setOperation({ status: "error", title: "创建训练任务失败", detail }); } finally { setBusy(false); }
  };
  const activeStage = stages[activeStageIndex];
  const activeValidation = stageValidation[activeStageIndex];
  const previousOutputPreview = activeStageIndex > 0 ? preview?.stages[activeStageIndex - 1]?.output_directory : null;

  return <div className="space-y-4">
    <header className="flex flex-col gap-3 border-b border-console-line pb-5 lg:flex-row lg:items-end lg:justify-between"><div><button type="button" className="mb-2 text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={onCancel}>← 返回训练任务</button><h2 className="text-xl font-semibold text-console-text">新建训练任务</h2><p className="mt-1 text-sm text-console-muted">一次任务生成一个模型版本；可添加多个阶段并按顺序执行。</p></div><ol className="flex max-w-full flex-wrap items-center gap-2 text-xs text-console-muted lg:justify-end" aria-label="创建训练步骤">{['选择模型', '配置资源', '分阶段参数', '预检启动'].map((label, index) => <li key={label} className="flex items-center gap-2"><span className={cn("flex h-6 w-6 items-center justify-center rounded-full border text-[11px] font-semibold", index === 0 ? "border-console-cyan bg-blue-50 text-console-cyan" : "border-console-line bg-console-panel text-console-muted")}>{index + 1}</span><span>{label}</span>{index < 3 ? <ArrowRight className="h-3.5 w-3.5 text-slate-300" aria-hidden="true" /> : null}</li>)}</ol></header>
    <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>{simulationTarget ? "真实训练未启用。当前只会创建可重复的多阶段模拟任务。" : "开发预览模式：可选择真实 GPU 并生成 RunSpec；不会启动进程、申请租约、创建任务或模型版本。"}</span></div>
    <TrainingOperationFeedback operation={operation} />
    <ConsoleCard className="shadow-none"><div className="mb-4 flex items-center gap-2"><Play className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">1. 选择模型和资源</h2><p className="text-sm text-console-muted">模型族的当前训练定义决定节点、入口和可设置参数。</p></div></div><div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_18rem]"><div><div className="grid gap-3 md:grid-cols-2"><label className="text-sm text-console-muted">模型族<select aria-label="模型族" className="mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={selectedFamilyRef} onChange={(event) => { setFamilyRef(event.target.value); setGpuIds([]); invalidatePreview(); }}>{availableModels.map((item) => <option key={item.family_ref} value={item.family_ref}>{`${item.family_name}${duplicateFamilyNames.has(item.family_name) ? ` · ${item.family_ref.slice(-6)}` : ""}（已训练 ${item.trained_version_count} 个版本）`}</option>)}</select></label><label className="text-sm text-console-muted">训练节点<TrainingServerSelect ariaLabel="训练节点" servers={selectedServerRecord ? [selectedServerRecord] : []} value={selectedServer} disabled onValueChange={() => undefined} /></label></div><h3 className="mb-2 mt-5 text-sm font-medium text-console-text">选择 GPU（{gpuIds.length} 张）</h3><GpuPicker gpus={gpus} selected={gpuIds} onChange={(ids) => { setGpuIds(ids); invalidatePreview(); }} disabled={!canCreate} />{!simulationTarget ? <p className="mt-3 text-xs text-console-muted">GPU 选择仅用于生成真实训练预览；当前不会占用或租用 GPU。</p> : null}</div><NewRunResourcePanel resources={selectedResources} selectedGpuIds={gpuIds} /></div></ConsoleCard>
    <ConsoleCard><div className="mb-4 flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold text-console-text">2. 配置训练阶段</h2><p className="text-sm text-console-muted">新增阶段会复制前一阶段全部参数；每个阶段仍使用同一份参数定义。</p></div><ConsoleButton variant="ghost" disabled={!canCreate || stages.length >= 10} onClick={addStage}><Plus className="h-4 w-4" />添加训练阶段</ConsoleButton></div>{!stageInput && stages.length > 1 ? <div className="mb-4 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-800">模型族未登记“阶段输入参数”，各阶段的加载路径需要手动填写。</div> : null}<div className="mb-4 flex flex-wrap gap-2" role="tablist" aria-label="训练阶段">{stages.map((_, index) => <div key={index} className="flex items-center"><button type="button" role="tab" aria-selected={activeStageIndex === index} className={cn("h-9 rounded-l-md border px-3 text-sm", activeStageIndex === index ? "border-console-cyan bg-blue-50 text-console-cyan" : "border-console-line bg-console-panel text-console-muted")} onClick={() => setActiveStageIndex(index)}>{stageNames[index]}</button>{index > 0 ? <button type="button" aria-label={`删除${stageNames[index]}`} className="flex h-9 w-8 items-center justify-center rounded-r-md border border-l-0 border-console-line text-console-muted hover:text-rose-600" onClick={() => removeStage(index)}><Trash2 className="h-3.5 w-3.5" /></button> : <span className="h-9 w-1" />}</div>)}</div>{activeStage && activeValidation ? <section aria-label={`${stageNames[activeStageIndex]}参数`} className="rounded-md border border-console-line p-4"><div className="mb-4 flex items-center justify-between"><div><h3 className="font-medium text-console-text">{stageNames[activeStageIndex]}</h3><p className="text-xs text-console-muted">阶段 {activeStageIndex + 1} / {stages.length}</p></div>{activeValidation.errors.length ? <StatusTag tone="danger">{activeValidation.errors.length} 项待修正</StatusTag> : <StatusTag tone="success">参数有效</StatusTag>}</div>{stageInput && activeStageIndex > 0 ? <div className="mb-4 rounded-md border border-console-line bg-console-panel2 p-3"><p className="text-sm font-medium text-console-text">{stageInput.label} <span className="font-mono text-xs text-console-muted">{stageInput.key}</span></p><div className="mt-2 flex flex-wrap gap-4 text-sm"><label><input type="radio" className="mr-2 accent-console-cyan" checked={activeStage.stage_input_source === "previous_stage_output"} onChange={() => updateStage(activeStageIndex, (stage) => ({ ...stage, stage_input_source: "previous_stage_output" }))} />使用上一阶段输出目录</label><label><input type="radio" className="mr-2 accent-console-cyan" checked={activeStage.stage_input_source === "manual"} onChange={() => updateStage(activeStageIndex, (stage) => ({ ...stage, stage_input_source: "manual" }))} />手动填写</label></div>{activeStage.stage_input_source === "previous_stage_output" ? <input aria-label="上一阶段输出目录" className="mt-2 h-9 w-full cursor-not-allowed rounded-md border border-console-line bg-slate-100 px-2 font-mono text-sm text-console-muted" disabled value={previousOutputPreview ?? "生成预览后显示上一阶段输出目录"} /> : null}</div> : null}<GroupedParameterFields definitions={activeStage.stage_input_source === "previous_stage_output" && activeStageIndex > 0 ? definitions.filter((item) => item.key !== stageInput?.key) : definitions} values={activeStage.parameters} onChange={(key, value) => updateStage(activeStageIndex, (stage) => ({ ...stage, parameters: { ...stage.parameters, [key]: value } }))} enabledParameterKeys={activeValidation.enabledKeys} disabled={!canCreate} /></section> : null}</ConsoleCard>
    <ConsoleCard><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-console-text">3. 校验并预览 RunSpec</h2><p className="text-sm text-console-muted">逐阶段显示安全 argv 和输出目录；预览不创建模型版本。</p></div><ConsoleButton variant="ghost" aria-busy={busy} onClick={() => void doPreview()} disabled={!canCreate || busy || !models.length || hasParameterErrors}>{busy ? <RefreshCw className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <RefreshCw className="h-4 w-4" />}{busy ? "正在生成…" : "生成预览"}</ConsoleButton></div>{message && !operation ? <p role="alert" className="mt-3 text-sm text-rose-700">{message}</p> : null}{preview ? <div className="mt-4 space-y-2">{preview.stages.map((stage) => <details key={stage.stage_number} className="rounded-md border border-console-line bg-console-panel2 px-3 py-2" open={stage.stage_number === 1}><summary className="cursor-pointer text-sm font-medium text-console-text">{stage.stage_name} · {stage.output_directory}</summary><div className="mt-3 rounded-md bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100 break-all">{stage.command_preview}</div><div className="mt-3 grid gap-2 md:grid-cols-3"><span className="text-sm text-console-muted">nproc_per_node：<b className="text-console-text">{stage.run_spec.nproc_per_node}</b></span><span className="text-sm text-console-muted">GPU：<b className="text-console-text">{stage.run_spec.gpu_uuids.length}</b></span><span className="text-sm text-console-muted">端口：<b className="text-console-text">{stage.run_spec.master_port ?? "不需要"}</b></span></div>{stage.preflight.map((item, index) => <p key={index} className={cn("mt-2 text-sm", item.ok ? "text-emerald-700" : "text-rose-700")}>{item.ok ? "✓" : "×"} {item.message}</p>)}</details>)}</div> : null}</ConsoleCard>
    <ConsoleCard><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-console-text">4. {simulationTarget ? "启动模拟训练" : "启动真实训练"}</h2><p className="text-sm text-console-muted">{simulationTarget ? `将创建一个模型版本，并顺序执行 ${stages.length} 个训练阶段；GPU 与端口会覆盖整个任务周期。` : "真实 Runner 接入后才会创建任务、模型版本和资源租约；当前预览不会执行任何训练命令。"}</p></div><ConsoleButton variant="primary" onClick={() => void start()} disabled={!canCreate || busy || !preview || !simulationTarget}><Play className="h-4 w-4" />{simulationTarget ? "启动模拟训练" : "真实训练未启用"}</ConsoleButton></div></ConsoleCard>
  </div>;
}

function RunDetail({ run, canStop, onRunChange }: { run: TrainingRun; canStop: boolean; onRunChange: (run: TrainingRun) => void }) {
  const [logs, setLogs] = useState<TrainingRunLog[]>([]); const [metrics, setMetrics] = useState<TrainingMetricSample[]>([]); const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const [selectedStageRef, setSelectedStageRef] = useState("");
  const lastLogSeq = useRef(0); const lastMetricSeq = useRef(0);
  useEffect(() => { setLogs([]); setMetrics([]); lastLogSeq.current = 0; lastMetricSeq.current = 0; setError(null); setSelectedStageRef(""); }, [run.run_ref]);
  useEffect(() => { let alive = true; const load = async () => { try { const [nextRun, nextLogs, nextMetrics] = await Promise.all([getTrainingRun(run.run_ref), getTrainingRunLogs(run.run_ref, lastLogSeq.current), getTrainingRunMetrics(run.run_ref, lastMetricSeq.current)]); if (!alive) return; onRunChange(nextRun); if (nextLogs.length) { lastLogSeq.current = Math.max(lastLogSeq.current, ...nextLogs.map((item) => item.seq)); setLogs((current) => [...current, ...nextLogs.filter((item) => !current.some((known) => known.seq === item.seq))].sort((a, b) => a.seq - b.seq)); } if (nextMetrics.length) { lastMetricSeq.current = Math.max(lastMetricSeq.current, ...nextMetrics.map((item) => item.seq)); setMetrics((current) => [...current, ...nextMetrics.filter((item) => !current.some((known) => known.seq === item.seq))].sort((a, b) => a.seq - b.seq)); } } catch (caught) { if (alive) setError(errorText(caught)); } }; void load(); const interval = window.setInterval(() => void load(), activeStatuses.has(run.status) ? 2000 : 8000); return () => { alive = false; window.clearInterval(interval); }; }, [run.run_ref, run.status, onRunChange]);
  const selectedStage = run.stages.find((stage) => stage.stage_ref === selectedStageRef) ?? run.stages.find((stage) => stage.stage_number === run.current_stage_number) ?? run.stages[0];
  const stageMetrics = selectedStage ? metrics.filter((item) => !item.stage_ref || item.stage_ref === selectedStage.stage_ref) : metrics;
  const stageLogs = selectedStage ? logs.filter((item) => !item.stage_ref || item.stage_ref === selectedStage.stage_ref) : logs;
  const stop = async () => { if (!canStop || !window.confirm("确定停止此训练任务吗？当前阶段和所有尚未执行的阶段都会取消，已写入的指标和日志会保留。")) return; setBusy(true); try { onRunChange(await stopTrainingRun(run.run_ref, run.state_revision)); } catch (caught) { setError(errorText(caught)); } finally { setBusy(false); } };
  const lossData = stageMetrics.length ? { labels: stageMetrics.map((item) => String(item.step)), data: stageMetrics.map((item) => item.loss), label: "Loss", color: "#6d5bd0" } : null;
  const lrData = stageMetrics.length ? { labels: stageMetrics.map((item) => String(item.step)), data: stageMetrics.map((item) => item.learning_rate), label: "Learning rate", color: "#b7791f" } : null;
  const gpuUtilizationData = stageMetrics.some((item) => item.gpu_utilization_percent != null) ? { labels: stageMetrics.map((item) => String(item.step)), data: stageMetrics.map((item) => item.gpu_utilization_percent ?? 0), label: "GPU 利用率", color: "#0284c7" } : null;
  const gpuMemoryData = stageMetrics.some((item) => item.gpu_memory_mib != null) ? { labels: stageMetrics.map((item) => String(item.step)), data: stageMetrics.map((item) => item.gpu_memory_mib ?? 0), label: "GPU 显存 MiB", color: "#059669" } : null;
  return <div className="space-y-4"><ConsoleCard className="shadow-none"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2"><h2 className="text-lg font-semibold text-console-text">{runModelDisplayName(run)}</h2><StatusTag tone={statusMeta[run.status].tone}>{statusMeta[run.status].label}</StatusTag></div><p className="mt-1 text-sm text-console-muted">任务 {run.run_ref}</p></div>{canStop && activeStatuses.has(run.status) ? <ConsoleButton variant="ghost" onClick={() => void stop()} disabled={busy}><Square className="h-4 w-4" />停止任务</ConsoleButton> : null}</div>{error ? <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p> : null}<div className="mt-5 grid divide-y divide-console-line border-y border-console-line sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-4"><div className="py-3 sm:px-4 sm:first:pl-0"><p className="text-xs text-console-muted">Epoch</p><p className="text-xl font-semibold tabular-nums text-console-text">{run.current_epoch}/{run.total_epochs}</p></div><div className="py-3 sm:px-4"><p className="text-xs text-console-muted">Step</p><p className="text-xl font-semibold tabular-nums text-console-text">{run.current_step}/{run.total_steps}</p></div><div className="py-3 sm:px-4"><p className="text-xs text-console-muted">最新 Loss</p><p className="text-xl font-semibold tabular-nums text-console-text">{formatNumber(run.latest_metric?.loss)}</p></div><div className="py-3 sm:px-4"><p className="text-xs text-console-muted">学习率</p><p className="text-xl font-semibold tabular-nums text-console-text">{formatNumber(run.latest_metric?.learning_rate, 7)}</p></div></div><ProgressBar className="mt-4" value={run.progress_percent} tone="purple" label={`训练进度 ${run.progress_percent.toFixed(1)}%`} /></ConsoleCard>
    <ConsoleCard><div className="flex flex-wrap gap-2" role="tablist" aria-label="任务训练阶段">{run.stages.map((stage) => <button key={stage.stage_ref} type="button" role="tab" aria-selected={selectedStage?.stage_ref === stage.stage_ref} className={cn("rounded-md border px-3 py-2 text-sm", selectedStage?.stage_ref === stage.stage_ref ? "border-console-cyan bg-blue-50 text-console-cyan" : "border-console-line text-console-muted")} onClick={() => setSelectedStageRef(stage.stage_ref)}>{stage.stage_name} · {stageStatusLabels[stage.status]}</button>)}</div>{selectedStage ? <><div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm text-console-muted"><span>阶段进度 <b className="text-console-text">{(selectedStage.progress_percent ?? (selectedStage.progress ?? 0) * 100).toFixed(1)}%</b></span><span>输出目录 <b className="font-mono text-console-text">{selectedStage.output_directory ?? "尚未生成"}</b></span></div>{selectedStage.failure_message ? <div role="alert" className="mt-3 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"><span className="font-medium">阶段失败：</span>{selectedStage.failure_message}</div> : null}</> : null}{run.version_model ? <div className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"><span className="font-medium">版本模型：</span><span className="font-mono">{run.version_model.output_directory}</span></div> : null}</ConsoleCard>
    <div className="grid gap-4 xl:grid-cols-2">{lossData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">{selectedStage?.stage_name} · Loss</h3><MiniChart type="line" title="Loss" data={lossData} /></ConsoleCard> : <ConsoleCard><p className="text-sm text-console-muted">当前阶段暂无指标。</p></ConsoleCard>}{lrData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">学习率</h3><MiniChart type="line" title="学习率" data={lrData} /></ConsoleCard> : null}</div>
    <div className="grid gap-4 xl:grid-cols-2">{gpuUtilizationData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">GPU 利用率</h3><MiniChart type="line" title="GPU 利用率" data={gpuUtilizationData} /></ConsoleCard> : null}{gpuMemoryData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">GPU 显存</h3><MiniChart type="line" title="GPU 显存" data={gpuMemoryData} /></ConsoleCard> : null}</div>
    <div className="grid gap-4 xl:grid-cols-2"><ConsoleCard><h3 className="mb-3 font-semibold text-console-text">{selectedStage?.stage_name}参数快照</h3><dl className="grid gap-2 text-sm sm:grid-cols-2">{Object.entries(selectedStage?.parameters ?? {}).map(([key, value]) => <div key={key} className="rounded-md bg-console-panel2 p-2"><dt className="text-console-muted">{key}</dt><dd className="mt-1 font-mono text-console-text">{String(value)}</dd></div>)}{!Object.keys(selectedStage?.parameters ?? {}).length ? <p className="text-console-muted">无可展示参数。</p> : null}</dl></ConsoleCard><ConsoleCard><h3 className="mb-3 font-semibold text-console-text">审计摘要</h3><div className="space-y-2 text-sm">{run.audit_events?.map((event, index) => <div key={`${event.created_at}-${index}`} className="rounded-md bg-console-panel2 p-2"><p className="font-medium text-console-text">{event.action}</p><p className="mt-1 text-console-muted">{event.summary} · {event.created_at}</p></div>)}{!run.audit_events?.length ? <p className="text-console-muted">暂无审计事件。</p> : null}</div></ConsoleCard></div>
    <ConsoleCard><div className="mb-3 flex items-center gap-2"><Terminal className="h-4 w-4 text-console-cyan" /><h3 className="font-semibold text-console-text">{selectedStage?.stage_name}训练日志</h3></div><div className="max-h-64 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100">{stageLogs.length ? stageLogs.map((item) => <p key={item.seq}><span className="text-slate-500">[{item.seq}]</span> {item.message}</p>) : "等待日志…"}</div></ConsoleCard>
  </div>;
}

function RunsPanel({ runs, selectedRun, canStop, canCreate, onCreate, onSelect, onRunChange }: { runs: TrainingRun[]; selectedRun: TrainingRun | null; canStop: boolean; canCreate: boolean; onCreate: () => void; onSelect: (run: TrainingRun | null) => void; onRunChange: (run: TrainingRun) => void }) {
  const [statusFilter, setStatusFilter] = useState<"all" | TrainingRun["status"]>("all");
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  // 搜索和筛选只改变当前表格的呈现，完整任务投影仍用于状态统计和深链详情。
  const filteredRuns = runs.filter((run) => {
    if (statusFilter !== "all" && run.status !== statusFilter) return false;
    if (!normalizedQuery) return true;
    return [runModelDisplayName(run), run.family_name, run.version_ref, run.run_ref, run.server_ref]
      .some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
  });

  if (selectedRun) {
    return (
      <div>
        <ConsoleButton variant="ghost" className="mb-3" onClick={() => onSelect(null)}>返回任务列表</ConsoleButton>
        <RunDetail run={selectedRun} canStop={canStop} onRunChange={onRunChange} />
      </div>
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
              onChange={(event) => setQuery(event.target.value)}
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
              onChange={(event) => setStatusFilter(event.target.value as "all" | TrainingRun["status"])}
            >
              <option value="all">全部状态</option>
              {Object.entries(statusMeta).map(([status, meta]) => <option key={status} value={status}>{meta.label}</option>)}
            </select>
          </label>
        </div>
      </header>

      <div className="overflow-x-auto border-t border-console-line">
        <table className="w-full min-w-[980px] table-fixed text-left text-sm">
          <thead className="bg-console-panel2 text-xs font-medium text-console-muted">
            <tr>
              <th className="w-[24%] px-4 py-3">任务 / 模型</th>
              <th className="w-[13%] px-4 py-3">服务器 / GPU</th>
              <th className="w-[23%] px-4 py-3">训练进度</th>
              <th className="w-[11%] px-4 py-3">最新 Loss</th>
              <th className="w-[11%] px-4 py-3">状态</th>
              <th className="w-[12%] px-4 py-3">更新时间</th>
              <th className="w-[6%] px-4 py-3 text-right">操作</th>
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
                  <span className="block truncate text-console-text">{run.server_ref}</span>
                  <span className="mt-1 block text-xs">GPU {run.gpu_uuids.length} 张</span>
                </td>
                <td className="px-4 py-3.5 align-middle">
                  <div className="flex items-center justify-between gap-3 text-xs">
                    <span className="text-console-muted">阶段 {run.current_stage_number ?? "-"}/{run.stage_count} · Epoch {run.current_epoch}/{run.total_epochs}</span>
                    <span className="tabular-nums text-console-text">{run.progress_percent.toFixed(1)}%</span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-label={`${runModelDisplayName(run)} 训练进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={run.progress_percent}>
                    <span className="block h-full rounded-full bg-console-cyan transition-[width] duration-200 motion-reduce:transition-none" style={{ width: `${Math.max(0, Math.min(100, run.progress_percent))}%` }} />
                  </div>
                </td>
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
          <p className="mt-3 text-sm font-medium text-console-text">{runs.length ? "没有符合筛选条件的任务" : "还没有训练任务"}</p>
          {runs.length ? <p className="mt-1 text-sm text-console-muted">请调整搜索内容或状态筛选。</p> : <><p className="mt-1 text-sm text-console-muted">选择已登记模型和 GPU，创建第一项训练。</p>{canCreate ? <ConsoleButton className="mt-4" variant="primary" onClick={onCreate}><Plus className="h-4 w-4" />新建训练任务</ConsoleButton> : null}</>}
        </div>
      ) : null}
    </section>
  );
}

function ModelFamilyCard({ model, servers, canManage, verifying, onEdit, onVerify }: { model: TrainingModel; servers: TrainingServer[]; canManage: boolean; verifying: boolean; onEdit: () => void; onVerify: () => void }) {
  const serverRef = model.configuration?.launch_template?.server_ref;
  const server = servers.find((item) => item.server_ref === serverRef);
  const usesRealWorker = server?.kind === "training_node";
  const nodeOnline = usesRealWorker && server?.status === "online" && server.available !== false;
  const verification = model.verification;
  const verificationActive = verification?.status === "queued" || verification?.status === "running";
  const verificationLabel = verification?.status === "queued" ? "等待 Worker" : verification?.status === "running" ? "正在验证" : verification?.status === "succeeded" ? "验证通过" : verification?.status === "failed" ? "验证未通过" : null;
  const parameterCount = model.configuration?.parameter_definitions.length ?? 0;
  const template = model.configuration?.launch_template;
  return <article className="rounded-lg border border-console-line bg-console-panel p-4 transition-[border-color,box-shadow] duration-150 hover:border-console-cyan/30 hover:shadow-sm motion-reduce:transition-none">
    <div className="flex items-center justify-between gap-2"><p className="font-medium text-console-text">{model.family_name}</p><StatusTag tone={modelStatusMeta[model.status].tone}>{modelStatusMeta[model.status].label}</StatusTag></div>
    <p className="mt-1 text-sm text-console-muted">已训练 {model.trained_version_count} 个模型版本 · 当前训练定义</p>
    <dl className="mt-3 grid grid-cols-2 gap-2 rounded-md bg-console-panel2 p-3 text-xs"><div><dt className="text-console-muted">训练参数</dt><dd className="mt-1 font-medium text-console-text">{parameterCount} 个</dd></div><div><dt className="text-console-muted">运行环境</dt><dd className="mt-1 truncate font-medium text-console-text">{template?.runtime_environment?.kind === "conda" ? `Conda · ${template.runtime_environment.conda_environment}` : "Worker 系统环境"}</dd></div><div className="col-span-2"><dt className="text-console-muted">训练入口</dt><dd className="mt-1 truncate font-mono text-console-text" title={template?.entrypoint}>{template?.entrypoint ?? "--"}</dd></div></dl>
    {verificationLabel ? <div className={cn("mt-2 rounded-md border px-2.5 py-2 text-xs", verification?.status === "succeeded" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : verification?.status === "failed" ? "border-rose-200 bg-rose-50 text-rose-800" : "border-sky-200 bg-sky-50 text-sky-800")}><p className="font-medium">{verificationLabel}</p>{verification?.checks?.length ? <ul className="mt-1 space-y-1">{verification.checks.map((check) => <li key={check.code}><span className="font-medium">{check.label}：</span>{check.detail}</li>)}</ul> : null}</div> : null}
    <div className="mt-2 flex flex-wrap gap-2">
      {canManage ? <ConsoleButton variant="ghost" onClick={onEdit}>编辑模型配置</ConsoleButton> : null}
      {canManage ? <ConsoleButton variant="ghost" disabled={!nodeOnline || verifying || verificationActive} onClick={onVerify}>{verifying || verificationActive ? "验证中" : "验证配置"}</ConsoleButton> : null}
    </div>
    {canManage && !usesRealWorker ? <p className="mt-1 text-xs text-console-muted">只有绑定已部署 Worker 的真实训练节点后才能验证。</p> : null}
    {canManage && usesRealWorker && !nodeOnline ? <p className="mt-1 text-xs text-console-muted">训练节点在线后才能验证。</p> : null}
  </article>;
}

function ModelsPanel({ models, servers, canManage, active, onSaved }: { models: TrainingModel[]; servers: TrainingServer[]; canManage: boolean; active: boolean; onSaved: (model: TrainingModel) => void }) {
  const [editorOpen, setEditorOpen] = useState(models.length === 0);
  const [editingFamilyRef, setEditingFamilyRef] = useState<string | null>(null);
  const [familyName, setFamilyName] = useState("");
  const [domain, setDomain] = useState(emptyLaunchTemplate.domain); const [serverRef, setServerRef] = useState(servers[0]?.server_ref ?? "");
  const [workingDirectory, setWorkingDirectory] = useState(emptyLaunchTemplate.working_directory); const [executable, setExecutable] = useState(emptyLaunchTemplate.executable);
  const [launcherKind, setLauncherKind] = useState<"torchrun" | "direct">(emptyLaunchTemplate.launcher_kind);
  const [entrypoint, setEntrypoint] = useState(emptyLaunchTemplate.entrypoint); const [fixedArgv, setFixedArgv] = useState(""); const [outputRoot, setOutputRoot] = useState(emptyLaunchTemplate.output_root); const [outputFlag, setOutputFlag] = useState(emptyLaunchTemplate.output_flag);
  const [runtimeKind, setRuntimeKind] = useState<"system" | "conda">(emptyLaunchTemplate.runtime_environment.kind);
  const [condaEnvironment, setCondaEnvironment] = useState("");
  const [monitoringFormat, setMonitoringFormat] = useState<"plain" | "transformers" | "jsonl">(emptyLaunchTemplate.monitoring.format);
  const [parameterDefinitions, setParameterDefinitions] = useState<TrainingParameterDefinition[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const [verifyingFamilyRef, setVerifyingFamilyRef] = useState<string | null>(null);
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
    if (!active || !editorOpen) return;
    const frame = window.requestAnimationFrame(() => editorHeadingRef.current?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, [active, editorOpen]);
  const resetCreateMode = () => {
    setEditingFamilyRef(null); setFamilyName("");
    setDomain(emptyLaunchTemplate.domain); setServerRef(servers[0]?.server_ref ?? ""); setWorkingDirectory(emptyLaunchTemplate.working_directory); setLauncherKind(emptyLaunchTemplate.launcher_kind); setExecutable(emptyLaunchTemplate.executable);
    setEntrypoint(emptyLaunchTemplate.entrypoint); setFixedArgv(""); setOutputRoot(emptyLaunchTemplate.output_root); setOutputFlag(emptyLaunchTemplate.output_flag);
    setRuntimeKind(emptyLaunchTemplate.runtime_environment.kind); setCondaEnvironment(""); setMonitoringFormat(emptyLaunchTemplate.monitoring.format); setParameterDefinitions([]); setError(null);
  };
  const showOperation = (next: TrainingOperationState) => {
    setOperation(next);
    setOperationOpen(true);
  };
  const openCreateMode = () => { resetCreateMode(); setEditorOpen(true); setOperation(null); setOperationOpen(false); };
  const populateFromModel = (model: TrainingModel) => {
    const template = model.configuration?.launch_template;
    if (!template || !model.configuration) { setError("该模型族缺少可编辑的 launch template，请重新加载管理员投影。"); return false; }
    setFamilyName(model.family_name);
    setDomain(template.domain); setServerRef(template.server_ref); setWorkingDirectory(template.working_directory); setLauncherKind(inferLauncherKind(template)); setExecutable(template.executable);
    setEntrypoint(template.entrypoint); setFixedArgv(template.fixed_argv.join("\n")); setOutputRoot(template.output_root); setOutputFlag(template.output_flag ?? "--output_dir");
    setRuntimeKind(template.runtime_environment?.kind ?? "system"); setCondaEnvironment(template.runtime_environment?.conda_environment ?? ""); setMonitoringFormat(template.monitoring?.format ?? "plain");
    setParameterDefinitions(model.configuration.parameter_definitions.map((parameter) => ({ ...structuredClone(parameter), editable: true }))); setError(null);
    return true;
  };
  const edit = (model: TrainingModel) => {
    if (!populateFromModel(model)) return;
    setEditingFamilyRef(model.family_ref);
    setEditorOpen(true);
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
      const configuration = { parameter_definitions: normalizedDefinitions, launch_template: launchTemplate };
      const editingModel = models.find((model) => model.family_ref === editingFamilyRef);
      const saved = editingModel
        ? await updateTrainingModel(editingModel.family_ref, { expected_revision: editingModel.edit_revision ?? 0, configuration })
        : await createTrainingModel({ family_name: familyName.trim(), configuration });
      onSaved(saved);
      showOperation({ status: "loading", title: "配置已保存，正在请求 Worker 验证", detail: "系统会在模型绑定的训练节点上检查目录、入口和运行环境。", steps: ["保存配置", "请求 Worker 验证", "等待验证结果"], activeStep: 1 });
      try {
        const verifying = await verifyTrainingModel(saved.family_ref, saved.edit_revision ?? 0);
        if (verifying) onSaved(verifying);
        showOperation({ status: "success", title: "模型配置已保存，验证任务已提交", detail: "Worker 会继续执行检查；验证结果会自动刷新到模型列表。" });
        setEditorOpen(false);
        resetCreateMode();
      } catch (verifyError) {
        const detail = errorText(verifyError);
        setError(`模型配置已保存，但验证未能启动：${detail}`);
        showOperation({ status: "error", title: "配置已保存，验证未完成", detail: "模型仍保留为草稿，可返回列表后重新验证。" });
        if (editingModel) edit(saved);
      }
    } catch (caught) { const detail = errorText(caught); setError(detail); showOperation({ status: "error", title: editingFamilyRef ? "保存模型配置失败" : "登记模型失败", detail }); } finally { setBusy(false); }
  };
  const loadNavilaPreset = () => {
    setFamilyName("NaVILA 轨迹训练");
    setDomain(navilaTrajectoryLaunchTemplate.domain); setServerRef(servers[0]?.server_ref ?? ""); setWorkingDirectory(navilaTrajectoryLaunchTemplate.working_directory);
    setLauncherKind(navilaTrajectoryLaunchTemplate.launcher_kind); setExecutable(navilaTrajectoryLaunchTemplate.executable); setEntrypoint(navilaTrajectoryLaunchTemplate.entrypoint); setFixedArgv("");
    setOutputRoot(navilaTrajectoryLaunchTemplate.output_root); setOutputFlag(navilaTrajectoryLaunchTemplate.output_flag);
    setRuntimeKind(navilaTrajectoryLaunchTemplate.runtime_environment.kind); setCondaEnvironment(navilaTrajectoryLaunchTemplate.runtime_environment.conda_environment ?? ""); setMonitoringFormat(navilaTrajectoryLaunchTemplate.monitoring.format);
    setParameterDefinitions(structuredClone(navilaTrajectoryParameters)); setError(null);
  };
  const verify = async (model: TrainingModel) => {
    setVerifyingFamilyRef(model.family_ref); setError(null); showOperation({ status: "loading", title: `正在验证 ${model.family_name}`, detail: "已请求 Worker 检查模型配置。" });
    try { const verifying = await verifyTrainingModel(model.family_ref, model.edit_revision ?? 0); if (verifying) onSaved(verifying); showOperation({ status: "success", title: "验证任务已提交", detail: "结果会自动刷新到模型列表。" }); }
    catch (caught) { const detail = errorText(caught); setError(detail); showOperation({ status: "error", title: "验证请求失败", detail }); }
    finally { setVerifyingFamilyRef(null); }
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
    `${renderCommandToken(outputFlag.trim())} <平台生成输出目录>`,
  ].filter(Boolean) : [];
  const formTitle = editingFamilyRef ? `编辑 ${models.find((model) => model.family_ref === editingFamilyRef)?.family_name ?? "模型"}` : "登记新模型";
  if (!editorOpen) return <div className="space-y-5">
    <header className="flex flex-col gap-3 border-b border-console-line pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div><h2 className="text-xl font-semibold text-console-text">模型注册</h2><p className="mt-1 text-sm text-console-muted">管理模型族当前训练定义。每次训练会保留当时的完整配置快照。</p></div>
      {canManage ? <ConsoleButton variant="primary" onClick={openCreateMode}><Plus className="h-4 w-4" />登记新模型</ConsoleButton> : null}
    </header>
    {error ? <p role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p> : null}
    <section aria-labelledby="registered-models-title">
      <div className="mb-3 flex items-center justify-between"><div className="flex items-center gap-2"><BookOpen className="h-5 w-5 text-console-cyan" /><h3 id="registered-models-title" className="font-semibold text-console-text">已登记模型族</h3></div><span className="text-xs text-console-muted">共 {models.length} 个</span></div>
      {models.length ? <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-3">{models.map((model) => <ModelFamilyCard key={model.family_ref} model={model} servers={servers} canManage={canManage} verifying={verifyingFamilyRef === model.family_ref} onEdit={() => { setOperation(null); edit(model); }} onVerify={() => void verify(model)} />)}</div> : <div className="rounded-xl border border-dashed border-console-line py-16 text-center"><BookOpen className="mx-auto h-8 w-8 text-console-muted" /><p className="mt-3 text-sm font-medium text-console-text">尚未登记模型</p><p className="mt-1 text-sm text-console-muted">登记训练入口和参数定义后即可创建训练。</p>{canManage ? <ConsoleButton className="mt-4" variant="primary" onClick={openCreateMode}><Plus className="h-4 w-4" />登记第一个模型</ConsoleButton> : null}</div>}
    </section>
    <TrainingOperationDialog open={operationOpen} operation={operation} onOpenChange={setOperationOpen} />
  </div>;

  return <div className="mx-auto max-w-[1520px] space-y-5">
    <header className="flex flex-col gap-3 border-b border-console-line pb-5 sm:flex-row sm:items-start sm:justify-between">
      <div><button type="button" className="mb-2 text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => { setEditorOpen(false); setError(null); }}>← 返回模型列表</button><h2 ref={editorHeadingRef} tabIndex={-1} className="text-xl font-semibold text-console-text outline-none">{formTitle}</h2><p className="mt-1 text-sm text-console-muted">{editingFamilyRef ? "修改只影响之后创建的训练，历史模型版本保留原配置快照。" : "登记训练入口、参数定义和输出规则；保存后系统会自动发起验证。"}</p></div>
      {!editingFamilyRef ? <ConsoleButton variant="ghost" disabled={!canManage || busy} onClick={loadNavilaPreset}>一键载入 NaVILA 轨迹训练模板</ConsoleButton> : null}
    </header>
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(320px,420px)]">
    <ConsoleCard className="min-w-0 shadow-none">
      <section aria-labelledby="model-basic-config-title"><h3 id="model-basic-config-title" className="font-semibold text-console-text">基础配置</h3><p className="mt-1 text-xs text-console-muted">灰色文字仅为填写示例，不会作为真实配置保存。</p>
        <label className="mt-4 block text-sm text-console-muted">模型族名称<input className={textInput} value={familyName} placeholder="例如 NaVILA 轨迹训练" disabled={!canManage || Boolean(editingFamilyRef)} onChange={(event) => setFamilyName(event.target.value)} /></label>
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
      <div className="sticky bottom-3 mt-5 flex flex-wrap justify-end gap-2 rounded-lg border border-console-line bg-white/95 p-3 shadow-lg backdrop-blur"><ConsoleButton disabled={busy} onClick={() => { setEditorOpen(false); setError(null); }}>取消</ConsoleButton><ConsoleButton variant="primary" aria-busy={busy} disabled={!canManage || busy || !familyName.trim() || !domain.trim() || !serverRef.trim() || !workingDirectory.trim() || !executable.trim() || !entrypoint.trim() || !outputRoot.trim() || !outputFlag.trim()} onClick={() => void save()}>{busy ? <RefreshCw className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Plus className="h-4 w-4" />}{busy ? "正在保存并验证…" : editingFamilyRef ? "保存模型配置" : "登记模型"}</ConsoleButton></div>
    </ConsoleCard>
    <aside className="min-w-0 xl:sticky xl:top-4" aria-label="实时命令摘要">
      <section className="rounded-xl border border-slate-800 bg-slate-950 p-4 shadow-[0_14px_38px_rgba(15,23,42,0.16)]" aria-labelledby="command-summary-title"><div className="flex items-start gap-3"><Terminal className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" aria-hidden="true" /><div><h3 id="command-summary-title" className="text-sm font-semibold text-slate-100">实时结构化命令摘要（默认值）</h3><p className="mt-1 text-xs leading-5 text-slate-400">按当前默认值生成，仅用于核对，不会执行。</p></div></div>{commandSummaryReady ? <pre className="console-soft-scrollbar mt-4 max-h-[calc(100vh-12rem)] min-h-48 overflow-auto whitespace-pre rounded-md bg-slate-900/70 p-3 font-mono text-xs leading-6 text-slate-100"><code>{commandLines.join("\n")}</code></pre> : <div className="mt-4 rounded-md border border-dashed border-slate-700 px-3 py-8 text-center text-xs leading-5 text-slate-400">填写启动程序、训练入口和产物输出参数后显示命令摘要。</div>}</section>
      <p className="mt-3 px-1 text-xs leading-5 text-console-muted">宽屏下摘要会固定在视口内；参数较多时仅滚动摘要区域。</p>
    </aside>
    </div>
    <TrainingOperationDialog open={operationOpen} operation={operation} onOpenChange={setOperationOpen} />
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
  const [tab, setTab] = useState<TrainingTab>("runs"); const [capabilities, setCapabilities] = useState<TrainingCapabilities | null>(null); const [models, setModels] = useState<TrainingModel[]>([]); const [nodes, setNodes] = useState<TrainingNode[]>([]); const [servers, setServers] = useState<TrainingServer[]>([]); const [resourcesByServer, setResourcesByServer] = useState<Record<string, TrainingServerResources>>({}); const [resourceErrors, setResourceErrors] = useState<Record<string, string>>({}); const [runs, setRuns] = useState<TrainingRun[]>([]); const [selectedRun, setSelectedRun] = useState<TrainingRun | null>(null); const [error, setError] = useState<string | null>(null); const [eventStreamDisconnected, setEventStreamDisconnected] = useState(false);
  const pendingNavigationTab = useRef<TrainingTab | null>(null);
  const load = useCallback(async () => {
    try {
      const [nextCapabilities, nextModels, nextNodes, nextServers, nextRuns] = await Promise.all([getTrainingCapabilities(), listTrainingModels(), listTrainingNodes(), listTrainingServers(), listTrainingRuns()]);
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
      })); setServers(nextServers); setResourcesByServer(nextResources); setResourceErrors(nextResourceErrors); setRuns(nextRuns);
      setSelectedRun((current) => current ? nextRuns.find((item) => item.run_ref === current.run_ref) ?? current : null);
      setError(null);
    } catch (caught) { setError(errorText(caught)); }
  }, []);
  useEffect(() => { void load(); const interval = window.setInterval(() => void load(), 2000); return () => window.clearInterval(interval); }, [load]);
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = openTrainingEvents(() => { setEventStreamDisconnected(false); void load(); }, 0, () => setEventStreamDisconnected(true));
    return () => source.close();
  }, [load]);
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
  const updateRun = useCallback((run: TrainingRun) => { setRuns((current) => [run, ...current.filter((item) => item.run_ref !== run.run_ref)]); setSelectedRun((current) => current?.run_ref === run.run_ref ? run : current); }, []);
  const selectRun = useCallback((run: TrainingRun | null) => { setSelectedRun(run); navigate(run ? `/model/runs/${encodeURIComponent(run.run_ref)}` : "/model"); }, [navigate]);
  const changeTab = useCallback((nextTab: TrainingTab) => {
    setTab(nextTab);
    if (nextTab !== "runs" && deepRunRef) {
      pendingNavigationTab.current = nextTab;
      setSelectedRun(null);
      navigate("/model");
    }
  }, [deepRunRef, navigate]);
  const allGpus = useMemo(() => Object.values(resourcesByServer).flatMap((resources) => resources.gpus), [resourcesByServer]);
  if (!capabilities && !error) return <LoadingCard />;
  return (
    <section className="w-full space-y-5 px-4 py-3 md:px-6 xl:px-8">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2 border-b border-console-line">
        <TrainingSectionTabs value={tab === "new" ? "runs" : tab} onChange={changeTab} />
        <div className="flex shrink-0 items-center gap-2 pb-2">
          <StatusTag tone="warning">真实训练未启用</StatusTag>
          <StatusTag tone={capabilities?.simulation_enabled ? "success" : "danger"}>{capabilities?.simulation_enabled ? "模拟模式" : "模拟不可用"}</StatusTag>
        </div>
      </div>

      {eventStreamDisconnected ? <div role="status" className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800"><AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />事件流已断开，正在使用轮询恢复。</div> : null}
      {error ? <div className="flex flex-col gap-3 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><p role="alert" className="text-sm text-rose-700">{error}</p><ConsoleButton className="shrink-0" onClick={() => void load()}><RefreshCw className="h-4 w-4" />重新加载</ConsoleButton></div> : null}

      {tab === "runs" && !selectedRun ? <TrainingOverviewMetrics runs={runs} gpus={allGpus} /> : null}

      <div id="training-platform-panel-runs" role="tabpanel" aria-labelledby="training-platform-tab-runs" hidden={tab !== "runs"}>
        <RunsPanel runs={runs} selectedRun={selectedRun} canStop={can(capabilities, "training:stop_runs")} canCreate={can(capabilities, "training:create_runs")} onCreate={() => changeTab("new")} onSelect={selectRun} onRunChange={updateRun} />
      </div>
      <div id="training-platform-panel-new" role="tabpanel" aria-labelledby="training-platform-tab-new" hidden={tab !== "new"}>
        <NewRunPanel models={models} servers={servers} resourcesByServer={resourcesByServer} canCreate={can(capabilities, "training:create_runs")} onCancel={() => changeTab("runs")} onCreated={(run) => { setModels((current) => current.map((model) => model.family_ref === run.family_ref ? { ...model, trained_version_count: model.trained_version_count + 1 } : model)); updateRun(run); setTab("runs"); selectRun(run); }} />
      </div>
      <div id="training-platform-panel-models" role="tabpanel" aria-labelledby="training-platform-tab-models" hidden={tab !== "models"}>
        <ModelsPanel models={models} servers={servers} canManage={can(capabilities, "training:manage_models")} active={tab === "models"} onSaved={(model) => setModels((current) => [model, ...current.filter((item) => item.family_ref !== model.family_ref)])} />
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
