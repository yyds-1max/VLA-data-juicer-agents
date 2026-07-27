# 自动标注 M1：Web 首帧标注与 Tracking 开发计划

> 状态：已完成并冻结（2026-07-27）；真实 Web/Tracking 功能验收通过，严格
> Store-bound Golden 未执行，冻结结论不包含 artifact 级数值等价声明
> 基线提交：`f618c6c`  
> 开发分支：`codex/automatic-annotation-m1`  
> 上位路线：`docs/automatic-annotation-roadmap.md`

## 1. 目标与边界

M1 从已同步的导航数据开始，仅实现冻结的 `navigation_odom_v1`：

```text
当天处理标定
→ job-private staging
→ odom_to_ins / resize / metadata / video
→ Web 首帧标注
→ Legacy YAML
→ 原 Tracking
→ tracked
```

M1 不执行拆解、同步、gridmap、投影、轨迹、final、Fix、二维复核或 AI，也不
修改 Router、NavigationDataAgent、NavigationTask schema 和 DataPilot 单一
智能体契约。Ins 或混合 Runtime 在 M1 必须 fail closed。

## 2. 领域与持久化

新增独立 `annotation.sqlite`、独立 migration ledger 和 Annotation
Application Service。核心持久化对象为：

- `annotation_jobs`、`annotation_job_source_clips`、`annotation_source_leases`
- `annotation_segments`
- `initial_annotation_drafts`、不可变的 `initial_annotation_revisions`
- 不可变的 `calibration_snapshots`
- `runtime_runs`、`runtime_run_steps`、`runtime_leases`
- 不可变的 `artifact_manifests`
- `annotation_segment_actions`、`annotation_mutation_receipts`

数据库启用 foreign keys、WAL 和 busy timeout；状态写入使用
`BEGIN IMMEDIATE` 与 revision CAS。数据库版本超前、迁移断档或不可变记录被
修改时拒绝继续。

状态固定为：

```text
Job:
preparing
→ waiting_initial_annotation
→ tracking
→ tracked
→ failed / cancelled

Segment:
pending_initial_annotation / draft
→ submitted
→ tracking
→ tracked

等待阶段可转 skipped
```

同一 `dataset_date + source clip` 只有一个权威 Job。`failed`、`tracked`
继续占用范围，`cancelled` 释放范围。skip 只作用于内部 segment；部分 skip
继续处理其余 segment；全部 skip 时通过“无可处理目标”动作转为
`cancelled + completion_outcome=no_processable_targets`，不执行 Tracking。

## 3. 标注契约

首帧严格取 NoobScenes resize 后 `fisheye_front` 中排序后的首个 `.jpg` 或
`.png`。草稿可以不完整，正式提交必须满足：

- 至少一个目标；首个为 `master`，后续依次为 `other1…otherN`；
- bbox 为整数 `[x, y, width, height]`，point 为整数 `[x, y]`；
- 坐标位于 resize 后图像范围内；
- 不增加 point 必须位于 bbox 内或 bbox 最小面积质量门；
- 上衣、裤子、鞋子颜色均显式选择；
- 颜色词表严格为旧工具的 14 项。

草稿使用 working copy 和乐观锁；提交时生成新的不可变
`InitialAnnotationRevision`。服务端 `LegacyYamlAdapter` 负责旧文件名、路径
和 YAML 字段，浏览器不能传递路径、文件名或脚本参数。

## 4. API

基础路径为 `/api/annotation`：

```text
GET  /capabilities
GET  /calibration-profiles?domain=navigation&purpose=processing

POST /jobs
GET  /jobs
GET  /jobs/{job_ref}
POST /jobs/{job_ref}/tracking
POST /jobs/{job_ref}/complete-no-processable-targets
POST /jobs/{job_ref}/cancel
POST /jobs/{job_ref}/retry

GET  /jobs/{job_ref}/segments/{segment_ref}
GET  /jobs/{job_ref}/segments/{segment_ref}/first-frame
PUT  /jobs/{job_ref}/segments/{segment_ref}/draft
POST /jobs/{job_ref}/segments/{segment_ref}/reopen
POST /jobs/{job_ref}/segments/{segment_ref}/submit
POST /jobs/{job_ref}/segments/{segment_ref}/skip
POST /jobs/{job_ref}/segments/{segment_ref}/unskip
```

所有 mutation 必须携带 `Idempotency-Key` 和 expected revision。同 key 同请求
返回原结果，同 key 不同请求返回 409。公开响应只使用随机 opaque refs，不返回
内部 sequence、数据库 ID、绝对路径、命令、工具名或脚本参数。

M1 采用可信内网模式，只记录 `manual_web`、部署实例和时间，不声称已认证到
个人。处理标定列表只包含 manifest 中登记的 `20260320` 与
`20260529_go2w`；`20260409_U` 留给 M2 Fix，页面不显示全局推荐。

## 5. Runtime 与恢复

Runtime 边界固定为：

```text
NavigationAnnotationRuntimeAdapter.prepare(...)
LegacyYamlAdapter.render(...)
NavigationTrackingRuntime.track(...)
```

- 每个 prepare attempt 使用全新 staging；拒绝残留目录、symlink 和特殊文件。
- 同步输入只允许 byte-copy、reflink 或真正 CoW，禁止 hardlink。
- `2_resize.py` 只修改 staging，绝不修改 `clip_data`。
- 保持冻结的脚本、Tracking binary、ONNX、命令顺序和数值逻辑。
- 使用 bubblewrap 将冻结 Runtime 只读挂载，并把 job-private `Data` 映射到
  Tracking 的 legacy 绝对路径；`/mnt/data1/.../Data` 只在私有 mount namespace
  中生成，不在宿主机创建兼容目录。
- 使用固定 Xvfb 无界面运行，不回退 XQuartz。
- Annotation DB lease 防止重复 claim；系统级 `fcntl` writer lock 同时约束
  Annotation Worker 和现有 Navigation plan execution。
- writer 协调把运行所有权与恢复隔离分开：`<lock>.active` 只由当前 writer
  持有并在确认子进程退出后删除；每个未知副作用事件创建独立、append-only 的
  `<lock>.quarantine.<opaque-ref>`。任一标记存在时，Navigation 与 Annotation
  writer 都 fail closed。
- 等待 Web 标注期间不持有 writer lock。
- 每个 target 完成后提交 checkpoint；retry 只复用哈希仍匹配的完成项。
- 未知副作用转 `failed/recovery_required`，并全局停止所有 Annotation queued
  claim。恢复必须先完成“全部 Navigation/Annotation writer 进程组已消失”的
  全局两阶段审计，再用其 completed action ref 对单个 Job 执行 retry/abandon；
  普通 retry/cancel 不得绕过。全局 action 还要绑定每个 marker 的精确摘要：
  completion 后清理到一半时，同 action 只可补删原集合的剩余子集；现场一旦
  出现新 marker，旧 action 必须零删除并要求重新全局确认。
- cancel 继续使用独立进程组 SIGTERM→SIGKILL，并保留 staging 与审计。

生产 writer 必须显式配置专用 `VLA_ANNOTATION_WORK_ROOT`。任务创建和 Worker
执行前均进行容量预留与 free-space preflight；不足时 fail closed。M1 不提供
自动清理或删除，`tracked` staging 保留给 M2。

## 6. Golden v2

Golden v2 分别声明 reference/candidate 日期、clip、内部 segment 和 artifact
scope，并保持 v1 可读：

- 日期和 artifact root 只通过 reference/candidate 各自登记的 filesystem role
  scope 做身份映射；结构化文档当前只归一化精确登记的
  `paths.img2video_mp4` selector。真实产物若出现其他嵌入式日期/root，保持
  `DIFFERENT` 并先报告，不预设宽泛替换；
- `tracking_img_*/*` 比较文件树、数量、格式和尺寸，不比较动态 duration/fps
  叠字导致的不稳定 JPEG hash；
- YAML、`img_*.txt`、其他图片、结构与数值继续严格比较；
- 每个门禁除逐 segment case 外，还必须分别比较 staging 根的 `maps/` 与
  `v1.0-trainval/`；`.runtime/` 只受 prepare manifest hash 约束，不作为业务
  oracle scope。Store 拒绝任何额外或缺失的顶层业务 scope，不能只跑 segment
  case；
- candidate 的命令顺序、Runtime manifest、CalibrationSnapshot 和 annotation
  revision-set hash 必须来自 `RuntimeRun`；
- 历史 reference 标记 `historical_unattested`。

真实 candidate 与 2026 oracle 出现任何非白名单差异时立即停止验收，生成包含
相对文件、字段/数值 selector、所属阶段和可疑原因的差异报告。未经业务同事确认，
不得为了通过 Golden 修改业务逻辑、扩大 tolerance、增加 ignore 或新增
normalization。

主门禁为 `20270605_160904 ↔ 20260605_160904`；第二门禁覆盖
`20270623_145550` 的全部六个内部 segments。`152930` 用于 Web/recovery
smoke；`152856` 因历史尺寸不符合当前 resize 链，仅作 provenance 诊断。

## 7. Web

引入 URL Router：

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

自动标注 fixture 全部替换为真实 Jobs、Job 和 Segment 页面。Segment 工作台
采用队列、原图＋SVG、目标属性三栏布局，支持 bbox 创建/移动/八向缩放、point
移动、缩放/平移、数字输入和键盘微调。SVG 使用图像原始像素 viewBox。

自动保存请求串行；切换 segment 前等待保存；409 暂停自动保存并要求用户选择
服务器版本或基于最新 revision 重新保存。图片 natural size 不匹配时禁止编辑。
全部 segment resolved 后由用户显式点击“开始 Tracking”。

## 8. 实施和退出门

开发批次：

1. Annotation schema、Store、状态机、refs、幂等和 CalibrationSnapshot；
2. Golden v2、共享 writer lock、Runtime preflight、prepare 和 Legacy YAML；
3. API、first-frame 文件安全、Worker、取消、恢复和 Tracking sandbox；
4. 前端 Router、Jobs/Job/Segment 工作台；
5. fake-runtime 集成、全量回归和服务器验收。

服务器 writer 必须另行批准。验收先运行 `20270605_160904`，容量复核后再运行
`20270623_145550` 六个 segments；2026 原产物始终只读。固定 Xvfb 安装、
系统 Runtime payload 部署和 2027 测试日期拆解/同步均是服务器验收前置门。

原始退出门要求无需 XQuartz、Web 可完成全部首帧标注、Tracking 与 legacy
等价、刷新/取消/恢复可靠、原始及同步数据未被修改且全量回归通过。实际服务器
验收期间，用户明确把本轮真实产物判定收窄为对应 clip/segment 的状态一致，不再
要求执行 11 个 Store-bound Golden case；RuntimeRun、checkpoint 和 manifest
账本只作为补充观察证据。因此本次 M1 采用“功能冻结”结论：不声称 candidate
与历史 oracle 已通过 artifact 级数值等价；未来修改 Tracking Runtime、业务
算法、Legacy YAML 或进入依赖这些数值的发布门时，严格 Golden 仍是硬门禁，
不能由本次状态级验收替代。

## 9. 实施与验收结果（截至 2026-07-27）

M1 的本地代码实现、fake-runtime 集成和独立代码审计已经完成：

- Annotation Store、Application Service、公开 API、Worker、Runtime adapter、
  Legacy YAML、取消/恢复和跨 Navigation/Annotation writer 隔离均已落地；
- Web Jobs、Job、Segment 工作台和 URL 恢复已替换原自动标注 fixture；
- Golden registry 共 16 个 case，其中 M1 必需的 11 个 scope 覆盖
  `20270605_160904` 的 maps、metadata、segment，以及
  `20270623_145550` 的 maps、metadata 和全部六个 segments；
- Golden 生产入口只接受 `run_ref`，candidate 根与 scope 必须由
  AnnotationStore 派生；根级 CLI 等价、差异、输出隔离和脱敏均有端到端测试；
- 健康 writer、陈旧 active marker、durable quarantine 三态，以及 completion
  后部分 marker 清理的安全重放均有故障注入测试。
- 服务器 preflight 后补齐了 Tracking 两份直接配置的 manifest 冻结、binary/
  legacy YAML/`1_odom_convert.py` 内嵌路径证明、双 overlay 精确绑定，以及复制后
  SHA/size 复核；Legacy YAML overlay 由 bubblewrap 在任务 namespace 中创建，
  不产生宿主目录副作用；显式容量余量缺失时 Runtime fail closed；
- Web 与停机恢复 CLI 共用 Annotation service/maintenance flock。恢复 CLI
  精确绑定生产 DB 与 writer lock，只以 existing-only 模式打开 DB，拒绝在线
  Worker、错误 scope、DB inode 轮换和新 quarantine 竞态；
- Tracking 输出开始发布后，文件移动、权限收口、哈希、checkpoint、step 和终态
  manifest 共同构成恢复边界；任一账本状态不确定时保持 source lease 并转入
  `recovery_required`，并发取消不能绕过停机 operator 确认；
- `run_web.sh` 已统一有效 working directory，私有创建 state/working/log 目录；
  启动配置冲突 fail closed，但 `stop/status/logs` 不受阻断。

本地最终门禁：

```text
Python 全量                  1525 passed
Runtime targeted             125 passed
Web/Store/恢复 targeted      174 passed
run_web targeted              25 passed
前端 Vitest                  214 passed
前端 Playwright mock E2E     7 passed
前端 production build        PASS
Golden 全套                  73 passed
Golden registry              16 cases validated
DataPilot Router suite       17 cases validated
compileall / git diff --check PASS
独立代码审计                 PASS（含服务器 preflight 后 hardening）
```

2026-07-24 已完成服务器端 2027 测试副本的数据准备验收：

- `20270605` 下的 `152856`、`152930`、`160904` 以及 `20270623` 下的
  `145550` 均已通过现有系统完成拆解、同步；
- 四个 raw DB3 与对应 2026 来源逐文件 SHA-256 一致，`tmp_dir` 文件树、大小和
  SHA-256 一致；
- `sync_data` 中的图像、点云、odom 和时间元数据逐文件 SHA-256 一致，
  `145550` 的全部六个内部 segments 均已覆盖；
- 2027 同步候选不含历史 2026 目录中的 `grid_map`。这是预期阶段差异：
  `grid_map` 属于缺失时由后处理生成的产物，不属于拆解/同步，也不进入 M1；
- 2027 `finish_data` 尚未产生，raw/clip 范围内未发现 symlink 或特殊文件，
  M1 边界保持成立。

因此“测试数据拆解/同步”前置门已通过。对应 Navigation 任务仍停在已接受的
`extract_sync` 阶段边界，需由用户明确选择“到这里结束”后正常关闭；不能把其
等待状态误判为拆解/同步失败，也不能用取消整任务代替正常完成。

服务器 Runtime payload 已按 manifest 仅部署 55 个 frozen files，源端与部署端
均通过 SHA-256、大小和 executable bit 校验；活动 Data Runtime 的 10 个包版本、
GPU 和 bubblewrap 已复核。Legacy YAML sandbox-only target 的无业务 smoke 也已
通过，未创建宿主 `/mnt/data1`。2026-07-24 又安装并登记了固定 Xvfb，捕获五项
安装证据，并通过 Xvfb、bubblewrap 内 DISPLAY 和沙箱内 GPU 的无业务 smoke；
系统依赖摘要现在会逐包核对实际 dpkg 版本。提交 `896673f` 同步后，服务器以
完整正式配置执行的 Runtime capability 返回 `available=true`，无业务超时与
后代进程组清理 smoke 也通过。

真实 M1 writer 与 Web 验收结果：

- `20270605 / 20260605_160904` 最终为 `tracked`，1/1 segment 完成；
- `20270623 / 20260623_145550` 最终为 `tracked`，6/6 segments 完成；
- 两个任务的 prepare、Tracking run、checkpoint 和终态账本均已落库；
- 全程不依赖 XQuartz，首帧标注、刷新恢复、显式开始 Tracking 和只读结果页
  均通过人工验收；
- `20270605 / 20260605_152930` 覆盖草稿刷新、两标签页 revision CAS、409
  显式版本选择、另一页面先提交时只保留一个不可变 revision、运行中取消，以及
  取消后重新创建/释放 scope；
- 另一页面先提交时，页面持续显示“已在其他页面完成提交。本页内容未再次提交，
  现已切换到服务器版本。”，并切换为只读；服务器审计确认只有一次
  `submitted` 动作和一个正式 revision；
- 取消中的原始 `tracking` segment 作为私有审计事实保留；公开投影回到
  `submitted`，不会把已取消 Job 继续显示为运行中；
- 历史 `map.png` 的 1×1 形式由业务同事确认为当前兼容口径，且该文件在后续
  业务中基本不消费；这项确认不能推广为忽略其他图片或数值差异。

最终服务器只读审计确认：

- Annotation DB `quick_check=ok`；两个 tracked Job 的 run、step、checkpoint、
  revision 和 manifest 账本自洽，无活动 Runtime lease、marker 或残留
  Xvfb/bubblewrap/Tracking 进程；
- 2027 两个测试日期没有 `finish_data`，raw/clip 当前普通文件的最新 mtime 均
  早于首个 M1 writer，且未发现 symlink 或特殊文件；系统 work root 的 11 个
  Job 目录与 DB 记录一一对应；
- frozen Runtime 的 55 个文件和只读业务源目录中的对应文件均通过 manifest
  校验；公共 Tracking scratch 与历史 oracle 的当前最新 mtime 均早于 M1；
- 抽查公开成功响应、首帧响应头和错误响应，未发现绝对路径、内部数据库键、
  内部 segment 名、脚本、工具或参数泄漏；服务器私有 `web.log` 中只有两条
  第三方 WebSocket 弃用告警包含 Python 包源码路径，日志没有 API/UI 路由，
  不属于公开响应；
- frozen Runtime 与 annotation work root 无 group/other writable 项和 symlink，
  Annotation DB 权限为 `0600`。

由于 writer 前没有独立保存污染 fingerprint，上述 mtime、结构和 manifest 证据
只能证明“未发现 M1 后写入”，不能数学证明不存在删除或保留 mtime 的替换。
私有日志也不满足“所有私有日志绝对零路径”的更强口径；若未来采用该口径，应
通过依赖升级或精确告警策略处理，不得用宽泛日志脱敏破坏诊断信息。

按用户批准的最终验收口径，本轮只要求对应 clip/segment 状态一致。11 个已登记
的 Store-bound Golden case 没有针对真实 candidate/oracle 执行，故不得把本次
冻结描述为“Tracking 与 legacy artifact 全量等价”。`recovery_required` 的
故障注入、checkpoint 校验和停机恢复由本地测试覆盖；服务器没有为了验收而主动
制造未知副作用事件。
