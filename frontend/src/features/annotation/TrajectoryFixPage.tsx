import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Circle,
  CloudOff,
  LoaderCircle,
  Minus,
  PanelRightClose,
  PanelRightOpen,
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
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  useBeforeUnload,
  useBlocker,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useStore } from "zustand";

import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { cn } from "../../lib/utils";
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
import {
  buildReviewTransitionNotification,
  type ReviewTaskNoticeTone,
} from "./reviewNotifications";
import { trajectoryReviewPresentation } from "./reviewPresentation";
import { AnnotationWorkbenchLocation } from "./AnnotationWorkbenchLocation";
import { ReviewSegmentQueuePanel } from "./ReviewSegmentQueuePanel";
import type {
  CalibrationProfile,
  FixCommand,
  TrajectoryEvidenceGridmap,
  TrajectoryReview,
  TrajectoryReviewEvidence,
} from "./types";
import {
  cameraCanRender,
  evidenceMatchesReview,
  projectTrajectoryReviewEvidence,
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

type CommandFeedback = {
  successTitle?: string;
  failureTitle?: string;
};

type FixTaskNotice = {
  id: string;
  tone: ReviewTaskNoticeTone;
  title: string;
  detail?: string;
  occurredAt: string;
};

function safeFixError(error: unknown, fallback: string): string {
  if (error instanceof AnnotationApiError) {
    return error.detail?.code ? `${fallback}（${error.detail.code}）` : fallback;
  }
  const message = error instanceof Error ? error.message : "";
  return /(?:^|[\s("'`])\/(?:[^/\s]+\/){2,}|[A-Za-z]:\\/.test(message)
    ? fallback
    : message || fallback;
}

function fixActionNotice(
  review: TrajectoryReview,
  kind: string,
  title: string,
  tone: ReviewTaskNoticeTone,
  detail?: string,
  occurredAt?: string,
): FixTaskNotice {
  return {
    id: `annotation:review:${review.review_ref}:${kind}:${review.state_revision}`,
    tone,
    title,
    detail: detail ?? `${review.source_clip} · Segment ${String(review.segment_ordinal).padStart(2, "0")}`,
    occurredAt: occurredAt ?? (
      tone === "danger" || tone === "warning"
        ? new Date().toISOString()
        : review.updated_at
    ),
  };
}

function FixTaskNoticeBar({ notice }: { notice: FixTaskNotice }) {
  const toneClass = {
    success: "border-emerald-200 bg-emerald-50 text-emerald-800",
    info: "border-blue-200 bg-blue-50 text-blue-800",
    warning: "border-amber-200 bg-amber-50 text-amber-800",
    danger: "border-rose-200 bg-rose-50 text-rose-800",
  }[notice.tone];
  return (
    <div
      role={notice.tone === "danger" ? "alert" : "status"}
      className={cn(
        "flex min-w-0 max-w-[25rem] items-center gap-2 rounded-xl border px-3 py-2 text-xs shadow-[0_2px_8px_rgba(31,42,68,0.05)]",
        toneClass,
      )}
    >
      {notice.tone === "danger" || notice.tone === "warning"
        ? <AlertCircle aria-hidden="true" className="size-4 shrink-0" />
        : <CheckCircle2 aria-hidden="true" className="size-4 shrink-0" />}
      <span className="min-w-0">
        <strong className="block truncate font-semibold">{notice.title}</strong>
        {notice.detail && <span className="mt-0.5 block truncate opacity-80">{notice.detail}</span>}
      </span>
    </div>
  );
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

export function GridmapEvidenceView({
  gridmap,
  target,
  editable,
  fill = false,
  onPositionPreview,
  onDirectionPreview,
  onDragStateChange,
}: {
  gridmap: TrajectoryEvidenceGridmap;
  target: ProjectedTrajectoryTarget | null;
  editable: boolean;
  fill?: boolean;
  onPositionPreview: (x: number, y: number) => void;
  onDirectionPreview: (direction: number) => void;
  onDragStateChange: (dragging: boolean) => void;
}) {
  const [dragMode, setDragMode] = useState<"position" | "direction" | null>(
    null,
  );
  const toDisplay = useCallback((x: number, y: number) => {
    const xSpan = gridmap.x_range[1] - gridmap.x_range[0];
    const ySpan = gridmap.y_range[1] - gridmap.y_range[0];
    return {
      x: (1 - (y - gridmap.y_range[0]) / ySpan) * gridmap.width,
      y: (1 - (x - gridmap.x_range[0]) / xSpan) * gridmap.height,
    };
  }, [gridmap]);
  const fromPointer = useCallback((
    event: ReactPointerEvent<SVGSVGElement>,
  ) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const scale = Math.min(
      bounds.width / Math.max(gridmap.width, 1),
      bounds.height / Math.max(gridmap.height, 1),
    );
    const renderedWidth = gridmap.width * scale;
    const renderedHeight = gridmap.height * scale;
    const offsetX = (bounds.width - renderedWidth) / 2;
    const offsetY = (bounds.height - renderedHeight) / 2;
    const displayX = Math.min(
      gridmap.width,
      Math.max(0, (event.clientX - bounds.left - offsetX) / Math.max(scale, Number.EPSILON)),
    );
    const displayY = Math.min(
      gridmap.height,
      Math.max(0, (event.clientY - bounds.top - offsetY) / Math.max(scale, Number.EPSILON)),
    );
    const xSpan = gridmap.x_range[1] - gridmap.x_range[0];
    const ySpan = gridmap.y_range[1] - gridmap.y_range[0];
    return {
      x: gridmap.x_range[0]
        + (1 - displayY / gridmap.height) * xSpan,
      y: gridmap.y_range[0]
        + (1 - displayX / gridmap.width) * ySpan,
    };
  }, [gridmap]);
  const startDrag = (
    event: ReactPointerEvent<SVGElement>,
    mode: "position" | "direction",
  ) => {
    if (!editable || !target?.position) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.ownerSVGElement?.setPointerCapture?.(event.pointerId);
    setDragMode(mode);
    onDragStateChange(true);
  };
  const moveDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragMode || !target?.position) return;
    event.preventDefault();
    const next = fromPointer(event);
    if (dragMode === "position") {
      onPositionPreview(next.x, next.y);
    } else {
      onDirectionPreview(
        Math.atan2(
          next.y - target.position.y,
          next.x - target.position.x,
        ),
      );
    }
  };
  const finishDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragMode) return;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    }
    setDragMode(null);
    onDragStateChange(false);
  };
  const trajectory = (target?.trajectory_points ?? []).map((point) => (
    toDisplay(point[0], point[1])
  ));
  const baseTrajectory = (target?.base_trajectory_points ?? []).map((point) => (
    toDisplay(point[0], point[1])
  ));
  const original = target?.original_position
    ? toDisplay(target.original_position.x, target.original_position.y)
    : null;
  const current = target?.position
    ? toDisplay(target.position.x, target.position.y)
    : null;
  const dog = toDisplay(0, 0);
  const directionEnd = (
    target?.position
    && target.direction !== null
  ) ? toDisplay(
      target.position.x + Math.cos(target.direction) * 0.6,
      target.position.y + Math.sin(target.direction) * 0.6,
    ) : null;
  const directionGeometry = current && directionEnd
    ? (() => {
        const deltaX = directionEnd.x - current.x;
        const deltaY = directionEnd.y - current.y;
        const length = Math.hypot(deltaX, deltaY);
        if (length < Number.EPSILON) return null;
        const unitX = deltaX / length;
        const unitY = deltaY / length;
        const normalX = -unitY;
        const normalY = unitX;
        const arrowLength = Math.max(4.5, gridmap.width / 90);
        const arrowHalfWidth = Math.max(2.5, gridmap.width / 170);
        const baseX = directionEnd.x - unitX * arrowLength;
        const baseY = directionEnd.y - unitY * arrowLength;
        return {
          arrowPoints: [
            `${directionEnd.x},${directionEnd.y}`,
            `${baseX + normalX * arrowHalfWidth},${baseY + normalY * arrowHalfWidth}`,
            `${baseX - normalX * arrowHalfWidth},${baseY - normalY * arrowHalfWidth}`,
          ].join(" "),
          handleRadius: Math.max(1.75, gridmap.width / 180),
          hitRadius: Math.max(5, gridmap.width / 85),
        };
      })()
    : null;
  const trajectoryPoints = trajectory
    .map((point) => `${point.x},${point.y}`)
    .join(" ");

  return (
    <div
      className={cn(
        "relative mx-auto w-full overflow-hidden bg-slate-950",
        fill ? "h-full" : "max-w-[62rem] rounded-xl border border-console-line",
      )}
      style={fill ? undefined : { aspectRatio: `${gridmap.width} / ${gridmap.height}` }}
      aria-label="当前帧 Gridmap 与轨迹证据"
    >
      <img
        src={gridmap.url}
        width={gridmap.width}
        height={gridmap.height}
        alt="当前帧 Gridmap 鸟瞰图"
        className="absolute inset-0 h-full w-full object-contain [image-rendering:pixelated]"
      />
      <svg
        aria-label={editable ? "可拖动的当前帧 Gridmap 与轨迹" : "当前帧 Gridmap 与轨迹"}
        className="absolute inset-0 h-full w-full touch-none select-none"
        viewBox={`0 0 ${gridmap.width} ${gridmap.height}`}
        preserveAspectRatio="xMidYMid meet"
        onPointerMove={moveDrag}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
      >
        {baseTrajectory.length > 1 && (
          <polyline
            points={baseTrajectory
              .map((point) => `${point.x},${point.y}`)
              .join(" ")}
            fill="none"
            stroke="#64748b"
            strokeDasharray="5 4"
            strokeWidth={Math.max(1.25, gridmap.width / 180)}
            vectorEffect="non-scaling-stroke"
          />
        )}
        {trajectory.length > 1 && (
          <polyline
            points={trajectoryPoints}
            fill="none"
            stroke="#2563eb"
            strokeWidth={Math.max(1.5, gridmap.width / 150)}
            vectorEffect="non-scaling-stroke"
          />
        )}
        <circle
          cx={dog.x}
          cy={dog.y}
          r={Math.max(2.5, gridmap.width / 80)}
          fill="#0f172a"
          stroke="#ffffff"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
        />
        {original && (
          <circle
            cx={original.x}
            cy={original.y}
            r={Math.max(3, gridmap.width / 65)}
            fill="#ffffff"
            stroke="#475569"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {current && directionEnd && directionGeometry && (
          <line
            x1={current.x}
            y1={current.y}
            x2={directionEnd.x}
            y2={directionEnd.y}
            stroke="#f97316"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {editable && directionEnd && directionGeometry && (
          <polygon
            points={directionGeometry.arrowPoints}
            fill="#f97316"
            stroke="#ffffff"
            strokeWidth="0.75"
            vectorEffect="non-scaling-stroke"
            pointerEvents="none"
          />
        )}
        {directionEnd && directionGeometry && (
          <circle
            cx={directionEnd.x}
            cy={directionEnd.y}
            r={directionGeometry.handleRadius}
            fill="#ffffff"
            stroke="#f97316"
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
            pointerEvents="none"
          />
        )}
        {directionEnd && directionGeometry && (
          <circle
            cx={directionEnd.x}
            cy={directionEnd.y}
            r={directionGeometry.hitRadius}
            fill="transparent"
            stroke="none"
            pointerEvents="all"
            className={editable ? "cursor-grab active:cursor-grabbing" : ""}
            aria-label="拖动目标方向"
            onPointerDown={(event) => startDrag(event, "direction")}
          />
        )}
        {current && (
          <circle
            cx={current.x}
            cy={current.y}
            r={Math.max(2.5, gridmap.width / 80)}
            fill="#f97316"
            stroke="#ffffff"
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
            className={editable ? "cursor-grab active:cursor-grabbing" : ""}
            aria-label={editable ? "拖动目标位置" : undefined}
            onPointerDown={editable ? (event) => startDrag(event, "position") : undefined}
          />
        )}
      </svg>
    </div>
  );
}

export function CameraEvidenceView({
  frameIndex,
  camera,
  projection,
  target,
  fixRevision,
  authoritativeProjection = false,
  fill = false,
}: {
  frameIndex: number;
  camera: TrajectoryReviewEvidence["frames"][number]["camera"];
  projection: TrajectoryReviewEvidence["frames"][number]["projection"];
  target: ProjectedTrajectoryTarget | null;
  fixRevision: boolean;
  authoritativeProjection?: boolean;
  fill?: boolean;
}) {
  const source = authoritativeProjection
    ? projection ?? camera
    : fixRevision ? camera : projection ?? camera;
  if (!cameraCanRender(source)) {
    return (
      <div className={cn(
        "flex min-h-96 items-center justify-center border border-dashed border-console-line text-sm text-console-muted",
        fill ? "h-full" : "rounded-xl",
      )}>
        当前帧没有可公开的相机投影证据。
      </div>
    );
  }
  const renderOverlay = (
    fixRevision
    && !authoritativeProjection
    && cameraCanRender(camera)
    && camera.width !== null
    && camera.height !== null
  );
  const compositeProjection = Boolean(
    projection !== null
    && source === projection
    && source.width !== null
    && source.height !== null
    && source.width / source.height > 2,
  );
  const trajectory = target?.camera_trajectory_points ?? [];
  const position = target?.camera_position ?? null;
  const imageLabel = `第 ${frameIndex + 1} 帧${fixRevision ? "Fix 结果" : "原后处理"}投影`;
  return (
    <div
      className={cn(
        "relative mx-auto w-full overflow-hidden bg-slate-950",
        fill ? "h-full" : "rounded-xl",
      )}
      style={fill ? undefined : {
        aspectRatio: compositeProjection
          ? `${(source.width ?? 2) / 2} / ${source.height ?? 1}`
          : `${source.width ?? 16} / ${source.height ?? 9}`,
      }}
    >
      {compositeProjection && source.width !== null && source.height !== null ? (
        <svg
          role="img"
          aria-label={imageLabel}
          data-evidence-layout="legacy-composite-camera"
          className="absolute inset-0 h-full w-full"
          viewBox={`${source.width / 2} 0 ${source.width / 2} ${source.height}`}
          preserveAspectRatio="xMidYMid meet"
        >
          <title>{imageLabel}</title>
          <image
            href={source.url}
            width={source.width}
            height={source.height}
            preserveAspectRatio="none"
          />
        </svg>
      ) : (
        <img
          src={source.url}
          width={source.width ?? undefined}
          height={source.height ?? undefined}
          alt={imageLabel}
          className="absolute inset-0 h-full w-full object-contain"
        />
      )}
      {renderOverlay && (
        <svg
          aria-hidden="true"
          className="absolute inset-0 h-full w-full"
          viewBox={`0 0 ${camera.width} ${camera.height}`}
        >
          {trajectory.length > 1 && (
            <polyline
              points={trajectory.map((point) => point.join(",")).join(" ")}
              fill="none"
              stroke="#22c55e"
              strokeWidth="3"
              vectorEffect="non-scaling-stroke"
            />
          )}
          {position && (
            <circle
              cx={position[0]}
              cy={position[1]}
              r="7"
              fill="#f97316"
              stroke="#ffffff"
              strokeWidth="2"
              vectorEffect="non-scaling-stroke"
            />
          )}
        </svg>
      )}
    </div>
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
  const [geometryDragging, setGeometryDragging] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [differenceReason, setDifferenceReason] = useState("");
  const [frameIndex, setFrameIndex] = useState(0);
  const [frameInput, setFrameInput] = useState("1");
  const [frameInputError, setFrameInputError] = useState("");
  const [targetRef, setTargetRef] = useState("");
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [taskNotice, setTaskNotice] = useState<FixTaskNotice | null>(null);
  const [editor, setEditor] = useState<EditableTarget>(() => targetEditor(null));
  const [savedEditor, setSavedEditor] = useState<EditableTarget>(() => targetEditor(null));
  const [decision, setDecision] = useState<Decision>(null);
  const [decisionReason, setDecisionReason] = useState("");
  const reviewStateRef = useRef<TrajectoryReview | null>(null);
  const conflictEditorRef = useRef<{ targetRef: string; editor: EditableTarget } | null>(null);
  const initialRefreshCompleteRef = useRef(false);

  useEffect(() => {
    reviewStateRef.current = review;
  }, [review]);

  const showTaskNotice = useCallback((notice: FixTaskNotice) => {
    setTaskNotice(notice);
  }, []);

  useEffect(() => {
    if (!taskNotice) return;
    const timeout = window.setTimeout(() => {
      setTaskNotice((current) => current?.id === taskNotice.id ? null : current);
    }, taskNotice.tone === "danger" ? 7_000 : 4_800);
    return () => window.clearTimeout(timeout);
  }, [taskNotice]);

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
      if (item.dataset_date !== review.dataset_date) {
        continue;
      }
      const current = matching.get(item.review_ref);
      if (!current || current.state_revision <= item.state_revision) {
        matching.set(item.review_ref, item);
      }
    }
    setReviewQueue(
      [...matching.values()].sort((left, right) => (
        left.source_clip.localeCompare(right.source_clip)
        || left.segment_ordinal - right.segment_ordinal
        || left.review_ref.localeCompare(right.review_ref)
      )),
    );
  }, [projectedReviews, review]);

  const loadEvidence = useCallback(async (
    expectedReview?: TrajectoryReview,
  ): Promise<boolean> => {
    if (!reviewRef) return false;
    const owner = expectedReview ?? reviewStateRef.current;
    try {
      const next = await getTrajectoryReviewEvidence(reviewRef);
      if (!owner || !evidenceMatchesReview(next, owner)) {
        throw new Error("轨迹证据版本与当前复核任务不一致，请刷新后重试。");
      }
      setEvidence(next);
      setEvidenceError("");
      return true;
    } catch (requestError) {
      const safeError = safeFixError(
        requestError,
        "服务器尚未提供该轨迹版本的公开证据，当前不能执行几何修正。",
      );
      setEvidence(null);
      setEvidenceError(safeError);
      if (owner) showTaskNotice(fixActionNotice(
        owner,
        "evidence-error",
        "轨迹证据加载失败",
        "danger",
        safeError,
      ));
      return false;
    }
  }, [reviewRef, showTaskNotice]);

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
    const transition = buildReviewTransitionNotification(current, projectedReview);
    if (transition) {
      showTaskNotice({
        id: transition.dedupeKey,
        tone: transition.tone,
        title: transition.title,
        detail: transition.detail,
        occurredAt: transition.occurredAt,
      });
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
  }, [loadEvidence, projectedReview, showTaskNotice]);

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
            .filter((item) => item.dataset_date === owner.dataset_date)
            .sort((left, right) => (
              left.source_clip.localeCompare(right.source_clip)
              || left.segment_ordinal - right.segment_ordinal
              || left.review_ref.localeCompare(right.review_ref)
            ));
          setReviewQueue(
            [
              ...matching.filter((item) => item.review_ref !== owner.review_ref),
              owner,
            ].sort((left, right) => (
              left.source_clip.localeCompare(right.source_clip)
              || left.segment_ordinal - right.segment_ordinal
              || left.review_ref.localeCompare(right.review_ref)
            )),
          );
          setReviewQueueError("");
        } catch (requestError) {
          setReviewQueue([owner]);
          setReviewQueueError(safeFixError(
            requestError,
            "读取同一数据日期的 Segment 队列失败",
          ));
        }
      } else {
        setReviewQueue((current) => (
          [
            ...current.filter((item) => item.review_ref !== owner.review_ref),
            owner,
          ].sort((left, right) => (
            left.source_clip.localeCompare(right.source_clip)
            || left.segment_ordinal - right.segment_ordinal
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
  const gridmapTarget = useMemo(() => {
    if (!selectedTarget) return null;
    const x = finiteValue(editor.x);
    const y = finiteValue(editor.y);
    const direction = finiteValue(editor.direction);
    return {
      ...selectedTarget,
      position: (
        selectedTarget.present && x !== null && y !== null
          ? { x, y }
          : selectedTarget.position
      ),
      direction: direction ?? selectedTarget.direction,
    };
  }, [editor.direction, editor.x, editor.y, selectedTarget]);

  useEffect(() => {
    if (!currentFrame) return;
    if (frameIndex !== currentFrame.frame_index) setFrameIndex(currentFrame.frame_index);
    const nextTarget = currentFrame.targets.find((target) => target.target_ref === targetRef)
      ?? currentFrame.targets[0]
      ?? null;
    if (nextTarget && targetRef !== nextTarget.target_ref) setTargetRef(nextTarget.target_ref);
    const nextEditor = targetEditor(nextTarget, currentFrame.pass);
    const preservedConflictEditor = conflictEditorRef.current;
    // 409 冲突刷新权威数据后，保留操作员尚未提交的输入，避免一次对账清空编辑现场。
    if (preservedConflictEditor && nextTarget?.target_ref === preservedConflictEditor.targetRef) {
      setEditor(preservedConflictEditor.editor);
      conflictEditorRef.current = null;
    } else {
      setEditor(nextEditor);
      if (preservedConflictEditor) conflictEditorRef.current = null;
    }
    setSavedEditor(nextEditor);
  }, [
    currentFrame?.frame_index,
    currentFrame?.pass,
    projectedEvidence?.draft_revision,
    projectedEvidence?.review_state_revision,
    targetRef,
  ]);

  const editorDirty = JSON.stringify(editor) !== JSON.stringify(savedEditor);
  const pendingSetupDirty = Boolean(
    review
    && (review.status === "pending" || review.status === "returned")
    && (
      selectedProfile !== (review.fix_draft?.calibration.profile_ref ?? "")
      || differenceReason !== (review.fix_draft?.calibration.difference_reason ?? "")
    )
  );
  const pageDirty = editorDirty || saving || pendingSetupDirty;
  const evidenceAvailable = evidence !== null && evidenceError === "";
  const fixRuntimeBusy = (
    review?.active_fix_run?.status === "queued"
    || review?.active_fix_run?.status === "running"
  );
  const publicationBusy = (
    review?.status === "approved"
    && review.latest_publication?.status === "publishing"
  );
  const currentClipQueue = review
    ? reviewQueue.filter((item) => item.source_clip === review.source_clip)
    : [];
  const reviewQueueIndex = review
    ? currentClipQueue.findIndex((item) => item.review_ref === review.review_ref)
    : -1;
  const clipSegmentOrdinal = review
    ? (reviewQueueIndex >= 0 ? reviewQueueIndex + 1 : review.segment_ordinal)
    : 1;
  const editable = Boolean(
    review?.status === "in_progress"
    && review.fix_draft
    && evidenceAvailable
    && selectedTarget
    && !conflict
    && !acting
    && !fixRuntimeBusy
  );
  const frameCount = evidence?.frame_count ?? 0;
  const frameNavigationLocked = Boolean(
    // 切帧会替换当前编辑器上下文；存在未保存修改或 Runtime 正在运行时必须冻结导航。
    !evidenceAvailable
    || editorDirty
    || saving
    || geometryDragging
    || conflict
    || acting
    || fixRuntimeBusy
  );

  useEffect(() => {
    setFrameInput(String(frameIndex + 1));
    setFrameInputError("");
  }, [frameIndex, frameCount]);

  const goToFrame = useCallback((nextIndex: number) => {
    // 所有按钮、滑杆和数字输入统一走这个入口，保证边界与脏数据保护口径一致。
    if (frameNavigationLocked || frameCount < 1) return false;
    if (!Number.isSafeInteger(nextIndex) || nextIndex < 0 || nextIndex >= frameCount) {
      return false;
    }
    setFrameIndex(nextIndex);
    setFrameInput(String(nextIndex + 1));
    setFrameInputError("");
    return true;
  }, [frameCount, frameNavigationLocked]);

  const commitFrameInput = useCallback(() => {
    if (!/^\d+$/.test(frameInput)) {
      setFrameInput(String(frameIndex + 1));
      setFrameInputError(`请输入 1 至 ${Math.max(frameCount, 1)} 的整数`);
      return;
    }
    const nextFrame = Number(frameInput);
    if (!Number.isSafeInteger(nextFrame) || nextFrame < 1 || nextFrame > frameCount) {
      setFrameInput(String(frameIndex + 1));
      setFrameInputError(`帧序号范围为 1 至 ${Math.max(frameCount, 1)}`);
      return;
    }
    if (!goToFrame(nextFrame - 1)) {
      setFrameInput(String(frameIndex + 1));
      setFrameInputError("当前状态正在保存或处理，暂时不能切换帧");
    }
  }, [frameCount, frameIndex, frameInput, goToFrame]);

  const updateFromResult = useCallback((next: TrajectoryReview, suppressNotification = false) => {
    const transition = buildReviewTransitionNotification(reviewStateRef.current, next);
    if (transition && !suppressNotification) {
      showTaskNotice({
        id: transition.dedupeKey,
        tone: transition.tone,
        title: transition.title,
        detail: transition.detail,
        occurredAt: transition.occurredAt,
      });
    }
    cacheTrajectoryReview(next);
    setReview(next);
    reviewStateRef.current = next;
    setConflict(false);
  }, [showTaskNotice]);

  const runCommand = useCallback(async (
    command: FixCommand,
    onSaved?: () => void,
    feedback?: CommandFeedback,
  ) => {
    const current = reviewStateRef.current;
    if (
      !reviewRef
      || !current?.fix_draft
      || current.status !== "in_progress"
      || current.active_fix_run?.status === "queued"
      || current.active_fix_run?.status === "running"
      || conflict
    ) {
      return false;
    }
    setSaving(true);
    setError("");
    try {
      // expected_* revision 是 Fix 写操作的 CAS 条件；服务端返回 409 时进入冲突恢复流程。
      const next = await applyFixCommand(reviewRef, {
        expected_review_revision: current.state_revision,
        expected_draft_revision: current.fix_draft.revision,
        command,
      });
      updateFromResult(next);
      await loadEvidence(next);
      onSaved?.();
      if (feedback?.successTitle) {
        showTaskNotice(fixActionNotice(
          next,
          `command-${command.kind}`,
          feedback.successTitle,
          "success",
        ));
      }
      return true;
    } catch (requestError) {
      if (requestError instanceof AnnotationApiError && requestError.status === 409) {
        setConflict(true);
      }
      const safeError = safeFixError(requestError, "保存 Fix 修改失败");
      setError(safeError);
      if (feedback?.failureTitle) {
        const owner = reviewStateRef.current;
        if (owner) {
          showTaskNotice(fixActionNotice(
            owner,
            `command-${command.kind}-failed`,
            feedback.failureTitle,
            "danger",
            safeError,
            new Date().toISOString(),
          ));
        }
      }
      return false;
    } finally {
      setSaving(false);
    }
  }, [conflict, loadEvidence, reviewRef, showTaskNotice, updateFromResult]);

  useEffect(() => {
    if (
      !editable
      || !editorDirty
      || saving
      || conflict
      || geometryDragging
      || !selectedTarget
    ) return;
    // 位置、方向和速度共用约 700ms 防抖自动保存；每次只发送首个发生变化的字段，
    // 等服务端返回新 revision 后再处理后续变化，避免并行命令使用同一旧 revision。
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
    geometryDragging,
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

  const selectedCalibration = profiles.find((profile) => profile.profile_ref === selectedProfile)
    ?? (
      review.fix_draft?.calibration.profile_ref === selectedProfile
        ? review.fix_draft.calibration
        : undefined
    );
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
  const currentProjection = currentFrame?.projection ?? null;
  const previewMatchesDraft = Boolean(
    revision
    && review.fix_draft
    && revision.source_draft_revision === review.fix_draft.revision
    && evidence?.evidence_kind === "fix_revision"
    && evidence.fix_revision_ref === revision.revision_ref,
  );
  // 只有证据引用的 Fix revision 与当前草稿 revision 完全一致，才允许作出“通过”结论。
  // 这是前端的 fail-closed 检查，不能用“曾生成过预览”替代。
  const previewIsStale = Boolean(revision && !previewMatchesDraft);
  const previewNotice: FixTaskNotice | null = previewMatchesDraft
    ? {
        id: `preview-ready:${revision?.revision_ref ?? review.state_revision}`,
        tone: "success",
        title: "权威 Fix 预览已就绪",
        detail: "当前展示与草稿一致，可据此作出复核结论。",
        occurredAt: revision?.created_at ?? review.updated_at,
      }
    : previewIsStale
      ? {
          id: `preview-stale:${revision?.revision_ref ?? review.state_revision}`,
          tone: "warning",
          title: "Fix 预览已过期",
          detail: "请重新生成预览后再通过。",
          occurredAt: review.updated_at,
        }
      : null;
  const headerNotice = taskNotice ?? previewNotice;

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
      const safeError = safeFixError(requestError, "启动人工 Fix 失败");
      setError(safeError);
      showTaskNotice(fixActionNotice(
        review,
        "fix-session-failed",
        "创建 Fix 草稿失败",
        "danger",
        safeError,
      ));
    } finally {
      setActing(false);
    }
  };

  const submitRevision = async () => {
    if (!review.fix_draft || editorDirty || saving || fixRuntimeBusy) return;
    setActing(true);
    setError("");
    try {
      const next = await createFixRevision(review.review_ref, {
        expected_review_revision: review.state_revision,
        expected_draft_revision: review.fix_draft.revision,
      });
      updateFromResult(next);
      await loadEvidence(next);
    } catch (requestError) {
      const safeError = safeFixError(requestError, "提交 Fix 版本失败");
      setError(safeError);
      showTaskNotice(fixActionNotice(
        review,
        "fix-preview-submit-failed",
        "Fix 预览提交失败",
        "danger",
        safeError,
      ));
    } finally {
      setActing(false);
    }
  };

  const restoreMissingTarget = async () => {
    if (!selectedTarget || frameIndex < 1) return;
    const saved = await runCommand(
      {
        kind: "add_missing_target",
        frame_index: frameIndex,
        target_ref: selectedTarget.target_ref,
      },
      undefined,
      { failureTitle: "补回目标失败" },
    );
    if (!saved) return;
    // 补回目标会改变整段权威轨迹：保存命令成功后立即串联生成 Fix 预览，
    // 避免界面停留在“目标已恢复但证据仍是旧版本”的中间状态。
    const current = reviewStateRef.current;
    if (
      !current?.fix_draft
      || current.status !== "in_progress"
      || current.active_fix_run?.status === "queued"
      || current.active_fix_run?.status === "running"
    ) {
      return;
    }
    setActing(true);
    setError("");
    try {
      const next = await createFixRevision(current.review_ref, {
        expected_review_revision: current.state_revision,
        expected_draft_revision: current.fix_draft.revision,
      });
      updateFromResult(next, true);
      await loadEvidence(next);
      showTaskNotice(fixActionNotice(
        next,
        "target-restored-preview-queued",
        "目标已补回，Fix 预览已进入生成队列",
        "info",
      ));
    } catch (requestError) {
      const safeError = safeFixError(
        requestError,
        "补回目标已保存，但生成权威 Fix 预览失败，请重试生成预览。",
      );
      setError(safeError);
      const owner = reviewStateRef.current;
      if (owner) {
        showTaskNotice(fixActionNotice(
          owner,
          "target-restored-preview-failed",
          "目标已补回，但 Fix 预览提交失败",
          "warning",
          safeError,
        ));
      }
    } finally {
      setActing(false);
    }
  };

  const applyDecision = async () => {
    // 通过必须绑定当前权威 Fix revision；退回和废弃必须携带人工原因。
    if (!decision || fixRuntimeBusy || pageDirty || geometryDragging || conflict) return;
    if (decision === "approve" && (!revision || !previewMatchesDraft)) return;
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
      const safeError = safeFixError(requestError, "提交复核结论失败");
      setError(safeError);
      showTaskNotice(fixActionNotice(
        review,
        `decision-${decision}-failed`,
        "提交复核结论失败",
        "danger",
        safeError,
      ));
    } finally {
      setActing(false);
    }
  };

  return (
    <section className="mx-auto max-w-[1900px] space-y-4 px-3 pb-28 pt-4 md:px-4 lg:px-5">
      <DirtyNavigationGuard dirty={pageDirty} />

      <AnnotationWorkbenchLocation
        datasetDate={review.dataset_date}
        sourceClip={review.source_clip}
        segmentOrdinal={clipSegmentOrdinal}
        segmentCount={Math.max(currentClipQueue.length, 1)}
        statusLabel={reviewPresentation.label}
        statusTone={reviewPresentation.tone}
        backLabel="返回人工复核"
        navigationLabel="Fix 工作台位置"
        onBack={() => navigate("/annotation/reviews")}
        actions={(headerNotice || review.latest_publication?.status === "failed") ? (
          <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
            {headerNotice && <FixTaskNoticeBar notice={headerNotice} />}
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
                  const safeError = safeFixError(requestError, "重试发布失败");
                  setError(safeError);
                  showTaskNotice(fixActionNotice(
                    review,
                    "publication-retry-failed",
                    "重新提交发布失败",
                    "danger",
                    safeError,
                  ));
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
        ) : undefined}
      />

      {reviewQueueError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <span>{reviewQueueError}</span>
          <ConsoleButton onClick={() => void refresh(false, true, true)}>重新加载 Segment 队列</ConsoleButton>
        </div>
      )}

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
                conflictEditorRef.current = null;
                setConflict(false);
                void refresh();
              }}
            >
              使用服务器版本
            </ConsoleButton>
            <ConsoleButton
              variant="primary"
              disabled={!selectedTarget}
              onClick={async () => {
                try {
                  if (selectedTarget) {
                    conflictEditorRef.current = {
                      targetRef: selectedTarget.target_ref,
                      editor,
                    };
                  }
                  const next = await loadTrajectoryReview(review.review_ref, {
                    force: true,
                  });
                  updateFromResult(next);
                  await loadEvidence(next);
                } catch (requestError) {
                  conflictEditorRef.current = null;
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
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-semibold text-amber-900">轨迹证据不可用</h3>
              <p className="mt-1 text-sm text-amber-800">{evidenceError}</p>
              <p className="mt-1 text-xs text-amber-700">
                系统不会构造替代数据；相机、gridmap 和领域 Fix 命令暂时禁用。
              </p>
            </div>
            <ConsoleButton
              className="shrink-0"
              disabled={acting}
              onClick={() => void loadEvidence(review)}
            >
              重新加载证据
            </ConsoleButton>
          </div>
        </div>
      )}

      {(review.status === "pending" || review.status === "returned") && (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
          <ReviewSegmentQueuePanel
            reviews={reviewQueue}
            currentReviewRef={review.review_ref}
            className="min-h-[28rem] lg:max-h-[38rem]"
            disabled={pageDirty || acting || conflict || fixRuntimeBusy}
            onNavigate={(nextReviewRef) => {
              if (nextReviewRef !== review.review_ref) {
                navigate(`/annotation/reviews/${encodeURIComponent(nextReviewRef)}`);
              }
            }}
          />
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
        </div>
      )}

      {(review.status === "in_progress" || terminal) && (
        <div className="min-w-0 space-y-4">
          <div
            className="fix-workbench relative isolate min-h-[44rem] overflow-hidden rounded-[16px] border border-console-line bg-[#edf0f5] shadow-[0_10px_30px_rgba(31,42,68,0.06)] xl:h-[calc(100dvh-13.5rem)] xl:min-h-[46rem]"
            data-inspector-state={inspectorCollapsed ? "closed" : "open"}
            data-focus-mode={geometryDragging ? "true" : "false"}
          >
            <div className="relative z-20 flex min-h-14 flex-wrap items-center gap-2 border-b border-console-line bg-white/96 px-3 py-2.5 backdrop-blur-sm sm:px-4">
              <span className="shrink-0 text-xs font-medium text-console-muted">Frame</span>
              <label className="relative flex h-9 shrink-0 items-center rounded-lg border border-console-line bg-white focus-within:border-[#3156c8] focus-within:ring-2 focus-within:ring-[#3156c8]/15">
                <span className="sr-only">当前帧</span>
                <input
                  aria-label="当前帧"
                  aria-invalid={frameInputError ? "true" : undefined}
                  aria-describedby={frameInputError ? "fix-frame-error" : undefined}
                  className="h-full w-16 rounded-lg bg-transparent px-2 text-right text-sm font-semibold tabular-nums text-console-text outline-none disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={frameNavigationLocked || frameCount < 1}
                  inputMode="numeric"
                  min={1}
                  max={Math.max(frameCount, 1)}
                  step={1}
                  value={frameInput}
                  onChange={(event) => {
                    setFrameInput(event.target.value);
                    setFrameInputError("");
                  }}
                  onBlur={commitFrameInput}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      commitFrameInput();
                    } else if (event.key === "Escape") {
                      setFrameInput(String(frameIndex + 1));
                      setFrameInputError("");
                    }
                  }}
                />
                <span className="pr-2 text-xs tabular-nums text-console-muted">/ {frameCount}</span>
              </label>
              <div className="flex items-center gap-1">
                <ConsoleButton aria-label="第一帧" disabled={frameNavigationLocked || frameIndex <= 0} onClick={() => void goToFrame(0)}>
                  <ChevronsLeft aria-hidden="true" className="size-4" />
                </ConsoleButton>
                <ConsoleButton aria-label="上一帧" disabled={frameNavigationLocked || frameIndex <= 0} onClick={() => void goToFrame(frameIndex - 1)}>
                  <ChevronLeft aria-hidden="true" className="size-4" />
                </ConsoleButton>
                <ConsoleButton aria-label="下一帧" disabled={frameNavigationLocked || frameIndex >= frameCount - 1} onClick={() => void goToFrame(frameIndex + 1)}>
                  <ChevronRight aria-hidden="true" className="size-4" />
                </ConsoleButton>
                <ConsoleButton aria-label="最后一帧" disabled={frameNavigationLocked || frameIndex >= frameCount - 1} onClick={() => void goToFrame(frameCount - 1)}>
                  <ChevronsRight aria-hidden="true" className="size-4" />
                </ConsoleButton>
              </div>
              <label className="min-w-36 flex-1 sm:min-w-56">
                <span className="sr-only">轨迹帧时间线</span>
                <input
                  type="range"
                  aria-label="轨迹帧时间线"
                  className="w-full accent-console-cyan"
                  min={0}
                  max={Math.max(0, frameCount - 1)}
                  step={1}
                  value={Math.min(frameIndex, Math.max(0, frameCount - 1))}
                  disabled={frameNavigationLocked || frameCount < 2}
                  onChange={(event) => void goToFrame(Number(event.target.value))}
                />
              </label>
              <label className="hidden min-w-40 sm:block">
                <span className="sr-only">当前目标</span>
                <Select
                  disabled={frameNavigationLocked}
                  value={selectedTarget?.target_ref ?? ""}
                  onValueChange={setTargetRef}
                >
                  <SelectTrigger aria-label="当前目标" className="h-9 bg-white">
                    <SelectValue placeholder="暂无目标" />
                  </SelectTrigger>
                  <SelectContent>
                    {(currentFrame?.targets ?? []).map((target) => (
                      <SelectItem key={target.target_ref} value={target.target_ref}>{target.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <span id="fix-frame-error" aria-live="polite" className="basis-full text-xs text-rose-600 empty:hidden">
                {frameInputError}
              </span>
            </div>

            <div className="grid h-[calc(100%-3.5rem)] min-h-[40rem] grid-rows-[minmax(13rem,1fr)_minmax(24rem,2fr)] overflow-hidden lg:grid-cols-[minmax(13rem,34fr)_minmax(0,66fr)] lg:grid-rows-1">
              <section className="relative min-h-0 overflow-hidden border-b border-console-line bg-slate-950 lg:border-b-0 lg:border-r" aria-label="相机投影证据">
                <div className="pointer-events-none absolute left-3 top-3 z-10 rounded-lg bg-slate-950/72 px-3 py-2 text-white backdrop-blur-sm">
                  <h3 className="text-sm font-semibold">相机投影证据</h3>
                  <p className="mt-0.5 text-[11px] text-white/70">
                    {evidence?.evidence_kind === "fix_revision"
                      ? "使用所选 Fix 标定，由冻结 Runtime 投影"
                      : "原后处理投影；生成 Fix 预览后更新"}
                  </p>
                </div>
                <CameraEvidenceView
                  fill
                  frameIndex={frameIndex}
                  camera={currentCamera}
                  projection={currentProjection}
                  target={selectedTarget}
                  fixRevision={evidence?.evidence_kind === "fix_revision"}
                />
              </section>

              <section className="relative min-h-0 overflow-hidden bg-slate-950" aria-label="Gridmap 与轨迹证据">
                <div className="pointer-events-none absolute left-3 top-3 z-10 rounded-lg bg-slate-950/72 px-3 py-2 text-white backdrop-blur-sm">
                  <h3 className="text-sm font-semibold">Gridmap / 轨迹证据</h3>
                  <p className="mt-0.5 text-[11px] text-white/70">
                    {geometryDragging
                      ? "专注调整中：其他控件已锁定，松开后自动保存"
                      : "拖动目标修改位置，拖动方向端点修改朝向"}
                  </p>
                </div>
                {currentFrame?.gridmap ? (
                  <GridmapEvidenceView
                    fill
                    gridmap={currentFrame.gridmap}
                    target={gridmapTarget}
                    editable={editable && Boolean(selectedTarget?.present)}
                    onDragStateChange={setGeometryDragging}
                    onPositionPreview={(x, y) => setEditor((current) => ({
                      ...current,
                      x: String(Number(x.toFixed(6))),
                      y: String(Number(y.toFixed(6))),
                    }))}
                    onDirectionPreview={(direction) => setEditor((current) => ({
                      ...current,
                      direction: String(Number(direction.toFixed(12))),
                    }))}
                  />
                ) : (
                  <div className="flex h-full min-h-96 items-center justify-center border border-dashed border-slate-700 text-sm text-slate-300">
                    当前帧没有可公开的 Gridmap 鸟瞰图。
                  </div>
                )}
                <div className="pointer-events-none absolute bottom-3 left-3 z-10 flex max-w-[calc(100%-1.5rem)] flex-wrap gap-x-3 gap-y-1 rounded-lg bg-slate-950/72 px-3 py-2 text-[11px] text-white/75 backdrop-blur-sm">
                  <span className="inline-flex items-center gap-1"><Circle aria-hidden="true" className="size-2.5" />原始目标</span>
                  <span className="inline-flex items-center gap-1 text-orange-300"><Circle aria-hidden="true" className="size-2.5 fill-current" />当前草稿目标</span>
                  <span className="inline-flex items-center gap-1"><Minus aria-hidden="true" className="size-3" strokeDasharray="2 2" />原始轨迹</span>
                  <span className="inline-flex items-center gap-1 text-blue-300"><Minus aria-hidden="true" className="size-3" />当前权威轨迹</span>
                </div>
              </section>
            </div>

            <aside
              aria-label="轨迹属性"
              aria-hidden={geometryDragging ? "true" : undefined}
              inert={geometryDragging ? true : undefined}
              data-collapsed={inspectorCollapsed ? "true" : "false"}
              className="fix-inspector-panel absolute z-50 flex min-h-0 flex-col overflow-hidden rounded-[14px] border border-[#dfe4ed] bg-white/96 shadow-[0_14px_36px_rgba(25,36,62,0.16)] backdrop-blur-md transition-[width,max-height,opacity,transform] duration-180 ease-out motion-reduce:transition-none"
            >
            <div className={cn(
              "flex min-h-13 shrink-0 items-center justify-between gap-2 border-b border-console-line px-3.5 py-2.5",
              inspectorCollapsed && "justify-center border-b-0 px-2.5 max-[900px]:justify-between max-[900px]:border-b max-[900px]:px-3.5",
            )}>
                <div className={cn(inspectorCollapsed && "min-[901px]:hidden")}>
                  <h3 className="text-sm font-semibold text-console-text">轨迹属性</h3>
                  <p className="mt-1 text-xs text-console-muted">{selectedTarget?.label ?? "未选择目标"}</p>
                </div>
                <div className="flex items-center gap-2">
                  {!inspectorCollapsed && (
                    <span className="text-xs text-console-muted">
                      {saving ? "正在自动保存…" : editorDirty ? "等待自动保存" : "草稿已保存"}
                    </span>
                  )}
                  <button
                    type="button"
                    aria-label={inspectorCollapsed ? "展开轨迹属性" : "收起轨迹属性"}
                    aria-expanded={!inspectorCollapsed}
                    className="flex size-8 shrink-0 items-center justify-center rounded-lg text-[#667085] transition-[color,background-color] duration-150 hover:bg-[#f1f3f7] hover:text-[#202938] active:bg-[#e8ebf1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none"
                    onClick={() => setInspectorCollapsed((value) => !value)}
                  >
                    {inspectorCollapsed
                      ? <PanelRightOpen aria-hidden="true" className="size-4" />
                      : <PanelRightClose aria-hidden="true" className="size-4" />}
                  </button>
                </div>
            </div>
            <div className={cn("console-soft-scrollbar min-h-0 flex-1 overflow-y-auto", inspectorCollapsed && "hidden")}>
            <div className="border-b border-console-line p-3 sm:hidden">
              <Select
                disabled={frameNavigationLocked}
                value={selectedTarget?.target_ref ?? ""}
                onValueChange={setTargetRef}
              >
                <SelectTrigger aria-label="当前目标" className="h-9 bg-white">
                  <SelectValue placeholder="暂无目标" />
                </SelectTrigger>
                <SelectContent>
                  {(currentFrame?.targets ?? []).map((target) => (
                    <SelectItem key={target.target_ref} value={target.target_ref}>{target.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <fieldset disabled={!editable || saving || acting || terminal || geometryDragging} className="space-y-4 p-4">
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
                <span className="mb-1 block text-xs text-console-muted">方向（弧度）</span>
                <input
                  aria-label="方向"
                  inputMode="decimal"
                  value={editor.direction}
                  onChange={(event) => setEditor((current) => ({ ...current, direction: event.target.value }))}
                  className="h-9 w-full rounded-lg border border-console-line bg-white px-3 text-sm"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-console-muted">速度（m/s）</span>
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
                本帧不进入训练
              </label>
              <p className="-mt-2 text-xs leading-5 text-console-muted">
                仅排除当前帧，不会废弃整个 Segment。
              </p>
              <div className="grid grid-cols-2 gap-2 border-t border-console-line pt-4">
                <ConsoleButton
                  disabled={
                    !selectedTarget
                    || frameIndex < 1
                    || selectedTarget.present
                    || acting
                  }
                  onClick={() => void restoreMissingTarget()}
                >
                  {acting
                    ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
                    : <UserPlus aria-hidden="true" className="h-4 w-4" />}
                  {acting ? "正在补回…" : "补回目标"}
                </ConsoleButton>
                <ConsoleButton
                  disabled={!selectedTarget || !selectedTarget.present}
                  onClick={() => selectedTarget && void runCommand(
                    {
                      kind: "delete_target",
                      frame_index: frameIndex,
                      target_ref: selectedTarget.target_ref,
                    },
                    undefined,
                    { successTitle: "当前帧目标已删除", failureTitle: "删除目标失败" },
                  )}
                >
                  <Trash2 aria-hidden="true" className="h-4 w-4" />
                  删除目标
                </ConsoleButton>
                <ConsoleButton
                  className="col-span-2"
                  onClick={() => void runCommand(
                    { kind: "restore_frame", frame_index: frameIndex },
                    undefined,
                    { successTitle: "当前帧已恢复", failureTitle: "恢复当前帧失败" },
                  )}
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
                    || conflict
                    || geometryDragging
                    || fixRuntimeBusy
                  }
                  onClick={() => void submitRevision()}
                >
                  <Save aria-hidden="true" className="h-4 w-4" />
                  {revision ? "更新 Fix 预览" : "生成 Fix 预览"}
                </ConsoleButton>
                <p className="text-xs leading-5 text-console-muted">
                  草稿自动保存；生成预览时才调用冻结旧 Runtime 重算轨迹。确认同屏结果后再通过。
                </p>
                <div className="grid grid-cols-3 gap-2">
                  <ConsoleButton
                    disabled={!previewMatchesDraft || pageDirty || acting || conflict || geometryDragging || fixRuntimeBusy}
                    onClick={() => setDecision("approve")}
                  >
                    <CheckCircle2 aria-hidden="true" className="h-4 w-4" />
                    通过
                  </ConsoleButton>
                  <ConsoleButton
                    disabled={pageDirty || acting || conflict || geometryDragging || fixRuntimeBusy}
                    onClick={() => setDecision("return")}
                  >
                    <RotateCcw aria-hidden="true" className="h-4 w-4" />
                    退回
                  </ConsoleButton>
                  <ConsoleButton
                    disabled={pageDirty || acting || conflict || geometryDragging || fixRuntimeBusy}
                    onClick={() => setDecision("discard")}
                  >
                    <Trash2 aria-hidden="true" className="h-4 w-4" />
                    废弃 Segment
                  </ConsoleButton>
                </div>
              </div>
            )}
            </div>
            </aside>

          </div>
          <ReviewSegmentQueuePanel
            reviews={reviewQueue}
            currentReviewRef={review.review_ref}
            className="min-h-[13rem] max-h-[20rem]"
            disabled={pageDirty || acting || conflict || geometryDragging || fixRuntimeBusy || decision !== null}
            layout="horizontal"
            onNavigate={(nextReviewRef) => {
              if (nextReviewRef !== review.review_ref) {
                navigate(`/annotation/reviews/${encodeURIComponent(nextReviewRef)}`);
              }
            }}
          />
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
                || pageDirty
                || geometryDragging
                || conflict
                || (decision === "approve" && !previewMatchesDraft)
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
