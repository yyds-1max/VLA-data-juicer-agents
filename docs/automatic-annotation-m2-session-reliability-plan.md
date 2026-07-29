# 自动标注 M2：会话与实时状态可靠性返修计划

> 状态：已批准，实施中
> 日期：2026-07-29
> 上位计划：`docs/automatic-annotation-m2-plan.md`
> 实施分支：`codex/automatic-annotation-m2`

## 1. 背景与目标

M2 首轮真实链路验收暴露的不是独立前端显示问题，而是长任务跨越
DataPilot、Navigation Plan、AnnotationJob、Web 工作台和 AgentScope Inbox 时，
缺少完整的状态同步与恢复契约：

- 用户确认标定后可能出现通用兜底，而不是明确进入工作台等待；
- 首帧全部提交并完成 Tracking 后，会话正文没有新的可见进展；
- 自动标注页在 `tracked` 停止刷新，而 M2 的后处理尚未开始；
- AnnotationJob、Navigation Task 和会话 TaskStrip 可能显示相互矛盾的状态；
- 同一标签页刷新后，纯内存前端 Store 丢失当前会话和未完成确认。

本计划在继续 M2 后处理和 Fix 验收前修复这些问题。目标是：

1. 业务数据库保存唯一权威状态；
2. 会话状态是可恢复、单调更新的公开投影；
3. WebSocket 只是低延迟通知通道，不是唯一事实源；
4. 关键阶段在会话正文中留下持久、幂等的公开里程碑；
5. 同一标签页刷新、断线和服务重启后可恢复当前会话与确认；
6. 自动标注页无需用户手动刷新即可跟随后处理生命周期。

## 2. 已确认的会话恢复边界

本次不建设会话注意力中心、待处理会话列表或跨标签页自动恢复。

- 同一浏览器标签页刷新：通过 `sessionStorage` 恢复刚才查看的会话；
- 页面路由切换和自动标注页刷新：不得改变当前 DataPilot 会话；
- 服务重启但原标签页仍在：重连后恢复当前会话；
- 关闭标签页后重新打开、新开标签页或重新打开浏览器：默认进入新建会话页；
- 新建会话发送第一条消息前，继续保留返回上一个 active 会话的现有能力；
- 新会话发送第一条消息后，旧会话继续按既有只读历史契约处理；
- 用户主动点击“+”明确进入新会话，不自动抢回旧会话。

`sessionStorage` 只保存不透明会话 ID 和视图模式，不保存消息、任务状态、确认内容
或领域数据，也不作为任务事实源。

## 3. 明确不在本轮处理的内容

- 不修改 Router 或 Navigation Prompt 来解决模型输出工具名的问题；
- 不改写已有工具事件命名和公开动作映射；
- 不建设跨浏览器、跨设备或关闭标签页后的会话恢复；
- 不允许历史只读会话重新激活；
- 不建设浏览器通知或全局待办中心；
- 不把每个 Segment 或底层执行步骤写入会话正文；
- 不修改导航后处理、Tracking 或 Fix 的业务算法。

## 4. 状态与所有权契约

### 4.1 三层事实

| 层级 | 权威内容 | 规则 |
| --- | --- | --- |
| 领域/编排事实 | AnnotationJob、Navigation Task/Plan、ReviewTask、RuntimeRun | 数据库和 revision 是唯一权威 |
| 会话公开投影 | Turn、Interaction、Task binding、公开时间线 | 只能由权威事实单调投影，不反向决定业务状态 |
| 页面状态 | React/Zustand、轮询缓存、WebSocket 消息 | 可随时丢弃，必须能从 snapshot 和持久事件恢复 |

跨数据库更新继续采用 durable handoff/outbox：

```text
业务事实提交
→ 会话公开投影落库
→ Inbox/WakeQueue 幂等投递
→ 消费确认
→ handoff delivery 完成
→ WebSocket 实时通知
```

`active` 或“处理中”必须存在真实所有者：

- queued/running RuntimeRun；
- 已登记的后台执行；
- 待消费或正在消费的 durable wakeup；
- 明确的人工等待状态。

不存在上述所有者时不得持续显示旋转状态，必须收敛为
`recovery_required` 或等价公开状态。

### 4.2 M2 页面状态

- `preparing`、`tracking`、`postprocessing`：系统实际执行中；
- `waiting_initial_annotation`：等待用户在工作台处理；
- `tracked`：M2 中间态，继续等待 DataPilot 调查/后处理或进入恢复状态；
- `annotated`、`failed`、`cancelled`：Job 终态。

`tracked` 不能归入历史并停止刷新。页面需要结合 Navigation/handoff 投影区分：

- Tracking 已完成，等待 DataPilot 继续；
- DataPilot 正在继续后处理；
- 自动恢复失败，需要重试。

## 5. 会话正文和确认交互

### 5.1 持久里程碑

新增系统生成、白名单化的公开里程碑。它们属于会话时间线，不冒充新的
Assistant final，也不破坏唯一 final 和 response authority：

- `initial_annotation_waiting`
- `tracking_started`
- `tracking_completed`
- `postprocessing_started`
- `postprocessing_completed`
- `workflow_recovery_required`
- `fix_waiting`
- `review_completed`

每个里程碑使用稳定 `origin_key` 去重，正文显示为简洁的
“DataPilot · 状态更新”。TaskStrip 继续提供摘要，但不能成为唯一可见状态载体。

### 5.2 确认卡

确认请求在发起位置原位显示，至少包含：

- 确认原因和公开处理范围；
- 可选项；
- 发起时间；
- pending、submitting、resolved、superseded、failed 状态。

提交后卡片原位保留只读的“已选择”结果。待确认卡滚出视口时，可以在会话窗底部
显示单个跳转提示，但不建设跨会话待办列表。

确认、解决记录和 revision 保存于服务端。同一标签页刷新后重新获取 Session
snapshot 并恢复；多标签页同时提交时继续使用 CAS，一个成功，另一个获取服务器
最新状态。

## 6. 实施批次

### 批次 A：固定复现和正确唤醒

1. 为 Tracking 完成 handoff 增加 AgentScope Inbox schema 测试；
2. 将工作台唤醒消息改为 InboxMiddleware 接受的 `HintBlock`；
3. 所有 Annotation handoff 使用由 `handoff_ref + task_ref + kind` 派生的稳定
   dispatch key；
4. 完成 Navigation Plan step、写入 Inbox、入 WakeQueue 和完成 delivery 之间
   保持幂等；
5. Tracking 已成功但唤醒失败时保留 Job=`tracked`，不得重跑 Tracking；
6. 失败投影为明确恢复状态，不得保留假运行 TaskStrip。

### 批次 B：持久里程碑和事件一致性

1. 增加持久 `workflow_milestone` 公开事件及白名单投影；
2. Session snapshot 返回当前最高持久事件序号；
3. WebSocket 支持从客户端已知序号之后补发持久事件；
4. 前端按 event sequence、task revision 和 interaction revision 单调合并；
5. 旧 HTTP snapshot 不得覆盖较新的 WebSocket 状态；
6. 断线或发现序号缺口时重新拉取 snapshot 并继续订阅。

### 批次 C：同标签页会话恢复

1. 在 `sessionStorage` 保存当前会话 ID 和 active/history 视图模式；
2. 应用初始化时验证会话仍存在，并恢复 snapshot；
3. 无效、已删除或不兼容会话安全回退到新建会话页；
4. 页面内刷新按钮和路由切换不得调用 `enterDraft()`；
5. 用户点击“+”时清除当前恢复指针并进入新会话；
6. 关闭标签页后不通过 `localStorage` 自动恢复。

### 批次 D：自动标注页实时更新

1. 列表、Job 详情和 Segment 工作台统一使用 M2 生命周期；
2. `tracked` 保持轮询；
3. 实际运行态使用短周期兜底轮询，人工等待态降低频率；
4. `focus`、`visibilitychange`、`online` 和工作台 mutation 后立即刷新；
5. Job 数据请求与 capabilities/dataset 请求解耦，避免无关请求延迟状态；
6. 所有响应按 `state_revision` 单调合并；
7. 页面刷新只刷新 Annotation 数据，不影响 DataPilot 会话。

### 批次 E：本地回归和服务器续跑

1. Python、前端 Vitest、Playwright、production build 和既有 Router 基线通过；
2. 在服务器停机同步并完成必要 migration；
3. 从当前 `20270623 / 20260623_145550` 的 `tracked` 权威事实重新投递 handoff；
4. 不重新首帧标注、不重跑 Tracking；
5. 验证原 Navigation Session 进入后处理；
6. 继续完成 M2 后处理、ReviewTask 和人工 Fix 验收。

## 7. 测试门禁

- 错误 Inbox payload 在发送边界被拒绝；
- 同一个 handoff 多次恢复只产生一次 Inbox、一次 WakeQueue 和一次里程碑；
- Tracking 成功后唤醒失败不会污染 Job 或重跑 Tracking；
- 失败后服务重启可以从 durable delivery 恢复；
- TaskStrip 与 Navigation Task/AnnotationJob 终态不会互相回退；
- 同一标签页刷新恢复原会话、消息、TaskStrip 和 pending Interaction；
- 新标签页和关闭后重开仍进入新建会话页；
- 用户点击“+”明确进入新会话；
- 旧 GET snapshot 晚于新事件返回时不回退 task、不清除 interaction；
- WebSocket 断线重连不丢失、不重复持久里程碑；
- 第六个 Segment 提交后页面自动进入 Tracking；
- `tracking → tracked → postprocessing → annotated` 无需手动刷新；
- Tracking、后处理和恢复失败只在正文各出现一次关键里程碑；
- 当前真实 Job 从现有 Tracking 产物续跑。

## 8. 退出条件

本返修只有在以下条件全部成立后才完成：

- 页面和会话对同一任务展示的阶段一致；
- “处理中”始终有真实执行或 durable 调度事实；
- 同标签页刷新不丢会话和确认；
- 自动标注页无需手动刷新即可经过 M2 中间态；
- 关键阶段同时体现在 TaskStrip 和会话正文；
- 当前服务器 Job 可从 `tracked` 安全继续后处理；
- 不改变任何业务算法、原始/同步数据或历史 oracle。

## 9. 2026-07-29 本地实施记录

本轮已完成并通过本地门禁：

- 修正 Annotation 工作台唤醒载荷，改用 AgentScope Inbox 接受的 `HintBlock`；
- handoff 使用稳定 dispatch key，已知的 Tracking 完成唤醒不再因消息 Schema
  错误在消费端静默丢失；
- Tracking 开始、Tracking 完成、后处理完成和复核更新写入幂等公开里程碑；
- Session snapshot 返回最高持久事件序号，WebSocket 支持 `after_seq` 重放和缺口补偿；
- 前端按事件序号、Task revision 和 Interaction revision 单调合并，旧 snapshot
  不再清除较新的确认请求；
- `sessionStorage` 只恢复同一标签页当前 active/history 会话；点击“+”仍进入新会话；
- 修复 React Strict Mode 双 effect 导致恢复指针存在但会话未恢复的竞态；
- Annotation 列表和 Job 页在轮询、重新聚焦、恢复网络和页面重新可见时刷新；
- `tracked` 不再进入历史区，改为“等待 DataPilot 继续”，同时保持不可取消。

本地结果：

- Python：`1680 passed`；
- 前端 Vitest：`246 passed, 8 skipped`；
- Playwright：`10 passed`，包含同标签页刷新后恢复 pending confirmation；
- production build 与 bundle size gate：通过；
- `datapilot-v1` / `navigation-m2` eval schema：`17 / 7` 个 case 验证通过。

仍待服务器停机同步后完成：

- 对现有 `tracked` 测试任务重投递 handoff，或在保留诊断记录后重新创建测试任务；
- 验证原 Navigation 会话进入后处理并出现一次公开里程碑；
- 继续 M2 后处理、ReviewTask 和人工 Fix 的真实验收；
- 验证不存在真实执行或 durable 调度所有者时的最终恢复投影。

模型输出内部工具名的问题按本轮已确认范围继续延期，不包含在上述实现中。
