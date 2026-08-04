import {
  AlertTriangle,
  BoxSelect,
  Check,
  ChevronDown,
  ChevronUp,
  CircleDot,
  Minus,
  MousePointer2,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-react";
import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { StatusTag } from "../../components/console/StatusTag";
import { cn } from "../../lib/utils";
import { SegmentRuler } from "./SegmentRuler";
import {
  AnnotationApiError,
  getAnnotationSegment,
  saveAnnotationDraft,
  submitInitialAnnotation,
} from "./api";
import {
  ANNOTATION_COLORS,
  type AnnotationJobDetail,
  type AnnotationSegmentDetail,
  type AnnotationTarget,
} from "./types";

type WorkbenchProps = {
  job: AnnotationJobDetail;
  segment: AnnotationSegmentDetail;
  onSegmentUpdated: (segment: AnnotationSegmentDetail) => void;
  onJobRefresh: () => Promise<void>;
  onExternalSubmissionResolved?: (message: string) => void;
  registerFlush?: (flush: () => Promise<boolean>) => void;
  onNavigateSegment?: (segmentRef: string) => void | Promise<void>;
};

type EditorMode = "select" | "box" | "point";
type HandleDirection = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";
type Point = { x: number; y: number };
type Gesture =
  | { kind: "draw"; start: Point; current: Point }
  | { kind: "move"; targetRef: string; start: Point; original: [number, number, number, number] }
  | { kind: "point"; targetRef: string }
  | {
      kind: "resize";
      targetRef: string;
      start: Point;
      original: [number, number, number, number];
      direction: HandleDirection;
    };

type ConflictState = {
  localTargets: AnnotationTarget[];
  current: AnnotationSegmentDetail | null;
};

const AUTOSAVE_DELAY_MS = 700;
const HANDLE_DIRECTIONS: HandleDirection[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const EXTERNAL_SUBMISSION_MESSAGE =
  "已在其他页面完成提交。本页内容未再次提交，现已切换到服务器版本。";

function cloneTargets(targets: AnnotationTarget[]): AnnotationTarget[] {
  return targets.map((target) => ({
    ...target,
    bbox: target.bbox ? [...target.bbox] : null,
    point: target.point ? [...target.point] : null,
    colors: { ...target.colors },
  }));
}

function targetsFingerprint(targets: AnnotationTarget[]): string {
  return JSON.stringify(targets);
}

function targetRef(): string {
  const token = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(32, "0").slice(0, 32);
  return `target_${token}`;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.round(value)));
}

function eventPoint(
  event: Pick<ReactPointerEvent<SVGSVGElement>, "clientX" | "clientY" | "currentTarget">,
  width: number,
  height: number,
): Point {
  const rect = event.currentTarget.getBoundingClientRect();
  return {
    x: clamp(((event.clientX - rect.left) / Math.max(rect.width, 1)) * width, 0, width),
    y: clamp(((event.clientY - rect.top) / Math.max(rect.height, 1)) * height, 0, height),
  };
}

function normalizedBox(
  start: Point,
  end: Point,
  imageWidth: number,
  imageHeight: number,
): [number, number, number, number] {
  const firstX = clamp(start.x, 0, imageWidth);
  const secondX = clamp(end.x, 0, imageWidth);
  const firstY = clamp(start.y, 0, imageHeight);
  const secondY = clamp(end.y, 0, imageHeight);
  const left = Math.min(firstX, secondX);
  const top = Math.min(firstY, secondY);
  const right = Math.max(firstX, secondX);
  const bottom = Math.max(firstY, secondY);
  return [left, top, right - left, bottom - top];
}

function handlePoint(
  box: [number, number, number, number],
  direction: HandleDirection,
): Point {
  const [x, y, width, height] = box;
  const horizontal = direction.includes("w") ? x : direction.includes("e") ? x + width : x + width / 2;
  const vertical = direction.includes("n") ? y : direction.includes("s") ? y + height : y + height / 2;
  return { x: horizontal, y: vertical };
}

function resizeBox(
  original: [number, number, number, number],
  direction: HandleDirection,
  delta: Point,
  imageWidth: number,
  imageHeight: number,
): [number, number, number, number] {
  let [left, top, width, height] = original;
  let right = left + width;
  let bottom = top + height;
  if (direction.includes("w")) left = clamp(left + delta.x, 0, right);
  if (direction.includes("e")) right = clamp(right + delta.x, left, imageWidth);
  if (direction.includes("n")) top = clamp(top + delta.y, 0, bottom);
  if (direction.includes("s")) bottom = clamp(bottom + delta.y, top, imageHeight);
  width = right - left;
  height = bottom - top;
  return [left, top, width, height];
}

function targetComplete(target: AnnotationTarget): boolean {
  return Boolean(
    target.bbox
      && target.point
      && target.colors.upper
      && target.colors.lower
      && target.colors.shoes,
  );
}

function segmentFromConflict(value: unknown): AnnotationSegmentDetail | null {
  if (!value || typeof value !== "object") return null;
  const candidate = "segment" in value
    ? (value as { segment?: unknown }).segment
    : value;
  if (!candidate || typeof candidate !== "object" || !("segment_ref" in candidate)) return null;
  return candidate as AnnotationSegmentDetail;
}

export function InitialAnnotationWorkbench({
  job,
  segment,
  onSegmentUpdated,
  onJobRefresh,
  onExternalSubmissionResolved,
  registerFlush,
  onNavigateSegment,
}: WorkbenchProps) {
  const firstFrame = segment.first_frame;
  const [targets, setTargets] = useState<AnnotationTarget[]>(() => cloneTargets(segment.draft?.targets ?? []));
  const [selectedTargetRef, setSelectedTargetRef] = useState<string | null>(
    segment.draft?.targets[0]?.target_ref ?? null,
  );
  const [mode, setMode] = useState<EditorMode>("select");
  const [gesture, setGesture] = useState<Gesture | null>(null);
  const [zoom, setZoom] = useState(1);
  const [imageSizeValid, setImageSizeValid] = useState<boolean | null>(null);
  const [imageLoadError, setImageLoadError] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "dirty" | "saving" | "saved" | "error">("idle");
  const [saveMessage, setSaveMessage] = useState("");
  const [externalSubmissionResolved, setExternalSubmissionResolved] = useState(false);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const conflictRef = useRef<ConflictState | null>(null);
  const segmentRevisionRef = useRef(segment.state_revision);
  const draftRevisionRef = useRef(segment.draft_revision);
  const lastSavedFingerprintRef = useRef(targetsFingerprint(segment.draft?.targets ?? []));
  const targetsRef = useRef(targets);
  const saveChainRef = useRef<Promise<boolean>>(Promise.resolve(true));
  const performSaveRef = useRef<(snapshot: AnnotationTarget[], bypassConflict?: boolean) => Promise<boolean>>(
    async () => false,
  );
  const autosaveTimerRef = useRef<number | null>(null);
  const editable = (
    job.status === "waiting_initial_annotation"
    && !job.cancel_requested
    && (segment.status === "pending_initial_annotation" || segment.status === "draft")
    && !externalSubmissionResolved
  );
  const canEdit = editable && !submitting && imageSizeValid === true;
  const focusMode = mode === "box" || mode === "point";

  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      const target = event.target;
      if (target instanceof HTMLElement && target.closest("input, textarea, select, [contenteditable='true']")) {
        return;
      }
      if (event.key === "Escape" && focusMode) {
        event.preventDefault();
        setGesture(null);
        setMode("select");
        return;
      }
      if (!canEdit || event.metaKey || event.ctrlKey || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "v") setMode("select");
      if (key === "b") setMode("box");
      if (key === "p" && selectedTargetRef) setMode("point");
    };
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [canEdit, focusMode, selectedTargetRef]);

  useEffect(() => {
    const nextTargets = cloneTargets(segment.draft?.targets ?? []);
    setTargets(nextTargets);
    targetsRef.current = nextTargets;
    setSelectedTargetRef(nextTargets[0]?.target_ref ?? null);
    segmentRevisionRef.current = segment.state_revision;
    draftRevisionRef.current = segment.draft_revision;
    lastSavedFingerprintRef.current = targetsFingerprint(nextTargets);
    setSaveState("idle");
    setSaveMessage("");
    setExternalSubmissionResolved(false);
    conflictRef.current = null;
    setConflict(null);
  }, [segment.segment_ref]);

  useEffect(() => {
    targetsRef.current = targets;
  }, [targets]);

  const selectedTarget = useMemo(
    () => targets.find((target) => target.target_ref === selectedTargetRef) ?? null,
    [selectedTargetRef, targets],
  );

  const synchronizeExternalSegment = useCallback(async (latest: AnnotationSegmentDetail) => {
    const nextTargets = cloneTargets(latest.draft?.targets ?? []);
    segmentRevisionRef.current = latest.state_revision;
    draftRevisionRef.current = latest.draft_revision;
    lastSavedFingerprintRef.current = targetsFingerprint(nextTargets);
    setTargets(nextTargets);
    targetsRef.current = nextTargets;
    setSelectedTargetRef(nextTargets[0]?.target_ref ?? null);
    conflictRef.current = null;
    setConflict(null);
    setSaveState("saved");
    if (latest.status === "submitted") {
      setSaveMessage("已载入服务器版本");
      setExternalSubmissionResolved(true);
      onExternalSubmissionResolved?.(EXTERNAL_SUBMISSION_MESSAGE);
    } else {
      setSaveMessage("该 Segment 状态已变化，已同步服务器状态。");
    }
    onSegmentUpdated(latest);
    await onJobRefresh().catch(() => undefined);
  }, [onExternalSubmissionResolved, onJobRefresh, onSegmentUpdated]);

  const performSave = useCallback((snapshot: AnnotationTarget[], bypassConflict = false): Promise<boolean> => {
    if (!editable) return Promise.resolve(true);
    if (conflictRef.current && !bypassConflict) return Promise.resolve(false);
    const fingerprint = targetsFingerprint(snapshot);

    const task = saveChainRef.current.then(async () => {
      if (conflictRef.current && !bypassConflict) return false;
      if (fingerprint === lastSavedFingerprintRef.current) return true;
      setSaveState("saving");
      setSaveMessage("");
      try {
        const updated = await saveAnnotationDraft(job.job_ref, segment.segment_ref, {
          expected_segment_revision: segmentRevisionRef.current,
          expected_draft_revision: draftRevisionRef.current,
          targets: cloneTargets(snapshot),
        });
        segmentRevisionRef.current = updated.state_revision;
        draftRevisionRef.current = updated.draft_revision;
        lastSavedFingerprintRef.current = fingerprint;
        onSegmentUpdated(updated);
        const currentTargets = cloneTargets(targetsRef.current);
        if (targetsFingerprint(currentTargets) === fingerprint) {
          setSaveState("saved");
        } else {
          setSaveState("dirty");
          void performSaveRef.current(currentTargets);
        }
        return true;
      } catch (error) {
        if (error instanceof AnnotationApiError && error.status === 409) {
          const latest = segmentFromConflict(error.detail?.current);
          if (latest && (
            latest.status === "pending_initial_annotation"
            || latest.status === "draft"
          )) {
            const nextConflict = {
              localTargets: cloneTargets(targetsRef.current),
              current: latest,
            };
            conflictRef.current = nextConflict;
            setConflict(nextConflict);
            setSaveState("error");
            setSaveMessage("服务器上已有更新，请选择保留哪一版。");
          } else if (latest) {
            await synchronizeExternalSegment(latest);
          } else {
            setSaveState("error");
            setSaveMessage("该 Segment 状态已变化，请刷新后重试。");
          }
        } else {
          setSaveState("error");
          setSaveMessage(error instanceof Error ? error.message : "草稿保存失败");
        }
        return false;
      }
    });
    saveChainRef.current = task.catch(() => false);
    return task;
  }, [editable, job.job_ref, segment.segment_ref, synchronizeExternalSegment]);
  performSaveRef.current = performSave;

  useEffect(() => {
    if (!editable || conflict) return;
    const fingerprint = targetsFingerprint(targets);
    if (fingerprint === lastSavedFingerprintRef.current) return;
    setSaveState("dirty");
    if (autosaveTimerRef.current !== null) window.clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = window.setTimeout(() => {
      autosaveTimerRef.current = null;
      void performSave(cloneTargets(targetsRef.current));
    }, AUTOSAVE_DELAY_MS);
    return () => {
      if (autosaveTimerRef.current !== null) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, [conflict, editable, performSave, targets]);

  const flushDraft = useCallback(async (): Promise<boolean> => {
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    if (conflictRef.current) return false;
    return performSave(cloneTargets(targetsRef.current));
  }, [performSave]);

  useEffect(() => {
    registerFlush?.(flushDraft);
  }, [flushDraft, registerFlush]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      const dirty = targetsFingerprint(targetsRef.current) !== lastSavedFingerprintRef.current;
      if (!dirty && saveState !== "saving" && !conflict) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [conflict, saveState]);

  const updateTarget = useCallback(
    (ref: string, updater: (target: AnnotationTarget) => AnnotationTarget) => {
      setTargets((current) => {
        const next = current.map((target) => target.target_ref === ref ? updater(target) : target);
        targetsRef.current = next;
        return next;
      });
    },
    [],
  );

  const pointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!canEdit || !firstFrame || mode !== "box" || event.button !== 0) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const point = eventPoint(event, firstFrame.width, firstFrame.height);
    setGesture({ kind: "draw", start: point, current: point });
  };

  const pointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!gesture || !firstFrame) return;
    const point = eventPoint(event, firstFrame.width, firstFrame.height);
    if (gesture.kind === "draw") {
      setGesture({ ...gesture, current: point });
      return;
    }
    if (gesture.kind === "point") {
      updateTarget(gesture.targetRef, (target) => ({
        ...target,
        point: [
          clamp(point.x, 0, firstFrame.width - 1),
          clamp(point.y, 0, firstFrame.height - 1),
        ],
      }));
      return;
    }
    const delta = { x: point.x - gesture.start.x, y: point.y - gesture.start.y };
    if (gesture.kind === "move") {
      const [x, y, width, height] = gesture.original;
      const nextX = clamp(x + delta.x, 0, firstFrame.width - width);
      const nextY = clamp(y + delta.y, 0, firstFrame.height - height);
      updateTarget(gesture.targetRef, (target) => {
        const pointDelta = { x: nextX - x, y: nextY - y };
        return {
          ...target,
          bbox: [nextX, nextY, width, height],
          point: target.point
            ? [
                clamp(target.point[0] + pointDelta.x, 0, firstFrame.width - 1),
                clamp(target.point[1] + pointDelta.y, 0, firstFrame.height - 1),
              ]
            : null,
        };
      });
      return;
    }
    updateTarget(gesture.targetRef, (target) => ({
      ...target,
      bbox: resizeBox(gesture.original, gesture.direction, delta, firstFrame.width, firstFrame.height),
    }));
  };

  const pointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!gesture || !firstFrame) return;
    if (gesture.kind === "draw") {
      const bbox = normalizedBox(
        gesture.start,
        gesture.current,
        firstFrame.width,
        firstFrame.height,
      );
      const ref = targetRef();
      setTargets((current) => {
        const next = [
          ...current,
          {
            target_ref: ref,
            bbox,
            point: null,
            colors: { upper: null, lower: null, shoes: null },
          } satisfies AnnotationTarget,
        ];
        targetsRef.current = next;
        return next;
      });
      setSelectedTargetRef(ref);
      setMode("point");
    }
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setGesture(null);
  };

  const beginMove = (
    event: ReactPointerEvent<SVGRectElement>,
    target: AnnotationTarget,
  ) => {
    if (!canEdit || mode !== "select" || !target.bbox || !firstFrame) return;
    event.stopPropagation();
    setSelectedTargetRef(target.target_ref);
    const svg = event.currentTarget.ownerSVGElement;
    if (!svg) return;
    const point = eventPoint(
      { clientX: event.clientX, clientY: event.clientY, currentTarget: svg } as ReactPointerEvent<SVGSVGElement>,
      firstFrame.width,
      firstFrame.height,
    );
    svg.setPointerCapture?.(event.pointerId);
    setGesture({ kind: "move", targetRef: target.target_ref, start: point, original: target.bbox });
  };

  const beginResize = (
    event: ReactPointerEvent<SVGCircleElement>,
    target: AnnotationTarget,
    direction: HandleDirection,
  ) => {
    if (!canEdit || !target.bbox || !firstFrame) return;
    event.stopPropagation();
    setSelectedTargetRef(target.target_ref);
    const svg = event.currentTarget.ownerSVGElement;
    if (!svg) return;
    const point = eventPoint(
      { clientX: event.clientX, clientY: event.clientY, currentTarget: svg } as ReactPointerEvent<SVGSVGElement>,
      firstFrame.width,
      firstFrame.height,
    );
    svg.setPointerCapture?.(event.pointerId);
    setGesture({ kind: "resize", targetRef: target.target_ref, start: point, original: target.bbox, direction });
  };

  const beginPoint = (
    event: ReactPointerEvent<SVGSVGElement | SVGCircleElement>,
    targetRef: string | null,
    existingPoint = false,
  ) => {
    if (!canEdit || !firstFrame || !targetRef || (!existingPoint && mode !== "point")) return;
    event.stopPropagation();
    const svg = event.currentTarget.ownerSVGElement
      ?? event.currentTarget as SVGSVGElement;
    if (!svg) return;
    const point = eventPoint(
      { clientX: event.clientX, clientY: event.clientY, currentTarget: svg } as ReactPointerEvent<SVGSVGElement>,
      firstFrame.width,
      firstFrame.height,
    );
    setSelectedTargetRef(targetRef);
    updateTarget(targetRef, (target) => ({
      ...target,
      point: [
        clamp(point.x, 0, firstFrame.width - 1),
        clamp(point.y, 0, firstFrame.height - 1),
      ],
    }));
    svg.setPointerCapture?.(event.pointerId);
    setGesture({ kind: "point", targetRef });
    setMode("select");
  };

  const keyboardMove = (event: React.KeyboardEvent<SVGSVGElement>) => {
    if (!canEdit || !selectedTarget || !selectedTarget.bbox || !firstFrame) return;
    const direction = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    }[event.key];
    if (!direction) return;
    event.preventDefault();
    const step = event.shiftKey ? 10 : 1;
    const [x, y, width, height] = selectedTarget.bbox;
    const nextX = clamp(x + direction[0] * step, 0, firstFrame.width - width);
    const nextY = clamp(y + direction[1] * step, 0, firstFrame.height - height);
    updateTarget(selectedTarget.target_ref, (target) => ({
      ...target,
      bbox: [nextX, nextY, width, height],
      point: target.point
        ? [
            clamp(target.point[0] + nextX - x, 0, firstFrame.width - 1),
            clamp(target.point[1] + nextY - y, 0, firstFrame.height - 1),
          ]
        : null,
    }));
  };

  const useServerVersion = async () => {
    const latest = conflict?.current ?? await getAnnotationSegment(job.job_ref, segment.segment_ref);
    const nextTargets = cloneTargets(latest.draft?.targets ?? []);
    segmentRevisionRef.current = latest.state_revision;
    draftRevisionRef.current = latest.draft_revision;
    lastSavedFingerprintRef.current = targetsFingerprint(nextTargets);
    setTargets(nextTargets);
    targetsRef.current = nextTargets;
    setSelectedTargetRef(nextTargets[0]?.target_ref ?? null);
    conflictRef.current = null;
    setConflict(null);
    setSaveState("saved");
    setSaveMessage("已载入服务器版本");
    onSegmentUpdated(latest);
  };

  const keepLocalVersion = async () => {
    if (!conflict) return;
    const localTargets = cloneTargets(conflict.localTargets);
    const latest = conflict.current ?? await getAnnotationSegment(job.job_ref, segment.segment_ref);
    segmentRevisionRef.current = latest.state_revision;
    draftRevisionRef.current = latest.draft_revision;
    lastSavedFingerprintRef.current = targetsFingerprint(latest.draft?.targets ?? []);
    setTargets(localTargets);
    targetsRef.current = localTargets;
    conflictRef.current = null;
    setConflict(null);
    setSaveState("dirty");
    await performSave(localTargets, true);
  };

  const submit = async () => {
    setSubmitting(true);
    setSaveMessage("");
    setExternalSubmissionResolved(false);
    try {
      const saved = await flushDraft();
      if (!saved || conflict || draftRevisionRef.current === null) return;
      const updated = await submitInitialAnnotation(
        job.job_ref,
        segment.segment_ref,
        segmentRevisionRef.current,
        draftRevisionRef.current,
      );
      segmentRevisionRef.current = updated.state_revision;
      draftRevisionRef.current = updated.draft_revision;
      onSegmentUpdated(updated);
      await onJobRefresh();
      setSaveState("saved");
      setSaveMessage("首帧标注已提交");
    } catch (error) {
      if (error instanceof AnnotationApiError && error.status === 409) {
        const latest = segmentFromConflict(error.detail?.current);
        if (latest && (
          latest.status === "pending_initial_annotation"
          || latest.status === "draft"
        )) {
          const nextConflict = {
            localTargets: cloneTargets(targetsRef.current),
            current: latest,
          };
          conflictRef.current = nextConflict;
          setConflict(nextConflict);
          setSaveState("error");
          setSaveMessage("服务器上已有更新，请选择保留哪一版。");
        } else if (latest) {
          await synchronizeExternalSegment(latest);
        } else {
          setSaveState("error");
          setSaveMessage("该 Segment 状态已变化，请刷新后重试。");
        }
      } else {
        setSaveState("error");
        setSaveMessage(error instanceof Error ? error.message : "提交失败");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const validForSubmit = targets.length > 0 && targets.every(targetComplete);
  const previewBox = gesture?.kind === "draw" && firstFrame
    ? normalizedBox(gesture.start, gesture.current, firstFrame.width, firstFrame.height)
    : null;
  const saveStateLabel = saveMessage || {
    idle: "尚无修改",
    dirty: "等待自动保存",
    saving: "正在保存…",
    saved: "草稿已保存",
    error: "草稿保存失败",
  }[saveState];

  if (!firstFrame) {
    return (
      <ConsoleCard>
        <div className="flex items-center gap-3 text-amber-700">
          <AlertTriangle className="h-5 w-5" aria-hidden="true" />
          <p className="text-sm">准备完成后才能读取 resize 后的首帧。</p>
        </div>
      </ConsoleCard>
    );
  }

  return (
    <section
      data-testid="annotation-workbench"
      className="relative flex min-h-168 min-w-0 flex-1 overflow-hidden bg-[#edf0f5] lg:min-h-152 xl:h-full xl:min-h-0"
    >
      <div
        data-testid="annotation-canvas-region"
        className="relative flex min-h-136 min-w-0 flex-1 flex-col overflow-hidden lg:min-h-0"
      >
        <div className="absolute left-3 top-3 z-40 flex min-h-12 max-w-[calc(100%-1.5rem)] flex-wrap items-center justify-between gap-2 rounded-xl border border-[#dfe4ed] bg-white/94 p-1.5 shadow-[0_8px_24px_rgba(27,39,68,0.12)] backdrop-blur-md sm:left-4 sm:top-4">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <ConsoleButton
              variant="ghost"
              aria-label="选择/调整"
              className={cn(
                "h-9 border-transparent bg-transparent px-2.5 text-[#374151] shadow-none hover:border-[#dfe4ed] hover:bg-[#f4f6fa] active:bg-[#e9edf4]",
                mode === "select" && "border-[#cfe5d4] bg-[#e8f6eb] text-[#17663a] hover:border-[#b9ddc2] hover:bg-[#ddf1e2]",
              )}
              onClick={() => setMode("select")}
              disabled={!canEdit}
              aria-pressed={mode === "select"}
            >
              <MousePointer2 className="h-4 w-4" aria-hidden="true" />
              选择/调整
              <kbd className="ml-1 rounded border border-current/20 px-1 py-0.5 text-[9px] font-medium opacity-60">V</kbd>
            </ConsoleButton>
            <ConsoleButton
              variant="ghost"
              aria-label="框选目标"
              className={cn(
                "h-9 border-transparent bg-transparent px-2.5 text-[#374151] shadow-none hover:border-[#dfe4ed] hover:bg-[#f4f6fa] active:bg-[#e9edf4]",
                mode === "box" && "border-[#bcd0fb] bg-[#e9f0ff] text-[#2456ba] hover:border-[#a9c1f5] hover:bg-[#dfe9ff]",
              )}
              onClick={() => setMode("box")}
              disabled={!canEdit}
              aria-pressed={mode === "box"}
            >
              <BoxSelect className="h-4 w-4" aria-hidden="true" />
              框选目标
              <kbd className="ml-1 rounded border border-current/20 px-1 py-0.5 text-[9px] font-medium opacity-60">B</kbd>
            </ConsoleButton>
            <ConsoleButton
              variant="ghost"
              aria-label="前景点"
              className={cn(
                "h-9 border-transparent bg-transparent px-2.5 text-[#374151] shadow-none hover:border-[#dfe4ed] hover:bg-[#f4f6fa] active:bg-[#e9edf4]",
                mode === "point" && "border-[#bcd0fb] bg-[#e9f0ff] text-[#2456ba] hover:border-[#a9c1f5] hover:bg-[#dfe9ff]",
              )}
              onClick={() => setMode("point")}
              disabled={!canEdit || !selectedTarget}
              aria-pressed={mode === "point"}
            >
              <CircleDot className="h-4 w-4" aria-hidden="true" />
              前景点
              <kbd className="ml-1 rounded border border-current/20 px-1 py-0.5 text-[9px] font-medium opacity-60">P</kbd>
            </ConsoleButton>
          </div>
        </div>

        <div
          className={cn(
            "absolute right-3 top-3 z-40 flex items-center gap-1 rounded-xl border border-[#dfe4ed] bg-white/94 p-1.5 shadow-[0_8px_24px_rgba(27,39,68,0.12)] backdrop-blur-md transition-[right,opacity,transform] duration-180 ease-out motion-reduce:transition-none sm:right-4 sm:top-4",
            !inspectorCollapsed && "lg:right-[23.5rem]",
            focusMode && "pointer-events-none translate-y-1 opacity-20",
          )}
          aria-hidden={focusMode ? "true" : undefined}
          inert={focusMode ? true : undefined}
        >
            <ConsoleButton
              className="h-8 border-transparent bg-transparent px-2 text-[#3d4657] shadow-none hover:border-[#dfe4ed] hover:bg-[#f4f6fa] active:bg-[#e9edf4]"
              aria-label="缩小画布"
              onClick={() => setZoom((value) => Math.max(1, value - 0.25))}
            >
              <Minus className="h-4 w-4" aria-hidden="true" />
            </ConsoleButton>
            <span className="w-12 text-center text-xs font-medium tabular-nums text-[#3d4657]">{Math.round(zoom * 100)}%</span>
            <ConsoleButton
              className="h-8 border-transparent bg-transparent px-2 text-[#3d4657] shadow-none hover:border-[#dfe4ed] hover:bg-[#f4f6fa] active:bg-[#e9edf4]"
              aria-label="放大画布"
              onClick={() => setZoom((value) => Math.min(3, value + 0.25))}
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
            </ConsoleButton>
            <ConsoleButton
              className="h-8 border-transparent bg-transparent px-2 text-[#3d4657] shadow-none hover:border-[#dfe4ed] hover:bg-[#f4f6fa] active:bg-[#e9edf4]"
              aria-label="重置缩放"
              onClick={() => setZoom(1)}
            >
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
            </ConsoleButton>
        </div>

        <div
          data-annotation-canvas-scroll
          className={cn(
            "console-soft-scrollbar flex min-h-0 flex-1 items-center overflow-auto bg-[#e8ecf2] pb-20 pt-18 transition-[padding] duration-200 ease-out motion-reduce:transition-none sm:px-3 sm:pb-24 sm:pt-3",
            !inspectorCollapsed && "sm:pr-[23rem]",
          )}
        >
          <div
            className="relative mx-auto shrink-0 overflow-hidden bg-slate-950 shadow-[0_18px_50px_rgba(15,23,42,0.22)]"
            style={{
              width: `${zoom * 100}%`,
              maxWidth: zoom === 1 ? "100%" : "none",
            }}
          >
            <img
              src={firstFrame.url}
              alt={`Segment ${segment.ordinal} resize 后首帧`}
              className="block h-auto w-full select-none"
              draggable={false}
              onLoad={(event) => {
                const image = event.currentTarget;
                setImageLoadError(false);
                setImageSizeValid(
                  image.naturalWidth === firstFrame.width && image.naturalHeight === firstFrame.height,
                );
              }}
              onError={() => {
                setImageLoadError(true);
                setImageSizeValid(false);
              }}
            />
            <svg
              className={cn(
                "absolute inset-0 h-full w-full touch-none focus-visible:outline-2 focus-visible:outline-offset-[-3px] focus-visible:outline-white",
                mode === "box" && "cursor-crosshair",
                mode === "point" && "cursor-cell",
              )}
              viewBox={`0 0 ${firstFrame.width} ${firstFrame.height}`}
              preserveAspectRatio="none"
              aria-label="首帧标注画布"
              role="application"
              tabIndex={0}
              onPointerDown={(event) => mode === "point"
                ? beginPoint(event, selectedTargetRef)
                : pointerDown(event)}
              onPointerMove={pointerMove}
              onPointerUp={pointerUp}
              onPointerCancel={() => setGesture(null)}
              onKeyDown={keyboardMove}
            >
              {targets.map((target, index) => {
                if (!target.bbox) return null;
                const [x, y, width, height] = target.bbox;
                const selected = target.target_ref === selectedTargetRef;
                return (
                  <g key={target.target_ref}>
                    <rect
                      data-annotation-box-ref={target.target_ref}
                      aria-label={`${index === 0 ? "master" : `other${index}`} bounding box`}
                      x={x}
                      y={y}
                      width={width}
                      height={height}
                      fill={selected ? "rgba(45,108,223,0.14)" : "rgba(22,132,91,0.08)"}
                      stroke={selected ? "#2d6cdf" : "#16a36a"}
                      strokeWidth={Math.max(2, firstFrame.width / 650)}
                      vectorEffect="non-scaling-stroke"
                      onPointerDown={(event) => beginMove(event, target)}
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelectedTargetRef(target.target_ref);
                      }}
                    />
                    <text
                      x={x + 8}
                      y={Math.max(20, y + 22)}
                      fill="#ffffff"
                      fontSize={Math.max(18, firstFrame.width / 75)}
                      fontWeight="700"
                      stroke="rgba(15,23,42,.82)"
                      strokeWidth="3"
                      paintOrder="stroke"
                      pointerEvents="none"
                    >
                      {index === 0 ? "master" : `other${index}`}
                    </text>
                    {target.point && (
                      <>
                        <circle
                          data-annotation-point-hit-ref={target.target_ref}
                          cx={target.point[0]}
                          cy={target.point[1]}
                          r={Math.max(1.5, firstFrame.width / 65)}
                          fill="transparent"
                          stroke="transparent"
                          pointerEvents="all"
                          onPointerDown={(event) => beginPoint(event, target.target_ref, true)}
                        />
                        <circle
                          data-annotation-point-ref={target.target_ref}
                          aria-label={`${index === 0 ? "master" : `other${index}`} foreground point`}
                          cx={target.point[0]}
                          cy={target.point[1]}
                          r={Math.max(7, firstFrame.width / 150)}
                          fill="#f59e0b"
                          stroke="#fff"
                          strokeWidth="2"
                          vectorEffect="non-scaling-stroke"
                          onPointerDown={(event) => beginPoint(event, target.target_ref, true)}
                        />
                      </>
                    )}
                    {selected && HANDLE_DIRECTIONS.map((direction) => {
                      const point = handlePoint(target.bbox!, direction);
                      return (
                        <g key={direction}>
                          <circle
                            data-annotation-resize-hit-ref={target.target_ref}
                            data-resize-hit-direction={direction}
                            cx={point.x}
                            cy={point.y}
                            r={Math.max(1.5, firstFrame.width / 75)}
                            fill="transparent"
                            stroke="transparent"
                            pointerEvents="all"
                            onPointerDown={(event) => beginResize(event, target, direction)}
                          />
                          <circle
                            data-annotation-resize-ref={target.target_ref}
                            data-resize-direction={direction}
                            aria-label={`${index === 0 ? "master" : `other${index}`} ${direction} resize handle`}
                            cx={point.x}
                            cy={point.y}
                            r={Math.max(6, firstFrame.width / 180)}
                            fill="#ffffff"
                            stroke="#2d6cdf"
                            strokeWidth="2"
                            vectorEffect="non-scaling-stroke"
                            onPointerDown={(event) => beginResize(event, target, direction)}
                          />
                        </g>
                      );
                    })}
                  </g>
                );
              })}
              {previewBox && (
                <rect
                  x={previewBox[0]}
                  y={previewBox[1]}
                  width={previewBox[2]}
                  height={previewBox[3]}
                  fill="rgba(45,108,223,.12)"
                  stroke="#60a5fa"
                  strokeDasharray="8 5"
                  strokeWidth="2"
                  vectorEffect="non-scaling-stroke"
                />
              )}
            </svg>
          </div>
        </div>

        <div
          className={cn(
            "absolute left-3 top-17 z-30 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-2 rounded-lg border border-[#dfe4ed] bg-white/92 px-3 py-1.5 text-[11px] text-[#667085] shadow-sm backdrop-blur-sm transition-[opacity,transform] duration-180 motion-reduce:transition-none sm:left-4 sm:top-18",
            focusMode && "pointer-events-none -translate-y-1 opacity-20",
          )}
          aria-hidden={focusMode ? "true" : undefined}
        >
          <span className="tabular-nums">{firstFrame.width} × {firstFrame.height} · resize 后首帧坐标</span>
          {imageSizeValid === null && (
            <span aria-live="polite">正在加载并校验首帧图片…</span>
          )}
          {imageSizeValid === false && (
            <span aria-live="assertive" className="font-medium text-rose-600">
              {imageLoadError
                ? "首帧图片加载失败，请刷新页面重试"
                : "图片尺寸与元数据不一致，已停止编辑"}
            </span>
          )}
          {imageSizeValid === true && (
            <span className="hidden border-l border-[#dfe4ed] pl-2 sm:inline">方向键微调 · Shift ×10</span>
          )}
        </div>

        {focusMode ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-22 z-30 flex justify-center px-4">
            <div className="pointer-events-auto flex items-center gap-3 rounded-xl border border-white/25 bg-[#1f2b3d]/90 px-4 py-2 text-xs text-white shadow-[0_12px_32px_rgba(15,23,42,0.3)] backdrop-blur-md">
              <span>
                {mode === "box"
                  ? "专注框选：在画面中拖拽创建目标框"
                  : "专注前景点：在选中目标内部点击前景点"}
              </span>
              <button
                type="button"
                className="rounded-md border border-white/20 px-2 py-1 font-medium transition-[background-color,border-color] duration-150 hover:border-white/35 hover:bg-white/10 active:bg-white/15 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white motion-reduce:transition-none"
                onClick={() => setMode("select")}
              >
                Esc 退出
              </button>
            </div>
          </div>
        ) : null}

        {onNavigateSegment ? (
          <div
            className={cn(
              "absolute inset-x-0 bottom-3 z-40 flex justify-center px-3 transition-[opacity,transform,padding] duration-180 motion-reduce:transition-none sm:bottom-4",
              !inspectorCollapsed && "sm:pr-[22.5rem]",
              focusMode && "pointer-events-none translate-y-2 opacity-20",
            )}
            aria-hidden={focusMode ? "true" : undefined}
            inert={focusMode ? true : undefined}
          >
            <SegmentRuler
              segments={job.segments}
              currentSegmentRef={segment.segment_ref}
              disabled={focusMode || submitting}
              onNavigate={onNavigateSegment}
            />
          </div>
        ) : null}
      </div>

      <aside
        data-testid="annotation-inspector-region"
        data-collapsed={inspectorCollapsed ? "true" : "false"}
        className={cn(
          "absolute inset-x-3 bottom-3 z-40 flex max-h-[70%] min-h-0 flex-col overflow-hidden rounded-2xl border border-[#dfe4ed] bg-white/96 shadow-[0_18px_50px_rgba(25,36,62,0.18)] backdrop-blur-md transition-[width,transform,opacity] duration-200 ease-out motion-reduce:transition-none sm:inset-x-auto sm:bottom-4 sm:right-4 sm:top-4 sm:max-h-none sm:w-88",
          inspectorCollapsed && "inset-x-auto left-auto right-3 top-auto h-12 w-12 sm:right-4 sm:top-4 sm:h-12 sm:w-12",
          focusMode && "pointer-events-none translate-x-3 opacity-20",
        )}
        aria-label="目标属性检查器"
        inert={focusMode ? true : undefined}
      >
        <span className="sr-only">Segment {String(segment.ordinal).padStart(2, "0")}</span>
        <div className={cn(
          "flex min-h-14 shrink-0 items-center justify-between gap-3 border-b border-console-line px-4 py-2",
          inspectorCollapsed && "min-h-12 justify-center border-b-0 p-0",
        )}>
          <div className="flex items-center justify-between gap-3">
            <div className={cn(inspectorCollapsed && "hidden")}>
              <h2 className="text-sm font-semibold text-console-text">目标属性</h2>
              <p className="mt-0.5 text-[11px] text-console-muted">顺序决定 master / otherN</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {!inspectorCollapsed ? (
              <StatusTag tone={editable ? "info" : "neutral"}>{editable ? "编辑中" : "只读"}</StatusTag>
            ) : null}
            <button
              type="button"
              aria-label={inspectorCollapsed ? "展开目标属性" : "收起目标属性"}
              aria-expanded={!inspectorCollapsed}
              className="flex size-8 shrink-0 items-center justify-center rounded-lg text-[#667085] transition-[color,background-color] duration-150 hover:bg-[#f1f3f7] hover:text-[#202938] active:bg-[#e8ebf1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none"
              onClick={() => setInspectorCollapsed((value) => !value)}
            >
              {inspectorCollapsed
                ? <PanelRightOpen className="size-4" aria-hidden="true" />
                : <PanelRightClose className="size-4" aria-hidden="true" />}
            </button>
          </div>
        </div>

        <div className={cn(
          "console-soft-scrollbar min-h-0 flex-1 space-y-2 overflow-y-auto p-3",
          inspectorCollapsed && "hidden",
        )}>
          <div className="flex min-h-8 items-center justify-between gap-3 pb-1">
            <span className="text-xs font-medium text-console-muted">目标列表（{targets.length}）</span>
            <button
              type="button"
              disabled={!canEdit}
              className="inline-flex h-7 items-center gap-1 rounded-lg border border-console-line bg-white px-2 text-xs font-medium text-console-text transition-[color,background-color,border-color] duration-150 hover:border-[#b8c9ee] hover:bg-[#f5f8ff] hover:text-[#3156c8] active:bg-[#eaf0ff] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] disabled:cursor-not-allowed disabled:opacity-45 motion-reduce:transition-none"
              onClick={() => setMode("box")}
            >
              <Plus className="size-3.5" aria-hidden="true" />
              添加目标
            </button>
          </div>
          {targets.length === 0 && (
            <div className="border border-dashed border-console-line bg-console-panel2/40 p-5 text-center text-sm text-console-muted">
              点击“框选目标”，在首帧上拖出 master 框。
            </div>
          )}
          {targets.map((target, index) => {
            const isSelected = selectedTargetRef === target.target_ref;
            return (
              <div
                key={target.target_ref}
                className={cn(
                  "border bg-console-panel",
                  isSelected ? "border-console-cyan shadow-[inset_3px_0_0_#2d6cdf]" : "border-console-line",
                )}
              >
                <button
                  type="button"
                  className="flex min-h-10 w-full items-center justify-between gap-2 px-3 py-2 text-left hover:bg-console-panel2"
                  onClick={() => setSelectedTargetRef(target.target_ref)}
                >
                  <span className="text-sm font-semibold text-console-text">
                    {index === 0 ? "master" : `other${index}`}
                  </span>
                  <StatusTag tone={targetComplete(target) ? "success" : "warning"}>
                    {targetComplete(target) ? "完整" : "待补充"}
                  </StatusTag>
                </button>

                {isSelected && (
                  <div className="space-y-4 border-t border-console-line bg-console-panel2/35 p-3">
                    <div>
                      <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-console-muted">
                        边界框
                      </p>
                    <div className="grid grid-cols-4 gap-2">
                      {(["x", "y", "w", "h"] as const).map((label, fieldIndex) => (
                        <label key={label} className="text-[11px] text-console-muted">
                          <span className="mb-1 block uppercase">{label}</span>
                          <input
                            aria-label={`${index === 0 ? "master" : `other${index}`} bbox ${label}`}
                            type="number"
                            value={target.bbox?.[fieldIndex] ?? ""}
                            disabled={!canEdit || !target.bbox}
                            min={0}
                            className="h-8 w-full rounded-sm border border-console-line bg-white px-2 text-xs text-console-text focus:border-console-cyan focus:outline-hidden"
                            onChange={(event) => {
                              if (!target.bbox || !firstFrame) return;
                              const next = [...target.bbox] as [number, number, number, number];
                              next[fieldIndex] = Number(event.target.value);
                              next[0] = clamp(next[0], 0, firstFrame.width);
                              next[1] = clamp(next[1], 0, firstFrame.height);
                              next[2] = clamp(next[2], 0, firstFrame.width - next[0]);
                              next[3] = clamp(next[3], 0, firstFrame.height - next[1]);
                              updateTarget(target.target_ref, (current) => ({ ...current, bbox: next }));
                            }}
                          />
                        </label>
                      ))}
                    </div>
                    </div>

                    <div>
                      <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-console-muted">
                        前景点
                      </p>
                    <div className="grid grid-cols-2 gap-2">
                      {(["x", "y"] as const).map((label, pointIndex) => (
                        <label key={label} className="text-[11px] text-console-muted">
                          <span className="mb-1 block uppercase">{label}</span>
                          <input
                            aria-label={`${index === 0 ? "master" : `other${index}`} point ${label}`}
                            type="number"
                            value={target.point?.[pointIndex] ?? ""}
                            disabled={!canEdit}
                            min={0}
                            className="h-8 w-full rounded-sm border border-console-line bg-white px-2 text-xs text-console-text focus:border-console-cyan focus:outline-hidden"
                            onChange={(event) => {
                              if (!firstFrame) return;
                              const next: [number, number] = target.point ? [...target.point] : [0, 0];
                              next[pointIndex] = clamp(
                                Number(event.target.value),
                                0,
                                pointIndex === 0 ? firstFrame.width - 1 : firstFrame.height - 1,
                              );
                              updateTarget(target.target_ref, (current) => ({ ...current, point: next }));
                            }}
                          />
                        </label>
                      ))}
                    </div>
                    </div>

                    <div>
                      <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-console-muted">
                        服饰颜色
                      </p>
                      <div className="space-y-2">
                        {([
                          ["upper", "上衣颜色"],
                          ["lower", "裤子颜色"],
                          ["shoes", "鞋子颜色"],
                        ] as const).map(([field, label]) => (
                          <label key={field} className="block text-xs text-console-muted">
                            <span className="mb-1 block">{label}</span>
                            <select
                              aria-label={`${index === 0 ? "master" : `other${index}`} ${label}`}
                              value={target.colors[field] ?? ""}
                              disabled={!canEdit}
                              className="h-9 w-full rounded-sm border border-console-line bg-white px-2 text-sm text-console-text focus:border-console-cyan focus:outline-hidden"
                              onChange={(event) => updateTarget(target.target_ref, (current) => ({
                                ...current,
                                colors: {
                                  ...current.colors,
                                  [field]: event.target.value || null,
                                },
                              }))}
                            >
                              <option value="">请选择</option>
                              {ANNOTATION_COLORS.map((color) => (
                                <option key={color} value={color}>{color}</option>
                              ))}
                            </select>
                          </label>
                        ))}
                      </div>
                    </div>

                    <div className="flex gap-2 border-t border-console-line pt-3">
                      <ConsoleButton
                        aria-label={`上移 ${index === 0 ? "master" : `other${index}`}`}
                        disabled={!canEdit || index === 0}
                        onClick={() => setTargets((current) => {
                          const next = [...current];
                          [next[index - 1], next[index]] = [next[index], next[index - 1]];
                          targetsRef.current = next;
                          return next;
                        })}
                      >
                        <ChevronUp className="h-4 w-4" aria-hidden="true" />
                      </ConsoleButton>
                      <ConsoleButton
                        aria-label={`下移 ${index === 0 ? "master" : `other${index}`}`}
                        disabled={!canEdit || index === targets.length - 1}
                        onClick={() => setTargets((current) => {
                          const next = [...current];
                          [next[index], next[index + 1]] = [next[index + 1], next[index]];
                          targetsRef.current = next;
                          return next;
                        })}
                      >
                        <ChevronDown className="h-4 w-4" aria-hidden="true" />
                      </ConsoleButton>
                      <ConsoleButton
                        className="ml-auto text-rose-700"
                        aria-label={`删除 ${index === 0 ? "master" : `other${index}`}`}
                        disabled={!canEdit}
                        onClick={() => {
                          setTargets((current) => {
                            const next = current.filter((item) => item.target_ref !== target.target_ref);
                            targetsRef.current = next;
                            return next;
                          });
                          setSelectedTargetRef(null);
                        }}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                        删除
                      </ConsoleButton>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div className={cn(
          "space-y-3 border-t border-console-line bg-console-panel px-3 py-3",
          inspectorCollapsed && "hidden",
        )}>
          {conflict && (
            <div role="alert" className="border border-amber-200 bg-amber-50 p-3">
              <p className="text-xs font-medium text-amber-800">检测到并发修改</p>
              <p className="mt-1 text-xs text-amber-700">不会自动合并几何数据，请明确选择。</p>
              <div className="mt-2 flex gap-2">
                <ConsoleButton onClick={() => void useServerVersion()}>使用服务器版本</ConsoleButton>
                <ConsoleButton variant="primary" onClick={() => void keepLocalVersion()}>保留本地版本</ConsoleButton>
              </div>
            </div>
          )}
          <div className="flex min-h-9 items-center justify-between gap-2 text-xs">
            <span
              aria-live="polite"
              className={cn(
                "inline-flex min-w-0 items-center gap-1.5",
                saveState === "error" ? "text-rose-700" : "text-console-muted",
              )}
            >
              {saveState === "saved" && <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden="true" />}
              <span className="truncate">{saveStateLabel}</span>
            </span>
            <ConsoleButton
              aria-label="立即保存草稿"
              className="h-8 shrink-0 px-2"
              disabled={!editable || saveState === "saving" || Boolean(conflict)}
              onClick={() => void flushDraft()}
            >
              <Save className="h-4 w-4" aria-hidden="true" />
            </ConsoleButton>
          </div>
          <ConsoleButton
            className="w-full"
            variant="primary"
            disabled={!editable || !validForSubmit || submitting || Boolean(conflict) || imageSizeValid !== true}
            onClick={() => void submit()}
          >
            {submitting ? "提交中…" : "提交首帧标注"}
          </ConsoleButton>
          {!validForSubmit && editable && (
            <p className="text-xs leading-5 text-console-muted">
              每个目标都需要 bbox、前景点及上衣、裤子、鞋子三项颜色。
            </p>
          )}
        </div>
      </aside>
    </section>
  );
}
