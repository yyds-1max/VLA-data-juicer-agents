import {
  AlertCircle,
  ArrowLeft,
  Ban,
  Check,
  ChevronRight,
  CircleDot,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
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
  return Number.isNaN(date.valueOf()) ? timestamp : date.toLocaleString("zh-CN", { hour12: false });
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

function countsSummary(job: AnnotationJobSummary): string {
  const resolved = job.counts.submitted + job.counts.skipped + job.counts.tracked;
  return `${resolved}/${job.counts.total} 个 segment 已处理`;
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
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const selectedDateRef = useRef("");
  const dateRequestGenerationRef = useRef(0);

  const refreshJobs = useCallback(async () => {
    try {
      const nextJobs = await listAnnotationJobs();
      setJobs((current) => mergeMonotonicJobs(current, nextJobs));
    } catch (requestError) {
      setError(safeError(requestError, "读取自动标注任务失败"));
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
      setError("");
    } catch (requestError) {
      setError(safeError(requestError, "刷新自动标注数据失败"));
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
        setError(safeError(jobsResult.reason, "读取自动标注任务失败"));
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
    setError("");
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
        setError(safeError(requestError, "读取已同步 clip 失败"));
      }
    }
  };

  const availableClips = (dateDetail?.clips ?? []).filter(
    (clip) => clip.has_sync_data || clip.status === "synced",
  );
  const selectedCalibration = profiles.find((profile) => profile.profile_ref === selectedProfile);
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
    setError("");
    try {
      const job = await createAnnotationJob({
        dataset_date: selectedDate,
        source_clips: selectedClips,
        calibration_profile_ref: selectedCalibration.profile_ref,
        calibration_content_sha256: selectedCalibration.content_sha256,
      });
      navigate(`/annotation/jobs/${encodeURIComponent(job.job_ref)}`);
    } catch (requestError) {
      setError(safeError(requestError, "创建自动标注任务失败"));
    } finally {
      setCreating(false);
    }
  };

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-console-text">自动标注任务</h2>
          <p className="mt-1 text-sm text-console-muted">从已同步数据开始，完成 Web 首帧标注与 Tracking。</p>
        </div>
        <div className="flex gap-2">
          <ConsoleButton onClick={() => void refreshJobsAndDatasets()} aria-label="刷新自动标注任务">
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            刷新
          </ConsoleButton>
          <ConsoleButton variant="primary" onClick={() => setShowCreate((value) => !value)}>
            {showCreate ? <X className="h-4 w-4" aria-hidden="true" /> : <Plus className="h-4 w-4" aria-hidden="true" />}
            {showCreate ? "收起" : "新建任务"}
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

      {showCreate && (
        <ConsoleCard>
          <div className="mb-4">
            <h2 className="text-base font-semibold text-console-text">新建导航自动标注任务</h2>
            <p className="mt-1 text-sm text-console-muted">处理标定由用户按当天数据显式选择，页面不提供全局推荐。</p>
          </div>
          <div className="grid gap-4 lg:grid-cols-3">
            <label className="text-sm text-console-muted">
              <span className="mb-2 block font-medium text-console-text">数据日期</span>
              <select
                aria-label="自动标注数据日期"
                value={selectedDate}
                className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text focus:border-console-cyan focus:outline-none"
                onChange={(event) => void chooseDate(event.target.value)}
              >
                <option value="">请选择已同步日期</option>
                {dates.map((date) => <option key={date.date} value={date.date}>{date.date}</option>)}
              </select>
            </label>
            <label className="text-sm text-console-muted">
              <span className="mb-2 block font-medium text-console-text">当天处理标定</span>
              <select
                aria-label="当天处理标定"
                value={selectedProfile}
                className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text focus:border-console-cyan focus:outline-none"
                onChange={(event) => setSelectedProfile(event.target.value)}
              >
                <option value="">请选择标定参数</option>
                {profiles.map((profile) => (
                  <option key={profile.profile_ref} value={profile.profile_ref}>{profile.label}</option>
                ))}
              </select>
            </label>
            <div className="flex items-end">
              <ConsoleButton className="w-full" variant="primary" disabled={!canCreate} onClick={() => void create()}>
                {creating ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Plus className="h-4 w-4" aria-hidden="true" />}
                {creating ? "正在创建…" : "创建并准备"}
              </ConsoleButton>
            </div>
          </div>

          <div className="mt-4">
            <p className="mb-2 text-sm font-medium text-console-text">外层 clips</p>
            {!selectedDate ? (
              <p className="text-sm text-console-muted">请先选择日期。</p>
            ) : dateDetail === null ? (
              <p className="text-sm text-console-muted">正在读取已同步 clips…</p>
            ) : availableClips.length === 0 ? (
              <p className="text-sm text-console-muted">该日期没有可处理的已同步 clip。</p>
            ) : (
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {availableClips.map((clip) => (
                  <label
                    key={clip.clip}
                    className="flex items-center gap-3 rounded-lg border border-console-line bg-console-panel2/65 px-3 py-2 text-sm text-console-text"
                  >
                    <input
                      type="checkbox"
                      checked={selectedClips.includes(clip.clip)}
                      onChange={(event) => setSelectedClips((current) => (
                        event.target.checked
                          ? [...current, clip.clip]
                          : current.filter((item) => item !== clip.clip)
                      ))}
                    />
                    <span className="truncate">{clip.clip}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </ConsoleCard>
      )}

      {error && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      {loading ? (
        <PageMessage icon={LoaderCircle} title="正在读取自动标注任务…" />
      ) : jobs.length === 0 ? (
        <PageMessage
          icon={Tags}
          title="还没有自动标注任务"
          detail="新建任务后，系统会先准备 resize 后首帧，再进入 Web 标注队列。"
        />
      ) : (
        <div className="space-y-3">
          {jobs.map((job) => {
            const status = JOB_STATUS[job.status];
            const statusLabel = job.cancel_requested
              ? "正在取消"
              : job.completion_outcome === "no_processable_targets"
              ? "无可处理目标"
              : status.label;
            return (
              <button
                key={job.job_ref}
                type="button"
                className="grid w-full gap-3 rounded-lg border border-console-line bg-console-panel p-4 text-left shadow-sm transition hover:border-console-cyan/45 focus:outline-none focus:ring-2 focus:ring-console-cyan md:grid-cols-[10rem_1fr_auto] md:items-center"
                onClick={() => navigate(`/annotation/jobs/${encodeURIComponent(job.job_ref)}`)}
              >
                <div>
                  <p className="text-sm font-semibold text-console-text">{job.dataset_date}</p>
                  <p className="mt-1 text-xs text-console-muted">{formattedTime(job.updated_at)}</p>
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm text-console-text">{job.source_clips.join("、")}</p>
                  <p className="mt-1 text-xs text-console-muted">{countsSummary(job)}</p>
                </div>
                <div className="flex items-center justify-between gap-3 md:justify-end">
                  <StatusTag tone={job.cancel_requested ? "warning" : status.tone}>{statusLabel}</StatusTag>
                  <ChevronRight className="h-4 w-4 text-console-muted" aria-hidden="true" />
                </div>
              </button>
            );
          })}
        </div>
      )}
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
    <div className="console-soft-scrollbar max-h-[calc(100vh-15rem)] space-y-4 overflow-y-auto p-3">
      {grouped.map(([sourceClip, segments]) => (
        <div key={sourceClip}>
          <p className="mb-2 truncate px-1 text-xs font-semibold text-console-muted" title={sourceClip}>{sourceClip}</p>
          <div className="space-y-2">
            {segments.map((segment) => {
              const status = SEGMENT_STATUS[segment.status];
              return (
                <button
                  key={segment.segment_ref}
                  type="button"
                  className={`w-full rounded-lg border p-3 text-left transition focus:outline-none focus:ring-2 focus:ring-console-cyan ${
                    currentSegmentRef === segment.segment_ref
                      ? "border-console-cyan bg-blue-50/70"
                      : "border-console-line bg-console-panel2/65 hover:border-console-cyan/45"
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
    <section className="mx-auto max-w-[96rem] space-y-4 px-4 py-6 md:px-6">
      <DataRouterFlushBlocker
        enabled={Boolean(editable)}
        flush={flushForNavigation}
      />
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-start gap-3">
          <ConsoleButton
            aria-label="返回自动标注任务"
            onClick={() => void navigateSafely(`/annotation/jobs/${encodeURIComponent(jobRef)}`)}
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </ConsoleButton>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold text-console-text">
                Segment {String(segment.ordinal).padStart(2, "0")}
              </h2>
              <StatusTag tone={status.tone}>{status.label}</StatusTag>
            </div>
            <p className="mt-1 text-sm text-console-muted">{job.dataset_date} · {segment.source_clip}</p>
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
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          {actionError || error}
        </div>
      )}

      <label className="block text-sm text-console-muted xl:hidden">
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

      {showSkip && (
        <ConsoleCard>
          <div className="grid gap-3 lg:grid-cols-[1fr_1.2fr_auto] lg:items-end">
            <label className="text-sm text-console-muted">
              <span className="mb-2 block font-medium text-console-text">跳过原因</span>
              <select
                aria-label="跳过原因"
                value={skipReason}
                className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text"
                onChange={(event) => setSkipReason(event.target.value as typeof skipReason)}
              >
                <option value="no_valid_target">首帧无有效目标</option>
                <option value="unusable_first_frame">首帧不可用</option>
                <option value="other">其他原因</option>
              </select>
            </label>
            <label className="text-sm text-console-muted">
              <span className="mb-2 block font-medium text-console-text">说明</span>
              <input
                aria-label="跳过说明"
                value={skipNote}
                placeholder={skipReason === "other" ? "其他原因必须填写说明" : "可选"}
                className="h-10 w-full rounded-lg border border-console-line bg-white px-3 text-sm text-console-text"
                onChange={(event) => setSkipNote(event.target.value)}
              />
            </label>
            <div className="flex gap-2">
              <ConsoleButton onClick={() => setShowSkip(false)}>取消</ConsoleButton>
              <ConsoleButton
                variant="primary"
                disabled={acting || (skipReason === "other" && !skipNote.trim())}
                onClick={() => void skip()}
              >
                确认跳过
              </ConsoleButton>
            </div>
          </div>
        </ConsoleCard>
      )}

      <div className="grid gap-4 xl:grid-cols-[16rem_minmax(0,1fr)]">
        <ConsoleCard className="hidden p-0 xl:block">
          <div className="border-b border-console-line p-4">
            <h3 className="text-sm font-semibold text-console-text">Segment 队列</h3>
            <p className="mt-1 text-xs text-console-muted">{countsSummary(job)}</p>
          </div>
          <SegmentQueue
            job={job}
            currentSegmentRef={segment.segment_ref}
            onNavigate={(nextSegmentRef) => navigateSafely(
              `/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(nextSegmentRef)}`,
            )}
          />
        </ConsoleCard>

        {segment.status === "skipped" ? (
          <PageMessage
            icon={SkipForward}
            title="此 Segment 已显式跳过"
            detail={segment.skip_reason?.note || "它不会进入 Tracking；Tracking 开始前仍可恢复标注。"}
          />
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
            registerFlush={(flush) => {
              flushRef.current = flush;
            }}
          />
        )}
      </div>
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
