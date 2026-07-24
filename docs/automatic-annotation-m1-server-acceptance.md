# 自动标注 M1 服务器验收 Runbook

> 状态：2027 测试数据准备已通过；待人工批准 Runtime 部署、Xvfb 与真实 Tracking
> 适用分支：`codex/automatic-annotation-m1`
> 上位计划：`docs/automatic-annotation-m1-plan.md`
> 本文只定义验收步骤，不授权连接、部署、安装或运行服务器 writer。

## 1. 审批边界与停止条件

M1 服务器验收分为四类外部变更，每一类都需要用户单独明确批准：

1. 同步已审核的代码和系统专用 Runtime payload；
2. 安装固定 Xvfb、创建安装摘要和更新受控 Runtime manifest；
3. 仅对 2027 测试日期执行系统已有的拆解、同步能力；
4. 通过 Web 启动真实 prepare/Tracking writer。

一次批准不能自动覆盖后续阶段。dry-run、代码同步、任务创建或旧任务历史均不能
推断为真实 writer 授权。到达 Web 首帧标注、Golden oracle 选择或任何异常恢复
边界时，必须有用户或指定业务人员在场。

任一情况出现时立即停止，不自动重试：

- 本地、服务器和批准记录中的 commit 不一致；
- 工作树存在未说明修改，或数据库/schema 版本不符合本次代码；
- Runtime、Python、Xvfb、bubblewrap、模型、标定或依赖摘要哈希不匹配；
- 生产专用 work root、writer lock、磁盘安全余量或 GPU preflight 不满足；
- 旧进程组仍存在，或任务进入 `recovery_required`；
- 2026 oracle、`raw_data`、`clip_data`、`finish_data`、同事源码或公共 scratch
  出现非授权修改；
- API、日志或验收报告暴露绝对路径、内部数据库 ID、内部 segment 名、命令、
  脚本参数或凭据；
- Golden 出现任何非白名单差异。

## 2. 验收角色与证据目录

开始前记录以下角色，不使用共享的模糊责任：

- 批准人：批准每个写阶段及目标范围；
- 操作人：执行部署和只读核对；
- 标注人：通过 Web 复现首帧标注；
- oracle 确认人：确认 2026 权威历史产物；
- 复核人：检查 Golden、污染快照和回滚条件。

验收证据只能写入本次批准的系统专用目录，不得写入数据集、同事代码目录或公共
scratch。建议先由操作人设置以下任务专用变量，并逐项打印、人工核对其
`realpath`；变量为空、相对路径、指向 symlink 或超出已批准目录时停止：

```bash
M1_REPO=/media/heying/hy_data1/Trajectory_visualization/VLA-data-juicer-agents
M1_DATA_ROOT=/media/heying/hy_data1/VLADatasets
M1_WORK_ROOT=<approved-system-annotation-work-root>
M1_RUNTIME_ROOT=<approved-navigation-odom-v1-runtime-root>
M1_EVIDENCE_ROOT=<approved-m1-acceptance-evidence-root>
M1_APPROVED_COMMIT=<full-40-character-commit>
```

证据包至少包含：

- 审批记录、目标日期/clip、操作人和时间；
- commit、配置摘要、数据库备份标识和 migration 版本；
- 磁盘、GPU、Xvfb/bubblewrap 和 Runtime capability 结果；
- Runtime manifest 与安装摘要哈希；
- 2026 oracle 人工选择记录；
- 每个 Job/RuntimeRun 的安全引用和状态时间线；
- Golden JSON/Markdown 报告；
- 数据和同事目录的前后污染快照；
- 回滚或保留 staging 的最终决定。

私有证据可以保存服务器路径映射；公开 DataPilot 时间线、API 响应和脱敏 Golden
报告不得包含这些路径。

## 3. 部署前核对

### 3.1 Git 与工作树

在本地和服务器分别只读执行：

```bash
git -C "$M1_REPO" status --short
git -C "$M1_REPO" rev-parse HEAD
git -C "$M1_REPO" branch --show-current
```

必须同时满足：

- `HEAD` 等于 `M1_APPROVED_COMMIT`；
- 服务器工作树干净，或所有差异均已逐文件登记并重新批准；
- 当前代码来自 `f618c6c` 之后的 M1 分支，而不是落后的 `origin/main`；
- 前端 production build 与本次 commit 对应；
- 不通过 `git reset --hard`、宽泛 checkout 或覆盖式同步“修复”差异。

### 3.2 配置与敏感信息

人工核对配置是否显式绑定以下项目，但证据只记录“已设置、路径哈希或安全摘要”，
不要输出 API key、Bearer、完整环境转储或其他凭据：

```text
WORKING_DIR
VLA_DATA_AGENT_WEB_WORKING_DIR
VLA_ANNOTATION_WORK_ROOT
VLA_ANNOTATION_MINIMUM_FREE_BYTES
VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS
VLA_NAVIGATION_WRITER_LOCK_PATH
VLA_VLADATASETS_ROOT
VLA_TRACKING_BINARY_DATA_ROOT
VLA_TRACKING_LEGACY_DATA_ROOT
VLA_LEGACY_CLIP_DATA_ROOT
VLA_NAVIGATION_ODOM_V1_SOURCE
VLA_NAVIGATION_ODOM_V1_MANIFEST
AGENT_DATA_ENV_SETUP
AGENT_DATA_PYTHON
VLA_XVFB
VLA_XVFB_RUN
VLA_XVFB_DEB
VLA_BWRAP
VLA_RUNTIME_DEPENDENCY_SUMMARY
```

核对原则：

- 通过 `scripts/run_web.sh` 启动时，`WORKING_DIR` 是传给 Web CLI
  `--working-dir` 的权威值；只设置 `VLA_DATA_AGENT_WEB_WORKING_DIR` 时脚本采用
  该值，两者同时设置时必须逐字相同，否则脚本在构建或启动前 fail closed。
  `stop`、`status` 和 `logs` 不受该冲突阻断，确保紧急停止和诊断入口始终可用；
  两者都未设置时才回退到 `STATE_DIR`。脚本以 `umask 077` 创建新的 working、
  state 和日志目录，但不会 `chmod` 已有目录；已有目录权限不安全时必须停止并
  由操作人单独处理；
- `start`、`stop`、`restart` 和 `status` 必须通过
  `scripts/run_web_control.py` 在稳定的 `/usr` inode 全局锁及 PID 专用 control
  lock 下串行执行；验收机必须先证明支持对 `/usr` 执行 POSIX exclusive
  `flock`。PID parent 必须是真实、当前服务用户所有且不可被 group/other 写入的
  目录；PID/control/instance 文件不得是 symlink、非普通文件、硬链接、其他用户
  文件或 group/other 可写文件。PID 文件只允许一个大于 1 的规范 ASCII 十进制
  数，并且必须同时匹配 Web 进程终身持有的 instance token lock 和稳定的系统进程
  出生标识（Linux 为 boot ID 加 `/proc/<pid>/stat` starttime，macOS 为内核进程
  start time）；只有通过该实例复验（Linux 上同时使用 pidfd）的 PID 才允许收到
  TERM/KILL。stale 或复用 PID 只能清理匹配记录，禁止发送信号；旧 instance lock
  仍被持有时必须保留记录并阻断新启动。内部 lifecycle action 不得只信任环境变量
  标记，必须校验外层 helper 继承的 anchor、PID parent 和 control lock FD，并在
  启动 Web 前关闭这三个 control FD；
- `VLA_ANNOTATION_WORK_ROOT` 必须是绝对、规范、无 symlink ancestor、由当前
  服务 UID 持有且权限精确为 `0700` 的系统专用真实目录；Runtime 不会替操作人
  自动创建或修正权限。它不得位于 `raw_data`、`clip_data`、`finish_data` 或
  同事业务代码目录内；
- `VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS` 已审批为 `21600`，表示单条冻结
  Runtime 命令最多运行 6 小时。缺失、零、负数或其他格式时 capability 必须
  fail closed，禁止创建 writer Job；
- writer lock 必须是绝对路径，父目录归系统服务所有，不得是 symlink；
- Annotation Runtime 不得使用未配置时的 `/tmp` 兼容默认值；缺少
  `VLA_NAVIGATION_WRITER_LOCK_PATH`、父目录非当前服务所有、父目录可被 group/
  other 写入、路径为相对路径或存在 symlink 时，capability 必须 fail closed；
- `<lock>.active` 表示 writer/子进程组所有权，
  `<lock>.quarantine.<opaque-ref>` 表示一次独立的未知副作用事件；任意一个
  安全标记存在时 capability 必须返回协调不可用，Navigation writer 和
  Annotation claim 均不得调用底层业务动作；
- `VLA_VLADATASETS_ROOT` 只指向本次已确认的数据根；
- `VLA_TRACKING_BINARY_DATA_ROOT` 是 Tracking binary 内硬编码的 `_01/Data`
  绝对路径对应的 sandbox overlay target；`VLA_TRACKING_LEGACY_DATA_ROOT`
  是 legacy YAML 中 `/mnt/data1/.../Data` 绝对路径对应的另一个 overlay
  target。两者必须分别显式配置为不同的绝对路径，不能合并、互设为同一路径或
  省略其中任一挂载；运行时将同一份 job-private `Data` 分别映射到两个 target。
  binary target 继续要求宿主存在真实、规范的目录；Legacy YAML target
  是固定的 sandbox-only 逻辑路径，宿主只要求 `/mnt` 是真实、规范的目录，不得创建
  `/mnt/data1/gh/tracking_1/Data`。bubblewrap 必须先以私有 tmpfs 覆盖 `/mnt`，
  再仅在任务 namespace 内创建目录并 bind；任务退出后自动消失。能力检查必须从
  manifest 匹配的冻结 binary 只读证明其三个内嵌路径共享该 binary Data root，
  并证明 YAML 的 intrinsics/extrinsics 共享另一个 Data root；任意路径错配、
  嵌套或与 work、同步数据、Runtime source 重叠时 fail closed；
- `VLA_LEGACY_CLIP_DATA_ROOT` 必须精确匹配 manifest 冻结的
  `NoobScenes/include/1_odom_convert.py` 中 `clip_data` 字面量；只允许把
  job-private 同步输入以只读方式映射到该兼容目标；
- Python 和 setup 必须是 M0 冻结的活动环境，不使用交互式 SSH 默认 Python；
- processing calibration 页面只提供 manifest 登记的 `20260320` 和
  `20260529_go2w`，不显示跨数据集推荐；
- M1 不配置 `20260409_U` 为 processing calibration。

### 3.3 数据库

停止 Web 服务和 Annotation Worker 后核对：

- `annotation.sqlite` 位于启动脚本最终传入的有效 `WORKING_DIR` 下；若同时设置
  `VLA_DATA_AGENT_WEB_WORKING_DIR`，它必须与 `WORKING_DIR` 完全相同；
- SQLite foreign keys、WAL、busy timeout 和 migration ledger 可用；
- 数据库版本不超前、不缺迁移；
- 当前无 `running` RuntimeRun 或未过期 writer lease；
- 备份文件写到本次专用证据/备份目录，而不是数据库同目录的模糊覆盖文件。

可使用 SQLite 的在线备份命令创建带时间戳的独立备份；备份完成后立即执行
`PRAGMA integrity_check` 并记录 SHA-256。不得直接复制一个仍由 writer 写入的
WAL 数据库，也不得覆盖上一次备份。

Web 首次启动会在 Annotation DB 同目录创建私有 service/maintenance lock。取得
lease 的固定顺序为系统根 `/` 目录 inode、Annotation DB 父目录 inode、私有 lock
文件 inode，三个 flock 都必须持有到 Annotation Worker 完全停止；因此即使同 UID
进程轮换整个 working directory，也不能为第二个 Web/CLI 建立新的锁域。该保守
策略把同一主机上的 Annotation lifecycle 全局串行化；任一层不支持 flock 或无法
安全打开时均 fail closed。异常恢复只允许在 Web 服务和 Annotation Worker 均已
停止、所有相关进程组已由操作人核实消失后，通过停机运维入口执行；CLI 不创建
缺失的 maintenance lock，锁缺失或 Web 仍在线时一律 fail closed：

```bash
"$M1_REPO/.venv/bin/python" -m \
  vla_data_juicer_agents.annotation.operator_cli \
  --annotation-db "$VLA_DATA_AGENT_WEB_WORKING_DIR/annotation.sqlite" \
  --writer-lock "$VLA_NAVIGATION_WRITER_LOCK_PATH" \
  list-recovery
```

`clear-global` 必须携带精确确认串
`all_navigation_annotation_writer_process_groups_absent`，随后
`confirm-job retry|abandon` 必须携带精确确认串
`old_process_group_absent`；两步都必须提供 operator/ticket reference 和全新
Idempotency-Key。不得在服务仍运行时执行，不得跳过 `list-recovery`，不得通过
删除 marker、编辑 SQLite 或调用未鉴权 Web API 绕过恢复审计。入口只输出安全
引用、公开状态和安全错误码，不输出绝对路径、内部数据库 ID、命令或私有故障
详情。CLI 参数必须精确匹配显式
`VLA_DATA_AGENT_WEB_WORKING_DIR/annotation.sqlite` 和
`VLA_NAVIGATION_WRITER_LOCK_PATH`；它以 existing-only `mode=rw` 打开 DB，
不创建、不迁移、不 chmod，并在连接与事务边界持续校验 DB inode。

### 3.4 work root 与容量

只读核对：

```bash
realpath "$M1_WORK_ROOT"
findmnt -T "$M1_WORK_ROOT"
df -h "$M1_WORK_ROOT"
df -B1 "$M1_WORK_ROOT"
```

记录：

- 总容量、可用字节和配置的安全余量；
- 当前活动 Job 的 reservation 总量；
- 选中同步数据的 image、lidar、odom 总字节数；
- 当前 staging 字节数；
- 预计 prepare 和 Tracking 峰值。

创建 Job 和 Worker 执行前都必须通过 fail-closed capacity preflight；人工等待后
开始 Tracking 时必须再次计算，不能复用创建任务时的旧结果。先验收较小的
`160904`；只有它结束并重新核对容量后，才可启动六个内部 segment 的 `145550`。
`VLA_ANNOTATION_MINIMUM_FREE_BYTES` 已审批为 `107374182400`（100 GiB）。这是
任务估算之外必须保留的安全余量，不会预分配或直接占用 100 GiB。

## 4. 污染基线快照

2027 拆解、同步完成后、M1 writer 开始前，创建一份只读污染基线。快照文件必须
写到 `M1_EVIDENCE_ROOT`，不能写回被审计目录。

至少覆盖：

- 2026 oracle 对应的 `raw_data`、`clip_data`、`finish_data`；
- 2027 测试日期已完成同步后的 `raw_data` 和 `clip_data`；
- 2027 `finish_data`（M1 不应发布到此处）；
- `_01/run_odom.sh` 所在同事业务目录及 frozen Runtime 依赖；
- 同事公共 Tracking `Data`/scratch；
- 系统专用 Runtime source；
- `annotation.sqlite` 和系统专用 work root。

每份快照记录相对文件名、类型、大小、mtime 和普通文件 SHA-256；遇到 symlink、
socket、device 或其他特殊文件单独报告。大目录可以使用已审核的流式 manifest
工具，但不能只记录目录 mtime，也不能把绝对路径写进公开报告。

预期变化范围必须先登记：

- M1 允许变化：`annotation.sqlite`、系统专用 work root、系统私有日志和本次
  evidence；
- M1 不允许变化：所有 2026 数据、2027 `raw_data`/`clip_data`/`finish_data`、
  同事源码、frozen Runtime source 和公共 scratch。

验收结束后使用同一工具、同一范围和同一排序规则重新捕获；任何不在允许范围内的
变化均判失败。

## 5. 系统专用 Runtime 与无界面环境

### 5.1 部署 frozen payload

在单独批准后：

1. 停止所有 Navigation/Annotation writer；
2. 将 manifest 中的 frozen payload 按原字节复制到新的、带版本号的系统专用
   Runtime 目录；
3. 校验每个文件的 SHA-256、大小和 executable bit；
4. 大型 Tracking binary、ONNX、配置和 map 仍保持原业务内容；
5. 不修改、格式化、重编译、优化或删减业务算法；
6. 不把同事可变业务目录作为长期 runtime source；
7. payload 目录和文件不得对 group/other 开放写权限；
8. payload 不匹配时停止，不自动选择同级“相似版本”。

使用仓库验证器核对 frozen source：

```bash
vla-nav-runtime-manifest verify-root \
  --manifest "$M1_REPO/runtime/navigation_odom_v1/manifest.json" \
  --root-alias NAVIGATION_ODOM_V1_SOURCE \
  --root "$M1_RUNTIME_ROOT/source"
```

验证报告只能进入私有 evidence，并确认其中不包含未脱敏绝对路径。

### 5.2 Xvfb、bubblewrap 与安装摘要

固定版本为：

```text
2:1.20.13-1ubuntu1~20.04.20
```

2026-07-24 已获得安装审批。APT 模拟结果为新增一个 `xvfb` 包、升级 0、降级
0、删除 0；随后只安装了上述固定版本，新增磁盘占用约 2.3 MB。APT 下载并用于
安装的确切 deb 仍保留在 cache，已逐字节复制到系统专用 Runtime 安装证据目录。
复制前后 SHA-256 一致，`dpkg-deb` 中的 package/version/architecture 与实际
`dpkg-query` 结果一致。以后重新安装或升级仍须先保存并记录确切 deb 的 SHA-256。

安装后只读捕获：

- Xvfb deb 的 SHA-256、大小和版本；
- 实际 `/usr/bin/Xvfb`；
- 实际 `/usr/bin/xvfb-run`；
- 实际 `/usr/bin/bwrap`；
- 已安装系统依赖的稳定排序摘要；
- `dpkg-query` 返回的 Xvfb 精确版本。

以上证据分别登记为：

```text
xvfb_deb_package
xvfb_server_binary
xvfb_launcher
sandbox_binary
runtime_dependency_summary
```

真实哈希只能在服务器安装后只读捕获，再通过单独审核的 manifest 提交加入仓库；
不得在本地猜测或手填占位哈希。在这五项缺失、文件不匹配或版本不符时，
`GET /api/annotation/capabilities` 必须返回不可用，禁止创建 writer Job。

本次安装证据为：

| role | SHA-256 | size | executable |
|---|---|---:|---:|
| `xvfb_deb_package` | `b671759ad2b8280723b0b55361368dc69c12cb301ea9a6a5e443e4f8d2745a2d` | 780884 | false |
| `xvfb_server_binary` | `d341ff11d9235f85edfd481c884517a80af0ca862350a432f3b22cea624c55a4` | 2056648 | true |
| `xvfb_launcher` | `48ee444c30fdaaede4cc311644b30e554162e956f949b206858e37eb8ba1ae05` | 5701 | true |
| `sandbox_binary` | `af662c55cd85178a58da083220a9348c4a7d3c24333fd0bc7badb18c93392987` | 68032 | true |
| `runtime_dependency_summary` | `04f79d26200ff993732fd5fb3e184589ec9bb9d5886e154f0f99fe64fd06bb00` | 2211 | false |

依赖摘要列出 18 个稳定排序的系统包及其架构、精确版本。Runtime 不只校验摘要
文件本身，还严格解析其 Schema，并重新使用 `dpkg-query` 核对每个包的实际版本；
摘要格式、文件或系统包发生漂移时均 fail closed。

更新 manifest 后重新部署对应 commit，并再次执行完整 Runtime、Python、package、
GPU 和安装摘要 preflight。记录 capability 的安全错误码，不把内部路径或命令写入
公开响应。

### 5.3 关闭 XQuartz

正式 Web 验收前：

1. 在开发 Mac 上正常退出 XQuartz；
2. 用 `pgrep -x XQuartz` 确认它未运行；
3. SSH 连接不使用 `-X` 或 `-Y`；
4. 服务进程外不依赖用户提供的 `DISPLAY`；
5. prepare/Tracking 只能通过固定
   `xvfb-run --auto-servernum --server-args="-screen 0 1920x1536x24 -nolisten tcp -noreset"`
   启动 bubblewrap；
6. Xvfb 或 DISPLAY preflight 失败时停止，不回退 XQuartz 或桌面 GUI。

同时验证 bubblewrap 的宿主根和 frozen Runtime 为只读、job staging 为私有可写、
Tracking binary 的硬编码 `_01/Data` target 与 YAML 的
`/mnt/data1/.../Data` target 分别映射到同一 job-private scratch、GPU 可见，并
执行超时与进程组清理 preflight。Legacy YAML target 的无业务 smoke 必须在运行
前后都证明宿主 `/mnt/data1/.../Data` 不存在，沙箱写入只进入临时 job-private
Data；同事 `_01/Data` 的文件树和摘要保持不变。两个 target 不能合并。超时
preflight 使用独立
的无业务副作用测试命令，确认达到已批准的
`VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS` 后，系统按同一
SIGTERM→SIGKILL 语义清理完整进程组；不得为测试超时而启动真实 Tracking，也不得
把 timeout 关闭或设为无限。

> 2026-07-24 已使用服务器 bubblewrap `0.4.0-1ubuntu4.1` 完成无业务 smoke：
> `--ro-bind / /` 后直接 `--dir` 会按预期因只读根失败；使用
> `--tmpfs /mnt`、逐级 `--dir`、再 bind job-private Data 时成功。测试前后宿主
> `/mnt/data1` 均不存在，沙箱写入只出现在临时私有目录，临时目录已清理。
> 本记录只证明 sandbox-only 挂载拓扑，不代表 Xvfb、sandbox 内 GPU、超时、
> 进程组清理或真实 Tracking 门禁已经通过。

> 2026-07-24 安装固定 Xvfb 后又完成两项无业务 smoke：固定参数启动的 Xvfb
> 可被 `xdpyinfo` 访问；同一 DISPLAY 进入 bubblewrap 后仍可访问，且沙箱内
> `nvidia-smi -L` 成功。该结果证明 Xvfb、DISPLAY 和 GPU 在当前无业务沙箱
> 拓扑中可用，仍不代表真实 Tracking、Golden 或业务算法已经验收。
>
> 安装证据 manifest 随提交 `896673f` 同步到服务器后，Schema 92 项和部署的
> 55 个 frozen files 再次验证通过；以 Web 服务 UID、正式 Python 和完整 pending
> 配置执行的直接 capability 探针返回 `available=true`。另用完全无业务的临时
> Python sleep 进程验证超时清理，包含忽略 SIGTERM 的后代进程组在约 2.1 秒内
> 完成 TERM→KILL 清理。Web 服务仍保持停止，尚未创建 Annotation Job、运行
> prepare/Tracking 或写入 `finish_data`。

## 6. 2027 测试数据准备顺序

> 2026-07-24 结果：本节前置门已完成并通过。四个 raw DB3、`tmp_dir` 和
> `sync_data` 的同步阶段业务文件均与对应 2026 来源严格一致；`145550` 的六个
> 内部 segments 全部覆盖。2027 候选没有历史目录中的 `grid_map`，这是后处理
> 产物的预期阶段差异，不得为 M1 补生成或复制。2027 `finish_data` 未产生。

拆解、同步不是 M1 业务阶段，但属于服务器验收前置 writer。必须单独批准，并只用
系统已有的新工具处理以下测试副本：

```text
20270605:
  20260605_152856
  20260605_152930
  20260605_160904

20270623:
  20260623_145550
```

顺序固定：

1. 核对 2027 raw 副本与原数据的来源记录；
2. 只对上述日期/clip 完成拆解、同步；
3. 验证同步结果位于 2027 日期范围；
4. 停止拆解/同步 writer；
5. 再次核对没有相关进程；
6. 对完成同步的 2027 `raw_data`/`clip_data` 捕获 M1 污染基线；
7. M1 之后只读取这些同步产物。

不得对 20260605 或 20260623 运行新的拆解、同步、prepare、Tracking 或旧脚本。
不得把 2027 M1 staging 发布进正式 `finish_data`。

## 7. 2026 oracle 的只读选择

2026 产物只作为历史 reference：

```text
20260605_160904  → 20270605_160904
20260623_145550  → 20270623_145550
```

操作要求：

- 不在 oracle 目录运行任何业务脚本；
- 不生成、覆盖或“修复”其 YAML、Tracking 图片或 points；
- reference 标记为 `historical_unattested`，不补造旧 RuntimeRun；
- capture 前后比较 oracle 污染快照，必须完全一致；
- comparator 只以只读方式打开 reference；
- 历史绝对路径只能进入精确登记的
  `paths.img2video_mp4` normalization，不能增加宽泛替换。
- 比较前逐个确认 2026 `finish_temp` scope 是否已经混入 `grid_map`、投影、世界坐标、
  轨迹或其他 M2 后处理产物；这些文件不是 M1 Tracking 的允许输出。若 candidate
  与 oracle 因此出现 missing/extra，仍立即 STOP，并把首要可疑点记录为
  `historical_oracle_contains_postprocessing_artifact_outside_m1_boundary`。不得通过
  patterns、ignore 或复制 candidate 产物来排除差异；必须由业务确认真正位于
  Tracking 时点的历史 oracle，或另行评审一个 stage-scoped contract。

当前两个门禁 clip 都存在重复历史候选：`20260605_temp` 与
`20260605_temp1` 均包含 `160904`，`20260623_temp` 与
`20260623_temp_1` 均包含 `145550` 的六个 segments。系统不得根据目录名、
mtime、文件数量或“看起来更完整”自动选择。在运行对应 Golden 前，由 oracle
确认人逐项检查并分别书面确定权威根，记录：

- 两个候选根的只读 fingerprint；
- 选择理由和确认人；
- 适用的 clip，以及 `145550` 的 `_0…_5` segments；
- 随验收证据保存的 opaque `oracle_ref`。

未确认时停止。`160904` 与 `145550` 的所有 Golden case 都必须显式携带各自已
确认的 `oracle_ref`。

同一权威 `finish_temp` 根还必须覆盖 `maps/` 与 `v1.0-trainval/`。这两个目录
分别使用登记的 `m1_prepare_maps_*`、`m1_prepare_metadata_*` case 比较，不能
因为六个 segment 已通过就省略。`.runtime/` 仅是新 Runtime 的私有 staging，
不属于历史业务 oracle；它不进入 comparator scope，但始终受 prepare manifest
hash 约束。Store 会拒绝 staging 顶层出现
`.runtime/maps/samples/v1.0-trainval` 以外的任何目录或文件，也会拒绝未映射到
Store tracked segment 的 `samples` 子目录。

## 8. M1 Web/Tracking 验收顺序

### 8.1 主门禁：160904

1. 确认 XQuartz 已关闭、capability 为 available；
2. 在 `/annotation/jobs` 选择日期 `20270605`、外层 clip
   `20260605_160904` 和当天 processing calibration；
3. 记录创建时的容量结果、`job_ref` 和 CalibrationSnapshot 哈希；
4. 等待 prepare 到 `waiting_initial_annotation`，记录 prepare Runtime manifest
   与 prepared artifact tree 哈希；
5. 刷新、前进和后退，确认恢复相同 job/segment；
6. 用 Web 数字输入复现已确认 oracle 的首帧标注；
7. 核对 resize 后图片 natural size、bbox、point、master/other 顺序和三种颜色；
8. 提交不可变 revision，显式点击“开始 Tracking”；
9. 再次检查容量，然后运行 Tracking；
10. 核对每个 target checkpoint、最终 `tracked` 状态，并确认 Tracking manifest
    中的实际 Runtime/prepared 哈希与 prepare 两项完全一致；
11. 核对 `runtime_run_steps` 中 prepare、首帧 YAML 和 Tracking 的真实
    `started → succeeded/failed` 账本；成功语义步骤必须有返回码 0，失败步骤
    只能带安全 diagnostic ref，不能包含路径或命令；命令失败的非零返回码及受限
    stdout/stderr tail 只能进入私有 failure detail；
12. 通过 Store-bound Golden 入口依次比较
    `m1_prepare_maps_20260605_160904`、
    `m1_prepare_metadata_20260605_160904` 和
    `m1_tracking_20260605_160904`；三者共同组成
    `20270605_160904 ↔ 20260605_160904` 的完整 M1 门禁。

不得通过 CLI JSON、命令行参数或人工文本自报 candidate 的 Runtime manifest、
命令顺序、标定或 revision-set；这些事实必须来自已提交
`AnnotationStore.golden_candidate_binding(run_ref, case identity)`。该绑定同时从
Store 校验 succeeded Tracking run、Job 日期、所选 source clip、内部 segment
映射和私有 staging；命令行不得出现 `--candidate-root`、
`--candidate-source-root` 或 candidate artifact scope。

在执行生产比较命令前，先对 baseline/candidate scope 内的 JSON、YAML 和文本类
结构化产物做只读枚举，检查是否嵌入了 `2026/2027` 日期 token、oracle/staging
artifact root 或其他环境相关绝对路径。扫描结果只进入私有 evidence，不能把路径
写入公开报告。当前实现不会擅自归一化这些尚未由真实产物证明存在的字段；发现后
应将比较保持为 `DIFFERENT` 并立即 STOP，先向用户报告精确文件、selector 和可疑
来源。只有业务确认其属于非业务差异并单独批准后，才能为固定
`path_pattern + selector` 增加极窄规则；值还必须精确对应 role date 或解析到
该 role 的安全 scope，且表示在文档中唯一出现。禁止正则或全局替换。

### 8.2 多 segment 门禁：145550

仅在 160904 通过、污染快照无异常并重新核对磁盘后继续：

1. 创建 `20270623 / 20260623_145550` Job；
2. 确认系统发现且页面匿名展示六个内部 segment；
3. 完成六个首帧标注并逐一提交；
4. 显式开始 Tracking；
5. 每个 target 后检查 durable checkpoint；
6. 先运行 `m1_prepare_maps_20260623_145550` 与
   `m1_prepare_metadata_20260623_145550`，再分别运行 `_0…_5` 六个
   Tracking case；
7. 每个 case 显式使用已人工确认的 `oracle_ref`；
8. 两个 prepare-global case 和六个 segment case 全部等价后才判该 clip
   通过。

### 8.3 恢复 smoke：152930

`20270605 / 20260605_152930` 只用于恢复行为：

- 草稿自动保存后刷新页面；
- 409 时确认不会自动合并几何；
- 等待标注期间确认不持有 writer lock；
- 在已批准时机取消运行中的隔离进程组；
- 确认 staging 与审计保留；
- retry 先验证 committed checkpoint 哈希，只运行未完成 target；
- 进程状态或副作用不明确时必须得到 `recovery_required`，不得自动重试。

#### 8.3.1 `recovery_required` 的唯一受支持恢复顺序

`recovery_required` 是跨 Navigation 与 Annotation 的全局 writer 隔离，不是某个
Job 的普通失败。操作人必须按以下顺序处理：

1. 停止 Web/Annotation Worker 以及所有 Navigation plan writer，不再创建或
   claim 新任务；
2. 只读核对系统进程、进程组、GPU 进程和本次私有日志，确认所有
   Navigation＋Annotation writer 进程组均已消失；仅确认某个 Annotation Job
   的 PID 不足以通过；
3. 通过受控运维入口调用全局 clear，confirmation 必须精确为
   `all_navigation_annotation_writer_process_groups_absent`，并填写操作人或
   ticket reference 与新的 idempotency key；
4. 系统先 durable 写入 action intent，再非阻塞取得共享 flock；在 `.active`
   和所有 append-only quarantine marker 原样保留期间提交 completion 审计，
   commit 成功后才精确删除该 action 捕获的 marker 集合并释放 flock；
5. 保存 completed 的 `global_quarantine_action_ref`。若 intent 后出现新的
   recovery marker，旧 action 必须失败且不得删除新 marker，操作人需重新完成
   第 1～4 步；
6. 对每个 `recovery_required` Job 单独调用 operator confirm，引用上述 completed
   global action，并明确选择 `retry` 或 `abandon`；Job confirm 现场仍须确认
   当前不存在任何新 marker；
7. `retry` 后重新进行 Runtime、容量和 checkpoint preflight；`abandon` 只释放
   该 Job 的 source scope，保留 staging、失败记录和全局审计。

禁止手工删除 `.active`、任何 `.quarantine.<opaque-ref>`，禁止普通
`POST /retry`/`POST /cancel`，也禁止直接修改 Job、RuntimeRun、lease 或 operator
audit 表。若 completion INSERT/commit 失败，marker 必须仍在；使用同一
idempotency key 重放 pending global action。若 completion 已提交但 marker
清理失败，marker 继续 fail closed，同样只允许重放原 action。该 action 逐项
绑定原 marker 摘要，只能补删当前仍存在的原集合子集；若现场还出现任意新
marker，旧 action 对旧残留和新 marker 都不得删除，必须重新执行第 1～4 步并
使用新的 idempotency key。任一步出现未解释的 marker 状态变化、flock busy、
审计不完整或数据库错误时停止并保留证据。

### 8.4 provenance 诊断：152856

`152856` 的历史产物为 1280×720，与当前 `2_resize.py` 正向基线不一致。它只能
用于只读 provenance 诊断，不得作为当前 resize 等价通过证据，也不得为匹配它而
修改算法、图片规则或 Golden comparator。

## 9. Golden 判定规则

### 9.1 唯一生产比较命令

先为每个 case 创建一个新的、空的证据子目录，并人工核对以下变量。annotation
数据库必须是已经存在的普通文件，不能是 symlink、FIFO、device 或待 CLI 创建的
空路径；Tracking run 必须已提交为 `succeeded`。oracle root 和可选 source root
必须来自第 7 节已确认的只读历史范围：

```bash
M1_ANNOTATION_DB=<existing-annotation.sqlite>
M1_TRACKING_RUN_REF=<succeeded-tracking-run-ref>
M1_GOLDEN_CASE=m1_prepare_maps_20260605_160904
M1_ORACLE_ROOT=<approved-read-only-oracle-root>
M1_ORACLE_REF=<approved-opaque-oracle-ref>
M1_GOLDEN_OUTPUT="$M1_EVIDENCE_ROOT/golden/$M1_GOLDEN_CASE"
```

固定命令为：

```bash
vla-nav-golden compare-annotation-run \
  --cases "$M1_REPO/runtime/navigation_odom_v1/golden-cases.yaml" \
  --case "$M1_GOLDEN_CASE" \
  --annotation-db "$M1_ANNOTATION_DB" \
  --candidate-run-ref "$M1_TRACKING_RUN_REF" \
  --baseline-root "$M1_ORACLE_ROOT" \
  --baseline-oracle-ref "$M1_ORACLE_REF" \
  --output-dir "$M1_GOLDEN_OUTPUT"
```

对 `20270605_160904` 必须分别把 `M1_GOLDEN_CASE` 设为：

```text
m1_prepare_maps_20260605_160904
m1_prepare_metadata_20260605_160904
m1_tracking_20260605_160904
```

对 `20270623_145550` 必须运行两个同名日期的 prepare-global case 和 `_0…_5`
六个 Tracking case。每个 case 使用独立空证据目录；任一退出码非 0 都立即停止，
不能用其他 scope 的通过结果替代。

除逐 segment Tracking scope 外，每个 candidate prepare run 还必须用 Store-bound
固定 allowlist 比较根级 `maps/` 与 `v1.0-trainval/` scope；两者不得由 CLI
自报路径，也不得因 segment case 通过而省略。它们应分别对照批准的 2026 oracle
根级产物，严格枚举文件树、结构化内容及非动态图片。prepare staging 中未被
segment scope 或这两个根级 scope 覆盖的新增顶层目录/文件，应由根布局校验直接
判为基础设施失败。

只有登记了 `source_expectations` 的 case 才在同一命令中增加
`--baseline-source-root "$M1_ORACLE_SOURCE_ROOT"`；该变量仍必须指向已批准的只读
oracle source。不得增加任何 candidate root/source/scope 参数，也不得为了满足
case 的 `samples/<date>/<segment>` 层级而复制、软链接或重组 staging。比较器从
Store 中保存的 staging 与 segment 映射直接读取实际 scope。

退出码固定解释如下：

- `0`：业务等价，JSON 和 Markdown 已通过安全扫描并写入新证据目录；
- `1`：存在 Golden 差异，报告已写出；立即停止当前 clip 和后续 writer；
- `2`：基础设施、Store provenance、输入安全或报告安全扫描失败；不得把它解释为
  业务差异或通过，也不得复用旧报告。

公开 JSON/Markdown 只保留 candidate `run_ref`、`oracle_ref`、Runtime manifest、
CalibrationSnapshot 和 annotation revision-set 的 SHA-256，以及脱敏差异。
不得包含 staging/oracle 的绝对路径、内部数据库 ID、内部 segment 映射、命令参数
或凭据。命令前后都要验证 oracle 污染 fingerprint 完全不变。

M1 允许的非字节比较只有：

- 各角色登记的日期和 artifact scope；
- YAML 中精确 selector `paths.img2video_mp4`，且值必须指向各自 scope 内
  `dog.mp4`；
- `tracking_img_*/*` 的文件树、数量、格式和尺寸比较，不比较动态叠字造成的
  图片内容哈希。

以下内容继续严格：

- YAML 的其他字段及字节表示，包括键顺序和空白；
- `img_*.txt`；
- 非 Tracking 动态图片；
- 文件树和数量；
- JSON/YAML Schema；
- gridmap/轨迹数值（虽不属于 M1 输出，比较器不能放宽）；
- Runtime manifest、CalibrationSnapshot、annotation revision-set 和命令顺序。

任何非白名单差异必须立即停止当前 clip 和后续 writer。报告至少包含：

```text
relative_file
first_selector_or_numeric_location
baseline_value_or_hash
candidate_value_or_hash
pipeline_stage
suspected_cause
candidate_run_ref / oracle_ref
runtime_manifest_sha256
calibration_snapshot_sha256
annotation_revision_set_sha256
```

不要在报告中写绝对路径、数据库 ID 或凭据。不得为了得到 PASS 而：

- 改写或简化业务算法；
- 扩大 tolerance；
- 增加 ignore pattern；
- 增加宽泛 normalization；
- 排序数组；
- 全局忽略图片；
- 静默换 oracle；
- 把差异降级为 warning。

只有业务同事确认差异原因并明确批准新的业务行为后，才可把算法变化作为独立任务
处理；原验收保持失败记录。

## 10. 最终污染检查与退出条件

完成每个 clip 后都执行一次增量污染检查；全部任务结束后执行完整前后对比：

- 2026 oracle：零变化；
- 2027 `raw_data`/`clip_data`：M1 阶段零变化；
- 2027 `finish_data`：零变化；
- 同事源码和 frozen Runtime source：零变化；
- 公共 Tracking scratch：零变化；
- 系统专用 work root：只出现登记的 Job/attempt/run；
- Annotation DB：状态、revision、checkpoint、manifest 和 receipt 与时间线一致；
- API/前端/日志：无私有路径、内部 ID、内部 segment、工具名和参数泄漏。

M1 只有同时满足以下条件才能冻结：

- 不使用 XQuartz；
- Web 可完成全部 segment 首帧标注；
- Legacy YAML 和原 Tracking 与 oracle 等价；
- 160904 与 145550 六个 case 全部通过；
- 152930 刷新、取消和恢复可靠；
- 原始、同步、正式产物和同事环境无污染；
- Python 全量、前端测试、生产构建和 Router 冻结基线无回归；
- 服务器证据包经操作人、oracle 确认人和复核人签字确认。

## 11. 安全回滚

回滚也需要单独批准，且只作用于系统专用目录和本次部署状态：

1. 停止 Web 服务和 Annotation Worker，确认无子进程组；
2. 保存当前失败状态、日志、数据库和 staging 的只读证据；
3. 将当前 `annotation.sqlite` 移到本次专用隔离位置，再恢复已验证备份；
4. 将系统专用 Runtime 的活动版本指针切回上一个已验证版本；
5. 将代码切回批准的旧 commit，使用非破坏性分支/部署切换，不运行
   `git reset --hard`；
6. 保留失败 Job staging，除非之后对某个精确 `job_ref/run_ref` 取得删除批准；
7. 重新执行 capability、数据库 integrity 和污染检查；
8. 记录哪些内容已恢复、哪些隔离证据仍保留及其恢复方式。

禁止提供或执行针对以下目标的递归宽泛删除：

```text
数据集根
仓库根
系统 Runtime 根
系统 annotation work root
用户主目录
任何未解析变量、glob 或 symlink
```

不得在回滚中删除或覆盖 `raw_data`、`clip_data`、`finish_data`、同事代码、公共
scratch 或历史 oracle。需要清理时，应在新的审批中先解析一个精确的系统私有
`job_ref/run_ref` 目录、验证其 realpath 和审计归属，再采用可恢复隔离，而不是
直接删除。
