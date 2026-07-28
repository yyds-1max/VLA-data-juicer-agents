import type {
  FixCommand,
  TrajectoryEvidenceCamera,
  TrajectoryEvidenceFrame,
  TrajectoryEvidenceGridmap,
  TrajectoryEvidenceTarget,
  TrajectoryPoint,
  TrajectoryReview,
  TrajectoryReviewEvidence,
} from "./types";

const REVIEW_REF = /^review_[0-9a-f]{32}$/;
const TRAJECTORY_REVISION_REF = /^trajectory_revision_[0-9a-f]{32}$/;
const TARGET_REF = /^target_[0-9a-f]{32}$/;
const MAX_FRAMES = 100_000;
const MAX_TRAJECTORY_POINTS = 10_000;

export class TrajectoryEvidenceContractError extends Error {
  constructor() {
    super("服务器返回的轨迹证据不符合公开契约。");
    this.name = "TrajectoryEvidenceContractError";
  }
}

function contractError(): never {
  throw new TrajectoryEvidenceContractError();
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return contractError();
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): void {
  const actual = Object.keys(value).sort();
  const required = [...expected].sort();
  if (
    actual.length !== required.length
    || actual.some((key, index) => key !== required[index])
  ) {
    contractError();
  }
}

function integer(value: unknown, minimum = 0): number {
  if (
    typeof value !== "number"
    || !Number.isInteger(value)
    || value < minimum
  ) {
    return contractError();
  }
  return value;
}

function finite(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return contractError();
  }
  return value;
}

function nullableFinite(value: unknown): number | null {
  return value === null ? null : finite(value);
}

function tuple(
  value: unknown,
  lengths: readonly number[],
): number[] {
  if (
    !Array.isArray(value)
    || !lengths.includes(value.length)
  ) {
    return contractError();
  }
  return value.map(finite);
}

function nullableTuple(
  value: unknown,
  length: number,
): number[] | null {
  return value === null ? null : tuple(value, [length]);
}

function nonEmptyString(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) {
    return contractError();
  }
  return value;
}

function opaqueRef(value: unknown, pattern: RegExp): string {
  const ref = nonEmptyString(value);
  return pattern.test(ref) ? ref : contractError();
}

function parseCamera(
  value: unknown,
  expectedUrl: string,
): TrajectoryEvidenceCamera | null {
  if (value === null) return null;
  const camera = record(value);
  exactKeys(camera, ["height", "url", "width"]);
  if (nonEmptyString(camera.url) !== expectedUrl) contractError();
  const width = camera.width === null ? null : integer(camera.width, 1);
  const height = camera.height === null ? null : integer(camera.height, 1);
  if ((width === null) !== (height === null)) contractError();
  return { url: expectedUrl, width, height };
}

function parseGridmap(
  value: unknown,
  expectedUrl: string,
): TrajectoryEvidenceGridmap | null {
  if (value === null) return null;
  const gridmap = record(value);
  exactKeys(gridmap, ["height", "url", "width"]);
  if (nonEmptyString(gridmap.url) !== expectedUrl) contractError();
  return {
    url: expectedUrl,
    width: integer(gridmap.width, 1),
    height: integer(gridmap.height, 1),
  };
}

function parseTarget(value: unknown): TrajectoryEvidenceTarget {
  const target = record(value);
  exactKeys(target, [
    "color",
    "direction",
    "image_box",
    "label",
    "position",
    "speed",
    "target_ref",
    "trajectory_points",
  ]);
  if (!Array.isArray(target.color) || target.color.length > 3) {
    return contractError();
  }
  const color = target.color.map(nonEmptyString);
  if (
    !Array.isArray(target.trajectory_points)
    || target.trajectory_points.length > MAX_TRAJECTORY_POINTS
  ) {
    return contractError();
  }
  const trajectoryPoints = target.trajectory_points.map((point) => {
    const values = tuple(point, [2, 3]);
    return values.length === 2
      ? [values[0], values[1]] as [number, number]
      : [values[0], values[1], values[2]] as [number, number, number];
  });
  const position = nullableTuple(target.position, 2);
  const imageBox = nullableTuple(target.image_box, 4);
  const speed = nullableFinite(target.speed);
  if (speed !== null && speed < 0) contractError();
  return {
    target_ref: opaqueRef(target.target_ref, TARGET_REF),
    label: nonEmptyString(target.label),
    position: position === null
      ? null
      : [position[0], position[1]],
    direction: nullableFinite(target.direction),
    speed,
    color,
    image_box: imageBox === null
      ? null
      : [imageBox[0], imageBox[1], imageBox[2], imageBox[3]],
    trajectory_points: trajectoryPoints,
  };
}

function parseCommand(
  value: unknown,
  {
    frameCount,
    targetRefs,
  }: {
    frameCount: number;
    targetRefs: ReadonlySet<string>;
  },
): FixCommand {
  const command = record(value);
  const kind = nonEmptyString(command.kind);
  const frameIndex = integer(command.frame_index);
  if (frameIndex >= frameCount) contractError();

  if (kind === "restore_frame") {
    exactKeys(command, ["frame_index", "kind"]);
    return { kind, frame_index: frameIndex };
  }
  if (kind === "toggle_pass") {
    exactKeys(command, ["frame_index", "kind", "value"]);
    if (typeof command.value !== "boolean") return contractError();
    return { kind, frame_index: frameIndex, value: command.value };
  }

  const targetRef = opaqueRef(command.target_ref, TARGET_REF);
  if (!targetRefs.has(targetRef)) contractError();
  if (
    kind === "delete_target"
    || kind === "add_missing_target"
  ) {
    exactKeys(command, ["frame_index", "kind", "target_ref"]);
    if (kind === "add_missing_target" && frameIndex === 0) contractError();
    return { kind, frame_index: frameIndex, target_ref: targetRef };
  }
  if (kind === "set_position") {
    exactKeys(command, ["frame_index", "kind", "target_ref", "x", "y"]);
    return {
      kind,
      frame_index: frameIndex,
      target_ref: targetRef,
      x: finite(command.x),
      y: finite(command.y),
    };
  }
  if (kind === "set_direction") {
    exactKeys(command, ["direction", "frame_index", "kind", "target_ref"]);
    return {
      kind,
      frame_index: frameIndex,
      target_ref: targetRef,
      direction: finite(command.direction),
    };
  }
  if (kind === "set_speed") {
    exactKeys(command, ["frame_index", "kind", "speed", "target_ref"]);
    const speed = finite(command.speed);
    if (speed < 0) contractError();
    return {
      kind,
      frame_index: frameIndex,
      target_ref: targetRef,
      speed,
    };
  }
  return contractError();
}

/**
 * Validate the exact public evidence envelope before any values reach the
 * workbench. Unknown fields are rejected so private runtime data cannot
 * silently become part of the browser contract.
 */
export function parseTrajectoryReviewEvidence(
  value: unknown,
  expectedReviewRef: string,
): TrajectoryReviewEvidence {
  const evidence = record(value);
  exactKeys(evidence, [
    "availability",
    "draft_commands",
    "draft_revision",
    "frame_count",
    "frames",
    "review_ref",
    "review_state_revision",
    "trajectory_revision_ref",
  ]);
  if (evidence.availability !== "available") contractError();
  const reviewRef = opaqueRef(evidence.review_ref, REVIEW_REF);
  if (reviewRef !== expectedReviewRef) contractError();
  const trajectoryRevisionRef = opaqueRef(
    evidence.trajectory_revision_ref,
    TRAJECTORY_REVISION_REF,
  );
  const reviewStateRevision = integer(evidence.review_state_revision);
  const draftRevision = evidence.draft_revision === null
    ? null
    : integer(evidence.draft_revision, 1);
  const frameCount = integer(evidence.frame_count, 1);
  if (frameCount > MAX_FRAMES || !Array.isArray(evidence.frames)) {
    return contractError();
  }
  if (evidence.frames.length !== frameCount) contractError();

  const expectedBase = `/api/annotation/reviews/${encodeURIComponent(reviewRef)}/evidence/frames`;
  let stableTargetRefs: Set<string> | null = null;
  const stableLabels = new Map<string, string>();
  const frames: TrajectoryEvidenceFrame[] = evidence.frames.map(
    (rawFrame, expectedIndex) => {
      const frame = record(rawFrame);
      exactKeys(frame, [
        "camera",
        "frame_index",
        "gridmap",
        "pass",
        "targets",
      ]);
      const frameIndex = integer(frame.frame_index);
      if (frameIndex !== expectedIndex || typeof frame.pass !== "boolean") {
        return contractError();
      }
      if (!Array.isArray(frame.targets) || frame.targets.length === 0) {
        return contractError();
      }
      const targets = frame.targets.map(parseTarget);
      const refs = new Set(targets.map((target) => target.target_ref));
      if (refs.size !== targets.length) contractError();
      if (stableTargetRefs === null) {
        stableTargetRefs = refs;
        for (const target of targets) {
          stableLabels.set(target.target_ref, target.label);
        }
      } else if (
        refs.size !== stableTargetRefs.size
        || [...refs].some((ref) => !stableTargetRefs?.has(ref))
        || targets.some(
          (target) => stableLabels.get(target.target_ref) !== target.label,
        )
      ) {
        contractError();
      }
      return {
        frame_index: frameIndex,
        pass: frame.pass,
        camera: parseCamera(
          frame.camera,
          `${expectedBase}/${frameIndex}/camera`,
        ),
        gridmap: parseGridmap(
          frame.gridmap,
          `${expectedBase}/${frameIndex}/gridmap`,
        ),
        targets,
      };
    },
  );
  if (!Array.isArray(evidence.draft_commands) || stableTargetRefs === null) {
    return contractError();
  }
  const draftCommands = evidence.draft_commands.map((command) => parseCommand(
    command,
    { frameCount, targetRefs: stableTargetRefs! },
  ));
  if (
    (draftRevision === null && draftCommands.length !== 0)
    || (draftRevision !== null && draftRevision !== draftCommands.length + 1)
  ) {
    contractError();
  }

  return {
    availability: "available",
    review_ref: reviewRef,
    trajectory_revision_ref: trajectoryRevisionRef,
    review_state_revision: reviewStateRevision,
    draft_revision: draftRevision,
    frame_count: frameCount,
    frames,
    draft_commands: draftCommands,
  };
}

export function evidenceMatchesReview(
  evidence: TrajectoryReviewEvidence,
  review: TrajectoryReview,
): boolean {
  return (
    evidence.review_ref === review.review_ref
    && evidence.trajectory_revision_ref
      === review.trajectory_revision.revision_ref
    && evidence.review_state_revision === review.state_revision
    && evidence.draft_revision === (review.fix_draft?.revision ?? null)
  );
}

export type ProjectedTrajectoryTarget = Omit<
  TrajectoryEvidenceTarget,
  "direction" | "position" | "speed"
> & {
  original_position: TrajectoryPoint | null;
  position: TrajectoryPoint | null;
  original_direction: number | null;
  direction: number | null;
  original_speed: number | null;
  speed: number | null;
  present: boolean;
  projection: "original" | "command" | "runtime_derived";
};

export type ProjectedTrajectoryFrame = Omit<
  TrajectoryEvidenceFrame,
  "targets"
> & {
  original_pass: boolean;
  targets: ProjectedTrajectoryTarget[];
};

export type ProjectedTrajectoryEvidence = Omit<
  TrajectoryReviewEvidence,
  "frames"
> & {
  frames: ProjectedTrajectoryFrame[];
};

function point(value: [number, number] | null): TrajectoryPoint | null {
  return value === null ? null : { x: value[0], y: value[1] };
}

function resetFrame(
  frame: ProjectedTrajectoryFrame,
): void {
  frame.pass = frame.original_pass;
  for (const target of frame.targets) {
    target.position = target.original_position
      ? { ...target.original_position }
      : null;
    target.direction = target.original_direction;
    target.speed = target.original_speed;
    target.present = target.original_position !== null;
    target.projection = "original";
  }
}

/**
 * Project the persisted command log for presentation only.
 *
 * This never reimplements legacy Fix math. Direct field commands are reflected
 * exactly; `add_missing_target` is marked runtime-derived and keeps an unknown
 * position until a later `set_position` supplies explicit coordinates.
 */
export function projectTrajectoryReviewEvidence(
  evidence: TrajectoryReviewEvidence,
): ProjectedTrajectoryEvidence {
  const frames: ProjectedTrajectoryFrame[] = evidence.frames.map((frame) => ({
    ...frame,
    original_pass: frame.pass,
    targets: frame.targets.map((target) => {
      const originalPosition = point(target.position);
      return {
        ...target,
        original_position: originalPosition,
        position: originalPosition ? { ...originalPosition } : null,
        original_direction: target.direction,
        original_speed: target.speed,
        present: originalPosition !== null,
        projection: "original" as const,
      };
    }),
  }));

  for (const command of evidence.draft_commands) {
    const frame = frames[command.frame_index];
    if (command.kind === "restore_frame") {
      resetFrame(frame);
      continue;
    }
    if (command.kind === "toggle_pass") {
      frame.pass = command.value;
      continue;
    }
    const target = frame.targets.find(
      (candidate) => candidate.target_ref === command.target_ref,
    );
    if (!target) continue;
    if (command.kind === "delete_target") {
      target.position = null;
      target.present = false;
      target.projection = "command";
    } else if (command.kind === "add_missing_target") {
      target.present = true;
      target.projection = "runtime_derived";
    } else if (command.kind === "set_position") {
      target.position = { x: command.x, y: command.y };
      target.present = true;
      target.projection = "command";
    } else if (command.kind === "set_direction") {
      target.direction = command.direction;
      if (target.projection !== "runtime_derived") {
        target.projection = "command";
      }
    } else if (command.kind === "set_speed") {
      target.speed = command.speed;
      if (target.projection !== "runtime_derived") {
        target.projection = "command";
      }
    }
  }

  return { ...evidence, frames };
}

export function trajectoryPositionPath(
  evidence: ProjectedTrajectoryEvidence | null,
  targetRef: string,
  source: "original" | "projected",
): Array<TrajectoryPoint | null> {
  if (!evidence || !targetRef) return [];
  return evidence.frames.map((frame) => {
    const target = frame.targets.find(
      (candidate) => candidate.target_ref === targetRef,
    );
    const value = source === "original"
      ? target?.original_position
      : target?.position;
    return value ? { ...value } : null;
  });
}

export function cameraCanRender(
  camera: TrajectoryEvidenceCamera | null,
): camera is TrajectoryEvidenceCamera & { width: number; height: number } {
  return (
    camera !== null
    && camera.width !== null
    && camera.height !== null
  );
}
