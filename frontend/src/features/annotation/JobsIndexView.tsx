import * as React from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Bot,
  CircleAlert,
  Eye,
  FileStack,
  Info,
  RefreshCw,
} from "lucide-react";

import { ScrambleTitle } from "@/components/console/ScrambleTitle";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CircularProgress } from "@/components/ui/circular-progress";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

import { AnnotationListPageHeader } from "./AnnotationListPageHeader";
import {
  ANNOTATION_JOB_LIST_FILTERS,
  annotationJobListMetrics,
  annotationJobPrimaryActionLabel,
  annotationJobsForFilter,
  annotationJobStatusPresentation,
  annotationJobTableProgress,
  buildAnnotationJobPopoverModel,
  formatAnnotationJobUpdatedAt,
  type AnnotationJobListFilter,
} from "./annotationJobPresentation";
import type { AnnotationCapability, AnnotationJobSummary } from "./types";

const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;

type DisplaySelection = {
  filter: AnnotationJobListFilter;
  page: number;
  pageSize: number;
};

type TransitionPhase = "idle" | "out" | "enter";

function requestFrame(callback: FrameRequestCallback): number {
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(() => callback(performance.now()), 16);
}

function cancelFrame(handle: number): void {
  if (
    typeof window.requestAnimationFrame === "function" &&
    typeof window.cancelAnimationFrame === "function"
  ) {
    window.cancelAnimationFrame(handle);
    return;
  }
  window.clearTimeout(handle);
}

export type JobsIndexViewProps = {
  jobs: AnnotationJobSummary[];
  loading: boolean;
  error?: string | null;
  capability?: AnnotationCapability | null;
  refreshing?: boolean;
  dataPilotDisabled?: boolean;
  initialFilter?: AnnotationJobListFilter;
  className?: string;
  onRefresh: () => void;
  onOpenDataPilot: () => void;
  onPrimaryAction: (job: AnnotationJobSummary) => void;
};

function usePrefersReducedMotion(): boolean {
  const [reducedMotion, setReducedMotion] = React.useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });

  React.useEffect(() => {
    if (!window.matchMedia) return undefined;
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updatePreference = () => setReducedMotion(mediaQuery.matches);
    updatePreference();
    mediaQuery.addEventListener?.("change", updatePreference);
    return () => mediaQuery.removeEventListener?.("change", updatePreference);
  }, []);

  return reducedMotion;
}

function StatusBadge({ job }: { job: AnnotationJobSummary }) {
  const status = annotationJobStatusPresentation(job);
  return (
    <span className="inline-flex items-center gap-2 text-xs font-medium text-slate-600">
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 rounded-full",
          status.tone === "neutral" && "bg-slate-400",
          status.tone === "info" && "bg-blue-500",
          status.tone === "warning" && "bg-amber-500",
          status.tone === "success" && "bg-emerald-500",
          status.tone === "danger" && "bg-rose-500",
        )}
      />
      {status.label}
    </span>
  );
}

function JobDetailsPopover({ job }: { job: AnnotationJobSummary }) {
  const model = buildAnnotationJobPopoverModel(job);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          aria-label={`查看 ${job.dataset_date} 任务详情`}
          className="h-7 px-2 text-xs text-slate-500 hover:bg-blue-50 hover:text-blue-700 active:translate-y-px"
        >
          <Info aria-hidden="true" className="size-3.5" />
          详情
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={8}
        className="w-[min(19rem,calc(100vw-2rem))] rounded-xl border-slate-200 p-0 shadow-xl shadow-slate-950/8"
      >
        <div className="border-b border-slate-100 px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-950">
                {job.dataset_date}
              </p>
              <p className="mt-0.5 truncate font-mono text-[11px] text-slate-400">
                {job.job_ref}
              </p>
            </div>
            <StatusBadge job={job} />
          </div>
        </div>

        <div className="space-y-4 px-4 py-4">
          <div className="flex items-center gap-4">
            <div className="flex shrink-0 flex-col items-center gap-1">
              <CircularProgress
                value={model.ringValue}
                aria-label={`${model.ringCaption}，${model.ringLabel}`}
                centerLabel={model.ringLabel}
                className="size-[76px] text-[#3157cf]"
                trackClassName="text-[#e8edf8]"
                indicatorClassName="text-[#3157cf]"
              />
              <span className="max-w-20 truncate text-[10px] text-slate-500">
                {model.ringCaption}
              </span>
            </div>

            <dl className="min-w-0 flex-1 space-y-1.5 text-xs">
              {model.breakdown.length > 0 ? (
                model.breakdown.map((item) => (
                  <div key={item.status} className="flex items-center justify-between gap-3">
                    <dt className="truncate text-slate-500">{item.label}</dt>
                    <dd className="font-medium tabular-nums text-slate-800">{item.count}</dd>
                  </div>
                ))
              ) : (
                <p className="text-slate-500">暂无 Segment 明细</p>
              )}
            </dl>
          </div>

          {model.failureMessage ? (
            <div className="rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-xs leading-5 text-rose-700">
              {model.failureMessage}
            </div>
          ) : null}

          <dl className="grid grid-cols-[5rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
            <dt className="text-slate-500">源数据</dt>
            <dd className="min-w-0 text-right text-slate-800">
              {model.sourceClips.length > 0 ? (
                <span
                  className="block max-h-24 space-y-1 overflow-y-auto overscroll-contain pr-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
                  tabIndex={model.sourceClips.length > 3 ? 0 : undefined}
                  aria-label="全部源数据 clip"
                >
                  {model.sourceClips.map((clip) => (
                    <span key={clip} className="block truncate" title={clip}>
                      {clip}
                    </span>
                  ))}
                </span>
              ) : (
                "未提供"
              )}
            </dd>
            <dt className="text-slate-500">处理标定</dt>
            <dd className="truncate text-right text-slate-800" title={model.calibrationLabel}>
              {model.calibrationLabel}
            </dd>
            <dt className="text-slate-500">最近更新</dt>
            <dd className="text-right tabular-nums text-slate-800">{model.updatedAtLabel}</dd>
          </dl>

          <div className="rounded-lg border border-blue-100 bg-blue-50/70 px-3 py-2.5">
            <p className="text-[11px] font-medium text-blue-700">下一步</p>
            <p className="mt-1 text-xs leading-5 text-slate-700">{model.nextStep}</p>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function LightweightMetric({
  label,
  value,
  index,
}: {
  label: string;
  value: number;
  index: number;
}) {
  return (
    <article
      className={cn(
        "flex min-h-22 items-center px-4 py-4 sm:px-5 lg:px-6",
        index < 2 && "border-b border-slate-200 xl:border-b-0",
        index % 2 === 0 && "border-r border-slate-200",
        index < 3 && "xl:border-r xl:border-slate-200",
        index === 2 && "xl:border-b-0",
      )}
    >
      <div className="min-w-0">
        <p className="text-xs font-medium text-slate-500">{label}</p>
        <p className="mt-2 text-2xl font-semibold tabular-nums tracking-tight text-slate-950">
          {value.toLocaleString("zh-CN")}
        </p>
      </div>
    </article>
  );
}

function LoadingView() {
  return (
    <div aria-label="正在加载标注任务" aria-busy="true" className="space-y-4">
      <div
        className="grid grid-cols-2 border-y border-slate-200 bg-white xl:grid-cols-4"
        data-testid="annotation-metrics-strip"
      >
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className={cn("min-h-22 px-5 py-4", index < 2 && "border-b border-slate-200", index % 2 === 0 && "border-r border-slate-200", index < 3 && "xl:border-r", index === 2 && "xl:border-b-0")}>
            <div className="space-y-3">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-7 w-14" />
            </div>
          </div>
        ))}
      </div>
      <section className="min-h-[30rem] border-y border-slate-200 bg-white">
        <div className="space-y-4 px-5 py-5">
          <Skeleton className="h-7 w-48" />
          <Skeleton className="h-9 w-full" />
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-12 w-full" />
          ))}
        </div>
      </section>
    </div>
  );
}

const EMPTY_COPY: Record<AnnotationJobListFilter, { title: string; description: string }> = {
  waiting: {
    title: "没有待处理任务",
    description: "当前没有需要继续完成首帧标注的任务。",
  },
  running: {
    title: "没有运行中的任务",
    description: "DataPilot 当前没有正在准备、Tracking 或后处理的任务。",
  },
  error: {
    title: "没有异常任务",
    description: "当前任务运行正常，无需额外处理。",
  },
  history: {
    title: "暂无历史记录",
    description: "已标注或已取消的任务会显示在这里。",
  },
};

export function JobsIndexView({
  jobs,
  loading,
  error,
  capability,
  refreshing = false,
  dataPilotDisabled = false,
  initialFilter = "waiting",
  className,
  onRefresh,
  onOpenDataPilot,
  onPrimaryAction,
}: JobsIndexViewProps) {
  const reducedMotion = usePrefersReducedMotion();
  const [filter, setFilter] = React.useState<AnnotationJobListFilter>(initialFilter);
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(10);
  const [displaySelection, setDisplaySelection] = React.useState<DisplaySelection>({
    filter: initialFilter,
    page: 1,
    pageSize: 10,
  });
  const displaySelectionRef = React.useRef<DisplaySelection>(displaySelection);
  const [transitionPhase, setTransitionPhase] = React.useState<TransitionPhase>("idle");
  const filterTabsRef = React.useRef<HTMLDivElement>(null);

  const metrics = React.useMemo(() => annotationJobListMetrics(jobs), [jobs]);
  const categoryCounts = React.useMemo(
    () =>
      Object.fromEntries(
        ANNOTATION_JOB_LIST_FILTERS.map(({ value }) => [
          value,
          annotationJobsForFilter(jobs, value).length,
        ]),
      ) as Record<AnnotationJobListFilter, number>,
    [jobs],
  );
  const selectedJobs = React.useMemo(
    () => annotationJobsForFilter(jobs, filter),
    [filter, jobs],
  );
  const pageCount = Math.max(1, Math.ceil(selectedJobs.length / pageSize));
  const currentPage = Math.min(page, pageCount);

  React.useEffect(() => {
    if (page !== currentPage) setPage(currentPage);
  }, [currentPage, page]);

  React.useEffect(() => {
    const nextSelection = { filter, page: currentPage, pageSize };
    const currentSelection = displaySelectionRef.current;
    if (
      currentSelection.filter === nextSelection.filter &&
      currentSelection.page === nextSelection.page &&
      currentSelection.pageSize === nextSelection.pageSize
    ) {
      setTransitionPhase("idle");
      return undefined;
    }
    if (reducedMotion) {
      displaySelectionRef.current = nextSelection;
      setDisplaySelection(nextSelection);
      setTransitionPhase("idle");
      return undefined;
    }

    setTransitionPhase("out");
    let enterFrame = 0;
    let settleFrame = 0;
    const switchTimer = window.setTimeout(() => {
      displaySelectionRef.current = nextSelection;
      setDisplaySelection(nextSelection);
      setTransitionPhase("enter");
      enterFrame = requestFrame(() => {
        settleFrame = requestFrame(() => {
          setTransitionPhase("idle");
        });
      });
    }, 100);

    return () => {
      window.clearTimeout(switchTimer);
      if (enterFrame) cancelFrame(enterFrame);
      if (settleFrame) cancelFrame(settleFrame);
    };
  }, [currentPage, filter, pageSize, reducedMotion]);

  const displayedJobs = React.useMemo(
    () => annotationJobsForFilter(jobs, displaySelection.filter),
    [displaySelection.filter, jobs],
  );
  const displayedPageCount = Math.max(
    1,
    Math.ceil(displayedJobs.length / displaySelection.pageSize),
  );
  const safeDisplayedPage = Math.min(displaySelection.page, displayedPageCount);
  const firstDisplayedIndex = (safeDisplayedPage - 1) * displaySelection.pageSize;
  const displayedPageJobs = displayedJobs.slice(
    firstDisplayedIndex,
    firstDisplayedIndex + displaySelection.pageSize,
  );
  const activeFilterConfig =
    ANNOTATION_JOB_LIST_FILTERS.find((item) => item.value === filter) ??
    ANNOTATION_JOB_LIST_FILTERS[0];
  const isInitialLoading = loading && jobs.length === 0;
  const dataPilotUnavailable = capability ? !capability.available : false;

  const selectFilter = React.useCallback((nextFilter: AnnotationJobListFilter) => {
    setFilter(nextFilter);
    setPage(1);
  }, []);

  const handleFilterKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
      let nextIndex: number | null = null;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        nextIndex = (currentIndex + 1) % ANNOTATION_JOB_LIST_FILTERS.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        nextIndex =
          (currentIndex - 1 + ANNOTATION_JOB_LIST_FILTERS.length) %
          ANNOTATION_JOB_LIST_FILTERS.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = ANNOTATION_JOB_LIST_FILTERS.length - 1;
      }
      if (nextIndex === null) return;

      event.preventDefault();
      const nextFilter = ANNOTATION_JOB_LIST_FILTERS[nextIndex].value;
      selectFilter(nextFilter);
      requestFrame(() => {
        filterTabsRef.current
          ?.querySelector<HTMLButtonElement>(`[data-filter="${nextFilter}"]`)
          ?.focus();
      });
    },
    [selectFilter],
  );

  const selectedSummary = React.useMemo(() => {
    const latest = selectedJobs[0]?.updated_at
      ? formatAnnotationJobUpdatedAt(selectedJobs[0].updated_at)
      : "暂无更新";
    if (filter === "waiting") {
      return `${selectedJobs.length} 个任务 · ${metrics.waitingSegments} 个 Segment 待处理 · 最近更新 ${latest}`;
    }
    if (filter === "running") {
      return `${selectedJobs.length} 个任务 · 覆盖准备、Tracking 与后处理阶段 · 最近更新 ${latest}`;
    }
    if (filter === "error") {
      return `${selectedJobs.length} 个任务 · ${metrics.failedJobs} 个异常待处理 · 最近更新 ${latest}`;
    }
    return `${selectedJobs.length} 条记录 · 已完成或取消 · 最近更新 ${latest}`;
  }, [filter, metrics.failedJobs, metrics.waitingSegments, selectedJobs]);

  const pageHeader = (
    <AnnotationListPageHeader
      headingId="annotation-jobs-heading"
      title="标注任务"
      description="导航数据首帧标注、自动处理与任务状态跟踪。"
      actions={
        <>
          <Button
            type="button"
            variant="outline"
            onClick={onRefresh}
            disabled={loading || refreshing}
            aria-label={refreshing ? "正在刷新标注任务" : "刷新标注任务"}
            className="bg-white text-slate-700 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700 active:translate-y-px"
          >
            <RefreshCw
              aria-hidden="true"
              className={cn("size-4", refreshing && "animate-spin motion-reduce:animate-none")}
            />
            {refreshing ? "刷新中" : "刷新"}
          </Button>
          <Button
            type="button"
            onClick={onOpenDataPilot}
            disabled={dataPilotDisabled}
            className="bg-[#274bc8] px-3 text-white shadow-sm hover:bg-[#203fae] active:translate-y-px"
          >
            <Bot aria-hidden="true" />
            交给 DataPilot 处理
          </Button>
        </>
      }
    />
  );

  if (isInitialLoading) {
    return (
      <section aria-labelledby="annotation-jobs-heading" className={cn("space-y-4", className)}>
        {pageHeader}
        <LoadingView />
      </section>
    );
  }

  return (
    <section
      aria-labelledby="annotation-jobs-heading"
      aria-busy={refreshing || undefined}
      className={cn("space-y-4", className)}
    >
      {pageHeader}

      {error ? (
        <Alert variant="destructive" className="border-rose-200 bg-rose-50/80">
          <AlertCircle aria-hidden="true" />
          <AlertTitle>部分数据加载失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {dataPilotUnavailable ? (
        <Alert className="border-amber-200 bg-amber-50/80 text-amber-950">
          <CircleAlert aria-hidden="true" />
          <AlertTitle>当前处理环境尚未通过预检</AlertTitle>
          <AlertDescription>
            <span className="block">
              {capability?.reason?.message || "当前运行环境未提供自动标注能力。"}
            </span>
            <span className="mt-0.5 block">
              仍可提交数据范围，由 DataPilot 检查事实并说明阻塞。
            </span>
          </AlertDescription>
        </Alert>
      ) : null}

      <div
        className="grid grid-cols-2 border-y border-slate-200 bg-white xl:grid-cols-4"
        data-testid="annotation-metrics-strip"
      >
        <LightweightMetric
          label="待首帧标注"
          value={metrics.waitingSegments}
          index={0}
        />
        <LightweightMetric
          label="运行中"
          value={metrics.runningJobs}
          index={1}
        />
        <LightweightMetric
          label="异常任务"
          value={metrics.failedJobs}
          index={2}
        />
        <LightweightMetric
          label="已标注"
          value={metrics.annotatedJobs}
          index={3}
        />
      </div>

      <section
        className="min-h-[31rem] border-y border-slate-200 bg-white"
        data-testid="annotation-task-surface"
      >
        <div className="console-soft-scrollbar overflow-x-auto border-b border-slate-200 px-4 sm:px-5">
          <div
            ref={filterTabsRef}
            role="tablist"
            aria-label="标注任务筛选"
            className="flex min-w-max items-center gap-6"
          >
            {ANNOTATION_JOB_LIST_FILTERS.map((item, index) => {
              const selected = item.value === filter;
              return (
                <button
                  key={item.value}
                  type="button"
                  role="tab"
                  id={`annotation-job-filter-${item.value}`}
                  data-filter={item.value}
                  aria-label={item.label}
                  aria-selected={selected}
                  aria-controls="annotation-jobs-panel"
                  tabIndex={selected ? 0 : -1}
                  onClick={() => selectFilter(item.value)}
                  onKeyDown={(event) => handleFilterKeyDown(event, index)}
                  className={cn(
                    "relative inline-flex min-h-11 items-center justify-center px-1 text-sm font-medium outline-none transition-[color,opacity,transform] duration-150 after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:origin-center after:bg-[#3157cf] after:transition-transform after:duration-[180ms] focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 active:translate-y-px motion-reduce:transition-none motion-reduce:after:transition-none",
                    selected
                      ? "text-[#3157cf] after:scale-x-100"
                      : "text-slate-500 after:scale-x-0 hover:text-slate-800",
                  )}
                >
                  <span>{item.label}</span>
                  <span className="sr-only">，{categoryCounts[item.value]} 项</span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="px-4 py-5 sm:px-5">
          <p className="font-mono text-[10px] font-medium tracking-[0.18em] text-slate-400">
            ANNOTATION QUEUE / {String(Math.max(1, categoryCounts[filter])).padStart(2, "0")}
          </p>
          <ScrambleTitle
            as="h3"
            text={activeFilterConfig.title}
            className="mt-1 min-h-7 text-xl font-semibold text-slate-950"
          />
          <p className="mt-1 text-xs text-slate-500">{selectedSummary}</p>
        </div>

          <div
            id="annotation-jobs-panel"
            role="tabpanel"
            aria-labelledby={`annotation-job-filter-${filter}`}
            aria-busy={transitionPhase !== "idle" || undefined}
            tabIndex={0}
            className="min-h-[22rem]"
          >
            <Table
              containerAriaLabel="标注任务列表"
              containerTabIndex={0}
              className="min-w-[900px]"
            >
              <TableHeader>
                <TableRow className="border-slate-200 bg-transparent hover:bg-transparent">
                  <TableHead className="w-[13%] px-5 text-xs text-slate-500">数据日期</TableHead>
                  <TableHead className="w-[15%] text-xs text-slate-500">状态</TableHead>
                  <TableHead className="w-[20%] text-xs text-slate-500">Segment 进度</TableHead>
                  <TableHead className="w-[18%] text-xs text-slate-500">处理标定</TableHead>
                  <TableHead className="w-[15%] text-xs text-slate-500">更新时间</TableHead>
                  <TableHead className="w-[13%] text-xs text-slate-500">操作</TableHead>
                  <TableHead className="w-[6%] pr-5 text-right text-xs text-slate-500">详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody aria-hidden={transitionPhase !== "idle" || undefined}>
                {displayedPageJobs.length > 0 ? (
                  displayedPageJobs.map((job, index) => {
                    const progress = annotationJobTableProgress(job);
                    return (
                      <TableRow
                        key={job.job_ref}
                        className={cn(
                          "h-[68px] border-slate-100 transition-[opacity,transform,background-color] duration-150 ease-out hover:bg-blue-50/35 motion-reduce:transition-none",
                          transitionPhase === "out" && "translate-y-1 opacity-0",
                          transitionPhase === "enter" && "-translate-y-1 opacity-0",
                          transitionPhase === "idle" && "translate-y-0 opacity-100",
                        )}
                        style={
                          reducedMotion
                            ? undefined
                            : { transitionDelay: `${Math.min(index, 7) * 22}ms` }
                        }
                      >
                        <TableCell className="px-5">
                          <span className="font-medium tabular-nums text-slate-900">
                            {job.dataset_date}
                          </span>
                        </TableCell>
                        <TableCell><StatusBadge job={job} /></TableCell>
                        <TableCell>
                          <div className="w-36 max-w-full space-y-1.5">
                            <div className="flex items-center justify-between gap-2 text-xs">
                              <span className="text-slate-500">{progress.stageLabel}</span>
                              <span className="font-medium tabular-nums text-slate-700">
                                {progress.resolved}/{progress.total}
                              </span>
                            </div>
                            <Progress
                              value={progress.value}
                              aria-label={`${job.dataset_date} ${progress.stageLabel}`}
                              aria-valuetext={`${progress.resolved} / ${progress.total}`}
                              className="h-1.5 bg-slate-100"
                              indicatorClassName="bg-[#3157cf]"
                            />
                          </div>
                        </TableCell>
                        <TableCell>
                          <span
                            className="block max-w-[12rem] truncate text-slate-600"
                            title={job.calibration?.label || "未提供"}
                          >
                            {job.calibration?.label || "未提供"}
                          </span>
                        </TableCell>
                        <TableCell className="tabular-nums text-slate-500">
                          {formatAnnotationJobUpdatedAt(job.updated_at)}
                        </TableCell>
                        <TableCell>
                          <Button
                            type="button"
                            variant="link"
                            size="sm"
                            onClick={() => onPrimaryAction(job)}
                            className="h-7 px-0 text-blue-700 active:translate-y-px"
                          >
                            <Eye aria-hidden="true" />
                            {annotationJobPrimaryActionLabel(job)}
                          </Button>
                        </TableCell>
                        <TableCell className="pr-5 text-right">
                          <JobDetailsPopover job={job} />
                        </TableCell>
                      </TableRow>
                    );
                  })
                ) : (
                  <TableRow className="h-[19rem] border-0 hover:bg-transparent">
                    <TableCell colSpan={7} className="whitespace-normal px-6 text-center">
                      <div className="mx-auto max-w-sm">
                        <div className="mx-auto grid size-11 place-items-center rounded-xl border border-slate-200 bg-slate-50 text-slate-400">
                          <FileStack className="size-5" aria-hidden="true" />
                        </div>
                        <p className="mt-3 font-medium text-slate-800">
                          {EMPTY_COPY[displaySelection.filter].title}
                        </p>
                        <p className="mt-1 text-sm leading-6 text-slate-500">
                          {EMPTY_COPY[displaySelection.filter].description}
                        </p>
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>

          <div className="flex flex-col gap-3 border-t border-slate-100 px-4 py-3 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <div className="flex items-center gap-2">
              <span>每页</span>
              <Select
                value={String(pageSize)}
                onValueChange={(value) => {
                  setPageSize(Number(value));
                  setPage(1);
                }}
              >
                <SelectTrigger size="sm" aria-label="每页任务数量" className="w-[72px] bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  {PAGE_SIZE_OPTIONS.map((option) => (
                    <SelectItem key={option} value={String(option)}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span>共 {selectedJobs.length} 条</span>
            </div>

            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="icon-sm"
                aria-label="上一页"
                disabled={currentPage <= 1 || transitionPhase !== "idle"}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                className="bg-white active:translate-y-px"
              >
                <ArrowLeft aria-hidden="true" />
              </Button>
              <span className="min-w-20 text-center tabular-nums text-slate-600">
                第 {currentPage} / {pageCount} 页
              </span>
              <Button
                type="button"
                variant="outline"
                size="icon-sm"
                aria-label="下一页"
                disabled={currentPage >= pageCount || transitionPhase !== "idle"}
                onClick={() => setPage((current) => Math.min(pageCount, current + 1))}
                className="bg-white active:translate-y-px"
              >
                <ArrowRight aria-hidden="true" />
              </Button>
            </div>
          </div>
      </section>

      {refreshing ? (
        <p className="sr-only" role="status">正在刷新标注任务列表</p>
      ) : null}
    </section>
  );
}
