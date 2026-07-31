import {
  Bot,
  ChartNoAxesCombined,
  Database,
  FlaskConical,
  GitBranch,
  PanelLeftClose,
  PanelLeftOpen,
  PenTool,
} from "lucide-react";
import { useNavigate } from "react-router-dom";

import type { ConsolePageId, NavItem } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type ConsoleSidebarProps = {
  activePage: ConsolePageId;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
};

export const consoleNavItems: NavItem[] = [
  { id: "dashboard", label: "闭环仪表盘", group: "概览", icon: ChartNoAxesCombined, path: "/" },
  { id: "agent", label: "Agent 工作流", group: "流程", icon: GitBranch, path: "/agent" },
  { id: "data", label: "数据管理", group: "数据", icon: Database, path: "/data" },
  { id: "annotate", label: "自动标注", group: "标注", icon: PenTool, path: "/annotation/jobs" },
  { id: "model", label: "模型迭代", group: "模型", icon: Bot, path: "/model" },
  { id: "simulation", label: "测试/仿真", group: "验证", icon: FlaskConical, path: "/simulation" },
];

export function ConsoleSidebar({ activePage, collapsed, onCollapsedChange }: ConsoleSidebarProps) {
  const navigate = useNavigate();

  return (
    <aside
      data-testid="console-sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      className={cn(
        "fixed inset-x-0 top-0 z-20 border-b border-console-line bg-console-panel/95 px-4 shadow-xs backdrop-blur-sm md:inset-y-0 md:left-0 md:right-auto md:border-b-0 md:border-r md:px-0 md:shadow-none md:transition-[width] md:duration-200",
        collapsed ? "md:w-20" : "md:w-64",
      )}
    >
      <div className="flex h-full flex-col">
        <div
          className={cn(
            "flex h-16 items-center gap-3 md:h-auto md:border-b md:border-console-line md:p-5",
            collapsed && "md:justify-center md:px-3",
          )}
        >
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-console-line bg-white p-1.5 shadow-xs">
            <img
              src="/brand/wise-explore-favicon.png"
              alt="智瀚星途 logo"
              className="max-h-full max-w-full object-contain"
            />
          </div>
          <div className={cn("min-w-0", collapsed && "md:hidden")}>
            <div className="truncate text-base font-semibold tracking-normal text-console-text">智瀚星途</div>
            <div className="truncate text-[10px] font-medium uppercase tracking-[0.34em] text-console-muted">WISEXPLORE</div>
          </div>
        </div>

        <nav
          className={cn(
            "min-w-0 flex-1 overflow-x-auto border-t border-console-line py-2 md:overflow-y-auto md:border-t-0 md:py-4",
            collapsed ? "md:px-2" : "md:px-3",
          )}
          aria-label="DataLoop console navigation"
        >
          <ul className="flex gap-2 md:block md:space-y-2">
            {consoleNavItems.map((item, index) => (
              <li key={item.id} className="md:space-y-1">
                <div
                  className={cn(
                    "hidden h-8 items-center text-[10px] font-medium uppercase tracking-[0.12em] text-console-muted md:flex",
                    collapsed ? "justify-center px-0" : "justify-between pl-3 pr-2",
                  )}
                >
                  <span className={cn(collapsed && "sr-only")}>{item.group}</span>
                  {index === 0 ? (
                    <button
                      type="button"
                      aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
                      title={collapsed ? "展开侧边栏" : "收起侧边栏"}
                      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-hidden focus-visible:bg-console-panel2 focus-visible:text-console-text"
                      onClick={() => onCollapsedChange(!collapsed)}
                    >
                      {collapsed ? (
                        <PanelLeftOpen className="h-[18px] w-[18px]" aria-hidden="true" />
                      ) : (
                        <PanelLeftClose className="h-[18px] w-[18px]" aria-hidden="true" />
                      )}
                    </button>
                  ) : null}
                </div>
                <button
                  type="button"
                  aria-current={activePage === item.id ? "page" : undefined}
                  title={collapsed ? item.label : undefined}
                  className={cn(
                    "flex h-10 w-max min-w-32 shrink-0 items-center gap-2 rounded-lg border border-transparent bg-transparent px-3 text-left text-sm text-console-muted transition hover:bg-console-panel2 hover:text-console-text focus:outline-hidden focus:ring-2 focus:ring-console-cyan md:w-full",
                    collapsed && "md:min-w-0 md:justify-center md:px-0",
                    activePage === item.id &&
                      "border-console-line bg-console-panel2 text-console-text shadow-[inset_3px_0_0_#2d6cdf]",
                  )}
                  onClick={() => navigate(item.path)}
                >
                  <item.icon className={cn("h-4 w-4 shrink-0", activePage === item.id && "text-console-cyan")} aria-hidden="true" />
                  <span className={cn("truncate", collapsed && "md:sr-only")}>{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <div className={cn("hidden border-t border-console-line p-4 md:block", collapsed && "md:hidden")}>
          <div className="px-3 py-2 text-sm font-medium leading-5 text-console-text">智瀚星途数据处理系统</div>
        </div>
      </div>
    </aside>
  );
}
