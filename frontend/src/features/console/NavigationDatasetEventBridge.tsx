import { useEffect } from "react";

import { scheduleNavigationDatasetDateRefresh } from "./navigationDatasetSummaryCache";

const NAVIGATION_DATASET_EVENTS_ROOT = "/api/navigation/datasets/events";

export type NavigationDatasetDomainEvent = {
  seq: number;
  event_ref: string;
  event_kind: "navigation.task.changed";
  dataset_date: string;
  task_ref?: string;
  status?: string;
  phase?: string;
  state_revision: number;
  occurred_at: string;
};

export function parseNavigationDatasetDomainEvent(
  raw: string,
): NavigationDatasetDomainEvent | null {
  try {
    const value = JSON.parse(raw) as Partial<NavigationDatasetDomainEvent>;
    if (
      !Number.isSafeInteger(value.seq)
      || Number(value.seq) < 0
      || typeof value.event_ref !== "string"
      || value.event_kind !== "navigation.task.changed"
      || typeof value.dataset_date !== "string"
      || !/^\d{8}$/.test(value.dataset_date)
      || !Number.isSafeInteger(value.state_revision)
      || typeof value.occurred_at !== "string"
    ) {
      return null;
    }
    return value as NavigationDatasetDomainEvent;
  } catch {
    return null;
  }
}

async function getNavigationDatasetEventCursor(signal: AbortSignal): Promise<number> {
  const response = await fetch(`${NAVIGATION_DATASET_EVENTS_ROOT}/cursor`, {
    headers: { accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new Error("Navigation dataset event cursor unavailable");
  const body = await response.json() as { cursor?: unknown };
  if (!Number.isSafeInteger(body.cursor) || Number(body.cursor) < 0) {
    throw new Error("Navigation dataset event cursor is invalid");
  }
  return Number(body.cursor);
}

/**
 * One application-wide stream invalidates the affected dataset date whenever
 * DataPilot advances an extraction, synchronization, or later navigation task.
 * The cache performs a targeted date read; the explicit Refresh button remains
 * the authoritative full filesystem rescan.
 */
export function NavigationDatasetEventBridge() {
  useEffect(() => {
    if (typeof EventSource === "undefined") return undefined;
    const controller = new AbortController();
    let source: EventSource | null = null;
    let stopped = false;
    let lastSeq = -1;

    void (async () => {
      let cursor = 0;
      try {
        cursor = await getNavigationDatasetEventCursor(controller.signal);
      } catch {
        if (controller.signal.aborted) return;
      }
      if (stopped) return;
      lastSeq = cursor;
      source = new EventSource(
        `${NAVIGATION_DATASET_EVENTS_ROOT}?after_seq=${encodeURIComponent(String(cursor))}`,
      );
      source.addEventListener("navigation_dataset", (rawEvent) => {
        const event = parseNavigationDatasetDomainEvent(
          (rawEvent as MessageEvent<string>).data,
        );
        if (!event || event.seq <= lastSeq) return;
        lastSeq = event.seq;
        scheduleNavigationDatasetDateRefresh(event.dataset_date);
      });
    })();

    return () => {
      stopped = true;
      controller.abort();
      source?.close();
    };
  }, []);

  return null;
}
