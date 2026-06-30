import { useEffect, useState } from "react";

import { getNavigationDatasetSummary } from "../../api/client";
import type { NavigationDatasetSummary } from "../../api/types";

type NavigationDatasetSummaryState = {
  summary: NavigationDatasetSummary | null;
  loading: boolean;
  error: string | null;
};

let cachedSummary: NavigationDatasetSummary | null = null;
let pendingSummary: Promise<NavigationDatasetSummary> | null = null;

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "导航数据汇总加载失败";
}

function loadNavigationDatasetSummary() {
  if (cachedSummary) {
    return Promise.resolve(cachedSummary);
  }

  if (pendingSummary) {
    return pendingSummary;
  }

  pendingSummary = getNavigationDatasetSummary()
    .then((summary) => {
      cachedSummary = summary;
      return summary;
    })
    .finally(() => {
      pendingSummary = null;
    });

  return pendingSummary;
}

export function resetNavigationDatasetSummaryCache() {
  cachedSummary = null;
  pendingSummary = null;
}

export function useNavigationDatasetSummary(): NavigationDatasetSummaryState {
  const [state, setState] = useState<NavigationDatasetSummaryState>(() =>
    cachedSummary
      ? { summary: cachedSummary, loading: false, error: null }
      : { summary: null, loading: true, error: null },
  );

  useEffect(() => {
    let active = true;

    if (cachedSummary) {
      setState({ summary: cachedSummary, loading: false, error: null });
      return () => {
        active = false;
      };
    }

    setState({ summary: null, loading: true, error: null });
    loadNavigationDatasetSummary()
      .then((summary) => {
        if (active) {
          setState({ summary, loading: false, error: null });
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setState({ summary: null, loading: false, error: errorMessage(error) });
        }
      });

    return () => {
      active = false;
    };
  }, []);

  return state;
}
