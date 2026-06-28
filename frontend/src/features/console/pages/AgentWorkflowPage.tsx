import { Database, GitBranch, Play, Save, Settings2, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";

import { ConsoleButton } from "../../../components/console/ConsoleButton";
import { ConsoleCard } from "../../../components/console/ConsoleCard";
import { ProgressBar } from "../../../components/console/ProgressBar";
import { StatusTag } from "../../../components/console/StatusTag";
import { cn } from "../../../lib/utils";
import { agentConnections, agentNodes } from "../consoleFixtures";
import { AgentConnectionCanvas } from "../visuals/AgentConnectionCanvas";

type AgentWorkflowPageProps = {
  onPlaceholderAction?: (message?: string) => void;
};

const workflowCoordinates = [
  { id: "an-1", x: 13, y: 22 },
  { id: "an-2", x: 35, y: 22 },
  { id: "an-3", x: 57, y: 22 },
  { id: "an-4", x: 79, y: 22 },
  { id: "an-5", x: 79, y: 52 },
  { id: "an-6", x: 57, y: 74 },
  { id: "an-7", x: 35, y: 74 },
  { id: "an-8", x: 13, y: 74 },
];

const categoryTone: Record<string, "success" | "info" | "warning" | "danger" | "neutral" | "purple"> = {
  数据源: "info",
  处理: "success",
  标注器: "purple",
  质量: "warning",
  分支: "neutral",
  模型: "success",
  仿真: "info",
  发布: "purple",
};

export function AgentWorkflowPage({ onPlaceholderAction }: AgentWorkflowPageProps) {
  const [selectedNodeId, setSelectedNodeId] = useState("an-1");
  const selectedNode = agentNodes.find((node) => node.id === selectedNodeId) ?? agentNodes[0];
  const positionedNodes = useMemo(
    () =>
      workflowCoordinates.map((position) => {
        const node = agentNodes.find((item) => item.id === position.id);

        return { ...position, node };
      }),
    [],
  );

  return (
    <section className="mx-auto max-w-7xl space-y-4 px-4 py-6 md:px-6">
      <div className="grid gap-4 xl:grid-cols-[17rem_1fr_20rem]">
        <ConsoleCard className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-console-text">节点库</h2>
              <p className="mt-1 text-sm text-console-muted">可视化拖拽外观，当前不保存布局状态</p>
            </div>
            <Sparkles aria-hidden="true" className="h-5 w-5 text-console-cyan" />
          </div>

          <div className="space-y-2">
            {agentNodes.map((node) => (
              <button
                key={node.id}
                type="button"
                aria-label={node.name}
                className={cn(
                  "group w-full cursor-grab rounded border border-console-line bg-console-panel2/80 p-3 text-left transition focus:outline-none focus:ring-2 focus:ring-console-cyan",
                  selectedNodeId === node.id && "border-console-cyan/50 bg-console-cyan/10",
                  selectedNodeId !== node.id && "hover:border-console-cyan/35 hover:bg-console-panel2",
                )}
                onClick={() => setSelectedNodeId(node.id)}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-console-text">{node.name}</span>
                  <StatusTag tone={categoryTone[node.category] ?? "neutral"}>{node.category}</StatusTag>
                </div>
                <p className="mt-2 line-clamp-2 text-xs leading-5 text-console-muted">{node.desc}</p>
              </button>
            ))}
          </div>
        </ConsoleCard>

        <ConsoleCard className="space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-base font-semibold text-console-text">工作流画布</h2>
              <p className="mt-1 text-sm text-console-muted">固定示例流程，仅用于前端编排展示</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <ConsoleButton onClick={() => onPlaceholderAction?.("保存流程暂未接入后端")}>
                <Save aria-hidden="true" className="h-4 w-4" />
                保存流程
              </ConsoleButton>
              <ConsoleButton variant="primary" onClick={() => onPlaceholderAction?.("执行流程暂未接入后端")}>
                <Play aria-hidden="true" className="h-4 w-4" />
                执行流程
              </ConsoleButton>
            </div>
          </div>

          <div className="relative min-h-[31rem] overflow-hidden rounded border border-console-line bg-console-bg/80">
            <div className="absolute inset-0 bg-[linear-gradient(rgba(148,163,184,0.08)_1px,transparent_1px),linear-gradient(90deg,rgba(148,163,184,0.08)_1px,transparent_1px)] bg-[size:32px_32px]" />
            <AgentConnectionCanvas nodes={workflowCoordinates} connections={agentConnections} className="absolute inset-0 h-full w-full" />
            {positionedNodes.map(({ node, x, y }) => {
              if (!node) {
                return null;
              }

              const active = selectedNodeId === node.id;

              return (
                <button
                  key={node.id}
                  type="button"
                  aria-label={`画布节点 ${node.name}`}
                  className={cn(
                    "absolute w-36 -translate-x-1/2 -translate-y-1/2 rounded border bg-console-panel/95 p-3 text-left shadow-[0_12px_30px_rgba(0,0,0,0.24)] transition focus:outline-none focus:ring-2 focus:ring-console-cyan",
                    active ? "border-console-cyan text-console-cyan" : "border-console-line text-console-text hover:border-console-cyan/45",
                  )}
                  style={{ left: `${x}%`, top: `${y}%` }}
                  onClick={() => setSelectedNodeId(node.id)}
                >
                  <span className="block truncate text-sm font-semibold">{node.name}</span>
                  <span className="mt-1 block text-xs text-console-muted">{node.category}</span>
                </button>
              );
            })}
          </div>
        </ConsoleCard>

        <aside className="space-y-4">
          <ConsoleCard>
            <div className="mb-4 flex items-center gap-2">
              <Settings2 aria-hidden="true" className="h-5 w-5 text-console-cyan" />
              <h2 className="text-base font-semibold text-console-text">节点属性</h2>
            </div>
            <div className="space-y-4">
              <div>
                <p className="text-xs uppercase tracking-[0.16em] text-console-muted">Selected node</p>
                <h3 className="mt-2 text-lg font-semibold text-console-text">{selectedNode.name}</h3>
                {selectedNode.id === "an-1" ? <p className="mt-2 text-sm leading-6 text-console-muted">从多个数据源拉取原始数据</p> : null}
                <p className="mt-2 text-sm leading-6 text-console-muted">{selectedNode.desc}</p>
              </div>
              <div className="grid grid-cols-2 gap-2 text-sm">
                <div className="rounded border border-console-line bg-console-panel2/70 p-3">
                  <p className="text-xs text-console-muted">输入</p>
                  <p className="mt-1 font-semibold text-console-text">batch.stream</p>
                </div>
                <div className="rounded border border-console-line bg-console-panel2/70 p-3">
                  <p className="text-xs text-console-muted">输出</p>
                  <p className="mt-1 font-semibold text-console-text">node.result</p>
                </div>
              </div>
              <ProgressBar value={82} tone="info" label="示例配置完整度 82%" />
            </div>
          </ConsoleCard>

          <ConsoleCard>
            <div className="mb-4 flex items-center gap-2">
              <Database aria-hidden="true" className="h-5 w-5 text-console-cyan" />
              <h2 className="text-base font-semibold text-console-text">执行摘要</h2>
            </div>
            <div className="space-y-3">
              {[
                ["节点数量", "8"],
                ["连接数量", String(agentConnections.length)],
                ["状态", "草稿"],
              ].map(([label, value]) => (
                <div key={label} className="flex items-center justify-between gap-3 rounded border border-console-line bg-console-panel2/70 p-3 text-sm">
                  <span className="text-console-muted">{label}</span>
                  <span className="font-semibold text-console-text">{value}</span>
                </div>
              ))}
            </div>
          </ConsoleCard>

          <ConsoleCard>
            <div className="mb-3 flex items-center gap-2">
              <GitBranch aria-hidden="true" className="h-5 w-5 text-console-cyan" />
              <h2 className="text-base font-semibold text-console-text">分支策略</h2>
            </div>
            <p className="text-sm leading-6 text-console-muted">质量检查未达阈值时回流至预处理管线，达标数据进入模型训练与仿真评估。</p>
          </ConsoleCard>
        </aside>
      </div>
    </section>
  );
}
