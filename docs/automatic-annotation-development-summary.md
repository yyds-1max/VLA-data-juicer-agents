# 自动标注 M0–M3 开发总结

> 状态：M0、M1、M1.5、M2、M3 已完成并冻结
> 总结日期：2026-08-11
> 适用范围：导航数据的 Web 首帧标注、Tracking、后处理、三维人工复核/Fix、数据资产状态与训练发布登记
> 后续方向：M4 模型辅助复核延期，不是当前业务闭环的上线门禁

## 1. 建设结果

本阶段把原来依赖服务器 Bash、桌面 GUI、人工改配置和公共工作目录的导航后处理
链路，接入为由 DataPilot 编排、确定性系统执行、Web 工作台提供人工输入的完整
业务闭环：

```text
同步产物
→ DataPilot 调查、规划并取得处理标定
→ Web 首帧标注
→ Tracking
→ gridmap、投影、世界坐标、速度、方向和轨迹后处理
→ 已标注 / 待人工复核
→ Web 三维轨迹 Fix
→ 人工通过、退回或废弃
→ 训练兼容文件
→ 日期级训练发布登记
```

自动标注从同步产物开始，拆解和同步仍属于导航数据处理模块。系统没有新增
`AnnotationAgent`，也没有建立一条绕过 DataPilot 的人工处理流水线。

## 2. 核心职责边界

- DataPilot 是用户看到的唯一智能体身份和处理任务入口。
- MainRouter 负责意图识别、委派、状态查询和任务控制，不持有 Tracking、投影或
  Fix 等底层工具。
- NavigationDataAgent 调查数据事实、选择规范化领域决策并生成 Plan。
- Application Service 校验 Plan、任务范围和调查事实，再调用冻结 Runtime。
- LLM 负责意图理解、规划和推理；系统负责路径、参数、数据搬运、格式转换、状态、
  幂等、并发、恢复、产物哈希和精确发布。
- bbox、轨迹、内部 ID、绝对路径和脚本参数通过领域 API 与服务端绑定传递，不依赖
  聊天自然语言转述。
- Web 页面只提供快捷入口、首帧标注、人工 Fix、状态和结果，不让普通用户选择
  脚本、gridmap 生成方式或轨迹变体。

## 3. M0：Runtime 与业务基线

M0 完成了 `_01/run_odom.sh`、`run_fix.sh`、活动 Python 脚本、Tracking 二进制、
模型、配置和标定的只读盘点，并建立 `navigation_odom_v1`：

- 仓库保存 wrapper、兼容适配器、manifest、校验器和 Golden 比较器；
- 大型二进制与模型留在配置驱动的服务器 Runtime 目录；
- manifest 使用 SHA-256 固定依赖，哈希或环境不符时 fail closed；
- Agent Python 与 ROS/CUDA/旧业务 Python 隔离；
- writer 使用全局锁，重型任务保持 capacity=1；
- job 使用私有 staging、进程组、超时、取消、checkpoint 和恢复账本；
- 原始数据与 `clip_data` 不允许被 hardlink 或原地修改；
- Xvfb 与 bubblewrap 取代 XQuartz，并把旧代码的硬编码兼容路径限制在任务私有
  mount namespace 中。

业务算法、步骤、命令顺序和数值方法没有被重写、简化或优化。后处理长期运行约束
见 [导航后处理 Runtime 契约](automatic-annotation-postprocessing-runtime-contract.md)，
完整冻结依赖见 [navigation_odom_v1 README](../runtime/navigation_odom_v1/README.md)。

主要测试数据使用 `20270605`、`20270623` 的开发副本；`20260605`、`20260623`
历史产物作为只读 oracle。测试副本不得写入历史 oracle 或同事的业务源码目录。

## 4. M1：Web 首帧标注与 Tracking

M1 新建独立 `annotation.sqlite`、migration ledger 和 Annotation Application
Service，实现：

- 按日期和外层 clips 创建唯一 AnnotationJob；
- 对内部 Segment 使用匿名公开 ref，隐藏内部路径和数据库 ID；
- 首帧读取 NoobScenes resize 后的真实坐标图像；
- Web bbox、前景点、master/otherN、三种服饰颜色、草稿自动保存和不可变提交；
- revision CAS、幂等 mutation、并发冲突、skip/unskip、取消和恢复；
- Legacy YAML Adapter 精确生成旧 Tracking 输入；
- 原 Tracking 二进制、模型、命令和业务方法在私有沙盒中运行；
- 刷新页面、断线或进程重启后从数据库和 manifest 恢复任务事实。

服务器已通过单 Segment 与六 Segment 的 Web 标注和 Tracking 验收，用户无需
XQuartz。M1 不包含二维人工复核、二维 AI 或新增 Tracking 质量门。

## 5. M1.5：前端基础设施

M1.5 把前端固定到 Node.js `24.18.0`、npm `11.16.0` 和 Tailwind 4，建立
Radix/shadcn primitive 与路由级懒加载基线：

- 通用 Button、Badge、Alert、Progress、Dialog 等组件进入仓库接受审查；
- 领域状态、标注画布和轨迹交互仍由业务组件实现；
- 不引入 Base UI 或第二套 primitive；
- `run_web.sh` 在构建前校验 Node/npm，并允许服务器配置固定 Node bin；
- shadcn MCP/CLI 仅用于开发，不部署到生产服务器。

长期前端规则见 [前端设计系统](frontend-design-system.md)。

## 6. M2：后处理与三维人工复核/Fix

M2 将后处理纳入 DataPilot 的唯一处理链。NavigationDataAgent 根据 localization、
gridmap 和已有产物调查结果选择规范化决策；系统验证后映射到冻结脚本：

- gridmap：复制既有产物、从 PCD 生成，或在投影事实已满足时跳过；
- trajectory：根据定位事实选择受支持的 Ins/odom 变体；
- 后处理使用全新 attempt staging，完成投影、坐标、速度、方向、轨迹和 final
  candidate；
- publication journal 负责兼容 `finish_data` 的可恢复发布；
- 每个非 skip Segment 冻结 TrajectoryRevision 并创建 ReviewTask。

后处理完成后，原 Navigation 任务结束并释放活动槽位。用户选择继续 Fix 时，系统
创建关联的新 Fix Task；pending ReviewTask 不占用普通会话任务槽。

人工复核工作台支持：

- 相机投影与 gridmap/轨迹同时展示；
- 拖动人物位置和方向；
- 调整位置、方向、速度和 pass；
- 删除目标、补回缺失目标、恢复当前帧；
- 独立选择 Fix 标定并记录与处理标定的差异原因；
- 生成冻结 Runtime 的 Fix 预览；
- 提交不可变 FixRevision；
- 人工通过、退回或废弃；
- 通过后发布兼容的 `*_trajectory_fix_five.json`。

`pass` 只表示该帧不进入训练，不等于废弃 Segment。Fix 标定独立于处理标定，
Revision 和历史产物不能原地覆盖。

真实服务器验收使用 `20270623 / 20260623_145550` 的六个复核单元，最终形成
5 个已验证、1 个已废弃，关联任务与兼容发布完成收口。

## 7. 实时状态、会话与工作台恢复

Annotation 生命周期通过持久领域事件和安全 DTO 推送到前端。前端收到事件后只
局部重读受影响任务；断线重连时用事件游标和服务端快照恢复，不把浏览器内存当作
事实源。

会话正文展示由 DataPilot 生成的用户可理解里程碑；确定性状态卡展示已确认的系统
事实。同一标签页刷新恢复刚才查看的会话，其他情况下仍默认进入新会话。确认卡、
NavigationTask、AnnotationJob、handoff outbox 和 RuntimeRun 分别持久化，不能从
自然语言消息猜测任务是否仍在运行。

## 8. M3：数据资产与训练发布

M3 将目录扫描事实和 AnnotationStore 联合为数据资产读模型，并正式导入历史
`*_trajectory_fix_five.json`。导入必须在停机窗口使用显式清单、路径约束和
SHA-256 校验，在线请求不能扫描目录后自动晋升历史数据。

数据资产采用四条独立状态轴：

```text
数据处理：待处理 / 已拆解 / 已同步 / 异常
自动标注：尚未标注 / 待首帧标注 / 处理中 / 已标注 / 异常
人工复核：待复核 / 修正中 / 已退回 / 已验证 / 已废弃 / 聚合状态
训练发布：— / 待发布 / 已发布（仅日期级）
```

详细投影、聚合和发布条件以
[数据资产生命周期契约](data-asset-lifecycle-contract.md) 为准。

产品结果包括：

- `/data` 展示目录、标注和复核三条资产状态；
- `/data/releases` 分开展示待发布和已发布日期；
- 仪表盘展示真实的已标注总时长、clips、覆盖率和 Segment 数；
- 历史和新任务使用同一 AnnotationStore 事实源；
- DataPilot 管理的拆解、同步、标注和复核事件触发局部更新；
- 显式“刷新”执行完整事实重扫，但不启动处理任务；
- 所有普通快捷入口文案统一为“交给 DataPilot”。

日期训练发布只登记该日期允许被模型训练模块选择：不默认选择、不搬运数据、不
重新生成轨迹，也不自动启动训练。

## 9. 生产约束与维护要求

- 继续保留 Runtime manifest、私有 work root、sandbox、writer lock、数据库迁移、
  revision、幂等、publication journal 和公开脱敏。
- `./scripts/run_web.sh start|stop|restart|status` 是当前统一启动入口；服务器固定配置
  位于 `~/.config/vla-data-juicer-agents/run-web.json`。
- 数据盘、Runtime payload、Node/npm、Xvfb、bubblewrap、GPU 和磁盘空间未通过
  preflight 时，不允许创建 writer Job。
- 业务脚本发生变化时，应建立新 Runtime 版本、重新生成 manifest 并执行 Golden，
  不能静默替换冻结依赖。
- 数据库升级必须停机、备份 DB/WAL/SHM、执行 migration、integrity check 和
  foreign-key check，再恢复服务。
- 公开 API、事件和 DataPilot 时间线不得泄漏绝对路径、内部 ID、脚本名、命令或
  凭据。

## 10. 当前边界与后续工作

- M4 的模型辅助复核、置信度和 AI 候选 Fix 已延期；人工仍是唯一最终审核者。
- 二维人工复核和二维 AI 不在当前路线内。
- 机械臂数据尚未实现，但 Annotation Service 保留领域扩展边界。
- 当前训练发布不处理发布后新增 clip、重开 revision 或多版本重新发布。
- 业务脚本版本发现与升级工作流尚未产品化，后续应建设 Runtime Catalog、差异报告、
  Golden 门禁和受控晋升流程。

## 11. 保留的权威文档

- [系统架构](architecture.md)
- [DataPilot V1 运维契约](datapilot-contract-v1-operations.md)
- [前端设计系统](frontend-design-system.md)
- [数据资产生命周期契约](data-asset-lifecycle-contract.md)
- [导航后处理 Runtime 契约](automatic-annotation-postprocessing-runtime-contract.md)
- [navigation_odom_v1 Runtime README](../runtime/navigation_odom_v1/README.md)
- [Navigation Plan 智能体指导](navigation-plan-agent-guidance.md)
- [Navigation Plan 服务器验收](navigation-plan-server-acceptance.md)
