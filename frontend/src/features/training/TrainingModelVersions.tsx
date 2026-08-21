import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Clock3,
  Database,
  ExternalLink,
  FileArchive,
  FolderCheck,
  HardDrive,
  LoaderCircle,
  RefreshCw,
  Search,
  Server,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getTrainingModelVersion,
  inspectTrainingModelVersionArtifact,
  listTrainingModelVersionFamilies,
  listTrainingModelVersions,
} from "../../api/client";
import type {
  TrainingArtifactAvailabilityStatus,
  TrainingModelVersionDetail,
  TrainingModelVersionFamily,
  TrainingModelVersionSummary,
} from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { StatusTag } from "../../components/console/StatusTag";
import { cn } from "../../lib/utils";

const artifactStatusMeta: Record<TrainingArtifactAvailabilityStatus, { label: string; tone: "success" | "warning" | "danger" | "neutral" }> = {
  unchecked: { label: "待检查", tone: "neutral" },
  checking: { label: "检查中", tone: "warning" },
  available: { label: "产物可用", tone: "success" },
  missing: { label: "产物缺失", tone: "danger" },
  unreadable: { label: "无法读取", tone: "danger" },
  unsafe: { label: "产物异常", tone: "danger" },
  check_failed: { label: "检查失败", tone: "warning" },
};

function errorText(error: unknown) {
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "--";
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor(rounded % 3600 / 60);
  const rest = rounded % 60;
  return hours ? `${hours} 小时 ${minutes} 分` : minutes ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`;
}

function formatBytes(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "--";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = Math.max(0, value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function formatMetric(value: number | null | undefined, digits = 4) {
  if (value == null || !Number.isFinite(value)) return "--";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function snapshotDates(snapshot: Record<string, unknown> | null | undefined, split: "train" | "test") {
  if (!snapshot) return [];
  const dateList = snapshot[`${split}_dates`];
  const direct = snapshot[split];
  const splits = snapshot.splits && typeof snapshot.splits === "object" ? (snapshot.splits as Record<string, unknown>)[split] : undefined;
  const manifest = snapshot.manifest && typeof snapshot.manifest === "object" ? snapshot.manifest as Record<string, unknown> : undefined;
  const manifestSplits = manifest?.splits && typeof manifest.splits === "object" ? (manifest.splits as Record<string, unknown>)[split] : undefined;
  const items = [dateList, direct, splits, manifestSplits].find(Array.isArray) as unknown[] | undefined;
  return (items ?? []).map((item) => {
    if (typeof item === "string") return item;
    if (!item || typeof item !== "object") return "";
    const record = item as Record<string, unknown>;
    return String(record.dataset_date ?? record.date ?? "");
  }).filter(Boolean);
}

function isInspectionStale(version: TrainingModelVersionDetail) {
  if (version.default_artifact.status === "unchecked") return true;
  if (!version.default_artifact.checked_at) return false;
  const checkedAt = new Date(version.default_artifact.checked_at).getTime();
  return Number.isFinite(checkedAt) && Date.now() - checkedAt > 10 * 60 * 1000;
}

function VersionCard({ version, onOpen }: { version: TrainingModelVersionSummary; onOpen: () => void }) {
  const artifact = artifactStatusMeta[version.default_artifact.status];
  return (
    <article className="flex min-h-52 flex-col rounded-xl border border-console-line bg-console-panel p-4 shadow-[0_8px_24px_rgba(31,42,68,0.045)] transition-[border-color,box-shadow,transform] duration-180 hover:-translate-y-0.5 hover:border-console-cyan/35 hover:shadow-[0_12px_28px_rgba(31,42,68,0.08)] motion-reduce:transform-none motion-reduce:transition-none">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0"><h4 className="truncate text-base font-semibold text-console-text">{version.version_label}</h4><p className="mt-1 text-xs text-console-muted">完成于 {formatDateTime(version.finished_at)}</p></div>
        <StatusTag tone={artifact.tone}>{artifact.label}</StatusTag>
      </div>
      <p className="mt-3 line-clamp-2 min-h-10 text-sm leading-5 text-console-muted" title={version.version_description ?? undefined}>{version.version_description?.trim() || "历史版本未填写说明。"}</p>
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 border-t border-console-line pt-3 text-xs">
        <div><dt className="text-console-muted">最终训练 Loss</dt><dd className="mt-1 font-semibold text-console-text">{formatMetric(version.final_loss)}</dd></div>
        <div><dt className="text-console-muted">完成 Step</dt><dd className="mt-1 font-semibold text-console-text">{formatMetric(version.final_step, 0)}</dd></div>
        <div><dt className="text-console-muted">训练耗时</dt><dd className="mt-1 font-semibold text-console-text">{formatDuration(version.duration_seconds)}</dd></div>
        <div><dt className="text-console-muted">数据日期</dt><dd className="mt-1 font-semibold text-console-text">训练 {version.train_date_count} · 测试 {version.test_date_count}</dd></div>
      </dl>
      <button type="button" className="mt-4 inline-flex items-center justify-end gap-1 text-sm font-medium text-console-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={onOpen}>查看版本 <ChevronRight className="h-4 w-4" aria-hidden="true" /></button>
    </article>
  );
}

function FamilyVersions({ family, onOpenVersion }: { family: TrainingModelVersionFamily; onOpenVersion: (versionRef: string) => void }) {
  const [versions, setVersions] = useState<TrainingModelVersionSummary[]>([]);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (after?: string) => {
    setLoading(true); setError(null);
    try {
      const page = await listTrainingModelVersions(family.family_ref, { after, limit: 6 });
      setVersions((current) => after ? [...current, ...page.versions.filter((item) => !current.some((existing) => existing.version_ref === item.version_ref))] : page.versions);
      setNextAfter(page.next_after ?? null);
    } catch (caught) { setError(errorText(caught)); }
    finally { setLoading(false); }
  }, [family.family_ref]);

  useEffect(() => { void load(); }, [load]);

  if (error && !versions.length) return <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"><p>{error}</p><button type="button" className="mt-2 font-medium underline" onClick={() => void load()}>重新加载</button></div>;
  if (loading && !versions.length) return <div role="status" className="flex items-center justify-center gap-2 py-10 text-sm text-console-muted"><LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" />正在加载模型版本…</div>;
  if (!versions.length) return <div className="rounded-lg border border-dashed border-console-line py-10 text-center text-sm text-console-muted">该模型族暂无可用版本。</div>;
  return <div>
    <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">{versions.map((version) => <VersionCard key={version.version_ref} version={version} onOpen={() => onOpenVersion(version.version_ref)} />)}</div>
    {nextAfter ? <div className="mt-4 flex justify-center"><ConsoleButton disabled={loading} onClick={() => void load(nextAfter)}>{loading ? <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <ChevronDown className="h-4 w-4" />}{loading ? "正在加载…" : "加载更多版本"}</ConsoleButton></div> : null}
    {error ? <p role="alert" className="mt-3 text-center text-sm text-rose-700">{error}</p> : null}
  </div>;
}

function ModelVersionLibrary({ onOpenVersion }: { onOpenVersion: (versionRef: string) => void }) {
  const [query, setQuery] = useState("");
  const [requestQuery, setRequestQuery] = useState("");
  const [families, setFamilies] = useState<TrainingModelVersionFamily[]>([]);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [expandedFamilyRef, setExpandedFamilyRef] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { const timer = window.setTimeout(() => setRequestQuery(query.trim()), 300); return () => window.clearTimeout(timer); }, [query]);
  const load = useCallback(async (after?: string) => {
    setLoading(true); setError(null);
    try {
      const page = await listTrainingModelVersionFamilies({ query: requestQuery, after, limit: 20 });
      setFamilies((current) => after ? [...current, ...page.families.filter((item) => !current.some((existing) => existing.family_ref === item.family_ref))] : page.families);
      setNextAfter(page.next_after ?? null);
      if (!after) setExpandedFamilyRef(null);
    } catch (caught) { setError(errorText(caught)); }
    finally { setLoading(false); }
  }, [requestQuery]);
  useEffect(() => { void load(); }, [load]);

  return <section aria-labelledby="training-model-versions-heading" className="border-b border-console-line bg-console-panel">
    <header className="flex flex-col gap-4 py-5 lg:flex-row lg:items-end lg:justify-between">
      <div><div className="flex items-center gap-2"><FileArchive className="h-5 w-5 text-console-cyan" aria-hidden="true" /><h2 id="training-model-versions-heading" className="text-lg font-semibold text-console-text">模型版本</h2></div><p className="mt-1 text-sm text-console-muted">按模型族管理训练成功后的版本模型，为后续测试和对比保留完整训练快照。</p></div>
      <label className="relative min-w-0 sm:w-72"><span className="sr-only">搜索模型族</span><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" aria-hidden="true" /><input type="search" aria-label="搜索模型族" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索模型族" className="h-9 w-full rounded-md border border-console-line bg-console-panel pl-9 pr-3 text-sm text-console-text outline-none focus-visible:border-console-cyan focus-visible:ring-2 focus-visible:ring-console-cyan/15" /></label>
    </header>
    {error && !families.length ? <div className="mb-5 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"><p>{error}</p><ConsoleButton className="mt-3" onClick={() => void load()}><RefreshCw className="h-4 w-4" />重新加载</ConsoleButton></div> : null}
    {loading && !families.length ? <div role="status" className="flex items-center justify-center gap-2 border-t border-console-line py-20 text-sm text-console-muted"><LoaderCircle className="h-5 w-5 animate-spin motion-reduce:animate-none" />正在加载模型版本…</div> : null}
    {!loading && !error && !families.length ? <div className="border-t border-console-line py-20 text-center"><FileArchive className="mx-auto h-9 w-9 text-console-muted" /><p className="mt-3 font-medium text-console-text">{requestQuery ? "没有匹配的模型族" : "暂无可用模型版本"}</p><p className="mt-1 text-sm text-console-muted">真实训练成功并登记版本模型后，会显示在这里。</p></div> : null}
    <div className="space-y-3 border-t border-console-line py-4">{families.map((family) => {
      const expanded = expandedFamilyRef === family.family_ref;
      return <article key={family.family_ref} className={cn("overflow-hidden rounded-xl border bg-console-panel transition-[border-color,box-shadow] duration-180 motion-reduce:transition-none", expanded ? "border-console-cyan/40 shadow-[0_10px_28px_rgba(31,42,68,0.07)]" : "border-console-line")}>
        <button type="button" aria-expanded={expanded} className="flex w-full items-center gap-4 px-4 py-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-console-cyan/35" onClick={() => setExpandedFamilyRef(expanded ? null : family.family_ref)}>
          <span className={cn("grid h-10 w-10 shrink-0 place-items-center rounded-lg", expanded ? "bg-sky-50 text-console-cyan" : "bg-console-panel2 text-console-muted")}><FileArchive className="h-5 w-5" aria-hidden="true" /></span>
          <span className="min-w-0 flex-1"><span className="block truncate font-semibold text-console-text">{family.family_name}</span><span className="mt-1 block truncate text-sm text-console-muted">{family.latest_version ? `最新 ${family.latest_version.version_label} · ${family.latest_version.version_description?.trim() || "未填写版本说明"}` : "暂无可用版本"}</span></span>
          <span className="hidden shrink-0 text-right sm:block"><span className="block text-sm font-semibold text-console-text">{family.available_version_count} 个版本</span><span className="mt-1 block text-xs text-console-muted">最近完成 {formatDateTime(family.latest_version?.finished_at)}</span></span>
          <ChevronDown className={cn("h-5 w-5 shrink-0 text-console-muted transition-transform duration-180 motion-reduce:transition-none", expanded && "rotate-180")} aria-hidden="true" />
        </button>
        {expanded ? <div className="border-t border-console-line bg-console-panel2/35 p-4"><FamilyVersions family={family} onOpenVersion={onOpenVersion} /></div> : null}
      </article>;
    })}</div>
    {nextAfter ? <div className="flex justify-center pb-5"><ConsoleButton disabled={loading} onClick={() => void load(nextAfter)}>{loading ? <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <ChevronDown className="h-4 w-4" />}{loading ? "正在加载…" : "加载更多模型族"}</ConsoleButton></div> : null}
  </section>;
}

function DetailStat({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-lg border border-console-line bg-console-panel2 px-3 py-3"><dt className="text-xs text-console-muted">{label}</dt><dd className="mt-1 truncate text-sm font-semibold text-console-text" title={value}>{value}</dd></div>;
}

function ModelVersionDetail({ versionRef, canInspect, eventRevision, onBack, onOpenRun }: { versionRef: string; canInspect: boolean; eventRevision: number; onBack: () => void; onOpenRun: (runRef: string) => void }) {
  const [version, setVersion] = useState<TrainingModelVersionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const autoChecked = useRef(new Set<string>());

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try { setVersion(await getTrainingModelVersion(versionRef)); setError(null); }
    catch (caught) { setError(errorText(caught)); }
    finally { if (!quiet) setLoading(false); }
  }, [versionRef]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (eventRevision) void load(true); }, [eventRevision, load]);

  const inspectionBlockedReason = useMemo(() => {
    if (!version || !canInspect) return null;
    if (version.artifact_inspection_supported === false) return "当前 Worker 版本不支持产物检查，请先在训练节点页更新 Worker。";
    if (version.node_status && version.node_status !== "online") return "训练节点当前离线，暂时无法刷新产物状态。";
    return null;
  }, [canInspect, version]);
  const inspectionAllowed = canInspect && !inspectionBlockedReason;

  const checkArtifact = useCallback(async () => {
    if (!inspectionAllowed) return;
    setChecking(true); setError(null);
    try {
      const result = await inspectTrainingModelVersionArtifact(versionRef);
      if (result.version) setVersion(result.version);
      else await load(true);
    } catch (caught) { setError(errorText(caught)); }
    finally { setChecking(false); }
  }, [inspectionAllowed, load, versionRef]);

  useEffect(() => {
    if (!version || !inspectionAllowed || !isInspectionStale(version) || autoChecked.current.has(versionRef)) return;
    autoChecked.current.add(versionRef); void checkArtifact();
  }, [checkArtifact, inspectionAllowed, version, versionRef]);
  useEffect(() => {
    if (version?.default_artifact.status !== "checking") return;
    const interval = window.setInterval(() => void load(true), 2000);
    return () => window.clearInterval(interval);
  }, [load, version?.default_artifact.status]);

  if (loading && !version) return <ConsoleCard><div role="status" className="flex items-center justify-center gap-2 py-16 text-sm text-console-muted"><LoaderCircle className="h-5 w-5 animate-spin motion-reduce:animate-none" />正在加载模型版本…</div></ConsoleCard>;
  if (!version) return <ConsoleCard><ConsoleButton onClick={onBack}><ArrowLeft className="h-4 w-4" />返回模型版本</ConsoleButton><p role="alert" className="mt-5 text-sm text-rose-700">{error ?? "未找到模型版本。"}</p></ConsoleCard>;

  const artifactMeta = artifactStatusMeta[version.default_artifact.status];
  const trainDates = snapshotDates(version.dataset_snapshot, "train");
  const testDates = snapshotDates(version.dataset_snapshot, "test");
  const checkpoints = version.artifacts.filter((item) => item.kind === "checkpoint");
  return <section className="space-y-4" aria-labelledby="model-version-detail-heading">
    <div className="flex flex-wrap items-center justify-between gap-3"><ConsoleButton onClick={onBack}><ArrowLeft className="h-4 w-4" />返回模型版本</ConsoleButton><ConsoleButton variant="primary" onClick={() => onOpenRun(version.run_ref)}>查看原训练任务 <ExternalLink className="h-4 w-4" /></ConsoleButton></div>
    {error ? <div role="alert" className="flex items-start gap-2 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{error}</div> : null}
    <ConsoleCard>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h2 id="model-version-detail-heading" className="text-xl font-semibold text-console-text">{version.family_name} {version.version_label}</h2><StatusTag tone={artifactMeta.tone}>{artifactMeta.label}</StatusTag></div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-console-muted">{version.version_description?.trim() || "历史版本未填写说明。"}</p></div><div className="shrink-0 text-sm text-console-muted">完成于 {formatDateTime(version.finished_at)}</div></div>
      <dl className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5"><DetailStat label="最终训练 Loss" value={formatMetric(version.final_loss)} /><DetailStat label="最终 Step" value={formatMetric(version.final_step, 0)} /><DetailStat label="最终学习率" value={formatMetric(version.final_learning_rate, 8)} /><DetailStat label="训练耗时" value={formatDuration(version.duration_seconds)} /><DetailStat label="Checkpoint" value={`${version.checkpoint_count} 个`} /></dl>
    </ConsoleCard>
    <ConsoleCard>
      <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex items-center gap-2"><FolderCheck className="h-5 w-5 text-console-cyan" /><div><h3 className="font-semibold text-console-text">版本模型</h3><p className="text-sm text-console-muted">最后成功训练阶段的输出目录，不代表经过验证的最佳检查点。</p></div></div>{canInspect ? <ConsoleButton disabled={!inspectionAllowed || checking || version.default_artifact.status === "checking"} onClick={() => void checkArtifact()}>{checking || version.default_artifact.status === "checking" ? <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <RefreshCw className="h-4 w-4" />}{checking || version.default_artifact.status === "checking" ? "正在检查…" : "重新检查"}</ConsoleButton> : null}</div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-3"><DetailStat label="文件数量" value={version.default_artifact.file_count == null ? "--" : `${version.default_artifact.file_count.toLocaleString()} 个`} /><DetailStat label="目录大小" value={formatBytes(version.default_artifact.total_bytes)} /><DetailStat label="最后检查" value={formatDateTime(version.default_artifact.checked_at)} /></dl>
      <div className="mt-3 rounded-lg border border-console-line bg-console-panel2 px-3 py-3"><p className="text-xs text-console-muted">版本模型目录</p><p className="mt-1 break-all font-mono text-sm text-console-text">{version.default_artifact.path ?? "当前权限不显示训练节点物理路径。"}</p></div>
      {version.default_artifact.message ? <p className="mt-3 text-sm text-console-muted">{version.default_artifact.message}</p> : null}
      {inspectionBlockedReason ? <p className="mt-3 flex items-center gap-2 text-sm text-amber-700"><AlertTriangle className="h-4 w-4 shrink-0" />{inspectionBlockedReason}</p> : null}
    </ConsoleCard>
    <div className="grid gap-4 xl:grid-cols-2">
      <ConsoleCard><div className="flex items-center gap-2"><Database className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">训练数据快照</h3></div><div className="mt-4 space-y-4"><div><p className="text-xs font-medium text-console-muted">训练集 · {version.train_date_count} 个日期</p><div className="mt-2 flex flex-wrap gap-2">{trainDates.length ? trainDates.map((date) => <span key={date} className="rounded-md bg-sky-50 px-2 py-1 text-xs font-medium text-sky-700">{date}</span>) : <span className="text-sm text-console-muted">未提供日期明细</span>}</div></div><div><p className="text-xs font-medium text-console-muted">测试集 · {version.test_date_count} 个日期</p><div className="mt-2 flex flex-wrap gap-2">{testDates.length ? testDates.map((date) => <span key={date} className="rounded-md bg-violet-50 px-2 py-1 text-xs font-medium text-violet-700">{date}</span>) : <span className="text-sm text-console-muted">未设置测试集</span>}</div></div></div></ConsoleCard>
      <ConsoleCard><div className="flex items-center gap-2"><Server className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">训练资源</h3></div><dl className="mt-4 grid gap-3 sm:grid-cols-2"><DetailStat label="训练节点" value={version.server_name ?? version.server_ref} /><DetailStat label="GPU" value={`${version.gpu_uuids.length} 张`} /><DetailStat label="训练阶段" value={`${version.stage_count} 个`} /><DetailStat label="版本引用" value={version.version_ref} /></dl></ConsoleCard>
    </div>
    <ConsoleCard><div className="flex items-center gap-2"><Clock3 className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">训练阶段与参数快照</h3></div><div className="mt-4 space-y-2">{version.stages.map((stage) => <details key={stage.stage_ref} className="rounded-lg border border-console-line bg-console-panel2 px-3 py-2"><summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium text-console-text"><span>{stage.stage_name}</span><StatusTag tone={stage.status === "succeeded" ? "success" : stage.status === "failed" || stage.status === "lost" ? "danger" : "neutral"}>{stage.status === "succeeded" ? "已完成" : stage.status}</StatusTag></summary><dl className="mt-3 grid gap-x-4 gap-y-2 border-t border-console-line pt-3 sm:grid-cols-2">{Object.entries(stage.parameters ?? {}).map(([key, value]) => <div key={key} className="min-w-0"><dt className="truncate font-mono text-xs text-console-muted">{key}</dt><dd className="mt-0.5 break-all text-sm text-console-text">{String(value)}</dd></div>)}</dl></details>)}</div></ConsoleCard>
    <ConsoleCard><div className="flex items-center gap-2"><HardDrive className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">Checkpoint 记录</h3></div>{checkpoints.length ? <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[560px] text-left text-sm"><thead className="text-xs text-console-muted"><tr><th className="pb-2">所属阶段</th><th className="pb-2">Step</th><th className="pb-2">相对路径</th></tr></thead><tbody>{checkpoints.map((item, index) => <tr key={item.artifact_ref ?? `${item.relative_path}-${index}`} className="border-t border-console-line"><td className="py-2.5">{item.stage_number ? `第 ${item.stage_number} 阶段` : "--"}</td><td className="py-2.5">{item.step ?? "--"}</td><td className="py-2.5 font-mono text-xs">{item.relative_path ?? "--"}</td></tr>)}</tbody></table></div> : <p className="mt-4 text-sm text-console-muted">该版本没有登记 checkpoint 事件。</p>}</ConsoleCard>
  </section>;
}

export function TrainingModelVersions({ active, versionRef, canInspect, eventRevision, onOpenVersion, onBack, onOpenRun }: { active: boolean; versionRef?: string; canInspect: boolean; eventRevision: number; onOpenVersion: (versionRef: string) => void; onBack: () => void; onOpenRun: (runRef: string) => void }) {
  if (!active) return null;
  return versionRef ? <ModelVersionDetail versionRef={versionRef} canInspect={canInspect} eventRevision={eventRevision} onBack={onBack} onOpenRun={onOpenRun} /> : <ModelVersionLibrary onOpenVersion={onOpenVersion} />;
}
