# 自动标注 M2：DataPilot 主导的后处理与三维人工复核/Fix 开发计划

> 状态：本地实现与回归通过，待停机迁移和服务器 Golden/writer 验收
> 开始日期：2026-07-28
> 开发基线：`a2d4ccd`
> 开发分支：`codex/automatic-annotation-m2`
> 上游里程碑：M1.5 完整冻结
> 上位路线：`docs/automatic-annotation-roadmap.md`

## 1. 目标、原则与边界

M2 把 M1 保留的 `tracked` staging 接入原导航后处理，并提供无需 XQuartz 的
三维人工 Fix/审核。处理任务始终由 DataPilot 主导：

```text
用户选择日期和外层 clips
→ MainRouter 委派 NavigationDataAgent
→ NavigationDataAgent 调查并选择规范化领域决策
→ Application Service 校验 Plan 与调查证据
→ 冻结 Runtime 执行
→ Web 工作台采集首帧标注或人工 Fix
→ durable handoff 恢复 Navigation Session 或关联 Fix Task
```

职责边界：

- DataPilot 是处理任务的唯一对话和编排入口。
- NavigationDataAgent 负责导航领域调查、计划、选择和恢复。
- LLM 只选择规范化业务决策，不选择脚本路径、不拼命令、不搬运几何数据。
- 系统负责 ID、路径、参数、状态、文件、锁、校验、恢复和原子发布。
- Web 只提供处理快捷入口、人工标注/Fix 工作台、状态和结果，不形成第二条
  手动处理流水线。
- MainRouter 不获得 Tracking、gridmap、投影、轨迹或 Fix 底层工具。
- 不新增 `AnnotationAgent`。

M2 不包含：

- 拆解或同步；
- Ins 或混合 Runtime 的生产执行；
- 二维人工复核或二维 AI；
- 三维 AI 审核、置信度或 AI 候选 Fix；
- 数据管理生命周期 JOIN、仪表盘真实统计和历史批量导入；
- Tracking 后新增质量/完整性门；
- 修改原后处理或 Fix 的业务算法和数值方法；
- 重构 M1/M1.5 已冻结的前端页面。

M1 的直接创建 Job、开始 Tracking API 继续保留给测试和运维兼容路径，但普通
产品 UI 不再显示。取消等安全控制继续保留。

## 2. 唯一处理链与任务边界

### 2.1 自动标注与后处理

用户从自动标注页点击“交给 DataPilot 处理”，只选择
`dataset_date + source clips`。快捷消息的目标固定为“执行自动标注并完成
后处理”，不在页面选择处理标定、脚本或步骤。

NavigationDataAgent：

1. 调查同步产物、已有 AnnotationJob、localization、gridmap、前置产物和 Runtime
   能力；
2. 从系统公布的规范化枚举中选择处理决策；
3. 由 validator 验证决策与调查事实一致；
4. 创建或复用 AnnotationJob；
5. 在需要首帧输入时进入 durable Web handoff；
6. 用户提交全部首帧后恢复原 Navigation Session；
7. 完成 Tracking、后处理和原始 trajectory 发布；
8. 冻结 TrajectoryRevision 并创建 pending TrajectoryReviewTask。

从已有 `tracked` Job 恢复时不得重复执行 prepare、首帧标注或 Tracking。

### 2.2 后处理与 Fix 的分界

后处理完成时必须先结束原处理任务：

```text
AnnotationJob = annotated
→ 冻结每个非 skipped segment 的 TrajectoryRevision
→ 创建 pending TrajectoryReviewTask
→ Navigation 处理任务 = completed
→ 释放会话活动任务槽和 writer lock
→ DataPilot 在唯一 final 中询问是否继续 Fix
```

这个询问是普通非阻塞回复，不创建 `AwaitUser` 或现有 Interaction：

- 用户回答“继续 Fix”：系统幂等创建关联的新 Navigation Fix Task；
- 用户回答“暂不 Fix”或不回复：ReviewTask 长期保持 pending，不占任务槽；
- 用户最初已经明确要求完整处理到 Fix：处理任务仍先 completed，随后自动创建
  关联 Fix Task，不重复询问；
- 会话已有其他活动任务时不得创建 Fix Task，也不得自动取消已有任务；
- 旧处理任务保持 completed，不因继续 Fix 被复活。

系统侧 lineage 保存日期、外层 clips、AnnotationJob、TrajectoryRevision 和
ReviewTask 关联；LLM 不在自然语言中转述内部 refs。

## 3. 领域模型、状态和迁移

### 3.1 Annotation 领域

扩展独立 `annotation.sqlite`：

- `annotation_jobs` 增加 `postprocessing`、`annotated`；
- `annotation_segments` 增加 `postprocessing`、`annotated`、
  `postprocessing_failed`；
- `runtime_runs.kind` 增加 `postprocessing`、`fix`、
  `compatibility_publish`；
- `postprocessing_specs` 保存经 validator 接受的 localization、gridmap
  decision、trajectory variant、Plan revision 和调查证据摘要；
- `trajectory_revisions` 保存每个非 skipped segment 的不可变原始轨迹；
- `trajectory_review_tasks` 保存人工审核任务；
- `fix_calibration_snapshots` 独立保存 Fix 标定内容和哈希；
- `fix_drafts` 保存可变 working copy，并使用 revision CAS；
- `fix_revisions` 保存不可变正式提交；
- `review_decisions` 保存不可变批准、退回或废弃记录；
- `compatibility_publications` 保存训练兼容文件发布账本；
- `workflow_handoffs` 保存首帧或 Fix 工作台 durable outbox；
- `annotation_task_links` 保存 Navigation task、AnnotationJob 和 ReviewTask
  lineage，不建立跨 SQLite 外键。

Job 状态：

```text
preparing
→ waiting_initial_annotation
→ tracking
→ tracked
→ postprocessing
→ annotated
→ failed / cancelled
```

Review 状态：

```text
pending → in_progress → approved
                      → returned → in_progress
                      → discarded
```

约束：

- approved 和 discarded 为终态；
- 需要重新处理时创建新的 TrajectoryRevision 和 ReviewTask，不原地重开；
- `pass` 保留为原业务字段，不等于审核状态，也不默认过滤训练数据；
- FixRevision、ReviewDecision 和发布 intent 的身份字段不可变；发布状态只允许
  `queued → running → succeeded/failed` 的受控 CAS；
- “已验证”只在批准版本成功发布后成立。

### 3.2 Navigation Plan 与任务 lineage

NavigationTask 核心状态机不改变。开发基线已经使用
`navigation-plan-v3`，因此 M2 将 Navigation Plan contract 升级为
`navigation-plan-v4`：

- 保留 `extract_sync`、`finish_processing`；
- 新增 `trajectory_review` phase；
- 增加 task outcome 和 lineage sidecar；
- v1～v3 历史 Plan 继续只读，不迁移或改写；
- 新任务使用 v4；
- Plan/step identity 由 Runtime 精确绑定领域对象，模型不传递数据库 ID。

### 3.3 离线迁移

真实 Annotation DB 和 Navigation DB 的 M2 migration 必须在同一停机窗口
分别显式执行，不能依赖应用启动时自动改表：

1. 停止 Web、Worker 和所有 writer；
2. 备份 Annotation、Navigation、Session 数据库主文件、WAL 和 SHM；
3. 获取 maintenance lock；
4. Annotation 从 schema v3 迁移至 v5；
5. Navigation 只接受精确的
   `navigation-attempts-final-v2`，再迁移至
   `navigation-attempts-m2-v1`；
6. 重建含新 CHECK 约束的 M1 表；
7. 为历史 Navigation task 回填最保守的
   `requested_outcome=auto`、`completion_outcome=NULL`，不伪造旧任务的
   Fix 或完成语义；
8. 保留原 ID、opaque ref、revision、manifest、receipt、Plan、step、
   observation、evidence、submission attempt、outbox 和 handoff；
9. 执行 `foreign_key_check` 和 `integrity_check`；
10. 验证 migration ledger 连续和 migration safety marker 为
    `verified`；
11. 数据库版本超前、迁移断档、来源 contract 漂移或校验失败时拒绝启动。

迁移工具不得自动触发真实后处理、创建 ReviewTask 或导入历史训练文件。

## 4. 后处理决策契约

后处理不得固定为 `_0525 + pcd_to_grid`。NavigationDataAgent 先调查，再选择
系统公布的规范化决策。

Gridmap decision：

- `copy_existing_gridmap`
- `generate_from_pcd`
- `skip_if_projection_ready`

Trajectory variant：

- Ins：`cjl_with_gridmap`
- odom：`cjl_0525_with_gridmap`

validator 必须验证：

- localization 证据与 trajectory variant 匹配；
- `copy_existing_gridmap` 的源 gridmap 存在且属于已选范围；
- `generate_from_pcd` 的点云前置存在；
- `skip_if_projection_ready` 的复用产物和 provenance 可验证；
- Runtime manifest 支持所选组合；
- Plan 的日期和外层 clips 与绑定任务范围完全一致。

M2 生产 Runtime 只支持已冻结的 odom 变体。Ins、混合 localization、未知组合或
证据冲突返回结构化 `unsupported_runtime_variant`，不得回退到同事目录中的其他
脚本。

Application Service 将规范化决策映射到冻结脚本和精确参数；这些映射不得进入
公开 API 或模型上下文。

## 5. 后处理 Runtime 与发布

### 5.1 全新 attempt staging

每次后处理 attempt：

1. 从 M1 tracked staging 构造只包含 `tracked` segments 的输入视图；
2. skipped segments 不进入后处理；
3. 输入只能 byte-copy、reflink 或真实 CoW，禁止 hardlink；
4. 拒绝残留目录、symlink 和特殊文件；
5. 校验并复制 M1 已提交 manifest 绑定的 `v1.0-trainval` 和 maps；M2 不得再次
   执行 `NoobScenes/main_smart_odom.py`；
6. 按已接受的 GridmapDecision 在私有镜像准备 gridmap；
7. 执行冻结投影、世界坐标、速度、方向、轨迹和 `3_move_dir`；
8. 保持现有末尾 `validate_navigation_outputs` 语义，不扩展检查项；
9. 生成私有 final candidate、manifest 和 TrajectoryRevision；
10. 对整批目标完成冲突预检和私有 staging 后，通过 publication journal 发布到
    兼容 `finish_data`。

`pcd_to_grid.py` 会写入输入 root，因此只能作用于 job-private `clip_data`
镜像。`3_move_dir.py` 会删除或覆盖 final，因此只能作用于私有模拟 final root。
不得修改 raw、真实 `clip_data`、历史 oracle、同事源码或公共 scratch。

每次 retry 都使用全新 attempt staging。不得复用旧 `finish_temp`，防止旧的空
`distance.txt` 等文件被误识别为新目标。

### 5.2 共享产物与原子发布

- 共享 maps/metadata 已存在时必须与本次 candidate 哈希一致；
- 不一致时停止发布并生成稳定错误码，不做覆盖或自动合并；
- 写入任何 clip 前先完成全批 candidate/目标冲突预检，并把所有缺失 clip staging
  到兼容目录的私有临时目录；
- 发布账本先记录 intent/staged，再以单个外层 clip 为单位原子移动 candidate，
  最后记录 committed；
- SQLite 无法为多个目录提供单一文件系统事务，因此这里的保证是“全批预检＋单
  clip 原子＋批次 journal 可恢复”，不宣称日期级原子提交；
- 进程中断后根据 journal 和哈希恢复，不根据目录存在猜测成功；
- TrajectoryRevision 只引用已提交的 immutable manifest；
- `annotated` 只在所有非 skipped segments 完成发布后成立；
- 部分成功保持可恢复的失败状态，不投影为整批已标注。

### 5.3 运行隔离

继续沿用 M1：

- Annotation/Navigation 共用的系统级 writer lock；
- capacity=1；
- bubblewrap 和只读冻结 Runtime；
- 独立进程组；
- SIGTERM→SIGKILL 取消；
- durable quarantine 和 recovery_required；
- 6 小时后处理超时；
- Runtime manifest、step、checkpoint 和产物哈希。

等待首帧、等待是否继续 Fix、等待人工 Fix 和等待审核时不得持有 writer lock。

## 6. 三维人工 Fix

### 6.1 Fix 标定

Fix 标定与处理标定独立：

- 页面展示原轨迹处理标定；
- 页面展示 `purpose=fix` 的可用 profiles，不显示全局推荐；
- `20260409_U` 可作为 Fix profile；
- 选择后冻结标定文件内容和哈希；
- Fix 标定与处理标定不同时必须填写原因；
- Fix 标定不得回写或覆盖处理标定。

### 6.2 领域命令

浏览器不得提交完整轨迹 JSON、脚本参数或路径，只提交绑定 expected revision 的
领域命令：

- `set_position`
- `set_direction`
- `set_speed`
- `delete_target`
- `add_missing_target`
- `restore_frame`
- `toggle_pass`

系统在独立 staging 中调用冻结 Fix 数值逻辑，保留旧脚本的默认速度、方向、
颜色、back、pass、缺失目标和轨迹点语义。不得在 FastAPI 进程中嵌入或长期运行
旧 GUI 实例。

### 6.3 Revision 与审核

```text
选择 Fix 标定
→ 创建 FixDraft
→ CAS 应用领域命令
→ 冻结 FixRevision
→ 人工批准 / 退回 / 废弃
```

- draft 是可变 working copy；
- 每次提交生成不可变 FixRevision；
- 409 冲突不自动合并轨迹，由用户选择服务器版本或基于最新 revision 重做；
- 退回时回到 Fix 工作台，不自动重跑后处理；
- 废弃保留完整审计，不进入训练出口；
- 批准只创建不可变 ReviewDecision，不直接宣称已验证。

批准后的兼容发布：

```text
单一 SQLite 事务：
FixRevision + approved ReviewDecision + review=approved
→ queued CompatibilityPublication + compatibility_publish RuntimeRun

AnnotationWorker：
queued → running
→ compatibility publication journal
→ *_trajectory_fix_five.json
→ DB committed / failed
→ 已验证
```

HTTP 批准请求不写兼容文件；文件发布由持有 writer lock 的 AnnotationWorker
异步执行。批准与文件发布不是数据库原子事务，但批准事实、终态 Review 和
durable queued intent 必须在一个事务内提交。因此绝不允许出现“文件已发布而
数据库尚未 approved”。发布失败保持 approved 事实和
`publication_failed`，由精确绑定原 revision 的 retry-publication 恢复，不创建
第二个审核决定。

## 7. 公开 API

基础路径继续为 `/api/annotation`：

```text
GET  /reviews
GET  /reviews/{review_ref}
GET  /reviews/{review_ref}/evidence/...

POST /reviews/{review_ref}/fix-sessions
POST /reviews/{review_ref}/fix-commands
POST /reviews/{review_ref}/fix-revisions

POST /reviews/{review_ref}/approve
POST /reviews/{review_ref}/return
POST /reviews/{review_ref}/discard
POST /reviews/{review_ref}/retry-publication

GET  /calibration-profiles?domain=navigation&purpose=fix
```

后处理启动不提供普通 Web API，只允许 plan-bound Application Service 调用。

所有 mutation：

- 必须携带 `Idempotency-Key`；
- 必须携带相应 expected review/draft/revision；
- 同 key 同请求返回原结果；
- 同 key 不同请求返回 409；
- Pydantic 输入 `extra="forbid"`；
- 公开响应只使用随机 128-bit
  `job_ref/segment_ref/review_ref/revision_ref`；
- 不返回内部 sequence 名、数据库 ID、绝对路径、脚本、命令、工具名或参数。

evidence API 只允许访问 manifest 登记的相机投影视图、gridmap 鸟瞰图和受控
轨迹摘要；必须拒绝路径穿越、symlink、特殊文件和任意 filesystem path。

## 8. DataPilot、评测与 durable handoff

### 8.1 实施顺序

修改 NavigationDataAgent Prompt 或领域工具前，先建立 Navigation 最小评测：

- 从 synced 创建或复用 AnnotationJob；
- 从 waiting handoff 恢复；
- 从 tracked 起点继续而不重跑 M1；
- 调查并选择 gridmap/localization/trajectory variant；
- 后处理完成创建 ReviewTask；
- 继续或暂不 Fix；
- 创建关联 Fix Task；
- 等待 Fix 工作台完成并报告；
- 停止、取消、错误恢复和范围保持。

Router 只小范围增加：

- “自动标注/后处理”；
- “继续 Fix/暂不 Fix”；
- “复核/修正三维轨迹”；
- 自动标注快捷入口；
- 已有活动任务时的冲突保护。

原 Router 冻结基线继续做候选/基线回归。范围不得扩大，导航意图不得调用通用
工具，委派后不得产生第二个 Router final。

### 8.2 快捷入口

“标注工作台”的普通入口改为“交给 DataPilot 处理”：

- 只选择日期和外层 clips；
- 可见消息明确“执行自动标注并完成后处理”；
- 继续使用可信 `navigation_dataset_selection_v1` 精确绑定范围；
- entrypoint 为 `annotation_processing_shortcut`；
- 创建新的 DataPilot 会话，不扩展 per-turn trusted context；
- 处理标定由 NavigationDataAgent 调查后通过结构化 Interaction 收集；
- 页面不显示跨数据集推荐，不显示脚本和业务变体。

### 8.3 工作台 handoff

首帧标注：

- Navigation 创建或复用 AnnotationJob 后写入 waiting handoff；
- 页面只展示绑定 Job 的内部匿名 segment 队列；
- 全部 resolved 后写入 durable outbox；
- 普通 UI 不显示“开始 Tracking”；
- Runtime 用 System Turn 唤醒原 Navigation Session；
- bbox、point、颜色和 revision 数据不进入聊天上下文。

Fix：

- linked Fix Task 写入 review handoff；
- 用户在 Fix 工作台提交 FixRevision 或审核动作；
- durable outbox 唤醒关联 Navigation Session；
- Runtime 只注入状态、公开结果和 opaque binding，不注入完整轨迹；
- 页面关闭、断线或长时间不操作不占 writer lock。

所有新公开事件必须经过 PublicActionRegistry，继续满足单一 DataPilot 身份、
唯一 final、response authority、迟到 final 拒绝和脱敏契约。

## 9. 前端

自动标注保持一个侧栏入口，内部使用 URL 化切换：

```text
/annotation/jobs
/annotation/reviews
/annotation/reviews/{review_ref}
```

- “标注工作台”承载现有 Job、首帧队列和历史任务；
- “人工复核”承载 Review 列表与统计；
- Fix 工作台为独立懒加载路由；
- 页面刷新、前进、后退必须恢复同一 review 和 draft；
- 内部 segment 只在工作台显示为匿名 `Segment 01…N`。

人工复核页：

- 展示待复核、修正中、已退回、已验证数量；
- active 列表包含 pending、in_progress、returned；
- history 包含 approved、discarded；
- 支持日期、外层 clip 和状态筛选；
- 按外层 clip 聚合内部 review 数量；
- 每项提供“进入人工 Fix”；
- M2 不显示“交给 DataPilot 复核”按钮。

Fix 工作台：

- 相机投影视图；
- gridmap 鸟瞰轨迹；
- 目标与帧时间线；
- 位置、方向、速度；
- 原始 TrajectoryRevision 与 FixDraft/FixRevision 对比；
- 独立 Fix 标定与差异原因；
- 草稿自动保存、CAS 冲突和脏状态导航保护；
- 提交、批准、退回和废弃动作。

通用无业务状态控件优先通过 shadcn MCP 检索并审查源码：

- 优先复用 Button、Badge、Alert、Progress、Dialog；
- 按真实调用方增加 Tabs、Table、Checkbox、ScrollArea、Select、
  AlertDialog、Resizable；
- 不引入 Base UI、第二套 primitive、cmdk、图表库或表单框架；
- 轨迹 Canvas、时间线和领域状态组件自行实现；
- Annotation CAS、revision 和状态逻辑不得放入 primitive。

## 10. 实施批次

1. 保存本计划，更新总体路线、架构和 DataPilot M2 契约扩展。
2. 建立 Navigation 最小评测和新增 Router case。
3. 完成 Annotation/Navigation migration、状态机和领域对象。
4. 完成 fake-runtime Application Service、handoff、幂等和恢复。
5. 完成冻结后处理 wrapper、publication journal 和 Golden。
6. 完成 Fix kernel adapter、FixRevision、审核和训练出口。
7. 完成 linked Fix Task、Navigation Prompt/工具和 PublicActionRegistry。
8. 完成“标注工作台/人工复核”页面和 Playwright。
9. 完成本地全量回归，再单独申请服务器部署和真实 writer 验收。

每批必须先通过针对性测试，不能等到服务器验收才验证跨库状态、幂等或发布恢复。

## 11. 测试门禁

### 11.1 后端与领域

- migration、未来版本拒绝、断档、完整性和备份恢复；
- Job、Segment、Review、Fix 和 publication 状态迁移；
- CAS、幂等、双击、并发编辑和 immutable revision；
- task link、linked child 幂等和旧任务不复活；
- tracked 起点不重跑 M1；
- 三种 GridmapDecision 与两个 trajectory variant；
- unsupported Ins、混合 variant 和证据冲突 fail closed；
- skipped segment 隔离；
- 每次 retry 新 staging；
- `distance.txt` 伪目标负向回归；
- writer lock、取消、超时、crash 和 recovery_required；
- final candidate 原子发布和 journal 中断恢复；
- shared maps/metadata 哈希冲突；
- Fix 标定差异原因和 CalibrationSnapshot；
- 全部 Fix 领域命令与旧数值逻辑等价；
- 批准、退回、废弃、发布失败和 retry-publication；
- 公开 DTO 无路径、内部 ID、工具、脚本、参数或凭据。

### 11.2 智能体与契约

- Navigation 必要调查和规范化决策；
- exact date/clip scope；
- synced、waiting、tracked 和 annotated 起点；
- 首帧 handoff、System Turn 和恢复；
- 后处理 completed 后释放活动任务槽；
- 继续 Fix 创建一个 linked child；
- 暂不 Fix、无响应和已有其他活动任务；
- 初始请求已包含 Fix 时不重复询问；
- 唯一 final、response authority 和 late-final rejection；
- 原 Router 评测基线无回归。

### 11.3 前端

- URL 切换、刷新和浏览器前后退；
- Job 与 Review 列表筛选和状态；
- Fix 标定、轨迹命令、autosave 和 409 冲突；
- 草稿脏状态离开保护；
- 批准、退回、废弃与错误恢复；
- 1440×900、1024×768、390×844；
- 键盘、焦点、Dialog/AlertDialog 和基础可访问性；
- 路由懒加载和 production bundle gate；
- 当前 Python、前端、Playwright、Golden、Router suite 和生产构建无回归。

## 12. Golden 与服务器验收

真实服务器写入必须另行批准。顺序固定为：

1. 核对服务器 commit、配置、数据库、Runtime manifest、writer 和备份；
2. 停机执行 M2 migration，并复核三个数据库和 migration ledger；
3. 只读捕获原 `20260605/20260623` oracle；
4. 先用 byte-identical tracked 输入进行后处理算法 Golden；
5. 再用真实 2027 AnnotationJob 验证端到端状态；
6. 单独运行缺 gridmap 测试副本；
7. 选择一个 segment 对比旧 `run_fix` 和 Web Fix 的同输入、同标定、同领域操作；
8. 验证 raw、clip_data、历史 oracle、同事源码和公共 scratch 未变化；
9. 扫描公开 API、事件和日志投影中的路径、内部 ID、工具和凭据。

严格主 oracle：

- `20260623 / 20260623_145550`，覆盖六个干净 segments。

补充用例：

- `20260605 / 160904` 的历史 oracle 曾因重复复用脏 staging，把空
  `distance.txt` 识别为伪目标；该样本只作为“新 attempt 不得复现污染”的负向
  回归，不作为干净主 oracle；
- 真实 M1 `20270605 / 160904` tracked staging 本身不等于上述历史污染 staging；
- `map.png` 继续以业务确认的 1×1 兼容形式为准；
- 缺 gridmap 的副本专门验收 `generate_from_pcd`。

新旧产物出现任何非白名单差异时立即停止，报告：

- 相对文件和阶段；
- Schema selector 或数值差异；
- 命令顺序；
- Runtime/标定/输入哈希；
- 可疑来源。

未经业务同事确认，不得修改算法、放宽 tolerance、增加 ignore 或扩大
normalization。

## 13. M2 退出条件

M2 只有在以下条件全部满足后才可冻结：

- DataPilot 可从 synced 或已有 tracked 事实调查并执行到 annotated；
- Navigation 选择的 gridmap/localization/trajectory variant 经过系统校验；
- 首帧与 Fix 均通过 durable Web handoff；
- 后处理结束后正确询问，或按明确完整请求自动创建 linked Fix Task；
- 原处理任务 completed，pending ReviewTask 不占活动任务槽；
- 用户无需 XQuartz 即可完成三维人工 Fix、批准和发布；
- 正式生成兼容 `_trajectory_fix_five.json`；
- 原始数据、同步数据、历史 oracle、同事源码和公共 scratch 未被修改；
- 严格 Golden、本地全量回归和服务器验收全部通过。

M2 冻结后才规划 M3 的数据管理、仪表盘、部分完成投影、历史导入和跨页面深链；
M4 才上线 DataPilot/模型辅助复核、置信度和
`AIProposedFixRevision`。

## 14. 本地实现结果（2026-07-28）

本计划的 9 个本地实施批次已经完成，当前实现包括：

- Annotation schema v5、Navigation Plan v4、状态机、task lineage 和 durable
  handoff；
- processing owner 唯一约束、精确 clip scope 复用，以及迁移完整性安全标记；
- tracked 起点后处理、三种 gridmap decision、odom trajectory variant、
  私有 attempt staging、publication journal 和 Golden v2 绑定；
- 独立 Fix 标定、领域命令、FixDraft/FixRevision、人工审核和异步兼容发布；
- 批准与发布分离：只有兼容文件成功发布才投影为“已验证”，失败可幂等重试；
- DataPilot 自动标注快捷入口、首帧恢复、后处理完成询问和 linked Fix child；
- 既有 tracked Job 进入后处理前会幂等绑定唯一 processing owner，完成 handoff
  可恢复原 Navigation Session；
- linked Fix 的 Redis inbox/wakeup 使用稳定 dispatch token 与原子 marker/XADD，
  SQLite receipt 仅用于审计；
- `/annotation/jobs`、`/annotation/reviews`、匿名 Segment 队列和三维 Fix 工作台；
- Navigation M2 最小评测，以及“继续 Fix”和“暂不 Fix”等 Router 小范围 case。

本地门禁结果：

- Python 全量：`1636 passed`；
- Annotation 专项：`284 passed`；
- 前端 Vitest：`238 passed, 8 skipped`；
- Playwright 全量：`9 passed`（其中 M2 新增 `2 passed`）；
- production build：通过，最大 JavaScript chunk `378188 < 512000` bytes；
- `router-smoke`、`datapilot-v1`、`navigation-m2` 分别验证
  `4 / 17 / 7` 个 case schema；
- Python compileall 与 `git diff --check`：通过。

停机迁移前的真实库只读预检与演练结果：

- 服务器旧 Navigation generation 为 `navigation-attempts-final-v2`；
- 旧库包含 7 个 task、6 个 Plan、24 个 step、53 个 observation、53 个
  evidence 和 10 个 submission attempt，outbox 与人工 handoff 均为空；
- 使用旧库只读副本完成 `final-v2 → m2-v1` 演练，除 generation marker
  外全部旧表数据逐行保持一致；
- 演练后 foreign key 为空、integrity 为 `ok`、migration safety 为
  `verified`，M2 Store 可打开；
- 该演练不代替正式停机迁移，也没有修改服务器数据库。

评测 case 模型在 M2 增加 Navigation 专用可选字段时，`datapilot-v1`
的 canonical hash 显式忽略这些新默认字段，保证未变化的旧 YAML 不因共享模型
扩展再次漂移。M1.5 基线之后单独批准过的冲突场景同义词扩展仍会形成一次真实
case-set 变更，不能伪装成与旧 baseline 兼容；本轮需要对其余旧 case 做
差异审计，并在明确批准后再决定是否晋升新 baseline。

上述评测结果中的 `navigation-m2` 仅表示 case、host 和确定性 grader 本地门禁
通过；真实模型重复运行和基线晋升尚未执行。真实 Annotation DB 离线迁移、冻结
Runtime 部署、服务器后处理/Fix writer、业务 Golden 和数据无污染审计也尚未
执行。因此 M2 当前不能冻结，下一步必须按第 12 节另行批准并完成服务器验收。
