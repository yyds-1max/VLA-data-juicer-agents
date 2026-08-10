import { useCallback, useEffect, useRef, useState } from "react";

import {
  getNavigationDatasetDate,
  getNavigationDatasetSummary,
} from "../../api/client";
import type {
  NavigationDatasetSummary,
  NavigationDateSummary,
} from "../../api/types";

type NavigationDatasetSummaryState = {
  summary: NavigationDatasetSummary | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
};

let cachedSummary: NavigationDatasetSummary | null = null;
let cacheGeneration = 0;
let pendingSummary: {
  generation: number;
  kind: "load" | "reload";
  promise: Promise<NavigationDatasetSummary>;
} | null = null;
const summaryListeners = new Set<(summary: NavigationDatasetSummary) => void>();
type DateRefreshGate = {
  dirty: boolean;
  pending: Promise<void> | null;
  timer: number | null;
};
const dateRefreshGates = new Map<string, DateRefreshGate>();
export const NAVIGATION_DATASET_EVENT_DEBOUNCE_MS = 250;

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "导航数据汇总加载失败";
}

function startNavigationDatasetSummaryRequest(kind: "load" | "reload") {
  const requestGeneration = cacheGeneration;
  const promise = getNavigationDatasetSummary()
    .then((summary) => {
      if (cacheGeneration === requestGeneration) {
        cachedSummary = summary;
        publishNavigationDatasetSummary(summary);
        resumeDeferredDateRefreshes();
      }
      return summary;
    })
    .finally(() => {
      if (pendingSummary?.promise === promise) {
        pendingSummary = null;
      }
    });

  pendingSummary = {
    generation: requestGeneration,
    kind,
    promise,
  };
  return promise;
}

function publishNavigationDatasetSummary(summary: NavigationDatasetSummary) {
  for (const listener of summaryListeners) listener(summary);
}

function aggregateNavigationDatasetSummary(
  dates: NavigationDateSummary[],
): NavigationDatasetSummary {
  const clips = dates.flatMap((date) => date.clips ?? []);
  return {
    totals: {
      date_count: dates.length,
      clip_count: dates.reduce((total, date) => total + date.clip_count, 0),
      total_duration_ns: dates.reduce(
        (total, date) => total + date.total_duration_ns,
        0,
      ),
      raw_message_count: dates.reduce(
        (total, date) => total + date.raw_message_count,
        0,
      ),
      extracted_clip_count: dates.reduce(
        (total, date) => total + date.extracted_clip_count,
        0,
      ),
      synced_clip_count: dates.reduce(
        (total, date) => total + date.synced_clip_count,
        0,
      ),
    },
    sync_distribution: {
      image: dates.reduce(
        (total, date) => total + date.sync_frame_counts.image,
        0,
      ),
      pointcloud: dates.reduce(
        (total, date) => total + date.sync_frame_counts.pointcloud,
        0,
      ),
      odom: dates.reduce(
        (total, date) => total + date.sync_frame_counts.odom,
        0,
      ),
      grid_map: dates.reduce(
        (total, date) => total + date.sync_frame_counts.grid_map,
        0,
      ),
    },
    annotation_totals: {
      annotated_clip_count: clips.reduce(
        (total, clip) => total + Number((clip.annotation?.annotated_unit_count ?? 0) > 0),
        0,
      ),
      annotated_duration_ns: clips.reduce(
        (total, clip) => total + ((clip.annotation?.annotated_unit_count ?? 0) > 0 ? clip.duration_ns : 0),
        0,
      ),
      verified_clip_count: clips.reduce(
        (total, clip) => total + Number(clip.annotation?.status === "verified"),
        0,
      ),
      annotated_unit_count: clips.reduce(
        (total, clip) => total + (clip.annotation?.annotated_unit_count ?? 0),
        0,
      ),
      verified_unit_count: clips.reduce(
        (total, clip) => total + (clip.annotation?.verified_unit_count ?? 0),
        0,
      ),
    },
    dates,
  };
}

function mergeNavigationDatasetDate(
  summary: NavigationDatasetSummary,
  date: NavigationDateSummary,
): NavigationDatasetSummary {
  const byDate = new Map(summary.dates.map((item) => [item.date, item]));
  byDate.set(date.date, date);
  return aggregateNavigationDatasetSummary(
    [...byDate.values()].sort((left, right) => left.date.localeCompare(right.date)),
  );
}

function dateRefreshGate(date: string): DateRefreshGate {
  const existing = dateRefreshGates.get(date);
  if (existing) return existing;
  const created: DateRefreshGate = { dirty: false, pending: null, timer: null };
  dateRefreshGates.set(date, created);
  return created;
}

async function refreshNavigationDatasetDate(
  date: string,
  gate: DateRefreshGate,
): Promise<void> {
  if (!cachedSummary || gate.pending) return gate.pending ?? Promise.resolve();
  const request = (async () => {
    while (gate.dirty && cachedSummary) {
      gate.dirty = false;
      const requestGeneration = cacheGeneration;
      const dateSummary = await getNavigationDatasetDate(date);
      if (cacheGeneration === requestGeneration && cachedSummary) {
        cachedSummary = mergeNavigationDatasetDate(cachedSummary, dateSummary);
        publishNavigationDatasetSummary(cachedSummary);
      }
    }
  })().finally(() => {
    if (gate.pending === request) gate.pending = null;
    if (!gate.dirty && gate.timer === null) dateRefreshGates.delete(date);
  });
  gate.pending = request;
  return request;
}

function armDateRefresh(date: string, gate: DateRefreshGate) {
  if (!cachedSummary || gate.timer !== null || gate.pending) return;
  gate.timer = window.setTimeout(() => {
    gate.timer = null;
    void refreshNavigationDatasetDate(date, gate);
  }, NAVIGATION_DATASET_EVENT_DEBOUNCE_MS);
}

function resumeDeferredDateRefreshes() {
  for (const [date, gate] of dateRefreshGates) {
    if (gate.dirty) armDateRefresh(date, gate);
  }
}

export function scheduleNavigationDatasetDateRefresh(date: string) {
  if (!/^\d{8}$/.test(date)) return;
  const gate = dateRefreshGate(date);
  gate.dirty = true;
  armDateRefresh(date, gate);
}

function clearDateRefreshes() {
  for (const gate of dateRefreshGates.values()) {
    if (gate.timer !== null) window.clearTimeout(gate.timer);
  }
  dateRefreshGates.clear();
}

function loadNavigationDatasetSummary() {
  if (cachedSummary) {
    return Promise.resolve(cachedSummary);
  }

  if (pendingSummary?.generation === cacheGeneration) {
    return pendingSummary.promise;
  }

  return startNavigationDatasetSummaryRequest("load");
}

function reloadNavigationDatasetSummary() {
  if (pendingSummary?.generation === cacheGeneration && pendingSummary.kind === "reload") {
    return pendingSummary.promise;
  }

  cacheGeneration += 1;
  cachedSummary = null;
  pendingSummary = null;
  clearDateRefreshes();
  return startNavigationDatasetSummaryRequest("reload");
}

export function getNavigationDatasetSummaryCached(): Promise<NavigationDatasetSummary> {
  return loadNavigationDatasetSummary();
}

export function resetNavigationDatasetSummaryCache() {
  cacheGeneration += 1;
  cachedSummary = null;
  pendingSummary = null;
  clearDateRefreshes();
}

export function useNavigationDatasetSummary(): NavigationDatasetSummaryState {
  const [state, setState] = useState<Omit<NavigationDatasetSummaryState, "reload">>(() =>
    cachedSummary
      ? { summary: cachedSummary, loading: false, error: null }
      : { summary: null, loading: true, error: null },
  );
  const mountedRef = useRef(false);
  const requestIdRef = useRef(0);
  const reloadPromiseRef = useRef<Promise<void> | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    const onSummary = (summary: NavigationDatasetSummary) => {
      if (mountedRef.current) {
        setState({ summary, loading: false, error: null });
      }
    };
    summaryListeners.add(onSummary);
    const requestId = ++requestIdRef.current;

    if (cachedSummary) {
      setState({ summary: cachedSummary, loading: false, error: null });
    } else {
      setState({ summary: null, loading: true, error: null });
      loadNavigationDatasetSummary()
        .then((summary) => {
          if (mountedRef.current && requestIdRef.current === requestId) {
            setState({ summary, loading: false, error: null });
          }
        })
        .catch((error: unknown) => {
          if (mountedRef.current && requestIdRef.current === requestId) {
            setState({ summary: null, loading: false, error: errorMessage(error) });
          }
        });
    }

    return () => {
      summaryListeners.delete(onSummary);
      mountedRef.current = false;
      requestIdRef.current += 1;
    };
  }, []);

  const reload = useCallback(() => {
    if (reloadPromiseRef.current) {
      return reloadPromiseRef.current;
    }

    const requestId = ++requestIdRef.current;
    if (mountedRef.current) {
      setState({ summary: null, loading: true, error: null });
    }

    const request = reloadNavigationDatasetSummary()
      .then(
        (summary) => {
          if (mountedRef.current && requestIdRef.current === requestId) {
            setState({ summary, loading: false, error: null });
          }
        },
        (error: unknown) => {
          if (mountedRef.current && requestIdRef.current === requestId) {
            setState({ summary: null, loading: false, error: errorMessage(error) });
          }
        },
      )
      .finally(() => {
        if (reloadPromiseRef.current === request) {
          reloadPromiseRef.current = null;
        }
      });

    reloadPromiseRef.current = request;
    return request;
  }, []);

  return { ...state, reload };
}
