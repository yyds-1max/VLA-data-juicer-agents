import {
  Bot,
  ChartNoAxesCombined,
  CircleUserRound,
  Database,
  FlaskConical,
  GitBranch,
  PanelLeftClose,
  PanelLeftOpen,
  PenTool,
} from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";

import type { ConsolePageId, NavItem } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";
import { Avatar, AvatarFallback } from "../ui/avatar";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../ui/tooltip";

type ConsoleSidebarProps = {
  activePage: ConsolePageId;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
};

export const consoleNavItems: NavItem[] = [
  { id: "dashboard", label: "仪表盘", group: "概览", icon: ChartNoAxesCombined, path: "/" },
  { id: "agent", label: "Agent 工作流", group: "流程", icon: GitBranch, path: "/agent" },
  { id: "data", label: "数据管理", group: "数据", icon: Database, path: "/data" },
  { id: "annotate", label: "自动标注", group: "标注", icon: PenTool, path: "/annotation/jobs" },
  { id: "model", label: "模型训练", group: "模型", icon: Bot, path: "/model" },
  { id: "simulation", label: "测试/仿真", group: "验证", icon: FlaskConical, path: "/simulation" },
];

const dataManagementSubpages = [
  { label: "数据资产", path: "/data" },
  { label: "训练发布", path: "/data/releases" },
] as const;

export function ConsoleSidebar({ activePage, collapsed, onCollapsedChange }: ConsoleSidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <aside
      data-testid="console-sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      className={cn(
        "fixed inset-x-0 top-0 z-20 overflow-hidden border-b border-[#e4e7ee] bg-[#f4f5f8]/95 px-4 shadow-xs backdrop-blur-sm md:inset-y-0 md:left-0 md:right-auto md:border-b-0 md:border-r md:px-0 md:shadow-none md:transition-[width] md:duration-[240ms] md:ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none",
        collapsed ? "md:w-20" : "md:w-64",
      )}
    >
      <div className="flex h-full flex-col">
        <div
          className={cn(
            "flex h-16 items-center gap-3 md:h-[92px] md:px-6 md:transition-[padding] md:duration-[240ms] md:ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none",
            collapsed && "md:px-5",
          )}
        >
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-[#dfe3eb] bg-white p-1.5 shadow-[0_3px_10px_rgba(30,41,59,0.05)]">
            <img
              src="/brand/wise-explore-favicon.png"
              alt="智瀚星途 logo"
              className="max-h-full max-w-full object-contain"
            />
          </div>
          <div
            className={cn(
              "min-w-0 transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none",
              collapsed && "md:pointer-events-none md:-translate-x-1 md:opacity-0",
            )}
          >
            <div className="truncate text-base font-semibold tracking-normal text-[#1d2433]">智瀚星途</div>
            <div className="mt-0.5 truncate text-[10px] font-medium uppercase tracking-[0.34em] text-[#5f687b]">WISEXPLORE</div>
          </div>
        </div>

        <nav
          id="console-primary-navigation"
          className="min-w-0 flex-1 overflow-x-auto border-t border-[#e4e7ee] py-2 md:overflow-y-auto md:border-t-0 md:px-3 md:py-2"
          aria-label="系统主导航"
        >
          <TooltipProvider delayDuration={180}>
            <ul className="flex gap-2 md:block md:space-y-1">
              {consoleNavItems.map((item, index) => (
                <li key={item.id} className="md:space-y-1">
                  <div
                    className="relative hidden h-8 items-center overflow-hidden pl-3 pr-2 text-[10px] font-medium uppercase tracking-[0.12em] text-[#60697a] md:flex"
                  >
                    <span
                      className={cn(
                        "transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none",
                        collapsed && "pointer-events-none -translate-x-1 opacity-0",
                      )}
                    >
                      {item.group}
                    </span>
                    {index === 0 ? (
                      <button
                        type="button"
                        aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
                        aria-controls="console-primary-navigation"
                        aria-expanded={!collapsed}
                        title={collapsed ? "展开侧边栏" : "收起侧边栏"}
                        className="absolute right-3 top-0 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-transparent text-[#5f687b] transition-[color,background-color,border-color,box-shadow] duration-150 ease-out hover:border-[#dfe3eb] hover:bg-white hover:text-[#26324a] active:bg-[#e9ecf2] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none"
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
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        aria-current={activePage === item.id && item.id !== "data" ? "page" : undefined}
                        className={cn(
                          "flex h-11 w-max min-w-32 shrink-0 items-center gap-3 overflow-hidden rounded-xl border border-transparent bg-transparent px-3 text-left text-sm font-medium text-[#647089] transition-[color,background-color,border-color,box-shadow,padding] duration-[240ms] ease-[cubic-bezier(0.2,0,0,1)] hover:bg-white/75 hover:text-[#25324b] active:bg-[#e8ebf2] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none md:w-full md:min-w-0 md:justify-start",
                          collapsed && "md:pl-[19px] md:pr-0",
                          activePage === item.id &&
                            "border-[#e3e7f0] bg-white text-[#3156c8] shadow-[0_4px_14px_rgba(40,58,112,0.06)] hover:bg-white hover:text-[#2849ad] active:bg-[#f8f9fc]",
                        )}
                        onClick={() => navigate(item.path)}
                      >
                        <item.icon className={cn("h-[18px] w-[18px] shrink-0", activePage === item.id && "text-[#3156c8]")} aria-hidden="true" />
                        <span
                          className={cn(
                            "min-w-0 truncate transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none",
                            collapsed && "md:pointer-events-none md:-translate-x-1 md:opacity-0",
                          )}
                        >
                          {item.label}
                        </span>
                      </button>
                    </TooltipTrigger>
                    {collapsed ? <TooltipContent side="right">{item.label}</TooltipContent> : null}
                  </Tooltip>
                  {item.id === "data" ? (
                    <ul
                      aria-label="数据管理子页面"
                      className={cn(
                        "mt-1 flex gap-1 md:ml-5 md:flex-col md:border-l md:border-[#dfe3eb] md:pl-3",
                        collapsed && "md:hidden",
                      )}
                    >
                      {dataManagementSubpages.map((subpage) => {
                        const isActive = location.pathname === subpage.path;
                        return (
                          <li key={subpage.path}>
                            <button
                              type="button"
                              aria-current={isActive ? "page" : undefined}
                              className={cn(
                                "flex h-8 w-max min-w-24 items-center rounded-lg px-3 text-left text-xs font-medium text-[#748097] transition-[color,background-color] duration-150 hover:bg-white/75 hover:text-[#25324b] active:bg-[#e8ebf2] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#3156c8] motion-reduce:transition-none md:w-full md:min-w-0",
                                isActive && "bg-[#e9eefc] text-[#3156c8] hover:bg-[#e9eefc] hover:text-[#2849ad]",
                              )}
                              onClick={() => navigate(subpage.path)}
                            >
                              {subpage.label}
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          </TooltipProvider>
        </nav>

        <TooltipProvider delayDuration={180}>
          <Tooltip>
            <TooltipTrigger asChild>
              <div
                className={cn(
                  "hidden min-h-20 items-center gap-3 border-t border-[#e4e7ee] px-3 py-4 md:flex md:transition-[padding] md:duration-[240ms] md:ease-[cubic-bezier(0.2,0,0,1)] motion-reduce:transition-none",
                  collapsed && "md:px-5",
                )}
                role="group"
                aria-label="当前用户：演示用户，数据闭环操作员"
              >
                <Avatar className="h-10 w-10 border border-[#d8deea] bg-white text-[#3156c8] shadow-[0_3px_10px_rgba(30,41,59,0.05)]">
                  <AvatarFallback className="bg-white text-[#3156c8]">
                    <CircleUserRound className="h-5 w-5" aria-hidden="true" />
                  </AvatarFallback>
                </Avatar>
                <div
                  className={cn(
                    "min-w-0 transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none",
                    collapsed && "md:pointer-events-none md:-translate-x-1 md:opacity-0",
                  )}
                >
                  <div className="truncate text-sm font-semibold text-[#242c3b]">演示用户</div>
                  <div className="mt-0.5 truncate text-xs text-[#616a7c]">数据闭环操作员</div>
                </div>
              </div>
            </TooltipTrigger>
            {collapsed ? <TooltipContent side="right">演示用户 · 数据闭环操作员</TooltipContent> : null}
          </Tooltip>
        </TooltipProvider>
      </div>
    </aside>
  );
}
