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

  const navigation = screen.getByRole("navigation", { name: "Segment 数字序号跳转" });
  expect(within(navigation).getAllByRole("button").map((button) => button.textContent)).toEqual([
    "01Segment 01待标注",
    "02Segment 02草稿",
    "100Segment 100已提交",
  ]);
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

test("jumps to a globally matched ordinal from the compact input", () => {
  const onNavigate = vi.fn();
  const first = segment(1, "clip-a");
  const target = segment(100, "clip-b", "submitted");
  render(
    <SegmentQueuePanel
      job={queueJob([first, target])}
      onNavigate={onNavigate}
    />,
  );

  const input = screen.getByRole("spinbutton", { name: "跳转至序号" });
  expect(input).toHaveAttribute("min", "1");
  expect(input).toHaveAttribute("max", "100");
  expect(input).toHaveAttribute("step", "1");

  fireEvent.change(input, { target: { value: "100" } });
  fireEvent.click(screen.getByRole("button", { name: "跳转" }));

  expect(onNavigate).toHaveBeenCalledTimes(1);
  expect(onNavigate).toHaveBeenCalledWith(target.segment_ref);
});

test("submits an ordinal with Enter", () => {
  const onNavigate = vi.fn();
  const target = segment(2, "clip-a", "draft");
  render(
    <SegmentQueuePanel
      job={queueJob([segment(1, "clip-a"), target])}
      onNavigate={onNavigate}
    />,
  );

  const input = screen.getByRole("spinbutton", { name: "跳转至序号" });
  fireEvent.change(input, { target: { value: "2" } });
  fireEvent.keyDown(input, { key: "Enter" });

  expect(onNavigate).toHaveBeenCalledTimes(1);
  expect(onNavigate).toHaveBeenCalledWith(target.segment_ref);
});

test.each([
  ["", "请输入 Segment 序号。"],
  ["1.5", "请输入有效的整数序号。"],
  ["2", "未找到该序号对应的 Segment。"],
])("reports an inline error for invalid ordinal %s", (value, message) => {
  const onNavigate = vi.fn();
  render(
    <SegmentQueuePanel
      job={queueJob([segment(1, "clip-a"), segment(3, "clip-b")])}
      onNavigate={onNavigate}
    />,
  );

  const input = screen.getByRole("spinbutton", { name: "跳转至序号" });
  fireEvent.change(input, { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "跳转" }));

  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(input).toHaveAttribute("aria-describedby");
  expect(screen.getByText(message)).toHaveAttribute("id", input.getAttribute("aria-describedby"));
  expect(onNavigate).not.toHaveBeenCalled();
});

test("treats jumping to the current ordinal as a no-op", () => {
  const onNavigate = vi.fn();
  const current = segment(7, "clip-a", "draft");
  render(
    <SegmentQueuePanel
      currentSegmentRef={current.segment_ref}
      job={queueJob([current])}
      onNavigate={onNavigate}
    />,
  );

  const input = screen.getByRole("spinbutton", { name: "跳转至序号" });
  fireEvent.change(input, { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "跳转" }));

  expect(input).not.toHaveAttribute("aria-invalid");
  expect(onNavigate).not.toHaveBeenCalled();
});

test("renders an honest empty queue", () => {
  render(<SegmentQueuePanel job={queueJob([])} onNavigate={vi.fn()} />);

  expect(screen.getByRole("heading", { name: "Segment 队列" })).toBeVisible();
  expect(screen.getByText("准备完成后显示内部 Segment 队列。")).toBeVisible();
  expect(screen.queryByRole("navigation", { name: "Segment 数字序号跳转" })).not.toBeInTheDocument();
});
