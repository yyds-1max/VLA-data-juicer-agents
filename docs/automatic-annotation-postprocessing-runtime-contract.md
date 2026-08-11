# 自动标注：导航后处理 Runtime 契约

> 状态：生产运行与变更验收的长期权威契约
> 盘点日期：2026-07-30  
> 适用 Runtime：`navigation_odom_v1`  
> 开发背景：`docs/automatic-annotation-development-summary.md`

## 1. 目的与边界

本文把原业务脚本依赖但没有通过参数、Schema 或退出码完整表达的事实，提升为
系统可校验的运行契约。其目的不是重写业务算法，而是保证冻结脚本在系统私有
sandbox 中仍看到与原生产入口等价的目录、命名、环境和执行顺序。

锁定原则：

- 自动标注开发阶段继续使用 job-private sandbox；
- `run_U.sh` 及拆解、同步不属于本 Runtime；
- 后处理业务基线仍是冻结的 `_01/run_odom.sh` 后半段及其依赖；
- 不修改、简化或重排原投影、坐标、速度、方向、gridmap、轨迹和发布算法；
- 系统只增加决策校验、兼容目录、私有镜像、只读挂载、锁、状态、manifest、
  journal 和失败门禁；
- 任何需要改变业务数值或输出语义的方案必须另行取得明确批准。

## 2. 事实源与优先级

运行契约按以下优先级确定：

1. 冻结 Runtime manifest 中登记并校验哈希的脚本、配置和依赖；
2. 业务同事确认的正式入口、标定和训练消费文件；
3. `20260623 / 20260623_145550` 六个干净 segments 的历史 oracle；
4. 系统 RuntimeRun、PostprocessingSpec、输入 manifest 和 CalibrationSnapshot；
5. 本文及自动化契约测试。

若实现、文档和历史 oracle 互相冲突，应停止 writer 并报告差异，不能通过放宽
Golden、修改脚本或猜测成功来消除冲突。

## 3. 端到端链路

导航后处理从已提交且完成 Tracking 的 Annotation staging 开始：

```text
tracked staging
→ 全新 postprocessing attempt
→ 构造 ${dataset_date}_temp
→ 复制选中 tracked segments、maps、metadata
→ 构造 job-private clip_data 镜像
→ 按已接受的 GridmapDecision 准备 gridmap
→ 投影
→ 世界坐标
→ 速度和方向
→ gridmap 转换（仅需要时）
→ odom trajectory_0525
→ job-private final candidate
→ 保持既有 validate_navigation_outputs
→ 构造 manifest payload / revision attestation
→ publication journal / 兼容 finish_data
→ 复验正式发布结果
→ SQLite 提交 manifest / TrajectoryRevision / ReviewTask
→ annotated
```

NavigationDataAgent 负责根据调查事实选择规范化决策；系统负责把决策映射成上述
冻结步骤。浏览器不选择脚本，也不直接启动后处理。

## 4. 目录与命名契约

### 4.1 日期临时根

冻结 `cp_gridmap.py` 不接收独立日期参数，而是从 `--root_data` 目录 basename
的第一个下划线前缀推导日期。因此传入目录必须严格为：

```text
${dataset_date}_temp
```

不能使用泛化名称 `finish_temp`。否则 `20270623` 会被错误推导为 `finish`，
脚本随后查找不存在的 `samples/finish`。

私有 attempt 中的兼容布局固定为：

```text
attempt/
├── ${dataset_date}_temp/
│   ├── samples/${dataset_date}/${private_segment_key}/
│   ├── maps/
│   └── v1.0-trainval/
├── .runtime/
│   ├── clip_data/${dataset_date}/${source_clip}/sync_data/${private_segment_key}/
│   ├── gridmap-before-transform/
│   ├── tmp/
│   └── VLADatasets/finish_data/${dataset_date}/
```

ArtifactManifest 最终保存在 `annotation.sqlite`，不位于 attempt 文件树。
publication journal 位于：

```text
postprocessing/publication-journals/${run_ref}/
```

### 4.2 segment 身份

- 用户和 DataPilot 的公开范围只有 `dataset_date + source clips`；
- 冻结脚本消费内部 sequence/segment 目录；
- 同一 Job 选中范围内的 `private_segment_key` 必须唯一；
- skipped segment 不复制到 M2 attempt，也不进入后处理；
- 内部目录名、绝对路径和脚本参数不得进入公开 API 或聊天消息。

### 4.3 legacy clip_data

`cp_gridmap.py` 和 `3_move_dir.py` 内部读取固定 legacy `clip_data` 根。系统不能
让它们访问真实业务目录，而必须：

1. byte-copy 选中同步输入到 job-private `clip_data` 镜像；
2. 将私有镜像只读覆盖挂载到脚本预期的 legacy 位置；
3. 仅 `pcd_to_grid.py` 生成 gridmap 时，允许写私有镜像；
4. 禁止 hardlink、symlink 和对真实 `clip_data` 的原地修改。

### 4.4 final 根

`3_move_dir.py` 会从 final root basename 推导日期，并具有删除、移动或覆盖语义。
因此它只能接收 job-private：

```text
.runtime/VLADatasets/finish_data/${dataset_date}
```

不能直接接收正式 `finish_data`。正式目录只能由 CompatibilityPublisher 在全部
候选预检完成后通过 journal 发布。

## 5. 规范化决策契约

### 5.1 localization 与 trajectory

当前冻结生产 Runtime 只接受：

```text
localization_kind = odom
trajectory_variant = cjl_0525_with_gridmap
```

Ins、混合 localization 或未知变体必须返回
`unsupported_runtime_variant`，不能回退到其他历史脚本。

### 5.2 gridmap

三个决策含义不能互相替换：

| 决策 | 已调查事实 | Runtime 行为 |
| --- | --- | --- |
| `copy_existing_gridmap` | 选中同步 segment 已有 gridmap | 验证私有镜像中的 gridmap，再运行冻结 `cp_gridmap.py` |
| `generate_from_pcd` | 缺 gridmap、点云前置满足 | 只在私有镜像运行冻结 `pcd_to_grid.py`，再次验证生成结果，再运行 `cp_gridmap.py` |
| `skip_if_projection_ready` | tracked staging 已有可验证的 projection-ready gridmap | 直接复用并验证，不再从 clip_data 复制，也不运行 `cp_gridmap.py` |

`pcd_to_grid.py` 和 `cp_gridmap.py` 存在“部分输入失败但进程仍可能退出 0”的历史
行为。系统不得重写数值变换，但必须验证已接受决策的输入存在，以及冻结脚本
是否生成完整、可解析的约定文件集合；这属于 Runtime 输入/输出契约，不是新增
二维质量审核或扩展 `validate_navigation_outputs`。

运行 `cp_gridmap.py` 前，系统会把 attempt 中原有的目标 `grid_map` 移入
`gridmap-before-transform` 私有备份，使冻结脚本面对空目标目录。脚本退出后必须
生成与私有 source gridmap 完全相同的 JSON 文件名集合，且每个结果仍满足
`data` 长度 40000。这样能识别“退出 0 但没有写入/只写部分”的情况；数值变换
仍完全由冻结脚本完成，wrapper 不计算或改写 gridmap 数值。

## 6. 冻结命令、cwd 与写入面

在 gridmap 准备完成后，冻结命令顺序固定为。下表“业务预期写入”描述冻结脚本
按当前业务实现会修改的路径，并不代表 bubblewrap 已将每一步收窄到该单一
子目录：

| 顺序 | 阶段 | cwd | 业务预期写入 |
| --- | --- | --- | --- |
| 1 | `NuscenesAanlysis_smart_pts_project/main.py` | `NuscenesAanlysis_smart_pts_project` | 当前 attempt 的日期 temp |
| 2 | `2_pt_project/0_img2world.py` | `2_pt_project` | 当前 attempt 的日期 temp |
| 3 | `2_pt_project/4_speed_direction_odom.py` | `2_pt_project` | 当前 attempt 的日期 temp |
| 4 | `other_code/cp_gridmap.py`（按决策） | `other_code` | 当前 attempt 的日期 temp；legacy clip mirror 只读 |
| 5 | `2_pt_project/2_othermethod_cjl_0525.py` | `2_pt_project` | 当前 attempt 的日期 temp |
| 6 | `2_pt_project/3_move_dir.py` | `2_pt_project` | 当前 attempt 的日期 temp 和 job-private final candidate；legacy clip mirror 只读 |

当前 sandbox 将整个 attempt 和 attempt-private `/tmp` 暴露为可写；宿主根、
冻结 Runtime、真实 clip_data、正式 finish_data 和同事业务目录仍不可由业务
脚本直接写入。若将来需要逐步骤进一步收窄写面，必须先证明
`3_move_dir.py` 等多目录副作用仍与 Golden 等价。

以下内容属于运行结果的一部分，不能随意改变：

- 命令顺序和脚本版本；
- cwd；
- 数据 Python 解释器和环境初始化；
- Runtime manifest、脚本哈希和依赖版本；
- 标定快照；
- 输入树和 revision-set 哈希；
- 私有挂载的 source/target；
- 超时、退出码、取消和 checkpoint 事实。

RuntimeRun 当前持久化的是规范化 semantic step ledger，不保存绝对 argv、cwd
或挂载路径。`generate_from_pcd` 和 `skip_if_projection_ready` 的实际分支由
PostprocessingSpec、step ledger 与私有 manifest 共同解释；真正的 invocation
ledger（只记录安全化 script/cwd/binding alias）尚未实现，不能声称仅凭静态
`command_steps` 已证明实际命令事实。

## 7. Python、GUI 与宿主环境

M2 脚本实际导入的 Python distribution 至少包括：

```text
Pillow
matplotlib
mmcv
numpy
nuscenes-devkit
open3d
opencv-python
pypcd
pyquaternion
scipy
similaritymeasures
```

创建 writer Job 前必须同时满足：

- manifest 中存在上述依赖及冻结版本；
- 数据 Python 环境实际探测版本与 manifest 完全一致；
- frozen script/config 哈希未变化；
- bubblewrap、Xvfb、GPU、writer lock、work root 和容量 preflight 通过；
- attempt-private `/tmp` 可写，宿主 `/tmp` 和同事业务目录不作为 scratch；
- GUI backend 初始化继续在固定 Xvfb 中运行，不回退 XQuartz。

M1 的 capability 成功不等于 M2 后处理依赖完整；M2 必须执行 stage-specific
preflight，并在 writer lock 内、创建 attempt 前再次验证，避免排队期间环境
漂移。manifest entry 的 stage/role 是来源和用途元数据，不是互斥执行范围；
M2 会按冻结脚本的实际 import 集合校验全部必要 distribution。

## 8. 输入、重试与污染隔离

每次 attempt 必须是一个不存在的新目录：

- 从已 attested 的 M1 tracked staging 复制输入；
- 复制前后验证 segment tree 和 prepared artifact hash；
- maps 和 `v1.0-trainval` 复用 M1 产物，不重新运行 M1 metadata 步骤；
- 拒绝 tracked segment 根下未绑定的 `*.txt`；
- 禁止复用历史 `finish_temp`。

冻结 trajectory 脚本会产生 `distance.txt`。若复用旧 staging，空的历史
`distance.txt` 可能被识别为新的目标身份，形成伪 `distance` 目标。因此：

- retry 永远创建新 run_ref 和新 attempt；
- 旧 attempt 只保留审计，不作为下一次写入目录；
- `20260605 / 160904` 的历史污染只作为负向回归；
- 严格主 oracle 仍使用 `20260623 / 145550` 六个干净 segments。

## 9. 锁、取消、超时与恢复

- 所有重型 Navigation/Annotation writer 共用同一个系统级 `fcntl` lock；
- 全局 capacity 保持 1；
- prepare/Tracking 等待人工标注时不持锁；
- 单次后处理最长 6 小时；
- 子进程使用独立进程组，取消执行 `SIGTERM → SIGKILL`；
- Runtime 返回 candidate 后，系统通过 SQLite 原子 publication fence 在“已请求
  取消”和“开始发布”之间做唯一选择：取消先提交则不调用 Publisher；fence 先
  提交则后续取消返回冲突，不能再把已可能发布的 Job 标成 cancelled；
- 副作用不明确、旧进程组可能存活或 journal 状态不完整时 fail closed，不自动
  推断成功。

当前 publication journal 能记录 intent、staged 和 committed，并支持通过哈希
人工判断状态；自动 reconcile 尚未实现。M2 冻结前必须补齐自动或明确的停机运维
恢复路径。在此之前，出现 publication 中断应进入
`publication_recovery_required`，禁止自动重跑。

## 10. 兼容发布契约

CompatibilityPublisher 必须：

1. 验证 finish root 为真实、可写目录；
2. 在创建日期目录前验证所有 candidate hash 和所有目标冲突；
3. 任一现有目标内容不同则整批停止；
4. 先写 journal intent；
5. 将所有缺失 clip staging 完成后，才执行第一个目标 rename；
6. 单个外层 clip 使用目录级原子 rename；
7. 发布后再次验证目标 hash；
8. journal 持久化之后的任何 mkdir、copy、rename、hash 或 fsync 失败统一进入
   `publication_recovery_required`；
9. 发布成功并复验后，在单一 SQLite 事务中提交 ArtifactManifest、
   TrajectoryRevision 和 ReviewTask；
10. 仅当所有非 skipped segments 都发布成功时把 Job 投影为 `annotated`。

这里提供的是“整批预检＋单 clip 原子＋journal 可恢复”，不是跨多个目录的单一
文件系统事务。

## 11. 分层校验与 Golden

| 层 | 负责校验 |
| --- | --- |
| Plan validator | 日期、外层 clips、localization、gridmap、trajectory 决策与调查事实 |
| Runtime preflight | manifest、脚本、依赖、解释器、sandbox、writer、容量 |
| attempt 构造 | 新目录、输入 hash、日期命名、私有镜像、无 symlink/hardlink |
| step boundary | 命令顺序、cwd、挂载、取消、输入 manifest 未变化 |
| 既有末尾校验 | 保持 `validate_navigation_outputs` 原语义，不扩展 |
| Golden | 文件树、Schema、图像尺寸、gridmap、轨迹数值、命令顺序和 provenance |

fake-runtime 测试只能证明状态机和 wrapper 契约，不能证明冻结业务算法等价。
服务器验收必须同时包含：

1. byte-identical tracked 输入的算法 Golden；
2. 真实 2027 AnnotationJob 的端到端状态验收；
3. 缺 gridmap 副本的 `generate_from_pcd`；
4. raw、clip_data、历史 oracle、同事源码和公共 scratch 未改变审计。

## 12. 2026-07-30 盘点后加固

本次盘点确认并修复：

- 日期 temp 从泛化 `finish_temp` 改为 `${dataset_date}_temp`；
- M2 preflight 增加冻结脚本完整 Python 依赖版本校验；
- `generate_from_pcd` 在退出 0 后仍重新验证私有 gridmap；
- `skip_if_projection_ready` 不再错误运行 `cp_gridmap.py`；
- candidate 完成后、正式发布前增加取消检查；
- 取消检查升级为数据库原子 publication fence，消除取消/rename 竞态；
- candidate 无效或目标冲突时，不提前创建空的正式日期目录；
- journal 持久化后的错误统一进入恢复门禁，并在 rename 后复验整个外层 clip
  hash；
- `cp_gridmap.py` 在空目标生成完整结果集合，退出 0 的静默失败不能再复用原始
  gridmap 冒充成功；
- durable run 真正执行前再次验证 M2 Python 依赖；
- 增加命令顺序、cwd、日期 basename、legacy 只读挂载、依赖、决策、取消和发布
  副作用的确定性回归测试。

仍需在 M2 冻结前完成：

- publication journal 的中断恢复验收；
- 冻结脚本级 synthetic/fixture contract（不能只使用 fake runtime）；
- `20260623 / 145550` 严格服务器 Golden；
- 缺 gridmap 测试副本的真实 writer 验收；
- 任何新旧数值或文件差异的业务确认。
