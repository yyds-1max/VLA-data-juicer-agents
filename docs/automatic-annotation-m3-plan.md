# 自动标注 M3：数据资产状态整合开发计划

> 状态：已完成并冻结（2026-08-10）
>
> 基线：`5253d23`
>
> 分支：`codex/automatic-annotation-m3`

## 1. 目标与边界

M3 只负责数据资产视图整合，不改变 M2 已冻结的标注、Tracking、后处理、Fix、
审核和兼容发布业务动作。

交付目标：

- 数据管理页同时展示原始数据 ingestion 状态和 Annotation 生命周期；
- 仪表盘“已标注数据”使用真实服务端统计，不展示细分审核队列；
- 日期、外层 clip、AnnotationJob、TrajectoryReview 和历史已验证资产之间可稳定深链；
- 数据管理、仪表盘、标注任务和人工复核读取同一个 AnnotationStore 事实源；
- 历史 `_trajectory_fix_five.json` 经过停机、显式清单和完整性校验后导入为已验证；
- 所有“刷新”均只重新读取当前路由所需事实，保留 URL、筛选和视图，不触发处理。

不进入 M3：

- 不修改 NavigationDataAgent、MainRouter、Plan、Runtime 或业务脚本；
- 不新增 AnnotationAgent；
- 不建设二维复核、AI 辅助复核或 AI Fix；
- 不根据 `finish_data` 文件存在动态猜测线上状态；
- 不改变 `pass`、批准、退回、废弃或训练兼容文件的既有语义。

## 2. 权威事实与生命周期投影

### 2.1 两条独立状态轴

导航目录继续提供 ingestion 状态：

```text
待处理 / 已拆解 / 已同步 / 异常
```

AnnotationStore 提供独立生命周期：

```text
尚未标注
处理中
待首帧标注
已标注 / 待轨迹复核
已验证
已退回
已废弃
处理失败
部分完成
```

数据管理页同时展示两列，禁止用 Annotation 状态覆盖 ingestion 状态。

### 2.2 内部单元映射

每个非取消的权威 AnnotationJob 通过 source lease 绑定日期和外层 clip。内部
segment 按下列规则映射：

- `preparing/tracking/tracked/postprocessing`：处理中；
- `waiting_initial_annotation`：待首帧标注；
- `failed` 或 `postprocessing_failed`：处理失败；
- 后处理完成且 Review 为 `pending`：已标注 / 待轨迹复核；
- Review 为 `in_progress` 或批准后的兼容发布正在执行：处理中；
- Review 为 `returned`：已退回；
- Review 为 `discarded`，或明确无可处理目标：已废弃；
- Review 为 `approved` 且兼容发布成功：已验证；
- Review 为 `approved` 但发布失败：处理失败。

同一外层 clip 的内部单元状态不一致时投影为“部分完成”；日期内各 clip 状态不
一致时日期也投影为“部分完成”。页面同时展示各状态数量，不能用单一目录存在性
掩盖混合进度。

取消且未形成业务产物的 Job 不占据 Annotation 生命周期；其审计记录仍保留在标注
任务历史中。

### 2.3 聚合统计

仪表盘只消费以下汇总：

- 已完成后处理的外层 clip 数；
- 已验证的外层 clip 数；
- 已完成后处理的内部 segment 数；
- 以已同步 clip 为分母、已完成后处理或已导入历史验证事实的 clip 为分子的标注
  覆盖率。

细分的待复核、修正中、已退回和已废弃数量只在数据管理及人工复核页展示。

## 3. 服务端读模型与 API

AnnotationStore 新增只读生命周期快照：

- 按 `dataset_date + source_clip` 聚合当前权威 Job、segments、reviews 和 publication；
- 只返回公开 ref、状态、数量、时间和深链所需绑定；
- 不返回数据库 ID、内部 segment 名、绝对路径、工具名或脚本参数；
- 原生 M2 账本优先于历史导入记录，避免同一 clip 重复计数。

导航数据 API 保持原路径并扩展响应：

```text
GET /api/navigation/datasets/summary
GET /api/navigation/datasets/{date}
```

新增字段：

```text
annotation_totals
dates[].annotation
dates[].clips[].annotation
```

`annotation_totals.annotated_duration_ns` 按与“已标注 clips”相同的口径汇总
这些 clips 在导航数据目录中的原始采集时长；缺少可关联 metadata 时不推测时长。

因此仪表盘和数据管理页读取完全相同的聚合结果。Annotation Jobs/Reviews 页面仍
读取现有 `/api/annotation` 领域 API。

历史已验证资产提供只读详情 API，仅返回公开 provenance、内容哈希和导入时间，
不直接返回训练文件内容或服务器路径。

## 4. 历史导入

迁移新增不可变的历史已验证资产表。导入只能通过停机运维 CLI 完成：

- 必须显式提供 `finish_data` 根目录和 JSON 清单；
- 清单逐项包含日期、外层 clip、segment ordinal、segment 总数、相对文件路径和
  预期 SHA-256；
- 文件必须位于声明根目录内、不是 symlink、名称以
  `_trajectory_fix_five.json` 结尾、JSON 可解析且哈希一致；
- 同一日期/clip 的 `segment_total` 必须一致，ordinal 不得重复或越界；
- 默认 dry-run；显式 `--apply` 后才写入；
- 重复导入同一内容幂等，不同内容冲突并停止；
- 输出只包含计数和随机公开 ref，不打印绝对路径或内部 segment 名。

历史 clip 只有全部清单单元导入后才显示“已验证”，不完整导入显示“部分完成”。

## 5. 前端与深链

### 数据管理

- 保留 ingestion 状态筛选；新增 Annotation 生命周期筛选；
- 日期和 clip 行显示生命周期标签和 `已完成/总单元` 数量；
- 权威 Job 深链到 `/annotation/jobs/{job_ref}`；
- 待复核、退回、废弃和已验证的原生数据深链到对应 Review；
- 历史已验证记录深链到只读已验证资产页；
- 页面级“刷新”执行完整 summary 重扫，保留数据类型、搜索、筛选、展开日期和
  抽屉状态；它与浏览器重载读取同一份服务端事实，但不触发任何 Runtime。
- DataPilot 管理的拆解、同步任务状态变化通过持久事件流通知前端，前端只重读受
  影响日期；AnnotationJob、Segment、Review 和历史生命周期变化使用相同的按日期
  局部刷新机制。
- 同事绕过 DataPilot 直接运行外部脚本时不会产生系统领域事件，此时显式“刷新”
  仍是权威文件系统重扫入口，M3 不增加文件系统 watcher。

### 仪表盘

- “已标注数据”改为真实外层 clip 数与覆盖率；
- 显式刷新与浏览器重载读取相同服务端事实；系统管理的 ingestion 与 Annotation
  生命周期变化会自动局部更新；
- 不把人工复核细分状态搬到仪表盘。

### 标注与复核

- 现有任务/复核刷新继续使用 force 读取；
- 深链刷新、前进和后退必须恢复同一个公开 ref；
- 所有快捷按钮统一显示“交给 DataPilot”。

## 6. 实施批次与门禁

1. 保存计划并标记总体路线 M3 已启动；
2. 实现生命周期纯读模型、聚合规则和 API 扩展；
3. 实现历史导入 migration、CLI、只读详情；
4. 数据管理页接入双状态、数量、筛选和深链；
5. 仪表盘接入真实统计，统一显式刷新语义；
6. 更新架构和总体路线，执行回归并冻结 M3。

必须覆盖：

- 无 Job、等待首帧、Tracking、后处理、失败、Review 五种状态和发布状态；
- 同一 clip 与同一日期的混合状态/部分完成；
- cancelled Job 不污染生命周期；
- 原生账本覆盖历史导入且不重复计数；
- 历史导入 dry-run、幂等、哈希不符、路径逃逸、symlink、重复 ordinal；
- API 和 CLI 输出脱敏；
- 数据管理双筛选、深链、刷新保留视图；
- 仪表盘真实统计、加载和失败状态；
- Python、前端、生产构建和冻结 Router 基线无回归。

M3 退出条件：数据管理、仪表盘、标注任务、人工复核与历史只读资产稳定指向同一
服务端事实；状态和数量不依赖目录猜测；所有现有标注/Fix 业务动作保持不变。

## 7. 历史导入清单与停机操作契约

历史导入清单必须是 UTF-8 JSON，顶层只能包含 `assets`。每项格式固定为：

```json
{
  "dataset_date": "YYYYMMDD",
  "source_clip": "外层 clip 名",
  "segment_ordinal": 1,
  "segment_total": 1,
  "relative_path": "日期/clip/segment_trajectory_fix_five.json",
  "sha256": "64 位小写十六进制 SHA-256"
}
```

服务器验收时必须先停止 Web 服务并备份 Annotation DB、WAL 和 SHM，再按现有
生产环境显式传入数据库与共享 writer lock：

```text
vla-annotation-operator \
  --annotation-db <annotation.sqlite 的绝对路径> \
  --writer-lock <共享 writer lock 的绝对路径> \
  migrate-schema \
  --backup-root <全新私有备份目录>

vla-annotation-operator \
  --annotation-db <annotation.sqlite 的绝对路径> \
  --writer-lock <共享 writer lock 的绝对路径> \
  import-history \
  --finish-data-root <finish_data 的绝对路径> \
  --manifest <历史导入清单的绝对路径>

# 人工核对 dry-run 的计数与清单哈希后，才允许增加：
--apply
```

路径仅写在停机运维命令和私有账本中；CLI 正常输出、错误输出、HTTP API 和前端均
不得回显绝对路径。导入不会修改、移动或重新生成原训练兼容文件。

## 8. 本地实施结果（2026-08-09）

已完成：

- Annotation schema v9、不可变历史已验证资产账本和只读 provenance API；
- 原生 M2 账本优先的生命周期快照，以及 clip/date/summary 聚合；
- 历史导入 dry-run、显式 apply、幂等、冲突、哈希和路径安全边界；
- 数据管理双状态、生命周期筛选、数量与深链；
- 仪表盘真实已标注统计；
- 同路由显式刷新，以及统一“交给 DataPilot”按钮文案；
- 历史已验证资产只读详情页。

本轮明确未修改：

- Navigation/Annotation 处理状态机和 M2 业务动作；
- Router、NavigationDataAgent、Plan 与评测 Prompt；
- Tracking、后处理、Fix Runtime、脚本、参数和数值逻辑；
- 兼容训练文件发布语义。

服务器冻结门禁中的数据库迁移、API、历史导入和状态事件端点已在 2026-08-10
完成；详细结果见第 9 节。

本地最终门禁为：Python `1783 passed, 1 warning`；前端
`389 passed, 8 skipped`；M3 专项 Playwright `2 passed`；production build
和 `512000` 字节 bundle gate 通过；`datapilot-v1/navigation-m2` 分别验证
`17/7` 个 case schema；`git diff --check` 通过。

完整 Playwright 套件同时暴露了基线前端重构遗留的 7 项失败：3 项旧首帧画布
定位、2 项旧人工复核文案/队列定位、1 项既有通知弹窗预期和 1 项既有仪表盘
低对比度。失败节点不在 M3 业务改动中；本轮没有用放宽断言掩盖它们，也没有借
M3 越界重构旧页面。它们应在独立前端质量收口任务中修复后再恢复全套
Playwright 绿色门禁。

M3 没有修改 M2 标注、Tracking、后处理、Fix、审核或兼容发布动作，现已完成冻结。

## 9. 服务器实施结果（2026-08-10）

- 服务器从干净的 `main@764a426` 切换到
  `codex/automatic-annotation-m3@5c3cad1`，没有文件覆盖式部署；
- Web 与 Annotation Worker 停止后，CLI 从 schema v8 迁移到 v9，并在
  `annotation.sqlite` 同目录创建全新私有备份；备份 manifest SHA-256 为
  `43aedd4ee19658301cb3a79c4f854746ed9073d3cbf349d5d283e73ddbdbe5c0`；
- 独立复核结果为 `integrity_check=ok`、foreign-key 违规 `0`、迁移账本连续
  `1..9`、安全标记为 `schema_version=9/status=verified`；
- 真实 M2 账本投影 `20270623 / 20260623_145550` 为 6 个内部单元：5 个已验证、
  1 个已废弃，clip/date 均为“部分完成”；仪表盘聚合为 1 个已标注 clip、
  6 个已标注单元、5 个已验证单元；
- Runtime capability、首页、数据管理和标注任务 SPA 路由均可用；摘要、capability
  和安全 404 响应未发现服务器绝对路径或用户名目录泄漏；
- 历史候选仅包含真实日期 20260227～20260623，排除 2027 开发测试数据；共
  669 个 segment、416 个外层 clip，目录深度一致且无重复 segment；
- 历史导入 dry-run 状态为 `historical_import_validated`，manifest SHA-256 为
  `e1091de08b7f4bb09e5e66935211e15b30b04d723bd71b32ec1bd7e08828bef7`；
- 经用户确认后，停机状态下再次校验相同清单并执行 `--apply`：导入 669 个历史
  segment、416 个外层 clip 作用域，日期范围为 20260227～20260623；数据库
  `integrity_check=ok`、外键违规为 0；没有修改、移动或重新生成任何业务产物；
- 正式导入前创建了新的私有 SQLite 停机备份；主文件 SHA-256 为
  `073b019cf55f4da5b3e07c0f6d2dded1b3bc22b03b3908523591f106e0d64f3c`；
- 导入后数据摘要为 53 个日期、643 个 clips、489 个已同步 clips；历史账本与原生
  M2 事实按当前数据目录取交集并由原生账本优先去重后，显示 361 个已标注 clips、
  571 个已标注单元、360 个已验证 clips 和 570 个已验证单元，覆盖率约 73.8%；
- 产品统计没有按 2027/2028 或其他日期过滤测试数据；此前历史导入清单排除开发
  副本，只是避免把复制出的测试产物登记成历史生产事实。正式上线时由数据治理
  删除测试数据即可；
- 新增持久的导航任务公开事件流和 Annotation 事件日期投影。DataPilot 管理的
  拆解、同步及后续任务变化会触发受影响日期的局部重读，Annotation 生命周期同样
  自动更新；手动“刷新”保持完整扫描语义；
- 状态自动刷新功能提交为 `b00f1cd`；服务器服务已恢复运行，事件 cursor API、
  摘要 API、SPA 构建和 bundle gate 均通过。
