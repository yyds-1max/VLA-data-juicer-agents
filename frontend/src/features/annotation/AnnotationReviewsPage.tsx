import {
  AlertCircle,
  CheckCircle2,
  ClipboardCheck,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { StatusTag } from "../../components/console/StatusTag";
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
import { AnnotationApiError, listTrajectoryReviews } from "./api";
import { useAnnotationEvents } from "./events";
import {
  isVerifiedReview,
  trajectoryReviewPresentation,
  type ReviewPresentation,
  type ReviewPresentationKey,
} from "./reviewPresentation";
import type {
  TrajectoryReview,
  TrajectoryReviewStatus,
} from "./types";

const REVIEW_FILTER_LABELS: Record<TrajectoryReviewStatus, string> = {
  pending: "待复核",
  in_progress: "修正中",
  returned: "已退回",
  approved: "已批准（全部）",
  discarded: "已废弃",
};

const PRESENTATION_ORDER: ReviewPresentationKey[] = [
  "pending",
  "in_progress",
  "returned",
  "approved_waiting",
  "approved_publishing",
  "approved_failed",
  "verified",
  "discarded",
];

type StatusFilter =
  | "active"
  | "history"
  | "all"
  | "verified"
  | TrajectoryReviewStatus;

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
  if (Number.isNaN(value.valueOf())) return timestamp;
  return value.toLocaleString("zh-CN", { hour12: false });
}

function matchesStatus(review: TrajectoryReview, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "verified") return isVerifiedReview(review);
  if (filter === "active") {
    return review.status === "pending"
      || review.status === "in_progress"
      || review.status === "returned"
      || trajectoryReviewPresentation(review).key === "approved_failed";
  }
  if (filter === "history") {
    return review.status === "approved" || review.status === "discarded";
  }
  return review.status === filter;
}

export function AnnotationReviewsPage() {
  const navigate = useNavigate();
  const [reviews, setReviews] = useState<TrajectoryReview[]>([]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [dateFilter, setDateFilter] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setReviews(await listTrajectoryReviews());
      setError("");
    } catch (requestError) {
      setError(safeReviewError(requestError, "读取轨迹复核任务失败"));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useAnnotationEvents({
    filter: (event) => event.aggregate_kind === "review",
    onEvent: () => refresh(true),
    onReconcile: () => refresh(true),
  });

  const counts = useMemo(() => {
    const next = {
      pending: 0,
      in_progress: 0,
      returned: 0,
      verified: 0,
      discarded: 0,
    };
    reviews.forEach((review) => {
      if (review.status === "approved") {
        if (isVerifiedReview(review)) next.verified += 1;
        return;
      }
      next[review.status] += 1;
    });
    return next;
  }, [reviews]);

  const groups = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const grouped = new Map<string, {
      datasetDate: string;
      sourceClip: string;
      reviews: TrajectoryReview[];
      updatedAt: string;
    }>();
    reviews
      .filter((review) => matchesStatus(review, statusFilter))
      .filter((review) => !dateFilter || review.dataset_date === dateFilter.replace(/-/g, ""))
      .filter((review) => (
        !normalizedQuery
        || review.source_clip.toLocaleLowerCase().includes(normalizedQuery)
        || review.dataset_date.includes(normalizedQuery)
      ))
      .forEach((review) => {
        const key = `${review.dataset_date}:${review.source_clip}`;
        const current = grouped.get(key);
        if (current) {
          current.reviews.push(review);
          if (review.updated_at > current.updatedAt) current.updatedAt = review.updated_at;
        } else {
          grouped.set(key, {
            datasetDate: review.dataset_date,
            sourceClip: review.source_clip,
            reviews: [review],
            updatedAt: review.updated_at,
          });
        }
      });
    return [...grouped.values()].sort((left, right) => (
      right.updatedAt.localeCompare(left.updatedAt)
    ));
  }, [dateFilter, query, reviews, statusFilter]);

  return (
    <section className="mx-auto max-w-360 space-y-4 px-3 pb-28 pt-4 md:px-4 lg:px-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold tracking-tight text-console-text">人工复核</h2>
          <p className="mt-1.5 text-sm text-console-muted">
            对后处理生成的三维轨迹进行人工 Fix、提交和最终审批。
          </p>
        </div>
        <ConsoleButton onClick={() => void refresh()}>
          <RefreshCw aria-hidden="true" className="h-4 w-4" />
          刷新
        </ConsoleButton>
      </div>

      <div className="grid overflow-hidden rounded-lg border border-console-line bg-console-panel sm:grid-cols-2 lg:grid-cols-5">
        {[
          { filter: "pending" as const, label: "待复核", count: counts.pending, icon: ClipboardCheck },
          { filter: "in_progress" as const, label: "修正中", count: counts.in_progress, icon: Wrench },
          { filter: "returned" as const, label: "已退回", count: counts.returned, icon: RotateCcw },
          { filter: "verified" as const, label: "已验证", count: counts.verified, icon: CheckCircle2 },
          { filter: "discarded" as const, label: "已废弃", count: counts.discarded, icon: Trash2 },
        ].map(({ filter, label, count, icon: Icon }) => (
          <button
            key={filter}
            type="button"
            className="flex min-h-22 items-center gap-3 border-b border-console-line px-4 text-left transition hover:bg-console-panel2 sm:border-r lg:border-b-0"
            onClick={() => setStatusFilter(filter)}
          >
            <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-console-panel2">
              <Icon aria-hidden="true" className="h-5 w-5 text-console-cyan" />
            </span>
            <span>
              <span className="block text-xs text-console-muted">{label}</span>
              <span className="mt-1 block text-2xl font-semibold tabular-nums text-console-text">
                {count}
              </span>
            </span>
          </button>
        ))}
      </div>

      <ConsoleCard className="p-0">
        <div className="flex flex-col gap-3 border-b border-console-line p-4 lg:flex-row lg:items-center">
          <label className="relative min-w-0 flex-1 lg:max-w-sm">
            <span className="sr-only">搜索日期或外层 clip</span>
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-console-muted"
            />
            <input
              aria-label="搜索复核任务"
              className="h-10 w-full rounded-lg border border-console-line bg-white pl-9 pr-3 text-sm text-console-text outline-hidden focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15"
              placeholder="搜索日期或外层 clip"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as StatusFilter)}>
            <SelectTrigger aria-label="复核状态筛选" className="h-10 w-full bg-white lg:w-40">
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
          <input
            type="date"
            aria-label="复核数据日期"
            className="h-10 rounded-lg border border-console-line bg-white px-3 text-sm text-console-text outline-hidden focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15"
            value={dateFilter}
            onChange={(event) => setDateFilter(event.target.value)}
          />
        </div>

        {error && (
          <div role="alert" className="m-4 flex gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
            <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
            {error}
          </div>
        )}

        {loading ? (
          <div role="status" className="flex min-h-48 items-center justify-center gap-2 text-sm text-console-muted">
            <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
            正在读取复核队列…
          </div>
        ) : groups.length === 0 ? (
          <div className="flex min-h-48 flex-col items-center justify-center px-6 text-center">
            <ClipboardCheck aria-hidden="true" className="h-8 w-8 text-console-muted" />
            <p className="mt-3 text-sm font-medium text-console-text">没有符合条件的复核任务</p>
            <p className="mt-1 text-xs text-console-muted">后处理完成后，轨迹复核会自动出现在这里。</p>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="bg-console-panel2/70">
                <TableHead>数据日期</TableHead>
                <TableHead>外层 clip</TableHead>
                <TableHead>内部复核进度</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>更新时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {groups.map((group) => {
                const actionable = group.reviews.find((review) => (
                  review.status === "in_progress"
                  || review.status === "returned"
                  || review.status === "pending"
                )) ?? group.reviews[0];
                const statusCounts = group.reviews.reduce<Map<
                  ReviewPresentationKey,
                  { presentation: ReviewPresentation; count: number }
                >>(
                  (result, review) => {
                    const presentation = trajectoryReviewPresentation(review);
                    const current = result.get(presentation.key);
                    result.set(presentation.key, {
                      presentation,
                      count: (current?.count ?? 0) + 1,
                    });
                    return result;
                  },
                  new Map(),
                );
                return (
                  <TableRow key={`${group.datasetDate}:${group.sourceClip}`}>
                    <TableCell className="font-medium text-console-text">{group.datasetDate}</TableCell>
                    <TableCell className="max-w-72 truncate text-console-muted" title={group.sourceClip}>
                      {group.sourceClip}
                    </TableCell>
                    <TableCell>{group.reviews.length} 个复核单元</TableCell>
                    <TableCell>
                      <div className="flex max-w-md flex-wrap gap-1.5">
                        {PRESENTATION_ORDER
                          .map((key) => statusCounts.get(key))
                          .filter((entry): entry is {
                            presentation: ReviewPresentation;
                            count: number;
                          } => entry !== undefined)
                          .map(({ presentation, count }) => (
                            <StatusTag key={presentation.key} tone={presentation.tone}>
                              {presentation.label} {count}
                            </StatusTag>
                          ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-xs text-console-muted">
                      {updatedTime(group.updatedAt)}
                    </TableCell>
                    <TableCell className="text-right">
                      <ConsoleButton
                        onClick={() => navigate(
                          `/annotation/reviews/${encodeURIComponent(actionable.review_ref)}`,
                        )}
                      >
                        {actionable.status === "approved" || actionable.status === "discarded"
                          ? "查看记录"
                          : "进入人工 Fix"}
                      </ConsoleButton>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </ConsoleCard>
    </section>
  );
}
