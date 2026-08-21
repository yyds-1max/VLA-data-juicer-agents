# 模型项目接入 DataPilot：算法工程师操作指南

这份文档写给需要把模型训练项目接入 DataPilot 的算法工程师。

先说结论：**模型代码不需要调用 DataPilot 的网页接口，也不需要连接中心服务器或 Worker。** DataPilot 启动训练时，会像人在终端里执行训练命令一样，把本次训练需要的信息传给模型项目。模型项目只要能接收这些信息、读取数据并把训练结果写到指定目录，就可以接入。

## 一、双方分别负责什么

DataPilot 负责：

- 把用户选择的数据从数据服务器传到训练节点；
- 选择 GPU，并启动、停止和监控训练进程；
- 告诉模型项目本次训练使用哪些数据、结果保存在哪里；
- 收集训练日志、Loss、学习率和 checkpoint 记录；
- 训练成功后登记一个模型版本。

模型项目负责：

- 读取 DataPilot 提供的数据清单；
- 理解本项目的数据格式，并把原始数据转换成训练样本；
- 完成模型训练；
- 把 checkpoint、最终权重和临时转换结果写入 DataPilot 指定的输出目录。

DataPilot 不负责理解图片、轨迹、问答、mask 或模型结构，也不会替模型项目决定怎样构造训练样本。

## 二、模型项目需要接入的四个接口

这里的“接口”不是 HTTP 地址，而是训练进程与 DataPilot 之间约定的输入和输出。

| 接口 | 在哪里 | 是否必须 | 用途 |
| --- | --- | --- | --- |
| `--dataset_manifest` | 训练命令的命令行参数 | 参数配置使用“DataPilot 托管数据”时必须 | 告诉模型项目本次训练使用哪些日期的数据，以及这些数据在训练节点上的位置 |
| `--output_dir` | 训练命令的命令行参数；实际名称由模型注册中的“产物输出参数”决定 | 必须 | 告诉模型项目本次训练的所有产物应该保存在哪里 |
| 训练日志和训练事件 | 训练程序的标准输出，也就是普通的 `print` 输出 | 日志必须可读；结构化事件可选 | 让网页展示日志、指标和 checkpoint 记录 |
| 阶段输入参数 | 模型注册中由用户指定的一个字符串参数 | 只有多阶段训练需要 | 第二阶段以后接收上一阶段的输出目录 |

DataPilot 还会设置三个环境变量，模型项目通常不需要使用：

- `DATAPILOT_RUN_REF`：本次训练任务的编号；
- `DATAPILOT_STAGE_REF`：当前训练阶段的编号；
- `DATAPILOT_VERSION_LABEL`：本次训练生成的模型版本名称。

它们只适合用来给模型自己的日志或缓存加标识，不能替代 `--dataset_manifest` 和 `--output_dir`。

## 三、最小接入：读取训练数据

### 1. 接收 `--dataset_manifest`
模型项目的训练入口必须支持`--dataset_manifest`这个命令行参数。
如果模型族在 DataPilot 中选择了“DataPilot 托管数据”，平台会在启动命令末尾自动增加：

```text
--dataset_manifest /某个绝对路径/dataset-manifest.json
```

因此，训练入口必须认识 `dataset_manifest` 这个参数。

普通 `argparse` 项目可以这样增加：

```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--dataset_manifest", type=str, default=None)
parser.add_argument("--output_dir", type=str, required=True)
args = parser.parse_args()
```

使用 Hugging Face `HfArgumentParser` 的项目，可以在已有的数据参数类中增加：

```python
from dataclasses import dataclass, field

@dataclass
class DataArguments:
    dataset_manifest: str | None = field(
        default=None,
        metadata={"help": "DataPilot 生成的本次训练数据清单"},
    )
```

如果模型只允许使用 DataPilot 托管数据，建议在程序启动后明确检查：

```python
if not data_args.dataset_manifest:
    raise ValueError("缺少 --dataset_manifest，无法确定本次训练数据")
```

### 2. 读取清单中的数据位置

`dataset-manifest.json` 是一个普通 JSON 文件，示例如下：

```json
{
  "contract": "datapilot_dataset_manifest_v1",
  "snapshot_ref": "dataset_snapshot_xxx",
  "run_ref": "run_xxx",
  "family_ref": "family_xxx",
  "splits": {
    "train": [
      {
        "dataset_date": "20260416",
        "release_ref": "dataset_release_xxx",
        "replica_ref": "dataset_replica_xxx",
        "local_root": "/data/caiji_test/datapilot-managed/20260416-xxxxxxx",
        "inventory_sha256": "..."
      }
    ],
    "test": []
  }
}
```

模型项目真正需要关注的只有：

- `splits.train`：本次用于训练的数据日期；
- `splits.train[*].dataset_date`：数据日期；
- `splits.train[*].local_root`：该日期数据在当前训练节点上的绝对路径；
- `splits.test`：用户为后续测试保留的数据，本轮训练可以不读取。

其余编号和摘要由 DataPilot 用来核对数据，模型项目一般不需要处理。

可以这样读取：

```python
import json
from pathlib import Path

def load_datapilot_training_roots(manifest_path: str) -> list[Path]:
    manifest_file = Path(manifest_path)
    with manifest_file.open("r", encoding="utf-8") as file:
        manifest = json.load(file)

    if manifest.get("contract") != "datapilot_dataset_manifest_v1":
        raise ValueError("不支持的数据清单格式")

    roots = []
    for item in manifest["splits"]["train"]:
        root = Path(item["local_root"])
        if not root.is_dir():
            raise FileNotFoundError(f"训练数据目录不存在：{root}")
        roots.append(root)

    if not roots:
        raise ValueError("本次训练没有选择训练数据")
    return roots
```

得到这些日期目录后，模型项目再按自己的规则查找图片、轨迹或标注文件，并生成训练所需的索引和样本。

### 3. 不要修改托管数据目录

`local_root` 指向 DataPilot 管理的数据副本。模型项目只能读取，不能在其中修改文件、生成缓存或保存索引。

需要生成中间文件时，请写入本次的 `output_dir`，例如：

```python
from pathlib import Path

prepared_dir = Path(args.output_dir) / "prepared"
prepared_dir.mkdir(parents=True, exist_ok=True)
```

这样每次训练的数据转换结果都会跟随对应模型版本保存，不会污染公共训练数据。

## 四、把训练结果保存到正确位置

用户在 DataPilot 登记模型族时，会填写“产物输出参数”。大多数项目使用 `--output_dir`。平台会为每个模型版本和训练阶段生成独立目录，并把完整路径传给这个参数。

模型项目应把以下内容都写入该目录或其子目录：

- checkpoint；
- 最终模型权重；
- 优化器状态；
- 数据索引和转换缓存；
- 模型项目自己生成的日志文件。

不要把本次训练产物固定写到项目目录或某个共享 checkpoint 目录，否则 DataPilot 无法知道哪个模型版本对应哪些文件，也可能覆盖其他任务。

训练程序退出码的含义是：

- `0`：当前阶段成功；
- 非 `0`：当前阶段失败，DataPilot 会保留输出和错误日志供用户排查。

## 五、日志、指标和 checkpoint 怎样出现在网页中

### 1. 普通日志

训练程序正常写到终端的内容会出现在任务详情页，例如：

```python
print("开始构造训练样本", flush=True)
```

不要在日志中打印密码、访问令牌或其他敏感参数。

### 2. 训练指标（可选，但建议接入）

DataPilot 可以尝试识别 Transformers Trainer 的常见日志。如果希望不同模型都能稳定展示 Loss、学习率和进度，训练程序可以额外输出一行 JSON：

```python
import json

event = {
    "contract": "datapilot_training_event_v1",
    "type": "metric",
    "step": 10,
    "total_steps": 100,
    "epoch": 0.5,
    "loss": 0.82,
    "learning_rate": 0.00001,
    "grad_norm": 1.2,
}
print(json.dumps(event, ensure_ascii=False), flush=True)
```

其中只有 `step` 是必填项。模型没有某项指标时可以不输出该字段，不要用 `NaN` 或 `Infinity` 代替。

### 3. checkpoint 记录（可选，但建议接入）

DataPilot 不会扫描模型目录并猜测哪个文件是 checkpoint。模型保存完 checkpoint 后，应主动输出一条记录：

```python
event = {
    "contract": "datapilot_training_event_v1",
    "type": "checkpoint",
    "step": 100,
    "relative_path": "checkpoint-100",
}
print(json.dumps(event, ensure_ascii=False), flush=True)
```

`relative_path` 是相对于当前 `output_dir` 的路径，例如 `checkpoint-100`。不能填写绝对路径，也不能包含 `..`。

这条记录只告诉 DataPilot“刚刚保存了一个 checkpoint”。平台会把它显示在模型版本详情中，但不会把它称为最佳 checkpoint，也不会判断模型效果。

完整的机器可读格式位于：

- [`datapilot_dataset_manifest_v1.schema.json`](../contracts/training/datapilot_dataset_manifest_v1.schema.json)
- [`datapilot_training_event_v1.schema.json`](../contracts/training/datapilot_training_event_v1.schema.json)

正常接入时先看本文即可；只有需要严格校验字段时才需要阅读这两个 Schema 文件。

## 六、多阶段训练怎样接入

如果一个任务分为多个训练阶段，DataPilot 会依次启动每个阶段，并为每个阶段生成独立的 `output_dir`。

模型注册时，用户可以把一个字符串参数标记为“阶段输入参数”。例如模型通过 `--model_name_or_path` 加载初始权重，就把这个参数标记为阶段输入参数。

之后用户在第二阶段选择“使用上一阶段输出目录”时，DataPilot 会自动执行类似命令：

```text
--model_name_or_path /上一阶段的输出目录
```

模型项目只需要像平时一样从这个参数加载模型或权重，不需要自己查询上一阶段任务。第一阶段仍使用用户在页面中填写的初始值。

## 七、停止训练时模型项目应该怎么做

用户点击“停止训练”后，DataPilot 会先通知训练进程正常退出；如果长时间没有退出，才会强制结束。

模型程序需要做到：

- 不要忽略系统发出的停止信号；
- 使用 Torchrun 或多进程训练时，主进程退出后应正确结束子进程；
- 如果项目捕获了停止信号，应尽快完成必要清理并退出，不要继续长时间训练。

中心服务短暂断开不会立刻结束训练，训练节点上的进程会继续运行，连接恢复后再同步状态。

## 八、在 DataPilot 页面中怎样登记这些信息

入口位于：`模型训练 → 新建训练任务`。

- 新模型：在模型族选择框中点击“登记新模型族”，或点击页面中的同名按钮；
- 已有模型：选择模型族后点击“修改参数配置”。

登记时需要确认：

1. “训练数据管理方式”选择“DataPilot 托管数据”；
2. 工作目录是训练节点上的模型项目绝对路径；
3. 启动程序和训练入口与平时在终端使用的一致；
4. 运行环境选择正确的 Conda 环境；
5. 产物输出参数填写模型实际支持的参数，通常是 `--output_dir`；
6. 指标日志格式根据模型当前输出选择“普通文本”“Transformers Trainer”或“JSONL”；
7. 如果需要多阶段训练，再指定一个阶段输入参数。

`--dataset_manifest` 是 DataPilot 保留参数，不需要在普通超参数或固定参数中重复登记。平台会在托管数据模式下自动加入它。

保存模型配置后，DataPilot 会检查项目目录、训练入口、Conda 环境和输出目录权限。这个检查只能证明命令具备启动条件，不能代替一次短训练验收。

## 九、建议的接入与验收顺序

1. 先在模型项目中增加 `--dataset_manifest` 参数。
2. 读取 `splits.train[*].local_root`，用本项目原有逻辑构造训练样本。
3. 把生成的索引、缓存、checkpoint 和最终权重全部改到 `output_dir` 下。
4. 在训练节点的目标 Conda 环境中直接运行一次数据读取检查，不加载模型和 GPU。
5. 在 DataPilot 中登记模型族并保存，确认“验证配置”通过。
6. 在新建训练页选择一份较小的数据，生成命令预览，核对 `--dataset_manifest` 和输出目录。
7. 使用空闲 GPU 运行 1～2 step 的短任务。
8. 在任务详情中检查原始日志、Loss 和任务状态。
9. 在模型版本详情中检查版本模型目录；如果接入了 checkpoint 事件，同时确认 checkpoint 记录可见。
10. 最后再验证停止训练和多阶段衔接。

## 十、NaVILA 的实际接入参考

开发阶段，我们已经在训练节点的 `/data/caiji_test/NaVILA` 中完成了一版接入。它可以作为其他模型项目的参考，但不要直接复制其中的 NaVILA 数据处理规则。

### 1. 增加 DataPilot 参数

在 `llava/train/args.py` 的 `DataArguments` 中增加了 `dataset_manifest`。这样原来的训练入口 `llava/train/train_mem.py` 不需要更换，仍由 Hugging Face 参数解析器接收平台自动传入的：

```text
--dataset_manifest /版本输出目录/dataset-manifest.json
```

原有的 `data_mixture=rxr` 也没有改成数据路径。它仍表示 NaVILA 使用哪套数据加载逻辑；真正的数据位置来自 manifest。

其他模型可以参考这种做法：**保留模型原有的“数据类型或数据配方”参数，再单独增加 `dataset_manifest` 接收本次训练的数据位置。**

### 2. 增加模型自己的数据转换程序

NaVILA 新增了 `llava/data/datapilot_manifest.py`，主要完成以下工作：

1. 打开 `dataset-manifest.json`；
2. 读取 `splits.train` 中每个日期的 `local_root`；
3. 核对目录中的 DataPilot 标记，防止误读其他目录；
4. 在日期目录中寻找修正后的 `*_trajectory_fix_five.json` 和 `fisheye_front` 图像；
5. 按 NaVILA 当前需要的格式生成训练索引；
6. 把索引原子写入版本输出目录下的 `prepared/`，不修改传输过来的日期数据。

生成的索引类似：

```text
<版本输出目录>/prepared/navila-train-<manifest摘要>.json
```

同一份 manifest 再次启动时可以复用该索引，manifest 发生变化时会生成新的索引文件。

该项目还提供了一个不加载模型和 GPU 的检查入口：

```bash
python -m llava.data.datapilot_manifest \
  --dataset-manifest /绝对路径/dataset-manifest.json \
  --check-only
```

它会输出日期数、clip 数、样本数、缺失文件以及首个样本的简单摘要。算法工程师可以先用这种轻量检查确认数据接入，再启动 GPU 训练。

### 3. 接回原来的 Dataset

在 `llava/data/builder.py` 中增加了一个很小的分支：

- 当 `data_mixture=rxr` 且传入了 `dataset_manifest` 时，先生成或复用上述索引；
- 再把该索引交给 NaVILA 原有的 `LazyVLNCEDataset`；
- 没有传入 manifest 时，仍保留项目原来的数据加载方式。

这种设计的重点是：DataPilot 只负责提供“本次数据在哪里”，模型项目自己的 Dataset 继续负责“怎样把这些数据变成张量”。不需要为了接入平台重写整个训练流程。

### 4. 上报 checkpoint

NaVILA 新增了 `llava/train/callbacks/datapilot_callback.py`。当 Transformers Trainer 保存 checkpoint 后，只有主训练进程会检查对应目录是否已经生成，然后输出一条 DataPilot checkpoint 事件。

该 callback 只在检测到 `DATAPILOT_RUN_REF` 时启用，因此算法工程师仍可以在 DataPilot 之外按原来的方式运行 NaVILA。

NaVILA 的 Loss、学习率和 Step 继续使用 Transformers Trainer 的日志，由 Worker 解析；checkpoint 则通过这个 callback 明确登记。这样模型版本详情页既能展示训练指标，也能展示 checkpoint 记录。

### 5. 哪些内容可以参考，哪些不能照搬

其他模型项目可以直接参考以下接入结构：

- 在参数类中增加 `dataset_manifest`；
- 增加一个模型自己的 manifest 转换模块；
- 把转换结果写到 `output_dir/prepared`；
- 在原有 Dataset 构建入口增加一个托管数据分支；
- 在 checkpoint 真正保存完成后输出事件；
- 保留不经过 DataPilot 时的原有运行方式。

NaVILA 适配中的以下内容属于当前开发验收数据，不是通用平台规则：

- 使用 `fisheye_front` 图像；
- 查找 `*_trajectory_fix_five.json`；
- 使用连续四帧；
- 使用 16×3 轨迹；
- 根据目标颜色和位置生成 `q/a`。

这套样本生成逻辑只用来验证 DataPilot 的数据传递和训练执行闭环，不代表正式的 NaVILA 数据算法。系统正式上线时，应由负责该模型的算法工程师确认或替换这部分业务实现。

## 十一、常见问题

### 启动时报“不认识 `--dataset_manifest`”

训练入口的参数解析器还没有增加该参数，或者真正启动的入口不是修改过的文件。

### 清单存在，但模型仍然读取旧的固定路径

项目虽然接收了 `dataset_manifest`，但数据集构造代码仍在使用原来的硬编码路径。需要让托管数据模式优先使用清单中的 `local_root`。

### 训练成功，但模型版本目录中没有权重

模型可能仍把权重写到了项目目录或旧的共享 checkpoint 目录。请检查保存逻辑是否真正使用平台传入的 `output_dir`。

### 网页能看到日志，但没有 Loss 曲线

先确认模型注册中的指标日志格式是否正确。如果模型日志不是 Transformers 常见格式，建议按本文第五节输出结构化 metric 事件。

### 模型版本中没有 checkpoint 记录

DataPilot 不会通过目录名自动猜测 checkpoint。请在保存完成后输出结构化 checkpoint 事件。

### 用户选择了测试集，为什么训练程序没有使用

当前测试集用于后续独立测试任务，训练程序可以暂时忽略 `splits.test`。不要把它当作训练期验证集自动加入训练。
