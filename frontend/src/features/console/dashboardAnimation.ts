import { useEffect, useState } from "react";

import type { NavigationDatasetSummary } from "../../api/types";

const ONE_SECOND_NS = 1_000_000_000;

function clampProgress(progress: number) {
  if (!Number.isFinite(progress)) {
    return 1;
  }

  return Math.min(1, Math.max(0, progress));
}

function easeOutCubic(progress: number) {
  const clamped = clampProgress(progress);

  return 1 - Math.pow(1 - clamped, 3);
}

function shouldReduceMotion() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function shouldSkipAnimation() {
  return import.meta.env.MODE === "test" || shouldReduceMotion();
}

export function animateInteger(target: number, progress: number) {
  const roundedTarget = Math.max(0, Math.round(target));

  if (roundedTarget === 0) {
    return 0;
  }

  if (clampProgress(progress) >= 1) {
    return roundedTarget;
  }

  return Math.min(roundedTarget, Math.max(1, Math.round(roundedTarget * clampProgress(progress))));
}

export function formatAnimatedDuration(totalDurationNs: number, progress: number) {
  const targetSeconds = Math.max(0, totalDurationNs / ONE_SECOND_NS);
  const animatedSeconds =
    targetSeconds > 0 ? Math.min(targetSeconds, Math.max(Math.min(1, targetSeconds), targetSeconds * clampProgress(progress))) : 0;

  if (animatedSeconds < 60) {
    return `${animatedSeconds.toFixed(1)} 秒`;
  }

  const minutes = animatedSeconds / 60;
  if (minutes < 60) {
    return `${minutes.toFixed(1)} 分钟`;
  }

  return `${(minutes / 60).toFixed(1)} 小时`;
}

export function formatAnimatedTotalDataDetail(summary: NavigationDatasetSummary, progress: number) {
  const { date_count: dateCount, clip_count: clipCount, raw_message_count: rawMessageCount } = summary.totals;

  return `${animateInteger(dateCount, progress)} 个日期 / ${animateInteger(clipCount, progress)} 个 clip / ${animateInteger(rawMessageCount, progress)} 条 ROS 消息`;
}

export function formatAnimatedCompactNumber(value: number, suffix: string, progress: number) {
  return `${animateInteger(value, progress)}${suffix}`;
}

export function formatAnimatedVersion(version: number, progress: number) {
  return `v${animateInteger(version, progress)}`;
}

export function useAnimatedProgress(animationKey: string, durationMs = 820) {
  const [progress, setProgress] = useState(() => (shouldSkipAnimation() ? 1 : 0));

  useEffect(() => {
    if (shouldSkipAnimation() || typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
      setProgress(1);
      return;
    }

    let frameId = 0;
    const start = window.performance.now();
    setProgress(0);

    const tick = (now: number) => {
      const rawProgress = (now - start) / durationMs;
      const nextProgress = easeOutCubic(rawProgress);
      setProgress(nextProgress);

      if (rawProgress < 1) {
        frameId = window.requestAnimationFrame(tick);
      }
    };

    frameId = window.requestAnimationFrame(tick);

    return () => {
      window.cancelAnimationFrame(frameId);
    };
  }, [animationKey, durationMs]);

  return progress;
}
