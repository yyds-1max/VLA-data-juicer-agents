# 自动标注 M1.5：Tailwind 4 与 Radix/shadcn 设计系统基线

> 状态：服务器功能验收通过；完整冻结因既有公开路径泄漏暂缓
> 开始日期：2026-07-27
> 开发基线：`a7315ca`
> 开发分支：`codex/automatic-annotation-m1-5`
> 上游里程碑：M1 Web 首帧标注与 Tracking 功能冻结

## 1. 目标与边界

M1.5 是 M1 与 M2 之间独立的前端基础设施迁移：

```text
固定 Node/npm 契约
→ Tailwind 3 迁移到 Tailwind 4
→ 以 Radix 为 shadcn primitive 基础
→ 建立受审查的 components/ui
→ 建立路由懒加载边界
→ 全站视觉、交互、响应式和可访问性回归
→ 冻结设计系统基线
```

本里程碑不得修改：

- Annotation Store、API、数据库 migration、状态机和 Runtime；
- 首帧标注、CAS、revision、Tracking、取消和恢复的业务语义；
- Router、NavigationDataAgent、DataPilot 单一智能体契约；
- M1 Runtime、Legacy YAML、Golden 或服务器业务数据；
- M2 的 gridmap、投影、轨迹、Fix 和三维复核；
- M3 数据管理联动、智能体 durable handoff；
- M4 AI 审核；
- 现有品牌色、页面信息架构和主要视觉密度。

M1.5 不重新运行真实 Tracking。服务器只做确定性依赖安装、生产构建、页面和
只读 API smoke。

## 2. 当前基线与已知风险

当前前端基线：

- Tailwind `3.4.19`；
- Vite `6.4.3`；
- React `19.2.7`；
- TypeScript `5.9.3`；
- PostCSS＋Autoprefixer 构建路径；
- 无 `.nvmrc`、`components.json`、`components/ui` 或 `@/*` alias；
- 六个页面静态导入，当前主 JavaScript 约 `558 KiB`，触发 Vite 大包告警；
- 四个独立 Radix 依赖中只有 Dialog 被实际使用，其余三个未被源码引用。

Tailwind 4 迁移的主要已知兼容点：

- `shadow-sm` 约 23 处；
- `backdrop-blur-sm` 约 4 处；
- `outline-none` 约 48 处；
- `space-x/y-*` 约 55 处；
- 裸 `border`、ring、placeholder、hover 和 Preflight 需要逐页检查。

迁移工具只用于产生候选 diff，不能替代人工审查。任何自动修改都必须通过组件
测试、页面交互测试和浏览器视觉检查。

## 3. Node/npm 与启动契约

仓库统一采用：

```text
Node.js 24.18.0 LTS
npm 11.16.0
```

新增：

- 根目录 `.nvmrc`；
- `frontend/package.json` 的 `engines` 和 `packageManager`；
- 根目录或前端目录 `.npmrc` 的 `engine-strict=true`。

`scripts/run_web.sh` 增加以下契约：

- 可选 `VLA_FRONTEND_NODE_BIN_DIR`；
- 配置后将该目录置于前端构建子进程 `PATH` 首位；
- 构建前验证 Node/npm 版本；
- 版本不符时 fail closed，并输出可执行的修复提示；
- 不读取某个用户的 shell profile；
- 不静默回退到服务器 `/usr/bin/node`；
- `SKIP_FRONTEND_BUILD=1` 时不要求 Node/npm，因为该路径只消费已构建的
  `dist`。

服务器配置示例：

```text
VLA_FRONTEND_NODE_BIN_DIR=/home/heying/.nvm/versions/node/v24.18.0/bin
```

Node 只安装在服务器用户的 nvm 目录，不替换系统 Node，不影响同事环境。

## 4. Tailwind 4 迁移

保持 React、Vite 和 TypeScript 主版本不变。依赖策略：

- `tailwindcss` 与 `@tailwindcss/vite` 固定为同一个经验证的 `4.3.x`
  精确补丁版本，不使用浮动 `latest`；
- `tailwind-merge` 固定到支持 Tailwind 4.3 的 `3.6.x`；
- 删除 `autoprefixer`、`postcss` 和旧 Tailwind 3 配置依赖；
- 删除 `postcss.config.js`；
- Tailwind 主题配置迁入 CSS，不保留 JavaScript 配置的隐式加载。

Vite 使用：

```text
@tailwindcss/vite
```

全局 CSS 入口改为：

```css
@import "tailwindcss";
```

现有六个 `console-*` 色值无损迁移到 CSS `@theme`。不得趁迁移调整品牌颜色、
字体、圆角、间距或页面布局。

必须显式审查：

- `shadow-sm → shadow-xs`；
- `backdrop-blur-sm → backdrop-blur-xs`；
- `outline-none → outline-hidden`，保留 forced-colors 可见性；
- bare `ring` 的宽度变化；
- 无显式颜色的 `border`；
- `space-x/y-*` 新选择器与子元素 margin 的组合；
- Button cursor、Dialog margin、placeholder、hover 和 Preflight。

浏览器契约固定为公司当前 Chromium/Edge，最低 Chromium 111。不承诺 M1.5
支持 Tailwind 4 官方基线以下的旧浏览器。

## 5. Radix/shadcn 基线

本地保留 shadcn MCP，用于检索和生成候选组件；MCP 不部署到服务器。

新增 `frontend/components.json`，明确：

```text
framework: Vite
base: radix
style: nova
icon library: lucide
css variables: true
rsc: false
```

增加：

```text
@/* → frontend/src/*
```

到 TypeScript 和 Vite 配置。

首批只建立当前能够实际测试的 primitive：

- Button；
- Badge；
- Alert；
- Progress；
- Dialog。

Tabs、Table、Form、Select、Textarea 等在 M2 出现真实调用方时再生成，避免提前
引入未使用代码。

生成规则：

- 明确使用 Radix，不采用 Base UI 或 React Aria；
- 生成源码提交到 `frontend/src/components/ui` 并逐项审查；
- shadcn CLI 不进入生产依赖；
- 如需 CLI，只能在本地使用写入决策日志的固定版本，服务器不得执行
  `npx ...@latest`；
- primitive 只负责无业务状态的结构、样式和可访问性；
- Annotation CAS、revision、Runtime 和任务状态继续位于领域组件和
  Application Service。

接入采用兼容适配层：

```text
ConsoleButton → ui/Button
StatusTag → ui/Badge
ProgressBar → ui/Progress
```

只有在 DOM、ARIA、键盘和视觉行为等价时才切换调用方。现有
`ConsoleHeader`、`ConsoleSidebar`、`MetricCard`、标注画布和页面业务组件不做
批量重写。

DataPilot 浮动窗口是可拖拽的非模态交互，不得替换成普通 modal Dialog。

在确认统一 Radix 包的行为等价后，只迁移现有两处 Dialog primitive import，并
删除未使用的 Popover、ScrollArea、Tooltip 独立依赖，避免两套 primitive
依赖长期并存。

## 6. 路由和包体

保持所有 URL、SPA fallback 和页面状态恢复语义不变。

首屏继续同步加载：

- AppShell；
- ConsoleHeader；
- ConsoleSidebar；
- DataPilot 浮动入口和窗口基础设施。

页面通过 `React.lazy` 和稳定的 `Suspense` fallback 按路由加载。至少将：

- Annotation；
- Data Management；
- Agent Workbench；

拆成独立 chunk。M2 三维轨迹/视频页和 M4 证据查看器以后必须继续采用独立路由
和懒加载。

不在 M1.5 手写复杂 vendor chunk。退出门禁是初始 bundle 不再触发当前
`500 KiB` 告警，并记录迁移前后的 JavaScript/CSS 原始与 gzip 大小。

## 7. 设计系统文档

新增：

```text
docs/frontend-design-system.md
```

至少记录：

- Node/npm/Tailwind 基线；
- Radix/shadcn 选择与生成方式；
- 品牌 token；
- `components/ui`、`Console*` 和领域组件的职责边界；
- primitive 引入和审查规则；
- focus、键盘、reduced-motion 和 forced-colors 要求；
- 响应式基线；
- 路由懒加载与大型查看器规则；
- 禁止把业务状态移入 UI primitive。

## 8. 实施批次

按以下批次执行，每批测试通过后再进入下一批：

1. 创建分支，保存本计划，更新总体路线；
2. 固定 Node/npm 契约，补 `run_web.sh` 前端构建预检；
3. 迁移 Tailwind 4，逐项修复 utility 和 Preflight 差异；
4. 建立 Radix/shadcn 最小基线和兼容适配器；
5. 建立路由懒加载边界和包体门禁；
6. 完成设计系统文档、测试、视觉 QA 和本地全量回归；
7. 提交冻结候选；
8. 另行执行服务器轻量部署验收，记录事实并冻结 M1.5。

建议提交边界：

```text
docs(frontend): start M1.5 infrastructure milestone
build(frontend): pin Node and migrate Tailwind 4
feat(frontend): establish Radix shadcn primitives
perf(frontend): lazy load console routes
test(frontend): freeze M1.5 design system baseline
```

## 9. 测试与视觉 QA

必须保留并通过：

- 前端现有 `214 passed`；
- Playwright 现有 `7 passed`；
- production build；
- Python 全量 `1525 passed`；
- Router 冻结基线 17 cases；
- Python compileall；
- `git diff --check`。

新增覆盖：

- Node/npm 版本正确、错误和 `SKIP_FRONTEND_BUILD` 分支；
- Tailwind 品牌 token 和关键兼容 utility；
- Button、Badge、Alert、Progress、Dialog；
- Dialog focus、Esc 和遮罩；
- 路由 lazy chunk、fallback 和深链接刷新；
- Sidebar 收起/展开；
- DataPilot 打开、关闭、拖动和非模态语义；
- Annotation SVG 框选、移动、缩放和键盘操作；
- autosave、dirty blocker、409 CAS 冲突和唯一提交；
- Tracking 进度；
- reduced-motion、forced-colors 和键盘可达性。

视觉检查视口：

```text
1440×900
1024×768
390×844
```

覆盖路由：

```text
/
/agent
/data
/annotation/jobs
/annotation/jobs/{job_ref}
/annotation/jobs/{job_ref}/segments/{segment_ref}
/model
/simulation
```

视觉 QA 截图只作为当次临时证据，不提交 `.artifacts/design-qa`。仓库中的长期
基线由 token、组件源码、自动化测试和 `docs/frontend-design-system.md`
共同构成。

## 10. 服务器轻量验收

服务器验收需要单独确认执行窗口，并固定为：

```text
核对 commit 和干净工作树
→ 用户级 nvm 安装/选择 Node 24.18.0
→ 配置 VLA_FRONTEND_NODE_BIN_DIR
→ npm ci
→ 前端测试和 production build
→ 重启 Web
→ 全路由页面 smoke
→ Annotation capability 和历史任务只读 smoke
→ DataPilot smoke
```

服务器不执行：

- 新建 AnnotationJob；
- 首帧标注；
- Tracking；
- Golden candidate；
- 数据文件写入。

部署前记录旧 commit、Node 版本、lockfile hash 和 dist hash。失败时恢复上一提交
和上一份可用 dist；M1.5 无数据库 migration，不需要数据库回滚。

## 11. 退出条件

M1.5 只有在以下条件全部满足后才能冻结并进入 M2：

- Node 24/npm 11 契约可重复，非交互 shell 不再误用 Node 10；
- Tailwind 4 正常运行，旧 PostCSS/Autoprefixer 路径完全移除；
- 无 Base UI、React Aria 或 shadcn CLI 生产依赖；
- Radix/shadcn 最小 primitive 和组件边界已冻结；
- 现有页面视觉、键盘、响应式和业务交互无退化；
- Annotation/Tracking、DataPilot 和后端契约无修改；
- 初始 bundle 不再触发当前 500 KiB 告警；
- 本地全量测试、构建和 Router 冻结基线通过；
- 服务器确定性安装、生产构建和轻量页面验收通过；
- 代码、文档和本地/服务器工作树干净。

## 12. 本地实施结果

2026-07-27 已完成本地批次 1～7：

- 从 `a7315ca` 创建 `codex/automatic-annotation-m1-5`，没有混入 M2
  后处理、三维复核或智能体业务；
- 固定 Node `24.18.0` / npm `11.16.0`，本地所有 npm 门禁均通过
  `fnm exec --using=24.18.0` 使用冻结工具链执行；
- `run_web.sh` 增加精确版本预检和 `VLA_FRONTEND_NODE_BIN_DIR`，首次部署或
  lockfile 变化时仍必须先显式执行 `npm ci`；
- Tailwind `4.3.3`、`@tailwindcss/vite` `4.3.3` 和
  `tailwind-merge` `3.6.0` 已固定；旧直接 PostCSS/Autoprefixer 构建配置及
  Tailwind 3 config 已移除；
- 建立 Radix＋Nova 的 Button、Badge、Alert、Progress、Dialog 受审查
  primitive，并以兼容适配层接入现有 Console 组件；
- shadcn CLI、MCP、Base UI、React Aria 和额外字体均未成为生产依赖；
- 六个页面完成路由级懒加载；构建门禁限制任一 JavaScript chunk 不得超过
  `512000` 字节，当前最大 chunk 为 `376304` 字节，Annotation chunk
  `75547` 字节，CSS `86743` 字节；
- 真实浏览器覆盖 `1440×900`、`1024×768`、`390×844` 与六个主路由，
  未发现 Tailwind 迁移后的全局横向溢出；发现并修复 Agent 工作流手机端
  min-content 溢出，DataPilot 手机浮窗边界正常；
- Annotation/Tracking、CAS、revision、Runtime、Router 和后端契约未改变，
  不需要重跑真实 Tracking。

本地门禁结果：

```text
Python                         1530 passed
Frontend Vitest                218 passed
Playwright                     7 passed
Router suite                   17 cases validated
Production build               passed
Largest JavaScript chunk       376304 / 512000 bytes
Python compileall              passed
git diff --check               passed
production npm audit high+     0
```

生产依赖审计仍报告 React Router 6 的 2 个 moderate 公告；官方自动修复会升级
到 Router 7，属于破坏性主版本变更，因此本里程碑记录风险但不执行
`npm audit fix --force`。

批次 8 的服务器轻量验收结果记录在下一节。由于验收扩大扫描发现既有公开路径
泄漏，M1.5 尚不能宣布完整冻结或进入 M2 实施。

## 13. 服务器轻量验收结果

2026-07-27 在公司服务器完成只读和前端部署验收：

- 旧部署为干净的 `a7315ca`，服务停止前记录旧 `dist` tree hash，并把旧
  `dist` 与 lockfile 保存为可回退副本；
- 通过完整 Git bundle 把服务器切换到
  `17325f90bdf83c2cdc4d588e26c49923f3be0fd9`，工作树干净；
- 用户级安装 Node `24.18.0` / npm `11.16.0`，不修改共享账号默认 Node；
- `npm ci` 安装 262 个锁定包；顶层依赖与本地一致；
- 服务器前端测试 `218 passed`，production build 通过，最大 JavaScript
  chunk `376304 / 512000` 字节；
- 服务器候选 `dist` tree SHA-256 为
  `310ec7e7dceff609646178b25eadaf89db3c5df3e308d22c375fc136c3d304ec`，
  与同提交、同工具链的本地产物一致；
- 未加载既有 M1 Runtime 配置时，
  `/api/annotation/capabilities` 按设计返回
  `processing_runtime_not_configured`；加载冻结配置并以显式
  `VLA_FRONTEND_NODE_BIN_DIR` 重启后，返回
  `available=true`、`runtime_id=navigation_odom_v1`；
- 八条显式 SPA 路由、所有构建 assets、两个历史深链接均为 200；未知路由为
  404；
- 历史 tracked Job/Segment、首帧 JPEG/ETag、安全响应头、两项 processing
  calibration、导航数据摘要和 9 条会话历史均可读；
- DataPilot 打开、历史、关闭和重新打开正常，保持非模态；未发送消息、未恢复
  Session、未操作标注，浏览器控制台无错误；
- 验收日志没有 POST、PUT、DELETE 或致命启动错误；只保留已登记的第三方
  WebSocket 弃用告警；
- 验收前后 Annotation、Session、Navigation 三个 SQLite 主文件 SHA-256
  完全一致，任务/会话状态计数和 lease 均未变化。

服务器部署和 M1.5 前端功能本身通过。但公开响应扩大扫描发现：

```text
GET /api/navigation/datasets/summary
→ dates 中 20260403 / recordings / errors[0]
→ 含服务器绝对路径
```

该字段由 M1 之前已有的 Navigation dataset catalog 错误字符串生成；
`a7315ca..17325f9` 没有后端代码变化，因此不是 Tailwind、Radix、路由懒加载或
服务器部署造成的回归，回滚到旧提交也不会消除。

根据“公开响应不得泄漏绝对路径”的系统原则，不以“既有问题”为由放宽门禁。
后续应建立一个独立、最小的安全修复：

- dataset summary 只返回稳定的公开错误码和无路径文案；
- 私有诊断保留在服务器日志或内部审计中；
- 增加异常 metadata/sync_data 的路径脱敏 API 测试；
- 回归 Data Management、Router 基线、Python 全量和服务器只读泄漏扫描。

该修复通过前，状态保持“服务器功能验收通过、完整冻结暂缓”。
