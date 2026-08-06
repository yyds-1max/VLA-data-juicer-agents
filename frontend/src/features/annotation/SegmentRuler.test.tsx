import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { SegmentRuler } from "./SegmentRuler";
import type { SegmentRulerItem } from "./segmentRulerAdapters";
import type { AnnotationSegmentStatus, AnnotationSegmentSummary } from "./types";

function segment(
  ordinal: number,
  status: AnnotationSegmentStatus,
): AnnotationSegmentSummary {
  return {
    segment_ref: `segment_${String(ordinal).padStart(32, "0")}`,
    ordinal,
    source_clip: "20260623_145550",
    status,
    state_revision: 1,
    draft_revision: status === "draft" ? 1 : null,
    submitted_revision: status === "submitted" ? 1 : null,
    first_frame: null,
  };
}

const segments = [
  segment(1, "submitted"),
  segment(2, "draft"),
  segment(3, "pending_initial_annotation"),
  segment(4, "postprocessing_failed"),
];

test("renders real segment states as an accessible colored ruler", () => {
  render(
    <SegmentRuler
      segments={segments}
      currentSegmentRef={segments[1].segment_ref}
      onNavigate={vi.fn()}
    />,
  );

  expect(screen.getByRole("button", { name: "切换到 Segment 01，已提交" })).toHaveTextContent("已提交");
  expect(screen.getByRole("button", { name: "当前 Segment 02，草稿" })).toHaveAttribute(
    "aria-current",
    "step",
  );
  expect(screen.getByRole("button", { name: "切换到 Segment 03，待标注" })).toHaveTextContent("待标注");
  expect(screen.getByRole("button", { name: "切换到 Segment 04，后处理失败" })).toHaveTextContent("后处理失败");
});

test("supports tick, previous, next and numeric keyboard navigation", () => {
  const onNavigate = vi.fn();
  render(
    <SegmentRuler
      segments={segments}
      currentSegmentRef={segments[1].segment_ref}
      onNavigate={onNavigate}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "切换到 Segment 03，待标注" }));
  fireEvent.click(screen.getByRole("button", { name: "上一个 Segment" }));
  fireEvent.click(screen.getByRole("button", { name: "下一个 Segment" }));
  fireEvent.keyDown(window, { key: "4" });
  fireEvent.keyDown(window, { key: "Enter" });

  expect(onNavigate).toHaveBeenNthCalledWith(1, segments[2].segment_ref);
  expect(onNavigate).toHaveBeenNthCalledWith(2, segments[0].segment_ref);
  expect(onNavigate).toHaveBeenNthCalledWith(3, segments[2].segment_ref);
  expect(onNavigate).toHaveBeenNthCalledWith(4, segments[3].segment_ref);
});

test("disables navigation while the workbench is in a protected interaction", () => {
  const onNavigate = vi.fn();
  render(
    <SegmentRuler
      segments={segments}
      currentSegmentRef={segments[1].segment_ref}
      onNavigate={onNavigate}
      disabled
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "切换到 Segment 03，待标注" }));
  fireEvent.keyDown(window, { key: "4" });
  fireEvent.keyDown(window, { key: "Enter" });
  expect(onNavigate).not.toHaveBeenCalled();
});

test("uses the reference ruler window and fades overflowing queue edges", () => {
  const longQueue = Array.from({ length: 24 }, (_, index) => (
    segment(index + 1, index < 8 ? "submitted" : "pending_initial_annotation")
  ));
  const { container } = render(
    <SegmentRuler
      segments={longQueue}
      currentSegmentRef={longQueue[12].segment_ref}
      onNavigate={vi.fn()}
    />,
  );

  const ticks = Array.from(container.querySelectorAll<HTMLButtonElement>("[aria-label='Segment 状态刻度'] > button"));
  expect(ticks).toHaveLength(21);
  expect(ticks[0]).toHaveStyle({ opacity: "0.12" });
  expect(ticks.at(-1)).toHaveStyle({ opacity: "0.12" });
  expect(screen.getByRole("button", { name: "当前 Segment 13，待标注" }))
    .toHaveAttribute("aria-current", "step");
});

test("renders the business-neutral model with configurable labels and resolved count", () => {
  const items: SegmentRulerItem[] = [
    { id: "review-b", ordinal: 2, label: "修正中", tone: "info", resolved: false },
    { id: "review-a", ordinal: 1, label: "已验证", tone: "success", resolved: true },
    { id: "review-c", ordinal: 3, label: "发布失败", tone: "danger", resolved: false },
  ];
  const onNavigate = vi.fn();

  render(
    <SegmentRuler
      items={items}
      currentId="review-b"
      itemLabel="轨迹"
      resolvedLabel="已验证"
      onNavigate={onNavigate}
    />,
  );

  expect(screen.getByRole("button", { name: "当前 轨迹 02，修正中" }))
    .toHaveAttribute("aria-current", "step");
  expect(screen.getByText("已验证", { selector: "div > div" })).toHaveTextContent("已验证 1 / 3");

  fireEvent.click(screen.getByRole("button", { name: "上一个 轨迹" }));
  fireEvent.keyDown(window, { key: "3" });
  fireEvent.keyDown(window, { key: "Enter" });

  expect(onNavigate).toHaveBeenNthCalledWith(1, "review-a");
  expect(onNavigate).toHaveBeenNthCalledWith(2, "review-c");
});
