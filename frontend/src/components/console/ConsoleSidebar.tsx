import { Bot, ChartNoAxesCombined, Database, FlaskConical, GitBranch, PenTool, Route } from "lucide-react";

import type { ConsolePageId, NavItem } from "../../features/console/consoleTypes";
import { cn } from "../../lib/utils";

type ConsoleSidebarProps = {
  activePage: ConsolePageId;
  onChange: (pageId: ConsolePageId) => void;
};

const navItems: NavItem[] = [
  { id: "dashboard", label: "闭环仪表盘", group: "概览", icon: ChartNoAxesCombined },
  { id: "agent", label: "Agent 工作流", group: "流程", icon: GitBranch },
  { id: "data", label: "数据管理", group: "数据", icon: Database },
  { id: "annotate", label: "自动标注", group: "标注", icon: PenTool },
  { id: "model", label: "模型迭代", group: "模型", icon: Bot },
  { id: "simulation", label: "测试/仿真", group: "验证", icon: FlaskConical },
];

export function ConsoleSidebar({ activePage, onChange }: ConsoleSidebarProps) {
  return (
    <aside className="fixed inset-x-0 top-0 z-20 border-b border-console-line bg-console-panel/95 px-4 backdrop-blur md:inset-y-0 md:left-0 md:right-auto md:w-64 md:border-b-0 md:border-r md:px-0">
      <div className="flex h-full flex-col">
        <div className="flex h-16 items-center gap-3 md:h-auto md:border-b md:border-console-line md:p-5">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded border border-console-cyan/40 bg-console-cyan/10 text-console-cyan shadow-[0_0_24px_rgba(21,209,216,0.16)]">
            <Route className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">智瀚星途 DataLoop</div>
            <div className="truncate text-[11px] uppercase text-console-muted">Voyager Forge</div>
          </div>
        </div>

        <nav className="min-w-0 flex-1 overflow-x-auto border-t border-console-line py-2 md:overflow-y-auto md:border-t-0 md:px-3 md:py-4" aria-label="DataLoop console navigation">
          <ul className="flex gap-2 md:block md:space-y-3">
            {navItems.map((item) => (
              <li key={item.id} className="md:space-y-1">
                <div className="hidden px-3 text-[10px] uppercase tracking-[0.18em] text-console-muted md:block">{item.group}</div>
                <button
                  type="button"
                  aria-current={activePage === item.id ? "page" : undefined}
                  className={cn(
                    "flex h-10 w-max min-w-32 shrink-0 items-center gap-2 rounded border border-console-line bg-console-panel2 px-3 text-left text-sm text-console-muted transition hover:border-console-cyan/40 hover:text-console-cyan focus:outline-none focus:ring-2 focus:ring-console-cyan md:w-full",
                    activePage === item.id &&
                      "border-console-cyan/45 bg-console-cyan/10 text-console-cyan shadow-[inset_3px_0_0_#15d1d8]",
                  )}
                  onClick={() => onChange(item.id)}
                >
                  <item.icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <div className="hidden border-t border-console-line p-4 md:block">
          <div className="rounded border border-console-line bg-console-panel2 px-3 py-2">
            <div className="text-sm font-medium">数据飞轮</div>
            <div className="mt-1 text-xs text-console-muted">Pro 版本</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
