# 自动标注板块总体开发路线

> 状态：M0～M2 已冻结；M3、M4 暂不启动
> 最后更新：2026-07-31
> 适用范围：导航数据自动标注、后处理、三维轨迹复核/Fix，以及后续可复用的标注领域能力  
> 优先级：本文件在自动标注范围内优先于 `architecture.md` 中的历史占位描述

## 1. 总体策略

采用“总体路线冻结、里程碑滚动细化”的开发方式：

```text
M0 契约与 Runtime 基线
→ M1 Web 首帧标注与 Tracking
→ M1.5 Tailwind 4 与 Radix/shadcn 设计系统基线
→ M2 DataPilot 主导的完整后处理与三维人工复核/Fix
→ M3 数据管理、仪表盘与跨页面状态整合
→ M4 DataPilot/模型辅助复核与 AI 候选 Fix
```

进入每个里程碑前，先根据仓库、服务器和上一个里程碑的实际结果建立当期任务级计划：

```text
docs/automatic-annotation-m0-plan.md
docs/automatic-annotation-m1-plan.md
docs/automatic-annotation-m1-5-plan.md
docs/automatic-annotation-m2-plan.md
docs/automatic-annotation-m3-plan.md
docs/automatic-annotation-m4-plan.md
```

## 2. 已确认的业务边界

完整历史导航链路为：

```text
run_U.sh（拆解、同步）
→ run_odom.sh（自动标注和后处理）
→ run_fix.sh（人工三维轨迹复核/Fix）
→ 训练消费 *_trajectory_fix_five.json
```

本板块从同步产物开始：

```text
同步产物
→ assemble_finish_temp
→ NoobScenes preprocessing
→ Web 首帧标注
→ Tracking
→ NavigationDataAgent 调查 localization、gridmap 和已有产物
→ 选择并校验 gridmap 决策与 trajectory variant
→ 投影、世界坐标、速度和方向
→ 按选定的原业务变体执行 gridmap transform 和轨迹生成
→ final 发布
→ 已标注 / 待轨迹复核
→ 人工三维轨迹复核/Fix
→ 已验证
→ 训练数据出口
```

`run_U.sh` 不属于自动标注模块，系统已有的拆解、同步实现不在本轮重复开发。

V1 明确不包含：

- 二维人工复核；
- 二维 AI 复核；
- 二维审核状态；
- Tracking 后新增的质量或完整性门；
- 机械臂具体标注界面；
- 新增 `AnnotationAgent`。

现有 finish Plan 末尾的 `validate_navigation_outputs` 保持原语义和测试，不扩展检查项，也不在自动标注页面中表现为独立业务阶段。

## 3. 不可违反的工程约束

### LLM 与确定性系统的职责边界

- LLM/多模态模型只承担其擅长的语义与认知工作：理解用户意图、识别目标与范围、规划步骤、基于系统返回的事实进行推理、生成面向用户的说明，以及在 M4 中基于受控证据执行三维轨迹辅助审核。
- DataPilot 是处理任务的唯一对话和编排入口；NavigationDataAgent 负责调查数据并
  选择规范化领域决策。Web 页面只提供任务快捷入口、人工输入工作台、状态和结果，
  不形成与智能体并行的第二条处理流水线。
- 后端不让 LLM 选择脚本路径或拼接命令。LLM 只选择系统公布的
  `GridmapDecision`、localization 和 trajectory variant；Application Service
  根据调查证据校验组合，再映射到冻结 Runtime。
- 数据搬运、格式转换、标识关联、参数精确传递、几何数据读写、状态迁移、并发控制、断点恢复、产物发布和机器可验证的校验全部由确定性系统实现。
- LLM 不作为 bbox、轨迹、绝对路径、内部 ID、标定内容或工具参数的中转存储，也不依赖自然语言复述保证这些数据的精确性。
- LLM 的计划必须由 Application Service 和 Runtime Adapter 校验后才能执行；未经系统确认的推断不得成为任务状态或资产状态事实。
- 手动页面和智能体入口只共享领域服务、任务引用和状态事件，不通过聊天上下文传输业务产物。
- M4 的模型审核是正式的辅助审核能力：模型读取由系统生成并绑定到目标 revision 的证据包，输出受 Schema 约束的问题、置信度和修正建议。模型可通过领域级修改指令生成独立的 `AIProposedFixRevision`，但不能原地修改父 revision、正式训练文件或审核状态；系统负责精确应用、校验、留痕和失效旧报告，人工仍是唯一最终审核者。

### No Business Behavior Change Without Explicit Approval

- 后处理业务基线为 `_01/run_odom.sh` 及其真实依赖。
- Fix 业务基线为现有 `run_fix.sh` 及其真实依赖。
- 不重写、简化、优化、省略或重新解释原业务算法、步骤和数值方法。
- 只允许增加 wrapper、参数传递、工作目录隔离、锁、状态、API、manifest 和前端入口。
- 路径参数化等机械改动必须与算法改动分离。
- 任何可能改变业务结果的修改必须单独提出并获得明确批准。
- 原始数据和 `clip_data` 同步产物不可被后处理或标注工作台原地修改。
- Golden 等价比较是迁移 Runtime、替换 GUI 和接入 Fix 的原定严格门禁。M1
  按用户批准偏差只完成状态级功能冻结，未宣称满足 artifact/数值等价；该偏差
  不适用于后续 Runtime、业务算法、Legacy YAML、M2 后处理或训练出口变更。

系统专用 Runtime 使用配置驱动的独立服务器目录。仓库保存 wrapper、manifest、比较器和兼容适配器；大型二进制及模型权重不直接提交 Git，但必须登记 SHA-256。未完成 Tracking 公共 scratch 隔离前，不开放并发 writer。

## 4. 核心架构

保持现有智能体边界：

```text
MainRouter
→ NavigationDataAgent
→ Annotation Application Service
→ Navigation Runtime Adapter / Annotation Store / Artifact Store
```

- MainRouter 继续负责普通文本入口、意图路由、任务委派和控制。
- NavigationDataAgent 继续负责导航领域调查、规划、执行和恢复。
- 不新增 AnnotationAgent。
- DataPilot 是所有数据处理任务的唯一所有者；普通 Web 页面不得直接决定或启动
  Tracking、gridmap、投影、轨迹和后处理。
- Web 首帧标注与 Fix 页面只承担人工输入；用户提交后由 durable handoff 唤醒
  原 Navigation Session 或其关联 Fix Task。
- M1 保留的直接创建 Job、开始 Tracking API 仅作为测试和运维兼容接口，M2
  普通产品 UI 不再暴露这些动作。
- 几何标注和轨迹编辑使用专用业务 API，不进入聊天消息或现有选项 Interaction。
- 前端始终只展示 DataPilot，不暴露内部 Agent、工具名、内部 ID、路径或多次 LLM 调用。

新增独立 `annotation.sqlite` 和独立 migration；不向 NavigationTask 状态机或 navigation schema 填入数据资产审核状态。

核心领域对象：

- `AnnotationJob`
- `AnnotationSegment`
- `InitialAnnotationRevision`
- `RuntimeRun`
- `ArtifactManifest`
- `CalibrationSnapshot`
- `TrajectoryRevision`
- `PostprocessingSpec`
- `FixRevision`
- `TrajectoryReviewTask`
- `FixCalibrationSnapshot`
- `ReviewDecision`
- `CompatibilityPublication`
- `WorkflowHandoff`
- `AnnotationTaskLink`
- `AIReviewReport`（M4，问题、证据和置信度）
- `AIProposedFixRevision`（M4，模型生成的隔离候选修正版）

用户和 Router 的范围始终是 `dataset_date + source clips`；同步产生的内部 sequence/segment 只作为标注工作台单元。

## 5. 状态与投影

执行状态与数据资产状态分离。

```text
AnnotationJob:
preparing
→ waiting_initial_annotation
→ tracking
→ tracked
→ postprocessing
→ annotated
→ failed / cancelled

TrajectoryReviewTask:
pending
→ in_progress
→ approved / returned / discarded
```

数据管理页展示的生命周期：

```text
已同步
处理中
待首帧标注
已标注 / 待轨迹复核
已验证
已退回
已废弃
处理失败
部分完成
```

已锁定的投影规则：

- 后处理完成、原始 trajectory 已发布：`已标注 / 待轨迹复核`。
- 人工批准当前 FixRevision：`已验证`。
- 同一天或同一外层 clip 的内部状态不一致：`部分完成`，同时展示各状态数量。
- 三维复核“退回”：返回 Fix 工作台，不自动重跑后处理。
- 已存在 `_trajectory_fix_five.json` 的历史数据：导入时直接映射为 `已验证`。
- `pass` 保留为原始兼容字段，不用于审核状态或默认训练过滤。

## 6. 标定规则

- 处理标定按数据日期由用户选择，页面不展示跨数据集固定推荐。
- 处理任务记录实际 profile、内容哈希、确认人和确认时间。
- Fix 标定独立选择，不覆盖原处理标定。
- Fix 记录原 trajectory 标定、Fix 标定、内容哈希和更换原因。
- `20260409_U` 只用于人工 Fix 阶段的可选参数，不自动应用到前面的投影链路。

## 7. 里程碑

### M0：契约与系统 Runtime 基线

交付：

- 权威路线、M0 任务计划和决策日志；
- `_01/run_odom.sh`、`run_fix.sh`、脚本、二进制、模型、配置和标定哈希 manifest；
- `navigation_odom_v1` 系统 Runtime 部署设计；
- Golden snapshot/comparator；
- 固定 Golden 样本和服务器验收门。

退出条件：业务依赖可审计，Golden 可重复比较，现有全量测试和 Router 基线无回归。

### M1：Web 首帧标注与 Tracking

交付：

- Annotation Store/Application Service 和手动任务入口；
- 处理标定选择；
- 基于 NoobScenes resize 后 `finish_temp` 首帧的 SVG 标注工作台；
- 与旧 `gen_box.py` 等价的 Legacy YAML Adapter；
- 原 Tracking 的安全调用和工作目录隔离。

原定严格退出条件：无需 XQuartz，可在 Web 完成首帧标注并获得与旧链路等价的
Tracking 结果。本次实际执行按用户批准偏差采用状态级功能冻结，严格 Golden
未执行，详见“实际进度与决策记录”的 M1 条目。

### M1.5：Tailwind 4 与 Radix/shadcn 设计系统基线

目标是把前端基础设施迁移与 M2 业务开发分离：

```text
Tailwind 3 → Tailwind 4
→ 以 Radix 作为 shadcn primitive 基础
→ 建立受审查的 components/ui
→ 全站视觉、交互、响应式和可访问性回归
→ 冻结设计系统基线
```

边界：

- M1 冻结基线继续使用 Tailwind 3 和现有 `Console*` 组件；M1.5 开始前不得把
  shadcn/Tailwind 4 生成脚手架或不兼容全局样式带入生产；
- shadcn MCP 只作为本地开发辅助，不部署到服务器；
- shadcn CLI 如需保留，只能作为 `devDependency`，不得成为生产运行依赖；
- 生成的组件源码进入仓库并逐项审查，不把 shadcn 当作不可控黑盒依赖；
- 优先复用仓库已有 Radix Dialog、Popover、ScrollArea 和 Tooltip，不再引入
  Base UI 形成第二套 primitive 体系；
- shadcn 只承担 Button、Dialog、Tabs、Table、Form、Alert、Badge 等无业务状态
  primitive；
- Annotation CAS、revision、Runtime、任务状态、轨迹审核等精确业务逻辑仍位于
  领域组件和 Application Service；
- 不实现 gridmap、投影、轨迹、Fix、数据管理联动、智能体接入或 AI 审核；
- M2 的三维轨迹/视频和 M4 证据查看器继续采用独立路由与懒加载，不并入主页面
  bundle。

退出条件：Tailwind 4 与 Radix/shadcn 依赖和生成策略明确，现有页面在全站视觉与
交互回归中无行为退化，设计系统基线冻结，服务器可通过确定性依赖安装和生产构建。

### M2：完整后处理、三维复核与 Fix

交付：

- 在修改 Prompt 和领域工具前建立 Navigation 最小评测入口，并只为自动标注、
  后处理和轨迹 Fix 补充必要的 Router case；
- DataPilot 从同步产物或已有 tracked Job 调查数据，选择并校验 gridmap、
  localization 和 trajectory variant，再完成原投影、速度、方向、轨迹和 final
  发布；
- 首帧 Web 工作台提交后的 durable handoff，以及后处理完成后的关联 Fix Task；
- 独立 TrajectoryReviewTask/FixJob；
- Web 三维轨迹复核/Fix；
- 独立 Fix 标定；
- FixRevision、通过、退回、废弃和训练出口。

后处理与 Fix 的任务边界：

```text
后处理完成
→ AnnotationJob = annotated
→ 冻结 TrajectoryRevision 并创建 pending ReviewTask
→ 原 Navigation 处理任务 completed，释放任务槽
→ DataPilot 询问是否继续 Fix
→ 用户确认后幂等创建关联 Fix Task
```

用户在最初请求中已经明确要求完整处理到 Fix 时，系统仍先结束处理任务，再自动
创建关联 Fix Task，不重复询问。用户暂不处理时，ReviewTask 长期保持 pending，
不占用任务槽。

自动标注模块保持一个侧栏入口，内部使用 URL 化页面：

```text
/annotation/jobs
/annotation/reviews
/annotation/reviews/{review_ref}
```

M2 的“人工复核”页只提供人工 Fix 入口，不显示“交给 DataPilot 复核”按钮；
该模型复核入口在 M4 与置信度和 AI 候选修正一起上线。

退出条件：DataPilot 可从同步或 tracked 事实安全执行到 annotated；用户无需
XQuartz 即可通过 Web 完成三维人工 Fix、批准并形成
`_trajectory_fix_five.json`；严格 Golden 和全量回归通过。

### M3：数据管理、仪表盘与跨页面状态整合

交付：

- 数据管理 ingestion 状态与 Annotation 生命周期联合投影；
- 仪表盘真实标注/复核统计；
- 日期和 clip 行的标注、结果、复核和已验证版本深链；
- annotation 状态缓存失效和显式刷新；
- 历史 `_trajectory_fix_five.json` 的受控批量导入；
- 同一天或同一外层 clip 的“部分完成”与数量投影。

退出条件：数据管理、仪表盘、标注工作台和人工复核页读取同一事实源，并能稳定
恢复到同一数据资产。

### M4：DataPilot/模型辅助复核与 AI 候选 Fix

交付：

- 独立 VisionReviewService 和 `VLA_AGENT_REVIEW_MODEL`；
- 三维轨迹/Fix 的受控视觉证据；
- 结构化 AIReviewReport，至少包含问题类型、目标、时间范围、证据引用、
  `issue_confidence` 和可选 `correction_confidence`；
- 模型针对有问题区间生成领域级修正指令，由确定性 Correction Service 校验并
  应用到新的 `AIProposedFixRevision`；
- AI 候选版本与父 revision 的位置、方向、速度、轨迹和证据对比；
- AI 建议侧栏、证据跳转，以及人工逐项接受、调整、拒绝和误报反馈；
- 在“人工复核”页上线“交给 DataPilot 复核”按钮；DataPilot 创建受控模型复核
  请求，不复用 M2 的人工 Fix 入口冒充模型能力；
- 独立真实模型评测基线。

硬边界：

- 模型不得直接覆盖 TrajectoryRevision、FixRevision 或
  `_trajectory_fix_five.json`；
- 模型输出只表达领域级 patch，不传递绝对路径，也不直接重写完整 JSON；
- 系统校验 revision 绑定、目标/帧范围、数值有限性、坐标系、连续性和物理约束；
  校验失败时不得生成候选版本；
- 每个 finding 和 correction 分别输出 0～1 置信度；在经过真实评测校准前，
  该值只表示模型自评，不能作为自动通过阈值；
- 目标 revision 或证据哈希变化后，旧报告和候选修正版自动失效；
- 模型可以 abstain，模型不可用或修正失败时自动降级为纯人工流程；
- 只有人工审核动作可以把候选修正纳入正式 FixRevision 并最终发布训练文件。

退出条件：AI 能发现高风险三维样本、生成可审阅的隔离修正候选并输出置信度；
人工可以接受、调整或拒绝，模型不可用时不阻塞流程，人工仍是唯一最终审核者。

## 8. 滚动开发与服务器门禁

每个里程碑统一执行：

```text
只读现状复核
→ 编写当期 decision-complete 计划
→ 分批实现
→ 单元/集成/前端测试
→ Golden 对比
→ 本地全量回归
→ 单独批准的服务器 dry-run
→ 单独批准的单 clip 真实验收
→ 修复并冻结
→ 再规划下一里程碑
```

服务器真实执行必须另行批准。部署前核对 commit、配置、数据库、Runtime manifest 和备份。发现业务数值变化、范围扩大、原始/同步产物被修改、公开路径泄漏或 revision 被覆盖时立即停止。

## 9. 里程碑实际结果

### M0（2026-07-23）

- 已保存总体路线、M0 决策和 LLM/确定性系统职责边界；
- 已只读冻结 `_01/run_odom.sh`、`run_fix.sh`、活动脚本、Tracking 二进制、
  ONNX、三套标定，以及活动 Data Runtime 的 setup、Python 3.8.10 和直接包
  版本；系统共享库/GPU 等仍作为外部条件登记，共 85 个 manifest 条目；
- 已建立 manifest schema/只读 verifier 和 Golden snapshot/comparator；
- 已登记两个历史完整样本和一个 20260714 缺 gridmap 同步输入样本；
- Python 全量 1194 项、前端 150 项、生产构建和 DataPilot v1 eval schema
  均通过；
- 未修改业务算法、现有 Navigation 执行入口、Router/Navigation Prompt 或
  eval case，未对服务器产生写入。

系统专用 Runtime 的服务器 payload 部署、部署环境与已绑定 Python/包版本的
复核、真实 Golden capture 和单 clip 业务验收仍受独立服务器门禁约束，不由
M0 的只读授权自动放行。

真实 writer/Golden 使用 `20270605`、`20270623` 下的 raw 测试副本；这些测试
日期需要先单独批准并完成拆解、同步。原日期 `20260605`、`20260623` 的既有
同步和 finish 产物由同事使用正式服务器脚本生成，作为只读 legacy oracle，不
重新运行旧脚本，也不作为写入目标。新系统只写测试日期，并将 candidate 与对应
原日期 oracle 比较。比较前必须证明 raw 同源、同步产物等价并建立稳定的
clip/segment 映射；只归一化明确登记的日期/root 等非业务差异。验收范围按外层
clip 确定，必须覆盖其同步后产生的全部内部 segments；历史单个 segment 的比较
case 不能替代整条 clip 的真实业务等价验收。来自 macOS 复制的 `._*`
AppleDouble 文件不属于业务输入，拆包发现与 raw 同源比较均须忽略并报告，不能
把它们当作 ROS bag。

### M1（2026-07-27，功能冻结）

- 已完成任务级设计、本地实现和独立代码审计，权威记录为
  `docs/automatic-annotation-m1-plan.md`；
- 开发基线固定为 `f618c6c`，开发分支为
  `codex/automatic-annotation-m1`；
- M1 只实现 `navigation_odom_v1` 的 Web 首帧标注与 Tracking，不提前接入
  M2 后处理/Fix、M3 智能体或 M4 AI；
- 本地最终门禁为 Python `1525 passed`、前端 `214 passed`、Playwright
  `7 passed`、Golden `73 passed`、Router suite `17 cases validated`，前端
  production build、compileall 和 diff-check 均通过；
- Golden 的 M1 必需范围已覆盖两个门禁 clip 的 `maps/`、`v1.0-trainval/`
  和全部内部 segments，不允许 candidate 自报路径、宽泛 ignore、非零 tolerance
  或未登记 normalization；
- 2027 测试数据拆解/同步已于 2026-07-24 通过服务器验收：raw、`tmp_dir`
  及同步阶段的图像、点云、odom 和时间元数据均与 2026 来源严格一致；历史
  `grid_map` 是后处理阶段差异，不属于同步或 M1；
- Legacy YAML 中写死的 `/mnt/data1/.../Data` 已锁定为 sandbox-only
  compatibility target：只在每个任务的 bubblewrap mount namespace 中创建，
  不要求或修改宿主兼容目录；服务器 bubblewrap 0.4.0 无业务 smoke 已验证写入
  只落入临时 job-private Data，测试前后宿主 `/mnt/data1` 均不存在；
- 系统 Runtime payload 已部署并验证；固定 Xvfb 已安装并由五项真实安装证据
  attestation，Xvfb＋bubblewrap DISPLAY 与沙箱内 GPU 无业务 smoke 已通过；
  服务器完整 Runtime capability 和无业务超时/进程组清理 smoke 也已通过；
- `20270605 / 20260605_160904` 已通过 Web 完成首帧标注和 Tracking，最终
  `tracked` 1/1；`20270623 / 20260623_145550` 最终 `tracked` 6/6；
- `152930` 已覆盖刷新、CAS/409、唯一 revision 并发提交、运行中取消和 scope
  释放；公开状态投影和持久中文提示已完成返修并由用户复核；
- 用户明确把本轮真实产物验收收窄为对应 clip/segment 状态一致，因此 11 个
  Store-bound Golden case 未针对真实 candidate/oracle 执行。本次只做 M1
  功能冻结，不声明 artifact 级数值等价；未来业务 Runtime、算法、Legacy YAML、
  M2 后处理或训练出口仍受严格 Golden 门禁；
- 历史 `map.png` 的 1×1 兼容形式已由业务同事确认，不能据此扩大任何 ignore
  或 normalization；
- 最终服务器只读审计确认两个 tracked Job 的 DB/Runtime/manifest/checkpoint
  账本自洽、无活动 lease/marker/子进程、未发现公开路径或内部信息泄漏，也未
  发现测试日期 raw/clip、历史 oracle、公共 scratch 的 M1 后写入；由于 writer
  前无独立污染 fingerprint，该结论受 mtime/当前结构证据边界约束；
- 服务器私有 `web.log` 有两条不经 API/UI 暴露的第三方 WebSocket 弃用告警，
  其中包含 Python 包源码路径；公开响应零路径门禁通过，但不声称所有私有日志
  绝对零路径；
- 功能冻结代码提交为 `01f57b6`；随后的收尾提交只补录部署事实，不改变构建
  产物。本地与服务器工作树均干净；服务器用 nvm Node `20.20.2` / npm
  `10.8.2` 构建重启后，前端深链接和 Annotation capability 均为 200，Runtime
  `available=true`。非交互 shell 默认 Node `10.19.0` 的陷阱由 M1.5 的 Node
  版本与确定性安装契约解决；
- M1.5 只进行前端基础设施迁移，不得修改本次冻结的 Annotation/Tracking 业务
  契约。

### M1.5（2026-07-27～2026-07-28，完整冻结）

- 开发基线固定为 `a7315ca`，开发分支为
  `codex/automatic-annotation-m1-5`；
- 权威任务级计划为 `docs/automatic-annotation-m1-5-plan.md`；
- 已固定 Node `24.18.0` / npm `11.16.0`，`run_web.sh` 在构建前精确校验
  工具链，并支持非交互部署显式绑定用户级 Node `bin`；
- Tailwind 已迁移到 `4.3.3` 的 Vite plugin 构建路径；旧直接
  PostCSS/Autoprefixer 配置和 Tailwind JavaScript config 已移除。依赖树中的
  PostCSS 仅为 Vite/Vitest 的传递开发依赖，不属于旧生产构建路径；
- 已建立 Radix＋Nova 的 Button、Badge、Alert、Progress、Dialog primitive，
  现有 Console 组件通过兼容适配层复用；生产依赖不含 shadcn CLI、Base UI、
  React Aria 或新增字体；
- 六个 Console 页面均采用路由级懒加载，AppShell、Sidebar 和 DataPilot
  继续同步加载；最大 JavaScript chunk 为 `376304` 字节，低于 `512000`
  字节构建门禁；
- 已在 `1440×900`、`1024×768` 和 `390×844` 检查六个主路由，并修复 Agent
  工作流手机端 grid 的横向溢出；手机端 DataPilot 浮窗保持非模态且完整位于
  视口内；
- 本地最终门禁为 Python `1530 passed`、前端 `218 passed`、Playwright
  `7 passed`、Router suite `17 cases validated`；production build、
  compileall 和 diff-check 均通过；
- 生产依赖审计没有 high/critical，仍有 React Router 6 的 2 个 moderate
  公告；自动修复会升级到不兼容的 Router 7，因此不在 M1.5 强制升级；
- 前端主体提交 `a7315ca..17325f9` 中，Annotation 领域文件只有 Tailwind
  utility 的机械迁移与 Dialog import 合并，API、CAS、revision、Runtime、
  Router 和后端均未修改，也未运行真实 Tracking 或服务器 writer；
- 服务器已从干净的 `a7315ca` 切换到 `17325f9`，旧 `dist` 和 lockfile 已保留
  为可回退副本；服务器与本地候选 `dist` tree hash 一致；
- 用户级 Node `24.18.0` / npm `11.16.0` 安装完成，未替换系统 Node 10，也
  未改变账号原有 nvm 默认 Node 20；M1.5 只通过显式路径绑定 Node 24。
  `npm ci`、前端 `218 passed`、production build 和 bundle gate 均通过；
- `run_web.sh` 的精确版本预检已在真实服务器构建中通过。未加载既有 M1 Runtime
  配置时 capability 正确 fail closed；加载冻结配置后重启，
  `navigation_odom_v1 available=true`；
- 八条显式 SPA 路由、全部 lazy assets、历史 tracked Job/Segment、首帧响应、
  数据摘要、会话历史和 DataPilot 非模态浮窗均通过只读 smoke；未知路由保持
  404，浏览器控制台无错误；
- 重启和浏览器验收前后，Annotation、Session 和 Navigation 三个 SQLite 主文件
  hash 完全一致；仍为 9 个 cancelled Job、2 个 tracked Job、9 条
  `status=active` Session 记录，但无 running/waiting turn、非终态 task 或
  Runtime/Session lease。服务日志没有 POST/PUT/DELETE；
- 扩大后的公开响应扫描发现一项早于 M1.5 的问题：
  `/api/navigation/datasets/summary` 中 20260403 的 `recordings` clip
  `errors[0]` 含绝对路径。M1.5 没有任何后端 diff，回滚不能消除该问题；
  Annotation、Session 与本次其他抽查响应未发现同类泄漏；
- 该既有问题已在独立提交
  `c75712eda79c211d42f766d7dfd736611e57634c` 中闭环：异常原文改为稳定公开
  文案，不改变 schema、状态、计数、扫描或处理逻辑。最终本地门禁为 targeted
  `49 passed`、Python `1536 passed`、前端 `218 passed`、Playwright
  `7 passed`、Router `17 cases validated`；
- 2026-07-28 服务器只读复核确认 `/summary` 与 `/20260403` 的目标 clip
  仍为 `status=error`、`error_count=1`，但不再含绝对路径；扩大抽查的
  Navigation、Annotation、Job 和 Session 响应无路径或凭据标记，三个数据库
  hash 未变，Runtime capability 仍为 true，服务和工作树正常；
- M1.5 至此完整冻结。相邻的独立公开 DTO、字段感知输入净化和前端第二道错误
  脱敏属于后续纵深防御候选，不扩大为 M1.5 或 M2 的隐含范围。

### M2（2026-07-28～2026-07-31，完整冻结）

- 开发基线为 M1.5 冻结提交 `a2d4ccd`，开发分支为
  `codex/automatic-annotation-m2`；
- 权威任务级计划为 `docs/automatic-annotation-m2-plan.md`；
- 已锁定 DataPilot 为处理任务唯一所有者，Web 只提供快捷入口和人工工作台，
  不建设第二条手动后处理流水线；
- Navigation 最小评测、领域工具、durable handoff 和 linked Fix Task 从原 M3
  前移到 M2；M3 收窄为数据管理、仪表盘和跨页面状态整合；
- M2 的人工复核页不提供模型复核按钮；DataPilot/模型辅助复核、置信度和
  `AIProposedFixRevision` 保留到 M4；
- 本地已经完成 Annotation schema v8、Navigation Plan v4 领域迁移、后处理、
  人工 Fix、异步兼容发布、durable handoff、linked Fix Task、M2 前端和最小
  评测实现；
- processing owner 唯一约束、精确 clip scope 复用、迁移完整性安全标记、后处理
  writer 锁边界、既有 tracked Job owner 绑定，以及 linked Fix 两阶段和 Redis
  原子幂等恢复均已补充回归；
- 本地门禁为 Python `1636 passed`、Annotation `284 passed`、前端
  `238 passed, 8 skipped`、Playwright 全量 `9 passed`、production build 通过，
  三套评测分别验证 `4 / 17 / 7` 个 case schema；
- 正式迁移前，服务器旧 Navigation `final-v2` 真实库已完成只读副本迁移演练：
  7 个 task、
  6 个 Plan、24 个 step、53 个 observation、53 个 evidence 和 10 个
  submission attempt 除 generation marker 外逐行不变，外键、完整性和安全
  标记均通过；后续正式停机迁移已完成；
- 服务器已完成 Annotation v8 与 Navigation M2 正式迁移、真实
  `20270623 / 20260623_145550` 六 Segment 的 prepare、Web 首帧标注、Tracking、
  `generate_from_pcd`、odom 后处理、三维人工 Fix/复核和兼容发布；
- 最终复核为 5 个批准、1 个废弃，5 个批准单元均发布
  `*_trajectory_fix_five.json`；任务、handoff、revision、publication 和公开状态
  闭环一致；
- 最终本地门禁为 Python `1763 passed, 1 warning`、前端
  `267 passed, 8 skipped`、Playwright `10 passed`、production build/bundle gate
  通过，`datapilot-v1/navigation-m2` 分别验证 `17/7` 个 case schema；
- 本次因新旧首帧框、目标与服饰颜色来自不同人工输入，经用户批准按文件树、
  Schema、数量、Runtime 决策、状态、revision、发布和无污染完成 Golden；不宣称
  不同人工输入的数值/图片哈希严格相等。未来 Runtime 或算法变更仍需相同输入的
  严格 Golden；
- 两条早期失败验收遗留的 `waiting_user` 任务已在停机备份后通过既有 Task Store
  转为 `cancelled` 并关闭任务槽，未删除历史账本；完整性、外键、服务启动和
  Runtime capability 均复核通过；
- 生产隔离策略的保留与可收缩边界已冻结在
  `docs/automatic-annotation-m2-plan.md` 第 18.4 节；
- M2 至此完整冻结。M3 暂不启动；下一阶段先独立进行小功能、智能体模块和前端
  UI/交互优化，不能借此扩大或改写 M3/M4 范围。
