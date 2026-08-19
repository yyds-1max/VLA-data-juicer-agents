# 模型训练模拟链路

模型训练任务的首个里程碑只运行模拟链路，不读取 NaVILA 目录，也不创建真实训练
进程。训练节点页可以在显式配置中心 HTTPS 地址后，通过一次 SSH 授权自动部署只读
Worker；该 Worker 仍不能执行训练命令，只能处理中心签发的固定类型只读验证任务。

## 本地启用管理操作

默认情况下训练 API 只读，所有模型登记、模拟启动和停止请求都会被拒绝。仅在
本地开发时显式设置以下变量：

```bash
export VLA_TRAINING_DEV_ADMIN=1
export VLA_TRAINING_SIMULATION_ENABLED=1
export VLA_TRAINING_FAKE_TICK_SECONDS=0.25
./scripts/run_web.sh foreground
```

训练状态默认保存在 Web working directory 下的 `training.sqlite`。需要隔离本地
状态时，可以设置一个绝对路径：

```bash
export VLA_TRAINING_DB_PATH=/tmp/datapilot-training.sqlite
```

`VLA_TRAINING_DEV_ADMIN` 只提供临时开发身份，不是账号系统。同步到运行服务器时
应删除该配置；服务器默认保持训练写操作禁用。

## 本地验收

1. 打开“模型训练”，确认页面标明真实训练未启用。
2. 登记一个模型族及其当前训练定义；模型登记本身不产生模型版本，
   `num_video_frames` 的实际字段值决定帧数。
3. 尚未登记训练节点时，确认“服务器资源”显示空状态，不出现示例 GPU。
4. 登记训练节点并部署 Worker，确认真实 CPU、内存、磁盘和 GPU 快照出现在
   “服务器资源”。
5. 在模型族上执行配置验证，确认检查由绑定节点的 Worker 只读完成。
6. 在绑定真实节点的模型族中选择 GPU、配置多个阶段并生成真实 RunSpec 预览，确认
   第二阶段接收第一阶段输出目录。该开发期预览不得创建任务、GPU 租约、模型版本或
   训练进程。

预览中的 argv 仅用于展示。Fake Provider 和 Fake Runner 仍用于自动化测试，但不会
作为 Web 资源目录的兜底数据；没有已登记训练节点时，公共服务器列表为空。

## 模型族、训练版本与多阶段训练

“模型登记”不是粘贴并执行一段 Shell。登记页只创建一个模型族，并保存该模型族
当前可编辑的训练定义：运行节点、launcher、工作目录、训练入口、输出根目录、固定
argv、参数定义、运行环境和指标日志契约。模型登记不会产生 `v1`，页面也不再提供
“登记新版本”“基于版本”或“版本说明”。

训练定义每次保存都会产生内部不可变 revision，但 revision 只是并发控制和历史快照
机制，不是用户管理的训练配置版本。保存后模型族回到 `draft`，需要重新执行只读验证。
模型族训练过后仍可继续修改；修改只影响以后创建的任务，已经创建的训练任务继续使用
创建时保存的参数、RunSpec 和训练定义快照。

模型版本由训练产生。一次训练任务成功持久化时，系统在同一事务中分配一个模型版本，
格式为 `vN-YYYYMMDD`，日期使用 `Asia/Shanghai`。预览、参数错误或 GPU 冲突不会占用
版本号；任务一旦创建，即使之后失败、取消或变为 `lost`，版本号也会保留。幂等重试
返回同一个任务和版本。同一个模型族的编号连续增加且不会复用。

管理员登记参数时可逐项设置字段名、CLI flag、类型、默认值、范围、枚举选项、
是否敏感、argv 表达方式、用途、解释、依赖关系和展示分组。参数用途包括普通超参数、
唯一的数据集输入和唯一的“阶段输入参数”。阶段输入参数必须是字符串，用来接收上一
阶段的结构化输出目录。产物输出参数由启动模板的 `output_flag` 声明并由平台管理，
不能再注册成普通参数。

启动方式是显式契约。`torchrun` 会由平台加入单机 `nnodes=1`、按所选 GPU 数计算的
`nproc_per_node`、master 地址/端口和 node rank；`direct` 只生成
`executable + entrypoint + argv`，不会加入 torchrun 参数，也不会申请 master port。
枚举、数值和字符串约束，参数依赖关系、展示分组及布尔 argv 表达方式仍按登记定义
进行两层校验。敏感值在公共响应和命令预览中遮蔽。

新建训练默认只有“第一阶段”，最多可增加到“第十阶段”。新增阶段会复制前一阶段的
全部参数值，各阶段使用同一模型族训练定义、训练节点和 GPU，但参数值可分别修改。
阶段名称由系统自动生成，不支持自定义、拖拽或单独复制。删除后续阶段时需要确认，
其后的阶段会自动重新编号。

第一阶段的阶段输入只能手动填写。第二阶段起，如果模型族登记了阶段输入参数，默认
选择“使用上一阶段输出目录”，平台会把上一阶段的 `output_directory` 写入该参数；
也可以切换为手动填写。没有登记阶段输入参数时仍可创建多阶段训练，但每个阶段都必须
手动提供路径。平台只传递目录字符串，不扫描 checkpoint、不解析权重文件，也不判断
模型内部如何加载。

预览不创建版本，输出目录使用：

```text
<output_root>/<family_ref>/preview/stage-01
<output_root>/<family_ref>/preview/stage-02
```

任务创建后的实际目录使用：

```text
<output_root>/<family_ref>/vN-YYYYMMDD/stage-01
<output_root>/<family_ref>/vN-YYYYMMDD/stage-02
```

Fake Runner 按阶段顺序执行。上一阶段成功后才进入下一阶段；当前阶段失败时，父任务
失败且后续阶段标记为跳过；停止任务会取消当前和未执行阶段；Worker 租约失效时当前
阶段和父任务进入 `lost`。GPU 与端口租约覆盖整个多阶段任务，只在父任务终态统一
释放。日志和指标的 seq 在整个任务内单调递增，同时带阶段游标供详情页筛选。

每个成功阶段只记录其声明的输出目录。最后成功阶段的输出目录是该模型版本默认的
“版本模型”，在没有评估依据前不称为“最优检查点”。checkpoint 扫描、最佳 checkpoint
判断、测试对比和部署选择属于后续里程碑。

绑定在线 Training Worker 节点的模型族可执行“验证配置”。中心把当前训练定义保存为
不可变验证请求，Worker 在下一次心跳中领取，并只检查工程目录是否可读、训练入口是否
存在、启动程序是否可找到、声明的运行环境是否可用、输出目录或最近父目录是否可写，
以及输出位置的剩余磁盘空间。验证不会运行 launcher、entrypoint 或固定 argv，不创建
目录和探测文件，也不会修改工程。普通只读用户只能看到验证状态和时间，不能看到包含
路径语义的详细检查结果。

## 真实训练边界

本里程碑已经建立训练节点、只读 Worker 和 SSH preflight，并把真实节点资源接入模型
注册与新建训练。服务器目录与“服务器资源”页面只展示 Worker 上报的真实节点；没有
登记节点时显示空状态，不混入 Fake GPU。资源详情集中在“服务器资源”，训练节点页
只负责登记、部署、修复、更换运行账号、卸载 Worker、删除节点记录和查看节点状态。
绑定真实节点的模型族可以选择 Worker 上报的真实 GPU、填写各阶段参数并生成
`execution_mode=real` 的 RunSpec 预览。该预览只做读取和结构化校验，不申请租约、
不创建任务或模型版本，也不向 Worker 发送命令。持久化任务接口仍只接受 simulation，
因此真实 Runner 仍保持关闭。

“可预览但不可启动”的页面提示和请求限制是开发期过渡逻辑；真实 Runner 完成后必须
删除这层临时限制，使同一份已确认 RunSpec 可以正常提交。预览步骤本身仍作为正式训练
流程的一部分保留。在训练服务器目录、输入权重、数据路径、输出根目录、账号权限和专用
连接凭据确认前，不允许 Worker 接收任务或启动训练。路径 allowlist、训练进程创建与
停止、artifact 校验以及 NaVILA metrics callback 均属于后续里程碑。

## 训练节点与 Worker v1

管理员可在“训练节点”页登记名称、主机地址和 SSH 端口，登记本身不绑定 Linux
账号。登记成功后页面直接进入 Worker 部署，用户此时才填写 SSH 登录账号及一次性
凭据。只有部署成功后，平台才记录该账号为 Worker 和训练所属账号；SSH 与 sudo 密码
仅存在于一次部署请求的内存中，不写入数据库、日志、argv、
环境变量或远端文件。节点状态为 `pending_enrollment / online / degraded / offline /
repair_required / disabled`，在线状态由中心根据最近心跳计算，不能由 Worker 自报。
默认只读身份只能查看安全投影，地址和 SSH 信息仅 `training:manage_nodes` 可见。
节点的 `state_revision` 只用于名称、地址、部署、停用等管理操作的乐观并发；Worker
注册和每次心跳使用独立的 `heartbeat_revision`。心跳只更新运行状态、资源快照和
`last_heartbeat_at`，不会让已经打开的管理表单失效。

部署时中心内部签发 600 秒有效的一次性 enrollment token；新 token 会使此前未使用的
token 失效，中心数据库只保存 SHA-256 摘要。Worker 首次注册换取的 bearer token
同样只在中心保存摘要；节点停用时立即吊销。

`datapilot-training-worker` 是独立进程。v1 采集 CPU、内存、磁盘和 GPU 状态并上报
心跳，并可领取模型配置只读验证、目录浏览、托管数据传输、暂停或取消传输和移除托管副本
这些固定类型的命令；它不能领取任意 Shell 或命令文本，也不能启动、停止、占用或检查
同事的训练进程。GPU fallback
只使用固定 `nvidia-smi` argv、五秒超时和 `shell=False`。节点本地保存私有 identity、
权限为 `0600` 的 Worker token，以及用于重启后保守对账的 SQLite ledger；PID 必须与
进程启动标记和 argv digest 同时匹配，无法确认时标为 unknown，绝不发送信号。

Worker HTTP 客户端只允许固定中心 origin 上的 enroll、heartbeat、命令长轮询、命令
结果回传和托管数据分块下载端点，拒绝重定向并限制超时和响应大小。部署使用系统级
systemd 自启动服务，但 Worker 与
未来训练都沿用 SSH 实际登录身份；平台不创建另一套 Linux 账号。systemd 仍启用
`NoNewPrivileges` 等不妨碍工程、Home、Conda 和输出目录访问的基础保护。
中心公网 Nginx 的最小端点白名单模板位于
`deployment/nginx/datapilot-training-center.conf`；新增 Worker 命令时必须同时更新该
白名单，否则请求会在到达中心应用前被拒绝。

## Training Worker 一次性 SSH 部署

中心服务必须配置一个训练节点可访问的 HTTPS origin：

```bash
export VLA_TRAINING_CENTER_BASE_URL=https://datapilot.example.internal
```

只有公网 IP、没有公共 CA 证书时，可以为中心入口签发带 IP SAN 的内部 TLS 证书，并配置
其 CA 公钥证书路径：

```bash
export VLA_TRAINING_CENTER_BASE_URL=https://120.202.207.116:8777
export VLA_TRAINING_CENTER_CA_CERT_PATH=/path/to/training-center-ca.pem
```

中心只读取 CA 公钥证书；CA 私钥和中心服务私钥不进入应用配置。一次性 SSH 部署会把
CA 公钥证书安装为 `/etc/datapilot-training-worker/center-ca.pem`，首次注册和长期
systemd 心跳都使用该证书验证中心身份。未配置自定义 CA 时，Worker 使用操作系统默认
的公共 CA 信任库。无论哪种模式，都不会关闭 TLS 证书或主机名验证。

Worker 默认读取 Linux 当前挂载表，自动上报所有持久存储挂载点，并过滤 `/proc`、
`tmpfs`、cgroup 等非磁盘文件系统；同一设备的 bind mount 只显示一次。这样 `/data`
等独立数据盘无需用户额外配置，新增挂载也会在后续心跳中自动出现。未挂载的裸设备不
属于可用文件系统容量，不在资源页面展示。

用户点击“登记并部署 Worker”后，平台先保存节点信息，再读取未受信任的 SSH host key 并显示
`SHA256:...` 指纹。用户必须通过可信渠道核对并明确确认；实际连接随后固定该 public
key，强制 `StrictHostKeyChecking=yes`，不会把首次扫描结果直接当作可信身份。

用户只需提供本次 SSH 登录密码。页面默认由系统自动检查 root、免密 sudo，以及登录
密码能否用于 sudo；只有 SSH 与 sudo 使用不同密码时，用户才需要展开并填写独立
sudo 密码，不要求算法用户预先判断提权方式。
密码通过本机短生命周期的受控 askpass 通道交给 OpenSSH，不使用 `sshpass`；部署结束
即销毁。安装按钮前会显示一次只读部署条件检查，固定检查 Linux、系统级 systemd、
Python 3、安装磁盘、NVIDIA 工具及 root/sudo 能力；未安装 NVIDIA 工具只会提示 GPU
资源无法上报，安装目录尚不存在会提示由首次安装创建。修改密码、指纹确认或 sudo 密码
后必须重新检查，实际部署时服务端还会强制复查。系统只执行内部固定的幂等安装操作，
不接收 Shell 文本或自定义命令：

1. 检查部署账号是 root 或可用 sudo；
2. 识别 SSH 实际登录账号及其主组；该身份将用于 Worker 和未来训练，root 也允许，
   但部署检查会明确提示其拥有节点完整权限；
3. 创建 `/opt/datapilot-training-worker`、`/var/lib/datapilot-training-worker` 和
   `/etc/datapilot-training-worker`；
4. 校验并安装版本化 Worker 制品，写入不含秘密的固定配置和 systemd unit；
5. 用短时 enrollment token 完成注册，再启用并启动系统服务。

如果部署账号不是 root 且不能使用 sudo，系统在任何特权写操作前终止，接口返回稳定
错误码 `training_node_deployment_account_insufficient`，页面明确显示“部署账号权限不足”。
系统不会让算法用户手工创建额外账号或执行补救命令。模型配置验证会以同一 SSH 身份
检查工程读取、所选 Conda 环境和输出目录写入权限；不满足时由用户更换 SSH 账号或路径。

系统级 service 不依赖登录会话或 systemd linger。正常升级可由仍在线的 Worker 后续
接管；Worker 完全损坏时，用户可在页面再次提供一次 SSH 授权执行同一套幂等修复。
修复只管理 Worker 自身，不扫描、重启、停止或修改已有训练进程。

“修复 Worker”沿用最近一次成功部署的账号；“更换Worker和训练所属账号”允许用户
输入另一个 SSH 账号并重新执行相同的只读检查和幂等部署。新账号只有在部署成功后才
成为记录中的运行账号，失败的尝试不会覆盖原账号。

已安装的节点可在“危险操作”中执行“卸载 Worker（保留节点）”。操作要求再次提供一次临时 SSH/sudo 凭据，
并经过影响说明和二次确认。中心会先撤销 Worker token、使节点退出可训练状态，再通过
固定卸载操作停止并删除 systemd 服务和 Worker 专用目录，但绝不删除 SSH 登录账号；
不会删除模型工程、数据集、权重、checkpoint 或训练输出。成功后保留节点登记记录并
恢复为“待部署 Worker”，以后可以重新一键部署；卸载失败时节点保持“需要修复”，不会
继续被当作可用训练节点。

页面还提供“删除训练节点”。从未部署过 Worker 的节点在确认后直接删除中心记录；已
部署 Worker 的节点必须先提供临时 SSH/sudo 凭据，系统成功卸载 Worker 后才删除中心
记录。该操作不删除 Linux 账号、模型工程、数据集、权重、checkpoint 或训练输出。

即使模型工程允许测试修改，Worker 也只安装到上述独立系统目录。除管理员主动发起的
模型配置只读验证外，当前部署和 Worker 不读取模型工程；任何情况下都不会修改模型工程、
权重、checkpoint 或训练输出。真实 Runner 仍保持禁用。

## 托管训练数据与 manifest 契约

模型训练模块的“训练数据”页面复用 Annotation 的已验证轨迹投影能力，同时列出整个日期已经
满足训练发布条件的待发布和已发布数据。用户先按日期进入只读检查，再按 Clip/Segment 浏览
相机投影和 Gridmap。页面只接受 `approved + published` 的 Fix revision，并校验展示证据与
兼容发布记录绑定的 revision 一致；原始 `*_trajectory.json`、仍在修正中的草稿以及发布失败
的 Fix 不会进入该页面。页面标识的权威业务产物为 `*_trajectory_fix_five.json`。

平台上线前由旧流程完成的修正结果通过同一个 Review 读接口展示，内部来源标记为
`historical_import`，但不会伪造曾在平台执行过的 Annotation Job 或人工操作记录。历史结果
只读解析已有 `*_trajectory_fix_five.json`、`fisheye_front`、`rout_plot_v2` 和 `grid_map`；
缺失的单帧证据仅在对应画面提示不可用，不会把历史记录变成可编辑任务。

日期发布按钮仅出现在检查详情中。发布继续使用 Annotation Store 的日期级原子发布契约，
不会复制文件或启动训练；发布成功后，该日期才会进入下述训练数据传输流程。

Training 将中心已发布的 `finish_data/<日期>` 视为不透明文件。平台只负责把完整发布日期
复制到模型绑定的训练节点、记录节点副本、按完整日期划分训练集和可选测试集，并向模型
工程提供一个统一 manifest；它不解释或生成模型专用的 instruction、answer、trajectory、
mask 或样本索引。

模型族的数据接入方式分为：

- `self_managed`：模型工程和普通训练参数自行管理所有数据路径；
- `datapilot_managed`：平台负责日期传输、划分，并保留 `--dataset_manifest` 参数。

`data_mixture` 等模型参数仍是普通超参数，不承担节点路径职责。每个节点对同一发布日最多
保留一份有效托管副本；换盘时应先移除原副本再重新传输。移除 Worker 或删除节点记录不会
自动删除已传输数据。

Worker 通过经过认证的出站 HTTPS 连接主动拉取数据，日常传输不使用 SSH。用户通过远程
目录浏览器选择一个 Worker 账号可进入且可写的父目录，Worker 最终创建：

```text
<选择目录>/datapilot-managed/<日期>-<release_ref短标识>/
```

中心异步为首次传输建立逐文件大小和 SHA-256 清单。Worker 分块下载到隐藏临时目录，支持
从已验证的部分文件继续，全部文件校验通过、写入平台 marker 后才原子发布为可选副本。
移除操作只能删除数据库登记路径且 marker 与 release 和清单摘要完全匹配的目录。

“暂停传输”会停止下载并保留隐藏的 `.part` 临时目录，用户之后点击“继续传输”时从已校验
内容断点续传；“取消本次传输”会要求 Worker 删除该临时目录，只有清理成功后任务才进入
`cancelled`。清理失败会保留失败状态供用户处理，不能伪装为已取消。传输浮窗在进行中、
暂停、失败或取消清理中只能收起为常驻胶囊，刷新页面后仍可恢复；只有传输完成或已彻底取消
时才允许关闭。

DataPilot 托管数据的一次训练使用一份不可变 DatasetSnapshot，多阶段共享同一划分。训练集
至少包含一个完整日期，测试集可以为空，同一日期不能同时进入两个集合。平台为预览生成
未来内容，为真实任务预留以下路径；预览不会写节点文件、创建版本、申请 GPU 或启动进程：

```text
<output_root>/<family_ref>/<version_label>/dataset-manifest.json
```

manifest 使用 `datapilot_dataset_manifest_v1`，每个 split 项只记录发布日期、release、
replica、节点本地根目录和清单摘要。所有训练阶段收到相同的 `--dataset_manifest` 值。模型
项目在启动时读取 manifest，并自行完成索引、转换、归一化和缓存。当前真实 Runner 仍禁用；
允许适配和验收的模型工程仅为 `/data/caiji_test/NaVILA`，不得读取、修改或运行
`/data/cui/NaVILA`。
