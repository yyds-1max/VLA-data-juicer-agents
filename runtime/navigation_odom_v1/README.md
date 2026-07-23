# navigation_odom_v1 Runtime 基线

本目录只保存可提交 Git 的 Runtime 契约、清单和 Golden 样本登记，不保存业务数据、模型权重、Tracking 二进制或服务器绝对路径。

## 目标

`navigation_odom_v1` 冻结现有 `_01/run_odom.sh`、`run_fix.sh` 及其活动依赖，供 M1/M2 在不改变业务算法、步骤和数值方法的前提下建立系统专用 Runtime。

自动标注模块从同步产物开始；`run_U.sh` 不属于本 Runtime。

## 文件

- `manifest.json`：只读调查得到的内容清单、所属阶段、可变 scratch 和外部运行环境。
- `golden-cases.yaml`：Golden 样本登记；只包含业务 ID 和适用范围，不包含数据内容或绝对路径。

活动执行顺序由本 README 和 `golden-cases.yaml` 中分阶段的
`expected_command_steps` 共同约束，不由 manifest 的 `stage` 字段推导。

## 部署模型

仓库只负责 manifest、验证器、wrapper 和兼容适配器。大型 payload 应复制到配置驱动的系统专用服务器目录，并保留原文件内容：

```text
system runtime root
├── source/       # manifest 中 navigation_odom_v1_source 的 frozen_file
├── external/     # 经单独选择和验证的外部共享库或环境描述
├── work/         # 每个 job 的 staging；不提交
└── locks/        # 重型 writer 的租约；不提交
```

部署和运行时必须遵守：

1. 先用 manifest 验证器核对 frozen file 的 SHA-256、大小和 executable bit。
2. 哈希不一致时拒绝真实写任务，不自动回退到同事业务目录中的另一个版本。
3. wrapper 只可做参数传递、工作目录隔离、锁、状态和审计；不得重写或简化算法。
4. `2_resize.py` 只能处理 job staging 中的 `finish_temp`，不得原地修改同步产物。
5. Tracking 仍使用全局配置和输出 scratch 时，整个 Tracking writer 必须全局串行。
6. `run_fix` 的标定由 Fix 任务显式选择并记录；不得依赖静默 fallback。
7. 服务器部署、dry-run 和真实单 clip 验收分别需要单独批准。

现有 `run_odom.sh`/`run_fix.sh` 在脚本内部调用裸 `python3`，但实际 Data Runtime
由服务配置先加载 `AGENT_DATA_ENV_SETUP`，再通过 `AGENT_DATA_PYTHON` 显式绑定
解释器。2026-07-23 的只读核对已冻结活动 setup 脚本和 Python 3.8.10 解释器的
SHA-256/大小，并使用同一 setup 和解释器逐个导入 manifest 所列 Python 包。
manifest 中记录的是这组活动 Runtime 的 distribution 版本；交互式 SSH 默认
Python 得到的另一组版本不属于数据处理基线。`numba` 是 NoobScenes 活动导入链
的直接依赖，已一并登记。

setup 脚本会选择数据解释器、加载两个 ROS setup，并调整命令/动态库搜索路径；
它不激活虚拟环境，也不设置 `PYTHONPATH`。这些文件仍属于服务器
`external_runtime`，没有复制进仓库；部署时必须按 manifest 再次核对 setup、
解释器、包版本和动态库，不能把“已只读审计”误解为“已部署可复制环境”。

M1 writer 还要求安装证据 fail-closed。服务器安装固定
Xvfb `2:1.20.13-1ubuntu1~20.04.20` 后，必须只读捕获并在受控 manifest 更新中
登记 `xvfb_deb_package`、`xvfb_server_binary`、`xvfb_launcher`、
`sandbox_binary` 与 `runtime_dependency_summary` 的 SHA-256、大小和 executable
bit；部署配置同时显式绑定 deb 与依赖摘要文件。当前仓库 manifest 尚未臆造这些
服务器安装后摘要，因此在完成该门禁并更新 manifest 前，M1
`/api/annotation/capabilities` 应返回不可用，writer Job 不得创建。

## 只读验证

验证提交清单本身：

```bash
vla-nav-runtime-manifest validate-manifest \
  --manifest runtime/navigation_odom_v1/manifest.json
```

验证已部署的 frozen payload：

```bash
vla-nav-runtime-manifest verify-root \
  --manifest runtime/navigation_odom_v1/manifest.json \
  --root-alias NAVIGATION_ODOM_V1_SOURCE \
  --root /configured/system/runtime/source
```

验证器不得执行业务脚本，也不得把传入的绝对 root 写入报告。
`verify-root` 只证明指定 alias 下的 `frozen_file`；它不验证
`external_runtime` 或 `generated_mutable`，对没有 frozen file 的 alias 会拒绝
返回成功。

## 活动业务顺序

只读盘点确认 `_01/run_odom.sh` 当前活动顺序为：

```text
复制所选 sensors
→ 0_creat_box.py
→ 1_odom_convert.py
→ 2_resize.py（只处理 finish_temp）
→ main_smart_odom.py
→ 复制 map.png
→ img2video.py
→ gen_box.py
→ 生成全局 dog.yaml
→ Tracking bin/main
→ projection main.py
→ 0_img2world.py
→ 4_speed_direction_odom.py
→ cp_gridmap.py
→ 2_othermethod_cjl_0525.py
→ 3_move_dir.py
```

缺少 gridmap 时，现有 `pcd_to_grid.py` 是进入上述链路前的独立准备能力，不是
`run_odom.sh` 内部命令。`run_fix.sh` 对 final 中的每个 segment 调用现有 `_0525`
Fix 脚本。注释掉的畸变、`cp_ins` 和旧 sensors 路径不属于活动依赖。

只读核对的 `_01/run_odom.sh` 在扫描 `sync_data` 时存在明确的活动业务条件：
仅把名称匹配 `2025*` 或 `2026*` 的内部 clip 放入 `CLIP_SOURCES`，无匹配项时
直接退出。M1 为保持冻结脚本语义，遇到其他年份前缀会返回
`unsupported_runtime_variant`，不会静默扩大处理范围。外层测试数据日期可以是
2027，但当前验收副本的内部 clip 名仍来自 2026。将来真实内部 clip 改为 2027
或其他命名时，应先核对并冻结新的业务脚本版本，再建立新的 Runtime 版本或经
明确批准扩展本 Runtime；不能把该限制当成普通安全正则直接删除。

当前尚不能只凭 Git manifest 完整复原的外部条件包括 GPU/驱动、系统 TBB、
`libstdc++`、glibc、Tk/X display 和 generated mutable scratch 的初始状态。
这些条目在 manifest 中明确标记为 `external_runtime` 或 `generated_mutable`，
不能被当作已冻结 payload。M1/M2 的服务器部署验收必须再次核对。

## Golden 使用边界

M0 只登记样本、实现 snapshot/comparator，并通过隔离 fixture 验证比较行为。
M1 Tracking、M2 postprocess 和 M2 Fix 使用独立 case；Fix case 显式包含人工复核
边界和独立 Fix 标定快照。CLI 会校验 sample 的内部 segment 名、case 的
`artifact_scope` 和必要产物：

- `finish_temp_date`：传入该日期的 `finish_temp` 根，case 定位
  `samples/<date>/<segment>`；M1 还分别以严格的根级 case 比较 `maps/` 和
  `v1.0-trainval/`，不能只比较 segment；
- `finish_date`：传入 final 日期根，case 定位内部 segment；
- `staged_sync_segment`：传入 `pcd_to_grid` 的 staging 输出 segment；缺
  gridmap case 还必须通过 `--source-root` 传入原始只读同步 segment，以分别验证
  “输入确实缺少 gridmap”和“输出已生成 gridmap”。

Golden contract v2 的 candidate 命令顺序、Runtime manifest、处理标定和首帧标注
revision 集合只能通过 `AnnotationStore.runtime_run_attestation(run_ref)` 从已提交的
`RuntimeRun` 账本投影。CLI 不接受 candidate attestation JSON，也不能用
`--command-step` 或 `--runtime-manifest-sha256` 为 v2 candidate 自报执行事实；生产
验收入口必须使用 Store 绑定的比较函数。历史 reference 保持
`historical_unattested`，不得补造旧执行账本。

M1 的 20260623/20270623 配对按 `_0` 至 `_5` 六个内部 segment 分别登记。历史
`20260623_temp` 与 `20260623_temp_1` 都含 Tracking 产物，因此比较时必须显式提供
已核对的 `oracle_ref`；工具不得按目录名、时间或存在性自行挑选 reference。

M1 prepare 在 staging 根产生的业务产物固定分为三类：

- `samples/<date>/<segment>`：逐 segment 严格比较；
- `maps/`：由 `map_publish` 产生，单独严格比较；
- `v1.0-trainval/`：由 `metadata_generate` 产生，单独严格比较。

`.runtime/` 是系统私有输入、overlay 和执行账本，不是历史业务 oracle scope，
不能作为 case 或公开报告的一部分；它仍包含在 prepare artifact hash 中。Store
绑定根级 case 时会把 staging 顶层严格限制为
`.runtime/maps/samples/v1.0-trainval`，并验证 `samples` 只包含本 Job 日期且与
Store 中全部 tracked segments 一一对应。任何额外、缺失、symlink 或未登记的
根级产物都必须使 attestation 失败；不能用 ignore 或只跑 segment case 绕过。

contract v2 不允许 ignore pattern 或非零数值 tolerance。唯一动态图片策略是
`tracking_img_*/*` 只比较文件树、数量、格式和尺寸；唯一文档归一化是已登记 YAML
中的 `paths.img2video_mp4`，且只能替换指向各自 artifact scope 内 `dog.mp4` 的
精确值。YAML 的其他字节表示（包括键顺序和空白）、`img_*.txt`、其他图片、Schema
和数值必须严格比较。任何非白名单差异都应立即判为 `DIFFERENT`，报告相对文件、
字段/数值、阶段和推测原因；不得为通过验收而静默调整算法或放宽比较器。

对公司业务数据进行 oracle/candidate capture、dry-run 或真实处理不属于本地 M0
自动动作，必须在服务器验收阶段单独批准；capture 时输入必须处于停止写入的稳定
状态。真实配对验收以原日期既有同步/finish 产物作为只读 legacy oracle，以新系统
在 2027 测试日期生成的产物作为 candidate，不重新执行旧脚本。比较前必须证明
raw 副本同源、同步产物等价、内部 segment 映射正确，并仅归一化 registry 明确
声明的非业务差异。
