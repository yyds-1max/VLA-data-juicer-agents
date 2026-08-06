import "@testing-library/jest-dom/vitest";
import { act, render } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import type { AnnotationDomainEvent } from "./events";

const bridgeMocks = vi.hoisted(() => ({
  useAnnotationEvents: vi.fn(),
  refreshAnnotationProjectionForEvent: vi.fn(),
  reconcileLoadedAnnotationProjections: vi.fn(),
}));

vi.mock("./events", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./events")>();
  return { ...actual, useAnnotationEvents: bridgeMocks.useAnnotationEvents };
});

vi.mock("./projectionStore", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./projectionStore")>();
  return {
    ...actual,
    refreshAnnotationProjectionForEvent:
      bridgeMocks.refreshAnnotationProjectionForEvent,
    reconcileLoadedAnnotationProjections:
      bridgeMocks.reconcileLoadedAnnotationProjections,
  };
});

import { AnnotationDomainEventBridge } from "./AnnotationDomainEventBridge";
import { resetAnnotationProjectionStore } from "./projectionStore";

beforeEach(() => {
  bridgeMocks.useAnnotationEvents.mockReset();
  bridgeMocks.refreshAnnotationProjectionForEvent.mockReset();
  bridgeMocks.reconcileLoadedAnnotationProjections.mockReset();
  resetAnnotationProjectionStore();
});

test("refreshes every aggregate without turning review events into global notifications", async () => {
  render(<AnnotationDomainEventBridge />);
  const options = bridgeMocks.useAnnotationEvents.mock.calls[0][0] as {
    onEvent: (event: AnnotationDomainEvent) => Promise<void>;
  };
  const reviewEvent: AnnotationDomainEvent = {
    seq: 1,
    event_ref: "event-review-published",
    event_kind: "annotation.review.changed",
    aggregate_kind: "review",
    job_ref: "job_0123456789abcdef0123456789abcdef",
    segment_ref: "segment_0123456789abcdef0123456789abcdef",
    review_ref: "review_0123456789abcdef0123456789abcdef",
    state_revision: 4,
    status: "approved",
    occurred_at: "2026-08-05T00:01:01Z",
  };

  await act(async () => options.onEvent(reviewEvent));
  await act(async () => options.onEvent(reviewEvent));
  await act(async () => options.onEvent({
    ...reviewEvent,
    seq: 2,
    event_ref: "event-job-changed",
    event_kind: "annotation.job.changed",
    aggregate_kind: "job",
    status: "tracked",
  }));
  expect(bridgeMocks.refreshAnnotationProjectionForEvent).toHaveBeenCalledTimes(3);
});
