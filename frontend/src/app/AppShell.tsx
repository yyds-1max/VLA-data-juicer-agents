import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { ConsoleHeader } from "../components/console/ConsoleHeader";
import { ConsoleSidebar } from "../components/console/ConsoleSidebar";
import { ConsoleToast } from "../components/console/ConsoleToast";
import type { ConsolePageId, StatusTone } from "../features/console/consoleTypes";
import { DashboardPage } from "../features/console/pages/DashboardPage";
import { BackgroundParticles } from "../features/console/visuals/BackgroundParticles";

type AppShellProps = {
  children?: ReactNode;
};

type ToastState = { message: string; tone: StatusTone } | null;

const pageCopy: Record<ConsolePageId, { title: string; text: string }> = {
  dashboard: {
    title: "闭环仪表盘",
    text: "汇总数据闭环、Agent 协作、模型迭代与仿真验证的核心状态。完整仪表盘将在后续任务接入。",
  },
  agent: {
    title: "Agent 工作流",
    text: "编排数据源、预处理、自动标注、质量检查、训练和评估节点。当前为迁移壳层占位。",
  },
  data: {
    title: "数据管理",
    text: "管理多模态数据批次、质量门禁和解锁状态。完整数据页面将在后续任务替换。",
  },
  annotate: {
    title: "自动标注",
    text: "承载自动标注任务、模型输出和人工复核入口。当前仅保留页面占位。",
  },
  model: {
    title: "模型迭代",
    text: "跟踪训练版本、部署状态和指标曲线。后续任务会接入真实模型迭代内容。",
  },
  simulation: {
    title: "测试/仿真",
    text: "展示测试用例、仿真报告和发布前验证结果。当前为最小可导航占位。",
  },
};

function PagePlaceholder({ pageId, onRequestToast }: { pageId: ConsolePageId; onRequestToast: () => void }) {
  const page = pageCopy[pageId];

  return (
    <section className="mx-auto max-w-7xl px-4 py-6 md:px-6">
      <div className="grid min-h-[calc(100vh-13rem)] gap-4 lg:grid-cols-[1fr_20rem]">
        <div className="rounded border border-console-line bg-console-panel/88 p-5 shadow-[0_18px_48px_rgba(0,0,0,0.2)]">
          <div className="mb-4 h-1.5 w-28 rounded-full bg-console-cyan/70" />
          <p className="text-xs uppercase tracking-[0.18em] text-console-muted">Console route</p>
          <p className="mt-3 max-w-3xl text-sm leading-6 text-console-muted">{page.text}</p>
          <button
            type="button"
            className="mt-5 rounded border border-console-line bg-console-panel2 px-3 py-2 text-sm text-console-text transition hover:border-console-cyan/45 hover:text-console-cyan focus:outline-none focus:ring-2 focus:ring-console-cyan"
            onClick={onRequestToast}
          >
            查看接入状态
          </button>
        </div>

        <aside className="rounded border border-console-line bg-console-panel/88 p-4">
          <h2 className="text-sm font-semibold">迁移状态</h2>
          <p className="mt-3 text-sm leading-6 text-console-muted">
            壳层导航、顶部栏、背景视觉和浮层入口已在 React 中就位。页面主体保持轻量占位，等待后续任务迁入完整内容。
          </p>
        </aside>
      </div>
    </section>
  );
}

export function AppShell({ children }: AppShellProps) {
  const [activePage, setActivePage] = useState<ConsolePageId>("dashboard");
  const [toast, setToast] = useState<ToastState>(null);
  const toastTimeoutRef = useRef<number | null>(null);

  const showPlaceholderToast = useCallback((message = "该功能暂未接入后端") => {
    if (toastTimeoutRef.current !== null) {
      window.clearTimeout(toastTimeoutRef.current);
    }

    setToast({ message, tone: "neutral" });
    toastTimeoutRef.current = window.setTimeout(() => {
      setToast(null);
      toastTimeoutRef.current = null;
    }, 2400);
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimeoutRef.current !== null) {
        window.clearTimeout(toastTimeoutRef.current);
      }
    };
  }, []);

  const activeTitle = pageCopy[activePage].title;

  return (
    <div className="min-h-screen bg-console-bg text-console-text">
      <BackgroundParticles />
      <ConsoleSidebar activePage={activePage} onChange={setActivePage} />

      <main className="relative z-10 pt-28 md:ml-64 md:pt-0">
        <ConsoleHeader title={activeTitle} />
        {activePage === "dashboard" ? <DashboardPage /> : <PagePlaceholder pageId={activePage} onRequestToast={() => showPlaceholderToast()} />}
      </main>

      {children}
      <ConsoleToast toast={toast} />
    </div>
  );
}
