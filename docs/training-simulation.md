# 模型训练模拟链路

模型训练任务的首个里程碑只运行模拟链路，不读取 NaVILA 目录，也不创建真实训练
进程。训练节点页可以在显式配置中心 HTTPS 地址后，通过一次 SSH 授权自动部署只读
Worker；该 Worker 仍不能执行训练命令。

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
2. 登记一个草稿模型及参数定义；`num_video_frames` 的实际字段值决定帧数。
3. 选择 Fake A100 资源并生成 RunSpec 预览。
4. 点击“启动模拟训练”，观察日志、Loss、学习率和 GPU 指标更新。
5. 停止运行中的模拟任务，确认状态变为已取消且 GPU 可再次选择。

预览中的 argv 仅用于展示。Fake Runner 直接生成确定性的模拟事件，不执行预览
文本或登记的入口路径。

## 模型指令如何注册

“模型注册”不是粘贴并执行一段 Shell。管理员先从已登记训练节点或本地模拟服务器中
选择运行位置，再登记 launcher、工作目录、
训练入口、输出根目录，以及一组类型化的训练脚本参数。参数无需预先填写数量，可
逐项添加或删除，并为每项设置字段名、CLI flag、类型、默认值、范围、枚举选项、
是否敏感、argv 表达方式、参数用途和展示分组。字符串或枚举参数可标记为唯一的
“数据集输入”，新建训练会把它从普通超参数中单独展示；当前仍填写模型脚本接受的
数据集标识，后续再接入数据管理模块的已发布版本选择器。每个参数还可填写不超过 120 个字符的可选
解释，用于训练配置页的悬停帮助。

模型 revision 还会保存运行环境与指标日志契约。运行环境当前只允许声明 Worker
系统环境或一个受限格式的 Conda 环境名；这是结构化元数据，不会拼接或执行
`conda activate`。日志来源当前固定为 stdout，可声明普通文本、Transformers Trainer
日志或 JSON Lines 指标。真实 Worker Runner 后续只能按这些已审核字段选择固定实现，
不能把它们转换为任意 Shell。NaVILA 预置声明系统环境和 Transformers 日志格式，
管理员可根据实际部署创建新 revision 调整。

启动方式是显式契约，不再根据任意 executable 猜测。`torchrun` 会由平台加入单机
`nnodes=1`、按所选 GPU 数计算的 `nproc_per_node`、master 地址/端口和 node rank；
`direct` 只生成 `executable + entrypoint + argv`，不会加入任何 torchrun 参数，也不会
申请 master port。旧 revision 没有该字段时，仅为兼容已有数据，按 executable 的文件名
是否为 `torchrun` 推断一次，并在下一 revision 中保存明确类型。

枚举参数使用结构化选项表，而不是解析自由文本：每项只登记一个选项值，该值同时
写入 argv 并显示在新建训练页；选项可添加、排序、删除及指定默认项，且必须唯一。
删除默认项前必须先选择其他默认项。整数和浮点数在
输入阶段保留编辑草稿并严格校验，避免空输入被静默转换成 `0`；整数限制在 JavaScript
安全整数范围内。填写数值上下界时，默认值必须位于闭区间内且最小值不得大于最大值。
字符串参数可登记 0–512 范围内的整数最短、最长字符数，并禁止换行和
控制字符。上述约束在模型注册和运行预览两层都会验证。

展示分组随模型 revision 保存。新模型初始只有系统保留的“常用参数”和“其他参数”：
前者在新建训练页常驻并固定最前，后者默认折叠并固定最后，两者均不可重命名或删除。
参数可直接选择已有分组，也可选择“新建分组…”并填写 2–30 个字符的名称。NaVILA
预置带入的推荐分组和用户新建分组均为普通用户分组，可重命名、排序和删除；删除前
必须二次确认并提示组内参数数量，确认后参数迁移到“其他参数”，参数本身不会被删除。
旧 revision 未登记展示分组时继续按已知字段名自动归类。

“设计依赖关系”可为目标参数登记一条类型安全的可用条件，页面表述为“仅当
【条件参数】等于【指定值】时，【目标参数】才可设置”。条件参数不能与目标参数相同；
后端还会拒绝不存在的引用、类型不匹配、枚举越界和循环依赖。条件不满足时，目标
参数仍保留在配置页原位置，但会灰显、不可编辑，并在设置框上显示禁止光标；同时
不会进入参数快照、RunSpec、argv 或命令预览。旧 revision 没有可用条件的参数继续
保持可编辑，避免改变已有模型行为。

内置 NaVILA 预置已登记 `save_steps` 的可用条件：仅当 `save_strategy=steps` 时才可设置。
选择按 epoch 保存或不保存时，该参数会灰显并从提交配置中省略；`save_total_limit` 仍保持常驻，因为它
对按 step 和按 epoch 保存都有效。

布尔参数明确区分两种形式：

- `explicit_boolean` 生成 `--bf16 True` 或 `--do_eval False`；
- `flag_when_true` 只在值为 True 时生成 `--some_flag`。

页面提供“NaVILA 轨迹训练”预置，覆盖当前已知训练脚本参数，并登记推荐展示分组；
用户可以在注册时重新分组。预置中的路径都是 `/workspace/...` 占位值，不绑定或读取实际共享
目录。GPU 选择、`CUDA_VISIBLE_DEVICES` 和每次运行的输出目录始终由平台生成，不能
注册为普通参数；仅 torchrun 启动方式还由平台生成 `nnodes`、`nproc_per_node`、master
地址/端口和 node rank。

新建训练页按照模型 revision 保存的布局展示参数；常用参数常驻，优化器与正则、
性能与显存、模型与多模态、数据与验证、日志与产物及用户新建分组默认折叠。
“其他参数”同样不会被忽略或从 RunSpec 参数快照中移除。

模型 revision 中登记的所有训练脚本参数都可以在创建任务时修改，不区分固定参数和
可编辑参数。注册页和运行预览都会显示结构化 argv，但不会执行它。

## 真实训练边界

本里程碑已经建立训练节点、只读 Worker 和 SSH preflight，并把真实节点资源接入模型
注册与新建训练。Fake Server 只用于模拟模式；绑定真实节点的模型可以查看节点快照和
填写参数，但页面不会开放模拟预览、GPU 选择或启动按钮，后端同样拒绝在真实节点上
创建 Fake Run。真实 Runner
仍保持关闭。在训练服务器目录、输入权重、数据路径、输出根目录、账号权限和专用
连接凭据确认前，不允许 Worker 接收任务或启动训练。路径 allowlist、训练进程创建与
停止、artifact 校验以及 NaVILA metrics callback 均属于后续里程碑。

## 训练节点与 Worker v1

管理员可在“训练节点”页登记名称、主机地址、SSH 端口和用户名；这些连接元数据会
保存，但 SSH 与 sudo 密码仅存在于一次部署请求的内存中，不写入数据库、日志、argv、
环境变量或远端文件。节点状态为 `pending_enrollment / online / degraded / offline /
repair_required / disabled`，在线状态由中心根据最近心跳计算，不能由 Worker 自报。
默认只读身份只能查看安全投影，地址和 SSH 信息仅 `training:manage_nodes` 可见。

部署时中心内部签发 600 秒有效的一次性 enrollment token；新 token 会使此前未使用的
token 失效，中心数据库只保存 SHA-256 摘要。Worker 首次注册换取的 bearer token
同样只在中心保存摘要；节点停用时立即吊销。

`datapilot-training-worker` 是独立进程。v1 只采集 CPU、内存、磁盘和 GPU 状态并上报
心跳，不轮询可执行任务，也不能启动、停止、占用或检查同事的训练命令。GPU fallback
只使用固定 `nvidia-smi` argv、五秒超时和 `shell=False`。节点本地保存私有 identity、
权限为 `0600` 的 Worker token，以及用于重启后保守对账的 SQLite ledger；PID 必须与
进程启动标记和 argv digest 同时匹配，无法确认时标为 unknown，绝不发送信号。

Worker HTTP 客户端只允许固定中心 origin 上的 enroll 和 heartbeat 两类 POST，拒绝
重定向并限制超时和响应大小。部署模板位于 `deployment/systemd/`，使用独立的
`datapilot-worker` 系统账号、系统级 systemd、`NoNewPrivileges` 和只读文件系统保护。

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

用户登记节点后点击“部署 Worker”，页面先读取未受信任的 SSH host key 并显示
`SHA256:...` 指纹。用户必须通过可信渠道核对并明确确认；实际连接随后固定该 public
key，强制 `StrictHostKeyChecking=yes`，不会把首次扫描结果直接当作可信身份。

用户只需提供本次 SSH 登录密码，并选择 sudo 使用同一密码、独立密码或无需密码。
密码通过本机短生命周期的受控 askpass 通道交给 OpenSSH，不使用 `sshpass`；部署结束
即销毁。安装按钮前会显示一次只读部署条件检查，固定检查 Linux、系统级 systemd、
Python 3、安装磁盘、NVIDIA 工具及 root/sudo 能力；未安装 NVIDIA 工具只会提示 GPU
资源无法上报，安装目录尚不存在会提示由首次安装创建。修改密码、指纹确认或提权方式
后必须重新检查，实际部署时服务端还会强制复查。系统只执行内部固定的幂等安装操作，
不接收 Shell 文本或自定义命令：

1. 检查部署账号是 root 或可用 sudo；
2. 创建无登录权限的 `datapilot-worker` 系统账号；
3. 创建 `/opt/datapilot-training-worker`、`/var/lib/datapilot-training-worker` 和
   `/etc/datapilot-training-worker`；
4. 校验并安装版本化 Worker 制品，写入不含秘密的固定配置和 systemd unit；
5. 用短时 enrollment token 完成注册，再启用并启动系统服务。

如果部署账号不是 root 且不能使用 sudo，系统在任何特权写操作前终止，接口返回稳定
错误码 `training_node_deployment_account_insufficient`，页面明确显示“部署账号权限不足”。
系统不会退回到用 SSH 登录账号长期运行 Worker，也不会让算法用户手工执行补救命令。

系统级 service 不依赖登录会话或 systemd linger。正常升级可由仍在线的 Worker 后续
接管；Worker 完全损坏时，用户可在页面再次提供一次 SSH 授权执行同一套幂等修复。
修复只管理 Worker 自身，不扫描、重启、停止或修改已有训练进程。

已安装的节点可在页面执行“删除 Worker”。操作要求再次提供一次临时 SSH/sudo 凭据，
并经过影响说明和二次确认。中心会先撤销 Worker token、使节点退出可训练状态，再通过
固定卸载操作停止并删除 systemd 服务、Worker 专用目录和 `datapilot-worker` 账号；
不会删除模型工程、数据集、权重、checkpoint 或训练输出。成功后保留节点登记记录并
恢复为“待部署 Worker”，以后可以重新一键部署；卸载失败时节点保持“需要修复”，不会
继续被当作可用训练节点。

即使模型工程允许测试修改，Worker 也只安装到上述独立系统目录。当前部署和 Worker
都不会读取或修改模型工程、权重、checkpoint 或训练输出；真实 Runner 仍保持禁用。
