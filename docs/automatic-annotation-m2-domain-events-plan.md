# 自动标注 M2：领域事件推送与 DataPilot 语义回复计划

> 状态：本地实现与回归已完成，待服务器离线迁移和真实验收
> 日期：2026-07-29
> 上位计划：`docs/automatic-annotation-m2-plan.md`
> 关联计划：`docs/automatic-annotation-m2-session-reliability-plan.md`
> 实施分支：`codex/automatic-annotation-m2`

## 1. 目标与不变量

本轮解决两个相互关联但不能互相替代的问题：

1. Annotation 页面必须及时、准确地跟随权威领域状态，不以浏览器短周期轮询为
   主更新机制；
2. DataPilot 必须在用户可理解的语义边界给出自然语言说明，而不是只留下
   TaskStrip 或机械状态卡。

不可破坏的边界：

- Annotation DB、Navigation Task/Plan 和 RuntimeRun 仍是权威事实；
- LLM 负责调查、判断、Plan 和解释，不负责精确传递状态、路径、参数或几何；
- 页面事件与会话回复都只能由已提交的领域事实派生；
- LLM 回复失败不能阻止状态更新，确定性公开里程碑始终作为兜底；
- 继续满足唯一 DataPilot 身份、唯一 final、response authority 和公开脱敏契约；
- 不修改 Tracking、后处理或 Fix 的业务算法。

## 2. 当前问题

当前 Annotation 列表、Job 和 Segment 页面主要每 2.5 秒发起 HTTP 轮询；DataPilot
会话时间线则通过持久事件和 WebSocket 更新。两套更新通道会造成：

- 页面先看到 Tracking/后处理变化，会话仍停在旧回复；
- 会话已获得工作台里程碑，页面要等下一次轮询；
- 网络、标签页可见性和慢响应可能让旧快照覆盖新状态；
- 每个页面各自维护轮询条件，扩展 Review/Fix 后容易遗漏状态。

## 3. Annotation 公共领域事件

Annotation schema 增加 append-only `annotation_public_events`。Job、Segment 和
Review 的公开 revision 每次提交后，由同一个 SQLite 事务中的 trigger 追加事件：

```text
annotation.job.changed
annotation.segment.changed
annotation.review.changed
```

公开事件只包含：

```text
seq
event_ref
event_kind
aggregate_kind
job_ref / segment_ref / review_ref
state_revision
status
occurred_at
```

事件不得包含日期目录之外的路径、数据库 ID、内部 sequence 名、脚本、工具、
命令、参数、模型响应或凭据。`seq` 是公开流 cursor，不是领域对象身份。

事件表和 trigger 是状态事务的一部分。不得先推送、后写数据库，也不得用内存
消息替代持久事件。

## 4. 服务端推送契约

新增：

```text
GET /api/annotation/events/cursor
GET /api/annotation/events?after_seq=N
```

第二个接口使用 Server-Sent Events：

- `id` 为事件 `seq`；
- `event` 固定为 `annotation`;
- `data` 为严格公开事件 JSON；
- 支持 query `after_seq` 和标准 `Last-Event-ID`；
- 服务端定期发送注释型 heartbeat；
- 断线重连后从最后已确认 cursor 重放。

浏览器初始化顺序：

```text
获取当前 cursor
→ 建立 after_seq=cursor 的 EventSource
→ 立即重新获取页面 snapshot
→ 合并随后到达的事件
```

这样 cursor 之前的事实由 snapshot 覆盖，cursor 之后的变化由事件覆盖。发现事件
缺口、解析失败或连接恢复时重新获取 snapshot。

SSE 是低延迟通知和失效信号，不承载完整 Job、标注几何或轨迹。前端收到相关
事件后只刷新受影响的公开查询。

## 5. 前端更新规则

- 事件按 `seq` 去重，旧事件不得触发状态回退；
- Job/Segment/Review 仍按各自 `state_revision` 单调合并；
- 同一批事件采用短 debounce 合并刷新，避免六个 Segment 同时完成产生请求风暴；
- `focus`、`visibilitychange`、`online` 和 mutation 后继续立即刷新；
- 保留 60 秒低频对账，作为代理、网络设备或浏览器丢失推送时的恢复措施；
- 不再使用 2.5 秒固定轮询作为主要更新机制；
- 页面事件不得切换、创建或关闭 DataPilot 会话。

## 6. DataPilot 自然语言边界

公开体验分为三层：

1. **实时状态**：确定性领域事件和 TaskStrip，准确显示执行状态；
2. **公开里程碑**：系统生成、幂等、持久的 “DataPilot · 状态更新”；
3. **语义回复**：NavigationDataAgent 只在需要解释或决定下一步时生成自然语言。

需要语义回复的边界：

- Plan 已接受并即将进入首帧工作台；
- Tracking 完成，需要继续调查/执行后处理；
- 后处理完成，需要总结并询问是否继续 Fix；
- 后处理失败，需要根据公开错误事实说明影响和下一步；
- Fix/复核完成，需要说明正式发布结果。

只需要确定性里程碑、不额外调用 LLM 的边界：

- 单个 Segment 保存或提交；
- Tracking 内部 target/checkpoint 进度；
- 后处理内部脚本步骤；
- 页面打开、刷新、重连和 cursor 恢复。

后台执行开始时允许一条简短的确定性承接语句；后台完成后由 durable wake
恢复 Navigation Session。若模型未产生合规 `Answer:`，Runtime 使用该语义边界
对应的中文公开兜底，不显示“未能生成安全回复”，也不泄漏工具名。

Tracking 完成使用独立、幂等的 Navigation Workflow System Turn。它不复活原用户
Turn，也不占用或改写用户消息；只在没有其他 Turn 持有 response authority 时创建。
因此模型的合规说明可显示为正常 DataPilot 回复，同时仍保持“每 Turn 唯一 final”。
如果用户/interaction Turn 正在运行，则不抢占焦点，仅保留确定性里程碑。

## 7. 实施批次

1. Annotation schema、公开事件 trigger、cursor 和重放查询；
2. SSE API、heartbeat、Last-Event-ID 和断线测试；
3. 前端 EventSource hook、过滤、去重、debounce 和低频对账；
4. 替换 Jobs、Job、Segment、Reviews/Fix 的短轮询；
5. 补齐 interaction-origin 和 workbench wake 的语义回复/兜底；
6. Python、Vitest、Playwright、production build 和冻结评测回归；
7. 服务器恢复连接后离线迁移、真实状态推送与后处理失败/成功验收。

## 8. 本地实施结果

- Annotation schema 已升级至 v8，公共事件与领域状态在同一 SQLite 事务提交；
- SSE 支持 cursor、`Last-Event-ID`、命名事件和 heartbeat；
- Jobs、Job、Segment、Review、Fix 页面已移除 2 秒/2.5 秒主轮询；
- 前端采用 80 ms 事件合并和 60 秒低频对账；
- interaction 确认后无模型 Answer 时使用“已收到选择并继续处理”的专用回复；
- Tracking 完成会创建幂等 Workflow System Turn；模型回复缺失时使用阶段专用回复；
- 普通 active background update 仍不创建公开 System Turn；
- 前端测试 `249 passed, 8 skipped`，Playwright `10 passed`，生产构建通过；
- Python 全量测试 `1687 passed`。

## 9. 退出条件

- 状态提交后，打开的 Annotation 页面无需手动刷新即可更新；
- SSE 断开和服务重启后能通过 cursor + snapshot 恢复；
- 六个 Segment 批量变化不会形成持续请求风暴；
- 页面、TaskStrip 和会话里程碑不展示相互矛盾的阶段；
- Tracking 完成、后处理成功或失败后都有合适的 DataPilot 自然语言承接；
- LLM 不可用或输出不合规时，状态仍准确且存在可理解的确定性兜底；
- 公开事件、API 和会话中无路径、内部 ID、工具名、脚本参数或凭据；
- 不改变任何业务算法和既有产物语义。
