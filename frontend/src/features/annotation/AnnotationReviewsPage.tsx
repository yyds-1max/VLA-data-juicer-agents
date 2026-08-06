import * as React from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CalendarDays,
  ClipboardCheck,
  Eye,
  Info,
  LoaderCircle,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useStore } from "zustand";

import { Button } from "../../components/ui/button";
import { CircularProgress } from "../../components/ui/circular-progress";
import { Input } from "../../components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "../../components/ui/popover";
import { Progress } from "../../components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { cn } from "../../lib/utils";
import { AnnotationListPageHeader } from "./AnnotationListPageHeader";
import { AnnotationApiError } from "./api";
import {
  annotationProjectionStore,
  loadTrajectoryReviews,
} from "./projectionStore";
import {
  buildReviewGroups,
  filterReviewGroups,
  reviewListMetrics,
  type ReviewDateRange,
  type ReviewGroupPresentation,
  type ReviewStatusFilter,
} from "./reviewListPresentation";
import type { ReviewStatusTone } from "./reviewPresentation";
import type { TrajectoryReviewStatus } from "./types";

const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;
const EMPTY_DATE_RANGE: ReviewDateRange = { from: "", to: "" };

const REVIEW_FILTER_LABELS: Record<TrajectoryReviewStatus, string> = {
  pending: "待复核",
  in_progress: "修正中",
  returned: "已退回",
  approved: "已批准（全部）",
  discarded: "已废弃",
};

type DisplaySelection = {
  status: ReviewStatusFilter;
  query: string;
  dateFrom: string;
  dateTo: string;
  page: number;
  pageSize: number;
};

type TransitionPhase = "idle" | "out" | "enter";

function safeReviewError(error: unknown, fallback: string): string {
  if (error instanceof AnnotationApiError) {
    return error.detail?.code ? `${fallback}（${error.detail.code}）` : fallback;
  }
  const message = error instanceof Error ? error.message : "";
  return /(?:^|[\s("'`])\/(?:[^/\s]+\/){2,}|[A-Za-z]:\\/.test(message)
    ? fallback
    : message || fallback;
}

function updatedTime(timestamp: string): string {
  const value = new Date(timestamp);
  if (Number.isNaN(value.valueOf())) return timestamp || "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(value);
}

function requestFrame(callback: FrameRequestCallback): number {
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(() => callback(performance.now()), 16);
}

function cancelFrame(handle: number): void {
  if (
    typeof window.requestAnimationFrame === "function"
    && typeof window.cancelAnimationFrame === "function"
  ) {
    window.cancelAnimationFrame(handle);
    return;
  }
  window.clearTimeout(handle);
}

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

function sameSelection(left: DisplaySelection, right: DisplaySelection): boolean {
  return left.status === right.status
    && left.query === right.query
    && left.dateFrom === right.dateFrom
    && left.dateTo === right.dateTo
    && left.page === right.page
    && left.pageSize === right.pageSize;
}

function toneDot(tone: ReviewStatusTone): string {
  return cn(
    tone === "neutral" && "bg-slate-400",
    tone === "info" && "bg-blue-500",
    tone === "warning" && "bg-amber-500",
    tone === "success" && "bg-emerald-500",
    tone === "danger" && "bg-rose-500",
  );
}

function StatusBadge({ status }: { status: ReviewGroupPresentation["status"] }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs font-medium text-slate-600">
      <span aria-hidden="true" className={cn("size-1.5 rounded-full", toneDot(status.tone))} />
      {status.label}
    </span>
  );
}

function toDateInputValue(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function recentDateRange(dayCount: number): ReviewDateRange {
  const to = new Date();
  const from = new Date(to);
  from.setDate(to.getDate() - Math.max(0, dayCount - 1));
  return { from: toDateInputValue(from), to: toDateInputValue(to) };
}

function compactDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[2]}/${match[3]}` : value;
}

function DateRangePopover({
  value,
  onChange,
}: {
  value: ReviewDateRange;
  onChange: (value: ReviewDateRange) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const [draft, setDraft] = React.useState<ReviewDateRange>(value);
  const partial = Boolean(draft.from) !== Boolean(draft.to);
  const reversed = Boolean(draft.from && draft.to && draft.from > draft.to);
  const invalid = partial || reversed;
  const hasRange = Boolean(value.from && value.to);
  const label = hasRange
    ? `${compactDate(value.from)}–${compactDate(value.to)}`
    : "全部日期";

  return (
    <div className="flex w-full items-center lg:w-auto">
      <Popover
        open={open}
        onOpenChange={(nextOpen) => {
          if (nextOpen) setDraft(value);
          setOpen(nextOpen);
        }}
      >
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            aria-label={`复核日期范围：${label}`}
            className={cn(
              "h-10 w-full justify-start rounded-r-none bg-white px-3 text-slate-600 lg:w-58",
              !hasRange && "rounded-r-lg",
            )}
          >
            <CalendarDays aria-hidden="true" />
            <span className="truncate">{label}</span>
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="end"
          sideOffset={8}
          className="w-[min(22rem,calc(100vw-2rem))] border border-slate-200 p-4"
        >
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-slate-950">数据日期范围</p>
              <p className="mt-1 text-xs text-slate-500">开始和结束日期均包含在筛选范围内。</p>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setDraft(recentDateRange(7))}
              className="text-blue-700"
            >
              最近 7 天
            </Button>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <label className="space-y-1.5 text-xs font-medium text-slate-600">
              <span>开始日期</span>
              <Input
                type="date"
                aria-label="复核开始日期"
                value={draft.from}
                max={draft.to || undefined}
                onChange={(event) => setDraft((current) => ({
                  ...current,
                  from: event.target.value,
                }))}
                className="h-9 bg-white text-xs"
              />
            </label>
            <label className="space-y-1.5 text-xs font-medium text-slate-600">
              <span>结束日期</span>
              <Input
                type="date"
                aria-label="复核结束日期"
                value={draft.to}
                min={draft.from || undefined}
                onChange={(event) => setDraft((current) => ({
                  ...current,
                  to: event.target.value,
                }))}
                className="h-9 bg-white text-xs"
              />
            </label>
          </div>
          {invalid ? (
            <p role="alert" className="mt-2 text-xs text-rose-600">
              {partial ? "请选择完整的开始和结束日期。" : "结束日期不能早于开始日期。"}
            </p>
          ) : null}
          <div className="mt-4 flex items-center justify-between gap-3">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDraft(EMPTY_DATE_RANGE)}
              className="text-slate-500"
            >
              清除
            </Button>
            <Button
              type="button"
              disabled={invalid}
              onClick={() => {
                onChange(draft);
                setOpen(false);
              }}
            >
              应用日期范围
            </Button>
          </div>
        </PopoverContent>
      </Popover>
      {hasRange ? (
        <Button
          type="button"
          variant="outline"
          size="icon-lg"
          aria-label="清除复核日期范围"
          onClick={() => onChange(EMPTY_DATE_RANGE)}
          className="h-10 rounded-l-none border-l-0 bg-white text-slate-500"
        >
          <X aria-hidden="true" />
        </Button>
      ) : null}
    </div>
  );
}

function ReviewDetailsPopover({ group }: { group: ReviewGroupPresentation }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          aria-label={`查看 ${group.datasetDate} ${group.sourceClips.join("、")} 复核详情`}
          className="h-7 px-2 text-xs text-slate-500 hover:bg-blue-50 hover:text-blue-700 active:translate-y-px"
        >
          <Info aria-hidden="true" />
          详情
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={8}
        className="w-[min(22rem,calc(100vw-2rem))] rounded-xl border-slate-200 p-0 shadow-xl shadow-slate-950/8"
      >
        <div className="border-b border-slate-100 px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="font-medium tabular-nums text-slate-950">{group.datasetDate}</p>
              <p className="mt-0.5 text-xs text-slate-500">
                {group.progress.total} 个复核单元
              </p>
            </div>
            <StatusBadge status={group.status} />
          </div>
        </div>

        <div className="space-y-4 px-4 py-4">
          <div className="flex items-center gap-4">
            <div className="flex shrink-0 flex-col items-center gap-1">
              <CircularProgress
                value={group.progress.value}
                aria-label={`轨迹复核进度，${group.progress.resolved} / ${group.progress.total}`}
                centerLabel={`${group.progress.resolved}/${group.progress.total}`}
                className="size-[76px] text-[#3157cf]"
                trackClassName="text-[#e8edf8]"
                indicatorClassName="text-[#3157cf]"
              />
              <span className="text-[10px] text-slate-500">轨迹复核进度</span>
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-[11px] font-medium text-slate-500">状态分布</p>
              <dl className="mt-2 space-y-1.5 text-xs">
                {group.statusBreakdown.map((item) => (
                  <div key={item.key} className="flex items-center justify-between gap-3">
                    <dt className="flex min-w-0 items-center gap-2 text-slate-500">
                      <span
                        aria-hidden="true"
                        className={cn("size-1.5 shrink-0 rounded-full", toneDot(item.presentation.tone))}
                      />
                      <span className="truncate">{item.presentation.label}</span>
                    </dt>
                    <dd className="font-medium tabular-nums text-slate-800">{item.count}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </div>

          <dl className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-3 border-t border-slate-100 pt-4 text-xs">
            <dt className="text-slate-500">使用修整标定</dt>
            <dd className="min-w-0 space-y-1 text-right text-slate-800">
              {group.calibrationBreakdown.map((item) => (
                <span key={item.profileRef ?? "unselected"} className="block">
                  <span className="break-words" title={item.label}>{item.label}</span>
                  {group.progress.total > 1 ? (
                    <span className="ml-1 text-slate-400">· {item.count} 个</span>
                  ) : null}
                </span>
              ))}
            </dd>
            <dt className="text-slate-500">最后更新时间</dt>
            <dd className="text-right tabular-nums text-slate-800">
              {updatedTime(group.updatedAt)}
            </dd>
            <dt className="text-slate-500">外层 clips</dt>
            <dd className="min-w-0 text-right text-slate-800">
              <span
                className="block max-h-24 space-y-1 overflow-y-auto overscroll-contain pr-1"
                tabIndex={group.sourceClips.length > 3 ? 0 : undefined}
                aria-label="全部外层 clips"
              >
                {group.sourceClips.map((clip) => (
                  <span key={clip} className="block truncate" title={clip}>{clip}</span>
                ))}
              </span>
            </dd>
          </dl>
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function AnnotationReviewsPage() {
  const navigate = useNavigate();
  const reviews = useStore(annotationProjectionStore, (state) => state.reviews);
  const reviewsLoaded = useStore(
    annotationProjectionStore,
    (state) => state.reviewsLoaded,
  );
  const reducedMotion = usePrefersReducedMotion();
  const [statusFilter, setStatusFilter] = React.useState<ReviewStatusFilter>("active");
  const [dateRange, setDateRange] = React.useState<ReviewDateRange>(EMPTY_DATE_RANGE);
  const [query, setQuery] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(10);
  const [loading, setLoading] = React.useState(!reviewsLoaded);
  const [error, setError] = React.useState("");
  const [displaySelection, setDisplaySelection] = React.useState<DisplaySelection>({
    status: "active",
    query: "",
    dateFrom: "",
    dateTo: "",
    page: 1,
    pageSize: 10,
  });
  const displaySelectionRef = React.useRef(displaySelection);
  const [transitionPhase, setTransitionPhase] = React.useState<TransitionPhase>("idle");

  const refresh = React.useCallback(async (force = false, silent = false) => {
    if (!silent) setLoading(true);
    try {
      await loadTrajectoryReviews({ force });
      setError("");
    } catch (requestError) {
      setError(safeReviewError(requestError, "读取轨迹复核任务失败"));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void refresh(false);
  }, [refresh]);

  const metrics = React.useMemo(() => reviewListMetrics(reviews), [reviews]);
  const allGroups = React.useMemo(() => buildReviewGroups(reviews), [reviews]);
  const selectedGroups = React.useMemo(() => filterReviewGroups(allGroups, {
    status: statusFilter,
    query,
    dateRange,
  }), [allGroups, dateRange, query, statusFilter]);
  const pageCount = Math.max(1, Math.ceil(selectedGroups.length / pageSize));
  const currentPage = Math.min(page, pageCount);

  React.useEffect(() => {
    if (page !== currentPage) setPage(currentPage);
  }, [currentPage, page]);

  React.useEffect(() => {
    const nextSelection: DisplaySelection = {
      status: statusFilter,
      query,
      dateFrom: dateRange.from,
      dateTo: dateRange.to,
      page: currentPage,
      pageSize,
    };
    if (sameSelection(displaySelectionRef.current, nextSelection)) {
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
        settleFrame = requestFrame(() => setTransitionPhase("idle"));
      });
    }, 100);

    return () => {
      window.clearTimeout(switchTimer);
      if (enterFrame) cancelFrame(enterFrame);
      if (settleFrame) cancelFrame(settleFrame);
    };
  }, [currentPage, dateRange.from, dateRange.to, pageSize, query, reducedMotion, statusFilter]);

  const displayedGroups = React.useMemo(() => filterReviewGroups(allGroups, {
    status: displaySelection.status,
    query: displaySelection.query,
    dateRange: {
      from: displaySelection.dateFrom,
      to: displaySelection.dateTo,
    },
  }), [allGroups, displaySelection]);
  const displayedPageCount = Math.max(
    1,
    Math.ceil(displayedGroups.length / displaySelection.pageSize),
  );
  const safeDisplayedPage = Math.min(displaySelection.page, displayedPageCount);
  const firstDisplayedIndex = (safeDisplayedPage - 1) * displaySelection.pageSize;
  const displayedPageGroups = displayedGroups.slice(
    firstDisplayedIndex,
    firstDisplayedIndex + displaySelection.pageSize,
  );
  const initialLoading = loading && reviews.length === 0;

  const selectStatus = React.useCallback((nextStatus: ReviewStatusFilter) => {
    setStatusFilter(nextStatus);
    setPage(1);
  }, []);

  return (
    <section
      aria-labelledby="annotation-reviews-heading"
      aria-busy={loading || transitionPhase !== "idle" || undefined}
      className="mx-auto max-w-360 space-y-4 px-3 pb-28 pt-2 md:px-4 lg:px-5"
    >
      <AnnotationListPageHeader
        headingId="annotation-reviews-heading"
        title="人工复核"
        description="对后处理生成的三维轨迹进行人工 Fix、提交和最终审批。"
        actions={
          <Button
            type="button"
            variant="outline"
            disabled={loading}
            aria-label={loading ? "正在刷新轨迹复核任务" : "刷新轨迹复核任务"}
            onClick={() => void refresh(true)}
            className="bg-white text-slate-700 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700"
          >
            <RefreshCw
              aria-hidden="true"
              className={cn(loading && "animate-spin motion-reduce:animate-none")}
            />
            {loading ? "刷新中" : "刷新"}
          </Button>
        }
      />

      <div
        className="grid grid-cols-2 border-y border-slate-200 bg-white lg:grid-cols-5"
        data-testid="review-metrics-strip"
      >
        {[
          { filter: "pending" as const, label: "待复核", count: metrics.pending },
          { filter: "in_progress" as const, label: "修正中", count: metrics.inProgress },
          { filter: "returned" as const, label: "已退回", count: metrics.returned },
          { filter: "verified" as const, label: "已验证", count: metrics.verified },
          { filter: "discarded" as const, label: "已废弃", count: metrics.discarded },
        ].map((item, index) => (
          <button
            key={item.filter}
            type="button"
            aria-label={`${item.label} ${item.count}`}
            aria-pressed={statusFilter === item.filter}
            onClick={() => selectStatus(item.filter)}
            className={cn(
              "min-h-22 px-4 py-4 text-left transition-colors hover:bg-blue-50/45 focus-visible:relative focus-visible:z-10 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-blue-500 motion-reduce:transition-none sm:px-5",
              index < 4 && "border-b border-slate-200 lg:border-b-0",
              index % 2 === 0 && index < 4 && "border-r border-slate-200",
              index < 4 && "lg:border-r lg:border-slate-200",
              statusFilter === item.filter && "bg-blue-50/65",
            )}
          >
            <span className="block text-xs font-medium text-slate-500">{item.label}</span>
            <span className="mt-2 block text-2xl font-semibold tabular-nums tracking-tight text-slate-950">
              {item.count.toLocaleString("zh-CN")}
            </span>
          </button>
        ))}
      </div>

      <section
        className="min-h-[31rem] border-y border-slate-200 bg-white"
        data-testid="review-task-surface"
      >
        <div className="flex flex-col gap-3 border-b border-slate-200 px-4 py-4 lg:flex-row lg:items-center sm:px-5">
          <label className="relative min-w-0 flex-1 lg:max-w-sm">
            <span className="sr-only">搜索日期或外层 clip</span>
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400"
            />
            <Input
              aria-label="搜索复核任务"
              className="h-10 bg-white pl-9 shadow-none"
              placeholder="搜索日期或外层 clip"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setPage(1);
              }}
            />
          </label>
          <Select value={statusFilter} onValueChange={(value) => selectStatus(value as ReviewStatusFilter)}>
            <SelectTrigger aria-label="复核状态筛选" className="h-10 w-full bg-white lg:w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="active">待我处理</SelectItem>
              <SelectItem value="history">历史结果</SelectItem>
              <SelectItem value="all">全部状态</SelectItem>
              <SelectItem value="verified">已验证</SelectItem>
              {(Object.keys(REVIEW_FILTER_LABELS) as TrajectoryReviewStatus[]).map((status) => (
                <SelectItem key={status} value={status}>{REVIEW_FILTER_LABELS[status]}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <DateRangePopover
            value={dateRange}
            onChange={(nextRange) => {
              setDateRange(nextRange);
              setPage(1);
            }}
          />
        </div>

        {error ? (
          <div role="alert" className="m-4 flex gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
            <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            {error}
          </div>
        ) : null}

        <div
          role="region"
          aria-label="轨迹复核任务列表"
          tabIndex={0}
          className="min-h-[22rem] overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
        >
          {initialLoading ? (
            <div role="status" className="flex min-h-[22rem] items-center justify-center gap-2 text-sm text-slate-500">
              <LoaderCircle aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
              正在读取复核队列…
            </div>
          ) : (
            <Table className="min-w-[860px]">
              <TableHeader>
                <TableRow className="border-slate-200 bg-transparent hover:bg-transparent">
                  <TableHead className="w-[15%] px-5 text-xs text-slate-500">数据日期</TableHead>
                  <TableHead className="w-[25%] text-xs text-slate-500">内部复核进度</TableHead>
                  <TableHead className="w-[20%] text-xs text-slate-500">状态</TableHead>
                  <TableHead className="w-[20%] text-xs text-slate-500">更新时间</TableHead>
                  <TableHead className="w-[14%] text-xs text-slate-500">操作</TableHead>
                  <TableHead className="w-[6%] pr-5 text-right text-xs text-slate-500">详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody aria-hidden={transitionPhase !== "idle" || undefined}>
                {displayedPageGroups.length > 0 ? (
                  displayedPageGroups.map((group, index) => (
                    <TableRow
                      key={group.key}
                      className={cn(
                        "h-[68px] border-slate-100 transition-[opacity,transform,background-color] duration-150 ease-out hover:bg-blue-50/35 motion-reduce:transition-none",
                        transitionPhase === "out" && "translate-y-1 opacity-0",
                        transitionPhase === "enter" && "-translate-y-1 opacity-0",
                        transitionPhase === "idle" && "translate-y-0 opacity-100",
                      )}
                      style={reducedMotion ? undefined : {
                        transitionDelay: `${Math.min(index, 7) * 22}ms`,
                      }}
                    >
                      <TableCell className="px-5 font-medium tabular-nums text-slate-900">
                        {group.datasetDate}
                      </TableCell>
                      <TableCell>
                        <div className="w-44 max-w-full space-y-1.5">
                          <div className="flex items-center justify-between gap-2 text-xs">
                            <span className="text-slate-500">轨迹复核完成</span>
                            <span className="font-medium tabular-nums text-slate-700">
                              {group.progress.resolved}/{group.progress.total}
                            </span>
                          </div>
                          <Progress
                            value={group.progress.value}
                            aria-label={`${group.datasetDate} 轨迹复核进度`}
                            aria-valuetext={`${group.progress.resolved} / ${group.progress.total}`}
                            className="h-1.5 bg-slate-100"
                            indicatorClassName="bg-[#3157cf]"
                          />
                        </div>
                      </TableCell>
                      <TableCell><StatusBadge status={group.status} /></TableCell>
                      <TableCell className="text-xs tabular-nums text-slate-500">
                        {updatedTime(group.updatedAt)}
                      </TableCell>
                      <TableCell>
                        <Button
                          type="button"
                          variant="link"
                          size="sm"
                          onClick={() => navigate(
                            `/annotation/reviews/${encodeURIComponent(group.actionableReview.review_ref)}`,
                          )}
                          className="h-7 px-0 text-blue-700 active:translate-y-px"
                        >
                          <Eye aria-hidden="true" />
                          {group.actionableReview.status === "approved"
                            || group.actionableReview.status === "discarded"
                            ? "查看记录"
                            : "进入人工 Fix"}
                        </Button>
                      </TableCell>
                      <TableCell className="pr-5 text-right">
                        <ReviewDetailsPopover group={group} />
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow className="h-[19rem] border-0 hover:bg-transparent">
                    <TableCell colSpan={6} className="whitespace-normal px-6 text-center">
                      <div className="mx-auto max-w-sm">
                        <div className="mx-auto grid size-11 place-items-center rounded-xl border border-slate-200 bg-slate-50 text-slate-400">
                          <ClipboardCheck aria-hidden="true" className="size-5" />
                        </div>
                        <p className="mt-3 font-medium text-slate-800">没有符合条件的复核任务</p>
                        <p className="mt-1 text-sm leading-6 text-slate-500">
                          后处理完成后，轨迹复核会自动出现在这里。
                        </p>
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
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
              <SelectTrigger size="sm" aria-label="每页复核任务数量" className="w-[72px] bg-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                {PAGE_SIZE_OPTIONS.map((option) => (
                  <SelectItem key={option} value={String(option)}>{option}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span>共 {selectedGroups.length} 条</span>
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

      {loading && reviews.length > 0 ? (
        <p className="sr-only" role="status">正在刷新轨迹复核任务列表</p>
      ) : null}
    </section>
  );
}
