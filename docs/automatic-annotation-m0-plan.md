# 自动标注 M0：契约与 Runtime 基线计划

> 状态：本地基线与活动 Python Runtime 绑定已完成（正式 Runtime 冻结待服务器部署和 Golden 门）  
> 开始日期：2026-07-23  
> 完成日期：2026-07-23  
> 上位路线：`docs/automatic-annotation-roadmap.md`

## 1. 目标

在不运行真实业务处理、不修改服务器文件、不改变现有业务算法的前提下，建立后续 M1/M2 所需的可审计 Runtime 基线和 Golden 等价比较能力。

M0 不实现首帧标注页面、Annotation 数据库、Tracking 新入口、三维 Fix 页面或智能体工具。

跨里程碑保持以下职责边界：LLM/多模态模型负责意图理解、规划、推理，并在 M4 基于受控证据执行三维辅助审核；确定性系统负责数据搬运、格式转换、参数精确传递、状态、校验、恢复和产物发布。M0 的 manifest、Runtime wrapper 和 Golden 比较器均属于后者，不能把精确业务数据交给 LLM 代为传递或判断。

## 2. 工作包

### M0-A：文档与决策基线

- 保存权威总体路线和本计划。
- 在 `architecture.md` 中修正自动标注状态语义并引用权威路线。
- 记录当前确认的生产入口、标定语义、训练消费文件和排除范围。
- 明确现有 `validate_navigation_outputs` 保留但不扩展。

完成标准：后续代理只阅读路线和当期计划即可获得一致边界。

### M0-B：只读 Runtime 依赖清单

以 root alias 记录，不在提交产物中保存用户名目录：

```text
PROCESSING_ROOT = Object_location_gh_v3_fisheye_five_U_add_SF_01
FIX_ROOT        = 与 PROCESSING_ROOT 相同的生产目录
```

清单至少覆盖：

- `run_odom.sh`
- `run_fix.sh`
- NoobScenes 及 include 脚本
- 首帧 YAML 生成和视频辅助脚本
- Tracking 二进制、配置和全部 ONNX/模型权重
- projection/img2world/speed_direction
- `_0525` trajectory
- gridmap copy/generation
- final publish
- `_0525` fix
- `20260320`、`20260409_U`、`20260529_go2w` 标定

每项记录：

- root-relative path
- kind
- SHA-256
- byte size
- executable bit
- 调用方或所属阶段
- 是否为外部/缺失依赖

完成标准：从入口脚本可追溯到所有直接业务依赖，未知项明确列出。

### M0-C：系统专用 Runtime 方案

- 仓库提交 Runtime 部署/调用约束、manifest 和只读验证器。
- 业务阶段 wrapper 和兼容适配器随 M1/M2 对应阶段实现；在系统专用 payload 尚未部署并通过 Golden 前，不把现有执行入口切换到未验收的新 Runtime。
- 服务器大型业务 payload 部署到配置驱动的系统专用目录。
- 启动或部署检查 manifest；哈希不一致时拒绝真实写任务。
- 原算法脚本尽量原样复制；仅路径/工作目录的机械适配允许单独补丁。
- Tracking 公共 scratch 未隔离前保持全局串行。
- `2_resize.py` 只作用于 `finish_temp` staging。

完成标准：M1 实现者无需直接依赖同事可修改的生产源码目录。

### M0-D：Golden snapshot 与 comparator

比较器必须支持：

- 相对文件树；
- 文件 SHA-256 和大小；
- 图片尺寸；
- YAML/JSON 结构；
- gridmap `data` 数组；
- trajectory/fix 的关键结构与数值；
- 忽略明确声明的非业务差异；
- 对数值字段使用显式、默认严格的绝对/相对容差。

安全要求：

- Snapshot 不保存绝对路径、原始图片、完整业务轨迹或凭据。
- 提交产物只保存相对路径、哈希、尺寸、Schema 指纹和数值摘要。
- 不允许比较器自动修改输入或发布目录。

完成标准：本地 fixture 能稳定报告等价、缺失、多余、结构变化和数值变化。

### M0-E：Golden 样本

固定：

- `20260605_160904_zhigu_wuhan_0`
- `20260623_145550_zhigu_wuhan_0`
- 20260714 中一个无 gridmap 的 segment（在只读调查后记录具体 ID）

前两个样本用于完整 odom/go2w/Tracking/trajectory/fix 兼容基线；第三个只用于后续验证现有 `pcd_to_grid.py` 能力，不在 M0 运行处理。

完成标准：样本选择、适用阶段、污染风险和不适用项均写入 commit-safe registry。

## 3. 实现顺序

1. 保存路线和 M0 计划。
2. 只读扫描服务器入口和依赖。
3. 固化 Runtime manifest schema 和 commit-safe manifest。
4. 实现 snapshot/comparator 及 CLI。
5. 编写隔离 fixture 和单元测试。
6. 生成或整理 Golden registry，不写服务器。
7. 运行目标测试、Python 全量测试和前端测试/构建。
8. 核对 Router eval schema；不调用真实模型刷新 baseline。
9. 记录 M0 已完成项、未完成服务器门和 M1 进入建议。

## 4. 测试与验收

目标测试：

- manifest schema/路径脱敏/哈希稳定性；
- 相同 snapshot 等价；
- 缺失和多余文件；
- 图片尺寸变化；
- YAML/JSON Schema 变化；
- gridmap 数组变化；
- trajectory 数值超容差；
- 非有限数值和损坏文件；
- 输入目录只读。

回归门：

```text
pytest
frontend unit tests
frontend build
vla-agent-eval validate --suite datapilot-v1
git diff --check
```

真实模型评测和真实数据写任务不属于 M0。

## 5. M0 退出条件

- 权威路线与 M0 计划已保存；
- Runtime 依赖 manifest 可审计；
- ONNX/二进制/配置/标定均有哈希或明确缺失说明；
- Golden comparator 及测试通过；
- Golden registry 已固定且无原始业务数据泄漏；
- Runtime 部署方案不改变业务逻辑；
- 全量回归无新增失败；
- 未对服务器或业务数据产生写入。

## 6. 实际盘点与锁定决策

2026-07-23 对服务器完成只读盘点，未执行业务脚本，也未创建、修改或删除服务器文件。

确认的活动业务顺序：

```text
复制所选 sensors
→ 0_creat_box.py
→ 1_odom_convert.py
→ 2_resize.py
→ main_smart_odom.py
→ map.png
→ img2video.py
→ gen_box.py
→ dog.yaml
→ Tracking
→ projection
→ img2world
→ speed/direction
→ cp_gridmap
→ trajectory_0525
→ final publish
```

`pcd_to_grid.py` 是缺少 gridmap 时进入上述链路前的独立准备能力，不是
`run_odom.sh` 内部步骤。`run_fix.sh` 对 final 内部 segment 调用现有 `_0525`
Fix 脚本。

Runtime 清单结果：

- `frozen_file`：53 项；
- `generated_mutable`：7 项；
- `external_runtime`：25 项；
- 当前活动直接文件/目录无缺失；
- NoobScenes 活动导入链的直接依赖 `numba` 已登记；
- Tracking 的 `dog.yaml`、输出目录和点文件仍为全局可变资源，必须全局串行；
- `run_odom.sh`/`run_fix.sh` 虽在脚本内使用裸 `python3`，实际 Data Runtime 已
  由 `AGENT_DATA_ENV_SETUP + AGENT_DATA_PYTHON` 显式绑定；活动基线为
  Python 3.8.10，setup 和解释器已记录 SHA-256/大小，13 个直接 Python
  distribution 已使用同一环境逐个导入并冻结版本；
- 交互式 SSH 默认 Python 的另一组版本不属于数据 Runtime，不能作为生产事实；
- Tracking 二进制的直接动态依赖已只读确认全部解析成功且依赖集合未变化；
- TBB、`libstdc++`、glibc、GPU/驱动和旧 GUI display 等外部条件尚未形成可复制 payload，已明确登记为未冻结外部条件；
- `2_resize.py` 只能在 job staging 的 `finish_temp` 中运行；
- 不把未被活动源码引用的备份文件或 build-generated 文件当作业务硬门禁。
- 当前系统的 `prepare_gridmap_for_projection` 在 Python 中实现了 gridmap payload
  转换，而历史基线在 motion 之后调用 `cp_gridmap.py`。M0 不判断两者数值等价；
  M2 必须以历史脚本顺序和 Golden 结果为准，不能把当前重写实现直接视为已验收。

M0 不切换当前 Navigation 执行入口。原因不是改变总体路线，而是阶段 wrapper 的
实际边界分别属于 M1 的 Web 首帧暂停/Tracking 和 M2 的后处理/Fix；提前把
`run_odom.sh` 包成不可暂停的整体入口会破坏已批准的里程碑边界。M0 已提供
manifest preflight 和部署约束，M1/M2 只能在系统专用 payload 验证和 Golden
通过后接入对应 wrapper。

## 7. Golden 样本与工具结果

已登记三个不包含绝对路径和业务内容的私有样本身份，并拆分为 M1 Tracking、
M2 postprocess、M2 Fix 和缺 gridmap 共七个 case：

- `20260605_160904_zhigu_wuhan_0`：历史完整链路样本；
- `20260623_145550_zhigu_wuhan_0`：历史完整链路样本；
- `20260714_104651_zhigu_wuhan_0`：同步输入样本，图像、点云、odom 各 71 帧，
  明确缺少 gridmap，且无 finish/final。

比较器已覆盖文件树、哈希、图片尺寸、JSON/YAML 结构、gridmap 和 trajectory
数值、命令顺序摘要，并执行以下约束：

- 默认绝对和相对容差均为零；
- 拒绝 symlink、重复 JSON/YAML key、NaN/Inf 和损坏图片；
- 不保存绝对输入 root、原始图片、完整数组、轨迹数值、命令参数或凭据；
- 等价退出码为 0，业务差异为 1，基础设施/安全错误为 2；
- M0 只用隔离 fixture 验证比较器；公司数据的历史 legacy oracle capture 和
  新系统 candidate capture 仍需服务器验收阶段单独批准；
- case 会核对内部 segment 名、阶段对应的 artifact scope 和必要产物，不能用空目录
  伪装目标样本；
- 缺 gridmap case 分别校验只读 source root 中 gridmap 缺失，以及 staging 输出
  root 中 gridmap 已生成，不能把“输入缺失”误当成期望输出；
- 命令步骤和 manifest hash 当前仍是调用者声明值；M1/M2 必须将其绑定到系统
  execution ledger 后才可作为真实执行证据。

## 8. 回归结果

```text
Runtime manifest schema：85 entries，PASS
Golden registry：3 samples / 7 cases，PASS
M0 + Navigation Runtime 针对性测试：119 passed
Python 全量：1194 passed，1 个既有依赖弃用 warning
Frontend：150 passed
Frontend production build：PASS
DataPilot v1 eval schema：17 cases validated
git diff --check：PASS
```

冻结文件摘要：

```text
manifest.json:
427bee353b84e6a6911ec020a212ce40af90285f9cd29916912b6e49858e2c90

golden-cases.yaml:
9f52cc417277932c0310bc411b3fc5e6f8b39d3e17c6f85a40dd91cd551ffa4d
```

本里程碑没有修改 Router/Navigation Prompt、智能体工具 Schema、
PublicActionRegistry 或 eval case，因此按冻结规则不调用真实模型刷新 Router
baseline。

## 9. M1 前置服务器门

M1 可以开始任务级设计和本地开发，但在真实服务器 writer 验收前必须另行批准：

1. 将已冻结 payload 复制到配置驱动的系统专用 Runtime 目录；
2. 使用 manifest 验证 53 个 frozen file；
3. 按 manifest 重新核对活动 setup、Python 解释器哈希和 package 版本；
4. 先对原日期既有产物做只读 legacy oracle capture，再对获批的测试日期
   candidate 产物 capture 并比较；不重新运行旧脚本；
5. 人工在场完成 Web 首帧标注/Tracking 等价验收。

## 10. 服务器测试副本决策

2026-07-23 只读核对确认：

- 测试日期 `20270605` 已有 raw 副本 `20260605_152856`、
  `20260605_152930`、`20260605_160904`；
- 测试日期 `20270623` 已有 raw 副本 `20260623_145550`；
- 两个测试日期目前均无 `clip_data`/同步产物；
- 两个测试日期均无 finish/final/temp 产物，不存在覆盖历史输出的风险；
- 原日期 `20260605`、`20260623` 下的同名 clips 已有同步和完整 finish
  产物，只允许只读参考，禁止作为 writer 验收目标。
- 原日期的只读结构表明，`20260605_152856`、`20260605_152930` 各产生一个
  内部 segment；`20260623_145550` 产生六个内部 segments（前五个各 120 帧，
  最后一个 26 帧）。这只是选择验收范围的历史证据，测试日期完成同步后仍须以
  测试副本的实际结构重新确认，不能直接假定内部身份不变。
- 用户已批准将上述四个 2027 raw 副本作为新系统真实测试目标；该授权仅覆盖这些
  明确的测试日期/来源 clips，不包含原日期 writer，也不是在工具和执行门禁尚未
  就绪时立即启动业务处理的指令。
- 后续补充的 `20260605_160904` 副本中，真实 `db3` 和 `metadata.yaml` 与原目录
  SHA-256 一致，但包含两个由 macOS 复制产生的 `._*` AppleDouble 文件。系统
  拆包发现逻辑已明确忽略这类非业务元数据；Golden 的 raw 同源检查也应忽略
  `._*` 后比较业务文件，并单独报告复制污染。

后续真实 Golden 必须使用测试副本，顺序为：

```text
核对测试 raw 副本与原日期 raw 的文件身份/哈希
→ 对原日期同步和 finish 产物做只读 legacy oracle capture
→ 执行前核对新系统 commit、配置、目标范围和 Runtime manifest
→ 新系统对已授权的测试日期执行拆解、同步
→ 比较两侧同步产物并建立 source clip / internal segment 映射
→ 冻结测试日期实际内部 segments 和输入计数
→ 更新 Golden registry 中的 paired oracle/candidate 身份
→ 使用与历史处理一致的处理标定、首帧标注和 Fix 标定
→ 新系统只在测试日期的隔离 staging/output 中运行
→ capture candidate
→ compare
```

对应关系为：

```text
20270605 / 20260605_152856
↔ 20260605 / 20260605_152856 的既有同步与 finish 产物

20270605 / 20260605_152930
↔ 20260605 / 20260605_152930 的既有同步与 finish 产物

20270605 / 20260605_160904
↔ 20260605 / 20260605_160904 的既有同步与 finish 产物

20270623 / 20260623_145550
↔ 20260623 / 20260623_145550 的既有同步与 finish 产物
```

原日期既有产物是同事使用服务器正式脚本生成的只读 legacy oracle。验收不再要求
对测试副本重新执行旧脚本；这样比较的目标是“新系统工具能否复现真实历史生产
结果”。为避免把输入差异误判为后处理差异，必须先完成以下门禁：

- raw 副本与原始 raw 的业务文件集合、大小和 SHA-256 一致；
- 新系统同步结果与原日期同步产物在允许的日期/root 归一化后等价；
- segment 配对依据来源 clip、帧时间和数据计数，不只依赖字符串名称；
- 每个配对 case 记录历史处理标定、可复现的首帧标注 revision 和独立 Fix 标定；
- 只允许归一化已登记的日期、根路径或运行时间等非业务字段；图片、点云、
  gridmap、轨迹及其他业务数值不得因日期复制而放宽比较；
- 任一前置条件无法证明时，该 case 只能标为不可归因，不能宣称新旧业务等价。

当前 registry 中的 `20260605_160904_zhigu_wuhan_0` 已有同源的 2027 raw 测试
副本，可以继续作为这轮 paired 真实验收样本。测试副本同步完成后，仍须确认其
实际内部 segment 映射；同时以 `20260605_152856`、`20260605_152930` 实际生成
的内部 segments 补充 paired cases。

原日期目录禁止运行任何 writer。测试副本同步完成前，不猜测其内部 segment
身份，也不启动 M1/M2 真实业务处理。由于生产工具只能选择外层 clip、不能选择
内部 segment，后续对测试副本 `20260623_145550` 的单 clip 验收必须运行并比较
该 clip 实际生成的全部内部 segments；当前只登记 `_0` 的历史 per-segment case
不能代表整条 clip 已经完成业务等价验收。
