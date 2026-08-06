import "@testing-library/jest-dom/vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getNavigationDatasetSummary } from "../../api/client";
import type { NavigationDatasetSummary } from "../../api/types";
import {
  getNavigationDatasetSummaryCached,
  resetNavigationDatasetSummaryCache,
  useNavigationDatasetSummary,
} from "./navigationDatasetSummaryCache";

vi.mock("../../api/client", () => ({
  getNavigationDatasetSummary: vi.fn(),
}));

const getSummaryMock = vi.mocked(getNavigationDatasetSummary);

function summary(totalDurationNs: number): NavigationDatasetSummary {
  return {
    totals: {
      date_count: 2,
      clip_count: 4,
      total_duration_ns: totalDurationNs,
      raw_message_count: 40,
      extracted_clip_count: 3,
      synced_clip_count: 2,
    },
    sync_distribution: {
      image: 10,
      pointcloud: 8,
      odom: 7,
      grid_map: 6,
    },
    dates: [],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("navigation dataset summary cache", () => {
  beforeEach(() => {
    resetNavigationDatasetSummaryCache();
    getSummaryMock.mockReset();
  });

  it("deduplicates pending reads and reuses the resolved cache", async () => {
    const pending = deferred<NavigationDatasetSummary>();
    getSummaryMock.mockReturnValue(pending.promise);

    const first = getNavigationDatasetSummaryCached();
    const second = getNavigationDatasetSummaryCached();

    expect(first).toBe(second);
    expect(getSummaryMock).toHaveBeenCalledTimes(1);

    const value = summary(1_000);
    pending.resolve(value);
    await expect(first).resolves.toEqual(value);
    await expect(getNavigationDatasetSummaryCached()).resolves.toEqual(value);
    expect(getSummaryMock).toHaveBeenCalledTimes(1);
  });

  it("does not let a request from an invalidated generation overwrite newer cache data", async () => {
    const oldRequest = deferred<NavigationDatasetSummary>();
    const newRequest = deferred<NavigationDatasetSummary>();
    getSummaryMock
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);

    const oldResult = getNavigationDatasetSummaryCached();
    resetNavigationDatasetSummaryCache();
    const newResult = getNavigationDatasetSummaryCached();

    oldRequest.resolve(summary(1_000));
    await oldResult;
    newRequest.resolve(summary(2_000));
    await newResult;

    await expect(getNavigationDatasetSummaryCached()).resolves.toMatchObject({
      totals: { total_duration_ns: 2_000 },
    });
    expect(getSummaryMock).toHaveBeenCalledTimes(2);
  });

  it("surfaces an initial error and reloads once for rapid repeated retries", async () => {
    const retryRequest = deferred<NavigationDatasetSummary>();
    getSummaryMock
      .mockRejectedValueOnce(new Error("网络连接失败"))
      .mockReturnValueOnce(retryRequest.promise);

    const { result } = renderHook(() => useNavigationDatasetSummary());

    await waitFor(() => {
      expect(result.current).toMatchObject({
        summary: null,
        loading: false,
        error: "网络连接失败",
      });
    });

    let firstRetry!: Promise<void>;
    let secondRetry!: Promise<void>;
    act(() => {
      firstRetry = result.current.reload();
      secondRetry = result.current.reload();
    });

    expect(firstRetry).toBe(secondRetry);
    expect(getSummaryMock).toHaveBeenCalledTimes(2);
    expect(result.current).toMatchObject({ summary: null, loading: true, error: null });

    const refreshed = summary(3_000);
    await act(async () => {
      retryRequest.resolve(refreshed);
      await firstRetry;
    });

    expect(result.current).toMatchObject({
      summary: refreshed,
      loading: false,
      error: null,
    });
  });

  it("ignores an older hook response after reload resolves", async () => {
    const initialRequest = deferred<NavigationDatasetSummary>();
    const reloadRequest = deferred<NavigationDatasetSummary>();
    getSummaryMock
      .mockReturnValueOnce(initialRequest.promise)
      .mockReturnValueOnce(reloadRequest.promise);

    const { result } = renderHook(() => useNavigationDatasetSummary());
    expect(getSummaryMock).toHaveBeenCalledTimes(1);

    let reloadPromise!: Promise<void>;
    act(() => {
      reloadPromise = result.current.reload();
    });
    expect(getSummaryMock).toHaveBeenCalledTimes(2);

    const refreshed = summary(4_000);
    await act(async () => {
      reloadRequest.resolve(refreshed);
      await reloadPromise;
    });
    expect(result.current.summary).toEqual(refreshed);

    await act(async () => {
      initialRequest.resolve(summary(500));
      await initialRequest.promise;
    });
    expect(result.current.summary).toEqual(refreshed);
    await expect(getNavigationDatasetSummaryCached()).resolves.toEqual(refreshed);
  });

  it("settles an in-flight request safely after the hook unmounts", async () => {
    const pending = deferred<NavigationDatasetSummary>();
    getSummaryMock.mockReturnValue(pending.promise);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    const { unmount } = renderHook(() => useNavigationDatasetSummary());
    unmount();

    await act(async () => {
      pending.resolve(summary(5_000));
      await pending.promise;
    });

    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
