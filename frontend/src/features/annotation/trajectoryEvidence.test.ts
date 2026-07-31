import {
  cameraCanRender,
  evidenceMatchesReview,
  parseTrajectoryReviewEvidence,
  projectTrajectoryReviewEvidence,
  trajectoryPositionPath,
} from "./trajectoryEvidence";
import type {
  TrajectoryReview,
  TrajectoryReviewEvidence,
} from "./types";

const reviewRef = "review_0123456789abcdef0123456789abcdef";
const trajectoryRef =
  "trajectory_revision_0123456789abcdef0123456789abcdef";
const targetRef = "target_0123456789abcdef0123456789abcdef";

function payload(): Record<string, unknown> {
  return {
    availability: "available",
    review_ref: reviewRef,
    evidence_kind: "trajectory_revision",
    fix_revision_ref: null,
    fix_revision_source_draft_revision: null,
    trajectory_revision_ref: trajectoryRef,
    review_state_revision: 6,
    draft_revision: 6,
    frame_count: 2,
    frames: [
      {
        frame_index: 0,
        pass: false,
        camera: {
          url: `/api/annotation/reviews/${reviewRef}/evidence/frames/0/camera`,
          width: 1920,
          height: 1536,
        },
        projection: {
          url: `/api/annotation/reviews/${reviewRef}/evidence/frames/0/projection`,
          width: 3840,
          height: 1536,
        },
        gridmap: {
          url: `/api/annotation/reviews/${reviewRef}/evidence/frames/0/gridmap`,
          width: 320,
          height: 240,
          resolution: 0.1,
          x_range: [-12, 12],
          y_range: [-12, 12],
        },
        targets: [{
          target_ref: targetRef,
          label: "Master",
          position: [1, 2],
          direction: 0,
          speed: 1,
          color: ["black", "gray", "white"],
          image_box: [10, 20, 30, 40],
          trajectory_points: [[1, 2], [3, 4, 5]],
          camera_position: null,
          camera_trajectory_points: [],
          base_position: [1, 2],
          base_direction: 0,
          base_speed: 1,
          base_trajectory_points: [[1, 2], [3, 4, 5]],
        }],
      },
      {
        frame_index: 1,
        pass: false,
        camera: null,
        projection: null,
        gridmap: null,
        targets: [{
          target_ref: targetRef,
          label: "Master",
          position: null,
          direction: 1,
          speed: 2,
          color: [],
          image_box: null,
          trajectory_points: [],
          camera_position: null,
          camera_trajectory_points: [],
          base_position: null,
          base_direction: 1,
          base_speed: 2,
          base_trajectory_points: [],
        }],
      },
    ],
    draft_commands: [
      {
        kind: "set_position",
        frame_index: 0,
        target_ref: targetRef,
        x: 5,
        y: 6,
      },
      { kind: "toggle_pass", frame_index: 0, value: true },
      { kind: "restore_frame", frame_index: 0 },
      {
        kind: "add_missing_target",
        frame_index: 1,
        target_ref: targetRef,
      },
      {
        kind: "set_direction",
        frame_index: 1,
        target_ref: targetRef,
        direction: 2.5,
      },
    ],
  };
}

test("strict evidence parser preserves only the public schema", () => {
  const evidence = parseTrajectoryReviewEvidence(payload(), reviewRef);

  expect(evidence).toMatchObject({
    availability: "available",
    review_ref: reviewRef,
    trajectory_revision_ref: trajectoryRef,
    review_state_revision: 6,
    draft_revision: 6,
    frame_count: 2,
  });
  expect(evidence.frames[0].targets[0].position).toEqual([1, 2]);
  expect(cameraCanRender(evidence.frames[0].camera)).toBe(true);
  expect(cameraCanRender(evidence.frames[1].camera)).toBe(false);
});

test("strict evidence parser accepts a Fix revision with only trailing draft commands", () => {
  const value = payload();
  value.evidence_kind = "fix_revision";
  value.fix_revision_ref =
    "fix_revision_0123456789abcdef0123456789abcdef";
  value.fix_revision_source_draft_revision = 4;
  value.draft_commands = (
    value.draft_commands as Array<Record<string, unknown>>
  ).slice(3);
  const frames = value.frames as Array<Record<string, unknown>>;
  const targets = frames[0].targets as Array<Record<string, unknown>>;
  targets[0].camera_position = [100, 200];
  targets[0].camera_trajectory_points = [[100, 200], [110, 210]];

  const evidence = parseTrajectoryReviewEvidence(value, reviewRef);

  expect(evidence).toMatchObject({
    evidence_kind: "fix_revision",
    fix_revision_ref:
      "fix_revision_0123456789abcdef0123456789abcdef",
    fix_revision_source_draft_revision: 4,
    draft_revision: 6,
  });
  expect(evidence.draft_commands).toHaveLength(2);
  expect(evidence.frames[0].targets[0].camera_position).toEqual([100, 200]);
});

test("draft projection replays direct commands and never invents runtime-derived positions", () => {
  const evidence = parseTrajectoryReviewEvidence(payload(), reviewRef);
  const projected = projectTrajectoryReviewEvidence(evidence);

  expect(projected.frames[0]).toMatchObject({
    pass: false,
    original_pass: false,
  });
  expect(projected.frames[0].targets[0]).toMatchObject({
    position: { x: 1, y: 2 },
    projection: "original",
  });
  expect(projected.frames[1].targets[0]).toMatchObject({
    present: true,
    position: null,
    direction: 2.5,
    projection: "runtime_derived",
  });
  expect(trajectoryPositionPath(projected, targetRef, "original")).toEqual([
    { x: 1, y: 2 },
    null,
  ]);
  expect(trajectoryPositionPath(projected, targetRef, "projected")).toEqual([
    { x: 1, y: 2 },
    null,
  ]);
});

test("evidence binding includes trajectory, review and draft revisions", () => {
  const evidence = parseTrajectoryReviewEvidence(payload(), reviewRef);
  const review = {
    review_ref: reviewRef,
    state_revision: 6,
    trajectory_revision: {
      revision_ref: trajectoryRef,
    },
    fix_draft: { revision: 6 },
  } as TrajectoryReview;

  expect(evidenceMatchesReview(evidence, review)).toBe(true);
  expect(evidenceMatchesReview(evidence, {
    ...review,
    state_revision: 7,
  })).toBe(false);
});

test.each([
  ["unknown fields", (value: Record<string, unknown>) => {
    value.private_artifact_path = "/private/runtime";
  }],
  ["mismatched review", (value: Record<string, unknown>) => {
    value.review_ref = "review_11111111111111111111111111111111";
  }],
  ["non-contiguous frames", (value: Record<string, unknown>) => {
    const frames = value.frames as Array<Record<string, unknown>>;
    frames[1].frame_index = 4;
  }],
  ["unbound media URL", (value: Record<string, unknown>) => {
    const frames = value.frames as Array<Record<string, unknown>>;
    const camera = frames[0].camera as Record<string, unknown>;
    camera.url = "/private/frame.jpg";
  }],
  ["gridmap without PNG dimensions", (value: Record<string, unknown>) => {
    const frames = value.frames as Array<Record<string, unknown>>;
    const gridmap = frames[0].gridmap as Record<string, unknown>;
    delete gridmap.width;
  }],
  ["draft log/revision mismatch", (value: Record<string, unknown>) => {
    value.draft_revision = 5;
  }],
])("rejects %s", (_label, mutate) => {
  const value = payload();
  mutate(value);
  expect(() => parseTrajectoryReviewEvidence(value, reviewRef)).toThrow(
    "服务器返回的轨迹证据不符合公开契约。",
  );
});

test("available evidence type documents the backend envelope", () => {
  const evidence: TrajectoryReviewEvidence = parseTrajectoryReviewEvidence(
    payload(),
    reviewRef,
  );
  expect(evidence.draft_commands).toHaveLength(5);
});
