import { useId, useState } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../../components/ui/select";
import { cn } from "../../../lib/utils";
import {
  ProcessTimeline,
  type ProcessTimelineStep,
} from "./ProcessTimeline";

export const DATA_FLOW_STAGES = [
  "原数据",
  "拆解同步",
  "标注处理",
  "AI复核",
  "人工复核",
  "模型训练",
  "测试批复",
  "部署验证",
] as const;

export type DataFlowStage = (typeof DATA_FLOW_STAGES)[number];

export const DATA_FLOW_STAGE_DESCRIPTIONS: Record<DataFlowStage, string> = {
  原数据: "已采集的原始 ROS Bag 按日期和片段接入服务器，保留消息、时长与传感器主题等只读信息，等待处理。",
  拆解同步: "将选定原始片段拆解为传感器数据，并按参考时间轴同步图像、点云和里程计，生成 sync_data。",
  标注处理: "对同步产物进行预处理和 Web 首帧标注，随后执行 Tracking、投影与轨迹生成，形成待复核结果。",
  AI复核: "计划由模型读取与轨迹版本绑定的受控证据，输出结构化问题与修正建议；当前尚未接入，且不能代替人工批准。",
  人工复核: "人工在三维轨迹工作台检查位置、方向和速度，必要时修正、退回或废弃；批准并成功发布后数据才标记为已验证。",
  模型训练: "计划使用已验证并成功发布的训练兼容数据迭代 VLA 模型，记录训练版本和指标；当前训练链路仍为前端占位。",
  测试批复: "计划在测试集或仿真场景评估候选模型的成功率、碰撞、延迟与稳定性，并由人工批复结果；当前尚未接入后端。",
  部署验证: "计划将批复通过的候选模型发布到受控验证环境，观察关键指标并确认版本可用性；当前部署验证流程仍为占位。",
};

export interface DataFlowBatch {
  id: string;
  currentStage: DataFlowStage;
}

export const DEFAULT_DATA_FLOW_BATCHES: readonly DataFlowBatch[] = [
  { id: "20260623", currentStage: "人工复核" },
  { id: "20260621", currentStage: "模型训练" },
  { id: "20260618", currentStage: "部署验证" },
];

export interface DataFlowTimelineProps {
  batches?: readonly DataFlowBatch[];
  className?: string;
  defaultBatchId?: string;
  disabled?: boolean;
  selectedBatchId?: string;
  onBatchChange?: (batchId: string, batch: DataFlowBatch) => void;
}

function resolveStepState(index: number, currentIndex: number): ProcessTimelineStep["state"] {
  if (index < currentIndex) {
    return "completed";
  }
  if (index === currentIndex) {
    return "current";
  }
  return "pending";
}

export function DataFlowTimeline({
  batches = DEFAULT_DATA_FLOW_BATCHES,
  className,
  defaultBatchId = batches[0]?.id,
  disabled = false,
  selectedBatchId,
  onBatchChange,
}: DataFlowTimelineProps) {
  const headingId = useId();
  const [internalBatchId, setInternalBatchId] = useState(defaultBatchId);
  const requestedBatchId = selectedBatchId ?? internalBatchId;
  const selectedBatch = batches.find((batch) => batch.id === requestedBatchId) ?? batches[0];
  const currentIndex = selectedBatch ? DATA_FLOW_STAGES.indexOf(selectedBatch.currentStage) : -1;
  const timelineSteps: ProcessTimelineStep[] = DATA_FLOW_STAGES.map((stage, index) => ({
    id: stage,
    label: stage,
    state: resolveStepState(index, currentIndex),
    description: DATA_FLOW_STAGE_DESCRIPTIONS[stage],
  }));

  function handleBatchChange(batchId: string) {
    const nextBatch = batches.find((batch) => batch.id === batchId);
    if (!nextBatch) {
      return;
    }

    if (selectedBatchId === undefined) {
      setInternalBatchId(batchId);
    }
    onBatchChange?.(batchId, nextBatch);
  }

  return (
    <section className={cn("min-w-0", className)} aria-labelledby={headingId}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id={headingId} className="text-base font-semibold text-[#202431]">
            数据闭环流程
          </h2>
        </div>

        <Select
          disabled={disabled || batches.length === 0}
          value={selectedBatch?.id ?? ""}
          onValueChange={handleBatchChange}
        >
          <SelectTrigger
            aria-label="选择数据批次"
            className="h-9 min-w-32 border-[#E3E6EF] bg-white px-3 text-[#30374A] shadow-[0_1px_2px_rgba(31,42,68,0.04)] transition-[border-color,box-shadow,background-color] duration-150 hover:border-[#BFC8E4] hover:bg-[#FAFBFF] focus-visible:border-[#3156C8] focus-visible:ring-[#3156C8]/20 data-[state=open]:border-[#3156C8] data-[state=open]:ring-3 data-[state=open]:ring-[#3156C8]/15"
          >
            <SelectValue placeholder="暂无批次" />
          </SelectTrigger>
          <SelectContent align="end" position="popper">
            {batches.map((batch) => (
              <SelectItem key={batch.id} value={batch.id}>
                {batch.id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {selectedBatch ? (
        <ProcessTimeline
          ariaLabel={`${selectedBatch.id} 数据闭环流程，可横向滚动`}
          className="mt-6"
          minWidthClassName="min-w-[64rem]"
          showSweep
          steps={timelineSteps}
          testIdPrefix="data-flow"
        />
      ) : (
        <div
          className="mt-6 flex min-h-24 items-center justify-center rounded-xl border border-dashed border-[#DDE2EE] bg-[#FAFBFD] px-4 text-sm text-[#626B7D]"
          role="status"
        >
          暂无可展示的数据批次
        </div>
      )}
    </section>
  );
}
