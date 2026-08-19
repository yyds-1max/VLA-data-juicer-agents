import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClipboardCheck,
  LoaderCircle,
  RefreshCw,
  Search,
  Send,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createNavigationDatasetRelease,
  getNavigationDatasetReleases,
} from "../../api/client";
import type { NavigationDatasetRelease } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { cn } from "../../lib/utils";
import {
  getTrajectoryReviewEvidence,
  listTrajectoryReviews,
} from "../annotation/api";
import { isVerifiedReview } from "../annotation/reviewPresentation";
import { ReviewSegmentQueuePanel } from "../annotation/ReviewSegmentQueuePanel";
import {
  CameraEvidenceView,
  GridmapEvidenceView,
} from "../annotation/TrajectoryFixPage";
import type {
  TrajectoryEvidenceTarget,
  TrajectoryReview,
  TrajectoryReviewEvidence,
} from "../annotation/types";
import type { ProjectedTrajectoryTarget } from "../annotation/trajectoryEvidence";

function releaseKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `training-data-release-${crypto.randomUUID()}`;
  }
  return `training-data-release-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function displayDate(value: string): string {
  return /^(\d{4})(\d{2})(\d{2})$/.test(value)
    ? value.replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3")
    : value;
}

function displayTime(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function authoritativeTarget(target: TrajectoryEvidenceTarget): ProjectedTrajectoryTarget {
  const position = target.position === null
    ? null
    : { x: target.position[0], y: target.position[1] };
  const originalPosition = target.base_position === null
    ? null
    : { x: target.base_position[0], y: target.base_position[1] };
  return {
    ...target,
    original_position: originalPosition,
    position,
    original_direction: target.base_direction,
    original_speed: target.base_speed,
    present: position !== null,
    projection: "runtime_derived",
  };
}

function ReviewEvidenceViewer({ review }: { review: TrajectoryReview }) {
  const [evidence, setEvidence] = useState<TrajectoryReviewEvidence | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [frameIndex, setFrameIndex] = useState(0);
  const [targetRef, setTargetRef] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await getTrajectoryReviewEvidence(review.review_ref);
      if (
        !["fix_revision", "historical_fix"].includes(next.evidence_kind)
        || !next.fix_revision_ref
        || next.fix_revision_ref !== review.latest_publication?.fix_revision_ref
      ) {
        throw new Error("当前记录没有可查看的修正后轨迹证据。");
      }
      setEvidence(next);
      setFrameIndex(next.frames[0]?.frame_index ?? 0);
      setTargetRef(next.frames[0]?.targets[0]?.target_ref ?? "");
    } catch (caught) {
      setEvidence(null);
      setError(caught instanceof Error ? caught.message : "修正后轨迹证据加载失败。");
    } finally {
      setLoading(false);
    }
  }, [review]);

  useEffect(() => {
    void load();
  }, [load]);

  const frame = evidence?.frames.find((item) => item.frame_index === frameIndex)
    ?? evidence?.frames[0]
    ?? null;
  const targets = useMemo(
    () => (frame?.targets ?? []).map(authoritativeTarget),
    [frame],
  );
  const target = targets.find((item) => item.target_ref === targetRef)
    ?? targets[0]
    ?? null;
  const framePosition = evidence?.frames.findIndex((item) => item.frame_index === frame?.frame_index) ?? -1;

  useEffect(() => {
    if (target && target.target_ref !== targetRef) setTargetRef(target.target_ref);
  }, [target, targetRef]);

  if (loading) {
    return (
      <div className="flex min-h-[36rem] items-center justify-center gap-2 text-sm text-console-muted">
        <LoaderCircle aria-hidden="true" className="size-4 animate-spin" />
        正在加载修正后轨迹…
      </div>
    );
  }
  if (error || !evidence || !frame) {
    return (
      <div className="flex min-h-[36rem] flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-console-line text-center">
        <p role="alert" className="text-sm text-rose-600">{error || "当前记录没有可查看的修正后轨迹证据。"}</p>
        <ConsoleButton onClick={() => void load()}><RefreshCw aria-hidden="true" className="size-4" />重新加载</ConsoleButton>
      </div>
    );
  }

  const goToFrame = (position: number) => {
    const next = evidence.frames[position];
    if (!next) return;
    setFrameIndex(next.frame_index);
    setTargetRef(next.targets[0]?.target_ref ?? "");
  };

  return (
    <div className="overflow-hidden rounded-xl border border-console-line bg-slate-950">
      <div className="flex min-h-14 flex-wrap items-center gap-2 border-b border-slate-700 bg-white px-3 py-2">
        <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">
          修正后数据 · *_trajectory_fix_five.json
        </span>
        <span className="text-xs text-console-muted">第 {framePosition + 1} / {evidence.frame_count} 帧</span>
        <div className="ml-auto flex items-center gap-1">
          <ConsoleButton
            aria-label="上一帧"
            disabled={framePosition <= 0}
            onClick={() => goToFrame(framePosition - 1)}
          >
            <ChevronLeft aria-hidden="true" className="size-4" />
          </ConsoleButton>
          <ConsoleButton
            aria-label="下一帧"
            disabled={framePosition < 0 || framePosition >= evidence.frames.length - 1}
            onClick={() => goToFrame(framePosition + 1)}
          >
            <ChevronRight aria-hidden="true" className="size-4" />
          </ConsoleButton>
        </div>
        <label className="min-w-44">
          <span className="sr-only">当前目标</span>
          <Select value={target?.target_ref} onValueChange={setTargetRef}>
            <SelectTrigger aria-label="当前目标" className="h-9 bg-white">
              <SelectValue placeholder="暂无目标" />
            </SelectTrigger>
            <SelectContent>
              {targets.map((item) => (
                <SelectItem key={item.target_ref} value={item.target_ref}>{item.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="basis-full px-1">
          <span className="sr-only">轨迹帧时间线</span>
          <input
            aria-label="轨迹帧时间线"
            type="range"
            className="w-full accent-console-cyan"
            min={0}
            max={Math.max(0, evidence.frames.length - 1)}
            step={1}
            value={Math.max(0, framePosition)}
            onChange={(event) => goToFrame(Number(event.target.value))}
          />
        </label>
      </div>
      <div className="grid min-h-[36rem] grid-rows-2 lg:grid-cols-[minmax(0,42fr)_minmax(0,58fr)] lg:grid-rows-1">
        <section className="relative min-h-72 overflow-hidden border-b border-slate-700 lg:border-b-0 lg:border-r" aria-label="修正后相机投影">
          <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-lg bg-slate-950/75 px-3 py-2 text-white">
            <h3 className="text-sm font-semibold">相机投影</h3>
            <p className="mt-0.5 text-[11px] text-white/70">修正后轨迹在相机画面中的投影</p>
          </div>
          <CameraEvidenceView
            fill
            frameIndex={frame.frame_index}
            camera={frame.camera}
            projection={frame.projection}
            target={target}
            fixRevision
            authoritativeProjection={evidence.evidence_kind === "historical_fix"}
          />
        </section>
        <section className="relative min-h-72 overflow-hidden" aria-label="修正后 Gridmap">
          <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-lg bg-slate-950/75 px-3 py-2 text-white">
            <h3 className="text-sm font-semibold">Gridmap</h3>
            <p className="mt-0.5 text-[11px] text-white/70">修正后位置、方向与轨迹</p>
          </div>
          {frame.gridmap ? (
            <GridmapEvidenceView
              fill
              gridmap={frame.gridmap}
              target={target}
              editable={false}
              onPositionPreview={() => undefined}
              onDirectionPreview={() => undefined}
              onDragStateChange={() => undefined}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-slate-300">当前帧没有 Gridmap 证据。</div>
          )}
        </section>
      </div>
    </div>
  );
}

export function TrainingDataReviewPanel({ active = true }: { active?: boolean }) {
  const [releases, setReleases] = useState<NavigationDatasetRelease[]>([]);
  const [reviews, setReviews] = useState<TrajectoryReview[]>([]);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [selectedReviewRef, setSelectedReviewRef] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [reviewsLoading, setReviewsLoading] = useState(false);
  const [error, setError] = useState("");
  const [releaseDialogOpen, setReleaseDialogOpen] = useState(false);
  const [releaseNote, setReleaseNote] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [notice, setNotice] = useState("");
  const [noticeClosing, setNoticeClosing] = useState(false);
  const [query, setQuery] = useState("");
  const refreshActiveRef = useRef(false);

  const refresh = useCallback(async (mode: "initial" | "manual" | "background" = "background") => {
    if (refreshActiveRef.current) return;
    refreshActiveRef.current = true;
    if (mode === "initial") setLoading(true);
    else if (mode === "manual") setRefreshing(true);
    setError("");
    try {
      const nextReleases = await getNavigationDatasetReleases();
      setReleases(nextReleases
        .filter((item) => item.status === "ready" || item.status === "released")
        .sort((left, right) => (
          (right.updated_at ?? "").localeCompare(left.updated_at ?? "")
          || right.dataset_date.localeCompare(left.dataset_date)
        )));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "训练数据加载失败，请稍后重试。");
    } finally {
      refreshActiveRef.current = false;
      if (mode === "initial") setLoading(false);
      else if (mode === "manual") setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh("initial");
  }, [refresh]);

  useEffect(() => {
    if (!active) return undefined;
    const interval = window.setInterval(() => void refresh("background"), 30_000);
    return () => window.clearInterval(interval);
  }, [active, refresh]);

  useEffect(() => {
    if (!notice) return undefined;
    setNoticeClosing(false);
    const closeTimer = window.setTimeout(() => setNoticeClosing(true), 3_600);
    const clearTimer = window.setTimeout(() => setNotice(""), 3_900);
    return () => {
      window.clearTimeout(closeTimer);
      window.clearTimeout(clearTimer);
    };
  }, [notice]);

  const openDate = async (datasetDate: string) => {
    setSelectedDate(datasetDate);
    setSelectedReviewRef("");
    setReviews([]);
    setError("");
    setReviewsLoading(true);
    try {
      const nextReviews = await listTrajectoryReviews({
        status: "approved",
        datasetDate,
      });
      setReviews(nextReviews.filter(isVerifiedReview));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "修正后标注结果加载失败，请稍后重试。");
    } finally {
      setReviewsLoading(false);
    }
  };

  const selectedRelease = releases.find((item) => item.dataset_date === selectedDate) ?? null;
  const selectedReviews = useMemo(
    () => reviews
      .filter((review) => review.dataset_date === selectedDate)
      .sort((left, right) => left.source_clip.localeCompare(right.source_clip) || left.segment_ordinal - right.segment_ordinal),
    [reviews, selectedDate],
  );
  const selectedReview = selectedReviews.find((item) => item.review_ref === selectedReviewRef)
    ?? selectedReviews[0]
    ?? null;

  useEffect(() => {
    if (selectedReview && selectedReview.review_ref !== selectedReviewRef) {
      setSelectedReviewRef(selectedReview.review_ref);
    }
  }, [selectedReview, selectedReviewRef]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredReleases = releases.filter((release) => (
    !normalizedQuery || displayDate(release.dataset_date).toLocaleLowerCase().includes(normalizedQuery)
  ));

  async function publishSelectedDate() {
    if (!selectedRelease?.scope_manifest_sha256) return;
    setPublishing(true);
    setError("");
    try {
      await createNavigationDatasetRelease(
        selectedRelease.dataset_date,
        selectedRelease.scope_manifest_sha256,
        releaseNote.trim() || null,
        releaseKey(),
      );
      const publishedDate = selectedRelease.dataset_date;
      setReleaseDialogOpen(false);
      setReleaseNote("");
      setNotice(`${displayDate(publishedDate)} 已发布，可在新建训练中传输到训练节点。`);
      await refresh("background");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "数据发布失败，请刷新后重试。");
    } finally {
      setPublishing(false);
    }
  }

  const noticeToast = notice ? (
    <div
      role="status"
      aria-live="polite"
      data-phase={noticeClosing ? "closing" : "open"}
      className="training-data-toast fixed left-1/2 top-4 z-[100] flex w-[min(32rem,calc(100vw-2rem))] items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 shadow-lg"
    >
      <CheckCircle2 aria-hidden="true" className="size-4 shrink-0" />
      <span>{notice}</span>
    </div>
  ) : null;

  if (selectedDate && selectedRelease) {
    return (
      <section className="space-y-4" aria-label="修正后数据查看">
        {noticeToast}
        <div className="flex flex-wrap items-center gap-3">
          <ConsoleButton onClick={() => { setSelectedDate(null); setSelectedReviewRef(""); }}>
            <ArrowLeft aria-hidden="true" className="size-4" />返回训练数据
          </ConsoleButton>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-xl font-semibold text-console-text">{displayDate(selectedDate)} 标注结果</h2>
              <span className={cn(
                "rounded-full px-2 py-0.5 text-xs font-medium",
                selectedRelease.status === "released"
                  ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200"
                  : "bg-amber-50 text-amber-700 ring-1 ring-amber-200",
              )}>
                {selectedRelease.status === "released" ? "已发布" : "待发布"}
              </span>
            </div>
            <p className="mt-1 text-sm text-console-muted">只读查看人工修正后发布的轨迹文件；不会修改标注结果。</p>
          </div>
          {selectedRelease.status === "ready" ? (
            <ConsoleButton className="ml-auto" variant="primary" disabled={reviewsLoading} onClick={() => setReleaseDialogOpen(true)}>
              <Send aria-hidden="true" className="size-4" />发布该日期
            </ConsoleButton>
          ) : null}
        </div>
        {error ? <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        {reviewsLoading ? (
          <div className="flex min-h-36 items-center justify-center gap-2 rounded-xl border border-console-line bg-white text-sm text-console-muted">
            <LoaderCircle aria-hidden="true" className="size-4 animate-spin" />正在读取修正结果…
          </div>
        ) : (
          <ReviewSegmentQueuePanel
            reviews={selectedReviews}
            currentReviewRef={selectedReview?.review_ref ?? ""}
            className="min-h-[13rem] max-h-[20rem]"
            layout="horizontal"
            onNavigate={setSelectedReviewRef}
          />
        )}
        {selectedReview ? <ReviewEvidenceViewer key={selectedReview.review_ref} review={selectedReview} /> : (
          <div className="flex min-h-[36rem] items-center justify-center rounded-xl border border-dashed border-console-line text-sm text-console-muted">
            {reviewsLoading ? "正在准备修正后证据…" : "当前日期没有可视化的修正后 Segment。"}
          </div>
        )}

        <Dialog open={releaseDialogOpen} onOpenChange={(open) => { if (!publishing) setReleaseDialogOpen(open); }}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>发布 {displayDate(selectedDate)}</DialogTitle>
              <DialogDescription>
                发布后，该日期会进入可传输训练数据列表。此操作不会移动数据或启动训练。
              </DialogDescription>
            </DialogHeader>
            <label className="space-y-2 text-sm text-console-text">
              <span className="font-medium">发布备注（可选）</span>
              <textarea
                aria-label="发布备注（可选）"
                className="min-h-24 w-full resize-y rounded-lg border border-console-line bg-white px-3 py-2 outline-none focus:border-console-cyan"
                maxLength={1000}
                value={releaseNote}
                onChange={(event) => setReleaseNote(event.target.value)}
              />
              <span className="block text-right text-xs text-console-muted">{releaseNote.length}/1000</span>
            </label>
            <DialogFooter>
              <ConsoleButton disabled={publishing} onClick={() => setReleaseDialogOpen(false)}>取消</ConsoleButton>
              <ConsoleButton variant="primary" disabled={publishing} onClick={() => void publishSelectedDate()}>
                {publishing ? <LoaderCircle aria-hidden="true" className="size-4 animate-spin" /> : <Send aria-hidden="true" className="size-4" />}
                {publishing ? "发布中" : "确认发布"}
              </ConsoleButton>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </section>
    );
  }

  return (
    <section aria-labelledby="training-data-release-heading" className="border-b border-console-line bg-console-panel">
      {noticeToast}
      <header className="flex flex-col gap-4 py-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ClipboardCheck aria-hidden="true" className="size-5 text-console-cyan" />
            <h2 id="training-data-release-heading" className="text-lg font-semibold text-console-text">训练数据</h2>
          </div>
          <p className="mt-1 text-sm text-console-muted">查看已完成人工修正的训练数据。确认标注质量后按日期发布，已发布数据仍可随时回看。</p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="relative min-w-0 sm:w-64">
            <span className="sr-only">搜索训练数据</span>
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-console-muted" />
            <input
              type="search"
              aria-label="搜索训练数据"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索数据日期"
              className="h-9 w-full rounded-md border border-console-line bg-console-panel pl-9 pr-3 text-sm text-console-text outline-none transition-[border-color,box-shadow] duration-180 placeholder:text-console-muted/70 focus-visible:border-console-cyan focus-visible:ring-2 focus-visible:ring-console-cyan/15 motion-reduce:transition-none"
            />
          </label>
          <ConsoleButton disabled={loading || refreshing} onClick={() => void refresh("manual")}>
            <RefreshCw aria-hidden="true" className={cn("size-4", refreshing && "animate-spin")} />
            {refreshing ? "刷新中" : "刷新"}
          </ConsoleButton>
        </div>
      </header>
      {error ? <div role="alert" className="mb-4 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
      <div className="overflow-x-auto border-t border-console-line">
        <table className="w-full min-w-[860px] table-fixed text-left text-sm">
          <thead className="bg-console-panel2 text-xs font-medium text-console-muted">
            <tr>
              <th className="w-[20%] px-4 py-3">数据日期</th>
              <th className="w-[11%] px-4 py-3">发布状态</th>
              <th className="w-[11%] px-4 py-3">Clips</th>
              <th className="w-[15%] px-4 py-3">已验证 Segment</th>
              <th className="w-[15%] px-4 py-3">已废弃 Segment</th>
              <th className="w-[20%] px-4 py-3">更新时间</th>
              <th className="w-[8%] px-4 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {!loading && filteredReleases.map((release) => (
              <tr key={release.dataset_date} className="border-t border-console-line transition-[background-color] duration-150 hover:bg-console-panel2/70 motion-reduce:transition-none">
                <td className="px-4 py-4 align-middle">
                  <button type="button" className="font-medium text-console-text hover:text-console-cyan focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => void openDate(release.dataset_date)}>
                    {displayDate(release.dataset_date)}
                  </button>
                </td>
                <td className="px-4 py-4 align-middle">
                  <span className={cn(
                    "inline-flex rounded-full px-2 py-0.5 text-xs font-medium",
                    release.status === "released"
                      ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200"
                      : "bg-amber-50 text-amber-700 ring-1 ring-amber-200",
                  )}>
                    {release.status === "released" ? "已发布" : "待发布"}
                  </span>
                </td>
                <td className="px-4 py-4 align-middle tabular-nums text-console-muted">{release.source_clip_count}</td>
                <td className="px-4 py-4 align-middle tabular-nums text-console-text">{release.verified_unit_count}</td>
                <td className="px-4 py-4 align-middle tabular-nums text-console-muted">{release.discarded_unit_count}</td>
                <td className="px-4 py-4 align-middle text-xs text-console-muted">{displayTime(release.updated_at)}</td>
                <td className="px-4 py-4 text-right align-middle">
                  <button type="button" className="inline-flex items-center gap-1 rounded px-1 py-1 text-xs font-medium text-console-cyan hover:bg-blue-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-console-cyan/30" onClick={() => void openDate(release.dataset_date)}>
                    查看 <ArrowRight aria-hidden="true" className="size-3.5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {loading || filteredReleases.length === 0 ? (
        <div className="border-t border-console-line py-16 text-center">
          {loading ? <LoaderCircle aria-hidden="true" className="mx-auto size-8 animate-spin text-console-muted" /> : <ClipboardCheck aria-hidden="true" className="mx-auto size-8 text-console-muted" />}
          <p className="mt-3 text-sm font-medium text-console-text">{loading ? "正在读取训练数据…" : releases.length ? "没有符合搜索条件的数据" : "当前没有可用训练数据"}</p>
          <p className="mt-1 text-sm text-console-muted">{loading ? "页面加载完成后会保留当前数据，切换标签不会重复刷新。" : releases.length ? "请调整搜索日期。" : "人工复核完成后，待发布和已发布数据都会显示在这里。"}</p>
        </div>
      ) : null}
    </section>
  );
}
