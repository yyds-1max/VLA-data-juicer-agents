import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { SegmentRuler } from "./SegmentRuler";
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

  expect(screen.getByRole("button", { name: "Segment 01，已提交" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Segment 02，草稿" })).toHaveAttribute(
    "aria-current",
    "step",
  );
  expect(screen.getByRole("button", { name: "Segment 03，待标注" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Segment 04，后处理失败" })).toBeVisible();
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

  fireEvent.click(screen.getByRole("button", { name: "Segment 03，待标注" }));
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

  fireEvent.click(screen.getByRole("button", { name: "Segment 03，待标注" }));
  fireEvent.keyDown(window, { key: "4" });
  fireEvent.keyDown(window, { key: "Enter" });
  expect(onNavigate).not.toHaveBeenCalled();
});
