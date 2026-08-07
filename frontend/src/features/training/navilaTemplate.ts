import type { TrainingParameterDefinition } from "../../api/types";
import { assignTrainingParameterGroup, legacyTrainingParameterGroup } from "./parameterGroups";

const value = (key: string, label: string, type: TrainingParameterDefinition["type"], defaultValue: TrainingParameterDefinition["default"], options: Partial<TrainingParameterDefinition> = {}): TrainingParameterDefinition => assignTrainingParameterGroup({
  key, label, type, default: defaultValue, editable: true, cli_flag: `--${key}`, argument_style: type === "boolean" ? "explicit_boolean" : "value", ...options,
}, legacyTrainingParameterGroup(key));

/**
 * A safe, editable registration preset. Paths are deliberately placeholders:
 * the template never binds to or reads either shared NaVILA directory.
 */
export const navilaTrajectoryParameters: TrainingParameterDefinition[] = [
  value("longvila_sampler", "LongVILA 采样器", "boolean", true, { description: "启用面向长视频序列的采样方式。" }),
  value("deepspeed", "DeepSpeed 配置", "string", "./scripts/zero3.json", { description: "指定 DeepSpeed/ZeRO 并行训练配置文件。" }),
  value("model_name_or_path", "预训练参数加载地址", "string", "/workspace/checkpoints/navila", { editable: true, description: "注册前请替换为部署环境允许的路径。" }),
  value("version", "模型版本", "enum", "llama_3", { choices: [{ value: "llama_3", label: "llama_3" }], description: "选择训练代码使用的对话模板版本。" }),
  value("seed", "随机种子", "integer", 10, { editable: true, minimum: 0, description: "控制随机初始化和数据顺序，便于复现。" }),
  value("data_mixture", "数据混合配置", "string", "rxr", { editable: true, description: "指定训练数据集或数据混合方案。" }),
  value("vision_tower", "视觉编码器", "string", "google/siglip-so400m-patch14-384", { description: "指定提取图像特征的视觉骨干模型。" }),
  value("mm_vision_select_feature", "视觉特征选择", "enum", "cls_patch", { choices: [{ value: "cls_patch", label: "cls_patch" }], description: "选择送入多模态模块的视觉特征类型。" }),
  value("mm_projector", "多模态投影器", "enum", "mlp_downsample", { choices: [{ value: "mlp_downsample", label: "mlp_downsample" }], description: "定义视觉特征到语言空间的映射结构。" }),
  value("num_video_frames", "视频帧数", "integer", 4, { editable: true, minimum: 1, maximum: 64, description: "实际训练帧数；与脚本名称无关。" }),
  value("tune_vision_tower", "训练视觉网络", "boolean", false, { editable: true, description: "决定视觉编码器参数是否参与训练。" }),
  value("tune_mm_projector", "训练对齐网络", "boolean", true, { editable: true, description: "决定多模态投影器是否参与训练。" }),
  value("tune_language_model", "训练语言网络", "boolean", true, { editable: true, description: "决定语言模型参数是否参与训练。" }),
  value("tune_traj_model", "训练轨迹网络", "boolean", true, { editable: true, description: "决定轨迹预测模块是否参与训练。" }),
  value("mm_vision_select_layer", "视觉特征层", "integer", -2, { description: "选择视觉编码器中用于训练的特征层。" }),
  value("mm_use_im_start_end", "使用图像起止 token", "boolean", false, { description: "是否在图像特征前后加入专用起止 token。" }),
  value("mm_use_im_patch_token", "使用图像 patch token", "boolean", false, { description: "是否使用专用 patch token 表示图像块。" }),
  value("image_aspect_ratio", "图像宽高比策略", "enum", "resize", { choices: [{ value: "resize", label: "resize" }], description: "指定输入图像的缩放和宽高比处理方式。" }),
  value("bf16", "启用 BF16", "boolean", true, { editable: true, description: "使用 BF16 精度训练以降低显存占用。" }),
  value("num_train_epochs", "训练 Epoch", "integer", 16, { editable: true, minimum: 1, maximum: 1000, description: "设置完整遍历训练数据的次数。" }),
  value("per_device_train_batch_size", "每卡 Batch size", "integer", 16, { editable: true, minimum: 1, maximum: 1024, description: "设置每张 GPU 每一步处理的样本数。" }),
  value("gradient_accumulation_steps", "梯度累积步数", "integer", 2, { editable: true, minimum: 1, maximum: 1024, description: "累积多步梯度后再更新一次参数。" }),
  value("do_eval", "执行验证", "boolean", false, { editable: true, description: "训练过程中是否运行验证流程。" }),
  value("save_strategy", "保存策略", "enum", "steps", { editable: true, choices: [{ value: "steps", label: "steps" }, { value: "epoch", label: "epoch" }, { value: "no", label: "no" }], description: "选择按 step、epoch 或不保存 checkpoint。" }),
  value("save_steps", "保存 step 间隔", "integer", 100, { editable: true, minimum: 1, visible_when: { parameter_key: "save_strategy", equals: "steps" }, description: "按 step 保存时的 checkpoint 间隔。" }),
  value("fps", "采样 FPS", "number", 0, { editable: true, minimum: 0, description: "控制视频按帧率采样；0 表示使用默认策略。" }),
  value("save_total_limit", "最多保留 checkpoint", "integer", 1, { editable: true, minimum: 1, description: "限制磁盘中保留的 checkpoint 数量。" }),
  value("learning_rate", "学习率", "number", 0.00001, { editable: true, minimum: 0, maximum: 1, description: "控制每次参数更新的步长。" }),
  value("weight_decay", "权重衰减", "number", 0, { editable: true, minimum: 0, description: "设置优化器的权重衰减强度。" }),
  value("warmup_ratio", "Warmup 比例", "number", 0.03, { editable: true, minimum: 0, maximum: 1, description: "设置学习率预热阶段占总训练步数的比例。" }),
  value("lr_scheduler_type", "学习率调度器", "enum", "cosine", { editable: true, choices: [{ value: "cosine", label: "cosine" }, { value: "linear", label: "linear" }, { value: "constant", label: "constant" }], description: "选择训练期间学习率的变化曲线。" }),
  value("logging_steps", "日志 step 间隔", "integer", 1, { editable: true, minimum: 1, description: "设置每隔多少 step 记录一次训练日志。" }),
  value("tf32", "启用 TF32", "boolean", false, { editable: true, description: "允许支持的 GPU 使用 TF32 加速矩阵计算。" }),
  value("model_max_length", "最大序列长度", "integer", 4096, { editable: true, minimum: 1, description: "限制单个训练样本的最大 token 数。" }),
  value("gradient_checkpointing", "梯度检查点", "boolean", true, { editable: true, description: "用额外计算换取更低的训练显存占用。" }),
  value("dataloader_num_workers", "DataLoader 线程数", "integer", 16, { editable: true, minimum: 0, maximum: 256, description: "设置并行加载和预处理数据的进程数。" }),
  value("lazy_preprocess", "延迟预处理", "boolean", true, { editable: true, description: "在样本实际使用时再执行预处理。" }),
  value("report_to", "指标上报目标", "enum", "wandb", { editable: true, choices: [{ value: "wandb", label: "wandb" }, { value: "none", label: "none" }], description: "选择训练指标上报到哪个平台。" }),
];

export const navilaTrajectoryLaunchTemplate = {
  domain: "vla",
  server_ref: "fake-local",
  working_directory: "/workspace/navila",
  executable: "torchrun",
  entrypoint: "llava/train/train_mem.py",
  fixed_argv: [] as string[],
  output_root: "/workspace/outputs",
  output_flag: "--output_dir",
};
