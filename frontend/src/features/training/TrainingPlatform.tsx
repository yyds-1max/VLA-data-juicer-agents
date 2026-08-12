import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BookOpen,
  ChevronDown,
  CircleHelp,
  FileText,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Square,
  Terminal,
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
  getTrainingNodeResources,
  getTrainingServerResources,
  listTrainingModels,
  listTrainingNodes,
  listTrainingRuns,
  listTrainingServers,
  openTrainingEvents,
  previewTrainingRun,
  stopTrainingRun,
  updateTrainingModel,
} from "../../api/client";
import type { TrainingCapabilities, TrainingGpuResource, TrainingMetricSample, TrainingModel, TrainingNode, TrainingNodeResourceSnapshot, TrainingParameterDefinition, TrainingRun, TrainingRunLog, TrainingRunPreview, TrainingServer, TrainingServerResources } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { ProgressBar } from "../../components/console/ProgressBar";
import { StatusTag } from "../../components/console/StatusTag";
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

type TrainingTab = "runs" | "new" | "models" | "nodes" | "resources";
const tabs = [
  { id: "runs", label: "训练任务" }, { id: "new", label: "新建训练" },
  { id: "models", label: "模型注册" }, { id: "nodes", label: "训练节点" }, { id: "resources", label: "服务器资源" },
] satisfies Array<TabItem<TrainingTab>>;

const defaultParameters: TrainingParameterDefinition[] = navilaTrajectoryParameters;
const emptyLaunchTemplate = {
  ...navilaTrajectoryLaunchTemplate,
  working_directory: "/workspace/project",
  entrypoint: "train.py",
};

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

function errorText(error: unknown) {
  // Keep this structural so app-level tests can supply a narrow API mock.
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
function formatNumber(value: number | undefined, digits = 4) { return value === undefined ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: digits }); }
function can(capabilities: TrainingCapabilities | null, permission: TrainingCapabilities["permissions"][number]) { return capabilities?.permissions.includes(permission) ?? false; }

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
    const valueError = conditionMet ? trainingParameterValueError(parameter, currentValue) : null;
    const renderedValue = typeof currentValue === "number" && !Number.isFinite(currentValue) ? "" : String(currentValue ?? "");
    return <div key={parameter.key} data-parameter-field={parameter.key} className={cn("block text-sm text-console-muted transition-opacity", !conditionMet && "opacity-50")}><span className="flex min-h-5 items-center gap-1.5"><label htmlFor={inputId} className="font-medium text-console-text">{parameter.label}</label><span className="font-mono text-[11px] text-console-muted">{parameter.key}</span><Tooltip><TooltipTrigger asChild><button type="button" className="inline-flex rounded-sm text-console-muted outline-none transition-[color,box-shadow] duration-150 hover:text-console-text focus-visible:ring-2 focus-visible:ring-console-cyan/35 motion-reduce:transition-none" aria-label={`${parameter.label} 参数说明`}><CircleHelp className="h-3.5 w-3.5" aria-hidden="true" /></button></TooltipTrigger><TooltipContent id={helpId} side="top" align="center" sideOffset={7} collisionPadding={12} className="w-72 max-w-[calc(100vw-1.5rem)] whitespace-normal text-left leading-5">{description}</TooltipContent></Tooltip>{parameter.type === "boolean" ? <input id={inputId} aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} className="ml-2 accent-console-cyan disabled:cursor-not-allowed" type="checkbox" checked={Boolean(values[parameter.key])} disabled={parameterDisabled} onChange={(event) => onChange(parameter.key, event.target.checked)} /> : null}</span>
      {parameter.type === "boolean" ? null
      : parameter.type === "enum" ? <select id={inputId} aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} className={inputClass} value={renderedValue} disabled={parameterDisabled} onChange={(event) => onChange(parameter.key, event.target.value)}>{parameter.choices?.map((choice) => <option key={choice.value} value={choice.value}>{choice.value}</option>)}</select>
      : <input id={inputId} aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} aria-invalid={Boolean(valueError)} className={cn(inputClass, valueError && "border-rose-500")} type={parameter.sensitive ? "password" : parameter.type === "string" ? "text" : "number"} step={parameter.type === "number" ? "any" : "1"} min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} maxLength={parameter.type === "string" ? parameter.string_max_length ?? 512 : undefined} value={renderedValue} disabled={parameterDisabled} onChange={(event) => {
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
    return <label key={gpu.gpu_uuid} className={cn("rounded-md border p-3", checked ? "border-console-cyan bg-sky-50" : "border-console-line bg-console-panel2", unavailable && "opacity-55")}>
      <div className="flex items-center justify-between gap-2"><span className="font-medium text-console-text">GPU {gpu.index}</span><input aria-label={`选择 GPU ${gpu.index}`} type="checkbox" checked={checked} disabled={disabled || unavailable} onChange={() => onChange(checked ? selected.filter((id) => id !== gpu.gpu_uuid) : [...selected, gpu.gpu_uuid])} /></div>
      <p className="mt-1 text-xs text-console-muted">{gpu.name} · {Math.round(gpu.used_memory_mib / 1024)}/{Math.round(gpu.total_memory_mib / 1024)} GiB</p>
      <p className="mt-1 text-xs text-console-muted">{unavailable ? (gpu.lease_run_ref ? "平台已租用" : "外部占用") : "可用"}</p>
    </label>;
  })}</div>;
}

function NewRunPanel({ models, servers, resourcesByServer, canCreate, onCreated }: { models: TrainingModel[]; servers: TrainingServer[]; resourcesByServer: Record<string, TrainingServerResources>; canCreate: boolean; onCreated: (run: TrainingRun) => void }) {
  const [modelRef, setModelRef] = useState(""); const [serverRef, setServerRef] = useState(""); const [gpuIds, setGpuIds] = useState<string[]>([]);
  const [values, setValues] = useState<Record<string, string | number | boolean>>({}); const [preview, setPreview] = useState<TrainingRunPreview | null>(null); const [busy, setBusy] = useState(false); const [message, setMessage] = useState<string | null>(null);
  const model = models.find((item) => item.model_ref === modelRef) ?? models[0];
  const definitions = model?.revision?.parameter_definitions?.length ? model.revision.parameter_definitions : defaultParameters;
  const selectedServer = serverRef || servers[0]?.server_ref || "";
  const gpus = resourcesByServer[selectedServer]?.gpus ?? [];
  useEffect(() => { if (model && model.model_ref !== modelRef) setModelRef(model.model_ref); }, [model, modelRef]);
  useEffect(() => { if (!serverRef && servers[0]) setServerRef(servers[0].server_ref); }, [serverRef, servers]);
  useEffect(() => { setValues(Object.fromEntries(definitions.map((item) => [item.key, item.default]))); setPreview(null); }, [model?.model_ref, model?.revision?.revision]);
  const enabledDefinitions = enabledTrainingParameters(definitions, values);
  const enabledParameterKeys = new Set(enabledDefinitions.map((item) => item.key));
  const parameterErrors = enabledDefinitions.map((item) => trainingParameterValueError(item, values[item.key])).filter((error): error is string => Boolean(error));
  const hasParameterErrors = parameterErrors.length > 0;
  const payload = () => ({ model_ref: modelRef, model_revision: model?.latest_revision, server_ref: selectedServer, gpu_uuids: gpuIds, parameters: Object.fromEntries(enabledDefinitions.map((item) => [item.key, values[item.key] ?? item.default])), execution_mode: "simulation" as const });
  const doPreview = async () => { if (!modelRef || !selectedServer || !gpuIds.length) return setMessage("请选择模型、服务器和至少一张可用 GPU。"); if (hasParameterErrors) return setMessage("请先修正标红的训练参数。"); setBusy(true); setMessage(null); try { setPreview(await previewTrainingRun(payload())); } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); } };
  const start = async () => { if (!preview) return; setBusy(true); setMessage(null); try { onCreated(await createTrainingRun(payload())); } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); } };
  return <div className="space-y-4">
    <header className="flex flex-col gap-3 border-b border-console-line pb-5 lg:flex-row lg:items-end lg:justify-between">
      <div><h2 className="text-xl font-semibold text-console-text">新建训练任务</h2><p className="mt-1 text-sm text-console-muted">按模型、资源、参数和预检顺序完成配置，启动前不会占用训练资源。</p></div>
      <ol className="flex max-w-full flex-wrap items-center justify-end gap-2 text-xs text-console-muted" aria-label="创建训练步骤">
        {['选择模型', '配置资源', '训练参数', '预检启动'].map((label, index) => <li key={label} className="flex items-center gap-2"><span className={cn("flex h-6 w-6 items-center justify-center rounded-full border text-[11px] font-semibold", index === 0 ? "border-console-cyan bg-blue-50 text-console-cyan" : "border-console-line bg-console-panel text-console-muted")}>{index + 1}</span><span>{label}</span>{index < 3 ? <ArrowRight className="h-3.5 w-3.5 text-slate-300" aria-hidden="true" /> : null}</li>)}
      </ol>
    </header>
    <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>真实训练未启用。此页面只会创建可重复的模拟训练任务，不会连接训练服务器或执行命令。</span></div>
    <ConsoleCard className="shadow-none"><div className="mb-4 flex items-center gap-2"><Play className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">1. 选择模型和资源</h2><p className="text-sm text-console-muted">草稿模型、模拟服务器与 GPU 租约都会在提交前重新校验。</p></div></div><div className="grid gap-3 md:grid-cols-2"><label className="text-sm text-console-muted">模型<select aria-label="模型" className="mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={modelRef} onChange={(event) => setModelRef(event.target.value)}>{models.map((item) => <option key={item.model_ref} value={item.model_ref}>{item.name} · r{item.latest_revision}（{modelStatusMeta[item.status].label}）</option>)}</select></label><label className="text-sm text-console-muted">服务器<select aria-label="服务器" className="mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={selectedServer} onChange={(event) => { setServerRef(event.target.value); setGpuIds([]); setPreview(null); }}>{servers.map((item) => <option key={item.server_ref} value={item.server_ref}>{item.name}</option>)}</select></label></div><h3 className="mb-2 mt-5 text-sm font-medium text-console-text">选择 GPU（{gpuIds.length} 张）</h3><GpuPicker gpus={gpus} selected={gpuIds} onChange={(ids) => { setGpuIds(ids); setPreview(null); }} /></ConsoleCard>
    <ConsoleCard><div className="mb-4"><h2 className="font-semibold text-console-text">2. 配置超参数</h2><p className="text-sm text-console-muted">满足依赖条件的模型参数都可以修改；视频帧数只取决于 <code>num_video_frames</code>，高级参数按用途折叠。</p></div><GroupedParameterFields definitions={definitions} values={values} onChange={(key, value) => { setValues((current) => ({ ...current, [key]: value })); setPreview(null); }} enabledParameterKeys={enabledParameterKeys} disabled={!canCreate} /></ConsoleCard>
    <ConsoleCard><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-console-text">3. 校验并预览 RunSpec</h2><p className="text-sm text-console-muted">显示安全 argv 预览，预览本身不创建任务。</p></div><ConsoleButton variant="ghost" onClick={() => void doPreview()} disabled={!canCreate || busy || !models.length || hasParameterErrors}><RefreshCw className="h-4 w-4" />生成预览</ConsoleButton></div>{message ? <p role="alert" className="mt-3 text-sm text-rose-700">{message}</p> : null}{preview ? <div className="mt-4 space-y-3"><div className="rounded-md bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100 break-all">{preview.command_preview}</div><div className="grid gap-2 md:grid-cols-3"><span className="text-sm text-console-muted">nproc_per_node：<b className="text-console-text">{preview.run_spec.nproc_per_node}</b></span><span className="text-sm text-console-muted">GPU：<b className="text-console-text">{preview.run_spec.gpu_uuids.length}</b></span><span className="text-sm text-console-muted">端口：<b className="text-console-text">{preview.run_spec.master_port}</b></span></div>{preview.preflight.map((item, index) => <p key={index} className={cn("text-sm", item.ok ? "text-emerald-700" : "text-rose-700")}>{item.ok ? "✓" : "×"} {item.message}</p>)}</div> : null}</ConsoleCard>
    <ConsoleCard><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-console-text">4. 启动模拟训练</h2><p className="text-sm text-console-muted">GPU 与端口将在服务端原子租用，冲突时任务不会启动。</p></div><ConsoleButton variant="primary" onClick={() => void start()} disabled={!canCreate || busy || !preview}><Play className="h-4 w-4" />启动模拟训练</ConsoleButton></div></ConsoleCard>
  </div>;
}

function RunDetail({ run, canStop, onRunChange }: { run: TrainingRun; canStop: boolean; onRunChange: (run: TrainingRun) => void }) {
  const [logs, setLogs] = useState<TrainingRunLog[]>([]); const [metrics, setMetrics] = useState<TrainingMetricSample[]>([]); const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const lastLogSeq = useRef(0); const lastMetricSeq = useRef(0);
  useEffect(() => { setLogs([]); setMetrics([]); lastLogSeq.current = 0; lastMetricSeq.current = 0; setError(null); }, [run.run_ref]);
  useEffect(() => { let alive = true; const load = async () => { try { const [nextRun, nextLogs, nextMetrics] = await Promise.all([getTrainingRun(run.run_ref), getTrainingRunLogs(run.run_ref, lastLogSeq.current), getTrainingRunMetrics(run.run_ref, lastMetricSeq.current)]); if (!alive) return; onRunChange(nextRun); if (nextLogs.length) { lastLogSeq.current = Math.max(lastLogSeq.current, ...nextLogs.map((item) => item.seq)); setLogs((current) => [...current, ...nextLogs.filter((item) => !current.some((known) => known.seq === item.seq))].sort((a, b) => a.seq - b.seq)); } if (nextMetrics.length) { lastMetricSeq.current = Math.max(lastMetricSeq.current, ...nextMetrics.map((item) => item.seq)); setMetrics((current) => [...current, ...nextMetrics.filter((item) => !current.some((known) => known.seq === item.seq))].sort((a, b) => a.seq - b.seq)); } } catch (caught) { if (alive) setError(errorText(caught)); } }; void load(); const interval = window.setInterval(() => void load(), activeStatuses.has(run.status) ? 2000 : 8000); return () => { alive = false; window.clearInterval(interval); }; }, [run.run_ref, run.status, onRunChange]);
  const stop = async () => { if (!canStop || !window.confirm("确定停止此模拟训练吗？已写入的指标和日志会保留。")) return; setBusy(true); try { onRunChange(await stopTrainingRun(run.run_ref, run.state_revision)); } catch (caught) { setError(errorText(caught)); } finally { setBusy(false); } };
  const lossData = metrics.length ? { labels: metrics.map((item) => String(item.step)), data: metrics.map((item) => item.loss), label: "Loss", color: "#6d5bd0" } : null;
  const lrData = metrics.length ? { labels: metrics.map((item) => String(item.step)), data: metrics.map((item) => item.learning_rate), label: "Learning rate", color: "#b7791f" } : null;
  const gpuUtilizationData = metrics.some((item) => item.gpu_utilization_percent != null) ? { labels: metrics.map((item) => String(item.step)), data: metrics.map((item) => item.gpu_utilization_percent ?? 0), label: "GPU 利用率", color: "#0284c7" } : null;
  const gpuMemoryData = metrics.some((item) => item.gpu_memory_mib != null) ? { labels: metrics.map((item) => String(item.step)), data: metrics.map((item) => item.gpu_memory_mib ?? 0), label: "GPU 显存 MiB", color: "#059669" } : null;
  return <div className="space-y-4"><ConsoleCard className="shadow-none"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2"><h2 className="text-lg font-semibold text-console-text">{run.model_name}</h2><StatusTag tone={statusMeta[run.status].tone}>{statusMeta[run.status].label}</StatusTag></div><p className="mt-1 text-sm text-console-muted">任务 {run.run_ref} · 模型 revision {run.model_revision}</p></div>{canStop && activeStatuses.has(run.status) ? <ConsoleButton variant="ghost" onClick={() => void stop()} disabled={busy}><Square className="h-4 w-4" />停止任务</ConsoleButton> : null}</div>{error ? <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p> : null}<div className="mt-5 grid divide-y divide-console-line border-y border-console-line sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-4"><div className="py-3 sm:px-4 sm:first:pl-0"><p className="text-xs text-console-muted">Epoch</p><p className="text-xl font-semibold tabular-nums text-console-text">{run.current_epoch}/{run.total_epochs}</p></div><div className="py-3 sm:px-4"><p className="text-xs text-console-muted">Step</p><p className="text-xl font-semibold tabular-nums text-console-text">{run.current_step}/{run.total_steps}</p></div><div className="py-3 sm:px-4"><p className="text-xs text-console-muted">最新 Loss</p><p className="text-xl font-semibold tabular-nums text-console-text">{formatNumber(run.latest_metric?.loss)}</p></div><div className="py-3 sm:px-4"><p className="text-xs text-console-muted">学习率</p><p className="text-xl font-semibold tabular-nums text-console-text">{formatNumber(run.latest_metric?.learning_rate, 7)}</p></div></div><ProgressBar className="mt-4" value={run.progress_percent} tone="purple" label={`训练进度 ${run.progress_percent.toFixed(1)}%`} /></ConsoleCard>
    <div className="grid gap-4 xl:grid-cols-2">{lossData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">Loss</h3><MiniChart type="line" title="Loss" data={lossData} /></ConsoleCard> : <ConsoleCard><p className="text-sm text-console-muted">等待第一条指标…</p></ConsoleCard>}{lrData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">学习率</h3><MiniChart type="line" title="学习率" data={lrData} /></ConsoleCard> : null}</div>
    <div className="grid gap-4 xl:grid-cols-2">{gpuUtilizationData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">GPU 利用率</h3><MiniChart type="line" title="GPU 利用率" data={gpuUtilizationData} /></ConsoleCard> : null}{gpuMemoryData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">GPU 显存</h3><MiniChart type="line" title="GPU 显存" data={gpuMemoryData} /></ConsoleCard> : null}</div>
    <div className="grid gap-4 xl:grid-cols-2"><ConsoleCard><h3 className="mb-3 font-semibold text-console-text">参数快照</h3><dl className="grid gap-2 text-sm sm:grid-cols-2">{Object.entries(run.parameters ?? {}).map(([key, value]) => <div key={key} className="rounded-md bg-console-panel2 p-2"><dt className="text-console-muted">{key}</dt><dd className="mt-1 font-mono text-console-text">{String(value)}</dd></div>)}{!Object.keys(run.parameters ?? {}).length ? <p className="text-console-muted">无可展示参数。</p> : null}</dl></ConsoleCard><ConsoleCard><h3 className="mb-3 font-semibold text-console-text">审计摘要</h3><div className="space-y-2 text-sm">{run.audit_events?.map((event, index) => <div key={`${event.created_at}-${index}`} className="rounded-md bg-console-panel2 p-2"><p className="font-medium text-console-text">{event.action}</p><p className="mt-1 text-console-muted">{event.summary} · {event.created_at}</p></div>)}{!run.audit_events?.length ? <p className="text-console-muted">暂无审计事件。</p> : null}</div></ConsoleCard></div>
    <ConsoleCard><div className="mb-3 flex items-center gap-2"><Terminal className="h-4 w-4 text-console-cyan" /><h3 className="font-semibold text-console-text">训练日志</h3></div><div className="max-h-64 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100">{logs.length ? logs.map((item) => <p key={item.seq}><span className="text-slate-500">[{item.seq}]</span> {item.message}</p>) : "等待日志…"}</div></ConsoleCard>
  </div>;
}

function RunsPanel({ runs, selectedRun, canStop, onSelect, onRunChange }: { runs: TrainingRun[]; selectedRun: TrainingRun | null; canStop: boolean; onSelect: (run: TrainingRun | null) => void; onRunChange: (run: TrainingRun) => void }) {
  const [statusFilter, setStatusFilter] = useState<"all" | TrainingRun["status"]>("all");
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  // 搜索和筛选只改变当前表格的呈现，完整任务投影仍用于状态统计和深链详情。
  const filteredRuns = runs.filter((run) => {
    if (statusFilter !== "all" && run.status !== statusFilter) return false;
    if (!normalizedQuery) return true;
    return [run.model_name, run.model_ref, run.run_ref, run.server_ref]
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
                    <span className="block truncate font-medium text-console-text hover:text-console-cyan">{run.model_name}</span>
                    <span className="mt-1 block truncate font-mono text-[11px] text-console-muted">{run.run_ref} · revision {run.model_revision}</span>
                  </button>
                </td>
                <td className="px-4 py-3.5 align-middle text-console-muted">
                  <span className="block truncate text-console-text">{run.server_ref}</span>
                  <span className="mt-1 block text-xs">GPU {run.gpu_uuids.length} 张</span>
                </td>
                <td className="px-4 py-3.5 align-middle">
                  <div className="flex items-center justify-between gap-3 text-xs">
                    <span className="text-console-muted">Epoch {run.current_epoch}/{run.total_epochs}</span>
                    <span className="tabular-nums text-console-text">{run.progress_percent.toFixed(1)}%</span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-label={`${run.model_name} 训练进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={run.progress_percent}>
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
          <p className="mt-3 text-sm font-medium text-console-text">{runs.length ? "没有符合筛选条件的任务" : "还没有训练任务。请从“新建训练”开始模拟运行。"}</p>
          {runs.length ? <p className="mt-1 text-sm text-console-muted">请调整搜索内容或状态筛选。</p> : null}
        </div>
      ) : null}
    </section>
  );
}

function ModelsPanel({ models, canManage, onSaved }: { models: TrainingModel[]; canManage: boolean; onSaved: (model: TrainingModel) => void }) {
  const [editingModelRef, setEditingModelRef] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [domain, setDomain] = useState("vla"); const [serverRef, setServerRef] = useState("fake-local");
  const [workingDirectory, setWorkingDirectory] = useState(emptyLaunchTemplate.working_directory); const [executable, setExecutable] = useState(emptyLaunchTemplate.executable);
  const [entrypoint, setEntrypoint] = useState(emptyLaunchTemplate.entrypoint); const [fixedArgv, setFixedArgv] = useState(""); const [outputRoot, setOutputRoot] = useState(emptyLaunchTemplate.output_root); const [outputFlag, setOutputFlag] = useState(emptyLaunchTemplate.output_flag);
  const [runtimeKind, setRuntimeKind] = useState<"system" | "conda">(emptyLaunchTemplate.runtime_environment.kind);
  const [condaEnvironment, setCondaEnvironment] = useState("");
  const [monitoringFormat, setMonitoringFormat] = useState<"plain" | "transformers" | "jsonl">(emptyLaunchTemplate.monitoring.format);
  const [parameterDefinitions, setParameterDefinitions] = useState<TrainingParameterDefinition[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const textInput = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text";
  const resetCreateMode = () => {
    setEditingModelRef(null); setName(""); setDescription("");
    setDomain(emptyLaunchTemplate.domain); setServerRef(emptyLaunchTemplate.server_ref); setWorkingDirectory(emptyLaunchTemplate.working_directory); setExecutable(emptyLaunchTemplate.executable);
    setEntrypoint(emptyLaunchTemplate.entrypoint); setFixedArgv(""); setOutputRoot(emptyLaunchTemplate.output_root); setOutputFlag(emptyLaunchTemplate.output_flag);
    setRuntimeKind(emptyLaunchTemplate.runtime_environment.kind); setCondaEnvironment(""); setMonitoringFormat(emptyLaunchTemplate.monitoring.format); setParameterDefinitions([]); setError(null);
  };
  const edit = (model: TrainingModel) => {
    const template = model.revision?.launch_template;
    if (!template || !model.revision) { setError("该模型缺少可编辑的 launch template，请重新加载管理员投影。"); return; }
    setEditingModelRef(model.model_ref); setName(model.name); setDescription(model.description ?? "");
    setDomain(template.domain); setServerRef(template.server_ref); setWorkingDirectory(template.working_directory); setExecutable(template.executable);
    setEntrypoint(template.entrypoint); setFixedArgv(template.fixed_argv.join("\n")); setOutputRoot(template.output_root); setOutputFlag(template.output_flag ?? "--output_dir");
    setRuntimeKind(template.runtime_environment?.kind ?? "system"); setCondaEnvironment(template.runtime_environment?.conda_environment ?? ""); setMonitoringFormat(template.monitoring?.format ?? "plain");
    setParameterDefinitions(model.revision.parameter_definitions.map((parameter) => ({ ...structuredClone(parameter), editable: true }))); setError(null);
  };
  const save = async () => {
    let normalizedDefinitions: TrainingParameterDefinition[];
    try { normalizedDefinitions = validateParameterDefinitions(parameterDefinitions); } catch (caught) { setError(errorText(caught)); return; }
    const fixedTokens = fixedArgv.split("\n").map((item) => item.trim()).filter(Boolean);
    const fixedTokenFlags = fixedTokens.map((token) => token.startsWith("--") ? token.split("=", 1)[0] : token);
    if (!/^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(outputFlag)) { setError("Output flag 格式无效。"); return; }
    if (parameterDefinitions.some((parameter) => (parameter.cli_flag || `--${parameter.key}`) === outputFlag)) { setError("Output flag 由平台管理，不能与训练参数重复。"); return; }
    if (fixedTokenFlags.includes(outputFlag)) { setError("额外固定 argv 不能重复声明平台管理的 Output flag。"); return; }
    const parameterFlags = new Set(normalizedDefinitions.map((parameter) => parameter.cli_flag || `--${parameter.key}`));
    const duplicateFixedFlag = fixedTokenFlags.find((token) => parameterFlags.has(token));
    if (duplicateFixedFlag) { setError(`额外固定 argv 与训练参数重复声明了 ${duplicateFixedFlag}。`); return; }
    const normalizedCondaEnvironment = condaEnvironment.trim();
    if (runtimeKind === "conda" && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(normalizedCondaEnvironment)) { setError("Conda 环境名只能包含字母、数字、点、下划线和短横线，且不能为空。"); return; }
    setBusy(true); setError(null);
    try {
      const launchTemplate = {
        domain, server_ref: serverRef, working_directory: workingDirectory, executable, entrypoint,
        // One argv token per line makes this a structured argv list, never a shell command.
        fixed_argv: fixedTokens, output_root: outputRoot, output_flag: outputFlag,
        runtime_environment: runtimeKind === "conda" ? { kind: "conda" as const, conda_environment: normalizedCondaEnvironment } : { kind: "system" as const },
        monitoring: { source: "stdout" as const, format: monitoringFormat },
      };
      const editingModel = models.find((model) => model.model_ref === editingModelRef);
      const saved = editingModel
        ? await updateTrainingModel(editingModel.model_ref, { expected_revision: editingModel.latest_revision, name, description, parameter_definitions: normalizedDefinitions, launch_template: launchTemplate })
        : await createTrainingModel({ name, description, parameter_definitions: normalizedDefinitions, launch_template: launchTemplate });
      onSaved(saved);
      if (editingModel) edit(saved);
    } catch (caught) { setError(errorText(caught)); } finally { setBusy(false); }
  };
  const loadNavilaPreset = () => {
    setName("NaVILA 轨迹训练"); setDescription("NaVILA 轨迹训练模板；所有路径均为待配置占位值，仅用于模拟预览。");
    setDomain(navilaTrajectoryLaunchTemplate.domain); setServerRef(navilaTrajectoryLaunchTemplate.server_ref); setWorkingDirectory(navilaTrajectoryLaunchTemplate.working_directory);
    setExecutable(navilaTrajectoryLaunchTemplate.executable); setEntrypoint(navilaTrajectoryLaunchTemplate.entrypoint); setFixedArgv("");
    setOutputRoot(navilaTrajectoryLaunchTemplate.output_root); setOutputFlag(navilaTrajectoryLaunchTemplate.output_flag);
    setRuntimeKind(navilaTrajectoryLaunchTemplate.runtime_environment.kind); setCondaEnvironment(navilaTrajectoryLaunchTemplate.runtime_environment.conda_environment ?? ""); setMonitoringFormat(navilaTrajectoryLaunchTemplate.monitoring.format);
    setParameterDefinitions(structuredClone(navilaTrajectoryParameters)); setError(null);
  };
  const defaultParameterValues = Object.fromEntries(parameterDefinitions.map((parameter) => [parameter.key, parameter.default]));
  const defaultEnabledParameters = enabledTrainingParameters(parameterDefinitions, defaultParameterValues);
  const commandTokens = [
    executable,
    "--nnodes=1", "--nproc_per_node=<所选 GPU 数>", "--master_port=<自动分配>", "--master_addr=127.0.0.1", "--node_rank=0",
    entrypoint,
    ...fixedArgv.split("\n").map((item) => item.trim()).filter(Boolean),
    ...defaultEnabledParameters.flatMap((parameter) => {
      const flag = parameter.cli_flag || `--${parameter.key}`;
      if (parameter.argument_style === "flag_when_true") return parameter.default ? [flag] : [];
      const rendered = parameter.sensitive ? "********" : parameter.type === "boolean" ? (parameter.default ? "True" : "False") : String(parameter.default);
      return [flag, rendered];
    }),
    outputFlag, "<平台生成输出目录>",
  ];
  return <div className="space-y-5"><header className="border-b border-console-line pb-5"><h2 className="text-xl font-semibold text-console-text">模型注册</h2><p className="mt-1 text-sm text-console-muted">维护可复用的训练入口、参数规范与不可变版本，供创建训练任务时选择。</p></header><div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(340px,.65fr)]"><ConsoleCard className="shadow-none"><div className="mb-4 flex items-start justify-between gap-3"><div className="flex items-center gap-2"><Plus className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">{editingModelRef ? "编辑草稿模型" : "登记草稿模型"}</h2><p className="text-sm text-console-muted">{editingModelRef ? "保存时创建不可变的新 revision，不覆盖历史版本。" : "仅创建 draft；launch template 只用于生成模拟预览，绝不从浏览器执行。"}</p></div></div>{editingModelRef ? <ConsoleButton variant="ghost" onClick={resetCreateMode}>创建新模型</ConsoleButton> : null}</div>
    <div className="mb-4 rounded-md border border-sky-200 bg-sky-50 p-3"><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-sm font-medium text-sky-900">不知道从哪里开始？</p><p className="text-xs text-sky-800">载入同事命令对应的完整参数结构，再按部署环境修改占位路径。</p></div><ConsoleButton variant="ghost" disabled={!canManage} onClick={loadNavilaPreset}>一键载入 NaVILA 轨迹训练模板</ConsoleButton></div></div>
    <label className="block text-sm text-console-muted">名称<input className={textInput} value={name} disabled={!canManage} onChange={(event) => setName(event.target.value)} /></label><label className="mt-3 block text-sm text-console-muted">说明<textarea className="mt-1 min-h-16 w-full rounded-md border border-console-line bg-console-panel p-2 text-console-text" value={description} disabled={!canManage} onChange={(event) => setDescription(event.target.value)} /></label>
    <div className="mt-3 grid gap-3 sm:grid-cols-2"><label className="text-sm text-console-muted">领域 · Domain<input className={textInput} value={domain} disabled={!canManage} onChange={(e) => setDomain(e.target.value)} /></label><label className="text-sm text-console-muted">服务器标识 · Server ref<input className={textInput} value={serverRef} disabled={!canManage} onChange={(e) => setServerRef(e.target.value)} /></label><label className="text-sm text-console-muted">工作目录 · Working directory<input className={textInput} value={workingDirectory} disabled={!canManage} onChange={(e) => setWorkingDirectory(e.target.value)} /></label><label className="text-sm text-console-muted">启动程序 · Executable<input className={textInput} value={executable} disabled={!canManage} onChange={(e) => setExecutable(e.target.value)} /></label><label className="text-sm text-console-muted">训练入口 · Entrypoint<input className={textInput} value={entrypoint} disabled={!canManage} onChange={(e) => setEntrypoint(e.target.value)} /></label><label className="text-sm text-console-muted">输出根目录 · Output root<input className={textInput} value={outputRoot} disabled={!canManage} onChange={(e) => setOutputRoot(e.target.value)} /></label><label className="text-sm text-console-muted">输出参数标志 · Output flag<input className={textInput} value={outputFlag} disabled={!canManage} onChange={(e) => setOutputFlag(e.target.value)} /></label><label className="text-sm text-console-muted">运行环境 · Runtime environment<select className={textInput} value={runtimeKind} disabled={!canManage} onChange={(e) => setRuntimeKind(e.target.value as "system" | "conda")}><option value="system">Worker 系统环境</option><option value="conda">Conda 环境</option></select></label>{runtimeKind === "conda" ? <label className="text-sm text-console-muted">Conda 环境名<input className={textInput} value={condaEnvironment} disabled={!canManage} maxLength={128} placeholder="例如 navila" onChange={(e) => setCondaEnvironment(e.target.value)} /></label> : null}<label className="text-sm text-console-muted">指标日志格式 · Metrics format<select className={textInput} value={monitoringFormat} disabled={!canManage} onChange={(e) => setMonitoringFormat(e.target.value as "plain" | "transformers" | "jsonl")}><option value="plain">普通文本（仅日志）</option><option value="transformers">Transformers Trainer 日志</option><option value="jsonl">JSON Lines 指标</option></select></label></div>
    <label className="mt-3 block text-sm text-console-muted">额外固定 argv（每行一个 token）<textarea className="mt-1 min-h-16 w-full rounded-md border border-console-line bg-console-panel p-2 font-mono text-xs text-console-text" value={fixedArgv} disabled={!canManage} onChange={(e) => setFixedArgv(e.target.value)} /></label><div className="mt-5"><ParameterDefinitionEditor definitions={parameterDefinitions} disabled={!canManage} onChange={setParameterDefinitions} /></div><p className="mt-3 text-xs text-console-muted">GPU、nnodes、nproc_per_node、master 地址/端口、node rank 和输出目录由平台管理，不注册为普通参数。</p><div className="mt-4 rounded-md border border-console-line bg-slate-950 p-3"><p className="mb-2 text-xs font-semibold text-slate-300">实时结构化命令摘要（默认值）</p><p className="font-mono text-xs leading-6 text-slate-100 break-all">{commandTokens.map((token) => /\s/.test(token) ? JSON.stringify(token) : token).join(" ")}</p></div>{error ? <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p> : null}<ConsoleButton className="mt-4" variant="primary" disabled={!canManage || busy || !name.trim() || !serverRef.trim() || !entrypoint.trim()} onClick={() => void save()}><Plus className="h-4 w-4" />{editingModelRef ? "创建新 revision" : "创建草稿"}</ConsoleButton></ConsoleCard><ConsoleCard className="h-fit shadow-none"><div className="mb-4 flex items-center gap-2"><BookOpen className="h-5 w-5 text-console-cyan" /><h2 className="font-semibold text-console-text">已登记模型</h2></div><div className="space-y-2">{models.map((model) => <div key={model.model_ref} className="rounded-md border border-console-line bg-console-panel2 p-3"><div className="flex items-center justify-between gap-2"><p className="font-medium text-console-text">{model.name}</p><StatusTag tone={modelStatusMeta[model.status].tone}>{modelStatusMeta[model.status].label}</StatusTag></div><p className="mt-1 text-sm text-console-muted">revision {model.latest_revision} · {model.description ?? "无说明"}</p><p className="mt-2 line-clamp-2 text-xs text-console-muted">参数：{model.revision?.parameter_definitions.map((p) => p.key).join("、") || "加载详情后显示"}</p>{canManage && model.status === "draft" ? <ConsoleButton className="mt-3" variant="ghost" onClick={() => edit(model)}>编辑并创建新 revision</ConsoleButton> : null}</div>)}{!models.length ? <p className="py-8 text-center text-sm text-console-muted">尚未登记模型。</p> : null}</div></ConsoleCard></div></div>;
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
  // 可用资源与创建训练任务时的 GPU 选择规则保持一致：外部占用或已有租约均不可用。
  const availableCount = gpus.filter((gpu) => !gpu.externally_occupied && !gpu.lease_run_ref).length;
  const averageUtilization = gpus.length ? gpus.reduce((sum, gpu) => sum + gpu.utilization_percent, 0) / gpus.length : 0;

  return (
    <section aria-labelledby="training-resources-heading" className="space-y-5">
      <header>
        <h2 id="training-resources-heading" className="text-xl font-semibold text-console-text">服务器资源</h2>
        <p className="mt-1 text-sm text-console-muted">按服务器查看 GPU 利用率、显存与平台租约；所有已纳管服务器每 2 秒独立刷新。</p>
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

      {gpus.length ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" role="region" aria-label={`${selectedServer?.name ?? "当前服务器"} GPU 资源`}>
          {gpus.map((gpu) => {
            const memoryPercent = gpu.total_memory_mib ? gpu.used_memory_mib / gpu.total_memory_mib * 100 : 0;
            const occupied = gpu.externally_occupied || Boolean(gpu.lease_run_ref);
            return (
              <article key={gpu.gpu_uuid} className="rounded-xl border border-console-line bg-console-panel p-4 shadow-[0_6px_20px_rgba(31,42,68,0.04)]">
                <div className="flex items-start justify-between gap-3"><div><p className="font-medium text-console-text">GPU {gpu.index}</p><p className="mt-1 text-xs text-console-muted">{gpu.name} · {gpu.temperature_c}°C</p></div><StatusTag tone={occupied ? "warning" : "success"}>{gpu.lease_run_ref ? "平台已租用" : gpu.externally_occupied ? "外部占用" : "可用"}</StatusTag></div>
                <ProgressBar className="mt-4" value={gpu.utilization_percent} tone="purple" label={`利用率 ${gpu.utilization_percent}%`} />
                <ProgressBar className="mt-4" value={memoryPercent} tone="info" label={`显存 ${Math.round(gpu.used_memory_mib / 1024)} / ${Math.round(gpu.total_memory_mib / 1024)} GiB`} />
                <div className="mt-4 border-t border-console-line pt-3"><p className="truncate font-mono text-[11px] text-console-muted" title={gpu.gpu_uuid}>{gpu.gpu_uuid}</p><p className="mt-1 truncate text-xs text-console-muted" title={gpu.lease_run_ref ?? undefined}>{gpu.lease_run_ref ? `租约任务：${gpu.lease_run_ref}` : occupied ? "由平台外部进程占用" : "可分配给新的训练任务"}</p></div>
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
  const [tab, setTab] = useState<TrainingTab>("runs"); const [capabilities, setCapabilities] = useState<TrainingCapabilities | null>(null); const [models, setModels] = useState<TrainingModel[]>([]); const [nodes, setNodes] = useState<TrainingNode[]>([]); const [nodeResources, setNodeResources] = useState<Record<string, TrainingNodeResourceSnapshot>>({}); const [servers, setServers] = useState<TrainingServer[]>([]); const [resourcesByServer, setResourcesByServer] = useState<Record<string, TrainingServerResources>>({}); const [resourceErrors, setResourceErrors] = useState<Record<string, string>>({}); const [runs, setRuns] = useState<TrainingRun[]>([]); const [selectedRun, setSelectedRun] = useState<TrainingRun | null>(null); const [error, setError] = useState<string | null>(null); const [eventStreamDisconnected, setEventStreamDisconnected] = useState(false);
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
      const nodeResourceResults = await Promise.allSettled(nextNodes.map(async (node) => ({ nodeRef: node.node_ref, resources: await getTrainingNodeResources(node.node_ref) })));
      const nextNodeResources: Record<string, TrainingNodeResourceSnapshot> = {};
      nodeResourceResults.forEach((result) => { if (result.status === "fulfilled") nextNodeResources[result.value.nodeRef] = result.value.resources; });
      setCapabilities(nextCapabilities); setModels(nextModels); setNodes((current) => nextNodes.map((node) => {
        const local = current.find((item) => item.node_ref === node.node_ref);
        return local && local.state_revision > node.state_revision ? local : node;
      })); setNodeResources(nextNodeResources); setServers(nextServers); setResourcesByServer(nextResources); setResourceErrors(nextResourceErrors); setRuns(nextRuns);
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
    if (!deepRunRef) { setSelectedRun(null); return; }
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
      setSelectedRun(null);
      navigate("/model");
    }
  }, [deepRunRef, navigate]);
  const allGpus = useMemo(() => Object.values(resourcesByServer).flatMap((resources) => resources.gpus), [resourcesByServer]);
  if (!capabilities && !error) return <LoadingCard />;
  return (
    <section className="w-full space-y-5 px-4 py-3 md:px-6 xl:px-8">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2 border-b border-console-line">
        <TrainingSectionTabs value={tab} onChange={changeTab} />
        <div className="flex shrink-0 items-center gap-2 pb-2">
          <StatusTag tone="warning">真实训练未启用</StatusTag>
          <StatusTag tone={capabilities?.simulation_enabled ? "success" : "danger"}>{capabilities?.simulation_enabled ? "模拟模式" : "模拟不可用"}</StatusTag>
        </div>
      </div>

      {eventStreamDisconnected ? <div role="status" className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800"><AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />事件流已断开，正在使用轮询恢复。</div> : null}
      {error ? <div className="flex flex-col gap-3 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><p role="alert" className="text-sm text-rose-700">{error}</p><ConsoleButton className="shrink-0" onClick={() => void load()}><RefreshCw className="h-4 w-4" />重新加载</ConsoleButton></div> : null}

      {tab === "runs" && !selectedRun ? <TrainingOverviewMetrics runs={runs} gpus={allGpus} /> : null}

      <div id="training-platform-panel-runs" role="tabpanel" aria-labelledby="training-platform-tab-runs" hidden={tab !== "runs"}>
        <RunsPanel runs={runs} selectedRun={selectedRun} canStop={can(capabilities, "training:stop_runs")} onSelect={selectRun} onRunChange={updateRun} />
      </div>
      <div id="training-platform-panel-new" role="tabpanel" aria-labelledby="training-platform-tab-new" hidden={tab !== "new"}>
        <NewRunPanel models={models} servers={servers} resourcesByServer={resourcesByServer} canCreate={can(capabilities, "training:create_runs")} onCreated={(run) => { updateRun(run); setTab("runs"); selectRun(run); }} />
      </div>
      <div id="training-platform-panel-models" role="tabpanel" aria-labelledby="training-platform-tab-models" hidden={tab !== "models"}>
        <ModelsPanel models={models} canManage={can(capabilities, "training:manage_models")} onSaved={(model) => setModels((current) => [model, ...current.filter((item) => item.model_ref !== model.model_ref)])} />
      </div>
      <div id="training-platform-panel-nodes" role="tabpanel" aria-labelledby="training-platform-tab-nodes" hidden={tab !== "nodes"}>
        <TrainingNodesPanel nodes={nodes} resourcesByNode={nodeResources} canManage={can(capabilities, "training:manage_nodes")} deploymentEnabled={capabilities?.node_deployment_enabled ?? false} deploymentDisabledReason={capabilities?.node_deployment_disabled_reason} onChanged={(node) => setNodes((current) => [node, ...current.filter((item) => item.node_ref !== node.node_ref)])} />
      </div>
      <div id="training-platform-panel-resources" role="tabpanel" aria-labelledby="training-platform-tab-resources" hidden={tab !== "resources"}>
        <ResourcesPanel servers={servers} resourcesByServer={resourcesByServer} resourceErrors={resourceErrors} onRefresh={() => void load()} />
      </div>
    </section>
  );
}
