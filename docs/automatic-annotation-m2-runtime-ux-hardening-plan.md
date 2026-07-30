# 自动标注 M2：Runtime 与交互收口计划

> 日期：2026-07-30  
> 分支：`codex/automatic-annotation-m2`  
> 状态：本地实施与回归通过，服务器 writer 尚未重跑

## 1. 目标与边界

本轮只修复 M2 真实验收暴露的部署、契约和状态投影问题，不改变冻结业务脚本的
算法、步骤、参数语义或数值方法：

1. 恢复服务器上 `./scripts/run_web.sh start` 的单命令启动体验；
2. processing 与 Fix 标定均来自冻结 Runtime 的受审计目录，不维护名称枚举；
3. NavigationDataAgent 在 Plan 尚未满足提交条件时仍可读取任务上下文；
4. 区分“等待人工输入”和“系统 Runtime 正在执行”，修复 TaskStrip 停在
   “等待确认”；
5. Annotation 页面使用一条常驻领域事件连接和进程内投影缓存，SPA 路由往返不
   重读完整列表；
6. 修复后处理私有 `/tmp` 隐藏 Xvfb socket 导致的 Tk/Matplotlib 投影失败。

以下内容不进入本轮：

- 不修改冻结 Python、Tracking 二进制或模型；
- 不放宽 Plan validator、任务/会话所有权或公开脱敏边界；
- 不把业务决策移到 Web；
- 不调整工具名泄漏 Prompt；
- 不建设 M3 数据管理或仪表盘 JOIN。

## 2. 根因与锁定方案

### 2.1 启动配置

服务器所需 Node 目录、Runtime、数据根、writer lock 和 work root 过去依赖终端
临时导出，导致不同 shell 的启动行为不一致。

锁定方案：

- 固定读取 `~/.config/vla-data-juicer-agents/run-web.json`；
- JSON 只接受非敏感 allowlist 字段，不执行 shell；
- 配置目录必须由当前用户拥有且为 `0700`，文件必须为 `0600`；
- 拒绝 symlink、hardlink、特殊文件、重复 key、超限文件和未知字段；
- 显式 shell 环境优先；
- API Key 只能从进程环境继承，禁止写入该文件。

### 2.2 动态标定

`20260409_U` 不是缺失文件，而是旧 processing 白名单主动隐藏。名称白名单会在
设备切换后再次遗漏。

锁定方案：

- 扫描冻结 Runtime 的 `NoobScenes/params/<profile>/sensors` 直接子目录；
- 只有 manifest 完整证明过的 regular files 才可公开选择；
- 目录清单、文件大小和 SHA-256 必须与 manifest 完全一致；
- 任何未证明目录、内容漂移、symlink 或特殊文件均使能力预检失败；
- manifest 的 `calibration` / `fix_calibration` stage 仅记录历史来源，不再作为
  processing / Fix 的用途白名单；所有通过上述扫描和审计的 profile 均可用于
  两种目的；
- processing 与 Fix 各自生成独立的不可变快照。

### 2.3 Navigation 上下文

旧工具面在 `scene_mode` 或调查事实不完整时同时隐藏 Plan 提交工具和只读上下文
工具，模型因无法诊断缺项而猜测不存在的工具。

锁定方案：

- `get_navigation_task_context_tool` 始终可读；
- `submit_finish_processing_plan_tool` 仍在事实和指导不完整时隐藏；
- guidance/observation 更新后 context token 必须变化，旧 token 继续拒绝；
- 会话不匹配继续 fail closed。

### 2.4 等待态与执行态

首帧工作台 handoff 将任务置为 `waiting_user`，但全部提交并启动 Tracking 后，
Navigation step 和 task 没有原子转回 `running/active`。后处理启动也被错误投影
为等待人工。

锁定方案：

```text
等待首帧标注：
step=waiting_user, task=waiting_user

全部提交并开始 Tracking：
step=running, task=active

后处理 Runtime：
step=running, task=active
```

状态转换由系统事务完成；TaskStrip 从当前 accepted Plan step 投影阶段和等待
原因，不从旧消息或前端本地状态猜测。

### 2.5 Annotation 领域事件

旧页面组件各自创建 SSE 并在挂载时读取列表，路由返回会重复加载；事件 debounce
只保留最后一个事件，还可能漏掉同批不同 Job/Segment。

锁定方案：

- AppShell 生命周期内保留唯一 EventSource；
- 事件按 aggregate identity 批处理并保留每个对象的最新 seq；
- Zustand 只缓存 HTTP 权威投影，不作为第二事实源；
- 事件只刷新受影响 Job/Segment/Review；
- 显式刷新、60 秒对账、focus/online/连接恢复重读已加载投影；
- 浏览器整页刷新会自然重建缓存，SPA 页面往返复用缓存。

### 2.6 Xvfb 与 bubblewrap

失败的 `2_othermethod_cjl_0525.py` 在创建 Matplotlib figure 时无法连接
`DISPLAY=:99`。原因是 `xvfb-run` 在宿主 `/tmp` 创建 socket 后才进入
bubblewrap，而后处理 attempt 又把私有目录 bind 到 `/tmp`，因此 sandbox 内看
不到宿主 X11 socket。

锁定方案：

```text
bubblewrap
→ bind attempt-private /tmp
→ sandbox 内启动 xvfb-run
→ 执行冻结脚本
```

这只修正进程隔离顺序，不修改冻结业务代码。

当前 sandbox 的安全边界是“宿主根只读、网络隔离、任务写目录隔离”，不是最小
读取权限沙盒：冻结 Runtime 仍通过 `--ro-bind / /` 读取宿主文件，设备也通过
`--dev-bind /dev /dev` 可见。后续若收缩读取和设备暴露范围，必须作为独立
Runtime 兼容性项目验收，不能在本轮暗改业务运行环境。

## 3. 实施与验证顺序

1. 启动配置 loader、动态标定 catalog 与安全测试；
2. Navigation context 工具面与评测 Host 同步；
3. waiting→running 状态迁移、TaskStrip 投影和 handoff 事件；
4. AppShell 领域事件桥、投影缓存和页面接入；
5. Xvfb/bubblewrap 命令顺序修复；
6. Python 组合测试、前端全量测试、生产构建和 Python 全量回归；
7. 提交后另行部署服务器固定 JSON，重启并做能力/标定轻量验收；
8. 放弃或收口旧失败 Job 后，用新的 `20270623` 任务重跑后处理。

服务器真实重跑仍遵循：发现新旧产物差异立即停止，报告文件、Schema、关键数值、
命令顺序和可疑来源，不自行放宽 Golden。

## 4. 退出条件

- 普通项目虚拟环境中可直接执行 `./scripts/run_web.sh start`；
- processing 页面显示冻结 Runtime 中全部受审计标定，包括 `20260409_U`；
- 缺 scene/guidance 时模型能读取上下文，不再因工具面隐藏而猜工具；
- 首帧提交后 TaskStrip 显示 Tracking，后处理阶段显示后处理；
- Annotation 页面状态由事件及时更新，SPA 路由往返不重新加载全列表；
- 后处理脚本在私有 `/tmp` 中可访问同 sandbox 内的 Xvfb；
- 全量回归通过，服务器 writer 重跑前不修改历史 oracle 或同事业务目录。

## 5. 本地验证结果

- Python 定向组合：`658 passed`
- Python 全量：`1749 passed`
- 前端全量：`264 passed, 8 skipped`
- 前端生产构建及 bundle size gate：通过
- Playwright：`10 passed`
- `git diff --check`：通过

这些结果只证明代码与 fake/local Runtime 契约通过；真实
`bwrap + private /tmp + xvfb-run`、动态标定目录和 20270623 后处理仍需在服务器
按第 3 节单独验收。
