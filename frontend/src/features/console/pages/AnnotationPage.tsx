import * as Dialog from "@radix-ui/react-dialog";
import {
  AlertCircle,
  ArrowLeft,
  Ban,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  CircleDot,
  History,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  SkipForward,
  Tags,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useBlocker,
  useNavigate,
  useParams,
} from "react-router-dom";

import { getNavigationDatasetDate } from "../../../api/client";
import type { NavigationDateSummary } from "../../../api/types";
import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { StatusTag } from "../../../components/console/StatusTag";
import {
  getNavigationDatasetSummaryCached,
  resetNavigationDatasetSummaryCache,
} from "../navigationDatasetSummaryCache";
import { InitialAnnotationWorkbench } from "../../annotation/InitialAnnotationWorkbench";
import {
  AnnotationApiError,
  createAnnotationJob,
  getAnnotationCapabilities,
  getAnnotationJob,
  getAnnotationSegment,
  getCalibrationProfiles,
  listAnnotationJobs,
  mutateAnnotationJob,
  mutateAnnotationSegment,
  skipAnnotationSegment,
} from "../../annotation/api";
import type {
  AnnotationCapability,
  AnnotationJobDetail,
  AnnotationJobStatus,
  AnnotationJobSummary,
  AnnotationSegmentDetail,
  AnnotationSegmentSummary,
  AnnotationSegmentStatus,
  CalibrationProfile,
} from "../../annotation/types";

const POLL_INTERVAL_MS = 2500;
const HISTORY_PAGE_SIZE = 5;
const JOB_TABLE_GRID_CLASS =
  "grid-cols-[5.5rem_minmax(7rem,1.15fr)_7rem_minmax(7.5rem,1fr)_6.75rem_7.5rem_minmax(3.5rem,.5fr)] xl:grid-cols-[6rem_9.5rem_7rem_15rem_7rem_8rem_3.5rem]";
const JOB_TABLE_GRID_LARGE_CLASS =
  "lg:grid-cols-[5.5rem_minmax(7rem,1.15fr)_7rem_minmax(7.5rem,1fr)_6.75rem_7.5rem_minmax(3.5rem,.5fr)] xl:grid-cols-[6rem_9.5rem_7rem_15rem_7rem_8rem_3.5rem]";

const JOB_STATUS: Record<AnnotationJobStatus, { label: string; tone: "success" | "info" | "warning" | "danger" | "neutral" }> = {
  preparing: { label: "准备中", tone: "info" },
  waiting_initial_annotation: { label: "待首帧标注", tone: "warning" },
  tracking: { label: "Tracking 中", tone: "info" },
  tracked: { label: "Tracking 已完成", tone: "success" },
  failed: { label: "处理失败", tone: "danger" },
  cancelled: { label: "已取消", tone: "neutral" },
};

const SEGMENT_STATUS: Record<AnnotationSegmentStatus, { label: string; tone: "success" | "info" | "warning" | "danger" | "neutral" }> = {
  pending_initial_annotation: { label: "待标注", tone: "warning" },
  draft: { label: "草稿", tone: "info" },
  submitted: { label: "已提交", tone: "success" },
  skipped: { label: "已跳过", tone: "neutral" },
  tracking: { label: "Tracking 中", tone: "info" },
  tracked: { label: "已完成", tone: "success" },
};

function safeError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback;
  const code = error instanceof AnnotationApiError ? error.detail?.code : null;
  const looksPrivate = /(?:^|[\s("'`])\/(?:[^/\s]+\/){2,}|[A-Za-z]:\\/.test(message);
  if (looksPrivate) return code ? `${fallback}（${code}）` : fallback;
  return message || fallback;
}

function formattedTime(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.valueOf())) return timestamp;
  const padded = (value: number) => String(value).padStart(2, "0");
  return [
    `${date.getFullYear()}-${padded(date.getMonth() + 1)}-${padded(date.getDate())}`,
    `${padded(date.getHours())}:${padded(date.getMinutes())}`,
  ].join(" ");
}

function activeJob(status: AnnotationJobStatus): boolean {
  return status === "preparing" || status === "waiting_initial_annotation" || status === "tracking";
}

function preferMonotonicJob<T extends AnnotationJobSummary>(
  current: T | null | undefined,
  next: T,
): T {
  if (!current || current.job_ref !== next.job_ref) return next;
  if (next.state_revision < current.state_revision) return current;
  if (
    next.state_revision === current.state_revision
    && current.cancel_requested
    && !next.cancel_requested
  ) {
    return current;
  }
  return next;
}

function mergeMonotonicJobs(
  current: AnnotationJobSummary[],
  next: AnnotationJobSummary[],
): AnnotationJobSummary[] {
  const currentByRef = new Map(current.map((job) => [job.job_ref, job]));
  return next.map((job) => preferMonotonicJob(currentByRef.get(job.job_ref), job));
}

function resolvedSegmentCount(job: Pick<AnnotationJobSummary, "counts">): number {
  return (
    job.counts.submitted
    + job.counts.skipped
    + job.counts.tracking
    + job.counts.tracked
  );
}

function countsSummary(job: AnnotationJobSummary): string {
  const resolved = resolvedSegmentCount(job);
  return `${resolved}/${job.counts.total} 个 segment 已处理`;
}

function jobStatusLabel(job: AnnotationJobSummary): string {
  if (job.cancel_requested) return "正在取消";
  if (job.completion_outcome === "no_processable_targets") return "无可处理目标";
  return JOB_STATUS[job.status].label;
}

function countsFromSegments(segments: AnnotationSegmentSummary[]) {
  return {
    total: segments.length,
    pending_initial_annotation: segments.filter((item) => item.status === "pending_initial_annotation").length,
    draft: segments.filter((item) => item.status === "draft").length,
    submitted: segments.filter((item) => item.status === "submitted").length,
    skipped: segments.filter((item) => item.status === "skipped").length,
    tracking: segments.filter((item) => item.status === "tracking").length,
    tracked: segments.filter((item) => item.status === "tracked").length,
  };
}

function PageMessage({
  icon: Icon = AlertCircle,
  title,
  detail,
  action,
}: {
  icon?: typeof AlertCircle;
  title: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  return (
    <ConsoleCard>
      <div className="flex min-h-48 flex-col items-center justify-center text-center">
        <Icon className="h-8 w-8 text-console-muted" aria-hidden="true" />
        <h2 className="mt-3 text-base font-semibold text-console-text">{title}</h2>
        {detail && <p className="mt-2 max-w-xl text-sm leading-6 text-console-muted">{detail}</p>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </ConsoleCard>
  );
}

function DataRouterFlushBlocker({
  enabled,
  flush,
}: {
  enabled: boolean;
  flush: () => Promise<boolean>;
}) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => (
      enabled && currentLocation.pathname !== nextLocation.pathname
    ),
  );

  useEffect(() => {
    if (blocker.state !== "blocked") return;
    let active = true;
    void flush().then((saved) => {
      if (!active || blocker.state !== "blocked") return;
      if (saved) blocker.proceed();
      else blocker.reset();
    });
    return () => {
      active = false;
    };
  }, [blocker, flush]);

  return null;
}

function JobRow({
  job,
  onOpen,
  actionLabel,
}: {
  job: AnnotationJobSummary;
  onOpen: () => void;
  actionLabel?: string;
}) {
  const status = JOB_STATUS[job.status];
  const resolved = resolvedSegmentCount(job);
  const progress = job.counts.total > 0
    ? Math.round((resolved / job.counts.total) * 100)
    : 0;
  return (
    <div
      className={`grid gap-2 border-b border-console-line px-3 py-2 last:border-b-0 ${JOB_TABLE_GRID_LARGE_CLASS} lg:min-h-11 lg:items-center`}
      data-testid="annotation-job-row"
    >
      <p className="text-xs font-normal tabular-nums text-console-muted">{job.dataset_date}</p>
      <div className="min-w-0">
        <p className="truncate text-xs font-normal text-console-muted" title={job.source_clips.join("、")}>
          {job.source_clips.join("、")}
        </p>
      </div>
      <div className="whitespace-nowrap">
        <StatusTag tone={job.cancel_requested ? "warning" : status.tone}>
          {jobStatusLabel(job)}
        </StatusTag>
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-xs tabular-nums text-console-text">{resolved}/{job.counts.total}</span>
          <span className="h-1.5 w-20 shrink-0 overflow-hidden rounded-full bg-slate-200 xl:w-28" aria-hidden="true">
            <span className="block h-full rounded-full bg-console-cyan" style={{ width: `${progress}%` }} />
          </span>
        </div>
      </div>
      <p className="truncate text-xs text-console-muted" title={job.calibration.label}>
        {job.calibration.label}
      </p>
      <p className="whitespace-nowrap text-[11px] tabular-nums text-console-muted">
        {formattedTime(job.updated_at)}
      </p>
      <button
        type="button"
        className="justify-self-start text-sm font-medium text-console-cyan transition hover:text-blue-700 hover:underline focus:outline-none focus-visible:underline"
        aria-label={actionLabel ? `${actionLabel} ${job.dataset_date}` : `查看任务 ${job.dataset_date}`}
        onClick={onOpen}
      >
        {actionLabel ?? "查看"}
      </button>
    </div>
  );
}

function JobsSection({
  title,
  jobs,
  empty,
  onOpen,
  actionLabel,
  tone = "neutral",
}: {
  title: string;
  jobs: AnnotationJobSummary[];
  empty: string;
  onOpen: (jobRef: string) => void;
  actionLabel?: string;
  tone?: "neutral" | "danger";
}) {
  return (
    <section className={`overflow-hidden rounded-lg border bg-console-panel ${
      tone === "danger" ? "border-rose-200" : "border-console-line"
    }`}>
      <div className={`flex items-start justify-between gap-3 px-4 py-2.5 ${
        tone === "danger" ? "bg-rose-50/70" : ""
      }`}>
        <div>
          <h3 className={`text-sm font-semibold ${tone === "danger" ? "text-rose-800" : "text-console-text"}`}>
            {title}
          </h3>
        </div>
        {jobs.length > 0 && (
          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
            tone === "danger" ? "bg-rose-100 text-rose-700" : "bg-console-panel2 text-console-muted"
          }`}>
            {jobs.length}
          </span>
        )}
      </div>
      {jobs.length > 0 && (
        <div className="mx-4 mb-3 overflow-x-auto border-y border-console-line">
          <div className="min-w-[47rem]">
            <div
              className={`hidden gap-2 border-b border-console-line bg-slate-100/80 px-3 py-1.5 text-[11px] font-medium text-console-muted ${JOB_TABLE_GRID_LARGE_CLASS} lg:grid`}
              data-testid="annotation-job-table-header"
            >
              <span>数据日期</span>
              <span>外层 clips</span>
              <span>状态</span>
              <span>Segment 进度</span>
              <span>处理标定</span>
              <span>更新时间</span>
              <span>操作</span>
            </div>
            {jobs.map((job) => (
              <JobRow
                key={job.job_ref}
                job={job}
                actionLabel={actionLabel}
                onOpen={() => onOpen(job.job_ref)}
              />
            ))}
          </div>
        </div>
      )}
      {jobs.length === 0 && (
        <p className="px-4 py-5 text-sm text-console-muted">{empty}</p>
      )}
    </section>
  );
}

function JobsPage() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<AnnotationJobSummary[]>([]);
  const [capability, setCapability] = useState<AnnotationCapability | null>(null);
  const [profiles, setProfiles] = useState<CalibrationProfile[]>([]);
  const [dates, setDates] = useState<NavigationDateSummary[]>([]);
  const [selectedDate, setSelectedDate] = useState("");
  const [dateDetail, setDateDetail] = useState<NavigationDateSummary | null>(null);
  const [selectedClips, setSelectedClips] = useState<string[]>([]);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [historyQuery, setHistoryQuery] = useState("");
  const [historyStatus, setHistoryStatus] = useState<"all" | AnnotationJobStatus>("all");
  const [historyStartDate, setHistoryStartDate] = useState("");
  const [historyEndDate, setHistoryEndDate] = useState("");
  const [historyPage, setHistoryPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [pageError, setPageError] = useState("");
  const [createError, setCreateError] = useState("");
  const [createDialogAttention, setCreateDialogAttention] = useState<0 | 1 | 2>(0);
  const selectedDateRef = useRef("");
  const dateRequestGenerationRef = useRef(0);
  const createDialogRef = useRef<HTMLDivElement | null>(null);
  const createDialogAttentionTimerRef = useRef<number | null>(null);
  const createDialogLastAttentionAtRef = useRef(0);

  useEffect(() => () => {
    if (createDialogAttentionTimerRef.current !== null) {
      window.clearTimeout(createDialogAttentionTimerRef.current);
    }
  }, []);

  useEffect(() => {
    if (!showCreate) return;
    setCreateDialogAttention(0);
    createDialogLastAttentionAtRef.current = 0;
  }, [showCreate]);

  const refreshJobs = useCallback(async () => {
    try {
      const nextJobs = await listAnnotationJobs();
      setJobs((current) => mergeMonotonicJobs(current, nextJobs));
    } catch (requestError) {
      setPageError(safeError(requestError, "读取自动标注任务失败"));
    }
  }, []);

  const refreshJobsAndDatasets = useCallback(async () => {
    resetNavigationDatasetSummaryCache();
    const requestedDate = selectedDateRef.current;
    const dateRequestGeneration = ++dateRequestGenerationRef.current;
    try {
      const [
        nextJobs,
        nextCapability,
        nextProfiles,
        summary,
        selectedDetail,
      ] = await Promise.all([
        listAnnotationJobs(),
        getAnnotationCapabilities(),
        getCalibrationProfiles(),
        getNavigationDatasetSummaryCached(),
        requestedDate ? getNavigationDatasetDate(requestedDate) : Promise.resolve(null),
      ]);
      setJobs((current) => mergeMonotonicJobs(current, nextJobs));
      setCapability(nextCapability);
      setProfiles(nextProfiles);
      setDates(summary.dates.filter((date) => date.synced_clip_count > 0));
      if (
        selectedDetail
        && dateRequestGenerationRef.current === dateRequestGeneration
        && selectedDateRef.current === requestedDate
      ) {
        setDateDetail(selectedDetail);
        const available = new Set(
          (selectedDetail.clips ?? [])
            .filter((clip) => clip.has_sync_data || clip.status === "synced")
            .map((clip) => clip.clip),
        );
        setSelectedClips((current) => current.filter((clip) => available.has(clip)));
      }
      setPageError("");
    } catch (requestError) {
      setPageError(safeError(requestError, "刷新自动标注数据失败"));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.allSettled([
      listAnnotationJobs(),
      getAnnotationCapabilities(),
      getCalibrationProfiles(),
      getNavigationDatasetSummaryCached(),
    ]).then(([jobsResult, capabilityResult, profilesResult, datasetResult]) => {
      if (cancelled) return;
      if (jobsResult.status === "fulfilled") {
        setJobs((current) => mergeMonotonicJobs(current, jobsResult.value));
      } else {
        setPageError(safeError(jobsResult.reason, "读取自动标注任务失败"));
      }
      setCapability(capabilityResult.status === "fulfilled"
        ? capabilityResult.value
        : {
            available: false,
            runtime_id: "navigation_odom_v1",
            reason: { code: "capability_unavailable", message: "暂时无法确认 Runtime 状态。" },
          });
      if (profilesResult.status === "fulfilled") setProfiles(profilesResult.value);
      if (datasetResult.status === "fulfilled") {
        setDates(datasetResult.value.dates.filter((date) => date.synced_clip_count > 0));
      }
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!jobs.some((job) => activeJob(job.status))) return;
    const interval = window.setInterval(() => void refreshJobs(), POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [jobs, refreshJobs]);

  const chooseDate = async (date: string) => {
    selectedDateRef.current = date;
    const requestGeneration = ++dateRequestGenerationRef.current;
    setSelectedDate(date);
    setSelectedClips([]);
    setSelectedProfile("");
    setDateDetail(null);
    setCreateError("");
    if (!date) return;
    try {
      const detail = await getNavigationDatasetDate(date);
      if (
        dateRequestGenerationRef.current === requestGeneration
        && selectedDateRef.current === date
      ) {
        setDateDetail(detail);
      }
    } catch (requestError) {
      if (
        dateRequestGenerationRef.current === requestGeneration
        && selectedDateRef.current === date
      ) {
        setCreateError(safeError(requestError, "读取已同步 clip 失败"));
      }
    }
  };

  const availableClips = (dateDetail?.clips ?? []).filter(
    (clip) => clip.has_sync_data || clip.status === "synced",
  );
  const selectedCalibration = profiles.find((profile) => profile.profile_ref === selectedProfile);
  const createDirty = Boolean(selectedDate || selectedClips.length || selectedProfile);
  const canCreate = Boolean(
    capability?.available
      && selectedDate
      && selectedClips.length
      && selectedCalibration
      && !creating,
  );

  const create = async () => {
    if (!selectedCalibration || !canCreate) return;
    setCreating(true);
    setCreateError("");
    try {
      const job = await createAnnotationJob({
        dataset_date: selectedDate,
        source_clips: selectedClips,
        calibration_profile_ref: selectedCalibration.profile_ref,
        calibration_content_sha256: selectedCalibration.content_sha256,
      });
      navigate(`/annotation/jobs/${encodeURIComponent(job.job_ref)}`);
    } catch (requestError) {
      setCreateError(safeError(requestError, "创建自动标注任务失败"));
    } finally {
      setCreating(false);
    }
  };

  const remindCreateDialog = () => {
    const now = Date.now();
    if (now - createDialogLastAttentionAtRef.current < 100) return;
    createDialogLastAttentionAtRef.current = now;
    if (createDialogAttentionTimerRef.current !== null) {
      window.clearTimeout(createDialogAttentionTimerRef.current);
    }
    setCreateDialogAttention((current) => current === 1 ? 2 : 1);
    createDialogAttentionTimerRef.current = window.setTimeout(() => {
      setCreateDialogAttention(0);
      createDialogAttentionTimerRef.current = null;
    }, 1200);
  };

  const openJob = (jobRef: string) => {
    navigate(`/annotation/jobs/${encodeURIComponent(jobRef)}`);
  };
  const waitingJobs = jobs.filter((job) => job.status === "waiting_initial_annotation");
  const runningJobs = jobs.filter((job) => job.status === "preparing" || job.status === "tracking");
  const failedJobs = jobs.filter((job) => job.status === "failed");
  const historyJobs = jobs
    .filter((job) => job.status === "tracked" || job.status === "cancelled")
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  const normalizedHistoryQuery = historyQuery.trim().toLocaleLowerCase();
  const historyStartToken = historyStartDate.replace(/-/g, "");
  const historyEndToken = historyEndDate.replace(/-/g, "");
  const filteredHistoryJobs = historyJobs.filter((job) => {
    const matchesStatus = historyStatus === "all" || job.status === historyStatus;
    const matchesDate = (
      (!historyStartToken || job.dataset_date >= historyStartToken)
      && (!historyEndToken || job.dataset_date <= historyEndToken)
    );
    const searchable = [
      ...job.source_clips,
      job.calibration.label,
    ].join(" ").toLocaleLowerCase();
    return matchesStatus
      && matchesDate
      && (!normalizedHistoryQuery || searchable.includes(normalizedHistoryQuery));
  });
  const historyPageCount = Math.max(1, Math.ceil(filteredHistoryJobs.length / HISTORY_PAGE_SIZE));
  const currentHistoryPage = Math.min(historyPage, historyPageCount);
  const pagedHistoryJobs = filteredHistoryJobs.slice(
    (currentHistoryPage - 1) * HISTORY_PAGE_SIZE,
    currentHistoryPage * HISTORY_PAGE_SIZE,
  );
  const firstVisibleHistoryPage = Math.max(
    1,
    Math.min(currentHistoryPage - 2, historyPageCount - 4),
  );
  const visibleHistoryPages = Array.from(
    { length: Math.min(5, historyPageCount) },
    (_, index) => firstVisibleHistoryPage + index,
  );
  const historyFiltersActive = Boolean(
    normalizedHistoryQuery
    || historyStatus !== "all"
    || historyStartDate
    || historyEndDate,
  );
  const pendingSegments = waitingJobs.reduce(
    (total, job) => total + job.counts.pending_initial_annotation + job.counts.draft,
    0,
  );
  const completedJobs = jobs.filter((job) => job.status === "tracked").length;

  return (
    <section className="mx-auto max-w-[90rem] space-y-3 px-3 pb-28 pt-4 md:px-4 lg:px-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-xl font-semibold tracking-tight text-console-text">自动标注任务</h2>
            {capability && (
              <span
                className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${
                  capability.available
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : "border-amber-200 bg-amber-50 text-amber-700"
                }`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${capability.available ? "bg-emerald-500" : "bg-amber-500"}`} />
                {capability.available ? "处理环境可用" : "处理环境不可用"}
              </span>
            )}
          </div>
          <p className="mt-1.5 text-sm text-console-muted">从已同步数据开始，完成 Web 首帧标注与 Tracking。</p>
        </div>
        <div className="flex gap-2">
          <ConsoleButton onClick={() => void refreshJobsAndDatasets()} aria-label="刷新自动标注任务">
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            刷新
          </ConsoleButton>
          <ConsoleButton
            variant="primary"
            aria-label="新建任务"
            onClick={() => {
              setCreateError("");
              setShowCreate(true);
            }}
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
            创建任务
          </ConsoleButton>
        </div>
      </div>

      {capability && !capability.available && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <div className="flex items-start gap-3">
            <AlertCircle className="mt-0.5 h-5 w-5 text-amber-700" aria-hidden="true" />
            <div>
              <p className="text-sm font-semibold text-amber-800">当前服务器暂不能创建处理任务</p>
              <p className="mt-1 text-sm text-amber-700">
                处理运行环境尚未通过部署预检，请联系运维人员核对本次部署配置。
              </p>
              {capability.reason?.error_ref && /^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$/.test(capability.reason.error_ref) && (
                <p className="mt-1 font-mono text-xs text-amber-800">
                  错误参考：{capability.reason.error_ref}
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {pageError && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          {pageError}
        </div>
      )}

      {loading ? (
        <PageMessage icon={LoaderCircle} title="正在读取自动标注任务…" />
      ) : (
        <div className="space-y-3">
          <div
            className="grid overflow-hidden rounded-lg border border-console-line bg-console-panel sm:grid-cols-2 lg:grid-cols-4"
            data-testid="annotation-job-metrics"
          >
            {[
              { label: "待首帧标注", value: pendingSegments, icon: Tags, color: "text-amber-600" },
              { label: "处理中", value: runningJobs.length, icon: LoaderCircle, color: "text-blue-600" },
              { label: "异常任务", value: failedJobs.length, icon: AlertCircle, color: "text-rose-600" },
              { label: "最近完成", value: completedJobs, icon: CircleCheck, color: "text-emerald-600" },
            ].map((metric, index) => (
              <div
                key={metric.label}
                className="relative flex min-h-24 items-center gap-4 px-5 py-4"
              >
                {index > 0 && (
                  <span
                    className="absolute inset-x-5 top-0 h-px bg-console-line sm:hidden"
                    aria-hidden="true"
                  />
                )}
                {(index === 1 || index === 3) && (
                  <span
                    className="absolute bottom-4 left-0 top-4 hidden w-px bg-console-line sm:block lg:hidden"
                    aria-hidden="true"
                  />
                )}
                {index >= 2 && (
                  <span
                    className="absolute inset-x-5 top-0 hidden h-px bg-console-line sm:block lg:hidden"
                    aria-hidden="true"
                  />
                )}
                {index > 0 && (
                  <span
                    className="absolute bottom-4 left-0 top-4 hidden w-px bg-console-line lg:block"
                    aria-hidden="true"
                  />
                )}
                <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-console-panel2">
                  <metric.icon className={`h-6 w-6 ${metric.color}`} aria-hidden="true" />
                </div>
                <div>
                  <p className="text-sm font-semibold text-console-text">{metric.label}</p>
                  <p className="mt-0.5 text-[1.7rem] font-semibold leading-none tabular-nums text-console-text">{metric.value}</p>
                </div>
              </div>
            ))}
          </div>

          {jobs.length === 0 ? (
            <div className="flex min-h-56 flex-col items-center justify-center rounded-lg border border-dashed border-console-line bg-console-panel px-6 text-center">
              <Tags className="h-8 w-8 text-console-muted" aria-hidden="true" />
              <h3 className="mt-3 text-base font-semibold text-console-text">还没有自动标注任务</h3>
              <p className="mt-1 max-w-md text-sm leading-6 text-console-muted">
                创建任务后，系统会先准备 resize 后首帧，再进入 Web 标注队列。
              </p>
              <ConsoleButton
                className="mt-4"
                variant="primary"
                onClick={() => setShowCreate(true)}
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                创建第一个任务
              </ConsoleButton>
            </div>
          ) : (
            <>
              <div className="space-y-2">
                <JobsSection
                  title="需要我处理"
                  jobs={waitingJobs}
                  empty="当前没有等待人工标注的任务。"
                  actionLabel="继续标注"
                  onOpen={openJob}
                />
                <JobsSection
                  title="系统运行中"
                  jobs={runningJobs}
                  empty="当前没有运行中的任务。"
                  onOpen={openJob}
                />
              </div>

              {failedJobs.length > 0 && (
                <JobsSection
                  title="异常任务"
                  jobs={failedJobs}
                  empty=""
                  actionLabel="查看处理"
                  onOpen={openJob}
                />
              )}

              <section className="overflow-hidden rounded-lg border border-console-line bg-console-panel">
                <div className="px-4 pt-3">
                  <span className="flex items-center gap-3">
                    <History className="h-5 w-5 text-console-muted" aria-hidden="true" />
                    <span>
                      <span className="block text-sm font-semibold text-console-text">历史任务</span>
                      <span className="mt-0.5 block text-xs text-console-muted">
                        已完成、已取消及无可处理目标的任务，共 {historyJobs.length} 条
                      </span>
                    </span>
                  </span>
                </div>

                {historyJobs.length === 0 ? (
                  <div className="mx-4 mb-3 mt-3 border-y border-console-line px-4 py-8 text-center text-sm text-console-muted">
                    暂无历史任务
                  </div>
                ) : (
                  <>
                    <div className="flex flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center lg:gap-4">
                        <label className="relative w-full lg:w-64">
                          <span className="sr-only">搜索历史任务</span>
                          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted" aria-hidden="true" />
                          <input
                            aria-label="搜索历史任务"
                            value={historyQuery}
                            onChange={(event) => {
                              setHistoryQuery(event.target.value);
                              setHistoryPage(1);
                            }}
                            placeholder="搜索 clip 或标定参数"
                            className="h-10 w-full rounded-lg border border-console-line bg-white pl-10 pr-3 text-sm text-console-text outline-none transition placeholder:text-console-muted focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15"
                          />
                        </label>
                        <label className="flex min-w-0 items-center gap-2">
                          <span className="shrink-0 text-sm font-medium text-console-muted">状态</span>
                          <select
                            aria-label="筛选历史任务状态"
                            value={historyStatus}
                            onChange={(event) => {
                              setHistoryStatus(event.target.value as "all" | AnnotationJobStatus);
                              setHistoryPage(1);
                            }}
                            className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text outline-none transition focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15 lg:w-36"
                          >
                            <option value="all">全部</option>
                            <option value="tracked">Tracking 已完成</option>
                            <option value="cancelled">已取消</option>
                          </select>
                        </label>
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="shrink-0 text-sm font-medium text-console-muted">数据日期</span>
                          <div
                            className="flex h-10 min-w-0 items-center gap-2 rounded-lg border border-console-line bg-white px-3 text-sm"
                            role="group"
                            aria-label="按数据日期筛选历史任务"
                          >
                            <CalendarDays className="h-4 w-4 shrink-0 text-console-muted" aria-hidden="true" />
                            <input
                              type="date"
                              aria-label="历史任务开始日期"
                              value={historyStartDate}
                              onChange={(event) => {
                                setHistoryStartDate(event.target.value);
                                setHistoryPage(1);
                              }}
                              className="min-w-0 flex-1 bg-transparent text-xs text-console-text outline-none"
                            />
                            <span className="text-console-muted" aria-hidden="true">→</span>
                            <input
                              type="date"
                              aria-label="历史任务结束日期"
                              value={historyEndDate}
                              onChange={(event) => {
                                setHistoryEndDate(event.target.value);
                                setHistoryPage(1);
                              }}
                              className="min-w-0 flex-1 bg-transparent text-xs text-console-text outline-none"
                            />
                          </div>
                        </div>
                        <button
                          type="button"
                          disabled={!historyFiltersActive}
                          className="h-10 shrink-0 rounded-lg border border-console-line bg-white px-3.5 text-sm font-medium text-console-text transition hover:bg-console-panel2 focus:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/20 disabled:cursor-default disabled:opacity-45 disabled:hover:bg-white"
                          onClick={() => {
                            setHistoryQuery("");
                            setHistoryStatus("all");
                            setHistoryStartDate("");
                            setHistoryEndDate("");
                            setHistoryPage(1);
                          }}
                        >
                          清空筛选
                        </button>
                      </div>
                    <div className="mx-4 mb-3 overflow-hidden border-y border-console-line">
                      <div className="overflow-x-auto">
                        <div className="min-w-[47rem]">
                          <div
                            className={`grid gap-2 border-b border-console-line bg-slate-100/80 px-3 py-1.5 text-[11px] font-medium text-console-muted ${JOB_TABLE_GRID_CLASS}`}
                            data-testid="annotation-history-table-header"
                          >
                            <span>数据日期</span>
                            <span>外层 clips</span>
                            <span>状态</span>
                            <span>Segment 进度</span>
                            <span>处理标定</span>
                            <span>更新时间</span>
                            <span>操作</span>
                          </div>
                          {pagedHistoryJobs.length > 0 ? (
                            pagedHistoryJobs.map((job) => (
                              <JobRow key={job.job_ref} job={job} onOpen={() => openJob(job.job_ref)} />
                            ))
                          ) : (
                            <p className="px-4 py-8 text-center text-sm text-console-muted">
                              没有符合当前筛选条件的历史任务。
                            </p>
                          )}
                        </div>
                      </div>
                      {filteredHistoryJobs.length > 0 && (
                        <div className="flex min-h-12 items-center justify-end gap-2 border-t border-console-line px-3 py-2">
                          <span className="mr-1 text-xs tabular-nums text-console-muted">
                            共 {filteredHistoryJobs.length} 条
                          </span>
                          {historyPageCount > 1 && (
                            <>
                              <button
                                type="button"
                                aria-label="上一页历史任务"
                                disabled={currentHistoryPage === 1}
                                className="flex h-8 w-8 items-center justify-center rounded-md border border-console-line text-console-muted transition hover:bg-console-panel2 hover:text-console-text disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-transparent"
                                onClick={() => setHistoryPage(Math.max(1, currentHistoryPage - 1))}
                              >
                                <ChevronLeft className="h-4 w-4" aria-hidden="true" />
                              </button>
                              {visibleHistoryPages.map((page) => (
                                <button
                                  key={page}
                                  type="button"
                                  aria-label={`前往历史任务第 ${page} 页`}
                                  aria-current={page === currentHistoryPage ? "page" : undefined}
                                  className={`flex h-8 min-w-8 items-center justify-center rounded-md border px-2 text-sm font-medium transition ${
                                    page === currentHistoryPage
                                      ? "border-console-cyan bg-console-cyan text-white"
                                      : "border-console-line text-console-text hover:bg-console-panel2"
                                  }`}
                                  onClick={() => setHistoryPage(page)}
                                >
                                  {page}
                                </button>
                              ))}
                              <button
                                type="button"
                                aria-label="下一页历史任务"
                                disabled={currentHistoryPage === historyPageCount}
                                className="flex h-8 w-8 items-center justify-center rounded-md border border-console-line text-console-muted transition hover:bg-console-panel2 hover:text-console-text disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-transparent"
                                onClick={() => setHistoryPage(Math.min(historyPageCount, currentHistoryPage + 1))}
                              >
                                <ChevronRight className="h-4 w-4" aria-hidden="true" />
                              </button>
                            </>
                          )}
                          <span className="ml-1 rounded-md border border-console-line bg-white px-2.5 py-1.5 text-xs text-console-muted">
                            {HISTORY_PAGE_SIZE} 条/页
                          </span>
                        </div>
                      )}
                    </div>
                  </>
                )}
              </section>
            </>
          )}
        </div>
      )}

      <Dialog.Root
        open={showCreate}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !creating) setShowCreate(false);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay
            className="fixed inset-0 z-[90] bg-slate-950/35 backdrop-blur-[1px]"
            data-testid="create-annotation-job-overlay"
            onMouseDown={remindCreateDialog}
          />
          <Dialog.Content
            ref={createDialogRef}
            aria-describedby="create-annotation-job-description"
            className={`fixed left-1/2 top-1/2 z-[91] flex max-h-[calc(100vh-2rem)] w-[calc(100vw-2rem)] max-w-2xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-xl border border-console-line bg-console-panel shadow-2xl outline outline-2 outline-offset-2 outline-transparent focus:outline-none ${
              createDialogAttention === 1
                ? "animate-[navigation-dialog-attention-a_760ms_ease-in-out] motion-reduce:animate-none motion-reduce:outline-slate-400"
                : createDialogAttention === 2
                  ? "animate-[navigation-dialog-attention-b_760ms_ease-in-out] motion-reduce:animate-none motion-reduce:outline-slate-400"
                  : ""
            }`}
            data-testid="create-annotation-job-dialog"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              createDialogRef.current?.focus({ preventScroll: true });
            }}
            onEscapeKeyDown={(event) => {
              if (createDirty || creating) event.preventDefault();
            }}
            onInteractOutside={(event) => {
              event.preventDefault();
              remindCreateDialog();
            }}
          >
            <div className="flex items-start justify-between gap-4 border-b border-console-line px-5 py-4">
              <div>
                <Dialog.Title className="text-base font-semibold text-console-text">创建导航自动标注任务</Dialog.Title>
                <Dialog.Description id="create-annotation-job-description" className="mt-1 text-sm text-console-muted">
                  选择已同步数据和当天处理使用的标定参数。
                </Dialog.Description>
              </div>
              <button
                type="button"
                aria-label="关闭创建任务"
                disabled={creating}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus-visible:bg-console-panel2 focus-visible:text-console-text disabled:cursor-not-allowed disabled:opacity-40"
                onClick={() => setShowCreate(false)}
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>

            <div className="console-soft-scrollbar min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="text-sm">
                  <span className="mb-2 block font-semibold text-console-text">数据日期</span>
                  <select
                    aria-label="自动标注数据日期"
                    value={selectedDate}
                    disabled={creating}
                    className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text outline-none transition focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15 disabled:cursor-not-allowed disabled:opacity-60"
                    onChange={(event) => void chooseDate(event.target.value)}
                  >
                    <option value="">请选择已同步日期</option>
                    {dates.map((date) => <option key={date.date} value={date.date}>{date.date}</option>)}
                  </select>
                </label>
                <label className="text-sm">
                  <span className="mb-2 block font-semibold text-console-text">当天处理标定</span>
                  <select
                    aria-label="当天处理标定"
                    value={selectedProfile}
                    disabled={creating}
                    className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text outline-none transition focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15 disabled:cursor-not-allowed disabled:opacity-60"
                    onChange={(event) => {
                      setSelectedProfile(event.target.value);
                      setCreateError("");
                    }}
                  >
                    <option value="">请选择标定参数</option>
                    {profiles.map((profile) => (
                      <option key={profile.profile_ref} value={profile.profile_ref}>{profile.label}</option>
                    ))}
                  </select>
                </label>
              </div>

              <fieldset disabled={creating}>
                <legend className="sr-only">外层 clips</legend>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <span className="text-sm font-semibold text-console-text">外层 clips</span>
                  {availableClips.length > 0 && (
                    <button
                      type="button"
                      className="text-xs font-medium text-console-cyan hover:text-blue-700 focus:outline-none focus:underline"
                      onClick={() => setSelectedClips(
                        selectedClips.length === availableClips.length
                          ? []
                          : availableClips.map((clip) => clip.clip),
                      )}
                    >
                      {selectedClips.length === availableClips.length ? "取消全选" : "全选"}
                    </button>
                  )}
                </div>
                <div className="overflow-hidden rounded-lg border border-console-line">
                  {!selectedDate ? (
                    <p className="px-4 py-5 text-sm text-console-muted">请先选择数据日期。</p>
                  ) : dateDetail === null ? (
                    <p className="flex items-center gap-2 px-4 py-5 text-sm text-console-muted">
                      <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                      正在读取已同步 clips…
                    </p>
                  ) : availableClips.length === 0 ? (
                    <p className="px-4 py-5 text-sm text-console-muted">该日期没有可处理的已同步 clip。</p>
                  ) : (
                    <div className="console-soft-scrollbar max-h-52 divide-y divide-console-line overflow-y-auto">
                      {availableClips.map((clip) => {
                        const checked = selectedClips.includes(clip.clip);
                        return (
                          <label
                            key={clip.clip}
                            className="flex cursor-pointer items-center gap-3 px-4 py-3 text-sm text-console-text transition hover:bg-console-panel2/70"
                          >
                            <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                              checked ? "border-console-cyan bg-console-cyan text-white" : "border-console-line bg-white"
                            }`}>
                              {checked && <Check className="h-3 w-3" aria-hidden="true" />}
                            </span>
                            <input
                              className="sr-only"
                              type="checkbox"
                              aria-label={clip.clip}
                              checked={checked}
                              onChange={(event) => setSelectedClips((current) => (
                                event.target.checked
                                  ? [...current, clip.clip]
                                  : current.filter((item) => item !== clip.clip)
                              ))}
                            />
                            <span className="min-w-0 flex-1 truncate" title={clip.clip}>{clip.clip}</span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              </fieldset>

              <div className="rounded-lg border border-console-line bg-console-panel2/60 px-4 py-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-console-muted">任务摘要</p>
                <p className="mt-1.5 text-sm text-console-text">
                  {selectedDate || "未选择日期"} · {selectedClips.length} 个 clip · {selectedCalibration?.label ?? "未选择标定"}
                </p>
                <p className="mt-1 text-xs text-console-muted">页面不提供全局推荐，请按当天数据显式选择处理标定。</p>
              </div>

              {capability && !capability.available && (
                <div role="status" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                  当前处理环境不可用，暂时不能创建任务。
                </div>
              )}

              {createError && (
                <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                  {createError}
                </div>
              )}
            </div>

            <div className="flex shrink-0 justify-end gap-2 border-t border-console-line bg-console-panel px-5 py-4">
              <ConsoleButton disabled={creating} onClick={() => setShowCreate(false)}>取消</ConsoleButton>
              <ConsoleButton variant="primary" disabled={!canCreate} onClick={() => void create()}>
                {creating ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Plus className="h-4 w-4" aria-hidden="true" />}
                {creating ? "正在创建…" : "创建并准备"}
              </ConsoleButton>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}

function SegmentQueue({
  job,
  currentSegmentRef,
  onNavigate,
}: {
  job: AnnotationJobDetail;
  currentSegmentRef?: string;
  onNavigate: (segmentRef: string) => void | Promise<void>;
}) {
  const grouped = useMemo(() => {
    const result = new Map<string, typeof job.segments>();
    job.segments.forEach((segment) => {
      const items = result.get(segment.source_clip) ?? [];
      items.push(segment);
      result.set(segment.source_clip, items);
    });
    return [...result.entries()];
  }, [job.segments]);

  if (job.segments.length === 0) {
    return <p className="p-4 text-sm text-console-muted">准备完成后显示内部 segment 队列。</p>;
  }

  return (
    <div className="console-soft-scrollbar max-h-[calc(100vh-15rem)] space-y-4 overflow-y-auto p-2 xl:max-h-none xl:flex-1">
      {grouped.map(([sourceClip, segments]) => (
        <div key={sourceClip}>
          <p className="mb-1 truncate px-2 pt-1 text-[11px] font-semibold text-console-muted" title={sourceClip}>{sourceClip}</p>
          <div className="space-y-0.5">
            {segments.map((segment) => {
              const status = SEGMENT_STATUS[segment.status];
              return (
                <button
                  key={segment.segment_ref}
                  type="button"
                  className={`w-full rounded-md border border-transparent px-2.5 py-2.5 text-left transition focus:outline-none focus:ring-2 focus:ring-console-cyan ${
                    currentSegmentRef === segment.segment_ref
                      ? "bg-blue-50 text-console-text shadow-[inset_3px_0_0_#2d6cdf]"
                      : "hover:bg-console-panel2"
                  }`}
                  onClick={() => void onNavigate(segment.segment_ref)}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-console-text">
                      Segment {String(segment.ordinal).padStart(2, "0")}
                    </span>
                    <StatusTag tone={status.tone}>{status.label}</StatusTag>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function JobActions({
  job,
  onUpdated,
}: {
  job: AnnotationJobDetail;
  onUpdated: (job: AnnotationJobDetail) => void;
}) {
  const [acting, setActing] = useState(false);
  const [error, setError] = useState("");
  const recoveryQuarantined = (
    job.status === "failed"
    && job.failure?.code === "recovery_required"
  );

  const mutate = async (
    action: "tracking" | "complete-no-processable-targets" | "cancel" | "retry",
  ) => {
    setActing(true);
    setError("");
    try {
      onUpdated(await mutateAnnotationJob(job.job_ref, action, job.state_revision));
    } catch (requestError) {
      setError(safeError(requestError, "任务操作失败"));
    } finally {
      setActing(false);
    }
  };

  return (
    <div>
      {job.cancel_requested ? (
        <div role="status" className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <p className="font-medium">正在取消任务</p>
          <p className="mt-1">
            取消请求已保存，系统正在等待 Runtime 进程确认结束。在此之前不会释放该任务的数据范围。
          </p>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
        {job.ready_for_tracking && (
          <ConsoleButton variant="primary" disabled={acting} onClick={() => void mutate("tracking")}>
            <Play className="h-4 w-4" aria-hidden="true" />
            开始 Tracking
          </ConsoleButton>
        )}
        {job.ready_for_no_processable_targets && (
          <ConsoleButton disabled={acting} onClick={() => void mutate("complete-no-processable-targets")}>
            <Ban className="h-4 w-4" aria-hidden="true" />
            无可处理目标
          </ConsoleButton>
        )}
        {(activeJob(job.status) || (
          job.status === "failed" && !recoveryQuarantined
        )) && (
          <ConsoleButton disabled={acting} onClick={() => void mutate("cancel")}>
            <X className="h-4 w-4" aria-hidden="true" />
            {job.status === "failed" ? "放弃任务" : "取消任务"}
          </ConsoleButton>
        )}
        {job.status === "failed" && job.failure?.retryable && !recoveryQuarantined && (
          <ConsoleButton disabled={acting} onClick={() => void mutate("retry")}>
            <RotateCcw className="h-4 w-4" aria-hidden="true" />
            重试失败阶段
          </ConsoleButton>
        )}
        </div>
      )}
      {recoveryQuarantined && (
        <div role="alert" className="mt-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <p className="font-medium">任务处于恢复隔离状态</p>
          <p className="mt-1">
            请由运维先确认旧进程已结束，再选择恢复任务或安全放弃。当前页面不会释放该任务的数据范围。
          </p>
          {job.failure?.error_ref && (
            <p className="mt-1 font-mono text-xs">错误参考：{job.failure.error_ref}</p>
          )}
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-rose-700">{error}</p>}
    </div>
  );
}

function JobPage({ jobRef }: { jobRef: string }) {
  const navigate = useNavigate();
  const [job, setJob] = useState<AnnotationJobDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const updateJob = useCallback((nextJob: AnnotationJobDetail) => {
    setJob((current) => preferMonotonicJob(current, nextJob));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const nextJob = await getAnnotationJob(jobRef);
      updateJob(nextJob);
      setError("");
    } catch (requestError) {
      setError(safeError(requestError, "读取任务详情失败"));
    } finally {
      setLoading(false);
    }
  }, [jobRef, updateJob]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!job || !activeJob(job.status)) return;
    const interval = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [job, refresh]);

  if (loading) {
    return <section className="mx-auto max-w-7xl px-4 py-6 md:px-6"><PageMessage icon={LoaderCircle} title="正在读取任务…" /></section>;
  }
  if (!job) {
    return (
      <section className="mx-auto max-w-7xl px-4 py-6 md:px-6">
        <PageMessage
          title="无法打开自动标注任务"
          detail={error}
          action={<ConsoleButton onClick={() => navigate("/annotation/jobs")}>返回任务列表</ConsoleButton>}
        />
      </section>
    );
  }

  const status = JOB_STATUS[job.status];
  const noProcessable = job.completion_outcome === "no_processable_targets";

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-start gap-3">
          <ConsoleButton aria-label="返回自动标注任务列表" onClick={() => navigate("/annotation/jobs")}>
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </ConsoleButton>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold text-console-text">{job.dataset_date}</h2>
              <StatusTag tone={job.cancel_requested ? "warning" : status.tone}>
                {job.cancel_requested ? "正在取消" : noProcessable ? "无可处理目标" : status.label}
              </StatusTag>
            </div>
            <p className="mt-1 text-sm text-console-muted">{job.source_clips.join("、")}</p>
          </div>
        </div>
        <JobActions job={job} onUpdated={updateJob} />
      </div>

      {job.failure && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-4">
          <p className="text-sm font-semibold text-rose-800">处理失败（{job.failure.code}）</p>
          <p className="mt-1 text-sm text-rose-700">
            {safeError(new Error(job.failure.message), "处理失败，请根据审计引用联系管理员。")}
          </p>
          {job.failure.error_ref && (
            <p className="mt-2 font-mono text-xs text-rose-600">审计引用：{job.failure.error_ref}</p>
          )}
        </div>
      )}
      {error && <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}

      <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
        <ConsoleCard className="p-0">
          <div className="border-b border-console-line p-4">
            <h3 className="text-sm font-semibold text-console-text">Segment 队列</h3>
            <p className="mt-1 text-xs text-console-muted">{countsSummary(job)}</p>
          </div>
          <SegmentQueue
            job={job}
            onNavigate={(segmentRef) => navigate(`/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(segmentRef)}`)}
          />
        </ConsoleCard>

        <div className="space-y-4">
          <ConsoleCard>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <div>
                <p className="text-xs text-console-muted">处理标定</p>
                <p className="mt-2 text-sm font-medium text-console-text">{job.calibration.label}</p>
              </div>
              <div>
                <p className="text-xs text-console-muted">待标注</p>
                <p className="mt-2 text-xl font-semibold text-console-text">{job.counts.pending_initial_annotation + job.counts.draft}</p>
              </div>
              <div>
                <p className="text-xs text-console-muted">已提交 / 已跳过</p>
                <p className="mt-2 text-xl font-semibold text-console-text">{job.counts.submitted} / {job.counts.skipped}</p>
              </div>
              <div>
                <p className="text-xs text-console-muted">Tracking 完成</p>
                <p className="mt-2 text-xl font-semibold text-console-text">{job.counts.tracked}</p>
              </div>
            </div>
          </ConsoleCard>

          {job.cancel_requested ? (
            <PageMessage
              icon={LoaderCircle}
              title="正在安全终止处理任务"
              detail="系统正在等待 Runtime 进程确认退出；确认前会保留任务状态和数据范围。"
            />
          ) : job.status === "preparing" ? (
            <PageMessage
              icon={LoaderCircle}
              title="正在准备 Web 首帧标注"
              detail="系统正在隔离的 staging 中生成 resize 后首帧。此阶段不需要打开 XQuartz。"
            />
          ) : job.status === "waiting_initial_annotation" ? (
            <PageMessage
              icon={CircleDot}
              title="请选择一个 Segment 开始标注"
              detail="全部 Segment 已提交或显式跳过后，回到此页面启动 Tracking。"
            />
          ) : job.status === "tracking" ? (
            <PageMessage
              icon={LoaderCircle}
              title="Tracking 正在串行执行"
              detail="页面可以安全刷新；已完成的 target 会保存 checkpoint。"
            />
          ) : job.status === "tracked" ? (
            <PageMessage icon={Check} title="Tracking 已完成" detail="M1 产物保留在任务 staging，等待 M2 继续后处理。" />
          ) : null}
        </div>
      </div>
    </section>
  );
}

function SegmentPage({ jobRef, segmentRef }: { jobRef: string; segmentRef: string }) {
  const navigate = useNavigate();
  const [job, setJob] = useState<AnnotationJobDetail | null>(null);
  const [segment, setSegment] = useState<AnnotationSegmentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [externalSubmissionNotice, setExternalSubmissionNotice] = useState("");
  const [showSkip, setShowSkip] = useState(false);
  const [skipReason, setSkipReason] = useState<"no_valid_target" | "unusable_first_frame" | "other">("no_valid_target");
  const [skipNote, setSkipNote] = useState("");
  const [acting, setActing] = useState(false);
  const flushRef = useRef<() => Promise<boolean>>(async () => true);
  const currentSegmentRef = useRef<AnnotationSegmentDetail | null>(null);
  const jobAllowsInitialAnnotation = (
    job?.status === "waiting_initial_annotation"
    && !job.cancel_requested
  );
  const editable = jobAllowsInitialAnnotation && (
    segment?.status === "pending_initial_annotation" || segment?.status === "draft"
  );
  const flushForNavigation = useCallback(() => flushRef.current(), []);
  const updateJob = useCallback((nextJob: AnnotationJobDetail) => {
    setJob((current) => preferMonotonicJob(current, nextJob));
  }, []);
  const updateSegment = useCallback((nextSegment: AnnotationSegmentDetail) => {
    const current = currentSegmentRef.current;
    if (
      current
      && current.segment_ref === nextSegment.segment_ref
      && nextSegment.state_revision < current.state_revision
    ) {
      return;
    }
    currentSegmentRef.current = nextSegment;
    setSegment(nextSegment);
  }, []);

  const refreshJob = useCallback(async () => {
    const nextJob = await getAnnotationJob(jobRef);
    updateJob(nextJob);
  }, [jobRef, updateJob]);

  useEffect(() => {
    setExternalSubmissionNotice("");
  }, [jobRef, segmentRef]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void Promise.all([
      getAnnotationJob(jobRef),
      getAnnotationSegment(jobRef, segmentRef),
    ]).then(([nextJob, nextSegment]) => {
      if (cancelled) return;
      updateJob(nextJob);
      updateSegment(nextSegment);
      setError("");
    }).catch((requestError) => {
      if (!cancelled) {
        setError(safeError(requestError, "读取首帧标注工作台失败"));
      }
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [jobRef, segmentRef, updateJob, updateSegment]);

  useEffect(() => {
    if (!job || !activeJob(job.status)) return;
    let cancelled = false;
    let requestGeneration = 0;
    const interval = window.setInterval(() => {
      const currentRequest = ++requestGeneration;
      void getAnnotationJob(jobRef).then(async (nextJob) => {
        let nextSegment: AnnotationSegmentDetail | null = null;
        if (nextJob.status !== "waiting_initial_annotation") {
          nextSegment = await getAnnotationSegment(jobRef, segmentRef);
        }
        if (cancelled || currentRequest !== requestGeneration) return;
        updateJob(nextJob);
        if (nextSegment) {
          updateSegment(nextSegment);
        }
      }).catch((requestError) => {
        if (!cancelled && currentRequest === requestGeneration) {
          setError(safeError(requestError, "刷新任务状态失败"));
        }
      });
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [job?.status, jobRef, segmentRef, updateJob, updateSegment]);

  const navigateSafely = async (path: string) => {
    const saved = await flushRef.current();
    if (saved) navigate(path);
  };

  const reopenOrUnskip = async (action: "reopen" | "unskip") => {
    if (!segment) return;
    setActing(true);
    setActionError("");
    try {
      const updated = await mutateAnnotationSegment(
        jobRef,
        segmentRef,
        action,
        segment.state_revision,
      );
      setExternalSubmissionNotice("");
      updateSegment(updated);
      await refreshJob();
    } catch (requestError) {
      setActionError(safeError(requestError, action === "reopen" ? "重新编辑失败" : "恢复 Segment 失败"));
    } finally {
      setActing(false);
    }
  };

  const skip = async () => {
    if (!segment || (skipReason === "other" && !skipNote.trim())) return;
    setActing(true);
    setActionError("");
    try {
      const saved = await flushRef.current();
      if (!saved) return;
      const latestSegment = currentSegmentRef.current;
      if (!latestSegment) return;
      const updated = await skipAnnotationSegment(
        jobRef,
        segmentRef,
        latestSegment.state_revision,
        skipReason,
        skipNote,
      );
      updateSegment(updated);
      setShowSkip(false);
      await refreshJob();
    } catch (requestError) {
      setActionError(safeError(requestError, "跳过 Segment 失败"));
    } finally {
      setActing(false);
    }
  };

  if (loading || !job || !segment) {
    return (
      <section className="mx-auto max-w-[96rem] px-4 py-6 md:px-6">
        {loading
          ? <PageMessage icon={LoaderCircle} title="正在恢复首帧标注工作台…" />
          : <PageMessage title="无法打开 Segment" detail={error} />}
      </section>
    );
  }

  const status = SEGMENT_STATUS[segment.status];

  return (
    <section className="mx-auto max-w-[110rem] space-y-3 px-3 py-3 md:px-4 xl:flex xl:h-[calc(100dvh-7.5rem)] xl:min-h-[44rem] xl:flex-col xl:overflow-hidden">
      <DataRouterFlushBlocker
        enabled={Boolean(editable)}
        flush={flushForNavigation}
      />
      <div className="flex shrink-0 flex-col gap-3 border border-console-line bg-console-panel px-3 py-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <ConsoleButton
            className="h-9 w-9 shrink-0 px-0"
            variant="ghost"
            aria-label="返回自动标注任务"
            onClick={() => void navigateSafely(`/annotation/jobs/${encodeURIComponent(jobRef)}`)}
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </ConsoleButton>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold text-console-text">首帧标注</h2>
              <span className="text-xs text-console-muted">
                Segment {String(segment.ordinal).padStart(2, "0")} / {job.counts.total}
              </span>
              <StatusTag tone={status.tone}>{status.label}</StatusTag>
            </div>
            <p className="mt-0.5 truncate text-xs text-console-muted" title={`${job.dataset_date} · ${segment.source_clip}`}>
              {job.dataset_date} · {segment.source_clip}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {editable && (
            <ConsoleButton disabled={acting} onClick={() => setShowSkip(true)}>
              <SkipForward className="h-4 w-4" aria-hidden="true" />
              跳过此 Segment
            </ConsoleButton>
          )}
          {jobAllowsInitialAnnotation && segment.status === "submitted" && (
            <ConsoleButton disabled={acting} onClick={() => void reopenOrUnskip("reopen")}>
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              重新编辑
            </ConsoleButton>
          )}
          {jobAllowsInitialAnnotation && segment.status === "skipped" && (
            <ConsoleButton disabled={acting} onClick={() => void reopenOrUnskip("unskip")}>
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              恢复标注
            </ConsoleButton>
          )}
        </div>
      </div>

      {(error || actionError) && (
        <div role="alert" className="shrink-0 border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {actionError || error}
        </div>
      )}

      {externalSubmissionNotice && (
        <div
          role="alert"
          className="shrink-0 border border-blue-200 bg-blue-50 px-3 py-2 text-sm font-medium leading-6 text-blue-800"
        >
          {externalSubmissionNotice}
        </div>
      )}

      <label className="block shrink-0 text-sm text-console-muted xl:hidden">
        <span className="mb-2 block font-medium text-console-text">切换 Segment</span>
        <select
          aria-label="切换 Segment"
          value={segment.segment_ref}
          className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text"
          onChange={(event) => void navigateSafely(
            `/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(event.target.value)}`,
          )}
        >
          {job.segments.map((item) => (
            <option key={item.segment_ref} value={item.segment_ref}>
              Segment {String(item.ordinal).padStart(2, "0")} · {SEGMENT_STATUS[item.status].label}
            </option>
          ))}
        </select>
      </label>

      <div
        data-testid="annotation-studio-shell"
        className="min-h-0 flex-1 overflow-hidden border border-console-line bg-console-panel xl:grid xl:grid-cols-[15rem_minmax(0,1fr)]"
      >
        <aside
          aria-label="Segment 队列"
          className="hidden min-h-0 flex-col border-r border-console-line bg-console-panel xl:flex"
        >
          <div className="shrink-0 border-b border-console-line px-3 py-3">
            <h3 className="text-sm font-semibold text-console-text">Segment 队列</h3>
            <p className="mt-0.5 text-[11px] text-console-muted">{countsSummary(job)}</p>
          </div>
          <SegmentQueue
            job={job}
            currentSegmentRef={segment.segment_ref}
            onNavigate={(nextSegmentRef) => navigateSafely(
              `/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(nextSegmentRef)}`,
            )}
          />
        </aside>

        <div className="min-h-0 min-w-0 overflow-hidden">
          {segment.status === "skipped" ? (
            <div className="h-full p-4">
              <PageMessage
                icon={SkipForward}
                title="此 Segment 已显式跳过"
                detail={segment.skip_reason?.note || "它不会进入 Tracking；Tracking 开始前仍可恢复标注。"}
              />
            </div>
          ) : (
            <InitialAnnotationWorkbench
              key={`${segment.segment_ref}:${
                segment.status === "pending_initial_annotation" || segment.status === "draft"
                  ? "editable"
                  : segment.status
              }`}
              job={job}
              segment={segment}
              onSegmentUpdated={(updated) => {
                updateSegment(updated);
                setJob((current) => {
                  if (!current) return current;
                  const segments = current.segments.map((item) => (
                    item.segment_ref === updated.segment_ref ? updated : item
                  ));
                  return { ...current, segments, counts: countsFromSegments(segments) };
                });
              }}
              onJobRefresh={refreshJob}
              onExternalSubmissionResolved={setExternalSubmissionNotice}
              registerFlush={(flush) => {
                flushRef.current = flush;
              }}
            />
          )}
        </div>
      </div>

      <Dialog.Root
        open={showSkip}
        onOpenChange={(open) => {
          if (!acting) setShowSkip(open);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-[90] bg-slate-950/35 backdrop-blur-[1px]" />
          <Dialog.Content
            aria-describedby="skip-segment-description"
            className="fixed left-1/2 top-1/2 z-[91] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-xl border border-console-line bg-console-panel shadow-2xl focus:outline-none"
          >
            <div className="flex items-start justify-between gap-4 border-b border-console-line px-5 py-4">
              <div>
                <Dialog.Title className="text-base font-semibold text-console-text">跳过此 Segment</Dialog.Title>
                <Dialog.Description id="skip-segment-description" className="mt-1 text-sm text-console-muted">
                  跳过后不会进入 Tracking，开始 Tracking 前仍可恢复。
                </Dialog.Description>
              </div>
              <button
                type="button"
                aria-label="关闭跳过 Segment"
                disabled={acting}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan disabled:opacity-40"
                onClick={() => setShowSkip(false)}
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="space-y-4 px-5 py-5">
              <label className="block text-sm text-console-muted">
                <span className="mb-2 block font-medium text-console-text">跳过原因</span>
                <select
                  aria-label="跳过原因"
                  value={skipReason}
                  disabled={acting}
                  className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text"
                  onChange={(event) => setSkipReason(event.target.value as typeof skipReason)}
                >
                  <option value="no_valid_target">首帧无有效目标</option>
                  <option value="unusable_first_frame">首帧不可用</option>
                  <option value="other">其他原因</option>
                </select>
              </label>
              <label className="block text-sm text-console-muted">
                <span className="mb-2 block font-medium text-console-text">说明</span>
                <input
                  aria-label="跳过说明"
                  value={skipNote}
                  disabled={acting}
                  placeholder={skipReason === "other" ? "其他原因必须填写说明" : "可选"}
                  className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text"
                  onChange={(event) => setSkipNote(event.target.value)}
                />
              </label>
            </div>
            <div className="flex justify-end gap-2 border-t border-console-line px-5 py-4">
              <ConsoleButton disabled={acting} onClick={() => setShowSkip(false)}>取消</ConsoleButton>
              <ConsoleButton
                variant="primary"
                disabled={acting || (skipReason === "other" && !skipNote.trim())}
                onClick={() => void skip()}
              >
                {acting && <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />}
                确认跳过
              </ConsoleButton>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}

export function AnnotationPage() {
  const { jobRef, segmentRef } = useParams<{ jobRef?: string; segmentRef?: string }>();
  if (jobRef && segmentRef) {
    return <SegmentPage key={`${jobRef}:${segmentRef}`} jobRef={jobRef} segmentRef={segmentRef} />;
  }
  if (jobRef) return <JobPage jobRef={jobRef} />;
  return <JobsPage />;
}
