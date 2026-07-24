# 自动标注板块总体开发路线

> 状态：已批准，M0 本地基线与活动 Python Runtime 绑定已完成；正式 Runtime 冻结待服务器部署和 Golden 门  
> 最后更新：2026-07-23  
> 适用范围：导航数据自动标注、后处理、三维轨迹复核/Fix，以及后续可复用的标注领域能力  
> 优先级：本文件在自动标注范围内优先于 `architecture.md` 中的历史占位描述

## 1. 总体策略

采用“总体路线冻结、里程碑滚动细化”的开发方式：

```text
M0 契约与 Runtime 基线
→ M1 Web 首帧标注与 Tracking
→ M2 完整后处理与三维复核/Fix
→ M3 数据管理、仪表盘与智能体接入
→ M4 三维 AI 辅助复核
```

进入每个里程碑前，先根据仓库、服务器和上一个里程碑的实际结果建立当期任务级计划：

```text
docs/automatic-annotation-m0-plan.md
docs/automatic-annotation-m1-plan.md
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
→ 缺少输入 gridmap 时在 staging 中先运行 pcd_to_grid
→ 投影、世界坐标、速度和方向
→ 按原顺序执行 cp_gridmap transform
→ _0525 轨迹
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
- Golden 等价比较是迁移 Runtime、替换 GUI 和接入 Fix 的硬门禁。

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
- 手动页面和智能体最终调用同一个 Annotation Application Service。
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
- `FixRevision`
- `TrajectoryReviewTask`
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

退出条件：无需 XQuartz，可在 Web 完成首帧标注并获得与旧链路等价的 Tracking 结果。

### M2：完整后处理、三维复核与 Fix

交付：

- 从 Tracking 继续完成原 gridmap、投影、速度、方向、轨迹和 final 发布；
- 独立 TrajectoryReviewTask/FixJob；
- Web 三维轨迹复核/Fix；
- 独立 Fix 标定；
- FixRevision、通过、退回、废弃和训练出口。

退出条件：同步产物可通过 Web 形成经人工批准的 `_trajectory_fix_five.json`。

### M3：数据管理、仪表盘与智能体接入

交付：

- 数据管理 ingestion 状态与 Annotation 生命周期联合投影；
- 仪表盘真实标注/复核统计；
- Navigation 最小评测入口；
- durable Web 工作台 handoff；
- NavigationDataAgent 的领域级标注/复核工具；
- Router 对新增产品意图的小范围评测补充。

退出条件：手动入口与聊天入口操作同一种领域任务，前端继续保持单一 DataPilot 体验。

### M4：三维 AI 辅助复核

交付：

- 独立 VisionReviewService 和 `VLA_AGENT_REVIEW_MODEL`；
- 三维轨迹/Fix 的受控视觉证据；
- 结构化 AIReviewReport，至少包含问题类型、目标、时间范围、证据引用、
  `issue_confidence` 和可选 `correction_confidence`；
- 模型针对有问题区间生成领域级修正指令，由确定性 Correction Service 校验并
  应用到新的 `AIProposedFixRevision`；
- AI 候选版本与父 revision 的位置、方向、速度、轨迹和证据对比；
- AI 建议侧栏、证据跳转，以及人工逐项接受、调整、拒绝和误报反馈；
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

### M1（2026-07-23，本地实现与回归完成，待服务器验收）

- 已完成任务级设计、本地实现和独立代码审计，权威记录为
  `docs/automatic-annotation-m1-plan.md`；
- 开发基线固定为 `f618c6c`，开发分支为
  `codex/automatic-annotation-m1`；
- M1 只实现 `navigation_odom_v1` 的 Web 首帧标注与 Tracking，不提前接入
  M2 后处理/Fix、M3 智能体或 M4 AI；
- 本地门禁为 Python `1513 passed`、前端 `204 passed`、Playwright
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
  attestation，Xvfb＋bubblewrap DISPLAY 与沙箱内 GPU 无业务 smoke 已通过。
  真实 Tracking 仍受单独服务器 writer 门禁约束；尚无 candidate/oracle 差异
  结论，因此 M1 尚未冻结，也不能进入 M2。
