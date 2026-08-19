import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as navigationApi from "../../api/client";
import * as annotationApi from "../annotation/api";
import type { TrajectoryReview, TrajectoryReviewEvidence } from "../annotation/types";
import { TrainingDataReviewPanel } from "./TrainingDataReviewPanel";

vi.mock("../../api/client", () => ({
  getNavigationDatasetReleases: vi.fn(),
  createNavigationDatasetRelease: vi.fn(),
}));

vi.mock("../annotation/api", () => ({
  listTrajectoryReviews: vi.fn(),
  getTrajectoryReviewEvidence: vi.fn(),
}));

const review: TrajectoryReview = {
  review_ref: "review_0123456789abcdef0123456789abcdef",
  status: "approved",
  state_revision: 5,
  job_ref: "job_0123456789abcdef0123456789abcdef",
  dataset_date: "20260814",
  source_clip: "clip-a",
  segment_ref: "segment_0123456789abcdef0123456789abcdef",
  segment_ordinal: 1,
  trajectory_revision: {
    revision_ref: "trajectory_revision_0123456789abcdef0123456789abcdef",
    content_sha256: "a".repeat(64),
  },
  processing_calibration: {
    profile_ref: "profile-a",
    label: "默认标定",
    content_sha256: "b".repeat(64),
  },
  fix_draft: null,
  fix_revisions: [],
  active_fix_run: null,
  fix_failure: null,
  latest_publication: {
    fix_revision_ref: "fix_revision_0123456789abcdef0123456789abcdef",
    attempt: 1,
    status: "published",
    content_sha256: "c".repeat(64),
    failure: null,
    created_at: "2026-08-14T10:00:00Z",
  },
  created_at: "2026-08-14T09:00:00Z",
  updated_at: "2026-08-14T10:00:00Z",
};

const evidence: TrajectoryReviewEvidence = {
  availability: "available",
  review_ref: review.review_ref,
  evidence_kind: "fix_revision",
  fix_revision_ref: review.latest_publication!.fix_revision_ref,
  fix_revision_source_draft_revision: 2,
  trajectory_revision_ref: review.trajectory_revision.revision_ref,
  review_state_revision: review.state_revision,
  draft_revision: 2,
  frame_count: 1,
  frames: [{
    frame_index: 0,
    pass: true,
    camera: { url: "/camera.png", width: 640, height: 480 },
    projection: null,
    gridmap: {
      url: "/gridmap.png",
      width: 400,
      height: 400,
      resolution: 0.1,
      x_range: [-20, 20],
      y_range: [-20, 20],
    },
    targets: [{
      target_ref: "target_0123456789abcdef0123456789abcdef",
      label: "行人 1",
      position: [1, 2],
      direction: 0.5,
      speed: 0.8,
      color: ["#22c55e"],
      image_box: null,
      trajectory_points: [[1, 2], [2, 3]],
      camera_position: [100, 120],
      camera_trajectory_points: [[90, 110], [100, 120]],
      base_position: [0, 0],
      base_direction: 0,
      base_speed: 0.5,
      base_trajectory_points: [[0, 0], [1, 1]],
    }],
  }],
  draft_commands: [],
};

function readyRelease() {
  return {
    dataset_date: "20260814",
    status: "ready" as const,
    release_ref: null,
    source_clip_count: 1,
    total_duration_ns: 1_000_000_000,
    verified_unit_count: 1,
    discarded_unit_count: 0,
    scope_manifest_sha256: "d".repeat(64),
    note: null,
    actor_kind: null,
    deployment_instance: null,
    released_at: null,
    updated_at: "2026-08-14T10:00:00Z",
  };
}

describe("TrainingDataReviewPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(navigationApi.getNavigationDatasetReleases).mockResolvedValue([
      readyRelease(),
      { ...readyRelease(), dataset_date: "20260813", status: "released", release_ref: "release-a" },
    ]);
    vi.mocked(annotationApi.listTrajectoryReviews).mockResolvedValue([review]);
    vi.mocked(annotationApi.getTrajectoryReviewEvidence).mockResolvedValue(evidence);
    vi.mocked(navigationApi.createNavigationDatasetRelease).mockResolvedValue({
      ...readyRelease(),
      status: "released",
      release_ref: "release-new",
      released_at: "2026-08-14T11:00:00Z",
    });
  });

  it("lists both pending and released dates and keeps their annotation results viewable", async () => {
    vi.mocked(annotationApi.listTrajectoryReviews).mockResolvedValue([
      { ...review, dataset_date: "20260813" },
    ]);
    render(<TrainingDataReviewPanel />);

    expect(await screen.findByText("2026-08-14")).toBeVisible();
    expect(screen.getByText("2026-08-13")).toBeVisible();
    const pendingRow = screen.getByText("2026-08-14").closest("tr");
    const releasedRow = screen.getByText("2026-08-13").closest("tr");
    expect(pendingRow).not.toBeNull();
    expect(releasedRow).not.toBeNull();
    expect(within(pendingRow!).getByText("待发布")).toBeVisible();
    expect(within(releasedRow!).getByText("已发布")).toBeVisible();
    fireEvent.click(within(releasedRow!).getByRole("button", { name: "查看" }));

    expect(await screen.findByText("修正后数据 · *_trajectory_fix_five.json")).toBeVisible();
    expect(annotationApi.listTrajectoryReviews).toHaveBeenCalledWith({ status: "approved", datasetDate: "20260813" });
    expect(screen.getByText("已发布")).toBeVisible();
    expect(screen.queryByRole("button", { name: "发布该日期" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "修正后相机投影" })).toBeVisible();
    expect(screen.getByRole("region", { name: "修正后 Gridmap" })).toBeVisible();
    expect(screen.getByText("相机投影").parentElement).toHaveClass("bottom-3", "left-3");
    expect(screen.getByText("Gridmap").parentElement).toHaveClass("bottom-3", "left-3");
    expect(annotationApi.getTrajectoryReviewEvidence).toHaveBeenCalledWith(review.review_ref);
    expect(screen.queryByLabelText("拖动目标位置")).not.toBeInTheDocument();
  });

  it("orders training dates by latest update while keeping dataset date as a tie-breaker", async () => {
    vi.mocked(navigationApi.getNavigationDatasetReleases).mockResolvedValue([
      {
        ...readyRelease(),
        dataset_date: "20260814",
        updated_at: "2026-08-15T10:00:00Z",
      },
      {
        ...readyRelease(),
        dataset_date: "20260813",
        updated_at: "2026-08-16T10:00:00Z",
      },
    ]);
    render(<TrainingDataReviewPanel />);

    await screen.findByText("2026-08-13");
    const dateCells = screen.getAllByText(/^2026-08-1[34]$/);
    expect(dateCells.map((cell) => cell.textContent)).toEqual([
      "2026-08-13",
      "2026-08-14",
    ]);
  });

  it("renders an imported historical result through the same native review experience", async () => {
    const historicalReview: TrajectoryReview = {
      ...review,
      source: "historical_import",
    };
    vi.mocked(annotationApi.listTrajectoryReviews).mockResolvedValue([
      historicalReview,
    ]);
    vi.mocked(annotationApi.getTrajectoryReviewEvidence).mockResolvedValue({
      ...evidence,
      evidence_kind: "historical_fix",
      frames: [{
        ...evidence.frames[0],
        projection: {
          url: "/historical-projection.png",
          width: 1280,
          height: 480,
        },
      }],
    });
    render(<TrainingDataReviewPanel />);

    fireEvent.click((await screen.findAllByRole("button", { name: "查看" }))[0]);

    expect(await screen.findByText("修正后数据 · *_trajectory_fix_five.json")).toBeVisible();
    const historicalProjection = screen.getByRole("img", { name: /Fix 结果投影/ });
    expect(historicalProjection).toHaveAttribute(
      "data-evidence-layout",
      "legacy-composite-camera",
    );
    expect(historicalProjection).toHaveAttribute("viewBox", "640 0 640 480");
    expect(historicalProjection.querySelector("image")).toHaveAttribute(
      "href",
      "/historical-projection.png",
    );
    expect(screen.getByRole("region", { name: "修正后 Gridmap" })).toBeVisible();
  });

  it("publishes only after the user has entered the date detail", async () => {
    const released = {
      ...readyRelease(),
      status: "released" as const,
      release_ref: "release-new",
      released_at: "2026-08-14T11:00:00Z",
      updated_at: "2026-08-14T11:00:00Z",
    };
    vi.mocked(navigationApi.getNavigationDatasetReleases)
      .mockResolvedValueOnce([readyRelease()])
      .mockResolvedValueOnce([released]);
    render(<TrainingDataReviewPanel />);

    expect(await screen.findByText("2026-08-14")).toBeVisible();
    expect(screen.queryByRole("button", { name: "发布该日期" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    expect(await screen.findByText("修正后数据 · *_trajectory_fix_five.json")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "发布该日期" }));
    fireEvent.change(screen.getByLabelText("发布备注（可选）"), { target: { value: "标注已抽查" } });
    fireEvent.click(screen.getByRole("button", { name: "确认发布" }));

    await waitFor(() => expect(navigationApi.createNavigationDatasetRelease).toHaveBeenCalledWith(
      "20260814",
      "d".repeat(64),
      "标注已抽查",
      expect.stringMatching(/^training-data-release-/),
    ));
    const toast = await screen.findByRole("status");
    expect(toast).toHaveTextContent(/已发布，可在新建训练中传输到训练节点/);
    expect(toast).toHaveClass("training-data-toast", "fixed", "top-4");
    expect(toast).toHaveAttribute("data-phase", "open");
    await waitFor(() => expect(screen.queryByRole("button", { name: "发布该日期" })).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "2026-08-14 标注结果" })).toBeVisible();
  });

  it("groups a large segment queue by clip and uses the bounded horizontal layout", async () => {
    const otherClipReview: TrajectoryReview = {
      ...review,
      review_ref: "review_1123456789abcdef0123456789abcdef",
      source_clip: "clip-b",
      segment_ref: "segment_1123456789abcdef0123456789abcdef",
      segment_ordinal: 9,
    };
    vi.mocked(annotationApi.listTrajectoryReviews).mockResolvedValue([review, otherClipReview]);
    render(<TrainingDataReviewPanel />);

    fireEvent.click((await screen.findAllByRole("button", { name: "查看" }))[0]);

    const queue = await screen.findByRole("complementary", { name: "Segment 复核队列" });
    expect(queue).toHaveAttribute("data-layout", "horizontal");
    expect(screen.getByRole("region", { name: "外层 clip clip-a" })).toBeVisible();
    expect(screen.getByRole("region", { name: "外层 clip clip-b" })).toBeVisible();
    expect(screen.getByTestId("review-segment-queue-scroll")).toHaveClass("overflow-x-scroll");
  });

  it("rejects non-fix evidence instead of showing the original trajectory", async () => {
    vi.mocked(annotationApi.getTrajectoryReviewEvidence).mockResolvedValue({
      ...evidence,
      evidence_kind: "trajectory_revision",
      fix_revision_ref: null,
      fix_revision_source_draft_revision: null,
    });
    render(<TrainingDataReviewPanel />);

    fireEvent.click((await screen.findAllByRole("button", { name: "查看" }))[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("当前记录没有可查看的修正后轨迹证据");
    expect(screen.queryByText("修正后数据 · *_trajectory_fix_five.json")).not.toBeInTheDocument();
  });
});
