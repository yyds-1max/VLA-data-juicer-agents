import { describe, expect, test } from "vitest";

import type { NavigationDatasetSummary } from "../../api/types";
import {
  animateInteger,
  formatAnimatedCompactNumber,
  formatAnimatedDuration,
  formatAnimatedTotalDataDetail,
  formatAnimatedVersion,
} from "./dashboardAnimation";

const summary = {
  totals: {
    date_count: 42,
    clip_count: 536,
    total_duration_ns: 32_023_000_000_000,
    raw_message_count: 12_739_018,
    extracted_clip_count: 420,
    synced_clip_count: 397,
  },
  sync_distribution: {
    image: 40_604,
    pointcloud: 36_998,
    odom: 28_062,
    grid_map: 36_484,
  },
  dates: [],
} satisfies NavigationDatasetSummary;

describe("dashboard animation formatting", () => {
  test("starts positive integer metrics from one and lands on the target", () => {
    expect(animateInteger(23, 0)).toBe(1);
    expect(animateInteger(23, 0.5)).toBe(12);
    expect(animateInteger(23, 1)).toBe(23);
    expect(animateInteger(0, 0)).toBe(0);
  });

  test("animates total data duration and detail while preserving final formatting", () => {
    expect(formatAnimatedDuration(summary.totals.total_duration_ns, 0)).toBe("1.0 秒");
    expect(formatAnimatedDuration(summary.totals.total_duration_ns, 1)).toBe("8.9 小时");

    expect(formatAnimatedTotalDataDetail(summary, 0)).toBe("1 个日期 / 1 个 clip / 1 条 ROS 消息");
    expect(formatAnimatedTotalDataDetail(summary, 1)).toBe("42 个日期 / 536 个 clip / 12739018 条 ROS 消息");
  });

  test("animates compact counts and model version strings", () => {
    expect(formatAnimatedCompactNumber(186, "K", 0)).toBe("1K");
    expect(formatAnimatedCompactNumber(186, "K", 1)).toBe("186K");
    expect(formatAnimatedVersion(47, 0)).toBe("v1");
    expect(formatAnimatedVersion(47, 1)).toBe("v47");
  });
});
