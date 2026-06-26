import type { ReactNode } from "react";
import { Activity, Database, FlaskConical, Route, Settings } from "lucide-react";

import { cn } from "../lib/utils";

type AppShellProps = {
  children?: ReactNode;
};

const navItems = [
  { label: "闭环仪表盘", icon: Activity, active: true },
  { label: "数据管理", icon: Database },
  { label: "实验验证", icon: FlaskConical },
  { label: "工作流", icon: Route },
  { label: "系统设置", icon: Settings },
];

const metricCards = [
  {
    title: "数据接入",
    value: "24.8K",
    detail: "今日新增轨迹",
    accent: "from-console-cyan/25 to-emerald-400/10",
    meta: "12 路数据源在线",
  },
  {
    title: "处理任务",
    value: "18",
    detail: "队列运行中",
    accent: "from-blue-400/25 to-console-cyan/10",
    meta: "平均等待 03m 12s",
  },
  {
    title: "质量检查",
    value: "97.4%",
    detail: "有效样本率",
    accent: "from-emerald-400/25 to-console-cyan/10",
    meta: "3 项规则需复核",
  },
  {
    title: "Agent 状态",
    value: "6/7",
    detail: "协作节点在线",
    accent: "from-violet-400/25 to-console-cyan/10",
    meta: "评估 Agent 待命",
  },
];

const pipelineSteps = ["采集", "清洗", "标注", "评估", "发布"];

export function AppShell({ children }: AppShellProps) {
  return (
    <div className="min-h-screen bg-console-bg text-console-text">
      <aside className="fixed inset-x-0 top-0 z-20 border-b border-console-line bg-console-panel/95 px-4 backdrop-blur md:inset-y-0 md:left-0 md:right-auto md:w-60 md:border-b-0 md:border-r md:px-0">
        <div className="flex h-16 items-center justify-between md:h-auto md:flex-col md:items-stretch">
          <div className="flex items-center gap-3 md:border-b md:border-console-line md:p-5">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-console-cyan text-console-bg">
              <Route className="h-5 w-5" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold tracking-wide">DataLoop</div>
              <div className="truncate text-[11px] uppercase tracking-[0.16em] text-console-muted">
                Voyager Forge
              </div>
            </div>
          </div>

          <nav className="hidden flex-1 px-3 py-4 md:block" aria-label="DataLoop navigation">
            <div className="mb-2 px-3 text-[10px] uppercase tracking-[0.24em] text-console-muted">
              Console
            </div>
            <div className="space-y-1">
              {navItems.map((item) => (
                <button
                  key={item.label}
                  className={cn(
                    "flex w-full items-center gap-3 rounded px-3 py-2.5 text-left text-sm text-console-muted transition hover:bg-console-panel2 hover:text-console-text",
                    item.active &&
                      "border border-console-cyan/35 bg-console-cyan/10 text-console-cyan shadow-[inset_3px_0_0_#15d1d8]",
                  )}
                  type="button"
                >
                  <item.icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{item.label}</span>
                </button>
              ))}
            </div>
          </nav>
        </div>

        <nav className="flex gap-2 overflow-x-auto border-t border-console-line py-2 md:hidden" aria-label="DataLoop mobile navigation">
          {navItems.map((item) => (
            <button
              key={item.label}
              className={cn(
                "flex shrink-0 items-center gap-2 rounded border border-console-line px-3 py-2 text-xs text-console-muted",
                item.active && "border-console-cyan/50 bg-console-cyan/10 text-console-cyan",
              )}
              type="button"
            >
              <item.icon className="h-4 w-4" aria-hidden="true" />
              {item.label}
            </button>
          ))}
        </nav>
      </aside>

      <main className="px-4 pb-8 pt-32 md:ml-60 md:px-6 md:pt-0">
        <header className="hidden h-16 items-center justify-between border-b border-console-line md:flex">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-console-muted">Operational Console</p>
            <h1 className="mt-1 text-lg font-semibold">闭环仪表盘</h1>
          </div>
          <div className="flex items-center gap-2 rounded border border-console-line bg-console-panel px-3 py-2 text-xs text-console-muted">
            <span className="h-2 w-2 rounded-full bg-console-cyan shadow-[0_0_12px_#15d1d8]" />
            Cluster stable
          </div>
        </header>

        <section className="mx-auto max-w-7xl py-5">
          <div className="mb-5 md:hidden">
            <p className="text-xs uppercase tracking-[0.22em] text-console-muted">Operational Console</p>
            <h1 className="mt-1 text-lg font-semibold">闭环仪表盘</h1>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {metricCards.map((card) => (
              <article
                key={card.title}
                className="min-h-36 rounded border border-console-line bg-console-panel p-4 shadow-[0_16px_40px_rgba(0,0,0,0.22)]"
              >
                <div className={cn("mb-4 h-1.5 rounded-full bg-gradient-to-r", card.accent)} />
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="truncate text-sm font-medium text-console-muted">{card.title}</h2>
                    <p className="mt-2 text-2xl font-semibold tracking-normal text-console-text">{card.value}</p>
                  </div>
                  <span className="rounded border border-console-cyan/30 bg-console-cyan/10 px-2 py-1 text-[11px] text-console-cyan">
                    Live
                  </span>
                </div>
                <p className="mt-2 text-sm text-console-text">{card.detail}</p>
                <p className="mt-4 truncate text-xs text-console-muted">{card.meta}</p>
              </article>
            ))}
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-[1.35fr_0.65fr]">
            <section className="rounded border border-console-line bg-console-panel p-4">
              <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h2 className="text-sm font-semibold">数据闭环流水线</h2>
                  <p className="mt-1 text-xs text-console-muted">最近 24 小时批处理吞吐与节点健康</p>
                </div>
                <span className="w-fit rounded border border-console-line bg-console-panel2 px-3 py-1 text-xs text-console-muted">
                  Batch DL-0626
                </span>
              </div>
              <div className="grid gap-3 md:grid-cols-5">
                {pipelineSteps.map((step, index) => (
                  <div key={step} className="rounded border border-console-line bg-console-panel2 p-3">
                    <div className="mb-3 flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">{step}</span>
                      <span className="text-xs text-console-cyan">0{index + 1}</span>
                    </div>
                    <div className="h-2 overflow-hidden rounded bg-console-bg">
                      <div className="h-full rounded bg-console-cyan" style={{ width: `${92 - index * 7}%` }} />
                    </div>
                    <p className="mt-3 text-xs text-console-muted">{92 - index * 7}% ready</p>
                  </div>
                ))}
              </div>
            </section>

            <section className="rounded border border-console-line bg-console-panel p-4">
              <h2 className="text-sm font-semibold">运行信号</h2>
              <div className="mt-4 space-y-3">
                {["采集延迟正常", "清洗规则更新", "质量阈值稳定"].map((item, index) => (
                  <div key={item} className="flex items-center gap-3 rounded border border-console-line bg-console-panel2 px-3 py-2">
                    <span
                      className={cn(
                        "h-2.5 w-2.5 rounded-full",
                        index === 1 ? "bg-amber-300" : "bg-console-cyan shadow-[0_0_10px_#15d1d8]",
                      )}
                    />
                    <span className="min-w-0 truncate text-sm">{item}</span>
                  </div>
                ))}
              </div>
            </section>
          </div>

          {children}
        </section>
      </main>
    </div>
  );
}
