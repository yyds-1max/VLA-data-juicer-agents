import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { ConsoleHeader } from "../components/console/ConsoleHeader";
import { ConsoleSidebar } from "../components/console/ConsoleSidebar";
import { ConsoleToast } from "../components/console/ConsoleToast";
import type { ConsolePageId, StatusTone } from "../features/console/consoleTypes";
import { AgentWorkflowPage } from "../features/console/pages/AgentWorkflowPage";
import { AnnotationPage } from "../features/console/pages/AnnotationPage";
import { DataManagementPage } from "../features/console/pages/DataManagementPage";
import { DashboardPage } from "../features/console/pages/DashboardPage";
import { ModelIterationPage } from "../features/console/pages/ModelIterationPage";
import { SimulationPage } from "../features/console/pages/SimulationPage";
import { cn } from "../lib/utils";

type AppShellProps = {
  children?: ReactNode;
};

type ToastState = { message: string; tone: StatusTone } | null;

const CONSOLE_SIDEBAR_STORAGE_KEY = "vla-console-sidebar";

function readInitialSidebarCollapsed(): boolean {
  if (typeof window === "undefined") {
    return false;
  }

  try {
    return window.localStorage.getItem(CONSOLE_SIDEBAR_STORAGE_KEY) === "collapsed";
  } catch {
    return false;
  }
}

const pageCopy: Record<ConsolePageId, { title: string }> = {
  dashboard: { title: "闭环仪表盘" },
  agent: { title: "Agent 工作流" },
  data: { title: "数据管理" },
  annotate: { title: "自动标注" },
  model: { title: "模型迭代" },
  simulation: { title: "测试/仿真" },
};

export function AppShell({ children }: AppShellProps) {
  const location = useLocation();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readInitialSidebarCollapsed);
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

  useEffect(() => {
    try {
      window.localStorage.setItem(CONSOLE_SIDEBAR_STORAGE_KEY, sidebarCollapsed ? "collapsed" : "expanded");
    } catch {
      // Storage can be unavailable in hardened or private browser contexts.
    }
  }, [sidebarCollapsed]);

  const activePage: ConsolePageId = location.pathname.startsWith("/annotation/jobs")
    ? "annotate"
    : location.pathname.startsWith("/agent")
      ? "agent"
      : location.pathname.startsWith("/data")
        ? "data"
        : location.pathname.startsWith("/model")
          ? "model"
          : location.pathname.startsWith("/simulation")
            ? "simulation"
            : "dashboard";
  const activeTitle = pageCopy[activePage].title;

  return (
    <div className="min-h-screen bg-console-bg text-console-text">
      <ConsoleSidebar
        activePage={activePage}
        collapsed={sidebarCollapsed}
        onCollapsedChange={setSidebarCollapsed}
      />

      <main
        data-testid="console-main"
        className={cn(
          "relative z-10 pt-28 md:pt-0 md:transition-[margin-left] md:duration-200",
          sidebarCollapsed ? "md:ml-20" : "md:ml-64",
        )}
      >
        <ConsoleHeader title={activeTitle} />
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/agent" element={<AgentWorkflowPage onPlaceholderAction={showPlaceholderToast} />} />
          <Route path="/data" element={<DataManagementPage onPlaceholderAction={showPlaceholderToast} />} />
          <Route path="/annotation/jobs" element={<AnnotationPage />} />
          <Route path="/annotation/jobs/:jobRef" element={<AnnotationPage />} />
          <Route path="/annotation/jobs/:jobRef/segments/:segmentRef" element={<AnnotationPage />} />
          <Route path="/model" element={<ModelIterationPage onPlaceholderAction={showPlaceholderToast} />} />
          <Route path="/simulation" element={<SimulationPage onPlaceholderAction={showPlaceholderToast} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>

      {children}
      <ConsoleToast toast={toast} />
    </div>
  );
}
