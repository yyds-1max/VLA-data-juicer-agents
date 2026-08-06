import { Dialog } from "radix-ui";
import {
  AlertCircle,
  ArrowLeft,
  Ban,
  LoaderCircle,
  RotateCcw,
  SkipForward,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useBlocker,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useStore } from "zustand";

import type { NavigationDateSummary } from "../../../api/types";
import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { StatusTag } from "../../../components/console/StatusTag";
import { datapilotStore } from "../../../store/datapilotStore";
import { NavigationDataPilotDialog } from "../components/NavigationDataPilotDialog";
import {
  buildAnnotationProcessingRequest,
  buildNavigationDatasetRequestContext,
  type NavigationDatasetSelection,
} from "../navigationDataPilotRequest";
import {
  getNavigationDatasetSummaryCached,
  resetNavigationDatasetSummaryCache,
} from "../navigationDatasetSummaryCache";
import { InitialAnnotationWorkbench } from "../../annotation/InitialAnnotationWorkbench";
import { AnnotationWorkbenchHelp } from "../../annotation/AnnotationWorkbenchHelp";
import { AnnotationWorkbenchLocation } from "../../annotation/AnnotationWorkbenchLocation";
import {
  AnnotationApiError,
  mutateAnnotationJob,
  mutateAnnotationSegment,
  skipAnnotationSegment,
} from "../../annotation/api";
import {
  annotationProjectionStore,
  cacheAnnotationJob,
  cacheAnnotationSegment,
  loadAnnotationCapability,
  loadAnnotationJob,
  loadAnnotationJobs,
  loadAnnotationSegment,
  retainAnnotationJobProjection,
  retainAnnotationSegmentProjection,
} from "../../annotation/projectionStore";
import type {
  AnnotationJobDetail,
  AnnotationJobStatus,
  AnnotationJobSummary,
  AnnotationSegmentDetail,
  AnnotationSegmentSummary,
  AnnotationSegmentStatus,
} from "../../annotation/types";
import { AnnotationJobProgress } from "../../annotation/AnnotationJobProgress";
import {
  AnnotationJobActivity,
  AnnotationJobNextStep,
  buildAnnotationJobNextStep,
} from "../../annotation/AnnotationJobDetailPanels";
import { JobsIndexView } from "../../annotation/JobsIndexView";
import { SegmentQueuePanel } from "../../annotation/SegmentQueuePanel";

const JOB_STATUS: Record<AnnotationJobStatus, { label: string; tone: "success" | "info" | "warning" | "danger" | "neutral" }> = {
  preparing: { label: "准备中", tone: "info" },
  waiting_initial_annotation: { label: "待首帧标注", tone: "warning" },
  tracking: { label: "Tracking 中", tone: "info" },
  tracked: { label: "Tracking 已完成", tone: "success" },
  postprocessing: { label: "后处理中", tone: "info" },
  annotated: { label: "已标注", tone: "success" },
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
  postprocessing: { label: "后处理中", tone: "info" },
  annotated: { label: "已标注", tone: "success" },
  postprocessing_failed: { label: "后处理失败", tone: "danger" },
};

function safeError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback;
  const code = error instanceof AnnotationApiError ? error.detail?.code : null;
  const looksPrivate = /(?:^|[\s("'`])\/(?:[^/\s]+\/){2,}|[A-Za-z]:\\/.test(message);
  if (looksPrivate) return code ? `${fallback}（${code}）` : fallback;
  return message || fallback;
}

function cancellableJob(status: AnnotationJobStatus): boolean {
  return (
    status === "preparing"
    || status === "waiting_initial_annotation"
    || status === "tracking"
    || status === "postprocessing"
  );
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

function resolvedSegmentCount(job: Pick<AnnotationJobSummary, "counts">): number {
  return (
    job.counts.submitted
    + job.counts.skipped
    + job.counts.tracking
    + job.counts.tracked
    + (job.counts.postprocessing ?? 0)
    + (job.counts.annotated ?? 0)
  );
}

function countsSummary(job: AnnotationJobSummary): string {
  const resolved = resolvedSegmentCount(job);
  return `${resolved}/${job.counts.total} 个 segment 已处理`;
}

function annotationJobProgressHint(job: AnnotationJobDetail): string {
  const pendingSegments = job.counts.pending_initial_annotation + job.counts.draft;

  if (job.cancel_requested) return "取消请求已提交，系统正在等待当前处理安全结束。";
  if (job.status === "failed") return "任务处理失败，请查看下方错误信息并选择安全的恢复方式。";
  if (job.status === "cancelled") {
    return job.completion_outcome === "no_processable_targets"
      ? "没有发现有效处理目标，本任务已结束。"
      : "任务已取消，已有处理记录仍会保留。";
  }
  if (job.status === "preparing") return "正在准备 Web 首帧，暂时无需人工操作。";
  if (job.status === "waiting_initial_annotation") {
    return job.ready_for_tracking
      ? "首帧标注已全部提交，等待 DataPilot 继续 Tracking。"
      : `还有 ${pendingSegments} 个 Segment 等待首帧标注。`;
  }
  if (job.status === "tracking") return "DataPilot 正在执行 Tracking，并持续保存处理检查点。";
  if (job.status === "tracked") return "Tracking 已完成，等待 DataPilot 启动后处理。";
  if (job.status === "postprocessing") return "DataPilot 正在执行后处理，页面可以安全关闭。";
  return "标注结果已生成，等待进入人工复核。";
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
    postprocessing: segments.filter((item) => item.status === "postprocessing").length,
    annotated: segments.filter((item) => item.status === "annotated").length,
    postprocessing_failed: segments.filter((item) => item.status === "postprocessing_failed").length,
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
  const jobs = useStore(annotationProjectionStore, (state) => state.jobs);
  const jobsLoaded = useStore(annotationProjectionStore, (state) => state.jobsLoaded);
  const capability = useStore(annotationProjectionStore, (state) => state.capability);
  const capabilityLoaded = useStore(
    annotationProjectionStore,
    (state) => state.capabilityLoaded,
  );
  const [dates, setDates] = useState<NavigationDateSummary[]>([]);
  const [loading, setLoading] = useState(!jobsLoaded || !capabilityLoaded);
  const [refreshing, setRefreshing] = useState(false);
  const [pageError, setPageError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [activeInvocationId, setActiveInvocationId] = useState<string | null>(null);
  const pendingInvocation = useStore(datapilotStore, (state) => state.pendingInvocation);

  const activeInvocation =
    activeInvocationId && pendingInvocation?.invocationId === activeInvocationId
      ? pendingInvocation
      : null;
  const submitting =
    activeInvocation?.status === "queued" || activeInvocation?.status === "submitting";
  const invocationError =
    activeInvocation?.status === "failed"
      ? activeInvocation.error ?? "提交失败，请重试。"
      : null;

  const refresh = useCallback(async (invalidateDataset = false) => {
    setPageError("");
    if (invalidateDataset) setRefreshing(true);
    if (invalidateDataset) resetNavigationDatasetSummaryCache();
    const results = await Promise.allSettled([
      loadAnnotationJobs({ force: invalidateDataset }),
      loadAnnotationCapability({ force: invalidateDataset }),
      getNavigationDatasetSummaryCached(),
    ]);
    const [jobsResult, capabilityResult, datasetResult] = results;
    if (jobsResult.status === "rejected") {
      setPageError(safeError(jobsResult.reason, "读取自动标注任务失败"));
    }
    if (capabilityResult.status === "rejected") {
      setPageError((current) => current || safeError(
        capabilityResult.reason,
        "读取处理环境状态失败",
      ));
    }
    if (datasetResult.status === "fulfilled") {
      setDates(datasetResult.value.dates.filter((date) => date.synced_clip_count > 0));
    } else {
      setPageError((current) => current || safeError(
        datasetResult.reason,
        "读取已同步数据失败",
      ));
    }
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void refresh(false);
  }, [refresh]);

  useEffect(() => {
    if (
      !activeInvocationId
      || pendingInvocation?.invocationId !== activeInvocationId
      || pendingInvocation.status !== "submitted"
    ) {
      return;
    }
    setDialogOpen(false);
    setActiveInvocationId(null);
    datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
  }, [activeInvocationId, pendingInvocation]);

  const confirmDataPilot = (selection: NavigationDatasetSelection) => {
    const message = buildAnnotationProcessingRequest(selection);
    const requestContext = buildNavigationDatasetRequestContext(selection);
    if (
      activeInvocationId
      && pendingInvocation?.invocationId === activeInvocationId
      && pendingInvocation.message === message
      && pendingInvocation.status === "failed"
    ) {
      datapilotStore.getState().retryDataPilotInvocation(activeInvocationId);
      return;
    }
    if (activeInvocationId) {
      datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
    }
    const invocationId = createAnnotationInvocationId();
    if (datapilotStore.getState().launchDataPilotRequest(
      invocationId,
      message,
      requestContext,
      "annotation_processing_shortcut",
    )) {
      setActiveInvocationId(invocationId);
    }
  };

  return (
    <section className="mx-auto max-w-360 px-3 pb-28 pt-2 md:px-4 lg:px-5">
      <JobsIndexView
        jobs={jobs}
        loading={loading}
        refreshing={refreshing}
        error={pageError}
        capability={capability}
        dataPilotDisabled={submitting}
        onRefresh={() => void refresh(true)}
        onOpenDataPilot={() => setDialogOpen(true)}
        onPrimaryAction={(job) => {
          navigate(`/annotation/jobs/${encodeURIComponent(job.job_ref)}`);
        }}
      />

      <NavigationDataPilotDialog
        dates={dates}
        error={invocationError}
        open={dialogOpen}
        submitting={submitting}
        onCancel={() => {
          if (activeInvocationId) {
            datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
          }
          setActiveInvocationId(null);
          setDialogOpen(false);
        }}
        onConfirm={confirmDataPilot}
        onSelectionChange={() => {
          if (
            activeInvocationId
            && pendingInvocation?.invocationId === activeInvocationId
            && pendingInvocation.status === "failed"
          ) {
            datapilotStore.getState().clearDataPilotInvocation(activeInvocationId);
            setActiveInvocationId(null);
          }
        }}
      />
    </section>
  );
}

function createAnnotationInvocationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `annotation-${crypto.randomUUID()}`;
  }
  return `annotation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
                  className={`w-full rounded-md border border-transparent px-2.5 py-2.5 text-left transition focus:outline-hidden focus:ring-2 focus:ring-console-cyan ${
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
    action: "complete-no-processable-targets" | "cancel" | "retry",
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
        {job.ready_for_no_processable_targets && (
          <ConsoleButton disabled={acting} onClick={() => void mutate("complete-no-processable-targets")}>
            <Ban className="h-4 w-4" aria-hidden="true" />
            无可处理目标
          </ConsoleButton>
        )}
        {(cancellableJob(job.status) || (
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
  const projectedJob = useStore(
    annotationProjectionStore,
    (state) => state.jobDetails[jobRef] ?? null,
  );
  const [job, setJob] = useState<AnnotationJobDetail | null>(projectedJob);
  const [loading, setLoading] = useState(projectedJob === null);
  const [error, setError] = useState("");
  const updateJob = useCallback((nextJob: AnnotationJobDetail) => {
    const cached = cacheAnnotationJob(nextJob);
    setJob((current) => preferMonotonicJob(current, cached));
  }, []);

  useEffect(
    () => retainAnnotationJobProjection(jobRef),
    [jobRef],
  );

  const refresh = useCallback(async () => {
    try {
      const nextJob = await loadAnnotationJob(jobRef);
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
    if (projectedJob) updateJob(projectedJob);
  }, [projectedJob, updateJob]);

  if (loading) {
    return <section className="mx-auto w-full max-w-[1900px] px-3 py-5 sm:px-4 md:py-6 lg:px-6 2xl:px-8"><PageMessage icon={LoaderCircle} title="正在读取任务…" /></section>;
  }
  if (!job) {
    return (
      <section className="mx-auto w-full max-w-[1900px] px-3 py-5 sm:px-4 md:py-6 lg:px-6 2xl:px-8">
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
  const nextStep = buildAnnotationJobNextStep(job);

  return (
    <section
      className="mx-auto w-full max-w-[1900px] space-y-4 px-3 py-5 sm:px-4 md:py-6 lg:px-6 2xl:px-8"
      data-testid="annotation-job-detail-page"
    >
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

      <ConsoleCard className="overflow-hidden px-4 pb-0 pt-4 sm:px-5 sm:pt-5">
        <AnnotationJobProgress job={job} />
        <p
          className="mt-3 border-t border-console-line px-2 py-3 text-center text-xs leading-5 text-console-muted"
        >
          {annotationJobProgressHint(job)}
        </p>
      </ConsoleCard>

      <div className="grid min-w-0 gap-4 xl:grid-cols-[20rem_minmax(0,1fr)] 2xl:grid-cols-[21rem_minmax(0,1fr)]">
        <SegmentQueuePanel
          job={job}
          currentSegmentRef={nextStep.segment?.segment_ref}
          className="min-h-[26rem] xl:min-h-[32rem] xl:max-h-[calc(100vh-12rem)]"
          onNavigate={(segmentRef) => navigate(`/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(segmentRef)}`)}
        />

        <div className="min-w-0 space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <ConsoleCard className="min-h-24">
              <div>
                <p className="text-xs text-console-muted">处理标定</p>
                <p className="mt-2 text-sm font-medium text-console-text">{job.calibration.label}</p>
              </div>
            </ConsoleCard>
            <ConsoleCard className="min-h-24">
              <div>
                <p className="text-xs text-console-muted">待标注</p>
                <p className="mt-2 text-xl font-semibold tabular-nums text-[#D88312]">{job.counts.pending_initial_annotation + job.counts.draft}</p>
              </div>
            </ConsoleCard>
            <ConsoleCard className="min-h-24">
              <div>
                <p className="text-xs text-console-muted">已提交</p>
                <p className="mt-2 text-xl font-semibold tabular-nums text-[#228B58]">{job.counts.submitted}</p>
              </div>
            </ConsoleCard>
            <ConsoleCard className="min-h-24">
              <div>
                <p className="text-xs text-console-muted">已跳过</p>
                <p className="mt-2 text-xl font-semibold tabular-nums text-[#657087]">{job.counts.skipped}</p>
              </div>
            </ConsoleCard>
          </div>

          <AnnotationJobNextStep
            job={job}
            onOpenReviews={() => navigate("/annotation/reviews")}
            onOpenSegment={(segmentRef) => navigate(`/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(segmentRef)}`)}
          />
          <AnnotationJobActivity
            guidance={annotationJobProgressHint(job)}
            job={job}
          />
        </div>
      </div>
    </section>
  );
}

function SegmentPage({ jobRef, segmentRef }: { jobRef: string; segmentRef: string }) {
  const navigate = useNavigate();
  const projectedJob = useStore(
    annotationProjectionStore,
    (state) => state.jobDetails[jobRef] ?? null,
  );
  const projectedSegment = useStore(
    annotationProjectionStore,
    (state) => state.segmentDetails[`${jobRef}:${segmentRef}`] ?? null,
  );
  const [job, setJob] = useState<AnnotationJobDetail | null>(projectedJob);
  const [segment, setSegment] = useState<AnnotationSegmentDetail | null>(projectedSegment);
  const [loading, setLoading] = useState(!projectedJob || !projectedSegment);
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
    const cached = cacheAnnotationJob(nextJob);
    setJob((current) => preferMonotonicJob(current, cached));
  }, []);
  const updateSegment = useCallback((nextSegment: AnnotationSegmentDetail) => {
    const cached = cacheAnnotationSegment(jobRef, nextSegment);
    const current = currentSegmentRef.current;
    if (
      current
      && current.segment_ref === cached.segment_ref
      && cached.state_revision < current.state_revision
    ) {
      return;
    }
    currentSegmentRef.current = cached;
    setSegment(cached);
  }, [jobRef]);

  useEffect(
    () => retainAnnotationJobProjection(jobRef),
    [jobRef],
  );

  useEffect(
    () => retainAnnotationSegmentProjection(jobRef, segmentRef),
    [jobRef, segmentRef],
  );

  const refreshJob = useCallback(async () => {
    const nextJob = await loadAnnotationJob(jobRef, { force: true });
    updateJob(nextJob);
  }, [jobRef, updateJob]);

  useEffect(() => {
    setExternalSubmissionNotice("");
  }, [jobRef, segmentRef]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void Promise.all([
      loadAnnotationJob(jobRef),
      loadAnnotationSegment(jobRef, segmentRef),
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
    if (projectedJob) updateJob(projectedJob);
  }, [projectedJob, updateJob]);

  useEffect(() => {
    if (projectedSegment) updateSegment(projectedSegment);
  }, [projectedSegment, updateSegment]);

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
      <section className="mx-auto max-w-384 px-4 py-6 md:px-6">
        {loading
          ? <PageMessage icon={LoaderCircle} title="正在恢复首帧标注工作台…" />
          : <PageMessage title="无法打开 Segment" detail={error} />}
      </section>
    );
  }

  const status = SEGMENT_STATUS[segment.status];
  const clipSegments = job.segments
    .filter((item) => item.source_clip === segment.source_clip)
    .sort((left, right) => (
      left.ordinal - right.ordinal
      || left.segment_ref.localeCompare(right.segment_ref)
    ));
  const clipSegmentIndex = clipSegments.findIndex((item) => item.segment_ref === segment.segment_ref);
  const clipSegmentOrdinal = clipSegmentIndex >= 0 ? clipSegmentIndex + 1 : segment.ordinal;
  const clipSegmentCount = clipSegments.length || 1;

  return (
    <section className="flex h-[calc(100dvh-124px)] min-h-168 flex-col overflow-hidden bg-[#edf0f5] md:h-dvh md:min-h-0">
      <DataRouterFlushBlocker
        enabled={Boolean(editable)}
        flush={flushForNavigation}
      />
      <div className="relative z-50 min-h-16 shrink-0 border-b border-[#e3e6ed] bg-white px-3 py-2 sm:px-4">
        <AnnotationWorkbenchLocation
          datasetDate={job.dataset_date}
          sourceClip={segment.source_clip}
          segmentOrdinal={clipSegmentOrdinal}
          segmentCount={clipSegmentCount}
          statusLabel={status.label}
          statusTone={status.tone}
          backLabel="返回自动标注任务"
          navigationLabel="标注工作台位置"
          onBack={() => void navigateSafely(`/annotation/jobs/${encodeURIComponent(jobRef)}`)}
          actions={<>
          {editable && (
            <ConsoleButton className="h-8 bg-white px-3 shadow-none" disabled={acting} onClick={() => setShowSkip(true)}>
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
          <AnnotationWorkbenchHelp />
          </>}
        />
      </div>

      {(error || actionError) && (
        <div role="alert" className="relative z-40 shrink-0 border-b border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {actionError || error}
        </div>
      )}

      {externalSubmissionNotice && (
        <div
          role="alert"
          className="relative z-40 shrink-0 border-b border-blue-200 bg-blue-50 px-3 py-2 text-sm font-medium leading-6 text-blue-800"
        >
          {externalSubmissionNotice}
        </div>
      )}

      <div
        data-testid="annotation-studio-shell"
        className="min-h-0 flex-1 overflow-hidden bg-[#edf0f5]"
      >
        <div className="h-full min-h-0 min-w-0 overflow-hidden">
          {segment.status === "skipped" ? (
            <div className="h-full p-4">
              <PageMessage
                icon={SkipForward}
                title="此 Segment 已显式跳过"
                detail={segment.skip_reason?.note || "它不会进入 Tracking；DataPilot 启动 Tracking 前仍可恢复标注。"}
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
              onNavigateSegment={(nextSegmentRef) => navigateSafely(
                `/annotation/jobs/${encodeURIComponent(jobRef)}/segments/${encodeURIComponent(nextSegmentRef)}`,
              )}
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
          <Dialog.Overlay className="fixed inset-0 z-90 bg-slate-950/35 backdrop-blur-[1px]" />
          <Dialog.Content
            aria-describedby="skip-segment-description"
            className="fixed left-1/2 top-1/2 z-91 w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-xl border border-console-line bg-console-panel shadow-2xl focus:outline-hidden"
          >
            <div className="flex items-start justify-between gap-4 border-b border-console-line px-5 py-4">
              <div>
                <Dialog.Title className="text-base font-semibold text-console-text">跳过此 Segment</Dialog.Title>
                <Dialog.Description id="skip-segment-description" className="mt-1 text-sm text-console-muted">
                  跳过后不会进入 Tracking，DataPilot 启动 Tracking 前仍可恢复。
                </Dialog.Description>
              </div>
              <button
                type="button"
                aria-label="关闭跳过 Segment"
                disabled={acting}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-hidden focus:ring-2 focus:ring-console-cyan disabled:opacity-40"
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
  if (jobRef) return <JobPage key={jobRef} jobRef={jobRef} />;
  return <JobsPage />;
}
