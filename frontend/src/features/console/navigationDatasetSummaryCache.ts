import { useCallback, useEffect, useRef, useState } from "react";

import { getNavigationDatasetSummary } from "../../api/client";
import type { NavigationDatasetSummary } from "../../api/types";

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

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "导航数据汇总加载失败";
}

function startNavigationDatasetSummaryRequest(kind: "load" | "reload") {
  const requestGeneration = cacheGeneration;
  const promise = getNavigationDatasetSummary()
    .then((summary) => {
      if (cacheGeneration === requestGeneration) {
        cachedSummary = summary;
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
  return startNavigationDatasetSummaryRequest("reload");
}

export function getNavigationDatasetSummaryCached(): Promise<NavigationDatasetSummary> {
  return loadNavigationDatasetSummary();
}

export function resetNavigationDatasetSummaryCache() {
  cacheGeneration += 1;
  cachedSummary = null;
  pendingSummary = null;
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
