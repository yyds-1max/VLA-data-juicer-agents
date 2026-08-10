import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleAlert,
  Database,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  Tags,
} from "lucide-react";
import type { ReactNode } from "react";
import { useMemo } from "react";
import { useNavigate } from "react-router-dom";

import type { NavigationDatasetSummary } from "../../../api/types";
import { MetricCard } from "../../../components/console/MetricCard";
import { StatusTag } from "../../../components/console/StatusTag";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import { Progress } from "../../../components/ui/progress";
import { Separator } from "../../../components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../../components/ui/table";
import {
  animateInteger,
  formatAnimatedDuration,
  useAnimatedProgress,
} from "../dashboardAnimation";
import { useNavigationDatasetSummary } from "../navigationDatasetSummaryCache";
import {
  dashboardAttentionItems,
  dashboardModelEpochs,
  dashboardRecentEvents,
  dataDistribution,
} from "../consoleFixtures";
import { DataFlowTimeline } from "../visuals/DataFlowTimeline";
import {
  DataDistributionChart,
  DataDistributionSkeleton,
  ModelMetricsChart,
  type DistributionDatum,
} from "../visuals/DashboardCharts";

const dashboardCardClass =
  "gap-0 rounded-2xl bg-white py-0 ring-1 ring-[#E7EAF1] shadow-[0_8px_28px_rgba(34,48,78,0.052)]";

const distributionColors = ["#274BC8", "#536FD7", "#7C8FE3", "#A7B3ED"];

function distributionFromSummary(summary: NavigationDatasetSummary | null): DistributionDatum[] {
  if (!summary) {
    return dataDistribution;
  }

  return [
    { label: "同步图像帧", value: summary.sync_distribution.image, color: distributionColors[0] },
    { label: "同步点云帧", value: summary.sync_distribution.pointcloud, color: distributionColors[1] },
    { label: "同步里程计帧", value: summary.sync_distribution.odom, color: distributionColors[2] },
    { label: "同步栅格图", value: summary.sync_distribution.grid_map, color: distributionColors[3] },
  ];
}

function DashboardPanelHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <CardHeader className="flex-row items-start justify-between gap-4 px-5 pb-0 pt-5">
      <div className="min-w-0">
        <CardTitle className="text-base font-semibold tracking-[-0.01em] text-[#202431]">{title}</CardTitle>
        {description ? <p className="mt-1 text-sm text-[#626B7D]">{description}</p> : null}
      </div>
      {action}
    </CardHeader>
  );
}

function DistributionPanel({
  summary,
  loading,
  error,
  animationProgress,
  onRetry,
}: {
  summary: NavigationDatasetSummary | null;
  loading: boolean;
  error: string | null;
  animationProgress: number;
  onRetry: () => void;
}) {
  const distribution = useMemo(() => distributionFromSummary(summary), [summary]);

  return (
    <Card className={dashboardCardClass} aria-busy={loading || undefined}>
      <DashboardPanelHeader title="数据类型分布" description="当前同步数据的模态构成" />
      <CardContent className="p-5 pt-4">
        {loading ? (
          <DataDistributionSkeleton />
        ) : error ? (
          <div
            data-slot="distribution-panel-body"
            className="flex min-h-40 flex-col items-center justify-center px-4 text-center"
            role="alert"
          >
            <CircleAlert className="size-8 text-[#B93755]" aria-hidden="true" />
            <p className="mt-3 text-sm font-medium text-[#30374A]">数据分布加载失败</p>
            <p className="mt-1 max-w-56 truncate text-xs text-[#626B7D]" title={error}>无法读取同步帧汇总</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-4 rounded-lg border-[#DDE2EE] text-[#3156C8] transition-[color,background-color,border-color,box-shadow] duration-150 hover:border-[#BFC8E4] hover:bg-[#F3F5FC] active:bg-[#E8ECF9] focus-visible:ring-2 focus-visible:ring-[#3156C8]/25 motion-reduce:transition-none"
              onClick={onRetry}
            >
              重新加载
            </Button>
          </div>
        ) : (
          <DataDistributionChart
            data={distribution}
            animationProgress={animationProgress}
          />
        )}
      </CardContent>
    </Card>
  );
}

export function DashboardPage() {
  const navigate = useNavigate();
  const { summary: datasetSummary, loading: summaryLoading, error: summaryError, reload } = useNavigationDatasetSummary();
  const animationKey = datasetSummary
    ? `${datasetSummary.totals.date_count}-${datasetSummary.totals.clip_count}-${datasetSummary.totals.total_duration_ns}`
    : "dashboard-fixture";
  const animationProgress = useAnimatedProgress(animationKey);
  const hasDataset = Boolean(datasetSummary && datasetSummary.totals.total_duration_ns > 0);
  const totalDataValue = datasetSummary
    ? formatAnimatedDuration(datasetSummary.totals.total_duration_ns, animationProgress)
    : "0 秒";
  const totalDataDetail = datasetSummary
    ? datasetSummary.totals.clip_count > 0
      ? `${animateInteger(datasetSummary.totals.date_count, animationProgress)} 个日期 · ${animateInteger(datasetSummary.totals.clip_count, animationProgress).toLocaleString("zh-CN")} clips`
      : "暂无导航数据"
    : "暂无导航数据";
  const annotationTotals = datasetSummary?.annotation_totals;
  const annotatedClipCount = annotationTotals?.annotated_clip_count ?? 0;
  const syncedClipCount = datasetSummary?.totals.synced_clip_count ?? 0;
  const annotationCoverage = syncedClipCount
    ? (annotatedClipCount / syncedClipCount) * 100
    : 0;

  return (
    <section className="mx-auto max-w-[1680px] space-y-4 px-4 py-5 md:px-7 md:py-6" aria-label="VLA 数据闭环仪表盘">
      <div className="flex justify-end">
        <Button
          type="button"
          variant="outline"
          disabled={summaryLoading}
          aria-label={summaryLoading ? "正在刷新仪表盘数据" : "刷新仪表盘数据"}
          onClick={() => { void reload(); }}
          className="bg-white text-slate-700 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700"
        >
          <RefreshCw
            aria-hidden="true"
            className={summaryLoading ? "animate-spin motion-reduce:animate-none" : ""}
          />
          {summaryLoading ? "刷新中" : "刷新"}
        </Button>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          title="总数据量"
          value={hasDataset ? totalDataValue : "0 秒"}
          detail={hasDataset ? totalDataDetail : "暂无导航数据"}
          icon={Database}
          loading={summaryLoading}
          error={summaryError}
          onRetry={() => { void reload(); }}
        />
        <MetricCard
          title="已标注数据"
          value={summaryError ? "--" : animateInteger(annotatedClipCount, animationProgress).toLocaleString("zh-CN")}
          detail={summaryError
            ? "标注统计暂不可用"
            : `标注覆盖率 ${annotationCoverage.toFixed(1)}% · ${annotatedClipCount.toLocaleString("zh-CN")}/${syncedClipCount.toLocaleString("zh-CN")} clips · ${animateInteger(annotationTotals?.annotated_unit_count ?? 0, animationProgress)} Segments`}
          icon={Tags}
          loading={summaryLoading}
        />
        <MetricCard
          title="待解锁批次"
          value={animateInteger(23, animationProgress).toLocaleString("zh-CN")}
          detail={`${animateInteger(7, animationProgress)} 批次待审核`}
          icon={LockKeyhole}
        />
        <MetricCard
          title="已验证模型"
          value={animateInteger(6, animationProgress).toLocaleString("zh-CN")}
          detail="最近验证 v46"
          icon={ShieldCheck}
        />
      </div>

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(16.5rem,1fr)]">
        <div className="min-w-0 space-y-4">
          <Card className={dashboardCardClass}>
            <CardContent className="p-4.5">
              <DataFlowTimeline />
            </CardContent>
          </Card>

          <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1.7fr)_minmax(17rem,0.9fr)]">
            <Card className={dashboardCardClass}>
              <DashboardPanelHeader
                title="当前模型指标"
                description="VLA v47 · Epoch 18/24"
                action={
                  <div className="hidden items-center gap-4 text-xs sm:flex" aria-label="图表图例">
                    <span className="flex items-center gap-1.5 text-[#697186]"><span className="size-2 rounded-full bg-[#3156C8]" aria-hidden="true" />成功率</span>
                    <span className="flex items-center gap-1.5 text-[#697186]"><span className="size-2 rounded-full bg-[#E8798E]" aria-hidden="true" />损失值</span>
                  </div>
                }
              />
              <CardContent className="p-5 pt-2">
                <ModelMetricsChart data={dashboardModelEpochs} />
              </CardContent>
            </Card>

            <DistributionPanel
              summary={datasetSummary}
              loading={summaryLoading}
              error={summaryError}
              animationProgress={animationProgress}
              onRetry={() => { void reload(); }}
            />
          </div>

          <Card className={dashboardCardClass}>
            <DashboardPanelHeader title="最近事件" description="数据闭环系统事件流" />
            <CardContent className="px-5 pb-4 pt-3">
              <Table
                aria-label="最近事件"
                containerAriaLabel="最近事件表，可横向滚动"
                containerTabIndex={0}
              >
                <TableHeader>
                  <TableRow className="border-[#E8EBF2] hover:bg-transparent">
                    <TableHead className="w-28 px-0 text-xs font-medium text-[#626B7D]">时间</TableHead>
                    <TableHead className="min-w-64 text-xs font-medium text-[#626B7D]">事件</TableHead>
                    <TableHead className="min-w-32 text-xs font-medium text-[#626B7D]">对象</TableHead>
                    <TableHead className="w-24 px-0 text-xs font-medium text-[#626B7D]">状态</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {dashboardRecentEvents.map((event) => (
                    <TableRow key={event.id} className="border-[#EDF0F5] hover:bg-[#FAFBFD]">
                      <TableCell className="px-0 py-3 text-[#687186]">{event.time}</TableCell>
                      <TableCell className="max-w-[28rem] truncate py-3 font-medium text-[#30374A]" title={event.event}>{event.event}</TableCell>
                      <TableCell className="py-3 text-[#687186]">{event.target}</TableCell>
                      <TableCell className="px-0 py-3"><StatusTag tone={event.tone}>{event.status}</StatusTag></TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>

        <aside className="min-w-0 space-y-4" aria-label="当前训练与待办">
          <Card className={dashboardCardClass}>
            <DashboardPanelHeader title="当前训练模型" />
            <CardContent className="p-4 pt-3">
              <div className="relative overflow-hidden rounded-xl bg-[#3156C8] p-5 text-white shadow-[0_12px_28px_rgba(49,86,200,0.22)]">
                <Bot className="absolute right-4 top-4 size-6 text-white/35" aria-hidden="true" />
                <p className="text-xl font-semibold tracking-[-0.02em]">VLA v47</p>
                <p className="mt-3 text-sm text-white/90">训练中 · Epoch 18/24</p>
                <p className="mt-2 text-sm text-white/80">数据集 925 clips</p>
              </div>

              <div className="mt-5 space-y-5">
                <div>
                  <div className="mb-2 flex items-end justify-between gap-3">
                    <span className="text-xs text-[#626B7D]">成功率</span>
                    <span className="font-semibold tabular-nums text-[#202431]">94.8%</span>
                  </div>
                  <Progress value={94.8} aria-label="成功率 94.8%" className="h-1.5 bg-[#ECEEF4]" indicatorClassName="bg-[#3156C8]" />
                </div>
                <div>
                  <div className="mb-2 flex items-end justify-between gap-3">
                    <span className="text-xs text-[#626B7D]">训练资源</span>
                    <span className="font-semibold tabular-nums text-[#202431]">68%</span>
                  </div>
                  <Progress value={68} aria-label="训练资源 68%" className="h-1.5 bg-[#ECEEF4]" indicatorClassName="bg-[#536FD7]" />
                </div>
              </div>

              <Button
                type="button"
                variant="outline"
                className="mt-5 w-full rounded-xl border-[#DFE3EE] bg-[#F8F9FD] text-[#3156C8] transition-[color,background-color,border-color,box-shadow,transform] duration-150 hover:border-[#C7D0EB] hover:bg-[#F1F3FB] active:translate-y-px active:bg-[#E8ECF9] focus-visible:ring-2 focus-visible:ring-[#3156C8]/25 motion-reduce:transform-none motion-reduce:transition-none"
                onClick={() => navigate("/model")}
              >
                查看训练详情
              </Button>
            </CardContent>
          </Card>

          <Card className={dashboardCardClass}>
            <DashboardPanelHeader title="需要关注" />
            <CardContent className="p-3 pt-2">
              <div className="space-y-1">
                {dashboardAttentionItems.map((item, index) => {
                  const Icon = item.tone === "danger" ? CircleAlert : item.tone === "warning" ? AlertTriangle : CheckCircle2;
                  const iconTone = item.tone === "danger" ? "bg-[#FEF0F3] text-[#B93755]" : item.tone === "warning" ? "bg-[#FFF5E9] text-[#A75600]" : "bg-[#EEF1FC] text-[#3156C8]";

                  return (
                    <div key={item.id}>
                      {index > 0 ? <Separator className="bg-[#EEF0F4]" /> : null}
                      <button
                        type="button"
                        className="group flex w-full items-center gap-3 rounded-xl px-2 py-3 text-left transition-[background-color,box-shadow,transform] duration-150 hover:bg-[#F8F9FC] active:translate-y-px active:bg-[#F0F2F7] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[#3156C8] motion-reduce:transform-none motion-reduce:transition-none"
                        onClick={() => navigate(item.path)}
                      >
                        <span className={`flex size-9 shrink-0 items-center justify-center rounded-full ${iconTone}`} aria-hidden="true">
                          <Icon className="size-4.5" />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium text-[#30374A]">{item.title}</span>
                          <span className="mt-0.5 block truncate text-xs text-[#626B7D]">{item.detail}</span>
                        </span>
                        <ArrowRight className="size-4 shrink-0 text-[#9AA2B3] transition-[color,transform] duration-150 group-hover:translate-x-0.5 group-hover:text-[#3156C8] motion-reduce:transform-none motion-reduce:transition-none" aria-hidden="true" />
                      </button>
                    </div>
                  );
                })}
              </div>
            </CardContent>
          </Card>
        </aside>
      </div>
    </section>
  );
}
