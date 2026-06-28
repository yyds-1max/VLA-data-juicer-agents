# DataLoop Console React Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the full `data_loop_v1.1.html` concept console into the React frontend while keeping DataPilot as the only real backend-connected Agent entry point.

**Architecture:** Replace the simplified `AppShell` with a componentized DataLoop console shell. Keep all concept-console data and interactions frontend-local, split shared primitives from page components, and render the existing DataPilot overlay independently above the console. Avoid copying the single-file HTML's global DOM scripts; use React state, refs, and effect cleanup.

**Tech Stack:** React 19, TypeScript, Vite, Tailwind CSS, Zustand, lucide-react, Vitest, Testing Library, Playwright when available.

---

## File Structure

Create or modify these frontend files:

```text
frontend/src/app/
  App.tsx                         # Keep DataPilot overlay mounted over the console shell
  AppShell.tsx                    # Replace simplified dashboard with routed DataLoop console shell
  App.test.tsx                    # Update shell/navigation/placeholder/DataPilot tests

frontend/src/components/console/
  ConsoleButton.tsx               # Shared button styling; supports inert placeholder actions
  ConsoleCard.tsx                 # Shared card container
  ConsoleHeader.tsx               # Top search/status header
  ConsoleSidebar.tsx              # Left navigation
  ConsoleToast.tsx                # Lightweight placeholder feedback away from DataPilot
  MetricCard.tsx                  # Dashboard metric card
  ProgressBar.tsx                 # Compact progress bars
  QualityRing.tsx                 # SVG circular score indicator
  SegmentedTabs.tsx               # Page-local tabs
  StatusTag.tsx                   # Compact colored status tags

frontend/src/features/console/
  consoleFixtures.ts              # Mock metrics, data rows, versions, reports, nodes
  consoleTypes.ts                 # Shared console page/tab/status types
  pages/
    AnnotationPage.tsx
    AgentWorkflowPage.tsx
    DashboardPage.tsx
    DataManagementPage.tsx
    ModelIterationPage.tsx
    SimulationPage.tsx
  visuals/
    AgentConnectionCanvas.tsx
    BackgroundParticles.tsx
    LoopFlowCanvas.tsx
    MiniChart.tsx
    PointCloudPreview.tsx

frontend/src/styles/globals.css   # Add console animations and scrollbar details only
```

Do not modify backend Web API files for this migration unless tests reveal a direct integration regression. Keep `frontend/src/components/datapilot/` in place.

## Non-Negotiable Boundaries

- `DataPilotButton` is the only way to open DataPilot.
- Console task buttons do not open DataPilot.
- Console task buttons do not submit backend turns.
- Console task buttons do not show success copy that implies real execution has happened.
- `data_loop_v1.1.html` remains a reference file and is not imported by the app.
- Do not introduce Chart.js unless the implementation proves the local SVG/canvas chart primitives cannot cover the visual need.
- Keep `.djx/` untracked and untouched.

## Task 1: Add Console Types, Fixtures, And Shared Primitives

**Files:**
- Create: `frontend/src/features/console/consoleTypes.ts`
- Create: `frontend/src/features/console/consoleFixtures.ts`
- Create: `frontend/src/components/console/ConsoleCard.tsx`
- Create: `frontend/src/components/console/ConsoleButton.tsx`
- Create: `frontend/src/components/console/StatusTag.tsx`
- Create: `frontend/src/components/console/ProgressBar.tsx`
- Create: `frontend/src/components/console/QualityRing.tsx`
- Create: `frontend/src/components/console/SegmentedTabs.tsx`
- Create: `frontend/src/components/console/MetricCard.tsx`
- Modify: `frontend/src/styles/globals.css`
- Test: `frontend/src/components/console/consolePrimitives.test.tsx`

- [ ] **Step 1: Write failing shared primitive tests**

Create `frontend/src/components/console/consolePrimitives.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { ConsoleButton } from "./ConsoleButton";
import { ProgressBar } from "./ProgressBar";
import { SegmentedTabs } from "./SegmentedTabs";
import { StatusTag } from "./StatusTag";

describe("console primitives", () => {
  test("ConsoleButton renders a real button and delegates clicks to the caller", () => {
    const onClick = vi.fn();

    render(<ConsoleButton onClick={onClick}>启动标注</ConsoleButton>);
    fireEvent.click(screen.getByRole("button", { name: "启动标注" }));

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  test("SegmentedTabs exposes accessible tabs", () => {
    const onChange = vi.fn();

    function ControlledTabs() {
      const [value, setValue] = useState("image");

      return (
        <SegmentedTabs
          value={value}
          tabs={[
            { id: "image", label: "图像数据" },
            { id: "pointcloud", label: "点云数据" },
          ]}
          onChange={(nextValue) => {
            onChange(nextValue);
            setValue(nextValue);
          }}
        />
      );
    }

    render(<ControlledTabs />);

    fireEvent.click(screen.getByRole("tab", { name: "点云数据" }));

    expect(screen.getByRole("tab", { name: "点云数据" })).toHaveAttribute("aria-selected", "true");
    expect(onChange).toHaveBeenCalledWith("pointcloud");
  });

  test("StatusTag and ProgressBar render readable status details", () => {
    render(
      <>
        <StatusTag tone="success">已解锁</StatusTag>
        <ProgressBar value={73} tone="info" label="进行中 73%" />
      </>,
    );

    expect(screen.getByText("已解锁")).toBeVisible();
    expect(screen.getByText("进行中 73%")).toBeVisible();
  });
});
```

Run:

```bash
cd frontend
npm test -- src/components/console/consolePrimitives.test.tsx
```

Expected: FAIL because the console primitives do not exist yet.

- [ ] **Step 2: Create shared console types**

Create `frontend/src/features/console/consoleTypes.ts`:

```ts
import type { LucideIcon } from "lucide-react";

export type ConsolePageId = "dashboard" | "agent" | "data" | "annotate" | "model" | "simulation";

export type Accent = "accent" | "accent2" | "warn" | "danger" | "purple" | "muted";

export type NavItem = {
  id: ConsolePageId;
  label: string;
  group: string;
  icon: LucideIcon;
};

export type TabItem<T extends string> = {
  id: T;
  label: string;
};

export type StatusTone = "success" | "info" | "warning" | "danger" | "neutral" | "purple";
```

Map semantic tones to existing Tailwind tokens where possible: use `console-cyan`, `console-muted`, `console-panel`, `console-panel2`, and standard Tailwind colors such as `amber-300`, `rose-400`, and `violet-400`. Do not assume a `console-card`, `accent`, or `accent2` color token exists unless the Tailwind config is updated in the same task.

- [ ] **Step 3: Create fixtures with stable IDs**

Create `frontend/src/features/console/consoleFixtures.ts` with deterministic data. Use stable IDs such as `IMG-000`, `PCD-000`, `TXT-001`, `B-2026-001`, and `TC-001` so tests can assert exact text. Include arrays for:

Named exports required in this file:

- `dashboardMetrics`
- `dataDistribution`
- `modelCurveSuccess`
- `modelCurveLoss`
- `activityFeed`
- `imageData`
- `pointCloudData`
- `textInstructionData`
- `batchData`
- `annotationResults`
- `modelVersions`
- `agentNodes`
- `agentConnections`
- `simulationReportRows`

Use Chinese labels from `data_loop_v1.1.html`. Do not use `Date.now()` or `Math.random()` in fixtures.

- [ ] **Step 4: Add shared components**

Implement these components with focused props:

```tsx
// ConsoleCard.tsx
export function ConsoleCard({ children, className }: { children: React.ReactNode; className?: string }) {
  return <section className={cn("rounded-xl border border-console-line bg-console-panel p-4 shadow-[0_16px_40px_rgba(0,0,0,0.2)]", className)}>{children}</section>;
}
```

```tsx
// ConsoleButton.tsx
type ConsoleButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "tab";
};

export function ConsoleButton({ variant = "ghost", className, ...props }: ConsoleButtonProps) {
  return <button type="button" className={cn(buttonClassForVariant(variant), className)} {...props} />;
}
```

`ConsoleButton` must not contain DataPilot logic. Placeholder behavior belongs to the page that uses it.

Implement `StatusTag`, `ProgressBar`, `QualityRing`, `SegmentedTabs`, and `MetricCard` as presentational components using existing `cn` from `frontend/src/lib/utils.ts`.

`SegmentedTabs` must be controlled: use the `value` prop to determine `aria-selected` and visual active state, and have tab clicks call only `onChange(tab.id)`. Do not keep internal selected-tab state in the component.

- [ ] **Step 5: Add animation and utility CSS**

Modify `frontend/src/styles/globals.css` to add only reusable CSS:

```css
@keyframes console-pulse-glow {
  0%, 100% { box-shadow: 0 0 4px currentColor; opacity: 1; }
  50% { box-shadow: 0 0 14px currentColor; opacity: 0.72; }
}

@keyframes console-border-flow {
  0% { clip-path: inset(0 100% 0 0); }
  100% { clip-path: inset(0 0 0 0); }
}

@keyframes console-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
```

Do not set `body { overflow: hidden }`.

- [ ] **Step 6: Run shared tests**

Run:

```bash
cd frontend
npm test -- src/components/console/consolePrimitives.test.tsx
```

Expected: primitive tests pass.

- [ ] **Step 7: Commit shared foundation**

```bash
git add frontend/src/components/console frontend/src/features/console/consoleTypes.ts frontend/src/features/console/consoleFixtures.ts frontend/src/styles/globals.css
git commit -m "feat: add DataLoop console primitives"
```

## Task 2: Replace AppShell With Full Console Shell

**Files:**
- Modify: `frontend/src/app/App.tsx`
- Replace: `frontend/src/app/AppShell.tsx`
- Create: `frontend/src/components/console/ConsoleSidebar.tsx`
- Create: `frontend/src/components/console/ConsoleHeader.tsx`
- Create: `frontend/src/components/console/ConsoleToast.tsx`
- Create: `frontend/src/features/console/visuals/BackgroundParticles.tsx`
- Test: `frontend/src/app/App.test.tsx`

- [ ] **Step 1: Write shell navigation tests**

Add tests:

```tsx
test("renders the full DataLoop console shell by default", () => {
  render(<App />);

  expect(screen.getByText("智瀚星途 DataLoop")).toBeVisible();
  expect(screen.getByText("Voyager Forge")).toBeVisible();
  expect(screen.getByRole("heading", { name: "闭环仪表盘" })).toBeVisible();
  expect(screen.getByPlaceholderText("搜索数据、模型、任务...")).toBeVisible();
});

test("sidebar navigation switches console pages", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Agent 工作流" }));
  expect(screen.getByRole("heading", { name: "Agent 工作流" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  expect(screen.getByRole("heading", { name: "测试/仿真" })).toBeVisible();
});
```

Run:

```bash
cd frontend
npm test -- src/app/App.test.tsx
```

Expected: FAIL until shell is implemented.

- [ ] **Step 2: Implement `ConsoleSidebar`**

Create sidebar with grouped nav matching the HTML:

```tsx
const navItems = [
  { id: "dashboard", group: "概览", label: "闭环仪表盘", icon: ChartLine },
  { id: "agent", group: "流程", label: "Agent 工作流", icon: Workflow },
  { id: "data", group: "数据", label: "数据管理", icon: Database },
  { id: "annotate", group: "标注", label: "自动标注", icon: Tags },
  { id: "model", group: "模型", label: "模型迭代", icon: Brain },
  { id: "simulation", group: "验证", label: "测试/仿真", icon: FlaskConical },
] satisfies NavItem[];
```

Render each nav entry as a real `<button>` with `aria-current={active ? "page" : undefined}` and `onClick={() => onChange(item.id)}`.

- [ ] **Step 3: Implement `ConsoleHeader`**

Create top bar with:

- page title
- `v2.4.1` badge
- search input placeholder `搜索数据、模型、任务...`
- notification icon with red dot
- cluster status `系统在线`

The search input is presentational for now and must not call backend APIs.

- [ ] **Step 4: Implement `ConsoleToast`**

Create a small toast component that renders in the top-right or bottom-left. It should accept:

```ts
type ConsoleToastState = { message: string; tone: StatusTone } | null;
```

Use it only for neutral placeholder feedback such as `该功能暂未接入后端`. Do not show task-success copy.

- [ ] **Step 5: Implement `BackgroundParticles`**

Create a canvas component with `requestAnimationFrame` and cleanup:

```tsx
useEffect(() => {
  let frame = 0;
  let animationId = requestAnimationFrame(draw);
  return () => {
    cancelAnimationFrame(animationId);
    window.removeEventListener("resize", resize);
  };
}, []);
```

Particles must be decorative with `aria-hidden="true"` and `pointer-events-none`.

- [ ] **Step 6: Replace `AppShell`**

`AppShell` should own:

- `activePage`
- optional toast state
- `showPlaceholderToast(message?: string)`
- page rendering switch

Render `children` after page content so `DataPilotButton` and `DataPilotWindow` stay mounted over the shell:

```tsx
<div className="min-h-screen bg-console-bg text-console-text">
  <BackgroundParticles />
  <ConsoleSidebar activePage={activePage} onChange={setActivePage} />
  <main>{renderActivePage()}</main>
  {children}
</div>
```

- [ ] **Step 7: Run shell tests**

Run:

```bash
cd frontend
npm test -- src/app/App.test.tsx
npm run build
```

Expected: shell tests pass; page-content tests from subsequent page tasks may still fail if those tests have already been added.

- [ ] **Step 8: Commit shell migration**

```bash
git add frontend/src/app/App.tsx frontend/src/app/AppShell.tsx frontend/src/components/console frontend/src/features/console/visuals/BackgroundParticles.tsx frontend/src/app/App.test.tsx
git commit -m "feat: migrate DataLoop console shell"
```

## Task 3: Implement Dashboard Page And Core Visuals

**Files:**
- Create: `frontend/src/features/console/pages/DashboardPage.tsx`
- Create: `frontend/src/features/console/visuals/MiniChart.tsx`
- Create: `frontend/src/features/console/visuals/LoopFlowCanvas.tsx`
- Modify: `frontend/src/app/AppShell.tsx`
- Test: `frontend/src/app/App.test.tsx`

- [ ] **Step 1: Write dashboard tests**

Add tests:

```tsx
test("dashboard preserves reference metric and activity content", () => {
  render(<App />);

  expect(screen.getByText("总数据量")).toBeVisible();
  expect(screen.getByText("284,729")).toBeVisible();
  expect(screen.getByText("数据类型分布")).toBeVisible();
  expect(screen.getByText("数据闭环流程")).toBeVisible();
  expect(screen.getByText("最近活动")).toBeVisible();
});

test("dashboard metric chart tabs switch between success and loss", () => {
  render(<App />);

  expect(screen.getByText("Success Rate (%)")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "损失值" }));
  expect(screen.getByText("Training Loss")).toBeVisible();
});
```

- [ ] **Step 2: Implement `MiniChart`**

Create a lightweight SVG chart primitive supporting:

- `type="donut"`
- `type="line"`
- `type="bar"`
- `type="radar"` is added in Task 5 before `ModelIterationPage` uses version comparison

Inputs are deterministic arrays from fixtures. Use `<svg role="img" aria-label={title}>`.

- [ ] **Step 3: Implement `LoopFlowCanvas`**

Render the five closed-loop nodes:

- 数据采集
- 自动标注
- 质量过滤
- 模型训练
- 部署验证

Use lucide icons and a canvas/ref effect for animated dashed connections. Add cleanup for animation frames and resize listeners.

- [ ] **Step 4: Implement `DashboardPage`**

Use `MetricCard`, `ConsoleCard`, `MiniChart`, `LoopFlowCanvas`, `SegmentedTabs`, and fixture data. Keep layout close to the reference:

- first row: four metric cards
- second row: data distribution and closed-loop process
- third row: model metric curve and recent activity

- [ ] **Step 5: Wire dashboard into `AppShell`**

Render `<DashboardPage />` for `activePage === "dashboard"`.

- [ ] **Step 6: Run dashboard tests**

Run:

```bash
cd frontend
npm test -- src/app/App.test.tsx
npm run build
```

Expected: dashboard tests pass.

- [ ] **Step 7: Commit dashboard**

```bash
git add frontend/src/features/console/pages/DashboardPage.tsx frontend/src/features/console/visuals/MiniChart.tsx frontend/src/features/console/visuals/LoopFlowCanvas.tsx frontend/src/app/AppShell.tsx frontend/src/app/App.test.tsx
git commit -m "feat: add DataLoop dashboard page"
```

## Task 4: Implement Data Management And Annotation Pages

**Files:**
- Create: `frontend/src/features/console/pages/DataManagementPage.tsx`
- Create: `frontend/src/features/console/pages/AnnotationPage.tsx`
- Create: `frontend/src/features/console/visuals/PointCloudPreview.tsx`
- Modify: `frontend/src/app/AppShell.tsx`
- Test: `frontend/src/app/App.test.tsx`

- [ ] **Step 1: Write data and annotation tests**

Add tests:

```tsx
test("data management tabs render image pointcloud text and unlock panels", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  expect(screen.getByText("IMG-000")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "点云数据" }));
  expect(screen.getByText("PCD-000")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "文本数据" }));
  expect(screen.getByText(/将红色杯子/)).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "数据解锁" }));
  expect(screen.getByText("解锁规则配置")).toBeVisible();
  expect(screen.getByText("批次管理")).toBeVisible();
});

test("console shared tabs switch visible panels without remounting DataPilot", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
  fireEvent.click(screen.getByRole("tab", { name: "点云数据" }));

  expect(screen.getByText("PCD-000")).toBeVisible();
  expect(screen.queryByText("IMG-000")).not.toBeVisible();
  expect(screen.getByRole("button", { name: "Open DataPilot" })).toBeVisible();
});

test("annotation page switches pipeline results and review views", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "自动标注" }));
  expect(screen.getByText("视觉检测")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "标注结果" }));
  expect(screen.getByText("ANN-82401")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "人工复核" }));
  expect(screen.getByText("待复核样本")).toBeVisible();
});
```

- [ ] **Step 2: Implement `PointCloudPreview`**

Use canvas drawing with a deterministic pseudo-random generator seeded from `id`, not `Math.random()`. Draw points on mount and redraw on resize if needed.

- [ ] **Step 3: Implement `DataManagementPage`**

Implement four tabs:

- `图像数据`: image cards using local CSS gradients or stable remote seed URLs
- `点云数据`: cards with `PointCloudPreview`
- `文本数据`: compact instruction rows
- `数据解锁`: quality cards, slider controls, batch table

Buttons such as `导出`, `上传数据`, `保存规则`, and `批量解锁选中批次` must call the local placeholder handler only. They must not open DataPilot.

- [ ] **Step 4: Implement `AnnotationPage`**

Implement three tabs:

- `标注流水线`: five pipeline nodes and two chart cards
- `标注结果`: list from fixtures
- `人工复核`: sample review and detail panels

`启动标注`, `通过`, `退回`, and `废弃` are placeholders. They must not call backend APIs.

- [ ] **Step 5: Wire pages into `AppShell`**

Render:

```tsx
case "data":
  return <DataManagementPage onPlaceholderAction={showPlaceholderToast} />;
case "annotate":
  return <AnnotationPage onPlaceholderAction={showPlaceholderToast} />;
```

- [ ] **Step 6: Run tests**

Run:

```bash
cd frontend
npm test -- src/app/App.test.tsx
npm run build
```

Expected: data and annotation tests pass.

- [ ] **Step 7: Commit pages**

```bash
git add frontend/src/features/console/pages/DataManagementPage.tsx frontend/src/features/console/pages/AnnotationPage.tsx frontend/src/features/console/visuals/PointCloudPreview.tsx frontend/src/app/AppShell.tsx frontend/src/app/App.test.tsx
git commit -m "feat: add DataLoop data and annotation pages"
```

## Task 5: Implement Model Iteration Page

**Files:**
- Create: `frontend/src/features/console/pages/ModelIterationPage.tsx`
- Modify: `frontend/src/features/console/visuals/MiniChart.tsx`
- Modify: `frontend/src/app/AppShell.tsx`
- Test: `frontend/src/app/App.test.tsx`

- [ ] **Step 1: Write model tests**

Add tests:

```tsx
test("model iteration page renders versions training and compare tabs", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "模型迭代" }));
  expect(screen.getByText("v47")).toBeVisible();
  expect(screen.getByText("当前部署")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "训练监控" }));
  expect(screen.getByText("训练损失曲线")).toBeVisible();
  expect(screen.getByText("GPU 监控 (实时)")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "版本对比" }));
  expect(screen.getByText("版本性能对比")).toBeVisible();
});
```

- [ ] **Step 2: Add radar support to `MiniChart`**

Add a simple SVG radar mode for version comparison. Use labels and radial polygons. No external chart dependency is required.

- [ ] **Step 3: Implement `ModelIterationPage`**

Implement:

- `版本时间线`
- deployment summary
- training resource progress panel
- `训练监控`
- `版本对比`

`新建训练` is a placeholder button and must not open DataPilot.

- [ ] **Step 4: Wire model page into `AppShell`**

Render `<ModelIterationPage onPlaceholderAction={showPlaceholderToast} />` for `activePage === "model"`.

- [ ] **Step 5: Run tests**

Run:

```bash
cd frontend
npm test -- src/app/App.test.tsx
npm run build
```

Expected: model tests pass.

- [ ] **Step 6: Commit model page**

```bash
git add frontend/src/features/console/pages/ModelIterationPage.tsx frontend/src/features/console/visuals/MiniChart.tsx frontend/src/app/AppShell.tsx frontend/src/app/App.test.tsx
git commit -m "feat: add DataLoop model iteration page"
```

## Task 6: Implement Agent Workflow And Simulation Pages

**Files:**
- Create: `frontend/src/features/console/pages/AgentWorkflowPage.tsx`
- Create: `frontend/src/features/console/pages/SimulationPage.tsx`
- Create: `frontend/src/features/console/visuals/AgentConnectionCanvas.tsx`
- Modify: `frontend/src/app/AppShell.tsx`
- Test: `frontend/src/app/App.test.tsx`

- [ ] **Step 1: Write workflow and simulation tests**

Add tests:

```tsx
test("agent workflow page selects nodes and keeps execute action placeholder-only", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Agent 工作流" }));
  expect(screen.getByText("节点库")).toBeVisible();
  expect(screen.getByText("工作流画布")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "数据源接入" }));
  expect(screen.getByText("从多个数据源拉取原始数据")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "执行流程" }));
  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();
});

test("simulation page switches config running and results views", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "测试/仿真" }));
  expect(screen.getByText("仿真场景配置")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "运行监控" }));
  expect(screen.getByText("实时任务日志")).toBeVisible();

  fireEvent.click(screen.getByRole("tab", { name: "测试结果" }));
  expect(screen.getByText("详细测试报告")).toBeVisible();
});
```

- [ ] **Step 2: Implement `AgentConnectionCanvas`**

Draw connection curves from fixed node coordinates in fixtures. Cleanup resize listeners and animation frames.

- [ ] **Step 3: Implement `AgentWorkflowPage`**

Include:

- draggable-looking node palette
- fixed workflow canvas with nodes
- connection canvas
- selected node property panel

Drag/drop can be visually represented but does not need to create durable workflow state in this iteration. `保存流程` and `执行流程` are placeholder-only.

- [ ] **Step 4: Implement `SimulationPage`**

Include:

- config form
- metric checkboxes
- test-case library
- resource panel
- running monitor
- result summary
- chart cards
- report table

`启动仿真`, `导出PDF`, and `导出CSV` are placeholder-only.

- [ ] **Step 5: Wire pages into `AppShell`**

Render:

```tsx
case "agent":
  return <AgentWorkflowPage onPlaceholderAction={showPlaceholderToast} />;
case "simulation":
  return <SimulationPage onPlaceholderAction={showPlaceholderToast} />;
```

- [ ] **Step 6: Run tests**

Run:

```bash
cd frontend
npm test -- src/app/App.test.tsx
npm run build
```

Expected: workflow and simulation tests pass.

- [ ] **Step 7: Commit pages**

```bash
git add frontend/src/features/console/pages/AgentWorkflowPage.tsx frontend/src/features/console/pages/SimulationPage.tsx frontend/src/features/console/visuals/AgentConnectionCanvas.tsx frontend/src/app/AppShell.tsx frontend/src/app/App.test.tsx
git commit -m "feat: add DataLoop workflow and simulation pages"
```

## Task 7: Preserve DataPilot Integration And Polish Responsive Layout

**Files:**
- Modify: `frontend/src/components/datapilot/DataPilotButton.tsx`
- Modify: `frontend/src/components/datapilot/DataPilotWindow.tsx`
- Modify: `frontend/src/app/App.test.tsx`
- Modify: `frontend/tests/datapilot.spec.ts`

- [ ] **Step 1: Write regression tests for DataPilot overlay**

Add or update tests:

```tsx
test("DataPilot opens only from the floating button after console migration", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "启动仿真" }));
  expect(screen.queryByRole("dialog", { name: "DataPilot" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));
  expect(screen.getByRole("dialog", { name: "DataPilot" })).toBeVisible();
});

test("DataPilot window remains above the console content", () => {
  render(<App />);

  fireEvent.click(screen.getByRole("button", { name: "Open DataPilot" }));

  const dialog = screen.getByRole("dialog", { name: "DataPilot" });
  expect(dialog.className).toContain("fixed");
  expect(dialog.className).toContain("z-");
});
```

- [ ] **Step 2: Adjust DataPilot z-index if needed**

If console modal/toast layers use higher z-index than the current `z-40`, raise DataPilot to a named class such as `z-[80]`. Keep the button and window above ordinary console UI.

- [ ] **Step 3: Update Playwright smoke test**

Update `frontend/tests/datapilot.spec.ts` so it verifies:

- default console shell visible
- DataPilot button visible
- clicking DataPilot button opens draft window
- console navigation still works after closing the window

- [ ] **Step 4: Run frontend unit and build verification**

Run:

```bash
cd frontend
npm test
npm run build
```

Expected: all unit tests pass and Vite build succeeds.

- [ ] **Step 5: Run Playwright if browser is installed**

Run:

```bash
cd frontend
npm run e2e
```

Expected: E2E passes. If the browser executable is missing, record the exact Playwright install message and do not claim E2E passed.

- [ ] **Step 6: Commit integration polish**

```bash
git add frontend/src/components/datapilot/DataPilotButton.tsx frontend/src/components/datapilot/DataPilotWindow.tsx frontend/src/app/App.test.tsx frontend/tests/datapilot.spec.ts
git commit -m "test: preserve DataPilot over migrated console"
```

## Task 8: Final Verification And Branch Review

**Files:**
- No planned source changes unless verification reveals a defect.

- [ ] **Step 1: Check worktree status**

Run:

```bash
git status --short --branch
```

Expected: only intentional files are changed. `.djx/` may remain untracked and should not be staged.

- [ ] **Step 2: Run full backend and frontend verification**

Run:

```bash
pytest -q
cd frontend
npm test
npm run build
```

Expected: backend tests pass, frontend tests pass, frontend build succeeds.

- [ ] **Step 3: Run E2E when available**

Run:

```bash
cd frontend
npm run e2e
```

Expected: passes when Playwright browsers are installed. If not installed, report that E2E was not run and include the install command.

- [ ] **Step 4: Review diff**

Run:

```bash
git diff --stat main...HEAD
git diff -- frontend/src/app frontend/src/components/console frontend/src/features/console frontend/src/components/datapilot frontend/tests
```

Expected:

- console code is under `components/console` and `features/console`
- DataPilot code remains isolated
- no backend code changed without reason
- no generated `.djx/` files staged

- [ ] **Step 5: Prepare final summary**

Summarize:

- pages migrated
- placeholder-button behavior
- DataPilot preservation
- tests run and any tests not run
- remaining limitations

Do not claim production backend support for console buttons.
