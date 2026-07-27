# DataPilot 前端设计系统基线

> 状态：服务器前端验收通过；M1.5 完整冻结因既有公开路径泄漏暂缓
> 适用范围：DataPilot Console、自动标注及后续领域页面
> 最后更新：2026-07-27

## 1. 工具链

前端构建固定使用：

```text
Node.js 24.18.0
npm 11.16.0
Tailwind CSS 4.3.3
@tailwindcss/vite 4.3.3
tailwind-merge 3.6.0
Radix unified package 1.6.7
```

版本事实来源依次为根目录 `.nvmrc`、`frontend/package.json` 和
`frontend/package-lock.json`。服务器非交互环境必须显式配置
`VLA_FRONTEND_NODE_BIN_DIR`，不能依赖 shell profile 或系统 Node。

Tailwind 通过 Vite plugin 构建。不得重新增加 PostCSS、Autoprefixer 或
JavaScript Tailwind config。

这里的“移除 PostCSS”指移除项目直接配置和生产构建入口；Vite/Vitest
依赖树中可能仍包含 PostCSS 传递开发包，不应为了追求依赖树字面为零而强制删除。

## 2. 组件层级

前端组件分为三层：

```text
components/ui
→ 无业务状态的 Radix/shadcn primitive

components/console
→ DataPilot Console 视觉和兼容适配层

features/*
→ 领域状态、API、CAS、revision、Runtime 和页面交互
```

`components/ui` 不得：

- 调用业务 API；
- 读取 AnnotationJob、NavigationTask 或 DataPilot Store；
- 持有任务状态机；
- 解释公开事件或内部 Runtime 状态；
- 处理 bbox、轨迹或标定业务数据。

`ConsoleButton`、`StatusTag` 和 `ProgressBar` 继续作为现有页面的稳定入口，内部
可复用 `components/ui`。领域页面不得为了使用 shadcn 而绕开原有 Application
Service 或 CAS 规则。

## 3. shadcn/Radix 规则

`frontend/components.json` 固定为 Radix＋Nova、Lucide、CSS variables 和 Vite
TSX。Base UI 和 React Aria 不进入当前设计系统。

首批受审查 primitive：

- Button；
- Badge；
- Alert；
- Progress；
- Dialog。

新增组件时：

1. 先确认存在真实调用方；
2. 使用本地 shadcn MCP 查询，或使用决策日志中记录的固定 CLI 版本生成候选；
3. 显式选择 Radix；
4. 审查依赖、全局 CSS、ARIA、键盘和 focus 行为；
5. 删除 CLI 自动加入但运行不需要的字体、脚手架和 CLI 依赖；
6. 提交生成源码和测试。

M1.5 使用过的生成器版本为 `shadcn 4.15.0`。生成器不是应用依赖，也不在服务器
执行。服务器只安装 lockfile 声明的构建和运行依赖。

DataPilot 浮动窗口是可拖拽、非模态交互，不得替换成 modal Dialog。

## 4. 颜色和尺寸

Console 权威品牌 token：

```text
console-bg      #f5f7fb
console-panel   #ffffff
console-panel2  #f8fafc
console-line    #d9e1eb
console-text    #17202e
console-muted   #637083
console-cyan    #2d6cdf
```

shadcn semantic token 必须映射到上述视觉体系。当前产品只冻结 light theme；`.dark`
变量仅作为 primitive 的非激活兼容定义，不能据此声明暗色模式已经交付。

不得在基础设施迁移中更换字体。权威字体栈仍为：

```text
Inter
→ ui-sans-serif
→ system-ui
→ -apple-system
→ BlinkMacSystemFont
→ Segoe UI
→ sans-serif
```

## 5. Tailwind 4 兼容规则

为保持 Tailwind 3 视觉：

- 旧 `shadow-sm` 使用 `shadow-xs`；
- 旧 `backdrop-blur-sm` 使用 `backdrop-blur-xs`；
- 旧 `outline-none` 使用 `outline-hidden`，保留 forced-colors focus 可见性；
- 旧 bare `rounded` 使用 `rounded-sm`；
- 旧 bare shadow、blur 和 ring 按 Tailwind 4 官方对应项迁移；
- `space-x/y-*` 与子元素 margin 混用时优先改为 flex/grid `gap`；
- border 必须优先使用显式颜色；M1.5 的 v3 border compatibility layer 只用于
  保持既有页面，不得成为新组件依赖。

支持浏览器基线为公司当前 Chromium/Edge，最低 Chromium 111。

## 6. 可访问性

所有新 primitive 和领域组件必须满足：

- 原生 button、input、select 语义优先；
- icon-only button 有稳定的可访问名称；
- Dialog 有 Title、Description、Esc 关闭和 focus 管理；
- tab、listbox 和复合控件支持键盘操作；
- progressbar 提供当前值和可访问名称；
- focus 样式在 forced-colors 下仍可见；
- 动画尊重 `prefers-reduced-motion`；
- 颜色不能是状态的唯一表达方式。

不允许因为视觉需求使用真正的 `outline-none` 删除键盘焦点，而没有等价的
forced-colors fallback。

## 7. 路由与包体

AppShell、Header、Sidebar、Toast 和 DataPilot 基础设施保持 eager。页面通过
`React.lazy` 按路由加载。

M2 三维轨迹/视频和 M4 证据查看器必须使用独立路由和懒加载，不得合并到主入口
chunk。

`npm run build` 在 Vite 构建后运行 bundle gate。任何单一 JavaScript chunk
不得超过 `500 KiB`。提高阈值必须提供依赖分析和明确批准，不能为了通过构建直接
修改数字。

## 8. 测试与视觉回归

基础设施改动至少执行：

```text
npm test
npm run build
npm run e2e
```

关键路由视觉检查：

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

视口基线：

```text
1440×900
1024×768
390×844
```

视觉 QA 截图是当次临时证据，不提交 `.artifacts/design-qa`。长期基线由本文件、
CSS token、受审查 primitive、交互测试和 bundle gate 共同构成。

自动标注页面改动还必须覆盖：

- URL 刷新恢复；
- autosave 和 dirty blocker；
- 409 CAS 冲突；
- 唯一 revision 提交；
- skip/reopen/unskip；
- Tracking 进度；
- SVG bbox、point、缩放和键盘微调。

## 9. 依赖和服务器规则

- 依赖只通过固定 Node/npm 生成的 `package-lock.json` 更新；
- 服务器使用 `npm ci`，不得使用 `npm install` 或 `npx ...@latest`；
- shadcn MCP 和 CLI 不部署；
- 生产构建不得包含 Base UI、React Aria、CLI 或未使用字体；
- `npm audit fix --force` 不得作为自动修复手段，破坏性主版本升级必须单独规划；
- M1.5 服务器验收只做构建、页面和只读 API smoke，不创建 Job 或运行 Tracking。
