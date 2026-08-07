import { Activity, AlertTriangle, BookOpen, ChevronDown, CircleHelp, Cpu, FileText, Play, Plus, RefreshCw, Server, Square, Terminal } from "lucide-react";
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
  listTrainingRuns,
  listTrainingServers,
  openTrainingEvents,
  previewTrainingRun,
  stopTrainingRun,
  updateTrainingModel,
} from "../../api/client";
import type { TrainingCapabilities, TrainingGpuResource, TrainingMetricSample, TrainingModel, TrainingParameterDefinition, TrainingRun, TrainingRunLog, TrainingRunPreview, TrainingServer } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { ProgressBar } from "../../components/console/ProgressBar";
import { SegmentedTabs } from "../../components/console/SegmentedTabs";
import { StatusTag } from "../../components/console/StatusTag";
import { cn } from "../../lib/utils";
import type { StatusTone, TabItem } from "../console/consoleTypes";
import { MiniChart } from "../console/visuals/MiniChart";
import { ParameterDefinitionEditor, validateParameterDefinitions } from "./ParameterDefinitionEditor";
import { parameterDependencySummary } from "./ParameterDependencyDialog";
import { navilaTrajectoryLaunchTemplate, navilaTrajectoryParameters } from "./navilaTemplate";
import { trainingParameterGroupFor, usedTrainingParameterGroups } from "./parameterGroups";
import { enabledTrainingParameters } from "./parameterAvailability";

type TrainingTab = "runs" | "new" | "models" | "resources";
const tabs = [
  { id: "runs", label: "训练任务" }, { id: "new", label: "新建训练" },
  { id: "models", label: "模型注册" }, { id: "resources", label: "服务器资源" },
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

function errorText(error: unknown) {
  // Keep this structural so app-level tests can supply a narrow API mock.
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
function formatNumber(value: number | undefined, digits = 4) { return value === undefined ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: digits }); }
function can(capabilities: TrainingCapabilities | null, permission: TrainingCapabilities["permissions"][number]) { return capabilities?.permissions.includes(permission) ?? false; }

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
  return <div className="grid gap-3 md:grid-cols-2">{definitions.map((parameter) => {
    const conditionMet = enabledParameterKeys.has(parameter.key);
    const parameterDisabled = disabled || !conditionMet;
    const inputClass = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-sm text-console-text focus:border-console-cyan focus:outline-hidden disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-console-muted";
    const cliFlag = parameter.cli_flag || `--${parameter.key}`;
    const description = parameter.description?.trim() || `${parameter.label}，对应命令参数 ${cliFlag}。`;
    const helpId = `training-parameter-help-${parameter.key}`;
    const conditionId = `training-parameter-condition-${parameter.key}`;
    const conditionSummary = !conditionMet ? parameterDependencySummary(definitions, parameter) : null;
    const currentValue = values[parameter.key];
    const valueError = conditionMet ? trainingParameterValueError(parameter, currentValue) : null;
    const renderedValue = typeof currentValue === "number" && !Number.isFinite(currentValue) ? "" : String(currentValue ?? "");
    return <label key={parameter.key} className={cn("block text-sm text-console-muted transition-opacity", !conditionMet && "opacity-50")}><span className="flex min-h-5 items-center gap-1.5"><span className="font-medium text-console-text">{parameter.label}</span><span className="font-mono text-[11px] text-console-muted">{parameter.key}</span><span className="group/help relative inline-flex" tabIndex={0} aria-label={`${parameter.label} 参数说明`} aria-describedby={helpId}><CircleHelp className="h-3.5 w-3.5 text-console-muted" aria-hidden="true" /><span id={helpId} role="tooltip" className="pointer-events-none invisible absolute left-0 top-full z-20 mt-2 w-72 rounded-md bg-slate-950 px-3 py-2 text-xs leading-5 text-slate-100 opacity-0 shadow-lg transition group-hover/help:visible group-hover/help:opacity-100 group-focus-within/help:visible group-focus-within/help:opacity-100">{description}</span></span>{parameter.type === "boolean" ? <input aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} className="ml-2 accent-console-cyan disabled:cursor-not-allowed" type="checkbox" checked={Boolean(values[parameter.key])} disabled={parameterDisabled} onChange={(event) => onChange(parameter.key, event.target.checked)} /> : null}</span>
      {parameter.type === "boolean" ? null
      : parameter.type === "enum" ? <select aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} className={inputClass} value={renderedValue} disabled={parameterDisabled} onChange={(event) => onChange(parameter.key, event.target.value)}>{parameter.choices?.map((choice) => <option key={choice.value} value={choice.value}>{choice.value}</option>)}</select>
      : <input aria-label={parameter.label} aria-describedby={conditionSummary ? conditionId : undefined} aria-invalid={Boolean(valueError)} className={cn(inputClass, valueError && "border-rose-500")} type={parameter.sensitive ? "password" : parameter.type === "string" ? "text" : "number"} step={parameter.type === "number" ? "any" : "1"} min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} maxLength={parameter.type === "string" ? parameter.string_max_length ?? 512 : undefined} value={renderedValue} disabled={parameterDisabled} onChange={(event) => {
        const raw = event.target.value;
        onChange(parameter.key, parameter.type === "string" ? raw : raw.trim() === "" ? Number.NaN : Number(raw));
      }} />}
      {valueError ? <span role="alert" className="mt-1 block text-xs text-rose-700">{valueError}</span> : null}
      {conditionSummary ? <span id={conditionId} className="mt-1 block text-xs text-console-muted">{conditionSummary}</span> : null}
    </label>;
  })}</div>;
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
  const groups = usedTrainingParameterGroups(definitions);
  const commonGroup = groups.find((group) => group.key === "common");
  const common = commonGroup ? definitions.filter((parameter) => trainingParameterGroupFor(parameter).key === commonGroup.key) : [];
  const foldedGroups = groups.filter((group) => group.key !== "common").map((group) => ({ ...group, definitions: definitions.filter((parameter) => trainingParameterGroupFor(parameter).key === group.key) }));
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
    return <label key={gpu.gpu_uuid} className={cn("rounded-md border p-3", checked ? "border-console-cyan bg-sky-50" : "border-console-line bg-console-panel2", unavailable && "opacity-55")}>
      <div className="flex items-center justify-between gap-2"><span className="font-medium text-console-text">GPU {gpu.index}</span><input aria-label={`选择 GPU ${gpu.index}`} type="checkbox" checked={checked} disabled={disabled || unavailable} onChange={() => onChange(checked ? selected.filter((id) => id !== gpu.gpu_uuid) : [...selected, gpu.gpu_uuid])} /></div>
      <p className="mt-1 text-xs text-console-muted">{gpu.name} · {Math.round(gpu.used_memory_mib / 1024)}/{Math.round(gpu.total_memory_mib / 1024)} GiB</p>
      <p className="mt-1 text-xs text-console-muted">{unavailable ? (gpu.lease_run_ref ? "平台已租用" : "外部占用") : "可用"}</p>
    </label>;
  })}</div>;
}

function NewRunPanel({ models, servers, gpus, canCreate, onCreated }: { models: TrainingModel[]; servers: TrainingServer[]; gpus: TrainingGpuResource[]; canCreate: boolean; onCreated: (run: TrainingRun) => void }) {
  const [modelRef, setModelRef] = useState(""); const [serverRef, setServerRef] = useState(""); const [gpuIds, setGpuIds] = useState<string[]>([]);
  const [values, setValues] = useState<Record<string, string | number | boolean>>({}); const [preview, setPreview] = useState<TrainingRunPreview | null>(null); const [busy, setBusy] = useState(false); const [message, setMessage] = useState<string | null>(null);
  const model = models.find((item) => item.model_ref === modelRef) ?? models[0];
  const definitions = model?.revision?.parameter_definitions?.length ? model.revision.parameter_definitions : defaultParameters;
  const selectedServer = serverRef || servers[0]?.server_ref || "";
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
  return <div className="space-y-4"><ConsoleCard><div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /><span>真实训练未启用。此页面只会创建可重复的模拟训练任务，不会连接训练服务器或执行命令。</span></div></ConsoleCard>
    <ConsoleCard><div className="mb-4 flex items-center gap-2"><Play className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">1. 选择模型和资源</h2><p className="text-sm text-console-muted">草稿模型、模拟服务器与 GPU 租约都会在提交前重新校验。</p></div></div><div className="grid gap-3 md:grid-cols-2"><label className="text-sm text-console-muted">模型<select aria-label="模型" className="mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={modelRef} onChange={(event) => setModelRef(event.target.value)}>{models.map((item) => <option key={item.model_ref} value={item.model_ref}>{item.name} · r{item.latest_revision} ({item.status})</option>)}</select></label><label className="text-sm text-console-muted">服务器<select aria-label="服务器" className="mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={selectedServer} onChange={(event) => { setServerRef(event.target.value); setGpuIds([]); setPreview(null); }}>{servers.map((item) => <option key={item.server_ref} value={item.server_ref}>{item.name}</option>)}</select></label></div><h3 className="mb-2 mt-5 text-sm font-medium text-console-text">选择 GPU（{gpuIds.length} 张）</h3><GpuPicker gpus={gpus} selected={gpuIds} onChange={(ids) => { setGpuIds(ids); setPreview(null); }} /></ConsoleCard>
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
  return <div className="space-y-4"><ConsoleCard><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2"><h2 className="text-lg font-semibold text-console-text">{run.model_name}</h2><StatusTag tone={statusMeta[run.status].tone}>{statusMeta[run.status].label}</StatusTag></div><p className="mt-1 text-sm text-console-muted">任务 {run.run_ref} · 模型 revision {run.model_revision}</p></div>{canStop && activeStatuses.has(run.status) ? <ConsoleButton variant="ghost" onClick={() => void stop()} disabled={busy}><Square className="h-4 w-4" />停止任务</ConsoleButton> : null}</div>{error ? <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p> : null}<div className="mt-5 grid gap-3 md:grid-cols-4"><div><p className="text-xs text-console-muted">Epoch</p><p className="text-xl font-semibold text-console-text">{run.current_epoch}/{run.total_epochs}</p></div><div><p className="text-xs text-console-muted">Step</p><p className="text-xl font-semibold text-console-text">{run.current_step}/{run.total_steps}</p></div><div><p className="text-xs text-console-muted">最新 Loss</p><p className="text-xl font-semibold text-console-text">{formatNumber(run.latest_metric?.loss)}</p></div><div><p className="text-xs text-console-muted">学习率</p><p className="text-xl font-semibold text-console-text">{formatNumber(run.latest_metric?.learning_rate, 7)}</p></div></div><ProgressBar className="mt-4" value={run.progress_percent} tone="purple" label={`训练进度 ${run.progress_percent.toFixed(1)}%`} /></ConsoleCard>
    <div className="grid gap-4 xl:grid-cols-2">{lossData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">Loss</h3><MiniChart type="line" title="Loss" data={lossData} /></ConsoleCard> : <ConsoleCard><p className="text-sm text-console-muted">等待第一条指标…</p></ConsoleCard>}{lrData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">学习率</h3><MiniChart type="line" title="学习率" data={lrData} /></ConsoleCard> : null}</div>
    <div className="grid gap-4 xl:grid-cols-2">{gpuUtilizationData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">GPU 利用率</h3><MiniChart type="line" title="GPU 利用率" data={gpuUtilizationData} /></ConsoleCard> : null}{gpuMemoryData ? <ConsoleCard><h3 className="mb-3 font-semibold text-console-text">GPU 显存</h3><MiniChart type="line" title="GPU 显存" data={gpuMemoryData} /></ConsoleCard> : null}</div>
    <div className="grid gap-4 xl:grid-cols-2"><ConsoleCard><h3 className="mb-3 font-semibold text-console-text">参数快照</h3><dl className="grid gap-2 text-sm sm:grid-cols-2">{Object.entries(run.parameters ?? {}).map(([key, value]) => <div key={key} className="rounded-md bg-console-panel2 p-2"><dt className="text-console-muted">{key}</dt><dd className="mt-1 font-mono text-console-text">{String(value)}</dd></div>)}{!Object.keys(run.parameters ?? {}).length ? <p className="text-console-muted">无可展示参数。</p> : null}</dl></ConsoleCard><ConsoleCard><h3 className="mb-3 font-semibold text-console-text">审计摘要</h3><div className="space-y-2 text-sm">{run.audit_events?.map((event, index) => <div key={`${event.created_at}-${index}`} className="rounded-md bg-console-panel2 p-2"><p className="font-medium text-console-text">{event.action}</p><p className="mt-1 text-console-muted">{event.summary} · {event.created_at}</p></div>)}{!run.audit_events?.length ? <p className="text-console-muted">暂无审计事件。</p> : null}</div></ConsoleCard></div>
    <ConsoleCard><div className="mb-3 flex items-center gap-2"><Terminal className="h-4 w-4 text-console-cyan" /><h3 className="font-semibold text-console-text">训练日志</h3></div><div className="max-h-64 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100">{logs.length ? logs.map((item) => <p key={item.seq}><span className="text-slate-500">[{item.seq}]</span> {item.message}</p>) : "等待日志…"}</div></ConsoleCard>
  </div>;
}

function RunsPanel({ runs, selectedRun, canStop, onSelect, onRunChange }: { runs: TrainingRun[]; selectedRun: TrainingRun | null; canStop: boolean; onSelect: (run: TrainingRun | null) => void; onRunChange: (run: TrainingRun) => void }) {
  const [statusFilter, setStatusFilter] = useState<"all" | TrainingRun["status"]>("all");
  const filteredRuns = statusFilter === "all" ? runs : runs.filter((run) => run.status === statusFilter);
  if (selectedRun) return <div><ConsoleButton variant="ghost" className="mb-3" onClick={() => onSelect(null)}>返回任务列表</ConsoleButton><RunDetail run={selectedRun} canStop={canStop} onRunChange={onRunChange} /></div>;
  return <ConsoleCard><div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2"><Activity className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">训练任务</h2><p className="text-sm text-console-muted">模拟任务状态、进度和最近指标会自动刷新。</p></div></div><label className="text-sm text-console-muted">状态筛选<select aria-label="状态筛选" className="ml-2 h-9 rounded-md border border-console-line bg-console-panel px-2 text-console-text" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as "all" | TrainingRun["status"])}><option value="all">全部</option>{Object.entries(statusMeta).map(([status, meta]) => <option key={status} value={status}>{meta.label}</option>)}</select></label></div>{filteredRuns.length ? <div className="space-y-2">{filteredRuns.map((run) => <button key={run.run_ref} type="button" className="grid w-full gap-2 rounded-md border border-console-line bg-console-panel2 p-3 text-left transition hover:border-console-cyan sm:grid-cols-[1fr_auto_auto] sm:items-center" onClick={() => onSelect(run)}><div><p className="font-medium text-console-text">{run.model_name}</p><p className="mt-1 text-xs text-console-muted">{run.run_ref} · GPU {run.gpu_uuids.length} 张 · epoch {run.current_epoch}/{run.total_epochs}</p></div><StatusTag tone={statusMeta[run.status].tone}>{statusMeta[run.status].label}</StatusTag><div className="text-sm text-console-muted">Loss {formatNumber(run.latest_metric?.loss)}</div></button>)}</div> : <div className="py-12 text-center"><FileText className="mx-auto h-8 w-8 text-console-muted" /><p className="mt-3 text-sm text-console-muted">{runs.length ? "没有符合筛选条件的任务。" : "还没有训练任务。请从“新建训练”开始模拟运行。"}</p></div>}</ConsoleCard>;
}

function ModelsPanel({ models, canManage, onSaved }: { models: TrainingModel[]; canManage: boolean; onSaved: (model: TrainingModel) => void }) {
  const [editingModelRef, setEditingModelRef] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [domain, setDomain] = useState("vla"); const [serverRef, setServerRef] = useState("fake-local");
  const [workingDirectory, setWorkingDirectory] = useState(emptyLaunchTemplate.working_directory); const [executable, setExecutable] = useState(emptyLaunchTemplate.executable);
  const [entrypoint, setEntrypoint] = useState(emptyLaunchTemplate.entrypoint); const [fixedArgv, setFixedArgv] = useState(""); const [outputRoot, setOutputRoot] = useState(emptyLaunchTemplate.output_root); const [outputFlag, setOutputFlag] = useState(emptyLaunchTemplate.output_flag);
  const [parameterDefinitions, setParameterDefinitions] = useState<TrainingParameterDefinition[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const textInput = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-console-text";
  const resetCreateMode = () => {
    setEditingModelRef(null); setName(""); setDescription("");
    setDomain(emptyLaunchTemplate.domain); setServerRef(emptyLaunchTemplate.server_ref); setWorkingDirectory(emptyLaunchTemplate.working_directory); setExecutable(emptyLaunchTemplate.executable);
    setEntrypoint(emptyLaunchTemplate.entrypoint); setFixedArgv(""); setOutputRoot(emptyLaunchTemplate.output_root); setOutputFlag(emptyLaunchTemplate.output_flag); setParameterDefinitions([]); setError(null);
  };
  const edit = (model: TrainingModel) => {
    const template = model.revision?.launch_template;
    if (!template || !model.revision) { setError("该模型缺少可编辑的 launch template，请重新加载管理员投影。"); return; }
    setEditingModelRef(model.model_ref); setName(model.name); setDescription(model.description ?? "");
    setDomain(template.domain); setServerRef(template.server_ref); setWorkingDirectory(template.working_directory); setExecutable(template.executable);
    setEntrypoint(template.entrypoint); setFixedArgv(template.fixed_argv.join("\n")); setOutputRoot(template.output_root); setOutputFlag(template.output_flag ?? "--output_dir"); setParameterDefinitions(model.revision.parameter_definitions.map((parameter) => ({ ...structuredClone(parameter), editable: true }))); setError(null);
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
    setBusy(true); setError(null);
    try {
      const launchTemplate = {
        domain, server_ref: serverRef, working_directory: workingDirectory, executable, entrypoint,
        // One argv token per line makes this a structured argv list, never a shell command.
        fixed_argv: fixedTokens, output_root: outputRoot, output_flag: outputFlag,
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
    setOutputRoot(navilaTrajectoryLaunchTemplate.output_root); setOutputFlag(navilaTrajectoryLaunchTemplate.output_flag); setParameterDefinitions(structuredClone(navilaTrajectoryParameters)); setError(null);
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
  return <div className="grid gap-4 xl:grid-cols-[.9fr_1.1fr]"><ConsoleCard><div className="mb-4 flex items-start justify-between gap-3"><div className="flex items-center gap-2"><Plus className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">{editingModelRef ? "编辑草稿模型" : "登记草稿模型"}</h2><p className="text-sm text-console-muted">{editingModelRef ? "保存时创建不可变的新 revision，不覆盖历史版本。" : "仅创建 draft；launch template 只用于生成模拟预览，绝不从浏览器执行。"}</p></div></div>{editingModelRef ? <ConsoleButton variant="ghost" onClick={resetCreateMode}>创建新模型</ConsoleButton> : null}</div>
    <div className="mb-4 rounded-md border border-sky-200 bg-sky-50 p-3"><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-sm font-medium text-sky-900">不知道从哪里开始？</p><p className="text-xs text-sky-800">载入同事命令对应的完整参数结构，再按部署环境修改占位路径。</p></div><ConsoleButton variant="ghost" disabled={!canManage} onClick={loadNavilaPreset}>一键载入 NaVILA 轨迹训练模板</ConsoleButton></div></div>
    <label className="block text-sm text-console-muted">名称<input className={textInput} value={name} disabled={!canManage} onChange={(event) => setName(event.target.value)} /></label><label className="mt-3 block text-sm text-console-muted">说明<textarea className="mt-1 min-h-16 w-full rounded-md border border-console-line bg-console-panel p-2 text-console-text" value={description} disabled={!canManage} onChange={(event) => setDescription(event.target.value)} /></label>
    <div className="mt-3 grid gap-3 sm:grid-cols-2"><label className="text-sm text-console-muted">领域 · Domain<input className={textInput} value={domain} disabled={!canManage} onChange={(e) => setDomain(e.target.value)} /></label><label className="text-sm text-console-muted">服务器标识 · Server ref<input className={textInput} value={serverRef} disabled={!canManage} onChange={(e) => setServerRef(e.target.value)} /></label><label className="text-sm text-console-muted">工作目录 · Working directory<input className={textInput} value={workingDirectory} disabled={!canManage} onChange={(e) => setWorkingDirectory(e.target.value)} /></label><label className="text-sm text-console-muted">启动程序 · Executable<input className={textInput} value={executable} disabled={!canManage} onChange={(e) => setExecutable(e.target.value)} /></label><label className="text-sm text-console-muted">训练入口 · Entrypoint<input className={textInput} value={entrypoint} disabled={!canManage} onChange={(e) => setEntrypoint(e.target.value)} /></label><label className="text-sm text-console-muted">输出根目录 · Output root<input className={textInput} value={outputRoot} disabled={!canManage} onChange={(e) => setOutputRoot(e.target.value)} /></label><label className="text-sm text-console-muted">输出参数标志 · Output flag<input className={textInput} value={outputFlag} disabled={!canManage} onChange={(e) => setOutputFlag(e.target.value)} /></label></div>
    <label className="mt-3 block text-sm text-console-muted">额外固定 argv（每行一个 token）<textarea className="mt-1 min-h-16 w-full rounded-md border border-console-line bg-console-panel p-2 font-mono text-xs text-console-text" value={fixedArgv} disabled={!canManage} onChange={(e) => setFixedArgv(e.target.value)} /></label><div className="mt-5"><ParameterDefinitionEditor definitions={parameterDefinitions} disabled={!canManage} onChange={setParameterDefinitions} /></div><p className="mt-3 text-xs text-console-muted">GPU、nnodes、nproc_per_node、master 地址/端口、node rank 和输出目录由平台管理，不注册为普通参数。</p><div className="mt-4 rounded-md border border-console-line bg-slate-950 p-3"><p className="mb-2 text-xs font-semibold text-slate-300">实时结构化命令摘要（默认值）</p><p className="font-mono text-xs leading-6 text-slate-100 break-all">{commandTokens.map((token) => /\s/.test(token) ? JSON.stringify(token) : token).join(" ")}</p></div>{error ? <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p> : null}<ConsoleButton className="mt-4" variant="primary" disabled={!canManage || busy || !name.trim() || !serverRef.trim() || !entrypoint.trim()} onClick={() => void save()}><Plus className="h-4 w-4" />{editingModelRef ? "创建新 revision" : "创建草稿"}</ConsoleButton></ConsoleCard><ConsoleCard><div className="mb-4 flex items-center gap-2"><BookOpen className="h-5 w-5 text-console-cyan" /><h2 className="font-semibold text-console-text">已登记模型</h2></div><div className="space-y-2">{models.map((model) => <div key={model.model_ref} className="rounded-md border border-console-line bg-console-panel2 p-3"><div className="flex items-center justify-between gap-2"><p className="font-medium text-console-text">{model.name}</p><StatusTag tone={model.status === "draft" ? "warning" : model.status === "verified" ? "success" : "neutral"}>{model.status}</StatusTag></div><p className="mt-1 text-sm text-console-muted">revision {model.latest_revision} · {model.description ?? "无说明"}</p><p className="mt-2 text-xs text-console-muted">参数：{model.revision?.parameter_definitions.map((p) => p.key).join("、") || "加载详情后显示"}</p>{canManage && model.status === "draft" ? <ConsoleButton className="mt-3" variant="ghost" onClick={() => edit(model)}>编辑并创建新 revision</ConsoleButton> : null}</div>)}{!models.length ? <p className="py-8 text-center text-sm text-console-muted">尚未登记模型。</p> : null}</div></ConsoleCard></div>;
}

function ResourcesPanel({ server, gpus }: { server: TrainingServer | null; gpus: TrainingGpuResource[] }) { return <ConsoleCard><div className="mb-4 flex items-center gap-2"><Server className="h-5 w-5 text-console-cyan" /><div><h2 className="font-semibold text-console-text">服务器资源</h2><p className="text-sm text-console-muted">每 2 秒刷新；数据来自 Fake Resource Provider。</p></div></div><p className="mb-4 text-sm text-console-muted">{server?.name ?? "未发现服务器"}</p><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">{gpus.map((gpu) => <div key={gpu.gpu_uuid} className="rounded-md border border-console-line bg-console-panel2 p-3"><div className="flex items-center justify-between"><span className="font-medium text-console-text">GPU {gpu.index}</span><Cpu className="h-4 w-4 text-violet-600" /></div><p className="mt-1 text-xs text-console-muted">{gpu.name} · {gpu.temperature_c}°C</p><ProgressBar className="mt-3" value={gpu.utilization_percent} tone="purple" label={`利用率 ${gpu.utilization_percent}%`} /><ProgressBar className="mt-3" value={gpu.total_memory_mib ? gpu.used_memory_mib / gpu.total_memory_mib * 100 : 0} tone="info" label={`显存 ${Math.round(gpu.used_memory_mib / 1024)}/${Math.round(gpu.total_memory_mib / 1024)} GiB`} /><p className={cn("mt-3 text-xs", gpu.lease_run_ref || gpu.externally_occupied ? "text-amber-700" : "text-emerald-700")}>{gpu.lease_run_ref ? `平台租约：${gpu.lease_run_ref}` : gpu.externally_occupied ? "外部占用" : "可用"}</p></div>)}</div></ConsoleCard>; }

export function TrainingPlatform() {
  const location = useLocation(); const navigate = useNavigate();
  const deepRunRef = useMemo(() => /^\/model\/runs\/([^/]+)\/?$/.exec(location.pathname)?.[1], [location.pathname]);
  const [tab, setTab] = useState<TrainingTab>("runs"); const [capabilities, setCapabilities] = useState<TrainingCapabilities | null>(null); const [models, setModels] = useState<TrainingModel[]>([]); const [servers, setServers] = useState<TrainingServer[]>([]); const [gpus, setGpus] = useState<TrainingGpuResource[]>([]); const [runs, setRuns] = useState<TrainingRun[]>([]); const [selectedRun, setSelectedRun] = useState<TrainingRun | null>(null); const [error, setError] = useState<string | null>(null); const [eventStreamDisconnected, setEventStreamDisconnected] = useState(false);
  const load = useCallback(async () => { try { const [nextCapabilities, nextModels, nextServers, nextRuns] = await Promise.all([getTrainingCapabilities(), listTrainingModels(), listTrainingServers(), listTrainingRuns()]); const resources = nextServers[0] ? await getTrainingServerResources(nextServers[0].server_ref) : null; setCapabilities(nextCapabilities); setModels(nextModels); setServers(nextServers); setRuns(nextRuns); setGpus(resources?.gpus ?? []); setSelectedRun((current) => current ? nextRuns.find((item) => item.run_ref === current.run_ref) ?? current : null); setError(null); } catch (caught) { setError(errorText(caught)); } }, []);
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
  if (!capabilities && !error) return <LoadingCard />;
  return <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6"><div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between"><SegmentedTabs idPrefix="training-platform" value={tab} tabs={tabs} onChange={setTab} aria-label="训练平台视图" /><div className="flex items-center gap-2"><StatusTag tone="warning">真实训练未启用</StatusTag><StatusTag tone={capabilities?.simulation_enabled ? "success" : "danger"}>{capabilities?.simulation_enabled ? "模拟模式" : "模拟不可用"}</StatusTag></div></div>{eventStreamDisconnected ? <div role="status" className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">事件流已断开，正在使用轮询恢复。</div> : null}{error ? <ConsoleCard><p role="alert" className="text-sm text-rose-700">{error}</p><ConsoleButton className="mt-3" onClick={() => void load()}><RefreshCw className="h-4 w-4" />重新加载</ConsoleButton></ConsoleCard> : null}<div hidden={tab !== "runs"}><RunsPanel runs={runs} selectedRun={selectedRun} canStop={can(capabilities, "training:stop_runs")} onSelect={selectRun} onRunChange={updateRun} /></div><div hidden={tab !== "new"}><NewRunPanel models={models} servers={servers} gpus={gpus} canCreate={can(capabilities, "training:create_runs")} onCreated={(run) => { updateRun(run); setTab("runs"); selectRun(run); }} /></div><div hidden={tab !== "models"}><ModelsPanel models={models} canManage={can(capabilities, "training:manage_models")} onSaved={(model) => setModels((current) => [model, ...current.filter((item) => item.model_ref !== model.model_ref)])} /></div><div hidden={tab !== "resources"}><ResourcesPanel server={servers[0] ?? null} gpus={gpus} /></div></section>;
}
