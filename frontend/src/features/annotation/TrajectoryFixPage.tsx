import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CloudOff,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  Trash2,
  Undo2,
  UserPlus,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  useBeforeUnload,
  useBlocker,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useStore } from "zustand";

import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { StatusTag } from "../../components/console/StatusTag";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog";
import { Checkbox } from "../../components/ui/checkbox";
import { ScrollArea, ScrollBar } from "../../components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "../../components/ui/tabs";
import {
  AnnotationApiError,
  applyFixCommand,
  createFixRevision,
  createFixSession,
  decideTrajectoryReview,
  getCalibrationProfiles,
  getTrajectoryReviewEvidence,
  retryReviewPublication,
} from "./api";
import {
  annotationProjectionStore,
  cacheTrajectoryReview,
  loadTrajectoryReview,
  loadTrajectoryReviews,
  retainTrajectoryReviewProjection,
} from "./projectionStore";
import { trajectoryReviewPresentation } from "./reviewPresentation";
import type {
  CalibrationProfile,
  FixCommand,
  TrajectoryPoint,
  TrajectoryReview,
  TrajectoryReviewEvidence,
} from "./types";
import {
  cameraCanRender,
  evidenceMatchesReview,
  projectTrajectoryReviewEvidence,
  trajectoryPositionPath,
} from "./trajectoryEvidence";
import type {
  ProjectedTrajectoryFrame,
  ProjectedTrajectoryTarget,
} from "./trajectoryEvidence";

type EditableTarget = {
  x: string;
  y: string;
  direction: string;
  speed: string;
  pass: boolean;
};

type Decision = "approve" | "return" | "discard" | null;

function safeFixError(error: unknown, fallback: string): string {
  if (error instanceof AnnotationApiError) {
    return error.detail?.code ? `${fallback}（${error.detail.code}）` : fallback;
  }
  const message = error instanceof Error ? error.message : "";
  return /(?:^|[\s("'`])\/(?:[^/\s]+\/){2,}|[A-Za-z]:\\/.test(message)
    ? fallback
    : message || fallback;
}

function targetEditor(
  target: ProjectedTrajectoryTarget | null,
  framePass = false,
): EditableTarget {
  return {
    x: target?.position?.x == null ? "" : String(target.position.x),
    y: target?.position?.y == null ? "" : String(target.position.y),
    direction: target?.direction == null ? "" : String(target.direction),
    speed: target?.speed == null ? "" : String(target.speed),
    pass: framePass,
  };
}

function finiteValue(value: string): number | null {
  if (!value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function latestRevision(review: TrajectoryReview) {
  return review.fix_revisions.at(-1) ?? null;
}

function TrajectoryCanvas({
  original,
  current,
}: {
  original: Array<TrajectoryPoint | null>;
  current: Array<TrajectoryPoint | null>;
}) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, canvas.clientWidth);
    const height = Math.max(1, canvas.clientHeight);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#f8fafc";
    context.fillRect(0, 0, width, height);
    context.strokeStyle = "#e2e8f0";
    context.lineWidth = 1;
    for (let x = 24; x < width; x += 24) {
      context.beginPath();
      context.moveTo(x, 0);
      context.lineTo(x, height);
      context.stroke();
    }
    for (let y = 24; y < height; y += 24) {
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }

    const points = [...original, ...current].filter(
      (point): point is TrajectoryPoint => point !== null,
    );
    if (!points.length) return;
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const scaleX = (width - 32) / Math.max(maxX - minX, 1);
    const scaleY = (height - 32) / Math.max(maxY - minY, 1);
    const scale = Math.min(scaleX, scaleY);
    const draw = (
      series: Array<TrajectoryPoint | null>,
      color: string,
      dashed: boolean,
    ) => {
      if (!series.some((point) => point !== null)) return;
      context.strokeStyle = color;
      context.lineWidth = 2;
      context.setLineDash(dashed ? [6, 4] : []);
      let active = false;
      series.forEach((point) => {
        if (point === null) {
          if (active) context.stroke();
          active = false;
          return;
        }
        const x = 16 + (point.x - minX) * scale;
        const y = height - 16 - (point.y - minY) * scale;
        if (!active) {
          context.beginPath();
          context.moveTo(x, y);
          active = true;
        } else {
          context.lineTo(x, y);
        }
      });
      if (active) context.stroke();
    };
    draw(original, "#64748b", true);
    draw(current, "#2563eb", false);
    context.setLineDash([]);
  }, [current, original]);

  return (
    <canvas
      ref={ref}
      aria-label="轨迹对比视图"
      className="h-72 w-full rounded-lg border border-console-line bg-slate-50"
    />
  );
}

function DirtyNavigationGuard({ dirty }: { dirty: boolean }) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    dirty && currentLocation.pathname !== nextLocation.pathname
  ));
  useBeforeUnload(
    useCallback((event) => {
      if (!dirty) return;
      event.preventDefault();
    }, [dirty]),
  );

  return (
    <AlertDialog open={blocker.state === "blocked"}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>修正仍在保存</AlertDialogTitle>
          <AlertDialogDescription>
            离开会丢弃本页尚未确认写入服务器的修改。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={() => blocker.state === "blocked" && blocker.reset()}>
            留在本页
          </AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            onClick={() => blocker.state === "blocked" && blocker.proceed()}
          >
            放弃并离开
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function TrajectoryFixWorkbench({
  reviewRef,
}: {
  reviewRef: string | undefined;
}) {
  const navigate = useNavigate();
  const projectedReviews = useStore(
    annotationProjectionStore,
    (state) => state.reviews,
  );
  const projectedReview = useStore(
    annotationProjectionStore,
    (state) => reviewRef
      ? state.reviewDetails[reviewRef] ?? null
      : null,
  );
  const [review, setReview] = useState<TrajectoryReview | null>(null);
  const [reviewQueue, setReviewQueue] = useState<TrajectoryReview[]>([]);
  const [reviewQueueError, setReviewQueueError] = useState("");
  const [profiles, setProfiles] = useState<CalibrationProfile[]>([]);
  const [evidence, setEvidence] = useState<TrajectoryReviewEvidence | null>(null);
  const [evidenceError, setEvidenceError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [acting, setActing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [differenceReason, setDifferenceReason] = useState("");
  const [frameIndex, setFrameIndex] = useState(0);
  const [targetRef, setTargetRef] = useState("");
  const [editor, setEditor] = useState<EditableTarget>(() => targetEditor(null));
  const [savedEditor, setSavedEditor] = useState<EditableTarget>(() => targetEditor(null));
  const [decision, setDecision] = useState<Decision>(null);
  const [decisionReason, setDecisionReason] = useState("");
  const reviewStateRef = useRef<TrajectoryReview | null>(null);
  const initialRefreshCompleteRef = useRef(false);

  useEffect(() => {
    reviewStateRef.current = review;
  }, [review]);

  useEffect(
    () => reviewRef
      ? retainTrajectoryReviewProjection(reviewRef)
      : undefined,
    [reviewRef],
  );

  useEffect(() => {
    if (!review) return;
    const matching = new Map<string, TrajectoryReview>();
    for (const item of [...projectedReviews, review]) {
      if (
        item.dataset_date !== review.dataset_date
        || item.source_clip !== review.source_clip
      ) {
        continue;
      }
      const current = matching.get(item.review_ref);
      if (!current || current.state_revision <= item.state_revision) {
        matching.set(item.review_ref, item);
      }
    }
    setReviewQueue(
      [...matching.values()].sort((left, right) => (
        left.segment_ordinal - right.segment_ordinal
        || left.review_ref.localeCompare(right.review_ref)
      )),
    );
  }, [projectedReviews, review]);

  const loadEvidence = useCallback(async (
    expectedReview?: TrajectoryReview,
  ): Promise<boolean> => {
    if (!reviewRef) return false;
    try {
      const next = await getTrajectoryReviewEvidence(reviewRef);
      const owner = expectedReview ?? reviewStateRef.current;
      if (!owner || !evidenceMatchesReview(next, owner)) {
        throw new Error("轨迹证据版本与当前复核任务不一致，请刷新后重试。");
      }
      setEvidence(next);
      setEvidenceError("");
      return true;
    } catch (requestError) {
      setEvidence(null);
      setEvidenceError(safeFixError(
        requestError,
        "服务器尚未提供该轨迹版本的公开证据，当前不能执行几何修正。",
      ));
      return false;
    }
  }, [reviewRef]);

  useEffect(() => {
    if (!projectedReview || !initialRefreshCompleteRef.current) return;
    const current = reviewStateRef.current;
    if (
      current
      && current.review_ref === projectedReview.review_ref
      && current.state_revision >= projectedReview.state_revision
    ) {
      return;
    }
    setReview(projectedReview);
    reviewStateRef.current = projectedReview;
    setSelectedProfile(
      projectedReview.fix_draft?.calibration.profile_ref ?? "",
    );
    setDifferenceReason(
      projectedReview.fix_draft?.calibration.difference_reason ?? "",
    );
    setError("");
    void loadEvidence(projectedReview);
  }, [loadEvidence, projectedReview]);

  const refresh = useCallback(async (
    silent = false,
    force = true,
    includeQueue = true,
  ) => {
    if (!reviewRef) return;
    if (!silent) setLoading(true);
    const [reviewResult, profilesResult] = await Promise.allSettled([
      loadTrajectoryReview(reviewRef, { force }),
      getCalibrationProfiles("fix"),
    ]);
    let nextReview: TrajectoryReview | undefined;
    if (reviewResult.status === "fulfilled") {
      const owner = reviewResult.value;
      nextReview = owner;
      setReview(owner);
      reviewStateRef.current = owner;
      setSelectedProfile(
        owner.fix_draft?.calibration.profile_ref ?? "",
      );
      setDifferenceReason(
        owner.fix_draft?.calibration.difference_reason ?? "",
      );
      setError("");
      if (includeQueue) {
        try {
          const queue = await loadTrajectoryReviews({ force });
          const matching = queue
            .filter((item) => (
              item.dataset_date === owner.dataset_date
              && item.source_clip === owner.source_clip
            ))
            .sort((left, right) => (
              left.segment_ordinal - right.segment_ordinal
              || left.review_ref.localeCompare(right.review_ref)
            ));
          setReviewQueue(
            [
              ...matching.filter((item) => item.review_ref !== owner.review_ref),
              owner,
            ].sort((left, right) => (
              left.segment_ordinal - right.segment_ordinal
              || left.review_ref.localeCompare(right.review_ref)
            )),
          );
          setReviewQueueError("");
        } catch (requestError) {
          setReviewQueue([owner]);
          setReviewQueueError(safeFixError(
            requestError,
            "读取同一外层 clip 的 Segment 队列失败",
          ));
        }
      } else {
        setReviewQueue((current) => (
          [
            ...current.filter((item) => item.review_ref !== owner.review_ref),
            owner,
          ].sort((left, right) => (
            left.segment_ordinal - right.segment_ordinal
            || left.review_ref.localeCompare(right.review_ref)
          ))
        ));
      }
    } else {
      setError(safeFixError(reviewResult.reason, "读取复核任务失败"));
      setReviewQueue([]);
      setReviewQueueError("");
    }
    if (profilesResult.status === "fulfilled") {
      setProfiles(profilesResult.value);
    }
    if (nextReview) {
      await loadEvidence(nextReview);
    } else {
      setEvidence(null);
    }
    initialRefreshCompleteRef.current = true;
    if (!silent) setLoading(false);
  }, [loadEvidence, reviewRef]);

  useEffect(() => {
    void refresh(false, false);
  }, [refresh]);

  const projectedEvidence = useMemo(
    () => evidence ? projectTrajectoryReviewEvidence(evidence) : null,
    [evidence],
  );
  const currentFrame: ProjectedTrajectoryFrame | null = useMemo(() => (
    projectedEvidence?.frames.find((frame) => frame.frame_index === frameIndex)
    ?? projectedEvidence?.frames[0]
    ?? null
  ), [frameIndex, projectedEvidence]);
  const selectedTarget = useMemo(() => (
    currentFrame?.targets.find((target) => target.target_ref === targetRef)
    ?? currentFrame?.targets[0]
    ?? null
  ), [currentFrame, targetRef]);

  useEffect(() => {
    if (!currentFrame) return;
    if (frameIndex !== currentFrame.frame_index) setFrameIndex(currentFrame.frame_index);
    const nextTarget = currentFrame.targets.find((target) => target.target_ref === targetRef)
      ?? currentFrame.targets[0]
      ?? null;
    if (nextTarget && targetRef !== nextTarget.target_ref) setTargetRef(nextTarget.target_ref);
    const nextEditor = targetEditor(nextTarget, currentFrame.pass);
    setEditor(nextEditor);
    setSavedEditor(nextEditor);
  }, [
    currentFrame?.frame_index,
    currentFrame?.pass,
    projectedEvidence?.draft_revision,
    projectedEvidence?.review_state_revision,
    targetRef,
  ]);

  const editorDirty = JSON.stringify(editor) !== JSON.stringify(savedEditor);
  const pageDirty = editorDirty || saving;
  const evidenceAvailable = evidence !== null && evidenceError === "";
  const fixRuntimeBusy = (
    review?.active_fix_run?.status === "queued"
    || review?.active_fix_run?.status === "running"
  );
  const publicationBusy = (
    review?.status === "approved"
    && review.latest_publication?.status === "publishing"
  );
  const editable = Boolean(
    review?.status === "in_progress"
    && review.fix_draft
    && evidenceAvailable
    && selectedTarget
    && !fixRuntimeBusy
  );
  const originalTrajectory = useMemo(
    () => trajectoryPositionPath(projectedEvidence, targetRef, "original"),
    [projectedEvidence, targetRef],
  );
  const projectedTrajectory = useMemo(
    () => trajectoryPositionPath(projectedEvidence, targetRef, "projected"),
    [projectedEvidence, targetRef],
  );
  const updateFromResult = useCallback((next: TrajectoryReview) => {
    cacheTrajectoryReview(next);
    setReview(next);
    reviewStateRef.current = next;
    setConflict(false);
  }, []);

  const runCommand = useCallback(async (
    command: FixCommand,
    onSaved?: () => void,
  ) => {
    const current = reviewStateRef.current;
    if (
      !reviewRef
      || !current?.fix_draft
      || current.status !== "in_progress"
      || current.active_fix_run?.status === "queued"
      || current.active_fix_run?.status === "running"
    ) {
      return false;
    }
    setSaving(true);
    setError("");
    try {
      const next = await applyFixCommand(reviewRef, {
        expected_review_revision: current.state_revision,
        expected_draft_revision: current.fix_draft.revision,
        command,
      });
      updateFromResult(next);
      await loadEvidence(next);
      onSaved?.();
      return true;
    } catch (requestError) {
      if (requestError instanceof AnnotationApiError && requestError.status === 409) {
        setConflict(true);
      }
      setError(safeFixError(requestError, "保存 Fix 修改失败"));
      return false;
    } finally {
      setSaving(false);
    }
  }, [loadEvidence, reviewRef, updateFromResult]);

  useEffect(() => {
    if (!editable || !editorDirty || saving || conflict || !selectedTarget) return;
    const timer = window.setTimeout(() => {
      const x = finiteValue(editor.x);
      const y = finiteValue(editor.y);
      const savedX = finiteValue(savedEditor.x);
      const savedY = finiteValue(savedEditor.y);
      if (x !== savedX || y !== savedY) {
        if (x == null || y == null) return;
        const nextX = editor.x;
        const nextY = editor.y;
        void runCommand({
          kind: "set_position",
          frame_index: frameIndex,
          target_ref: selectedTarget.target_ref,
          x,
          y,
        }, () => setSavedEditor((current) => ({ ...current, x: nextX, y: nextY })));
        return;
      }
      const direction = finiteValue(editor.direction);
      if (direction !== finiteValue(savedEditor.direction)) {
        if (direction == null) return;
        const nextDirection = editor.direction;
        void runCommand({
          kind: "set_direction",
          frame_index: frameIndex,
          target_ref: selectedTarget.target_ref,
          direction,
        }, () => setSavedEditor((current) => ({ ...current, direction: nextDirection })));
        return;
      }
      const speed = finiteValue(editor.speed);
      if (speed !== finiteValue(savedEditor.speed)) {
        if (speed == null || speed < 0) return;
        const nextSpeed = editor.speed;
        void runCommand({
          kind: "set_speed",
          frame_index: frameIndex,
          target_ref: selectedTarget.target_ref,
          speed,
        }, () => setSavedEditor((current) => ({ ...current, speed: nextSpeed })));
      }
    }, 700);
    return () => window.clearTimeout(timer);
  }, [
    conflict,
    editable,
    editor,
    editorDirty,
    frameIndex,
    runCommand,
    savedEditor,
    saving,
    selectedTarget,
  ]);

  if (!reviewRef) {
    return null;
  }
  if (loading) {
    return (
      <section className="mx-auto flex min-h-96 max-w-360 items-center justify-center px-4">
        <LoaderCircle aria-hidden="true" className="mr-2 h-5 w-5 animate-spin text-console-cyan" />
        <span className="text-sm text-console-muted">正在加载 Fix 工作台…</span>
      </section>
    );
  }
  if (!review) {
    return (
      <section className="mx-auto max-w-360 px-4 py-6">
        <ConsoleCard className="text-center">
          <AlertCircle aria-hidden="true" className="mx-auto h-8 w-8 text-rose-500" />
          <p className="mt-3 font-semibold text-console-text">无法打开复核任务</p>
          <p className="mt-2 text-sm text-console-muted">{error}</p>
          <ConsoleButton className="mt-4" onClick={() => navigate("/annotation/reviews")}>
            返回人工复核
          </ConsoleButton>
        </ConsoleCard>
      </section>
    );
  }

  const selectedCalibration = profiles.find((profile) => profile.profile_ref === selectedProfile);
  const calibrationDiffers = Boolean(
    selectedCalibration
    && selectedCalibration.profile_ref !== review.processing_calibration.profile_ref
  );
  const canStart = Boolean(
    (review.status === "pending" || review.status === "returned")
    && selectedCalibration
    && evidenceAvailable
    && !fixRuntimeBusy
    && (!calibrationDiffers || differenceReason.trim())
  );
  const revision = latestRevision(review);
  const terminal = review.status === "approved" || review.status === "discarded";
  const reviewPresentation = trajectoryReviewPresentation(review);
  const currentCamera = currentFrame?.camera ?? null;
  const renderableCamera = cameraCanRender(currentCamera)
    ? currentCamera
    : null;

  const startSession = async () => {
    if (!selectedCalibration || !canStart) return;
    setActing(true);
    setError("");
    try {
      const next = await createFixSession(review.review_ref, {
        expected_review_revision: review.state_revision,
        calibration_profile_ref: selectedCalibration.profile_ref,
        calibration_content_sha256: selectedCalibration.content_sha256,
        ...(differenceReason.trim()
          ? { calibration_difference_reason: differenceReason.trim() }
          : {}),
      });
      updateFromResult(next);
      await loadEvidence(next);
    } catch (requestError) {
      setError(safeFixError(requestError, "启动人工 Fix 失败"));
    } finally {
      setActing(false);
    }
  };

  const submitRevision = async () => {
    if (!review.fix_draft || editorDirty || saving || fixRuntimeBusy) return;
    setActing(true);
    try {
      const next = await createFixRevision(review.review_ref, {
        expected_review_revision: review.state_revision,
        expected_draft_revision: review.fix_draft.revision,
      });
      updateFromResult(next);
      await loadEvidence(next);
    } catch (requestError) {
      setError(safeFixError(requestError, "提交 Fix 版本失败"));
    } finally {
      setActing(false);
    }
  };

  const applyDecision = async () => {
    if (!decision || fixRuntimeBusy) return;
    if (decision === "approve" && !revision) return;
    if (decision !== "approve" && !decisionReason.trim()) return;
    setActing(true);
    try {
      const next = await decideTrajectoryReview(
        review.review_ref,
        decision,
        decision === "approve"
          ? {
              expected_review_revision: review.state_revision,
              fix_revision_ref: revision!.revision_ref,
            }
          : {
              expected_review_revision: review.state_revision,
              reason: decisionReason.trim(),
            },
      );
      updateFromResult(next);
      await loadEvidence(next);
      setDecision(null);
      setDecisionReason("");
    } catch (requestError) {
      setError(safeFixError(requestError, "提交复核结论失败"));
    } finally {
      setActing(false);
    }
  };

  return (
    <section className="mx-auto max-w-360 space-y-4 px-3 pb-28 pt-4 md:px-4 lg:px-5">
      <DirtyNavigationGuard dirty={pageDirty} />

      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex items-start gap-3">
          <ConsoleButton aria-label="返回人工复核" onClick={() => navigate("/annotation/reviews")}>
            <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          </ConsoleButton>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold text-console-text">
                {review.dataset_date} · {review.source_clip}
              </h2>
              <StatusTag tone={reviewPresentation.tone}>
                {reviewPresentation.label}
              </StatusTag>
            </div>
            <p className="mt-1 text-sm text-console-muted">
              Segment {String(review.segment_ordinal).padStart(2, "0")} ·
              原轨迹版本 {review.trajectory_revision.revision_ref.slice(-8)}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <ConsoleButton onClick={() => void refresh()}>
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            刷新
          </ConsoleButton>
          {review.latest_publication?.status === "failed" && (
            <ConsoleButton
              disabled={acting || fixRuntimeBusy}
              onClick={async () => {
                setActing(true);
                try {
                  const next = await retryReviewPublication(
                    review.review_ref,
                    review.state_revision,
                  );
                  updateFromResult(next);
                  await loadEvidence(next);
                } catch (requestError) {
                  setError(safeFixError(requestError, "重试发布失败"));
                } finally {
                  setActing(false);
                }
              }}
            >
              <RotateCcw aria-hidden="true" className="h-4 w-4" />
              重试发布
            </ConsoleButton>
          )}
        </div>
      </div>

      <ConsoleCard className="p-0">
        <div className="flex flex-col gap-1 border-b border-console-line px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="text-sm font-semibold text-console-text">Segment 队列</h3>
            <p className="mt-0.5 text-xs text-console-muted">
              同一日期与外层 clip，共 {reviewQueue.length} 个复核单元
            </p>
          </div>
          {reviewQueueError && (
            <span className="text-xs text-amber-700">{reviewQueueError}</span>
          )}
        </div>
        <ScrollArea className="w-full whitespace-nowrap">
          <div className="flex w-max min-w-full gap-2 p-3">
            {reviewQueue.map((item) => {
              const active = item.review_ref === review.review_ref;
              const label = `Segment ${String(item.segment_ordinal).padStart(2, "0")}`;
              const presentation = trajectoryReviewPresentation(item);
              return (
                <button
                  key={item.review_ref}
                  type="button"
                  aria-current={active ? "page" : undefined}
                  aria-label={`${active ? "当前" : "切换到"} ${label}`}
                  className={`min-w-36 rounded-lg border px-3 py-2 text-left transition ${
                    active
                      ? "border-console-cyan bg-blue-50 ring-2 ring-console-cyan/15"
                      : "border-console-line bg-white hover:bg-console-panel2"
                  }`}
                  onClick={() => {
                    if (active) return;
                    navigate(`/annotation/reviews/${encodeURIComponent(item.review_ref)}`);
                  }}
                >
                  <span className="block text-sm font-medium text-console-text">{label}</span>
                  <span className="mt-1 block">
                    <StatusTag tone={presentation.tone}>
                      {presentation.label}
                    </StatusTag>
                  </span>
                </button>
              );
            })}
          </div>
          <ScrollBar orientation="horizontal" />
        </ScrollArea>
      </ConsoleCard>

      {publicationBusy && (
        <div
          role="status"
          className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-blue-900"
        >
          <div className="flex items-start gap-3">
            <LoaderCircle
              aria-hidden="true"
              className="mt-0.5 h-5 w-5 shrink-0 animate-spin"
            />
            <div>
              <p className="font-semibold">已批准，训练兼容文件正在发布</p>
              <p className="mt-1 text-sm text-blue-800">
                页面会自动刷新。只有发布成功后，该轨迹才显示为“已验证”。
              </p>
            </div>
          </div>
        </div>
      )}
      {fixRuntimeBusy && (
        <div
          role="status"
          className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-blue-900"
        >
          <div className="flex items-start gap-3">
            <LoaderCircle
              aria-hidden="true"
              className="mt-0.5 h-5 w-5 shrink-0 animate-spin"
            />
            <div>
              <p className="font-semibold">
                {review.active_fix_run?.status === "queued"
                  ? "Fix 版本正在等待执行"
                  : "Fix Runtime 正在生成候选版本"}
              </p>
              <p className="mt-1 text-sm text-blue-800">
                草稿已冻结。完成前不能继续编辑、重复提交或作出审核结论；页面会自动刷新状态。
              </p>
            </div>
          </div>
        </div>
      )}
      {review.fix_failure && (
        <div
          role="alert"
          className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-rose-900"
        >
          <p className="font-semibold">Fix 版本生成失败</p>
          <p className="mt-1 text-sm text-rose-800">
            {safeFixError(
              new Error(review.fix_failure.message),
              "Fix Runtime 执行失败。",
            )}
          </p>
          <p className="mt-2 text-xs text-rose-700">
            错误代码：{review.fix_failure.code}
            {review.fix_failure.error_ref
              ? ` · 审计引用：${review.fix_failure.error_ref}`
              : ""}
          </p>
          <p className="mt-1 text-xs text-rose-700">
            {review.fix_failure.retryable
              ? "可以调整草稿后重新提交 Fix 版本。"
              : "该失败不可由页面直接重试，请联系运维人员核查。"}
          </p>
        </div>
      )}
      {!review.fix_failure && review.active_fix_run?.status === "failed" && (
        <div
          role="alert"
          className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-rose-900"
        >
          <p className="font-semibold">Fix 版本生成失败</p>
          <p className="mt-1 text-sm text-rose-800">
            Runtime 未生成可供审核的新 Fix 版本，请刷新或联系运维人员。
          </p>
          {review.active_fix_run.failure?.code && (
            <p className="mt-2 text-xs text-rose-700">
              错误代码：{review.active_fix_run.failure.code}
            </p>
          )}
        </div>
      )}
      {error && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </div>
      )}
      {conflict && (
        <div role="alert" className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <p className="font-semibold">检测到并发修改</p>
          <p className="mt-1">本页没有自动合并轨迹。请选择服务器版本，或基于最新版本重新提交本地数值。</p>
          <div className="mt-3 flex gap-2">
            <ConsoleButton
              onClick={() => {
                setConflict(false);
                void refresh();
              }}
            >
              使用服务器版本
            </ConsoleButton>
            <ConsoleButton
              variant="primary"
              onClick={async () => {
                try {
                  const next = await loadTrajectoryReview(review.review_ref, {
                    force: true,
                  });
                  updateFromResult(next);
                  await loadEvidence(next);
                } catch (requestError) {
                  setError(safeFixError(requestError, "刷新复核版本失败"));
                }
              }}
            >
              保留本地数值并重试
            </ConsoleButton>
          </div>
        </div>
      )}

      {!evidenceAvailable && (
        <div role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <div className="flex gap-3">
            <CloudOff aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-amber-700" />
            <div>
              <h3 className="text-sm font-semibold text-amber-900">轨迹证据不可用</h3>
              <p className="mt-1 text-sm text-amber-800">{evidenceError}</p>
              <p className="mt-1 text-xs text-amber-700">
                系统不会构造替代数据；相机、gridmap 和领域 Fix 命令暂时禁用。
              </p>
            </div>
          </div>
        </div>
      )}

      {(review.status === "pending" || review.status === "returned") && (
        <ConsoleCard>
          <h3 className="text-sm font-semibold text-console-text">
            {review.status === "returned" ? "继续人工 Fix" : "开始人工 Fix"}
          </h3>
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div>
              <p className="text-xs text-console-muted">原后处理标定</p>
              <p className="mt-1 text-sm font-medium text-console-text">
                {review.processing_calibration.label}
              </p>
            </div>
            <label>
              <span className="mb-1.5 block text-xs font-medium text-console-muted">Fix 标定</span>
              <Select
                disabled={review.status === "returned" && Boolean(review.fix_draft)}
                value={selectedProfile}
                onValueChange={setSelectedProfile}
              >
                <SelectTrigger aria-label="Fix 标定" className="h-10 w-full bg-white">
                  <SelectValue placeholder="请选择 Fix 标定" />
                </SelectTrigger>
                <SelectContent>
                  {profiles.map((profile) => (
                    <SelectItem key={profile.profile_ref} value={profile.profile_ref}>
                      {profile.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
          </div>
          {calibrationDiffers && (
            <label className="mt-4 block">
              <span className="mb-1.5 block text-xs font-medium text-console-muted">
                与处理标定不同时的原因
              </span>
              <textarea
                value={differenceReason}
                onChange={(event) => setDifferenceReason(event.target.value)}
                className="min-h-20 w-full rounded-lg border border-console-line bg-white p-3 text-sm text-console-text outline-hidden focus:border-console-cyan focus:ring-2 focus:ring-console-cyan/15"
                maxLength={1000}
              />
            </label>
          )}
          <div className="mt-4 flex justify-end">
            <ConsoleButton variant="primary" disabled={!canStart || acting} onClick={() => void startSession()}>
              {acting && <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />}
              {review.status === "returned" ? "返回 Fix 工作台" : "创建 Fix 草稿"}
            </ConsoleButton>
          </div>
        </ConsoleCard>
      )}

      {(review.status === "in_progress" || terminal) && (
        <div className="grid gap-4 xl:grid-cols-[17rem_minmax(0,1fr)_20rem]">
          <ConsoleCard className="p-0">
            <div className="border-b border-console-line p-4">
              <h3 className="text-sm font-semibold text-console-text">帧与目标</h3>
              <p className="mt-1 text-xs text-console-muted">
                {evidence?.frame_count ?? 0} 帧 · 当前第 {frameIndex + 1} 帧
              </p>
            </div>
            <div className="flex items-center justify-between border-b border-console-line p-3">
              <ConsoleButton
                aria-label="上一帧"
                disabled={!currentFrame || frameIndex <= 0}
                onClick={() => setFrameIndex(Math.max(0, frameIndex - 1))}
              >
                <ChevronLeft aria-hidden="true" className="h-4 w-4" />
              </ConsoleButton>
              <span className="text-sm tabular-nums text-console-text">{frameIndex + 1}</span>
              <ConsoleButton
                aria-label="下一帧"
                disabled={!evidence || frameIndex >= evidence.frame_count - 1}
                onClick={() => setFrameIndex(Math.min(
                  Math.max(0, (evidence?.frame_count ?? 1) - 1),
                  frameIndex + 1,
                ))}
              >
                <ChevronRight aria-hidden="true" className="h-4 w-4" />
              </ConsoleButton>
            </div>
            <div className="border-b border-console-line px-3 py-3">
              <label className="block">
                <span className="sr-only">轨迹帧时间线</span>
                <input
                  type="range"
                  aria-label="轨迹帧时间线"
                  className="w-full accent-console-cyan"
                  min={0}
                  max={Math.max(0, (evidence?.frame_count ?? 1) - 1)}
                  step={1}
                  value={Math.min(
                    frameIndex,
                    Math.max(0, (evidence?.frame_count ?? 1) - 1),
                  )}
                  disabled={!evidenceAvailable || (evidence?.frame_count ?? 0) < 2}
                  onChange={(event) => setFrameIndex(Number(event.target.value))}
                />
              </label>
            </div>
            <ScrollArea className="h-96">
              <div className="space-y-1 p-3">
                {(currentFrame?.targets ?? []).map((target) => (
                  <button
                    key={target.target_ref}
                    type="button"
                    className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition ${
                      selectedTarget?.target_ref === target.target_ref
                        ? "border-console-cyan bg-blue-50 text-blue-800"
                        : "border-console-line bg-white text-console-text hover:bg-console-panel2"
                    }`}
                    onClick={() => setTargetRef(target.target_ref)}
                  >
                    <span className="block font-medium">{target.label}</span>
                    <span className="mt-0.5 block text-xs text-console-muted">
                      {target.position
                        ? `${target.position.x}, ${target.position.y}`
                        : target.projection === "runtime_derived"
                          ? "已补回；坐标由 Fix Runtime 计算"
                          : "本帧缺失"}
                    </span>
                  </button>
                ))}
                {evidenceAvailable && !currentFrame?.targets.length && (
                  <p className="px-2 py-6 text-center text-sm text-console-muted">本帧没有公开目标。</p>
                )}
              </div>
            </ScrollArea>
          </ConsoleCard>

          <ConsoleCard className="min-w-0 p-0">
            <Tabs defaultValue="camera" className="gap-0">
              <div className="border-b border-console-line px-4 pt-3">
                <TabsList variant="line">
                  <TabsTrigger value="camera">相机投影</TabsTrigger>
                  <TabsTrigger value="gridmap">Gridmap / 轨迹</TabsTrigger>
                </TabsList>
              </div>
              <TabsContent value="camera" className="p-4">
                {renderableCamera ? (
                  <div className="flex min-h-96 items-center justify-center overflow-hidden rounded-lg bg-slate-950">
                    <img
                      src={renderableCamera.url}
                      width={renderableCamera.width}
                      height={renderableCamera.height}
                      alt={`第 ${frameIndex + 1} 帧相机投影`}
                      className="max-h-[36rem] max-w-full object-contain"
                    />
                  </div>
                ) : (
                  <div className="flex min-h-96 items-center justify-center rounded-lg border border-dashed border-console-line text-sm text-console-muted">
                    当前帧没有可公开的相机投影证据。
                  </div>
                )}
              </TabsContent>
              <TabsContent value="gridmap" className="space-y-4 p-4">
                {currentFrame?.gridmap ? (
                  <div className="flex min-h-48 items-center justify-center overflow-hidden rounded-lg border border-console-line bg-slate-950">
                    <img
                      src={currentFrame.gridmap.url}
                      width={currentFrame.gridmap.width}
                      height={currentFrame.gridmap.height}
                      alt={`第 ${frameIndex + 1} 帧 Gridmap 鸟瞰图`}
                      className="max-h-96 max-w-full object-contain [image-rendering:pixelated]"
                    />
                  </div>
                ) : (
                  <div className="flex min-h-36 items-center justify-center rounded-lg border border-dashed border-console-line text-sm text-console-muted">
                    当前帧没有可公开的 Gridmap 鸟瞰图。
                  </div>
                )}
                <TrajectoryCanvas
                  original={originalTrajectory}
                  current={projectedTrajectory}
                />
                <div className="flex gap-4 text-xs text-console-muted">
                  <span><span className="mr-1 inline-block h-0.5 w-5 bg-slate-500 align-middle" />原始位置序列</span>
                  <span><span className="mr-1 inline-block h-0.5 w-5 bg-blue-600 align-middle" />草稿命令投影</span>
                </div>
              </TabsContent>
            </Tabs>
          </ConsoleCard>

          <ConsoleCard className="p-0">
            <div className="border-b border-console-line p-4">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h3 className="text-sm font-semibold text-console-text">轨迹属性</h3>
                  <p className="mt-1 text-xs text-console-muted">{selectedTarget?.label ?? "未选择目标"}</p>
                </div>
                <span className="text-xs text-console-muted">
                  {saving ? "正在自动保存…" : editorDirty ? "等待自动保存" : "草稿已保存"}
                </span>
              </div>
            </div>
            <fieldset disabled={!editable || saving || terminal} className="space-y-4 p-4">
              <div className="grid grid-cols-2 gap-3">
                <label>
                  <span className="mb-1 block text-xs text-console-muted">位置 X</span>
                  <input
                    aria-label="位置 X"
                    inputMode="decimal"
                    value={editor.x}
                    onChange={(event) => setEditor((current) => ({ ...current, x: event.target.value }))}
                    className="h-9 w-full rounded-lg border border-console-line bg-white px-3 text-sm"
                  />
                </label>
                <label>
                  <span className="mb-1 block text-xs text-console-muted">位置 Y</span>
                  <input
                    aria-label="位置 Y"
                    inputMode="decimal"
                    value={editor.y}
                    onChange={(event) => setEditor((current) => ({ ...current, y: event.target.value }))}
                    className="h-9 w-full rounded-lg border border-console-line bg-white px-3 text-sm"
                  />
                </label>
              </div>
              <label className="block">
                <span className="mb-1 block text-xs text-console-muted">方向</span>
                <input
                  aria-label="方向"
                  inputMode="decimal"
                  value={editor.direction}
                  onChange={(event) => setEditor((current) => ({ ...current, direction: event.target.value }))}
                  className="h-9 w-full rounded-lg border border-console-line bg-white px-3 text-sm"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-console-muted">速度</span>
                <input
                  aria-label="速度"
                  inputMode="decimal"
                  min={0}
                  value={editor.speed}
                  onChange={(event) => setEditor((current) => ({ ...current, speed: event.target.value }))}
                  className="h-9 w-full rounded-lg border border-console-line bg-white px-3 text-sm"
                />
              </label>
              <label className="flex items-center gap-2 text-sm text-console-text">
                <Checkbox
                  checked={editor.pass}
                  onCheckedChange={(checked) => {
                    if (typeof checked !== "boolean" || !selectedTarget) return;
                    const previous = editor.pass;
                    setEditor((current) => ({ ...current, pass: checked }));
                    void runCommand({
                      kind: "toggle_pass",
                      frame_index: frameIndex,
                      value: checked,
                    }, () => setSavedEditor((current) => ({ ...current, pass: checked })))
                      .then((saved) => {
                        if (!saved) setEditor((current) => ({ ...current, pass: previous }));
                      });
                  }}
                />
                将本帧标记为 pass
              </label>
              <div className="grid grid-cols-2 gap-2 border-t border-console-line pt-4">
                <ConsoleButton
                  disabled={
                    !selectedTarget
                    || frameIndex < 1
                    || selectedTarget.present
                  }
                  onClick={() => selectedTarget && void runCommand({
                    kind: "add_missing_target",
                    frame_index: frameIndex,
                    target_ref: selectedTarget.target_ref,
                  })}
                >
                  <UserPlus aria-hidden="true" className="h-4 w-4" />
                  补回目标
                </ConsoleButton>
                <ConsoleButton
                  disabled={!selectedTarget || !selectedTarget.present}
                  onClick={() => selectedTarget && void runCommand({
                    kind: "delete_target",
                    frame_index: frameIndex,
                    target_ref: selectedTarget.target_ref,
                  })}
                >
                  <Trash2 aria-hidden="true" className="h-4 w-4" />
                  删除目标
                </ConsoleButton>
                <ConsoleButton
                  className="col-span-2"
                  onClick={() => void runCommand({
                    kind: "restore_frame",
                    frame_index: frameIndex,
                  })}
                >
                  <Undo2 aria-hidden="true" className="h-4 w-4" />
                  恢复当前帧
                </ConsoleButton>
              </div>
            </fieldset>

            {!terminal && review.status === "in_progress" && (
              <div className="space-y-2 border-t border-console-line p-4">
                <ConsoleButton
                  className="w-full justify-center"
                  variant="primary"
                  disabled={
                    !review.fix_draft
                    || editorDirty
                    || saving
                    || acting
                    || fixRuntimeBusy
                  }
                  onClick={() => void submitRevision()}
                >
                  <Save aria-hidden="true" className="h-4 w-4" />
                  提交 Fix 版本
                </ConsoleButton>
                <div className="grid grid-cols-3 gap-2">
                  <ConsoleButton
                    disabled={!revision || acting || fixRuntimeBusy}
                    onClick={() => setDecision("approve")}
                  >
                    <CheckCircle2 aria-hidden="true" className="h-4 w-4" />
                    通过
                  </ConsoleButton>
                  <ConsoleButton
                    disabled={acting || fixRuntimeBusy}
                    onClick={() => setDecision("return")}
                  >
                    <RotateCcw aria-hidden="true" className="h-4 w-4" />
                    退回
                  </ConsoleButton>
                  <ConsoleButton
                    disabled={acting || fixRuntimeBusy}
                    onClick={() => setDecision("discard")}
                  >
                    <Trash2 aria-hidden="true" className="h-4 w-4" />
                    废弃
                  </ConsoleButton>
                </div>
              </div>
            )}
          </ConsoleCard>
        </div>
      )}

      <AlertDialog open={decision !== null} onOpenChange={(open) => !open && setDecision(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {decision === "approve" ? "批准并发布 Fix 版本？" : decision === "return" ? "退回继续 Fix？" : "废弃该轨迹？"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {decision === "approve"
                ? "批准后会发布训练兼容文件，人工审核是唯一最终决策。"
                : decision === "return"
                  ? "复核任务会返回 Fix 工作台，已有 revision 和审计记录保留。"
                  : "废弃是终态；如需重新处理，应由 DataPilot 创建新的轨迹版本。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {decision !== "approve" && (
            <textarea
              aria-label={decision === "return" ? "退回原因" : "废弃原因"}
              className="min-h-24 w-full rounded-lg border border-console-line bg-white p-3 text-sm"
              maxLength={1000}
              placeholder="请填写原因"
              value={decisionReason}
              onChange={(event) => setDecisionReason(event.target.value)}
            />
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={acting}>取消</AlertDialogCancel>
            <AlertDialogAction
              disabled={
                acting
                || fixRuntimeBusy
                || (decision !== "approve" && !decisionReason.trim())
              }
              variant={decision === "discard" ? "destructive" : "default"}
              onClick={(event) => {
                event.preventDefault();
                void applyDecision();
              }}
            >
              {acting ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <Send aria-hidden="true" className="h-4 w-4" />}
              确认
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

export function TrajectoryFixPage() {
  const { reviewRef } = useParams<{ reviewRef: string }>();
  return (
    <TrajectoryFixWorkbench
      key={reviewRef ?? "missing-review"}
      reviewRef={reviewRef}
    />
  );
}
