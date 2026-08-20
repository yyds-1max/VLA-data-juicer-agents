# 模型项目接入 DataPilot 训练平台

本文约定训练项目接入 DataPilot 托管数据和真实训练执行的最小接口。平台负责节点、GPU、进程、日志、任务状态与模型版本记录；模型项目负责读取数据、构造样本并完成训练。

## 1. 数据清单（必需）

当模型注册时选择“DataPilot 托管数据”，训练入口必须接受：

```text
--dataset_manifest <绝对路径>
```

平台会在版本输出目录写入 `dataset-manifest.json`，并为每个阶段传入同一路径。文件符合 [`datapilot_dataset_manifest_v1`](../contracts/training/datapilot_dataset_manifest_v1.schema.json)：

```json
{
  "contract": "datapilot_dataset_manifest_v1",
  "snapshot_ref": "dataset_snapshot_xxx",
  "run_ref": "run_xxx",
  "family_ref": "family_xxx",
  "splits": {
    "train": [{"dataset_date": "20260416", "release_ref": "dataset_release_xxx", "replica_ref": "dataset_replica_xxx", "local_root": "/data/...", "inventory_sha256": "..."}],
    "test": []
  }
}
```

- `train` 是当前训练使用的数据；`test` 可先忽略，供后续测试模块使用。
- `local_root` 是训练节点上的只读日期副本。项目不得修改其中内容。
- 项目可把索引、缓存或转换结果写到平台提供的 `--output_dir` 下。
- 平台不理解图片、轨迹、问答、mask 或其他模型样本结构。项目应自行把日期副本转为所需样本。
- `data_mixture` 等已有模型参数仍属于模型自身；它们不承担数据路径传递职责。
- 平台不要求模型项目使用 Git，也不会检查或推断模型结构、权重格式与样本语义。

## 2. 输出、退出和多阶段

- 所有模型、checkpoint、日志和缓存必须写入平台传入的 `--output_dir`。不要覆盖输出目录外的共享路径。
- 退出码 `0` 代表当前阶段成功；非零代表失败，标准输出和标准错误会被保留在任务日志中。
- 平台会对训练进程组发送 `SIGTERM` 以停止任务；训练程序应让主进程和子进程正常退出。
- 多阶段任务由平台顺序启动。模型注册中标记为“阶段输入参数”的字符串参数，在后续阶段会收到上一阶段的输出目录；模型项目自行决定如何加载该目录。
- 训练进程可读取 `DATAPILOT_RUN_REF`、`DATAPILOT_STAGE_REF` 和 `DATAPILOT_VERSION_LABEL` 关联平台任务；项目不需要自行向中心服务发请求。

## 3. 可选结构化训练事件

不接入事件协议时，平台仍保存原始日志，并可尝试解析 Transformers Trainer 日志。为获得稳定指标和 checkpoint 记录，训练程序可向 stdout 输出一行 JSON：

```json
{"contract":"datapilot_training_event_v1","type":"metric","step":10,"total_steps":100,"epoch":0.5,"loss":0.82,"learning_rate":0.00001,"grad_norm":1.2}
```

或：

```json
{"contract":"datapilot_training_event_v1","type":"checkpoint","step":100,"relative_path":"checkpoint-100"}
```

对应格式见 [`datapilot_training_event_v1`](../contracts/training/datapilot_training_event_v1.schema.json)。checkpoint 路径必须相对于当前阶段 `--output_dir`，不可使用绝对路径或 `..`。

## 4. 接入检查清单

1. 为训练参数解析器增加 `dataset_manifest`。
2. 用 manifest 的 `splits.train[*].local_root` 构造本项目训练样本。
3. 确认项目在 Conda 环境内可由登记入口启动。
4. 确认 `--output_dir` 可保存 checkpoint 和最终产物。
5. 使用少量 step 先验证数据读取、日志、SIGTERM 和输出目录；不要把该 smoke 结果当作模型效果评估。
