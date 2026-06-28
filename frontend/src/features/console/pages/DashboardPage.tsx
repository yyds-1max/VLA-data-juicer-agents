import { Activity, CircleDot, Database, GitBranch, Layers3 } from "lucide-react";
import { useMemo, useState } from "react";

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

const referenceMetricValues: Record<string, string> = {
  "total-data": "284,729",
};

export function DashboardPage() {
  const [metricTab, setMetricTab] = useState<MetricCurveTab>("success");
  const activeCurve = metricTab === "success" ? modelCurveSuccess : modelCurveLoss;
  const displayedMetrics = useMemo(
    () =>
      dashboardMetrics.map((metric) => ({
        ...metric,
        value: referenceMetricValues[metric.id] ?? metric.value,
      })),
    [],
  );

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
                <span className="inline-flex items-center gap-1">
                  <Icon aria-hidden="true" className="h-3 w-3" />
                  {metric.delta}
                </span>
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
          <MiniChart type="donut" title="数据类型分布" data={dataDistribution} />
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
