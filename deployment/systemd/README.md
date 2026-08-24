# Training Worker 部署说明

Training Worker 是 DataPilot 在训练节点上的执行组件。它通过出站 HTTP/HTTPS 连接中心服务，不要求训练节点为 DataPilot 开放额外入站端口。

普通用户不需要手工复制本目录中的 systemd 文件。Web 页面中的“部署 Worker”“更新 Worker”“修复 Worker”和“卸载 Worker”会生成并管理当前版本所需的服务配置。

## 账号与权限

用户登记训练节点时提供一次 SSH 登录账号和密码：

- 系统先检查主机指纹、Python、systemd、目录和安装权限。
- 安装 `/opt`、`/var/lib`、`/etc` 下的系统文件需要 `root`、免密 sudo 或可用的 sudo 密码。
- Worker 服务使用本次 SSH 登录账号及其主组长期运行，真实训练进程也属于这个账号。
- 如果登录账号本身是 `root`，Worker 和训练进程会以 `root` 运行。
- SSH 密码和 sudo 密码只用于本次操作，不写入节点记录和 Worker 配置。

旧版本曾创建 `datapilot-worker` 专用账号。更新到当前版本时，安装器会在确认安全后清理不再使用的旧账号；不会删除模型工程、训练数据或训练产物。

## 安装布局

当前安装器使用以下固定路径：

```text
/opt/datapilot-training-worker/       版本化 Worker 程序
/opt/datapilot-training-worker/current
/var/lib/datapilot-training-worker/   Worker 身份、令牌、SQLite ledger
/etc/datapilot-training-worker/       非敏感连接配置
/etc/systemd/system/datapilot-training-worker.service
```

`worker.env` 只保存中心服务地址和节点编号：

```dotenv
DATAPILOT_CENTER_BASE_URL=https://datapilot.example.internal
DATAPILOT_NODE_REF=node_...
```

首次接入使用中心服务签发的一次性 enrollment token。token 通过标准输入交给 Worker，不出现在命令参数、环境变量或配置文件中。接入成功后，一次性 token 不再保存；Worker bearer token 以 `0600` 权限写入私有状态目录。

## Worker 能做什么

Worker 只处理平台定义的固定操作：

- 上报 CPU、内存、磁盘和 GPU 资源；
- 验证模型工作目录、训练入口、Conda 环境和输出权限；
- 浏览可进入的目录；
- 拉取、校验、暂停、继续或删除 DataPilot 托管数据副本；
- 使用结构化 argv 启动和停止真实训练；
- 上报日志、指标、GPU 状态和 checkpoint 事件；
- 在重启后核对仍在运行的训练进程；
- 检查成功模型版本的产物目录、marker、文件数和目录大小。

Worker 不接受任意 Shell 文本，不使用 `shell=True`，也不会让网页直接执行任意节点命令。

## 训练进程与 Worker 更新

动态生成的 systemd 单元使用 `KillMode=process`。因此更新或重启 Worker 不会因为 systemd 的默认行为顺带终止已经启动的训练进程。

Worker 会把训练 supervisor、进程身份、日志游标和上报序号写入本地 ledger。新 Worker 启动后会重新核对：

- 身份一致且进程仍在运行：继续监控和上传日志；
- supervisor 已记录退出：恢复成功、失败或取消状态；
- 能确认进程已经不存在：标记任务丢失；
- 无法安全确认进程身份：标记状态待确认，保留 GPU 和端口租约，不重复启动任务。

存在未终止真实任务时，平台会拒绝卸载 Worker、删除节点或更换运行账号。

## 网络要求

- `DATAPILOT_CENTER_BASE_URL` 必须是训练节点能够访问的固定中心服务地址。
- 正式环境应使用 HTTPS；如使用内部 CA，通过平台部署配置提供 CA 证书。
- Worker 只连接登记的固定 origin，拒绝 HTTP 重定向，并限制请求超时和响应大小。
- SSH 仍只用于 Worker 生命周期操作，不用于每次训练。

## 页面操作

- **部署 Worker**：首次检查并安装。
- **更新 Worker**：部署中心服务当前提供的 Worker 版本，保留节点和训练记录。
- **修复 Worker**：重新核对并覆盖 Worker 程序、配置和 systemd 服务，不会先删除模型工程或训练数据。
- **更换 Worker 和训练所属账号**：使用新的 SSH 账号重新部署，之后 Worker 和训练属于新账号。
- **卸载 Worker（保留节点）**：停止并删除 Worker 服务，节点记录和托管训练数据保留。
- **删除训练节点**：如已安装 Worker，先通过临时 SSH 卸载，再删除中心节点记录；模型工程、数据集和训练产物不会自动删除。

这些操作都应先在页面核对训练节点主机指纹。密码只对当前弹窗操作有效。

## 离线检查

开发或诊断时，可以不安装 systemd 服务，只采集一次本机状态：

```bash
datapilot-training-worker \
  --state-dir /tmp/datapilot-worker-state \
  --once
```

仓库中的 `datapilot-training-worker.service` 是旧的静态参考文件，不能表达部署时动态选择的 SSH 运行账号。正式安装以 Web 部署器生成的 systemd 单元为准，不要直接复制该文件覆盖节点上的服务。
