import { Download, FileDown, Gauge, Play, Server, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { ProgressBar } from "../../../components/console/ProgressBar";
import { SegmentedTabs } from "../../../components/console/SegmentedTabs";
import { StatusTag } from "../../../components/console/StatusTag";
import { cn } from "../../../lib/utils";
import type { TabItem } from "../consoleTypes";
import { simulationReportRows } from "../consoleFixtures";
import { MiniChart } from "../visuals/MiniChart";

type SimulationTab = "config" | "running" | "results";

type SimulationPageProps = {
  onPlaceholderAction?: (message?: string) => void;
};

const simulationTabs = [
  { id: "config", label: "仿真配置" },
  { id: "running", label: "运行监控" },
  { id: "results", label: "测试结果" },
] satisfies Array<TabItem<SimulationTab>>;

const simulationTabIdPrefix = "simulation";

const successChart = {
  labels: simulationReportRows.map((row) => row.name.slice(0, 6)),
  data: simulationReportRows.map((row) => row.sr),
  label: "Success Rate (%)",
  color: "#15d1d8",
};

const latencyChart = {
  labels: simulationReportRows.map((row) => row.name.slice(0, 6)),
  data: simulationReportRows.map((row) => row.latency),
  label: "Latency (ms)",
  color: "#fbbf24",
};

const stabilityChart = {
  labels: ["导航", "避障", "抓取", "指令", "复杂", "制动"],
  data: [96, 88, 92, 90, 86, 98],
  label: "综合稳定性",
  color: "#a78bfa",
};

const runningLogs = [
  "[10:24:01] queue accepted: v47-candidate / indoor-navigation",
  "[10:24:18] scene bundle loaded: 7 suites, 1,147 cases",
  "[10:25:04] worker-03 finished TC-001 with 95.2% success",
  "[10:25:37] warning: dynamic obstacle retry threshold reached",
  "[10:26:12] evaluator streaming metrics to report buffer",
];

function simulationTabId(tab: SimulationTab) {
  return `${simulationTabIdPrefix}-tab-${tab}`;
}

function simulationPanelId(tab: SimulationTab) {
  return `${simulationTabIdPrefix}-panel-${tab}`;
}

function ConfigPanel({ onPlaceholderAction }: SimulationPageProps) {
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_20rem]">
      <div className="space-y-4">
        <ConsoleCard>
          <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-base font-semibold text-console-text">仿真场景配置</h2>
              <p className="mt-1 text-sm text-console-muted">选择模型、场景、资源与评估指标</p>
            </div>
            <ConsoleButton variant="primary" onClick={() => onPlaceholderAction?.("启动仿真暂未接入后端")}>
              <Play aria-hidden="true" className="h-4 w-4" />
              启动仿真
            </ConsoleButton>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">候选模型</span>
              <select className="h-10 w-full rounded border border-console-line bg-console-panel2 px-3 text-sm text-console-text" defaultValue="v47">
                <option value="v47">v47 candidate</option>
                <option value="v46">v46 production baseline</option>
              </select>
            </label>
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">场景包</span>
              <select className="h-10 w-full rounded border border-console-line bg-console-panel2 px-3 text-sm text-console-text" defaultValue="full">
                <option value="full">DataLoop regression full suite</option>
                <option value="navigation">Indoor navigation subset</option>
                <option value="manipulation">Desktop manipulation subset</option>
              </select>
            </label>
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">并发 worker 12</span>
              <input className="w-full accent-cyan-300" type="range" min="1" max="24" defaultValue="12" aria-label="并发 worker" />
            </label>
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">超时阈值 240ms</span>
              <input className="w-full accent-amber-300" type="range" min="80" max="500" defaultValue="240" aria-label="超时阈值" />
            </label>
          </div>
        </ConsoleCard>

        <ConsoleCard>
          <h2 className="text-base font-semibold text-console-text">指标选择</h2>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {["成功率", "平均延迟", "碰撞次数", "轨迹稳定性"].map((metric) => (
              <label key={metric} className="flex items-center gap-3 rounded border border-console-line bg-console-panel2/70 p-3 text-sm text-console-text">
                <input type="checkbox" defaultChecked className="h-4 w-4 accent-cyan-300" />
                {metric}
              </label>
            ))}
          </div>
        </ConsoleCard>

        <ConsoleCard>
          <h2 className="text-base font-semibold text-console-text">测试用例库</h2>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {simulationReportRows.slice(0, 4).map((row) => (
              <div key={row.name} className="rounded border border-console-line bg-console-panel2/70 p-3">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-console-text">{row.name}</h3>
                  <StatusTag tone="info">{row.scene}</StatusTag>
                </div>
                <p className="mt-2 text-sm text-console-muted">{row.count} cases / baseline {row.sr}%</p>
              </div>
            ))}
          </div>
        </ConsoleCard>
      </div>

      <aside className="space-y-4">
        <ConsoleCard>
          <div className="mb-4 flex items-center gap-2">
            <Server aria-hidden="true" className="h-5 w-5 text-console-cyan" />
            <h2 className="text-base font-semibold text-console-text">资源面板</h2>
          </div>
          <div className="space-y-3">
            <ProgressBar value={62} tone="info" label="仿真 worker 占用 62%" />
            <ProgressBar value={48} tone="purple" label="GPU evaluator 48%" />
            <ProgressBar value={35} tone="warning" label="日志缓冲区 35%" />
          </div>
        </ConsoleCard>

        <ConsoleCard>
          <div className="mb-3 flex items-center gap-2">
            <ShieldCheck aria-hidden="true" className="h-5 w-5 text-console-cyan" />
            <h2 className="text-base font-semibold text-console-text">发布门禁</h2>
          </div>
          <p className="text-sm leading-6 text-console-muted">候选版本需通过成功率、碰撞、延迟和稳定性四类检查后才允许进入灰度发布。</p>
        </ConsoleCard>
      </aside>
    </div>
  );
}

function RunningPanel() {
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_20rem]">
      <div className="space-y-4">
        <div className="grid gap-4 md:grid-cols-3">
          {[
            { label: "运行进度", value: "68%", tone: "info" as const },
            { label: "已完成用例", value: "781 / 1,147", tone: "success" as const },
            { label: "异常告警", value: "3", tone: "warning" as const },
          ].map((item) => (
            <ConsoleCard key={item.label}>
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm text-console-muted">{item.label}</p>
                <StatusTag tone={item.tone}>{item.value}</StatusTag>
              </div>
              <ProgressBar className="mt-4" value={item.label === "异常告警" ? 18 : 68} tone={item.tone} />
            </ConsoleCard>
          ))}
        </div>

        <ConsoleCard>
          <div className="mb-3 flex items-center gap-2">
            <Gauge aria-hidden="true" className="h-5 w-5 text-console-cyan" />
            <h2 className="text-base font-semibold text-console-text">实时任务日志</h2>
          </div>
          <div className="rounded border border-console-line bg-console-bg p-3 font-mono text-xs leading-6 text-console-muted">
            {runningLogs.map((log) => (
              <div key={log}>{log}</div>
            ))}
          </div>
        </ConsoleCard>

        <ConsoleCard>
          <h2 className="mb-3 text-base font-semibold text-console-text">运行指标趋势</h2>
          <MiniChart type="line" title="运行指标趋势" data={successChart} />
        </ConsoleCard>
      </div>

      <aside className="space-y-4">
        <ConsoleCard>
          <h2 className="text-base font-semibold text-console-text">队列状态</h2>
          <div className="mt-4 space-y-3">
            <StatusTag tone="info">running</StatusTag>
            <ProgressBar value={72} tone="info" label="scene queue 72%" />
            <ProgressBar value={54} tone="purple" label="report writer 54%" />
          </div>
        </ConsoleCard>
        <ConsoleCard>
          <MiniChart type="bar" title="Latency (ms)" data={latencyChart} />
        </ConsoleCard>
      </aside>
    </div>
  );
}

function ResultsPanel({ onPlaceholderAction }: SimulationPageProps) {
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-4">
        {[
          ["综合评分", "A-", "success"],
          ["平均成功率", "91.5%", "info"],
          ["平均延迟", "188ms", "warning"],
          ["碰撞总数", "7", "danger"],
        ].map(([label, value, tone]) => (
          <ConsoleCard key={label}>
            <p className="text-sm text-console-muted">{label}</p>
            <StatusTag className="mt-3" tone={tone as "success" | "info" | "warning" | "danger"}>
              {value}
            </StatusTag>
          </ConsoleCard>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <ConsoleCard>
          <MiniChart type="line" title="Success Rate (%)" data={successChart} />
        </ConsoleCard>
        <ConsoleCard>
          <MiniChart type="radar" title="综合稳定性" data={stabilityChart} />
        </ConsoleCard>
      </div>

      <ConsoleCard>
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-console-text">详细测试报告</h2>
            <p className="mt-1 text-sm text-console-muted">前端固定示例数据，不代表真实评估结论</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <ConsoleButton onClick={() => onPlaceholderAction?.("导出PDF暂未接入后端")}>
              <FileDown aria-hidden="true" className="h-4 w-4" />
              导出PDF
            </ConsoleButton>
            <ConsoleButton onClick={() => onPlaceholderAction?.("导出CSV暂未接入后端")}>
              <Download aria-hidden="true" className="h-4 w-4" />
              导出CSV
            </ConsoleButton>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="text-xs uppercase text-console-muted">
              <tr className="border-b border-console-line">
                <th className="py-2 pr-3 font-medium">用例</th>
                <th className="py-2 pr-3 font-medium">场景</th>
                <th className="py-2 pr-3 font-medium">数量</th>
                <th className="py-2 pr-3 font-medium">成功率</th>
                <th className="py-2 pr-3 font-medium">延迟</th>
                <th className="py-2 pr-3 font-medium">碰撞</th>
                <th className="py-2 pr-3 font-medium">评级</th>
              </tr>
            </thead>
            <tbody>
              {simulationReportRows.map((row) => (
                <tr key={row.name} className="border-b border-console-line/70">
                  <td className="py-3 pr-3 font-medium text-console-text">{row.name}</td>
                  <td className="py-3 pr-3 text-console-muted">{row.scene}</td>
                  <td className="py-3 pr-3 text-console-muted">{row.count}</td>
                  <td className="py-3 pr-3 text-console-muted">{row.sr}%</td>
                  <td className="py-3 pr-3 text-console-muted">{row.latency}ms</td>
                  <td className="py-3 pr-3 text-console-muted">{row.collisions}</td>
                  <td className="py-3 pr-3">
                    <StatusTag tone={row.rating.startsWith("A") ? "success" : "warning"}>{row.rating}</StatusTag>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ConsoleCard>
    </div>
  );
}

export function SimulationPage({ onPlaceholderAction }: SimulationPageProps) {
  const [activeTab, setActiveTab] = useState<SimulationTab>("config");
  const panelClass = (tab: SimulationTab) => cn(activeTab !== tab && "hidden");

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <SegmentedTabs idPrefix={simulationTabIdPrefix} value={activeTab} tabs={simulationTabs} onChange={setActiveTab} aria-label="测试仿真视图" />
        <div className="flex flex-wrap gap-2">
          <StatusTag tone="info">1,147 cases</StatusTag>
          <StatusTag tone="success">baseline ready</StatusTag>
        </div>
      </div>

      <div role="tabpanel" id={simulationPanelId("config")} aria-labelledby={simulationTabId("config")} className={panelClass("config")} hidden={activeTab !== "config"}>
        <ConfigPanel onPlaceholderAction={onPlaceholderAction} />
      </div>
      <div role="tabpanel" id={simulationPanelId("running")} aria-labelledby={simulationTabId("running")} className={panelClass("running")} hidden={activeTab !== "running"}>
        <RunningPanel />
      </div>
      <div role="tabpanel" id={simulationPanelId("results")} aria-labelledby={simulationTabId("results")} className={panelClass("results")} hidden={activeTab !== "results"}>
        <ResultsPanel onPlaceholderAction={onPlaceholderAction} />
      </div>
    </section>
  );
}
