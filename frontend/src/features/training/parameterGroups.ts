import type { TrainingParameterDefinition } from "../../api/types";

export type TrainingParameterGroup = {
  key: string;
  label: string;
  order: number;
  collapsed: boolean;
  custom: boolean;
  hint: string;
};

export const builtinTrainingParameterGroups: TrainingParameterGroup[] = [
  { key: "common", label: "常用参数", order: 0, collapsed: false, custom: false, hint: "高频训练参数保持常驻，可直接调整。" },
  { key: "optimizer", label: "优化器与正则", order: 100, collapsed: true, custom: true, hint: "权重衰减和学习率预热。" },
  { key: "performance", label: "性能与显存", order: 200, collapsed: true, custom: true, hint: "并行配置、计算精度和显存优化。" },
  { key: "multimodal", label: "模型与多模态", order: 300, collapsed: true, custom: true, hint: "模型版本、视觉塔、投影器和图像 token 策略。" },
  { key: "data_eval", label: "数据与验证", order: 400, collapsed: true, custom: true, hint: "长视频采样、帧率、预处理和验证行为。" },
  { key: "artifacts", label: "日志与产物", order: 500, collapsed: true, custom: true, hint: "日志频率、保留策略和指标上报。" },
  { key: "other", label: "其他参数", order: 1000, collapsed: true, custom: false, hint: "尚未归入专用分组的参数。" },
];

export const commonTrainingParameterKeys = new Set([
  "model_name_or_path", "seed", "data_mixture", "num_video_frames",
  "tune_vision_tower", "tune_mm_projector", "tune_language_model", "tune_traj_model", "bf16",
  "num_train_epochs", "per_device_train_batch_size", "gradient_accumulation_steps", "save_strategy", "save_steps",
  "learning_rate", "lr_scheduler_type", "model_max_length", "dataloader_num_workers",
]);

const legacyGroupKeys: Record<string, Set<string>> = {
  optimizer: new Set(["weight_decay", "warmup_ratio"]),
  performance: new Set(["deepspeed", "tf32", "gradient_checkpointing"]),
  multimodal: new Set(["version", "vision_tower", "mm_vision_select_feature", "mm_projector", "mm_vision_select_layer", "mm_use_im_start_end", "mm_use_im_patch_token", "image_aspect_ratio"]),
  data_eval: new Set(["longvila_sampler", "fps", "do_eval", "lazy_preprocess"]),
  artifacts: new Set(["logging_steps", "save_total_limit", "report_to"]),
};

const builtinByKey = new Map(builtinTrainingParameterGroups.map((group) => [group.key, group]));

export function legacyTrainingParameterGroup(parameterKey: string): TrainingParameterGroup {
  if (commonTrainingParameterKeys.has(parameterKey)) return builtinByKey.get("common")!;
  for (const [groupKey, keys] of Object.entries(legacyGroupKeys)) {
    if (keys.has(parameterKey)) return builtinByKey.get(groupKey)!;
  }
  return builtinByKey.get("other")!;
}

export function trainingParameterGroupFor(parameter: TrainingParameterDefinition): TrainingParameterGroup {
  if (!parameter.display_group) return legacyTrainingParameterGroup(parameter.key);
  const builtin = builtinByKey.get(parameter.display_group);
  if (builtin) return { ...builtin, label: parameter.display_group_label?.trim() || builtin.label, order: parameter.display_group_order ?? builtin.order };
  return {
    key: parameter.display_group,
    label: parameter.display_group_label?.trim() || "未命名分组",
    order: parameter.display_group_order ?? 600,
    collapsed: true,
    custom: true,
    hint: "模型注册时创建的自定义参数分组。",
  };
}

export function availableTrainingParameterGroups(definitions: TrainingParameterDefinition[]): TrainingParameterGroup[] {
  const groups = new Map<string, TrainingParameterGroup>();
  groups.set("common", builtinByKey.get("common")!);
  groups.set("other", builtinByKey.get("other")!);
  definitions.forEach((parameter) => {
    const group = trainingParameterGroupFor(parameter);
    if (!groups.has(group.key)) groups.set(group.key, group);
  });
  return [...groups.values()].sort((left, right) => left.order - right.order || left.label.localeCompare(right.label, "zh-CN"));
}

export function usedTrainingParameterGroups(definitions: TrainingParameterDefinition[]): TrainingParameterGroup[] {
  const groups = new Map<string, TrainingParameterGroup>();
  groups.set("common", builtinByKey.get("common")!);
  groups.set("other", builtinByKey.get("other")!);
  definitions.forEach((parameter) => {
    const group = trainingParameterGroupFor(parameter);
    if (!groups.has(group.key)) groups.set(group.key, group);
  });
  return [...groups.values()].sort((left, right) => left.order - right.order || left.label.localeCompare(right.label, "zh-CN"));
}

export function assignTrainingParameterGroup(parameter: TrainingParameterDefinition, group: TrainingParameterGroup): TrainingParameterDefinition {
  return { ...parameter, display_group: group.key, display_group_label: group.label, display_group_order: group.order };
}

export function normalizeTrainingParameterGroups(definitions: TrainingParameterDefinition[]): TrainingParameterDefinition[] {
  return definitions.map((parameter) => assignTrainingParameterGroup(parameter, trainingParameterGroupFor(parameter)));
}
