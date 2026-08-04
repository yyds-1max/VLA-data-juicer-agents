import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import type {
  AnnotationJobDetail,
  AnnotationSegmentSummary,
  AnnotationSegmentStatus,
} from "./types";
import { SegmentQueuePanel } from "./SegmentQueuePanel";

function segment(
  ordinal: number,
  sourceClip: string,
  status: AnnotationSegmentStatus = "pending_initial_annotation",
): AnnotationSegmentSummary {
  return {
    segment_ref: `segment_${String(ordinal).padStart(32, "0")}`,
    ordinal,
    source_clip: sourceClip,
    status,
    state_revision: 1,
    draft_revision: null,
    submitted_revision: null,
    first_frame: null,
  };
}

function queueJob(segments: AnnotationSegmentSummary[]): Pick<AnnotationJobDetail, "segments" | "counts"> {
  return {
    segments,
    counts: {
      total: segments.length,
      pending_initial_annotation: segments.filter((item) => item.status === "pending_initial_annotation").length,
      draft: segments.filter((item) => item.status === "draft").length,
      submitted: segments.filter((item) => item.status === "submitted").length,
      skipped: segments.filter((item) => item.status === "skipped").length,
      tracking: segments.filter((item) => item.status === "tracking").length,
      tracked: segments.filter((item) => item.status === "tracked").length,
      postprocessing: segments.filter((item) => item.status === "postprocessing").length,
      annotated: segments.filter((item) => item.status === "annotated").length,
      postprocessing_failed: segments.filter((item) => item.status === "postprocessing_failed").length,
    },
  };
}

test("groups by clip and orders numeric Segment navigation by ordinal", () => {
  render(
    <SegmentQueuePanel
      job={queueJob([
        segment(2, "clip-a", "draft"),
        segment(100, "clip-b", "submitted"),
        segment(1, "clip-a"),
      ])}
      onNavigate={vi.fn()}
    />,
  );

  const navigation = screen.getByRole("navigation", { name: "Segment 分组队列" });
  expect(within(navigation).getByRole("button", { name: "收起外层 clip clip-a" })).toHaveAttribute("aria-expanded", "true");
  expect(within(navigation).getByRole("button", { name: "展开外层 clip clip-b" })).toHaveAttribute("aria-expanded", "false");
  expect(within(navigation).getByRole("button", { name: "打开 Segment 01，待标注" })).toBeVisible();
  expect(within(navigation).getByRole("button", { name: "打开 Segment 02，草稿" })).toBeVisible();
  expect(within(navigation).queryByRole("button", { name: "打开 Segment 100，已提交" })).not.toBeInTheDocument();

  fireEvent.click(within(navigation).getByRole("button", { name: "展开外层 clip clip-b" }));
  expect(within(navigation).getByRole("button", { name: "打开 Segment 100，已提交" })).toBeVisible();
  expect(screen.getByRole("region", { name: "clip-a" })).toBeVisible();
  expect(screen.getByRole("region", { name: "clip-b" })).toBeVisible();
  expect(screen.getByTestId("segment-queue-scroll")).toHaveClass("overflow-y-auto");
});

test("marks the current Segment and only calls navigation for another valid item", () => {
  const onNavigate = vi.fn();
  const first = segment(1, "clip-a");
  const second = segment(2, "clip-a", "draft");
  render(
    <SegmentQueuePanel
      currentSegmentRef={first.segment_ref}
      job={queueJob([first, second])}
      onNavigate={onNavigate}
    />,
  );

  const current = screen.getByRole("button", { name: "当前 Segment 01，待标注" });
  expect(current).toHaveAttribute("aria-current", "page");
  fireEvent.click(current);
  expect(onNavigate).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "打开 Segment 02，草稿" }));
  expect(onNavigate).toHaveBeenCalledTimes(1);
  expect(onNavigate).toHaveBeenCalledWith(second.segment_ref);
});

test("opens the group containing the current Segment and leaves other groups collapsed", () => {
  const current = segment(7, "clip-b", "draft");
  render(
    <SegmentQueuePanel
      currentSegmentRef={current.segment_ref}
      job={queueJob([segment(1, "clip-a"), current])}
      onNavigate={vi.fn()}
    />,
  );

  expect(screen.getByRole("button", { name: "展开外层 clip clip-a" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByRole("button", { name: "收起外层 clip clip-b" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "当前 Segment 07，草稿" })).toHaveAttribute("aria-current", "page");
});

test("shows per-clip Segment totals and supports independent expand and collapse", () => {
  render(
    <SegmentQueuePanel
      job={queueJob([
        segment(1, "clip-a", "submitted"),
        segment(2, "clip-a", "draft"),
        segment(3, "clip-b", "skipped"),
      ])}
      onNavigate={vi.fn()}
    />,
  );

  const firstGroup = screen.getByRole("button", { name: "收起外层 clip clip-a" });
  const secondGroup = screen.getByRole("button", { name: "展开外层 clip clip-b" });
  expect(firstGroup).toHaveTextContent("2 个 Segment");
  expect(firstGroup).toHaveTextContent("1 个待标注");
  expect(secondGroup).toHaveTextContent("1 个 Segment");
  expect(secondGroup).toHaveTextContent("全部已处理");

  fireEvent.click(firstGroup);
  expect(screen.getByRole("button", { name: "展开外层 clip clip-a" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("button", { name: "打开 Segment 01，已提交" })).not.toBeInTheDocument();
  expect(screen.getByText("选择一个外层 clip")).toBeVisible();

  fireEvent.click(secondGroup);
  expect(screen.getByRole("button", { name: "收起外层 clip clip-b" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "打开 Segment 03，已跳过" })).toBeVisible();
  expect(screen.queryByText("选择一个外层 clip")).not.toBeInTheDocument();
});

test("renders an honest empty queue", () => {
  render(<SegmentQueuePanel job={queueJob([])} onNavigate={vi.fn()} />);

  expect(screen.getByRole("heading", { name: "Segment 队列" })).toBeVisible();
  expect(screen.getByText("准备完成后显示内部 Segment 队列。")).toBeVisible();
  expect(screen.queryByRole("navigation", { name: "Segment 分组队列" })).not.toBeInTheDocument();
  expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
});

test("shows compact completion and only non-zero queue status totals", () => {
  render(
    <SegmentQueuePanel
      job={queueJob([
        segment(1, "clip-a", "submitted"),
        segment(2, "clip-a", "draft"),
        segment(3, "clip-a", "skipped"),
      ])}
      onNavigate={vi.fn()}
    />,
  );

  expect(screen.getByText("2 / 3 完成")).toBeVisible();
  const summary = screen.getByRole("list", { name: "Segment 状态汇总" });
  expect(within(summary).getByText("已提交")).toBeVisible();
  expect(within(summary).getByText("草稿")).toBeVisible();
  expect(within(summary).getByText("已跳过")).toBeVisible();
  expect(within(summary).queryByText("待标注")).not.toBeInTheDocument();
});
