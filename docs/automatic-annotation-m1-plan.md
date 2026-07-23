# 自动标注 M1：Web 首帧标注与 Tracking 开发计划

> 状态：开发中  
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
  Tracking 的 legacy 绝对路径。
- 使用固定 Xvfb 无界面运行，不回退 XQuartz。
- Annotation DB lease 防止重复 claim；系统级 `fcntl` writer lock 同时约束
  Annotation Worker 和现有 Navigation plan execution。
- 等待 Web 标注期间不持有 writer lock。
- 每个 target 完成后提交 checkpoint；retry 只复用哈希仍匹配的完成项。
- 未知副作用转 `failed/recovery_required`，确认旧进程组消失前不得重跑。
- cancel 继续使用独立进程组 SIGTERM→SIGKILL，并保留 staging 与审计。

生产 writer 必须显式配置专用 `VLA_ANNOTATION_WORK_ROOT`。任务创建和 Worker
执行前均进行容量预留与 free-space preflight；不足时 fail closed。M1 不提供
自动清理或删除，`tracked` staging 保留给 M2。

## 6. Golden v2

Golden v2 分别声明 reference/candidate 日期、clip、内部 segment 和 artifact
scope，并保持 v1 可读：

- 只允许精确登记的日期 token、artifact root 和
  `paths.img2video_mp4` selector 归一化；
- `tracking_img_*/*` 比较文件树、数量、格式和尺寸，不比较动态 duration/fps
  叠字导致的不稳定 JPEG hash；
- YAML、`img_*.txt`、其他图片、结构与数值继续严格比较；
- candidate 的命令顺序、Runtime manifest、CalibrationSnapshot 和 annotation
  revision-set hash 必须来自 `RuntimeRun`；
- 历史 reference 标记 `historical_unattested`。

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

只有在无需 XQuartz、Web 可完成全部首帧标注、Tracking 与 legacy 等价、刷新/
取消/恢复可靠、原始及同步数据未被修改、全量回归通过后，M1 才能冻结并进入
M2。
