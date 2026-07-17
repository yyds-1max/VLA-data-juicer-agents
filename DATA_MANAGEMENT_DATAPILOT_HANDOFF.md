# 数据管理页 DataPilot 快捷入口交接说明

## 当前状态

- 功能已合入 `main`，当前提交为 `aa8c578`，实现主体提交为 `efa931a`。
- 前端构建通过；前端测试 `183/183` 通过。
- 相关后端回归测试 `59/59` 通过。
- 未新增数据处理接口，仍使用现有会话与消息提交链路。

## 功能目标与边界

“交给 DataPilot”只是 DataPilot 的快捷入口，不是第二套数据处理入口。完整链路保持为：

```text
数据管理页选择数据
  → 打开 DataPilot
  → 创建新会话
  → 显示本地用户消息并连接事件流
  → submitTurn
  → MainRouterAgent
  → NavigationDataAgent
```

前端不直接调用导航处理工具，不判断应该从拆解、同步或后处理中的哪一步开始，也不发送页面状态、服务器绝对路径、`segments` 或内部 Agent/Plan 名称。页面统一使用 `clips`。

## 用户界面与选择语义

- “交给 DataPilot”位于导航数据集表格上方。
- 日期数据来自完整数据集汇总，不受页面搜索、场景或状态筛选影响。
- 第一版只支持一个日期：
  - “全选”表示处理整日期，消息中不包含 `clips`。
  - 选择部分 clips 时，消息中明确列出 clips。
  - 切换日期会清空原 clip 选择。
  - 未选择日期或 clips 时，“确定”不可用。
- 提交期间“确定”保持原文案，但按钮禁用并变灰；取消、X、Esc 和遮罩交互也不会中断提交。
- 弹窗外部尺寸固定为响应式稳定尺寸；日期和 clips 较多时，仅列表内部滚动。
- 日期选择已由系统原生下拉框改为弹窗内的受控列表，避免 macOS 下拉菜单随数据量撑满屏幕；支持方向键、Home、End 和 Esc。
- 点击弹窗外不会关闭弹窗，而会触发边框闪烁提醒；取消、X、Esc 仍可关闭。日期列表展开时，Esc 先关闭列表。
- 右上角 X 已移除蓝色点击焦点边框。

## 消息模板

消息只由 `buildNavigationDatasetRequest(selection)` 构造，位置：
`frontend/src/features/console/navigationDataPilotRequest.ts`。

整日期：

```text
请处理导航数据。

数据日期：20270605

请先检查当前实际产物状态，再根据检查结果决定从哪一步开始。
```

部分 clips：

```text
请处理导航数据。

数据日期：20270605
指定 clips：
- 20260605_152856
- 20260605_160012

请先检查当前实际产物状态，再根据检查结果决定从哪一步开始。
```

函数使用判别类型区分整日期和部分 clips，不用空数组同时表达“全部”和“未选择”，并校验日期及 clip 标识。

## DataPilot 调用、并发和失败处理

- `datapilotStore` 保存单个 `pendingInvocation`，每次请求带唯一 `invocationId`。
- `claimDataPilotInvocation` 原子认领请求，防止双击、StrictMode 或重复渲染导致重复提交。
- `DataPilotWindow` 统一复用新会话提交流程：创建会话、添加可见用户消息、建立事件连接、提交 turn。
- 当前浏览器内若已知存在 `running/waiting` 会话，会先调用 `getSession` 刷新真实状态：
  - 仍在运行：只打开 DataPilot，不创建会话、不提交消息，并提示用户等待或停止。
  - 刷新失败且本地仍认为任务运行：保守阻止新提交。
- 创建会话失败：保留选择，可重新创建。
- 会话创建成功但 `submitTurn` 失败：保留已创建的 sessionId，移除未持久化的本地消息和 turn；重试时复用该会话。
- 失败后修改选择：生成新 invocation；旧空会话不自动删除。
- 只有 `submitTurn` 成功后才关闭弹窗并清空选择。
- invocation 不持久化，页面刷新不会自动重发旧请求。

## 数据管理页同步调整

- 导航数据/机械臂数据改为中性分段切换控件，选中态文字由灰变黑，不使用蓝色文字或蓝色点击边框。
- 搜索框宽度缩短。
- 汇总指标改为紧凑信息条；处理流程改为单行紧凑展示。
- 日期批次表成为主要内容区；日期行可展开查看当天 clips。
- 侧边栏及 DataPilot 浮窗的原有布局和颜色未修改。
- 机械臂数据仍为占位入口，本次未接入处理能力。

## 主要文件

- `frontend/src/features/console/pages/DataManagementPage.tsx`：页面布局、弹窗入口、selection/invocation 生命周期。
- `frontend/src/features/console/components/NavigationDataPilotDialog.tsx`：日期与 clips 选择、固定尺寸、内部滚动及关闭交互。
- `frontend/src/features/console/navigationDataPilotRequest.ts`：集中消息模板与输入校验。
- `frontend/src/store/datapilotStore.ts`：pending invocation 状态机、原子认领、失败和重试。
- `frontend/src/components/datapilot/DataPilotWindow.tsx`：共享新会话提交动作、运行中拦截、事件连接和失败回滚。
- `frontend/src/styles/globals.css`：弹窗边框提醒动画。
- `frontend/src/app/App.test.tsx`、`NavigationDataPilotDialog.test.tsx`、`navigationDataPilotRequest.test.ts`、`eventReducer.test.ts`：前端回归覆盖。
- `tests/test_navigation_agent_tools.py`、`tests/test_agentscope_bootstrap.py`：路由 handoff 和重新调查真实产物的后端回归覆盖。

## 验证命令

```bash
cd frontend
npm test -- --reporter=dot
npm run build

cd ..
.venv/bin/pytest -q tests/test_agentscope_bootstrap.py tests/test_navigation_agent_tools.py
```

## 后续扩展注意事项

- 多日期任务应拆成多个独立任务，不要拼成一条模糊请求。
- 多任务并行开放前，继续保持“已知运行任务时不提交第二条消息”的限制。
- 若以后加入 Skill、计划恢复或导航工具恢复，只调整 DataPilot/Agent 内部链路；数据管理页仍只负责构造用户消息并发起标准会话。
- 前端字段继续使用 `clips`；内部 handoff 转换为 `segments` 的兼容逻辑留在 Agent/工具边界。
