import { Bot, CheckCircle2, CircleDot, ScanSearch, Split, Tags, XCircle } from "lucide-react";
import { useState } from "react";

import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { ProgressBar } from "../../../components/console/ProgressBar";
import { SegmentedTabs } from "../../../components/console/SegmentedTabs";
import { StatusTag } from "../../../components/console/StatusTag";
import { cn } from "../../../lib/utils";
import type { TabItem } from "../consoleTypes";
import { annotationResults } from "../consoleFixtures";
import { MiniChart } from "../visuals/MiniChart";

type AnnotationTab = "pipeline" | "results" | "review";

type AnnotationPageProps = {
  onPlaceholderAction?: (message?: string) => void;
};

const annotationTabs = [
  { id: "pipeline", label: "标注流水线" },
  { id: "results", label: "标注结果" },
  { id: "review", label: "人工复核" },
] satisfies Array<TabItem<AnnotationTab>>;

const annotationTabIdPrefix = "annotation";

function annotationTabId(tab: AnnotationTab) {
  return `${annotationTabIdPrefix}-tab-${tab}`;
}

function annotationPanelId(tab: AnnotationTab) {
  return `${annotationTabIdPrefix}-panel-${tab}`;
}

const pipelineNodes = [
  { name: "数据接入", icon: CircleDot, tone: "info", detail: "图像/点云/文本批次" },
  { name: "视觉检测", icon: ScanSearch, tone: "success", detail: "目标框与可交互区域" },
  { name: "点云分割", icon: Split, tone: "purple", detail: "实例与空间关系" },
  { name: "指令生成", icon: Bot, tone: "warning", detail: "动作语义拆解" },
  { name: "质量合并", icon: Tags, tone: "success", detail: "置信度与一致性检查" },
] as const;

const throughputChart = {
  labels: ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00"],
  data: [420, 510, 680, 620, 750, 810],
  label: "自动标注吞吐",
  color: "#15d1d8",
};

const confidenceChart = {
  labels: ["检测", "分割", "生成", "对齐", "质检"],
  data: [92, 87, 79, 91, 84],
  label: "模块置信度",
  color: "#34d399",
};

function PipelinePanel({ onPlaceholderAction }: AnnotationPageProps) {
  return (
    <div className="space-y-4">
      <ConsoleCard>
        <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-console-text">标注流水线</h2>
            <p className="mt-1 text-sm text-console-muted">多模型协同处理当前解锁候选批次</p>
          </div>
          <ConsoleButton variant="primary" onClick={() => onPlaceholderAction?.("启动标注尚未接入后端")}>
            启动标注
          </ConsoleButton>
        </div>
        <div className="grid gap-3 md:grid-cols-5">
          {pipelineNodes.map((node, index) => {
            const Icon = node.icon;

            return (
              <div key={node.name} className="relative rounded border border-console-line bg-console-panel2/70 p-3">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <Icon aria-hidden="true" className="h-5 w-5 text-console-cyan" />
                  <StatusTag tone={node.tone}>{index + 1}</StatusTag>
                </div>
                <h3 className="text-sm font-semibold text-console-text">{node.name}</h3>
                <p className="mt-2 text-xs leading-5 text-console-muted">{node.detail}</p>
              </div>
            );
          })}
        </div>
      </ConsoleCard>

      <div className="grid gap-4 lg:grid-cols-2">
        <ConsoleCard>
          <MiniChart type="bar" title="自动标注吞吐" data={throughputChart} />
        </ConsoleCard>
        <ConsoleCard>
          <MiniChart type="line" title="模块置信度" data={confidenceChart} />
        </ConsoleCard>
      </div>
    </div>
  );
}

function ResultsPanel() {
  return (
    <ConsoleCard>
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-console-text">标注结果</h2>
          <p className="mt-1 text-sm text-console-muted">自动标注输出与模型耗时</p>
        </div>
        <StatusTag tone="info">{annotationResults.length} 条</StatusTag>
      </div>
      <div className="space-y-3">
        {annotationResults.map((item) => (
          <div key={item.id} className="grid gap-3 rounded border border-console-line bg-console-panel2/70 p-3 lg:grid-cols-[7rem_7rem_1fr_8rem_5rem] lg:items-center">
            <div>
              <p className="text-sm font-semibold text-console-text">{item.id}</p>
              <p className="mt-1 text-xs text-console-muted">{item.input}</p>
            </div>
            <StatusTag tone={item.conf >= 0.9 ? "success" : item.conf >= 0.82 ? "info" : "warning"}>{item.type}</StatusTag>
            <div className="min-w-0">
              <p className="truncate text-sm text-console-text">{item.output}</p>
              <p className="mt-1 truncate text-xs text-console-muted">{item.model}</p>
            </div>
            <ProgressBar value={item.conf * 100} tone={item.conf >= 0.9 ? "success" : "warning"} label={`置信度 ${item.conf.toFixed(2)}`} />
            <span className="text-xs text-console-muted">{item.time}</span>
          </div>
        ))}
      </div>
    </ConsoleCard>
  );
}

function ReviewPanel({ onPlaceholderAction }: AnnotationPageProps) {
  return (
    <div className="grid gap-4 lg:grid-cols-[0.9fr_1.3fr]">
      <ConsoleCard>
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-console-text">待复核样本</h2>
            <p className="mt-1 text-sm text-console-muted">低置信度与策略抽检进入人工队列</p>
          </div>
          <StatusTag tone="warning">24</StatusTag>
        </div>
        <div className="space-y-2">
          {annotationResults.slice(1, 5).map((item) => (
            <button
              key={item.id}
              type="button"
              className="w-full rounded border border-console-line bg-console-panel2/70 p-3 text-left transition hover:border-console-cyan/45 focus:outline-none focus:ring-2 focus:ring-console-cyan"
            >
              <span className="block text-sm font-semibold text-console-text">{item.id}</span>
              <span className="mt-1 block text-xs text-console-muted">
                {item.type} / {item.input}
              </span>
            </button>
          ))}
        </div>
      </ConsoleCard>

      <ConsoleCard>
        <div className="mb-4">
          <h2 className="text-base font-semibold text-console-text">复核详情</h2>
          <p className="mt-1 text-sm text-console-muted">ANN-82403 / 指令生成 / 置信度 0.79</p>
        </div>
        <div className="rounded border border-console-line bg-console-panel2/70 p-4">
          <p className="text-sm leading-6 text-console-text">模型输出：抓取桌面上的蓝色瓶子，并移动到靠近机械臂的托盘中央。</p>
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <div className="rounded border border-console-line bg-console-bg/55 p-3">
              <p className="text-xs text-console-muted">目标对象</p>
              <p className="mt-2 text-sm font-medium text-console-text">蓝色瓶子</p>
            </div>
            <div className="rounded border border-console-line bg-console-bg/55 p-3">
              <p className="text-xs text-console-muted">动作类型</p>
              <p className="mt-2 text-sm font-medium text-console-text">pick_and_place</p>
            </div>
            <div className="rounded border border-console-line bg-console-bg/55 p-3">
              <p className="text-xs text-console-muted">风险</p>
              <p className="mt-2 text-sm font-medium text-amber-300">需核对终点</p>
            </div>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <ConsoleButton variant="primary" onClick={() => onPlaceholderAction?.("通过操作尚未接入后端")}>
            <CheckCircle2 aria-hidden="true" className="h-4 w-4" />
            通过
          </ConsoleButton>
          <ConsoleButton onClick={() => onPlaceholderAction?.("退回操作尚未接入后端")}>退回</ConsoleButton>
          <ConsoleButton onClick={() => onPlaceholderAction?.("废弃操作尚未接入后端")}>
            <XCircle aria-hidden="true" className="h-4 w-4" />
            废弃
          </ConsoleButton>
        </div>
      </ConsoleCard>
    </div>
  );
}

export function AnnotationPage({ onPlaceholderAction }: AnnotationPageProps) {
  const [activeTab, setActiveTab] = useState<AnnotationTab>("pipeline");
  const panelClass = (tab: AnnotationTab) => cn(activeTab !== tab && "hidden");

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <SegmentedTabs idPrefix={annotationTabIdPrefix} value={activeTab} tabs={annotationTabs} onChange={setActiveTab} aria-label="自动标注视图" />
        <div className="flex flex-wrap gap-2">
          <StatusTag tone="success">流水线在线</StatusTag>
          <StatusTag tone="warning">人工队列 24</StatusTag>
        </div>
      </div>

      <div
        role="tabpanel"
        id={annotationPanelId("pipeline")}
        aria-labelledby={annotationTabId("pipeline")}
        className={panelClass("pipeline")}
        hidden={activeTab !== "pipeline"}
      >
        <PipelinePanel onPlaceholderAction={onPlaceholderAction} />
      </div>
      <div
        role="tabpanel"
        id={annotationPanelId("results")}
        aria-labelledby={annotationTabId("results")}
        className={panelClass("results")}
        hidden={activeTab !== "results"}
      >
        <ResultsPanel />
      </div>
      <div
        role="tabpanel"
        id={annotationPanelId("review")}
        aria-labelledby={annotationTabId("review")}
        className={panelClass("review")}
        hidden={activeTab !== "review"}
      >
        <ReviewPanel onPlaceholderAction={onPlaceholderAction} />
      </div>
    </section>
  );
}
