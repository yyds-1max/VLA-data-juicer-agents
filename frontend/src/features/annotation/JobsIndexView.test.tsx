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

import { JobsIndexView, type JobsIndexViewProps } from "./JobsIndexView";
import type { AnnotationJobStatus, AnnotationJobSummary } from "./types";

function mockReducedMotion(matches: boolean) {
  let currentMatches = matches;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const mediaQuery = {
    get matches() {
      return currentMatches;
    },
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: vi.fn(
      (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    ),
    removeEventListener: vi.fn(
      (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    ),
    addListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => listeners.add(listener)),
    removeListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList;
  vi.stubGlobal("matchMedia", vi.fn(() => mediaQuery));
  return {
    setMatches(nextMatches: boolean) {
      currentMatches = nextMatches;
      const event = { matches: nextMatches, media: mediaQuery.media } as MediaQueryListEvent;
      listeners.forEach((listener) => listener(event));
    },
  };
}

function mockAnimationFrames() {
  let nextHandle = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) => {
      const handle = nextHandle;
      nextHandle += 1;
      callbacks.set(handle, callback);
      return handle;
    }),
  );
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((handle: number) => callbacks.delete(handle)),
  );

  return {
    flushNext() {
      const next = callbacks.entries().next();
      if (next.done) return false;
      const [handle, callback] = next.value;
      callbacks.delete(handle);
      callback(performance.now());
      return true;
    },
    pendingCount() {
      return callbacks.size;
    },
  };
}

function jobFixture(
  status: AnnotationJobStatus,
  overrides: Partial<AnnotationJobSummary> = {},
): AnnotationJobSummary {
  return {
    job_ref: `job-${status}`,
    dataset_date: "20260623",
    source_clips: ["clip-north-001", "clip-north-002"],
    status,
    cancel_requested: false,
    completion_outcome: null,
    state_revision: 1,
    calibration: {
      profile_ref: "calibration-profile",
      label: "20260529_go2w",
      content_sha256: "a".repeat(64),
    },
    counts: {
      total: 4,
      pending_initial_annotation: 0,
      draft: 0,
      submitted: 0,
      skipped: 0,
      tracking: 0,
      tracked: 0,
      postprocessing: 0,
      annotated: 0,
      postprocessing_failed: 0,
    },
    ready_for_tracking: false,
    ready_for_no_processable_targets: false,
    failure: null,
    created_at: "2026-08-01T08:00:00Z",
    updated_at: "2026-08-01T09:00:00Z",
    ...overrides,
  };
}

const defaultJobs = [
  jobFixture("waiting_initial_annotation", {
    counts: {
      ...jobFixture("waiting_initial_annotation").counts,
      total: 4,
      pending_initial_annotation: 2,
      draft: 1,
      submitted: 1,
    },
  }),
  jobFixture("tracked", {
    counts: {
      ...jobFixture("tracked").counts,
      total: 4,
      tracked: 3,
      skipped: 1,
    },
  }),
  jobFixture("failed", {
    failure: {
      code: "worker_failed",
      message: "Tracking worker 已退出",
      retryable: true,
      error_ref: "error-ref-1",
    },
  }),
  jobFixture("annotated", {
    counts: {
      ...jobFixture("annotated").counts,
      total: 4,
      annotated: 4,
    },
  }),
];

function renderView(overrides: Partial<JobsIndexViewProps> = {}) {
  const props: JobsIndexViewProps = {
    jobs: defaultJobs,
    loading: false,
    onRefresh: vi.fn(),
    onOpenDataPilot: vi.fn(),
    onPrimaryAction: vi.fn(),
    ...overrides,
  };
  return { ...render(<JobsIndexView {...props} />), props };
}

beforeEach(() => {
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

describe("JobsIndexView", () => {
  test("renders four metrics and a fixed seven-column waiting table", () => {
    renderView();

    expect(screen.getAllByText("待首帧标注").length).toBeGreaterThan(0);
    expect(screen.getAllByText("运行中").length).toBeGreaterThan(0);
    expect(screen.getByText("异常任务")).toBeInTheDocument();
    expect(screen.getByText("已标注")).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(7);
    expect(screen.getByRole("columnheader", { name: "数据日期" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Segment 进度" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /clip/i })).not.toBeInTheDocument();
    expect(screen.getByTestId("annotation-metrics-strip")).not.toHaveClass("rounded-xl");
    expect(screen.getByTestId("annotation-task-surface")).not.toHaveClass("rounded-xl");
    expect(screen.getByTestId("annotation-task-surface")).not.toHaveClass("shadow-sm");
    expect(screen.getByText("1/4")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看 20260623 任务详情" })).toHaveTextContent(
      "详情",
    );
  });

  test("switches filters immediately for reduced motion and keeps tracked running", async () => {
    renderView();

    fireEvent.click(screen.getByRole("tab", { name: /运行中/ }));

    expect(screen.getByRole("tab", { name: /运行中/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => expect(screen.getByText("Tracking 已完成")).toBeInTheDocument());
    expect(screen.queryByText("阶段 3/4")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看进度" })).toBeInTheDocument();
  });

  test("opens a detail popover with real summary data but no duplicated primary action", async () => {
    renderView();
    fireEvent.click(screen.getByRole("button", { name: "查看 20260623 任务详情" }));

    const popover = await screen.findByRole("dialog");
    expect(within(popover).getByText("clip-north-001")).toBeInTheDocument();
    expect(within(popover).getByText("20260529_go2w")).toBeInTheDocument();
    expect(within(popover).getByText("下一步")).toBeInTheDocument();
    expect(within(popover).getByText(/继续完成首帧标注/)).toBeInTheDocument();
    expect(within(popover).queryByRole("button", { name: "继续标注" })).not.toBeInTheDocument();
  });

  test("reports primary actions and toolbar actions through callbacks only", () => {
    const onRefresh = vi.fn();
    const onOpenDataPilot = vi.fn();
    const onPrimaryAction = vi.fn();
    renderView({ onRefresh, onOpenDataPilot, onPrimaryAction });

    fireEvent.click(screen.getByRole("button", { name: "刷新标注任务" }));
    fireEvent.click(screen.getByRole("button", { name: "交给 DataPilot 处理" }));
    fireEvent.click(screen.getByRole("button", { name: "继续标注" }));

    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onOpenDataPilot).toHaveBeenCalledTimes(1);
    expect(onPrimaryAction).toHaveBeenCalledWith(defaultJobs[0]);
  });

  test("supports 10-item pagination and changing page size to 20", async () => {
    const jobs = Array.from({ length: 12 }, (_, index) =>
      jobFixture("waiting_initial_annotation", {
        job_ref: `job-wait-${String(index + 1).padStart(2, "0")}`,
        dataset_date: `202606${String(index + 1).padStart(2, "0")}`,
        updated_at: "2026-08-01T09:00:00Z",
      }),
    );
    renderView({ jobs });

    expect(screen.getByText("第 1 / 2 页")).toBeInTheDocument();
    expect(screen.getByText("20260601")).toBeInTheDocument();
    expect(screen.queryByText("20260611")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByText("20260611")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("combobox", { name: "每页任务数量" }));
    fireEvent.click(await screen.findByRole("option", { name: "20" }));
    await waitFor(() => expect(screen.getByText("第 1 / 1 页")).toBeInTheDocument());
    expect(screen.getByText("20260601")).toBeInTheDocument();
  });

  test("renders stable loading, empty, error, refresh, and unavailable states", () => {
    const { rerender } = render(
      <JobsIndexView
        jobs={[]}
        loading={true}
        onRefresh={vi.fn()}
        onOpenDataPilot={vi.fn()}
        onPrimaryAction={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("正在加载标注任务")).toHaveAttribute("aria-busy", "true");

    rerender(
      <JobsIndexView
        jobs={[]}
        loading={false}
        refreshing={true}
        error="网络连接失败"
        capability={{
          available: false,
          runtime_id: "runtime-local",
          reason: { code: "runtime_offline", message: "运行环境未启动" },
        }}
        onRefresh={vi.fn()}
        onOpenDataPilot={vi.fn()}
        onPrimaryAction={vi.fn()}
      />,
    );

    expect(screen.getByText("没有待处理任务")).toBeInTheDocument();
    expect(screen.getByText("网络连接失败")).toBeInTheDocument();
    expect(screen.getByText("运行环境未启动")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "正在刷新标注任务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "交给 DataPilot 处理" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("正在刷新标注任务列表");
  });

  test("settles a staged transition after React commits the new rows", () => {
    vi.useFakeTimers();
    vi.unstubAllGlobals();
    mockReducedMotion(false);
    const animationFrames = mockAnimationFrames();
    renderView();

    fireEvent.click(screen.getByRole("tab", { name: /运行中/ }));
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("columnheader", { name: "数据日期" })).toBeVisible();

    act(() => {
      vi.advanceTimersByTime(100);
    });

    const runningRow = screen.getByText("Tracking 已完成").closest("tr");
    expect(runningRow).toHaveClass("opacity-0");
    expect(animationFrames.pendingCount()).toBe(1);

    act(() => {
      expect(animationFrames.flushNext()).toBe(true);
    });
    expect(runningRow).toHaveClass("opacity-0");

    act(() => {
      expect(animationFrames.flushNext()).toBe(true);
    });
    expect(runningRow).toHaveClass("opacity-100");
    expect(panel).not.toHaveAttribute("aria-busy");
  });

  test("cancels obsolete transitions during rapid filter changes", () => {
    vi.useFakeTimers();
    vi.unstubAllGlobals();
    mockReducedMotion(false);
    const animationFrames = mockAnimationFrames();
    renderView();

    fireEvent.click(screen.getByRole("tab", { name: "运行中" }));
    act(() => {
      vi.advanceTimersByTime(40);
    });
    fireEvent.click(screen.getByRole("tab", { name: "异常" }));
    act(() => {
      vi.advanceTimersByTime(100);
    });

    const failedRow = screen.getByText("处理失败").closest("tr");
    expect(failedRow).toHaveClass("opacity-0");
    expect(screen.queryByText("Tracking 已完成")).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "状态" })).toBeVisible();

    act(() => {
      animationFrames.flushNext();
      animationFrames.flushNext();
    });
    expect(failedRow).toHaveClass("opacity-100");
    expect(screen.getByRole("tabpanel")).not.toHaveAttribute("aria-busy");
  });

  test("finishes the current switch immediately when reduced motion turns on", () => {
    vi.useFakeTimers();
    vi.unstubAllGlobals();
    const motionPreference = mockReducedMotion(false);
    const animationFrames = mockAnimationFrames();
    renderView();

    fireEvent.click(screen.getByRole("tab", { name: "运行中" }));
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-busy", "true");

    act(() => {
      motionPreference.setMatches(true);
    });

    expect(screen.getByText("Tracking 已完成").closest("tr")).toHaveClass("opacity-100");
    expect(screen.getByRole("tabpanel")).not.toHaveAttribute("aria-busy");
    expect(animationFrames.pendingCount()).toBe(0);
  });

  test("clamps stale pages synchronously when refreshed data becomes shorter", () => {
    const jobs = Array.from({ length: 12 }, (_, index) =>
      jobFixture("waiting_initial_annotation", {
        job_ref: `job-clamp-${String(index + 1).padStart(2, "0")}`,
        dataset_date: `202607${String(index + 1).padStart(2, "0")}`,
        updated_at: "2026-08-01T09:00:00Z",
      }),
    );
    const { props, rerender } = renderView({ jobs });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByText("20260711")).toBeInTheDocument();

    rerender(<JobsIndexView {...props} jobs={jobs.slice(0, 2)} />);

    expect(screen.getByText("第 1 / 1 页")).toBeInTheDocument();
    expect(screen.getByText("20260701")).toBeInTheDocument();
    expect(screen.getByText(/2 个任务/)).toBeInTheDocument();
  });

  test("cleans a pending switch timer when the list unmounts", () => {
    vi.useFakeTimers();
    vi.unstubAllGlobals();
    mockReducedMotion(false);
    const animationFrames = mockAnimationFrames();
    const { unmount } = renderView();

    fireEvent.click(screen.getByRole("tab", { name: "运行中" }));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    unmount();

    expect(vi.getTimerCount()).toBe(0);
    expect(animationFrames.pendingCount()).toBe(0);
  });

  test("supports arrow-key navigation across the filter tabs", async () => {
    renderView();
    const waitingTab = screen.getByRole("tab", { name: /待我处理/ });
    waitingTab.focus();

    fireEvent.keyDown(waitingTab, { key: "ArrowRight" });

    const runningTab = screen.getByRole("tab", { name: /运行中/ });
    await waitFor(() => expect(runningTab).toHaveFocus());
    expect(runningTab).toHaveAttribute("aria-selected", "true");
  });
});
