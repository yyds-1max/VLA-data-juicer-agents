import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "../../../components/ui/chart";
import { Skeleton } from "../../../components/ui/skeleton";
import { cn } from "../../../lib/utils";
import { animateInteger } from "../dashboardAnimation";

export type ModelEpochDatum = {
  epoch: number;
  successRate: number;
  loss: number;
};

export type DistributionDatum = {
  label: string;
  value: number;
  color: string;
};

const distributionLayoutClass =
  "grid min-h-40 min-w-0 grid-cols-[8rem_minmax(0,1fr)] items-center gap-2";

const modelChartConfig = {
  successRate: { label: "成功率", color: "#3156C8" },
  loss: { label: "损失值", color: "#E8798E" },
} satisfies ChartConfig;

const percentFormatter = (value: number) => `${value}%`;
const lossFormatter = (value: number) => value.toFixed(2);

export function ModelMetricsChart({ data, className }: { data: ModelEpochDatum[]; className?: string }) {
  if (data.length === 0) {
    return (
      <div className={cn("flex min-h-64 items-center justify-center text-sm text-[#626B7D]", className)} role="status">
        暂无训练指标数据
      </div>
    );
  }

  return (
    <ChartContainer
      config={modelChartConfig}
      className={cn("h-40 w-full aspect-auto", className)}
      role="img"
      aria-label="VLA v47 按 Epoch 展示的成功率和损失值折线图"
    >
      <AreaChart accessibilityLayer data={data} margin={{ left: 8, right: 8, top: 10, bottom: 4 }}>
        <defs>
          <linearGradient id="dashboard-success-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#3156C8" stopOpacity={0.18} />
            <stop offset="100%" stopColor="#3156C8" stopOpacity={0.015} />
          </linearGradient>
          <linearGradient id="dashboard-loss-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#E8798E" stopOpacity={0.12} />
            <stop offset="100%" stopColor="#E8798E" stopOpacity={0.01} />
          </linearGradient>
        </defs>
        <CartesianGrid vertical={false} stroke="#E8EBF2" strokeDasharray="3 5" />
        <XAxis
          dataKey="epoch"
          axisLine={false}
          tickLine={false}
          tickMargin={10}
          minTickGap={20}
          label={{ value: "Epoch", position: "insideBottom", offset: -3, fill: "#626B7D", fontSize: 11 }}
        />
        <YAxis
          yAxisId="success"
          axisLine={false}
          tickLine={false}
          tickMargin={8}
          width={46}
          domain={[0, 100]}
          tickFormatter={percentFormatter}
        />
        <YAxis
          yAxisId="loss"
          orientation="right"
          axisLine={false}
          tickLine={false}
          tickMargin={8}
          width={40}
          domain={[0, 1]}
          tickFormatter={lossFormatter}
        />
        <ChartTooltip
          cursor={{ stroke: "#AAB5D9", strokeDasharray: "3 4" }}
          content={
            <ChartTooltipContent
              className="min-w-40 border-[#E3E6EF] bg-white text-[#202431] shadow-[0_12px_30px_rgba(36,48,82,0.14)]"
              labelFormatter={(_, payload) => `Epoch ${payload[0]?.payload?.epoch ?? ""}`}
              formatter={(value, name) => (
                <div className="flex w-full items-center justify-between gap-5">
                  <span className="text-[#626B7D]">{name === "successRate" ? "成功率" : "损失值"}</span>
                  <span className="font-semibold tabular-nums text-[#202431]">
                    {name === "successRate" ? `${Number(value).toFixed(1)}%` : Number(value).toFixed(2)}
                  </span>
                </div>
              )}
            />
          }
        />
        <ReferenceLine x={18} stroke="#B9C4E9" strokeDasharray="3 4" />
        <Area
          yAxisId="success"
          dataKey="successRate"
          name="successRate"
          type="monotone"
          stroke="var(--color-successRate)"
          strokeWidth={2.5}
          fill="url(#dashboard-success-area)"
          fillOpacity={1}
          dot={false}
          strokeLinecap="round"
          strokeLinejoin="round"
          activeDot={{ r: 4.5, strokeWidth: 3, stroke: "#FFFFFF", fill: "#3156C8" }}
          isAnimationActive={false}
        />
        <Area
          yAxisId="loss"
          dataKey="loss"
          name="loss"
          type="monotone"
          stroke="var(--color-loss)"
          strokeWidth={2.25}
          fill="url(#dashboard-loss-area)"
          fillOpacity={1}
          dot={false}
          strokeLinecap="round"
          strokeLinejoin="round"
          activeDot={{ r: 4, strokeWidth: 3, stroke: "#FFFFFF", fill: "#E8798E" }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartContainer>
  );
}

export function DataDistributionChart({
  data,
  animationProgress = 1,
  className,
}: {
  data: DistributionDatum[];
  animationProgress?: number;
  className?: string;
}) {
  const visibleData = data.filter((item) => Number.isFinite(item.value) && item.value > 0);
  const total = visibleData.reduce((sum, item) => sum + item.value, 0);
  const normalizedProgress = Number.isFinite(animationProgress)
    ? Math.min(1, Math.max(0, animationProgress))
    : 1;
  const animatedTotal = animateInteger(total, normalizedProgress);
  const totalLabel = animatedTotal.toLocaleString("zh-CN");
  const totalTextSize = totalLabel.length >= 11
    ? "text-xs"
    : totalLabel.length >= 8
      ? "text-sm"
      : totalLabel.length >= 6
        ? "text-base"
        : "text-lg";
  const config = Object.fromEntries(
    data.map((item, index) => [`segment${index + 1}`, { label: item.label, color: item.color }]),
  ) satisfies ChartConfig;

  return (
    <div
      data-slot="distribution-panel-body"
      className={cn(distributionLayoutClass, className)}
    >
      <div
        data-slot="distribution-donut-shell"
        data-animation-progress={normalizedProgress.toFixed(3)}
        className="relative mx-auto size-32"
      >
        {total > 0 ? (
          <ChartContainer
            config={config}
            className="size-32 aspect-square"
            role="img"
            aria-label={`数据类型分布，总计 ${total.toLocaleString("zh-CN")}`}
          >
            <PieChart accessibilityLayer>
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    hideLabel
                    nameKey="label"
                    className="border-[#E3E6EF] bg-white text-[#202431] shadow-[0_12px_30px_rgba(36,48,82,0.14)]"
                  />
                }
              />
              <Pie
                data={visibleData}
                dataKey="value"
                nameKey="label"
                startAngle={90}
                endAngle={90 - 360 * normalizedProgress}
                innerRadius={40}
                outerRadius={56}
                paddingAngle={2}
                cornerRadius={3}
                stroke="#FFFFFF"
                strokeWidth={2}
                isAnimationActive={false}
              >
                {visibleData.map((item) => (
                  <Cell key={item.label} fill={item.color} />
                ))}
              </Pie>
            </PieChart>
          </ChartContainer>
        ) : (
          <div
            className="absolute inset-2 rounded-full border-16 border-[#EEF1F8]"
            role="img"
            aria-label="数据类型分布，总计 0，暂无同步帧数据"
          />
        )}
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center" aria-hidden="true">
          <span
            data-testid="distribution-total"
            className={cn(
              "max-w-[5.25rem] whitespace-nowrap font-semibold leading-none tabular-nums text-[#202431]",
              totalTextSize,
            )}
          >
            {totalLabel}
          </span>
          <span className="mt-0.5 text-[11px] text-[#626B7D]">总数</span>
        </div>
      </div>

      <div className="min-w-0" aria-label="数据类型图例">
        {data.length > 0 ? (
          <dl
            data-slot="distribution-legend"
            className="grid min-w-0 grid-cols-[minmax(0,max-content)_auto] justify-start gap-x-4 gap-y-2 text-xs"
          >
            {data.map((item) => (
              <div key={item.label} className="contents">
                <dt className="flex min-w-0 items-center gap-1.5 text-[#697186]">
                <span
                  className="size-2.5 shrink-0 rounded-full"
                  data-testid="distribution-color"
                  style={{ backgroundColor: item.color }}
                  aria-hidden="true"
                />
                <span className="truncate" title={item.label}>{item.label}</span>
                </dt>
                <dd className="shrink-0 font-semibold tabular-nums text-[#202431]">
                  {animateInteger(Math.max(0, item.value), normalizedProgress).toLocaleString("zh-CN")}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-sm text-[#626B7D]" role="status">暂无同步帧数据</p>
        )}
      </div>
    </div>
  );
}

export function DataDistributionSkeleton({ className }: { className?: string }) {
  return (
    <div
      data-slot="distribution-panel-body"
      className={cn(distributionLayoutClass, className)}
      role="status"
      aria-label="数据类型分布加载中"
    >
      <div
        data-slot="distribution-donut-skeleton"
        className="relative mx-auto size-32"
        aria-hidden="true"
      >
        <Skeleton className="absolute inset-2 rounded-full bg-[#EEF0F5] motion-reduce:animate-none" />
        <span className="absolute inset-6 rounded-full bg-white" />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5">
          <Skeleton className="h-4 w-14 rounded bg-[#EEF0F5] motion-reduce:animate-none" />
          <Skeleton className="h-2.5 w-8 rounded bg-[#F2F3F7] motion-reduce:animate-none" />
        </div>
      </div>

      <div
        data-slot="distribution-legend-skeleton"
        className="grid min-w-0 grid-cols-[minmax(0,1fr)_2rem] items-center gap-x-4 gap-y-2"
        aria-hidden="true"
      >
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} data-testid="distribution-skeleton-row" className="contents">
            <div className="flex min-w-0 items-center gap-1.5">
              <Skeleton className="size-2.5 shrink-0 rounded-full bg-[#EEF0F5] motion-reduce:animate-none" />
              <Skeleton className="h-3 w-full max-w-18 rounded bg-[#F2F3F7] motion-reduce:animate-none" />
            </div>
            <Skeleton className="h-3 w-8 rounded bg-[#EEF0F5] motion-reduce:animate-none" />
          </div>
        ))}
      </div>
    </div>
  );
}
