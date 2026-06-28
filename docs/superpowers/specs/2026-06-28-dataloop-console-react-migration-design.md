# DataLoop Console React Migration Design

## Goal

Rebuild the current Web shell so it faithfully migrates the full `data_loop_v1.1.html` console into the React frontend, while preserving the existing DataPilot floating chat as the only real main-Agent entry point.

The target is not a new visual skin for the current simplified `AppShell`. The target is the whole DataLoop concept console from `data_loop_v1.1.html`: its sections, placeholder content, tabs, cards, tables, modals, charts, canvas effects, and simulated interactions should become maintainable React components.

## Current Context

The repository already has a working frontend under `frontend/` and a backend Web layer under `src/vla_data_juicer_agents/web/`. DataPilot currently supports:

- a floating button and chat window
- draft new-session flow
- recent session history
- REST turn submission
- WebSocket event streaming
- interruption of the current turn
- assistant streaming without duplicate user messages

The current `frontend/src/app/AppShell.tsx` is a simplified concept dashboard. It captures some dark-console styling, but it does not contain the full product structure of `data_loop_v1.1.html`.

The reference HTML is a single-file prototype. It includes global DOM scripts, repeated IDs, CDN-only dependencies, and simulated data. It should remain a reference source, not production frontend code.

## Product Boundary

The first migration keeps a strict boundary:

- The DataLoop console is a concept/control surface with rich placeholder content.
- DataPilot is the only real backend-connected Agent interface.
- DataPilot opens only from the persistent floating button.
- Task-oriented console buttons do not open DataPilot in this iteration.
- Task-oriented console buttons do not call backend APIs in this iteration.

Task-oriented buttons include:

- `启动标注`
- `执行流程`
- `启动仿真`
- `新建训练`
- `上传数据`
- `批量解锁`
- export/save/view-detail style actions

These controls should preserve their visual affordance, hover/focus/pressed states, and layout position. They may be inert or show restrained unavailable/placeholder feedback, but they must not imply that a real processing task has started. In particular, they should not display success messages such as `任务已启动` unless a real backend action exists.

## Source Page Inventory

`data_loop_v1.1.html` contains these major areas:

- Global shell: left sidebar, top search/status bar, background particle canvas, toast layer, upload modal.
- Dashboard: metric cards, data-type distribution chart, closed-loop process visualization, model metric chart tabs, activity feed.
- Data management: image, point cloud, text, and data-unlock tabs; filters; image grid; point cloud canvas previews; text instruction list; quality summary; unlock rules; batch table.
- Annotation: pipeline nodes, confidence/type charts, annotation result list, manual review cards.
- Model iteration: version timeline, deployment summary, training resource panel, training charts, version comparison chart.
- Agent workflow: node palette, drag/drop canvas, connection canvas, selected-node property panel, execution highlight animation.
- Simulation: configuration form, metric checkboxes, test-case library, resource panel, running-monitor charts/logs, result charts, report table.

The HTML also includes dynamic effects:

- glowing status dots
- card hover elevation
- running pipeline border animation
- modal entrance animation
- floating process nodes
- animated closed-loop lines
- background particles
- point cloud preview drawing
- Agent workflow connection drawing
- simple simulated progress updates
- tab switching and lazy chart initialization

## Recommended Architecture

Keep frontend and backend separated. This task should mainly affect `frontend/`.

Recommended frontend structure:

```text
frontend/src/
  app/
    App.tsx
    AppShell.tsx
  components/
    console/
      ConsoleCard.tsx
      ConsoleHeader.tsx
      ConsoleSidebar.tsx
      ConsoleToast.tsx
      MetricCard.tsx
      ProgressBar.tsx
      QualityRing.tsx
      SegmentedTabs.tsx
      StatusTag.tsx
    datapilot/
      ...
  features/
    console/
      consoleFixtures.ts
      pages/
        DashboardPage.tsx
        DataManagementPage.tsx
        AnnotationPage.tsx
        ModelIterationPage.tsx
        AgentWorkflowPage.tsx
        SimulationPage.tsx
      visuals/
        BackgroundParticles.tsx
        LoopFlowCanvas.tsx
        PointCloudPreview.tsx
        AgentConnectionCanvas.tsx
        MiniChart.tsx
  styles/
    globals.css
```

The exact file names can change during implementation, but the migration should avoid one large `AppShell.tsx`. The shell owns navigation and layout; page components own page-local state; shared console components own reusable styling.

## Visual And Interaction Rules

The migrated React console should match the reference page's dense dark operational-console feel:

- dark blue-black background
- cyan/green primary accents
- amber/red status accents
- compact cards, small typography, data-heavy panels
- subtle glow and canvas motion
- restrained hover states
- fixed application shell with scrollable main content

Use lucide icons instead of Font Awesome where practical. Do not add a marketing hero page. Do not replace operational screens with landing-page sections.

External CDN dependencies from the HTML should not be copied directly into production code:

- Tailwind already exists locally.
- Font Awesome should be replaced by lucide icons.
- Google Fonts can be replaced with system fonts or added through the frontend build only if needed.
- Chart.js can be added as a frontend dependency only if implementation chooses real chart rendering. Lightweight SVG/canvas chart components are also acceptable if they preserve the reference effect.
- Picsum image placeholders may remain for concept data if network availability is acceptable for development, but locally generated visual placeholders are preferable for reliability.

## State And Data Flow

Console state is frontend-local for this iteration:

- active page
- active tab within each page
- local range slider values
- selected Agent workflow node
- modal open state
- placeholder toast state
- simulated progress state where needed

DataPilot state remains in the existing Zustand store and backend API client. The console should not couple directly to DataPilot internals except rendering `DataPilotButton` and `DataPilotWindow` as overlay children.

Mock data should live outside component JSX in fixtures or small page-local modules. This includes metric values, data cards, batch rows, version records, activities, annotation results, and simulation reports.

## Overlay And Layout Constraints

The reference HTML uses global overlay layers that would conflict with DataPilot if copied as-is. The React migration should define clear layering:

- base particles below the shell
- shell and page content above particles
- console modal/toast above content
- DataPilot floating button/window above ordinary console UI

Console toast should avoid the bottom-right area occupied by DataPilot. A left-bottom toast or a top-right toast inside the console shell is safer.

Do not copy `body { overflow: hidden }` blindly. The app should keep a full-height shell, but main content and long tables must remain scrollable.

## Known Reference Cleanup

The HTML contains prototype artifacts that should be cleaned up during migration:

- repeated IDs such as `batch-table` and `chart-loss`
- a navigation-less `page-unlock` duplicate
- a misplaced or duplicated `model-view-training` block inside annotation content
- global functions that assume direct DOM ownership
- chart/canvas loops without cleanup

React components should use refs, local state, and `useEffect` cleanup instead of global DOM queries and global IDs.

## Testing Strategy

Update or add frontend tests for:

- default render shows the full DataLoop console shell and Dashboard.
- sidebar navigation switches between Dashboard, Agent workflow, Data management, Annotation, Model iteration, and Simulation.
- page-local tabs switch visible panels.
- task-oriented placeholder buttons do not call DataPilot APIs and do not open the DataPilot window.
- DataPilot floating button still opens the chat window.
- existing DataPilot session, history, streaming, and interruption tests continue passing.
- upload modal opens/closes if retained as a placeholder.
- long content remains scrollable without hiding DataPilot.

Run:

```bash
cd frontend
npm test
npm run build
```

If Playwright browsers are available, also run:

```bash
cd frontend
npm run e2e
```

## Implementation Notes

This should be implemented with subagent-driven development after the design is approved. Good work slices are:

1. console shell, sidebar, top bar, shared styling primitives
2. dashboard and shared chart/canvas primitives
3. data management and annotation pages
4. model iteration, Agent workflow, and simulation pages
5. tests, responsive polish, and visual verification

DataPilot components should remain in `frontend/src/components/datapilot/`. The migration should not move backend Web API code unless a test reveals a true integration issue.
