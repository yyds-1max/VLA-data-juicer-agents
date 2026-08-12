import { describe, expect, it } from "vitest";

import { parseNavigationDatasetDomainEvent } from "./NavigationDatasetEventBridge";

describe("navigation dataset domain events", () => {
  it("accepts a path-free task invalidation", () => {
    expect(parseNavigationDatasetDomainEvent(JSON.stringify({
      seq: 8,
      event_ref: "navigation_dataset_event_public",
      event_kind: "navigation.task.changed",
      dataset_date: "20270605",
      task_ref: "DP-PUBLIC",
      status: "active",
      phase: "拆解与同步",
      state_revision: 5,
      occurred_at: "2026-08-10T10:00:00Z",
    }))).toMatchObject({
      seq: 8,
      dataset_date: "20270605",
      state_revision: 5,
    });
  });

  it("rejects malformed dates and private-looking incomplete payloads", () => {
    expect(parseNavigationDatasetDomainEvent(JSON.stringify({
      seq: 1,
      event_ref: "event",
      event_kind: "navigation.task.changed",
      dataset_date: "/private/data/20270605",
      state_revision: 1,
      occurred_at: "2026-08-10T10:00:00Z",
    }))).toBeNull();
  });
});
