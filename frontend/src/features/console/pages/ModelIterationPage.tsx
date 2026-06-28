import { Cpu, GitCompareArrows, Plus, Rocket, Server } from "lucide-react";
import { useState } from "react";

import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { ProgressBar } from "../../../components/console/ProgressBar";
import { SegmentedTabs } from "../../../components/console/SegmentedTabs";
import { StatusTag } from "../../../components/console/StatusTag";
import { cn } from "../../../lib/utils";
import type { StatusTone, TabItem } from "../consoleTypes";
import { modelCurveLoss, modelCurveSuccess, modelVersions, type ModelVersionStatus } from "../consoleFixtures";
import { MiniChart } from "../visuals/MiniChart";

type ModelTab = "versions" | "training" | "compare";

type ModelIterationPageProps = {
  onPlaceholderAction?: (message?: string) => void;
};

const modelTabs = [
  { id: "versions", label: "版本时间线" },
  { id: "training", label: "训练监控" },
  { id: "compare", label: "版本对比" },
] satisfies Array<TabItem<ModelTab>>;

const modelTabIdPrefix = "model-iteration";

const learningRateChart = {
  labels: ["E01", "E04", "E08", "E12", "E16", "E20", "E24"],
  data: [0.0012, 0.001, 0.00082, 0.00054, 0.00031, 0.00018, 0.00008],
  label: "Cosine decay",
  color: "#fbbf24",
};

const gpuChart = {
  labels: ["00", "05", "10", "15", "20", "25"],
  data: [72, 78, 83, 86, 81, 88],
  label: "GPU Utilization (%)",
  color: "#a78bfa",
};

const radarChart = {
  labels: ["成功率", "稳定性", "泛化", "延迟", "数据效率", "部署风险"],
  data: [94, 90, 87, 82, 88, 76],
  label: "v47 候选 / v46 基线",
  color: "#15d1d8",
};

const statusMeta: Record<ModelVersionStatus, { label: string; tone: StatusTone }> = {
  training: { label: "训练中", tone: "purple" },
  deployed: { label: "已部署", tone: "success" },
  archived: { label: "归档", tone: "neutral" },
  failed: { label: "失败", tone: "danger" },
};

function modelTabId(tab: ModelTab) {
  return `${modelTabIdPrefix}-tab-${tab}`;
}

function modelPanelId(tab: ModelTab) {
  return `${modelTabIdPrefix}-panel-${tab}`;
}

function VersionsPanel({ onPlaceholderAction }: ModelIterationPageProps) {
  const deployedVersion = modelVersions.find((version) => version.status === "deployed");

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_20rem]">
      <ConsoleCard>
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-console-text">版本时间线</h2>
            <p className="mt-1 text-sm text-console-muted">训练、部署与归档版本的迭代记录</p>
          </div>
          <ConsoleButton variant="primary" onClick={() => onPlaceholderAction?.("新建训练入口暂未接入后端")}>
            <Plus aria-hidden="true" className="h-4 w-4" />
            新建训练
          </ConsoleButton>
        </div>

        <div className="space-y-3">
          {modelVersions.map((version) => {
            const status = statusMeta[version.status];

            return (
              <article key={version.ver} className="grid gap-3 rounded border border-console-line bg-console-panel2/70 p-3 lg:grid-cols-[4.5rem_6rem_7rem_7rem_7rem_1fr] lg:items-center">
                <div>
                  <p className="text-sm font-semibold text-console-text">{version.ver}</p>
                  <p className="mt-1 text-xs text-console-muted">{version.date}</p>
                </div>
                <StatusTag tone={status.tone}>{status.label}</StatusTag>
                <div>
                  <p className="text-xs text-console-muted">数据</p>
                  <p className="mt-1 text-sm text-console-text">{version.data}</p>
                </div>
                <div>
                  <p className="text-xs text-console-muted">Epoch</p>
                  <p className="mt-1 text-sm text-console-text">{version.epochs}</p>
                </div>
                <div>
                  <p className="text-xs text-console-muted">成功率</p>
                  <p className="mt-1 text-sm text-console-text">{version.success}</p>
                </div>
                <p className="text-sm leading-6 text-console-muted">{version.note}</p>
              </article>
            );
          })}
        </div>
      </ConsoleCard>

      <aside className="space-y-4">
        <ConsoleCard>
          <div className="mb-4 flex items-center gap-2">
            <Rocket aria-hidden="true" className="h-5 w-5 text-console-cyan" />
            <h2 className="text-base font-semibold text-console-text">当前部署</h2>
          </div>
          <p className="text-3xl font-semibold text-console-text">{deployedVersion?.ver ?? "v46"}</p>
          <p className="mt-2 text-sm leading-6 text-console-muted">{deployedVersion?.note ?? "当前生产版本"}</p>
          <div className="mt-4 space-y-3">
            <ProgressBar value={94.2} label="线上成功率 94.2%" />
            <ProgressBar value={72} tone="info" label="灰度覆盖 72%" />
          </div>
        </ConsoleCard>

        <ConsoleCard>
          <div className="mb-4 flex items-center gap-2">
            <Server aria-hidden="true" className="h-5 w-5 text-console-cyan" />
            <h2 className="text-base font-semibold text-console-text">训练资源</h2>
          </div>
          <div className="space-y-3">
            <ProgressBar value={88} tone="purple" label="A100 集群占用 88%" />
            <ProgressBar value={64} tone="warning" label="数据加载队列 64%" />
            <ProgressBar value={41} tone="info" label="评估任务排队 41%" />
          </div>
        </ConsoleCard>
      </aside>
    </div>
  );
}

function TrainingPanel() {
  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <ConsoleCard>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-console-text">训练损失曲线</h2>
          <StatusTag tone="purple">Epoch 18/24</StatusTag>
        </div>
        <MiniChart type="line" title="训练损失曲线" data={modelCurveLoss} />
      </ConsoleCard>

      <ConsoleCard>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-console-text">学习率调度</h2>
          <StatusTag tone="warning">cosine</StatusTag>
        </div>
        <MiniChart type="line" title="学习率调度" data={learningRateChart} />
      </ConsoleCard>

      <ConsoleCard>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-console-text">GPU 监控 (实时)</h2>
          <Cpu aria-hidden="true" className="h-5 w-5 text-violet-300" />
        </div>
        <MiniChart type="bar" title="GPU 监控 (实时)" data={gpuChart} />
        <div className="mt-3 space-y-2">
          <ProgressBar value={88} tone="purple" label="GPU 利用率 88%" />
          <ProgressBar value={76} tone="info" label="显存占用 76%" />
        </div>
      </ConsoleCard>
    </div>
  );
}

function ComparePanel() {
  return (
    <div className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
      <ConsoleCard>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-console-text">版本性能对比</h2>
          <GitCompareArrows aria-hidden="true" className="h-5 w-5 text-console-cyan" />
        </div>
        <MiniChart type="radar" title="版本性能对比" data={radarChart} />
      </ConsoleCard>

      <ConsoleCard>
        <h2 className="text-base font-semibold text-console-text">关键指标</h2>
        <div className="mt-4 space-y-3">
          {[
            { label: "v47 候选成功率", value: "94.8%", tone: "success" as const },
            { label: "v46 生产成功率", value: "94.2%", tone: "info" as const },
            { label: "平均延迟变化", value: "-8ms", tone: "success" as const },
            { label: "部署风险", value: "中低", tone: "warning" as const },
          ].map((item) => (
            <div key={item.label} className="flex items-center justify-between gap-3 rounded border border-console-line bg-console-panel2/70 p-3">
              <span className="text-sm text-console-muted">{item.label}</span>
              <StatusTag tone={item.tone}>{item.value}</StatusTag>
            </div>
          ))}
        </div>
        <MiniChart type="line" title="Success Rate (%)" data={modelCurveSuccess} className="mt-5" />
      </ConsoleCard>
    </div>
  );
}

export function ModelIterationPage({ onPlaceholderAction }: ModelIterationPageProps) {
  const [activeTab, setActiveTab] = useState<ModelTab>("versions");
  const panelClass = (tab: ModelTab) => cn(activeTab !== tab && "hidden");

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <SegmentedTabs idPrefix={modelTabIdPrefix} value={activeTab} tabs={modelTabs} onChange={setActiveTab} aria-label="模型迭代视图" />
        <div className="flex flex-wrap gap-2">
          <StatusTag tone="purple">v47 训练中</StatusTag>
          <StatusTag tone="success">v46 生产</StatusTag>
        </div>
      </div>

      <div role="tabpanel" id={modelPanelId("versions")} aria-labelledby={modelTabId("versions")} className={panelClass("versions")} hidden={activeTab !== "versions"}>
        <VersionsPanel onPlaceholderAction={onPlaceholderAction} />
      </div>
      <div role="tabpanel" id={modelPanelId("training")} aria-labelledby={modelTabId("training")} className={panelClass("training")} hidden={activeTab !== "training"}>
        <TrainingPanel />
      </div>
      <div role="tabpanel" id={modelPanelId("compare")} aria-labelledby={modelTabId("compare")} className={panelClass("compare")} hidden={activeTab !== "compare"}>
        <ComparePanel />
      </div>
    </section>
  );
}
