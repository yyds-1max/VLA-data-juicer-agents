# DataPilot Contract V1 部署、验证与回滚手册

本文档适用于当前的单进程 DataPilot Web/AgentScope 部署。服务只创建和运行 contract v1 会话；`sessions.contract_version=1` 是未来升级边界，不是运行时开关。旧测试会话不迁移，也不能由当前服务继续运行。

> 限制：当前实现不支持多进程或多实例协调。不要让多个 Web/Runtime 进程共享同一份 `sessions.sqlite`、`navigation-tasks.sqlite` 和 AgentScope 运行状态。资源租约和 outbox 的持久化不等于已具备分布式一致性。

当前全局重型 Navigation writer 上限由单进程内的容量信号量强制为 `1`，目标范围冲突另由 Navigation 的持久执行账本拦截。`runtime_resource_leases` 已提供存储原语，但尚未作为重型工具的生产调度器；因此它不能替代上述单实例部署约束，也不能用于跨进程抢占或唤醒。

## 1. 配置与数据位置

### 1.1 固定会话契约

`VLA_DATAPILOT_SINGLE_AGENT_MODE` 已删除。普通聊天和数据管理快捷入口都创建 contract v1 会话。快捷入口除可见模板消息外，必须在创建会话时提交 `navigation_dataset_selection_v1` 私有结构化上下文；后端把它绑定到首个 Turn，Router 不得覆盖或扩大该范围。

私有快捷上下文按 UTF-8 计最多 `3000` 字节，以保证完整 `RouterContextEnvelope` 不超过 `4000` 字符；前端会在提交前拦截过大的 clip 选择，API 也会以 `422` 拒绝绕过前端的超限请求，不会截断或默默扩大范围。

MainRouter 单次受监督 Run 默认硬截止为 `45` 秒，可通过 `VLA_AGENT_ROUTER_RUN_TIMEOUT_SECS` 设置正数秒值。截止或模型服务异常会进入 System Controller 的安全失败收口，关闭 Turn、reply lease 和 response authority；不能让前端无限停留在“正在理解你的请求”。Navigation 的长任务不使用这个 Router 截止时间。

部署前应重置开发阶段的旧 Web/Navigation/AgentScope 测试状态。不得删除或改写 VLADatasets 原始数据及已有业务产物。

### 1.2 SQLite 文件

通过当前 Web CLI 启动时，两份主数据库默认都在 `VLA_DATA_AGENT_WEB_WORKING_DIR`（默认 `./.djx`）下：

```text
<working-dir>/sessions.sqlite
<working-dir>/navigation-tasks.sqlite
```

`sessions.sqlite` 保存 Web 会话、Turn、公开时间线、task binding、response authority、interaction、outbox 和资源租约。`navigation-tasks.sqlite` 保存 Navigation Task、Plan、观测和执行账本。如果应用通过参数显式指定了 Web `db_path` 或 Runtime `workspace_root`，以进程的实际参数为准，不要仅根据默认路径备份。

AgentScope 会话状态另外依赖 Redis。SQLite 备份不是 Redis 备份；需要保留完整运行现场时，还应按公司 Redis 运维规范备份对应实例，并保留部署配置和密钥引用（不要把密钥明文写入备份说明）。

## 2. 升级前检查与备份

Schema migration 在创建 `WebSessionStore` 时自动执行，因此必须在新二进制第一次启动前完成备份。

### 2.1 停机窗口

1. 停止接收新请求。
2. 等待当前 Router/Navigation Run 结束，等待持久化工具完成安全收尾。
3. 停止唯一的 Web/Runtime 进程和其恢复循环。
4. 确认没有其他进程连接这两份 SQLite。

可在停止前使用以下只读查询辅助判断是否还有活动工作：

```sql
SELECT status, count(*) FROM web_turns
WHERE status IN ('running', 'waiting') GROUP BY status;

SELECT status, count(*) FROM turn_runs
WHERE status = 'running' GROUP BY status;

SELECT resource_key, owner_id, kind, expires_at
FROM runtime_resource_leases ORDER BY expires_at;
```

`runtime_outbox` 中有 `pending` 项不代表数据库不能备份，但它表示恢复后仍有工作需要重放，必须在发布记录中留档。

### 2.2 WAL checkpoint

先核对绝对路径，再做 checkpoint。下面的变量只是示例：

```bash
WORKING_DIR=/srv/vla-data-juicer/.djx
SESSIONS_DB="$WORKING_DIR/sessions.sqlite"
NAVIGATION_DB="$WORKING_DIR/navigation-tasks.sqlite"

sqlite3 "$SESSIONS_DB" 'PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(TRUNCATE);'
sqlite3 "$NAVIGATION_DB" 'PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(TRUNCATE);'
```

`wal_checkpoint` 输出为 `busy | log | checkpointed` 三列。第一列必须为 `0`；否则仍有连接阻止 checkpoint，不要继续备份或替换二进制。当数据库未使用 WAL journal 时，该命令仍可用；应额外用 `PRAGMA journal_mode;` 记录实际模式。

### 2.3 一致备份

同时保留 SQLite 逻辑备份和停机后的物理文件。下面的备份目录必须是新目录，且不能位于被备份数据库的目录中：

```bash
BACKUP_DIR=/srv/backups/datapilot/pre-contract-v1-YYYYMMDD-HHMMSS
mkdir -p "$BACKUP_DIR"

sqlite3 "$SESSIONS_DB" ".backup '$BACKUP_DIR/sessions.logical.sqlite'"
sqlite3 "$NAVIGATION_DB" ".backup '$BACKUP_DIR/navigation-tasks.logical.sqlite'"

for suffix in '' '-wal' '-shm'; do
  source_file="${SESSIONS_DB}${suffix}"
  if [ -e "$source_file" ]; then
    cp -p -- "$source_file" "$BACKUP_DIR/sessions.sqlite${suffix}"
  fi
done

for suffix in '' '-wal' '-shm'; do
  source_file="${NAVIGATION_DB}${suffix}"
  if [ -e "$source_file" ]; then
    cp -p -- "$source_file" "$BACKUP_DIR/navigation-tasks.sqlite${suffix}"
  fi
done

sqlite3 "$BACKUP_DIR/sessions.logical.sqlite" 'PRAGMA integrity_check;'
sqlite3 "$BACKUP_DIR/navigation-tasks.logical.sqlite" 'PRAGMA integrity_check;'
```

两次 `integrity_check` 都必须返回 `ok`。对备份目录做公司规范的权限控制、密文存储和校验和，并保留当次代码版本、环境变量名清单以及实际数据库路径。

## 3. 发布顺序

1. 完成第 2 节的停机备份，并确认服务和恢复循环已停止。
2. 隔离或重置旧的开发测试会话、Navigation 状态和对应 AgentScope/Redis 状态；保留备份用于诊断，不把旧会话改写为 v1。
3. 部署代码并启动唯一实例。启动会执行顺序 migration；未知或更高 schema version 必须使启动失败，不要手工删除 ledger 或覆盖结构。
4. 确认普通聊天和数据管理快捷入口创建的会话都为 `contract_version=1`。
5. 按顺序验证：快捷入口单 clip、普通文本单 clip（clip 前缀日期与 dataset date 不同）、日期-only 的 all-clips 语义、不提供 scene mode 的后台拆解同步与后续询问、Stop/恢复/Cancel。
6. 持续观察第 4 节指标。范围扩大、重复 final、任务状态分裂、公开泄露或运行中错误兜底均为停止验收的条件。

可用以下查询核对会话分布：

```sql
SELECT contract_version, count(*) AS session_count
FROM sessions GROUP BY contract_version ORDER BY contract_version;
```

## 4. 观测与告警

当前没有为下列 contract v1 状态提供独立的 Prometheus 端点。运维上应组合应用日志、Session API/WebSocket 抽样和 SQLite 只读查询；不要把“数据库有表”误当作“已有完整可观测性”。

### 4.1 Outbox

```sql
SELECT kind, status, count(*) AS item_count,
       max(attempts) AS max_attempts,
       min(available_at) AS oldest_available_at
FROM runtime_outbox
GROUP BY kind, status ORDER BY kind, status;

SELECT outbox_id, kind, status, attempts, available_at,
       lease_expires_at, last_error, updated_at
FROM runtime_outbox
WHERE status IN ('pending', 'claimed', 'failed')
ORDER BY available_at, created_at;
```

重点告警：长时间不减少的 `pending`、已过 `lease_expires_at` 仍为 `claimed`、`failed`、持续增长的 `attempts`，以及日志中的 Contract V1 outbox 恢复错误。恢复循环会重放 `navigation_start`、`navigation_continue`、结构化交互产生的 `navigation_resume`，以及等待当前 Router Turn 结束的 `system_turn`，不要直接把非 `completed` 行改为完成。

### 4.2 Response authority 与唯一 final

```sql
SELECT producer, lease_state, count(*) AS authority_count
FROM turn_response_authority
GROUP BY producer, lease_state ORDER BY producer, lease_state;

SELECT t.id, t.origin, t.status, a.producer, a.generation,
       a.lease_state, a.final_message_id, a.updated_at
FROM web_turns AS t
JOIN turn_response_authority AS a ON a.turn_id = t.id
WHERE (t.status IN ('completed', 'failed', 'interrupted') AND a.lease_state = 'open')
   OR (a.lease_state = 'closed' AND a.final_message_id IS NULL)
ORDER BY a.updated_at;

SELECT turn_id, count(*) AS final_count
FROM timeline_events WHERE type = 'final'
GROUP BY turn_id HAVING count(*) > 1;
```

最后一个查询应返回空集。存储层会基于 producer、generation 和开放租约拒绝迟到/重复 final；应关注 `response_authority_mismatch`、`system_turn_deferred` 以及 Event Bridge 反复重连日志。

### 4.3 Interaction

```sql
SELECT status, risk, kind, count(*) AS interaction_count
FROM interactions GROUP BY status, risk, kind
ORDER BY status, risk, kind;

SELECT interaction_id, task_ref, kind, risk, revision,
       expected_task_revision, expires_at, updated_at
FROM interactions
WHERE status = 'open'
ORDER BY created_at;
```

监控 HTTP 409 中的 `interaction_expired`、`interaction_revision_mismatch`、`interaction_idempotency_conflict` 和 task revision 冲突。少量冲突可能是双击、刷新或旧 snapshot；持续增长通常表明前端没有使用最新 revision。不要通过手工改 interaction revision 解决冲突。

### 4.4 公开投影与脱敏

Contract v1 的公开事件应只包含白名单事件和 `task_ref`，不应返回 Agent/工具名、`call_id`、`run_id`、`task_id`、绝对路径、用户名目录或凭据，也不应展示百分比进度。灰度期每次发布至少抽查：

- `GET /api/sessions/{session_id}` 返回的消息、timeline、task strip 和 interaction。
- WebSocket 从启动到 final 的完整事件流，包括断线重连后的 snapshot。
- 服务日志中 `AgentScope event bridge worker failed; reconnecting` 和公开投影校验异常。投影失败会表现为 Bridge 重连，当前没有独立的“脱敏告警计数器”。

一旦抽样发现凭据、绝对路径或内部 ID，立即停止服务或阻断新请求，并保留脱敏后的复现条件。不要把原始凭据复制到工单或发布群。

## 5. 回滚

### 5.1 使用当前版本暂停服务

1. 停止新请求，等待持久工具安全收尾。
2. 停止唯一实例和恢复循环，保留异常 v1 会话、数据库与脱敏日志作为诊断现场。
3. 修复并通过硬门禁后再启动；当前版本没有切回 v0 的运行开关。

### 5.2 回滚到旧二进制

旧二进制不理解 contract v1 会话和新 sidecar 状态。不支持在已升级的 SQLite 上做就地 schema 降级；如果必须回滚旧二进制，必须同时恢复升级前的 `sessions.sqlite` 和 `navigation-tasks.sqlite` 备份。

1. 停止服务并阻断写入。
2. 对当前升级后数据库再做一份隔离备份，便于事后分析。
3. 恢复升级前两份主数据库的同一时点备份。如果使用物理备份，一并恢复该备份集中的 `-wal`/`-shm`；如果使用已验证的 `.backup` 逻辑副本，不要混用新时点的 sidecar。
4. 按基础设施策略恢复与该备份时点匹配的 AgentScope/Redis 状态，或明确接受无法续接在途 AgentScope Run。
5. 使用旧二进制启动，执行只读数据库检查和最小 smoke。

恢复升级前备份会丢失备份时点之后的会话和 Navigation 状态，必须在回滚决策中明确接受该事实。不要只替换二进制却继续使用已升级数据库。

## 6. 故障排查

### 启动报 schema version 过高或 ledger 不连续

- 不要删表、修改 `schema_migrations` 或强制降版。
- 确认是否误用了旧二进制连接新数据库，或数据库来自更高版本环境。
- 回到匹配该 schema 的代码版本，或恢复升级前备份。

### 快捷入口创建失败或范围不一致

- 核对 `POST /api/sessions` 同时携带 `entrypoint: "data_management_shortcut"` 和私有 `request_context.kind: "navigation_dataset_selection_v1"`。
- 核对 `dataset_date` 与 `selection.kind/clips` 来自数据管理页真实选择；日期与 clip 前缀不要求相同。
- 后端不会根据模板文本或 `invocation_id` 猜测权威范围。不要修改已创建任务的范围；取消后重新创建。

### Navigation 已接受，但没有启动或恢复

- 查看 `runtime_outbox` 的 `navigation_start`、`navigation_continue`、`navigation_resume`、`system_turn` 项及 `attempts/last_error/lease_expires_at`。
- 查看日志中的 `Contract v1 outbox recovery failed`、Redis timeout 和 AgentScope wakeup/inbox 恢复诊断。
- 核对 `conversation_task_bindings`、`conversation_agent_sessions` 和 `turn_response_authority` 的 task/turn 关系，只读检查，不手工修改 ID、producer 或 generation。
- 重启单实例前先确认原进程已完全退出，避免两个恢复循环同时消费。

### 用户收到两条 final，或 Turn 一直不结束

- 先执行第 4.2 节的查询，再检查 Event Bridge 重连和 snapshot/WebSocket 重放。
- 区分“前端重复渲染同一事件”与“数据库存在两条 final”；`origin_key` 和 Turn final 唯一索引会拒绝后者。
- 如果是 authority 不一致，保留数据库和脱敏日志现场，不手工关闭租约或补写 final。

### Interaction 点击后返回 409

- 使用 409 返回的最新 Session snapshot 重画界面。
- 已过期、interaction revision 或 task revision 不一致时，不重放旧选项。
- 只对同一次用户点击复用原 `idempotency_key`；新操作必须使用新 key。

### 断线重连后任务条或 interaction 不一致

- 以 `GET /api/sessions/{session_id}` 的最新 snapshot 为基础恢复，WebSocket 事件只做幂等增量。
- 检查 Event Bridge cursor、`duplicate_events`、`subscription_reconnects` 和日志中的 Bridge 延迟/重连信息。这些是进程内指标与日志，当前没有独立运维 API。

## 7. 发布验收记录

每次部署或重新开放真实任务测试时，至少记录：

- 代码版本、启动时间和唯一进程证据。
- 两份 SQLite 的实际绝对路径（对外工单需脱敏）、checkpoint 结果、备份目录和校验和。
- `schema_migrations`、contract v1 数量、outbox 状态、authority 异常查询、interaction 409 数量和公开投影抽样结果。
- 自动化测试和前端构建结果，以及是否完成真实模型 smoke。

不应因为单元/集成测试通过就宣称“真实模型已验证”。真实模型 smoke 必须在目标服务器的实际模型、Redis、数据目录和权限配置下单独执行和记录；未执行时必须明确标注为未验证。
