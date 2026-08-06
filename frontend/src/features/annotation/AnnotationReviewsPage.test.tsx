import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  MemoryRouter,
  Route,
  Routes,
  useParams,
} from "react-router-dom";

import { listTrajectoryReviews } from "./api";
import { AnnotationReviewsPage } from "./AnnotationReviewsPage";
import { resetAnnotationProjectionStore } from "./projectionStore";
import type {
  CompatibilityPublicationSummary,
  TrajectoryReview,
  TrajectoryReviewStatus,
} from "./types";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, listTrajectoryReviews: vi.fn() };
});

const listReviewsMock = vi.mocked(listTrajectoryReviews);

function mockReducedMotion(matches: boolean) {
  const mediaQuery = {
    matches,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList;
  vi.stubGlobal("matchMedia", vi.fn(() => mediaQuery));
}

function mockAnimationFrames() {
  let nextHandle = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
    const handle = nextHandle;
    nextHandle += 1;
    callbacks.set(handle, callback);
    return handle;
  }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn((handle: number) => callbacks.delete(handle)));
  return {
    flushNext() {
      const next = callbacks.entries().next();
      if (next.done) return false;
      const [handle, callback] = next.value;
      callbacks.delete(handle);
      callback(performance.now());
      return true;
    },
  };
}

function reviewFixture(
  suffix: string,
  status: TrajectoryReviewStatus = "pending",
  overrides: Partial<TrajectoryReview> = {},
): TrajectoryReview {
  return {
    review_ref: `review_${suffix.padStart(32, "0")}`,
    status,
    state_revision: 1,
    job_ref: "job_0123456789abcdef0123456789abcdef",
    dataset_date: "20260804",
    source_clip: `outer-clip-${suffix}`,
    segment_ref: `segment_${suffix.padStart(32, "0")}`,
    segment_ordinal: Number(suffix),
    trajectory_revision: {
      revision_ref: `trajectory_revision_${suffix.padStart(32, "0")}`,
      content_sha256: "a".repeat(64),
    },
    processing_calibration: {
      profile_ref: "processing-calibration",
      label: "处理标定",
      content_sha256: "b".repeat(64),
    },
    fix_draft: null,
    fix_revisions: [],
    active_fix_run: null,
    fix_failure: null,
    latest_publication: null,
    created_at: "2026-08-04T08:00:00Z",
    updated_at: `2026-08-04T08:${suffix.padStart(2, "0")}:00Z`,
    ...overrides,
  };
}

function published(suffix: string): CompatibilityPublicationSummary {
  return {
    fix_revision_ref: `fix_revision_${suffix.padStart(32, "0")}`,
    attempt: 1,
    status: "published",
    content_sha256: "c".repeat(64),
    failure: null,
    created_at: "2026-08-04T09:00:00Z",
  };
}

function ReviewRouteProbe() {
  const { reviewRef } = useParams();
  return <div data-testid="opened-review">{reviewRef}</div>;
}

async function renderReviews(reviews: TrajectoryReview[]) {
  listReviewsMock.mockResolvedValue(reviews);
  render(
    <MemoryRouter
      initialEntries={["/annotation/reviews"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <Routes>
        <Route path="/annotation/reviews" element={<AnnotationReviewsPage />} />
        <Route path="/annotation/reviews/:reviewRef" element={<ReviewRouteProbe />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByRole("region", { name: "轨迹复核任务列表" });
}

beforeEach(() => {
  vi.clearAllMocks();
  resetAnnotationProjectionStore();
  mockReducedMotion(true);
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("AnnotationReviewsPage", () => {
  test("renders the six-column list and complete real-data detail popover without a list title", async () => {
    const pending = reviewFixture("1", "pending", { source_clip: "outer-clip-shared" });
    const verified = reviewFixture("2", "approved", {
      source_clip: "outer-clip-shared",
      latest_publication: published("2"),
      fix_draft: {
        revision: 1,
        content_sha256: "d".repeat(64),
        calibration: {
          profile_ref: "fix-calibration",
          label: "修整标定 v3",
          content_sha256: "e".repeat(64),
          differs_from_processing: true,
          difference_reason: "人工复核",
        },
      },
      updated_at: "2026-08-04T10:30:00Z",
    });
    await renderReviews([pending, verified]);

    expect(screen.queryByRole("heading", { name: "复核任务" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(6);
    expect(screen.queryByRole("columnheader", { name: /外层 clip/i })).not.toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /查看 20260804 outer-clip-shared 复核详情/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("轨迹复核进度")).toBeVisible();
    expect(within(dialog).getByText("状态分布")).toBeVisible();
    expect(within(dialog).getAllByText("待复核").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText("已验证").length).toBeGreaterThan(0);
    expect(within(dialog).getByText("修整标定 v3")).toBeVisible();
    expect(within(dialog).getByText("尚未选择")).toBeVisible();
    expect(within(dialog).getByText("最后更新时间")).toBeVisible();
    expect(within(dialog).getByText("外层 clips")).toBeVisible();
    expect(within(dialog).getByText("outer-clip-shared")).toBeVisible();
  });

  test("applies and clears an inclusive native date range", async () => {
    await renderReviews([
      reviewFixture("1", "pending", { dataset_date: "20260731" }),
      reviewFixture("2", "pending", { dataset_date: "20260802" }),
      reviewFixture("3", "pending", { dataset_date: "20260805" }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "复核日期范围：全部日期" }));
    fireEvent.change(await screen.findByLabelText("复核开始日期"), {
      target: { value: "2026-08-01" },
    });
    fireEvent.change(screen.getByLabelText("复核结束日期"), {
      target: { value: "2026-08-05" },
    });
    fireEvent.click(screen.getByRole("button", { name: "应用日期范围" }));

    await waitFor(() => expect(screen.queryByText("20260731")).not.toBeInTheDocument());
    expect(screen.getByText("20260802")).toBeVisible();
    expect(screen.getByText("20260805")).toBeVisible();
    expect(screen.getByRole("button", { name: "复核日期范围：08/01–08/05" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "清除复核日期范围" }));
    await waitFor(() => expect(screen.getByText("20260731")).toBeVisible());
  });

  test("supports 10/20/50 pagination and resets to page one when page size changes", async () => {
    const reviews = Array.from({ length: 12 }, (_, index) => reviewFixture(
      String(index + 1),
      "pending",
      {
        dataset_date: `202608${String(index + 1).padStart(2, "0")}`,
        source_clip: `page-clip-${String(index + 1).padStart(2, "0")}`,
        updated_at: "2026-08-04T08:00:00Z",
      },
    ));
    await renderReviews(reviews);

    expect(screen.getByText("第 1 / 2 页")).toBeVisible();
    expect(screen.getByText("20260801")).toBeVisible();
    expect(screen.queryByText("20260811")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByText("20260811")).toBeVisible());

    fireEvent.click(screen.getByRole("combobox", { name: "每页复核任务数量" }));
    fireEvent.click(await screen.findByRole("option", { name: "20" }));
    await waitFor(() => expect(screen.getByText("第 1 / 1 页")).toBeVisible());
    expect(screen.getByText("20260801")).toBeVisible();
  });

  test("stages row exit and entry and settles after the new rows commit", async () => {
    vi.unstubAllGlobals();
    mockReducedMotion(false);
    await renderReviews([
      reviewFixture("1", "pending", { dataset_date: "20260801" }),
      reviewFixture("2", "returned", { dataset_date: "20260802" }),
    ]);
    vi.useFakeTimers();
    const frames = mockAnimationFrames();

    fireEvent.click(screen.getByRole("button", { name: "待复核 1" }));

    const oldRow = screen.getByText("20260802").closest("tr");
    expect(oldRow).toHaveClass("opacity-0");
    expect(screen.getByRole("heading", { name: "人工复核" }).closest("section"))
      .toHaveAttribute("aria-busy", "true");

    act(() => vi.advanceTimersByTime(100));
    const nextRow = screen.getByText("20260801").closest("tr");
    expect(nextRow).toHaveClass("opacity-0");

    act(() => {
      expect(frames.flushNext()).toBe(true);
      expect(frames.flushNext()).toBe(true);
    });
    expect(nextRow).toHaveClass("opacity-100");
    expect(screen.getByRole("heading", { name: "人工复核" }).closest("section"))
      .not.toHaveAttribute("aria-busy");
  });

  test("switches filters immediately when reduced motion is requested", async () => {
    await renderReviews([
      reviewFixture("1", "pending", { dataset_date: "20260801" }),
      reviewFixture("2", "returned", { dataset_date: "20260802" }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "已退回 1" }));

    await waitFor(() => expect(screen.getByText("20260802")).toBeVisible());
    expect(screen.queryByText("20260801")).not.toBeInTheDocument();
    expect(screen.getByText("20260802").closest("tr")).toHaveClass("opacity-100");
  });

  test("opens a review that matches the selected status inside a complete outer-clip group", async () => {
    const inProgress = reviewFixture("1", "in_progress", {
      source_clip: "outer-clip-shared",
      segment_ordinal: 1,
    });
    const verified = reviewFixture("2", "approved", {
      source_clip: "outer-clip-shared",
      segment_ordinal: 2,
      latest_publication: published("2"),
    });
    await renderReviews([inProgress, verified]);

    fireEvent.click(screen.getByRole("button", { name: "已验证 1" }));
    await waitFor(() => expect(screen.getByText("1/2")).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "查看记录" }));

    expect(await screen.findByTestId("opened-review")).toHaveTextContent(verified.review_ref);
  });

  test("does not render an outer right divider on the final metric", async () => {
    await renderReviews([
      reviewFixture("1", "discarded", { dataset_date: "20260801" }),
    ]);

    expect(screen.getByRole("button", { name: "已废弃 1" })).not.toHaveClass("border-r");
  });
});
