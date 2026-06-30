import { Activity, CircleDot, Database, GitBranch, Layers3 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { getNavigationDatasetSummary } from "../../../api/client";
import type { NavigationDatasetSummary } from "../../../api/types";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { MetricCard } from "../../../components/console/MetricCard";
import { SegmentedTabs } from "../../../components/console/SegmentedTabs";
import { StatusTag } from "../../../components/console/StatusTag";
import {
  activityFeed,
  dashboardMetrics,
  dataDistribution,
  modelCurveLoss,
  modelCurveSuccess,
} from "../consoleFixtures";
import { LoopFlowCanvas } from "../visuals/LoopFlowCanvas";
import { MiniChart } from "../visuals/MiniChart";

type MetricCurveTab = "success" | "loss";

const metricIcons = [Database, Layers3, GitBranch, CircleDot];

const chartTabs = [
  { id: "success", label: "成功率" },
  { id: "loss", label: "损失值" },
] satisfies Array<{ id: MetricCurveTab; label: string }>;

const syncDistributionColors = ["#2d6cdf", "#16845b", "#b7791f", "#6d5bd0"];

function formatDuration(totalDurationNs: number) {
  const seconds = totalDurationNs / 1_000_000_000;

  if (seconds < 60) {
    return `${seconds.toFixed(1)} 秒`;
  }

  const minutes = seconds / 60;
  if (minutes < 60) {
    return `${minutes.toFixed(1)} 分钟`;
  }

  return `${(minutes / 60).toFixed(1)} 小时`;
}

function totalDataDetail(summary: NavigationDatasetSummary) {
  const { date_count: dateCount, clip_count: clipCount, raw_message_count: rawMessageCount } = summary.totals;

  return `${dateCount} 个日期 / ${clipCount} 个 clip / ${rawMessageCount} 条 ROS 消息`;
}

function syncDistributionData(summary: NavigationDatasetSummary | null) {
  if (!summary) {
    return dataDistribution;
  }

  return [
    { label: "同步图像帧", value: summary.sync_distribution.image, color: syncDistributionColors[0] },
    { label: "同步点云帧", value: summary.sync_distribution.pointcloud, color: syncDistributionColors[1] },
    { label: "同步里程计帧", value: summary.sync_distribution.odom, color: syncDistributionColors[2] },
    { label: "同步栅格图", value: summary.sync_distribution.grid_map, color: syncDistributionColors[3] },
  ];
}

export function DashboardPage() {
  const [metricTab, setMetricTab] = useState<MetricCurveTab>("success");
  const [datasetSummary, setDatasetSummary] = useState<NavigationDatasetSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const activeCurve = metricTab === "success" ? modelCurveSuccess : modelCurveLoss;

  useEffect(() => {
    let ignore = false;

    async function loadDatasetSummary() {
      setSummaryLoading(true);
      setSummaryError(null);

      try {
        const summary = await getNavigationDatasetSummary();
        if (!ignore) {
          setDatasetSummary(summary);
        }
      } catch (error) {
        if (!ignore) {
          setDatasetSummary(null);
          setSummaryError(error instanceof Error ? error.message : "导航数据汇总加载失败");
        }
      } finally {
        if (!ignore) {
          setSummaryLoading(false);
        }
      }
    }

    void loadDatasetSummary();

    return () => {
      ignore = true;
    };
  }, []);

  const displayedMetrics = useMemo(
    () =>
      dashboardMetrics.map((metric) => ({
        ...metric,
        value:
          metric.id === "total-data"
            ? summaryLoading
              ? "加载中"
              : datasetSummary
                ? formatDuration(datasetSummary.totals.total_duration_ns)
                : "不可用"
            : metric.value,
        detail:
          metric.id === "total-data"
            ? summaryLoading
              ? "正在扫描导航数据"
              : datasetSummary
                ? totalDataDetail(datasetSummary)
                : summaryError
            : metric.detail,
        delta: metric.id === "total-data" ? undefined : metric.delta,
      })),
    [datasetSummary, summaryError, summaryLoading],
  );
  const displayedDistribution = useMemo(() => syncDistributionData(datasetSummary), [datasetSummary]);

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {displayedMetrics.map((metric, index) => {
          const Icon = metricIcons[index] ?? Activity;

          return (
            <MetricCard
              key={metric.id}
              title={metric.label}
              value={metric.value}
              detail={metric.detail}
              meta={metric.delta}
              tone={metric.id === "pending-batches" ? "warning" : "success"}
              tag={
                metric.delta ? (
                  <span className="inline-flex items-center gap-1">
                    <Icon aria-hidden="true" className="h-3 w-3" />
                    {metric.delta}
                  </span>
                ) : null
              }
            />
          );
        })}
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.35fr]">
        <ConsoleCard>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-console-text">数据类型分布</h2>
              <p className="mt-1 text-sm text-console-muted">多模态训练样本构成</p>
            </div>
            <StatusTag tone="info">实时</StatusTag>
          </div>
          <MiniChart type="donut" title="数据类型分布" data={displayedDistribution} />
        </ConsoleCard>

        <ConsoleCard>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-console-text">数据闭环流程</h2>
              <p className="mt-1 text-sm text-console-muted">采集、标注、过滤、训练、验证持续回流</p>
            </div>
            <StatusTag tone="success">运行中</StatusTag>
          </div>
          <LoopFlowCanvas />
        </ConsoleCard>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.35fr_0.9fr]">
        <ConsoleCard>
          <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h2 className="text-base font-semibold text-console-text">模型指标曲线</h2>
              <p className="mt-1 text-sm text-console-muted">v40 到 v47 的训练表现追踪</p>
            </div>
            <SegmentedTabs value={metricTab} tabs={chartTabs} onChange={setMetricTab} aria-label="模型指标切换" />
          </div>
          <MiniChart type="line" title={activeCurve.label} data={activeCurve} />
        </ConsoleCard>

        <ConsoleCard>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-console-text">最近活动</h2>
              <p className="mt-1 text-sm text-console-muted">闭环系统事件流</p>
            </div>
            <StatusTag tone="neutral">{activityFeed.length} 条</StatusTag>
          </div>
          <ol className="space-y-3">
            {activityFeed.map((item) => (
              <li key={item.id} className="flex gap-3 rounded border border-console-line bg-console-panel2/70 p-3">
                <StatusTag tone={item.tone} className="mt-0.5 shrink-0">
                  {item.time}
                </StatusTag>
                <p className="min-w-0 text-sm leading-5 text-console-text">{item.text}</p>
              </li>
            ))}
          </ol>
        </ConsoleCard>
      </div>
    </section>
  );
}
