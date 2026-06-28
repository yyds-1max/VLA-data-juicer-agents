import { Cloud, FileText, Image, LockKeyhole, SlidersHorizontal, Upload } from "lucide-react";
import { useState } from "react";

import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { ProgressBar } from "../../../components/console/ProgressBar";
import { QualityRing } from "../../../components/console/QualityRing";
import { SegmentedTabs } from "../../../components/console/SegmentedTabs";
import { StatusTag } from "../../../components/console/StatusTag";
import { cn } from "../../../lib/utils";
import type { StatusTone, TabItem } from "../consoleTypes";
import { batchData, imageData, pointCloudData, textInstructionData } from "../consoleFixtures";
import { PointCloudPreview } from "../visuals/PointCloudPreview";

type DataTab = "images" | "pointcloud" | "text" | "unlock";

type DataManagementPageProps = {
  onPlaceholderAction?: (message?: string) => void;
};

const dataTabs = [
  { id: "images", label: "图像数据" },
  { id: "pointcloud", label: "点云数据" },
  { id: "text", label: "文本数据" },
  { id: "unlock", label: "数据解锁" },
] satisfies Array<TabItem<DataTab>>;

const imageGradients = [
  "from-cyan-300/45 via-slate-700/45 to-emerald-300/20",
  "from-violet-300/35 via-slate-700/50 to-cyan-300/25",
  "from-amber-300/35 via-slate-700/55 to-emerald-300/25",
  "from-sky-300/35 via-slate-700/45 to-rose-300/20",
];

function toneForStatus(status: string): StatusTone {
  if (status === "已标注" || status === "unlocked") {
    return "success";
  }

  if (status === "待标注" || status === "pending") {
    return "warning";
  }

  if (status === "rejected" || status === "已拒绝") {
    return "danger";
  }

  return "neutral";
}

function ImageDataPanel() {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {imageData.map((item, index) => (
        <ConsoleCard key={item.id} className="space-y-3">
          <div className={`relative h-36 overflow-hidden rounded border border-console-line bg-gradient-to-br ${imageGradients[index % imageGradients.length]}`}>
            <div className="absolute inset-4 rounded border border-white/10 bg-console-bg/20" />
            <div className="absolute left-5 top-5 h-10 w-16 rounded bg-console-cyan/25 shadow-[0_0_24px_rgba(21,209,216,0.22)]" />
            <div className="absolute bottom-5 right-5 h-12 w-12 rounded-full bg-amber-300/35" />
            <div className="absolute bottom-6 left-6 h-1 w-24 rounded-full bg-white/35" />
          </div>
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-console-text">{item.id}</h2>
              <p className="mt-1 text-xs text-console-muted">{item.scene}</p>
            </div>
            <StatusTag tone={toneForStatus(item.status)}>{item.status}</StatusTag>
          </div>
          <ProgressBar value={Number(item.conf) * 100} tone={toneForStatus(item.status)} label={`置信度 ${item.conf}`} />
        </ConsoleCard>
      ))}
    </div>
  );
}

function PointCloudPanel() {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {pointCloudData.map((item) => (
        <ConsoleCard key={item.id} className="space-y-3">
          <PointCloudPreview id={item.id} className="h-36 w-full rounded border border-console-line bg-console-bg" />
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-console-text">{item.id}</h2>
              <p className="mt-1 text-xs text-console-muted">
                {item.scene} / {item.points.toLocaleString()} points
              </p>
            </div>
            <StatusTag tone={toneForStatus(item.status)}>{item.status}</StatusTag>
          </div>
          <ProgressBar value={Number(item.conf) * 100} tone={toneForStatus(item.status)} label={`点云质量 ${item.conf}`} />
        </ConsoleCard>
      ))}
    </div>
  );
}

function TextDataPanel() {
  return (
    <ConsoleCard>
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-console-text">文本指令样本</h2>
          <p className="mt-1 text-sm text-console-muted">紧凑查看动作、对象与标注状态</p>
        </div>
        <StatusTag tone="info">{textInstructionData.length} 条</StatusTag>
      </div>
      <div className="space-y-2">
        {textInstructionData.map((item) => (
          <div key={item.id} className="grid gap-3 rounded border border-console-line bg-console-panel2/70 p-3 md:grid-cols-[5rem_1fr_8rem_6rem] md:items-center">
            <span className="text-xs font-semibold text-console-cyan">{item.id}</span>
            <p className="min-w-0 text-sm leading-5 text-console-text">{item.instruction}</p>
            <span className="text-xs text-console-muted">{item.action_type}</span>
            <StatusTag tone={toneForStatus(item.status)}>{item.status}</StatusTag>
          </div>
        ))}
      </div>
    </ConsoleCard>
  );
}

function UnlockPanel({ onPlaceholderAction }: DataManagementPageProps) {
  const pendingBatches = batchData.filter((item) => item.status === "pending");

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-[0.95fr_1.3fr]">
        <ConsoleCard>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-console-text">解锁规则配置</h2>
              <p className="mt-1 text-sm text-console-muted">质量门禁与批次放行条件</p>
            </div>
            <SlidersHorizontal aria-hidden="true" className="h-5 w-5 text-console-cyan" />
          </div>
          <div className="space-y-5">
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">质量阈值 0.85</span>
              <input className="w-full accent-cyan-300" type="range" min="0" max="100" value="85" readOnly aria-label="质量阈值" />
            </label>
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">自动标注置信度 0.80</span>
              <input className="w-full accent-emerald-300" type="range" min="0" max="100" value="80" readOnly aria-label="自动标注置信度" />
            </label>
            <label className="block space-y-2">
              <span className="text-sm text-console-muted">复核抽检比例 12%</span>
              <input className="w-full accent-amber-300" type="range" min="0" max="100" value="12" readOnly aria-label="复核抽检比例" />
            </label>
            <ConsoleButton variant="primary" onClick={() => onPlaceholderAction?.("解锁规则尚未接入后端")}>
              保存规则
            </ConsoleButton>
          </div>
        </ConsoleCard>

        <div className="grid gap-4 sm:grid-cols-3">
          <ConsoleCard className="flex items-center justify-between gap-3">
            <QualityRing value={92} label="图像质量" tone="success" />
          </ConsoleCard>
          <ConsoleCard className="flex items-center justify-between gap-3">
            <QualityRing value={87} label="点云质量" tone="info" />
          </ConsoleCard>
          <ConsoleCard className="flex items-center justify-between gap-3">
            <QualityRing value={79} label="文本一致性" tone="warning" />
          </ConsoleCard>
        </div>
      </div>

      <ConsoleCard>
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold text-console-text">批次管理</h2>
            <p className="mt-1 text-sm text-console-muted">{pendingBatches.length} 个批次等待解锁决策</p>
          </div>
          <ConsoleButton onClick={() => onPlaceholderAction?.("批量解锁尚未接入后端")}>批量解锁选中批次</ConsoleButton>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="text-xs uppercase text-console-muted">
              <tr className="border-b border-console-line">
                <th className="py-2 pr-3 font-medium">批次</th>
                <th className="py-2 pr-3 font-medium">类型</th>
                <th className="py-2 pr-3 font-medium">场景</th>
                <th className="py-2 pr-3 font-medium">数量</th>
                <th className="py-2 pr-3 font-medium">质量</th>
                <th className="py-2 pr-3 font-medium">状态</th>
              </tr>
            </thead>
            <tbody>
              {batchData.map((item) => (
                <tr key={item.id} className="border-b border-console-line/70">
                  <td className="py-3 pr-3 font-medium text-console-text">{item.id}</td>
                  <td className="py-3 pr-3 text-console-muted">{item.type}</td>
                  <td className="py-3 pr-3 text-console-muted">{item.scene}</td>
                  <td className="py-3 pr-3 text-console-muted">{item.count.toLocaleString()}</td>
                  <td className="py-3 pr-3 text-console-muted">{item.quality.toFixed(2)}</td>
                  <td className="py-3 pr-3">
                    <StatusTag tone={toneForStatus(item.status)}>{item.status}</StatusTag>
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

export function DataManagementPage({ onPlaceholderAction }: DataManagementPageProps) {
  const [activeTab, setActiveTab] = useState<DataTab>("images");
  const panelClass = (tab: DataTab) => cn(activeTab !== tab && "hidden");

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <SegmentedTabs value={activeTab} tabs={dataTabs} onChange={setActiveTab} aria-label="数据管理视图" />
        <div className="flex flex-wrap gap-2">
          <ConsoleButton onClick={() => onPlaceholderAction?.("上传数据尚未接入后端")}>
            <Upload aria-hidden="true" className="h-4 w-4" />
            上传数据
          </ConsoleButton>
          <ConsoleButton onClick={() => onPlaceholderAction?.("导出尚未接入后端")}>导出</ConsoleButton>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <ConsoleCard className="flex items-center gap-3">
          <Image aria-hidden="true" className="h-5 w-5 text-console-cyan" />
          <div>
            <p className="text-xs text-console-muted">图像样本</p>
            <p className="text-lg font-semibold">{imageData.length.toLocaleString()}</p>
          </div>
        </ConsoleCard>
        <ConsoleCard className="flex items-center gap-3">
          <Cloud aria-hidden="true" className="h-5 w-5 text-emerald-300" />
          <div>
            <p className="text-xs text-console-muted">点云批次</p>
            <p className="text-lg font-semibold">{pointCloudData.length.toLocaleString()}</p>
          </div>
        </ConsoleCard>
        <ConsoleCard className="flex items-center gap-3">
          <FileText aria-hidden="true" className="h-5 w-5 text-amber-300" />
          <div>
            <p className="text-xs text-console-muted">文本指令</p>
            <p className="text-lg font-semibold">{textInstructionData.length.toLocaleString()}</p>
          </div>
        </ConsoleCard>
        <ConsoleCard className="flex items-center gap-3">
          <LockKeyhole aria-hidden="true" className="h-5 w-5 text-violet-300" />
          <div>
            <p className="text-xs text-console-muted">待解锁批次</p>
            <p className="text-lg font-semibold">{batchData.filter((item) => item.status === "pending").length}</p>
          </div>
        </ConsoleCard>
      </div>

      <div className={panelClass("images")} hidden={activeTab !== "images"}>
        <ImageDataPanel />
      </div>
      <div className={panelClass("pointcloud")} hidden={activeTab !== "pointcloud"}>
        <PointCloudPanel />
      </div>
      <div className={panelClass("text")} hidden={activeTab !== "text"}>
        <TextDataPanel />
      </div>
      <div className={panelClass("unlock")} hidden={activeTab !== "unlock"}>
        <UnlockPanel onPlaceholderAction={onPlaceholderAction} />
      </div>
    </section>
  );
}
