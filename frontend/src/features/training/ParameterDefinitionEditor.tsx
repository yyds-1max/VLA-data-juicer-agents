import { ArrowDown, ArrowUp, Plus, Settings2, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { TrainingArgumentStyle, TrainingParameterDefinition, TrainingParameterType } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { ParameterDependencyDialog, parameterDependencySummary } from "./ParameterDependencyDialog";
import { assignTrainingParameterGroup, availableTrainingParameterGroups, normalizeTrainingParameterGroups, trainingParameterGroupFor, usedTrainingParameterGroups, type TrainingParameterGroup } from "./parameterGroups";

type Props = {
  definitions: TrainingParameterDefinition[];
  disabled?: boolean;
  onChange: (definitions: TrainingParameterDefinition[]) => void;
};

const inputClass = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-sm text-console-text focus:border-console-cyan focus:outline-hidden";
const maxSafeInteger = Number.MAX_SAFE_INTEGER;
const parameterTypes: Array<{ value: TrainingParameterType; label: string }> = [
  { value: "integer", label: "整数" }, { value: "number", label: "浮点数" }, { value: "boolean", label: "布尔值" }, { value: "enum", label: "枚举" }, { value: "string", label: "字符串" },
];

function defaultForType(type: TrainingParameterType): Pick<TrainingParameterDefinition, "default" | "choices" | "argument_style"> {
  if (type === "boolean") return { default: false, choices: undefined, argument_style: "explicit_boolean" };
  if (type === "integer" || type === "number") return { default: 0, choices: undefined, argument_style: "value" };
  if (type === "enum") return { default: "option_1", choices: [{ value: "option_1", label: "option_1" }], argument_style: "value" };
  return { default: "", choices: undefined, argument_style: "value" };
}

function NumericField({ ariaLabel, value, integer, optional = false, disabled, placeholder, externalError, onChange }: { ariaLabel: string; value: number | null | undefined | string; integer: boolean; optional?: boolean; disabled: boolean; placeholder?: string; externalError?: string | null; onChange: (value: number | undefined) => void }) {
  const initial = typeof value === "number" && Number.isFinite(value) ? String(value) : typeof value === "string" ? value : "";
  const [draft, setDraft] = useState(initial);
  const focused = useRef(false);
  useEffect(() => {
    if (focused.current || (typeof value === "number" && !Number.isFinite(value))) return;
    setDraft(typeof value === "number" ? String(value) : typeof value === "string" ? value : "");
  }, [value]);
  const parsed = draft.trim() === "" ? undefined : Number(draft);
  const intrinsicError = draft.trim() === "" ? (optional ? null : "请输入数值")
    : parsed == null || !Number.isFinite(parsed) ? "请输入有效数值"
    : integer && !Number.isInteger(parsed) ? "请输入整数"
    : integer && !Number.isSafeInteger(parsed) ? `整数范围不能超过 ±${maxSafeInteger}`
    : null;
  const error = intrinsicError ?? externalError ?? null;
  return <><input aria-label={ariaLabel} aria-invalid={Boolean(error)} className={`${inputClass} ${error ? "border-rose-500" : ""}`} type="text" inputMode={integer ? "numeric" : "decimal"} value={draft} disabled={disabled} placeholder={placeholder} onFocus={() => { focused.current = true; }} onBlur={() => { focused.current = false; }} onChange={(event) => {
    const raw = event.target.value;
    setDraft(raw);
    const numberValue = raw.trim() === "" ? undefined : Number(raw);
    const valid = numberValue != null && Number.isFinite(numberValue) && (!integer || (Number.isInteger(numberValue) && Number.isSafeInteger(numberValue)));
    onChange(valid ? numberValue : optional && raw.trim() === "" ? undefined : Number.NaN);
  }} />{error ? <span role="alert" className="mt-1 block text-[11px] text-rose-700">{error}</span> : null}</>;
}

export function validateParameterDefinitions(definitions: TrainingParameterDefinition[]) {
  if (!definitions.length) throw new Error("请至少添加一个训练参数。");
  if (definitions.filter((parameter) => parameter.semantic_role === "dataset").length > 1) throw new Error("每个模型族最多只能指定一个数据集参数。");
  if (definitions.filter((parameter) => parameter.semantic_role === "stage_input").length > 1) throw new Error("每个模型族最多只能指定一个阶段输入参数。");
  const keys = new Set<string>(); const flags = new Set<string>();
  definitions.forEach((parameter, index) => {
    const position = index + 1;
    if (!/^[A-Za-z][A-Za-z0-9_]{0,99}$/.test(parameter.key)) throw new Error(`第 ${position} 个参数的字段名无效，只能使用字母、数字和下划线，且不能超过 100 个字符。`);
    if (keys.has(parameter.key)) throw new Error(`参数字段名 ${parameter.key} 重复。`);
    keys.add(parameter.key);
    if (!parameter.label.trim()) throw new Error(`参数 ${parameter.key} 缺少显示名称。`);
    if (parameter.semantic_role === "dataset" && !["string", "enum"].includes(parameter.type)) throw new Error(`数据集参数 ${parameter.key} 只能使用字符串或枚举类型。`);
    if (parameter.semantic_role === "stage_input" && parameter.type !== "string") throw new Error(`阶段输入参数 ${parameter.key} 只能使用字符串类型。`);
    if (parameter.semantic_role === "stage_input" && parameter.visible_when) throw new Error(`阶段输入参数 ${parameter.key} 必须始终可用，不能设置参数依赖条件。`);
    if (parameter.label.trim().length > 200) throw new Error(`参数 ${parameter.key} 的显示名称不能超过 200 个字符。`);
    if ((parameter.description?.length ?? 0) > 120) throw new Error(`参数 ${parameter.key} 的解释不能超过 120 个字符。`);
    const flag = parameter.cli_flag || `--${parameter.key}`;
    if (!/^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(flag)) throw new Error(`参数 ${parameter.key} 的 CLI flag 无效。`);
    if (flags.has(flag)) throw new Error(`CLI flag ${flag} 重复。`);
    flags.add(flag);
    if (parameter.minimum != null && !Number.isFinite(parameter.minimum)) throw new Error(`参数 ${parameter.key} 的最小值必须是有效数值。`);
    if (parameter.maximum != null && !Number.isFinite(parameter.maximum)) throw new Error(`参数 ${parameter.key} 的最大值必须是有效数值。`);
    if (parameter.minimum != null && parameter.maximum != null && parameter.minimum > parameter.maximum) throw new Error(`参数 ${parameter.key} 的最小值不能大于最大值。`);
    if ((parameter.type === "integer" || parameter.type === "number") && (typeof parameter.default !== "number" || !Number.isFinite(parameter.default))) throw new Error(`参数 ${parameter.key} 需要有效数值默认值。`);
    if (parameter.type === "integer" && !Number.isInteger(parameter.default)) throw new Error(`参数 ${parameter.key} 需要整数默认值。`);
    if (parameter.type === "integer" && !Number.isSafeInteger(parameter.default)) throw new Error(`参数 ${parameter.key} 的整数默认值超出安全范围。`);
    if (parameter.type === "integer" && [parameter.minimum, parameter.maximum].some((bound) => bound != null && (!Number.isInteger(bound) || !Number.isSafeInteger(bound)))) throw new Error(`参数 ${parameter.key} 的整数范围必须使用安全整数。`);
    if (typeof parameter.default === "number" && parameter.minimum != null && parameter.default < parameter.minimum) throw new Error(`参数 ${parameter.key} 的默认值不能小于最小值。`);
    if (typeof parameter.default === "number" && parameter.maximum != null && parameter.default > parameter.maximum) throw new Error(`参数 ${parameter.key} 的默认值不能大于最大值。`);
    if (parameter.type === "boolean" && typeof parameter.default !== "boolean") throw new Error(`参数 ${parameter.key} 需要布尔默认值。`);
    if (parameter.type === "boolean" && !["explicit_boolean", "flag_when_true"].includes(parameter.argument_style ?? "flag_when_true")) throw new Error(`布尔参数 ${parameter.key} 的 argv 表达方式无效。`);
    if (parameter.type !== "boolean" && (parameter.argument_style ?? "value") !== "value") throw new Error(`非布尔参数 ${parameter.key} 只能使用 flag + value。`);
    if ((parameter.type === "string" || parameter.type === "enum") && typeof parameter.default !== "string") throw new Error(`参数 ${parameter.key} 需要字符串默认值。`);
    if (parameter.type === "string") {
      const defaultValue = typeof parameter.default === "string" ? parameter.default : "";
      if (/\x00|\r|\n/.test(defaultValue)) throw new Error(`字符串参数 ${parameter.key} 不能包含换行或控制字符。`);
      const minimumLength = parameter.string_min_length ?? 0;
      const maximumLength = parameter.string_max_length ?? 512;
      if (!Number.isInteger(minimumLength) || minimumLength < 0 || minimumLength > 512) throw new Error(`字符串参数 ${parameter.key} 的最短长度需要是 0–512 的整数。`);
      if (!Number.isInteger(maximumLength) || maximumLength < 0 || maximumLength > 512) throw new Error(`字符串参数 ${parameter.key} 的最长长度需要是 0–512 的整数。`);
      if (minimumLength > maximumLength) throw new Error(`字符串参数 ${parameter.key} 的最短长度不能大于最长长度。`);
      if (defaultValue.length < minimumLength || defaultValue.length > maximumLength) throw new Error(`字符串参数 ${parameter.key} 的默认值长度需要在 ${minimumLength}–${maximumLength} 个字符之间。`);
    }
    if (parameter.type === "enum") {
      const choices = parameter.choices ?? [];
      if (!choices.length) throw new Error(`枚举参数 ${parameter.key} 至少需要一个选项。`);
      if (choices.length > 100) throw new Error(`枚举参数 ${parameter.key} 最多支持 100 个选项。`);
      choices.forEach((choice, choiceIndex) => {
        if (!choice.value.trim()) throw new Error(`枚举参数 ${parameter.key} 的第 ${choiceIndex + 1} 个实际值不能为空。`);
        if (choice.value.trim().length > 200) throw new Error(`枚举参数 ${parameter.key} 的第 ${choiceIndex + 1} 个实际值不能超过 200 个字符。`);
      });
      const values = choices.map((choice) => choice.value.trim());
      if (new Set(values).size !== values.length) throw new Error(`枚举参数 ${parameter.key} 的选项值不能重复。`);
      const defaultChoice = choices.find((choice) => choice.value === String(parameter.default));
      if (!defaultChoice || !values.includes(defaultChoice.value.trim())) throw new Error(`枚举参数 ${parameter.key} 的默认值必须来自选项。`);
    }
  });
  const byKey = new Map(definitions.map((parameter) => [parameter.key, parameter]));
  const dependencies = new Map<string, string>();
  definitions.forEach((parameter) => {
    const condition = parameter.visible_when;
    if (!condition) return;
    const controller = byKey.get(condition.parameter_key);
    if (!controller) throw new Error(`参数 ${parameter.key} 的可用条件引用了不存在的参数。`);
    if (controller.key === parameter.key) throw new Error(`参数 ${parameter.key} 不能依赖自身。`);
    const expected = condition.equals;
    const typeMatches = controller.type === "boolean" ? typeof expected === "boolean"
      : controller.type === "integer" ? typeof expected === "number" && Number.isInteger(expected)
      : controller.type === "number" ? typeof expected === "number" && Number.isFinite(expected)
      : typeof expected === "string";
    if (!typeMatches) throw new Error(`参数 ${parameter.key} 的条件值与 ${controller.key} 类型不一致。`);
    if (controller.type === "enum" && !controller.choices?.some((choice) => choice.value === expected)) throw new Error(`参数 ${parameter.key} 的条件值不在 ${controller.key} 的枚举选项中。`);
    if (typeof expected === "number" && controller.minimum != null && expected < controller.minimum) throw new Error(`参数 ${parameter.key} 的条件值不能小于 ${controller.key} 的最小值。`);
    if (typeof expected === "number" && controller.maximum != null && expected > controller.maximum) throw new Error(`参数 ${parameter.key} 的条件值不能大于 ${controller.key} 的最大值。`);
    if (controller.type === "string" && typeof expected === "string" && expected.length < (controller.string_min_length ?? 0)) throw new Error(`参数 ${parameter.key} 的条件值短于 ${controller.key} 的最短长度。`);
    if (controller.type === "string" && typeof expected === "string" && expected.length > (controller.string_max_length ?? 512)) throw new Error(`参数 ${parameter.key} 的条件值超过 ${controller.key} 的最长长度。`);
    dependencies.set(parameter.key, controller.key);
  });
  dependencies.forEach((_, start) => {
    const visited = new Set<string>();
    let current: string | undefined = start;
    while (current && dependencies.has(current)) {
      if (visited.has(current)) throw new Error("参数可用条件不能形成循环依赖。");
      visited.add(current);
      current = dependencies.get(current);
    }
  });
  const normalized = normalizeTrainingParameterGroups(definitions);
  const metadata = new Map<string, { label: string; order: number }>();
  const labels = new Map<string, string>();
  normalized.forEach((parameter) => {
    const group = trainingParameterGroupFor(parameter);
    const label = parameter.display_group_label?.trim() ?? group.label;
    if (group.custom && (label.length < 2 || label.length > 30)) throw new Error(`自定义分组 ${group.key} 的名称需要 2–30 个字符。`);
    const known = metadata.get(group.key);
    if (known && (known.label !== label || known.order !== group.order)) throw new Error(`参数分组 ${group.key} 的名称或顺序不一致。`);
    const knownKey = labels.get(label.toLocaleLowerCase());
    if (knownKey && knownKey !== group.key) throw new Error(`参数分组名称“${label}”重复。`);
    metadata.set(group.key, { label, order: group.order });
    labels.set(label.toLocaleLowerCase(), group.key);
  });
  return normalized.map((parameter) => {
    if (parameter.type !== "enum") return { ...parameter, display_group_label: parameter.display_group_label?.trim(), editable: true };
    const choices = (parameter.choices ?? []).map((choice) => {
      const value = choice.value.trim();
      return { value, label: value };
    });
    const selected = (parameter.choices ?? []).find((choice) => choice.value === parameter.default);
    return { ...parameter, choices, default: selected?.value.trim() ?? parameter.default, display_group_label: parameter.display_group_label?.trim(), editable: true };
  });
}

function EnumChoiceEditor({ parameter, disabled, onChange }: { parameter: TrainingParameterDefinition; disabled: boolean; onChange: (parameter: TrainingParameterDefinition) => void }) {
  const choices = parameter.choices ?? [];
  const defaultIndex = choices.findIndex((choice) => choice.value === parameter.default);
  const updateChoice = (index: number, value: string) => {
    const nextChoices = choices.map((choice, choiceIndex) => choiceIndex === index ? { value, label: value } : choice);
    const nextDefault = index === defaultIndex ? value : parameter.default;
    onChange({ ...parameter, choices: nextChoices, default: nextDefault });
  };
  const addChoice = () => {
    let suffix = choices.length + 1;
    while (choices.some((choice) => choice.value === `option_${suffix}`)) suffix += 1;
    const choice = { value: `option_${suffix}`, label: `option_${suffix}` };
    onChange({ ...parameter, choices: [...choices, choice], default: choices.length ? parameter.default : choice.value });
  };
  const removeChoice = (index: number) => {
    if (choices.length <= 1) return;
    const nextChoices = choices.filter((_, choiceIndex) => choiceIndex !== index);
    onChange({ ...parameter, choices: nextChoices });
  };
  const moveChoice = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= choices.length) return;
    const nextChoices = [...choices];
    [nextChoices[index], nextChoices[target]] = [nextChoices[target], nextChoices[index]];
    onChange({ ...parameter, choices: nextChoices });
  };
  const duplicateValues = new Set(choices.map((choice) => choice.value.trim()).filter((value, index, values) => value && values.indexOf(value) !== index));

  return <fieldset className="rounded-md border border-console-line bg-console-panel p-3 md:col-span-2 xl:col-span-3">
    <legend className="px-1 text-xs font-medium text-console-text">枚举选项</legend>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <p className="max-w-2xl text-xs leading-5 text-console-muted">每个选项会原样写入训练命令，并显示在新建训练页。请选择其中一项作为默认值。</p>
      <ConsoleButton aria-label={`${parameter.key} 添加枚举选项`} disabled={disabled || choices.length >= 100} onClick={addChoice}><Plus className="h-4 w-4" />添加选项</ConsoleButton>
    </div>
    <div className="mt-3 hidden grid-cols-[minmax(0,1fr)_5rem_8.5rem] gap-2 px-2 text-[11px] text-console-muted md:grid">
      <span>选项值（写入命令并用于页面展示）</span><span className="text-center">默认值</span><span className="text-center">操作</span>
    </div>
    <div className="mt-2 space-y-2">
      {choices.map((choice, index) => {
        const valueError = !choice.value.trim() ? "实际值不能为空" : duplicateValues.has(choice.value.trim()) ? "实际值重复" : null;
        return <div key={index} className="grid gap-2 rounded-md border border-console-line bg-console-panel2 p-2 md:grid-cols-[minmax(0,1fr)_5rem_8.5rem] md:items-start">
          <label className="text-[11px] text-console-muted md:text-transparent">选项值<input aria-label={`${parameter.key} 第 ${index + 1} 个枚举实际值`} className={`${inputClass} font-mono md:mt-0 ${valueError ? "border-rose-500" : ""}`} value={choice.value} maxLength={200} disabled={disabled} placeholder="例如：steps" onChange={(event) => updateChoice(index, event.target.value)} />{valueError ? <span role="alert" className="mt-1 block text-[11px] text-rose-700 md:text-rose-700">{valueError}</span> : null}</label>
          <label className="flex h-9 cursor-pointer items-center justify-start gap-2 text-xs text-console-muted md:justify-center"><input type="radio" name={`${parameter.key}-enum-default`} aria-label={`${parameter.key} 第 ${index + 1} 个枚举设为默认值`} className="accent-console-cyan" checked={index === defaultIndex} disabled={disabled || !choice.value} onChange={() => onChange({ ...parameter, default: choice.value })} /><span className="md:sr-only">设为默认</span></label>
          <div className="flex justify-end gap-1">
            <ConsoleButton className="h-9 w-9 px-0" aria-label={`${parameter.key} 上移第 ${index + 1} 个枚举选项`} disabled={disabled || index === 0} onClick={() => moveChoice(index, -1)}><ArrowUp className="h-4 w-4" /></ConsoleButton>
            <ConsoleButton className="h-9 w-9 px-0" aria-label={`${parameter.key} 下移第 ${index + 1} 个枚举选项`} disabled={disabled || index === choices.length - 1} onClick={() => moveChoice(index, 1)}><ArrowDown className="h-4 w-4" /></ConsoleButton>
            <ConsoleButton className="h-9 w-9 px-0" aria-label={`${parameter.key} 删除第 ${index + 1} 个枚举选项`} title={choices.length <= 1 ? "枚举至少需要保留一个选项" : index === defaultIndex ? "请先选择其他默认值" : "删除选项"} disabled={disabled || choices.length <= 1 || index === defaultIndex} onClick={() => removeChoice(index)}><Trash2 className="h-4 w-4" /></ConsoleButton>
          </div>
        </div>;
      })}
    </div>
    <p className="mt-2 text-right text-[11px] text-console-muted">{choices.length}/100 个选项</p>
  </fieldset>;
}

function ParameterCard({ parameter, definitions, groups, disabled, onChange, onDelete, onRequestNewGroup }: { parameter: TrainingParameterDefinition; definitions: TrainingParameterDefinition[]; groups: TrainingParameterGroup[]; disabled: boolean; onChange: (parameter: TrainingParameterDefinition) => void; onDelete: () => void; onRequestNewGroup: () => void }) {
  const update = <K extends keyof TrainingParameterDefinition>(key: K, value: TrainingParameterDefinition[K]) => onChange({ ...parameter, [key]: value });
  const changeKey = (key: string) => onChange({
    ...parameter,
    key,
    cli_flag: !parameter.cli_flag || parameter.cli_flag === `--${parameter.key}` ? `--${key}` : parameter.cli_flag,
  });
  const changeType = (type: TrainingParameterType) => {
    if (parameter.semantic_role === "stage_input" && type !== "string" && !window.confirm("阶段输入参数只能使用字符串类型。继续修改会同时取消其阶段输入用途，确定继续吗？")) return;
    onChange({
      ...parameter,
      type,
      semantic_role: parameter.semantic_role === "stage_input"
        ? (type === "string" ? "stage_input" : "hyperparameter")
        : ["string", "enum"].includes(type) ? parameter.semantic_role : "hyperparameter",
      minimum: undefined,
      maximum: undefined,
      string_min_length: undefined,
      string_max_length: undefined,
      ...defaultForType(type),
    });
  };
  const dependencySummary = parameterDependencySummary(definitions, parameter);
  const selectedGroup = trainingParameterGroupFor(parameter);
  const numericDefault = typeof parameter.default === "number" && Number.isFinite(parameter.default) ? parameter.default : null;
  const minimum = typeof parameter.minimum === "number" && Number.isFinite(parameter.minimum) ? parameter.minimum : null;
  const maximum = typeof parameter.maximum === "number" && Number.isFinite(parameter.maximum) ? parameter.maximum : null;
  const defaultRangeError = numericDefault != null && minimum != null && numericDefault < minimum ? "默认值不能小于最小值"
    : numericDefault != null && maximum != null && numericDefault > maximum ? "默认值不能大于最大值"
    : null;
  const minimumRangeError = minimum != null && maximum != null && minimum > maximum ? "最小值不能大于最大值" : null;
  const maximumRangeError = minimum != null && maximum != null && maximum < minimum ? "最大值不能小于最小值" : null;
  const stringMinimum = typeof parameter.string_min_length === "number" && Number.isFinite(parameter.string_min_length) ? parameter.string_min_length : 0;
  const stringMaximum = typeof parameter.string_max_length === "number" && Number.isFinite(parameter.string_max_length) ? parameter.string_max_length : 512;
  const stringMinimumError = Number.isInteger(stringMinimum) && stringMinimum < 0 ? "最短字符数不能为负数"
    : Number.isInteger(stringMinimum) && stringMinimum > 512 ? "最短字符数不能超过 512"
    : Number.isInteger(stringMinimum) && Number.isInteger(stringMaximum) && stringMinimum > stringMaximum ? "最短字符数不能大于最长字符数"
    : null;
  const stringMaximumError = Number.isInteger(stringMaximum) && stringMaximum < 0 ? "最长字符数不能为负数"
    : Number.isInteger(stringMaximum) && stringMaximum > 512 ? "最长字符数不能超过 512"
    : Number.isInteger(stringMinimum) && Number.isInteger(stringMaximum) && stringMaximum < stringMinimum ? "最长字符数不能小于最短字符数"
    : null;
  const stringDefault = typeof parameter.default === "string" ? parameter.default : "";
  const stringDefaultError = Number.isInteger(stringMinimum) && Number.isInteger(stringMaximum) && stringMinimum >= 0 && stringMaximum >= 0 && stringMinimum <= stringMaximum
    ? stringDefault.length < stringMinimum ? "默认值短于最短字符数" : stringDefault.length > stringMaximum ? "默认值超过最长字符数" : null
    : null;
  return <div className="rounded-md border border-console-line bg-console-panel2 p-3">
    <div className="flex items-start justify-between gap-3"><div><p className="font-mono text-sm font-medium text-console-text">{parameter.key || "未命名参数"}</p><p className="text-xs text-console-muted">{dependencySummary ?? "用户可在创建训练时修改"}</p></div><ConsoleButton variant="ghost" disabled={disabled} aria-label={`删除参数 ${parameter.key || "未命名"}`} onClick={onDelete}><Trash2 className="h-4 w-4" />删除</ConsoleButton></div>
    <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      <label className="text-xs text-console-muted">参数字段名<input aria-label={`${parameter.key || "未命名参数"} 参数字段名`} className={inputClass} value={parameter.key} maxLength={100} disabled={disabled} placeholder="num_video_frames" onChange={(event) => changeKey(event.target.value)} /></label>
      <label className="text-xs text-console-muted">页面显示名称<input aria-label={`${parameter.key || "未命名参数"} 显示名称`} className={inputClass} value={parameter.label} maxLength={200} disabled={disabled} placeholder="视频帧数" onChange={(event) => update("label", event.target.value)} /></label>
      <label className="text-xs text-console-muted">CLI flag<input aria-label={`${parameter.key || "未命名参数"} CLI flag`} className={inputClass} value={parameter.cli_flag ?? `--${parameter.key}`} maxLength={102} disabled={disabled} placeholder="--num_video_frames" onChange={(event) => update("cli_flag", event.target.value)} /></label>
      <label className="text-xs text-console-muted">类型<select aria-label={`${parameter.key || "未命名参数"} 类型`} className={inputClass} value={parameter.type} disabled={disabled} onChange={(event) => changeType(event.target.value as TrainingParameterType)}>{parameterTypes.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}</select></label>
      <label className="text-xs text-console-muted">参数用途<select aria-label={`${parameter.key || "未命名参数"} 参数用途`} className={inputClass} value={parameter.semantic_role ?? "hyperparameter"} disabled={disabled || !["string", "enum"].includes(parameter.type)} onChange={(event) => {
        const role = event.target.value as NonNullable<TrainingParameterDefinition["semantic_role"]>;
        if (role === "stage_input") {
          const existing = definitions.find((item) => item !== parameter && item.semantic_role === "stage_input");
          if (existing && !window.confirm(`阶段输入参数最多只能有一个。是否改用 ${parameter.key}，并将 ${existing.key} 恢复为训练超参数？`)) return;
          if (parameter.visible_when && !window.confirm("阶段输入参数必须始终可用。设为阶段输入参数会移除该参数已有的依赖条件，确定继续吗？")) return;
          onChange({ ...parameter, semantic_role: role, visible_when: null });
          return;
        }
        update("semantic_role", role);
      }}><option value="hyperparameter">训练超参数</option><option value="dataset">数据集输入</option>{parameter.type === "string" ? <option value="stage_input">阶段输入参数（接收上一阶段输出）</option> : null}</select><span className="mt-1 block text-[11px] leading-4">阶段输入参数用于多阶段训练，后续阶段可自动接收上一阶段产物输出目录。</span></label>
      {parameter.type === "boolean" ? <label className="text-xs text-console-muted">默认值<select aria-label={`${parameter.key} 默认值`} className={inputClass} value={String(parameter.default)} disabled={disabled} onChange={(event) => update("default", event.target.value === "true")}><option value="true">True</option><option value="false">False</option></select></label>
      : parameter.type === "enum" ? null
      : parameter.type === "string" ? <label className="text-xs text-console-muted">默认值<input aria-label={`${parameter.key} 默认值`} aria-invalid={Boolean(stringDefaultError)} className={`${inputClass} ${stringDefaultError ? "border-rose-500" : ""}`} type="text" value={String(parameter.default)} maxLength={Number.isInteger(stringMaximum) && stringMaximum >= 0 && stringMaximum <= 512 ? stringMaximum : 512} disabled={disabled} onChange={(event) => update("default", event.target.value)} />{stringDefaultError ? <span role="alert" className="mt-1 block text-[11px] text-rose-700">{stringDefaultError}</span> : null}</label>
      : <label className="text-xs text-console-muted">默认值<NumericField ariaLabel={`${parameter.key} 默认值`} value={typeof parameter.default === "boolean" ? Number.NaN : parameter.default} integer={parameter.type === "integer"} disabled={disabled} externalError={defaultRangeError} onChange={(value) => update("default", value ?? Number.NaN)} /></label>}
      <label className="text-xs text-console-muted">argv 表达方式<select aria-label={`${parameter.key} argv 表达方式`} className={inputClass} value={parameter.argument_style ?? (parameter.type === "boolean" ? "explicit_boolean" : "value")} disabled={disabled || parameter.type !== "boolean"} onChange={(event) => update("argument_style", event.target.value as TrainingArgumentStyle)}>{parameter.type === "boolean" ? <><option value="explicit_boolean">显式 True / False</option><option value="flag_when_true">True 时仅输出 flag</option></> : <option value="value">flag + value</option>}</select></label>
      {parameter.type === "boolean" ? <p className="self-end text-xs leading-5 text-console-muted md:col-span-2 xl:col-span-3">“显式 True / False”会生成 <span className="font-mono">--flag True</span> 或 <span className="font-mono">--flag False</span>；“True 时仅输出 flag”在 False 时不生成该参数。</p> : null}
      {(parameter.type === "integer" || parameter.type === "number") ? <><label className="text-xs text-console-muted">最小值（可选）<NumericField ariaLabel={`${parameter.key} 最小值`} value={parameter.minimum} integer={parameter.type === "integer"} optional disabled={disabled} placeholder="不限制" externalError={minimumRangeError} onChange={(value) => update("minimum", value)} /></label><label className="text-xs text-console-muted">最大值（可选）<NumericField ariaLabel={`${parameter.key} 最大值`} value={parameter.maximum} integer={parameter.type === "integer"} optional disabled={disabled} placeholder="不限制" externalError={maximumRangeError} onChange={(value) => update("maximum", value)} /></label></> : null}
      {parameter.type === "string" ? <><label className="text-xs text-console-muted">最短字符数<NumericField ariaLabel={`${parameter.key} 最短字符数`} value={parameter.string_min_length ?? 0} integer disabled={disabled} externalError={stringMinimumError} onChange={(value) => update("string_min_length", value)} /></label><label className="text-xs text-console-muted">最长字符数<NumericField ariaLabel={`${parameter.key} 最长字符数`} value={parameter.string_max_length ?? 512} integer disabled={disabled} externalError={stringMaximumError} onChange={(value) => update("string_max_length", value)} /></label><p className="self-end text-xs leading-5 text-console-muted">字符数必须是 0–512 的整数，且最短字符数不能超过最长字符数。</p></> : null}
      {parameter.type === "enum" ? <EnumChoiceEditor parameter={parameter} disabled={disabled} onChange={onChange} /> : null}
      <label className="text-xs text-console-muted">参数解释（可选）<span className="float-right text-[11px]">{parameter.description?.length ?? 0}/120</span><input aria-label={`${parameter.key} 参数解释`} className={inputClass} value={parameter.description ?? ""} maxLength={120} disabled={disabled} placeholder="简要说明参数作用" onChange={(event) => update("description", event.target.value)} /></label>
      <label className="text-xs text-console-muted">展示分组<select aria-label={`${parameter.key || "未命名参数"} 展示分组`} className={inputClass} value={selectedGroup.key} disabled={disabled} onChange={(event) => {
        if (event.target.value === "__new_group__") { onRequestNewGroup(); return; }
        const group = groups.find((item) => item.key === event.target.value);
        if (group) onChange(assignTrainingParameterGroup(parameter, group));
      }}>{groups.map((group) => <option key={group.key} value={group.key}>{group.label}{group.key === "common" ? "（常驻）" : ""}</option>)}<option value="__new_group__">＋ 新建分组…</option></select></label>
    </div>
    <div className="mt-3 flex flex-wrap gap-5 text-sm text-console-muted"><label><input type="checkbox" className="mr-2 accent-console-cyan" checked={Boolean(parameter.sensitive)} disabled={disabled} onChange={(event) => update("sensitive", event.target.checked)} />敏感值（输入和预览遮蔽）</label></div>
  </div>;
}

export function ParameterDefinitionEditor({ definitions, disabled = false, onChange }: Props) {
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({ common: true });
  const [newGroupTargetKey, setNewGroupTargetKey] = useState<string | null>(null);
  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupError, setNewGroupError] = useState<string | null>(null);
  const [manageGroupsOpen, setManageGroupsOpen] = useState(false);
  const [deleteGroupKey, setDeleteGroupKey] = useState<string | null>(null);
  const [dependencyError, setDependencyError] = useState<string | null>(null);
  const groupOptions = availableTrainingParameterGroups(definitions);
  const usedGroups = usedTrainingParameterGroups(definitions);
  const deleteGroup = usedGroups.find((group) => group.key === deleteGroupKey);
  const deleteGroupParameterCount = definitions.filter((parameter) => trainingParameterGroupFor(parameter).key === deleteGroupKey).length;
  const replace = (target: TrainingParameterDefinition, next: TrainingParameterDefinition) => {
    const oldValues = target.type === "enum" ? (target.choices ?? []).map((choice) => choice.value) : [];
    const nextValues = next.type === "enum" ? (next.choices ?? []).map((choice) => choice.value) : [];
    const removedValues = oldValues.filter((value) => !nextValues.includes(value));
    const addedValues = nextValues.filter((value) => !oldValues.includes(value));
    const renamedValue = removedValues.length === 1 && addedValues.length === 1 ? { from: removedValues[0], to: addedValues[0] } : null;
    const dependents = definitions.filter((item) => item.visible_when?.parameter_key === target.key);
    const removesReferencedChoice = target.type === next.type && !renamedValue && removedValues.some((value) => dependents.some((item) => String(item.visible_when?.equals) === value));
    const changesTypeWithConfiguration = target.type !== next.type && dependents.length > 0;
    if (removesReferencedChoice && !window.confirm("该枚举值正在被参数依赖规则使用。删除后，对应依赖规则也会被移除。确定继续吗？")) return;
    if (changesTypeWithConfiguration && !window.confirm(`修改参数类型会重置类型专属配置${dependents.length ? `，并移除 ${dependents.length} 条以此参数为条件的依赖规则` : ""}。确定继续吗？`)) return;
    onChange(definitions.map((item) => {
    if (item === target) return next;
    if (next.semantic_role === "stage_input" && item.semantic_role === "stage_input") return { ...item, semantic_role: "hyperparameter" };
    if (target.type !== next.type && item.visible_when?.parameter_key === target.key) return { ...item, visible_when: null };
    if (target.key !== next.key && item.visible_when?.parameter_key === target.key) return { ...item, visible_when: { ...item.visible_when, parameter_key: next.key } };
    if (item.visible_when?.parameter_key === target.key && renamedValue && item.visible_when.equals === renamedValue.from) return { ...item, visible_when: { ...item.visible_when, equals: renamedValue.to } };
    if (item.visible_when?.parameter_key === target.key && removedValues.includes(String(item.visible_when.equals))) return { ...item, visible_when: null };
    return item;
    }));
  };
  const remove = (target: TrainingParameterDefinition) => {
    const dependentCount = definitions.filter((item) => item.visible_when?.parameter_key === target.key).length;
    if (dependentCount && !window.confirm(`有 ${dependentCount} 条参数依赖规则引用了“${target.label}”。删除参数后，这些规则也会被移除。确定继续吗？`)) return;
    onChange(definitions.filter((item) => item !== target).map((item) => item.visible_when?.parameter_key === target.key ? { ...item, visible_when: null } : item));
  };
  const add = () => {
    let suffix = definitions.length + 1;
    while (definitions.some((item) => item.key === `parameter_${suffix}`)) suffix += 1;
    const otherGroup = groupOptions.find((group) => group.key === "other")!;
    onChange([...definitions, assignTrainingParameterGroup({ key: `parameter_${suffix}`, label: "新参数", type: "string", default: "", editable: true, sensitive: false, cli_flag: `--parameter_${suffix}`, argument_style: "value" }, otherGroup)]);
    setOpenGroups((current) => ({ ...current, [otherGroup.key]: true }));
  };
  const requestNewGroup = (parameter: TrainingParameterDefinition) => {
    setNewGroupTargetKey(parameter.key);
    setNewGroupName("");
    setNewGroupError(null);
  };
  const createGroup = () => {
    const name = newGroupName.trim();
    if (name.length < 2 || name.length > 30) { setNewGroupError("分组名称需要 2–30 个字符。"); return; }
    if (groupOptions.some((group) => group.label.toLocaleLowerCase() === name.toLocaleLowerCase())) { setNewGroupError("已存在同名参数分组。"); return; }
    const target = definitions.find((parameter) => parameter.key === newGroupTargetKey);
    if (!target) { setNewGroupError("目标参数已不存在，请重新选择。"); return; }
    let suffix = 1;
    while (groupOptions.some((group) => group.key === `custom_group_${suffix}`)) suffix += 1;
    const middleOrders = groupOptions.filter((group) => group.key !== "common" && group.key !== "other").map((group) => group.order);
    const group: TrainingParameterGroup = { key: `custom_group_${suffix}`, label: name, order: Math.min(990, Math.max(500, ...middleOrders) + 10), collapsed: true, custom: true, hint: "模型注册时创建的自定义参数分组。" };
    onChange(definitions.map((parameter) => parameter === target ? assignTrainingParameterGroup(parameter, group) : parameter));
    setOpenGroups((current) => ({ ...current, [group.key]: true }));
    setNewGroupTargetKey(null);
    setNewGroupName("");
    setNewGroupError(null);
  };
  const renameGroup = (groupKey: string, label: string) => onChange(definitions.map((parameter) => {
    const group = trainingParameterGroupFor(parameter);
    return group.key === groupKey ? { ...parameter, display_group: group.key, display_group_label: label, display_group_order: group.order } : parameter;
  }));
  const moveGroup = (groupKey: string, direction: -1 | 1) => {
    const movable = usedGroups.filter((group) => group.key !== "common" && group.key !== "other");
    const index = movable.findIndex((group) => group.key === groupKey);
    const swap = movable[index + direction];
    const current = movable[index];
    if (!current || !swap) return;
    onChange(definitions.map((parameter) => {
      const group = trainingParameterGroupFor(parameter);
      if (group.key === current.key) return assignTrainingParameterGroup(parameter, { ...group, order: swap.order });
      if (group.key === swap.key) return assignTrainingParameterGroup(parameter, { ...group, order: current.order });
      return parameter;
    }));
  };
  const confirmDeleteGroup = () => {
    if (!deleteGroup?.custom) return;
    const otherGroup = groupOptions.find((group) => group.key === "other")!;
    onChange(definitions.map((parameter) => trainingParameterGroupFor(parameter).key === deleteGroup.key ? assignTrainingParameterGroup(parameter, otherGroup) : parameter));
    setOpenGroups((current) => ({ ...current, [otherGroup.key]: true }));
    setDeleteGroupKey(null);
    setManageGroupsOpen(true);
  };
  const updateDependencies = (nextDefinitions: TrainingParameterDefinition[]) => {
    const invalidStageInput = nextDefinitions.find((parameter) => parameter.semantic_role === "stage_input" && parameter.visible_when);
    if (invalidStageInput) {
      setDependencyError(`阶段输入参数 ${invalidStageInput.key} 必须始终可用，不能设置参数依赖条件。`);
      return;
    }
    setDependencyError(null);
    onChange(nextDefinitions);
  };
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-medium text-console-text">训练脚本参数</h3><p className="text-xs text-console-muted">无需填写参数数量。逐项添加字段、类型、默认值、CLI flag 与展示分组，平台会生成结构化 argv。</p></div><div className="flex flex-wrap gap-2"><ParameterDependencyDialog definitions={definitions} disabled={disabled} onChange={updateDependencies} /><ConsoleButton variant="ghost" disabled={disabled || !definitions.length} onClick={() => setManageGroupsOpen(true)}><Settings2 className="h-4 w-4" />管理参数分组</ConsoleButton><ConsoleButton variant="ghost" disabled={disabled} onClick={add}><Plus className="h-4 w-4" />添加参数</ConsoleButton></div></div>
    {dependencyError ? <p role="alert" className="text-sm text-rose-700">{dependencyError}</p> : null}
    {usedGroups.map((group) => { const items = definitions.filter((parameter) => trainingParameterGroupFor(parameter).key === group.key); return <section key={group.key} aria-label={group.label}><details className="rounded-md border border-console-line bg-console-panel p-3" open={openGroups[group.key] ?? !group.collapsed} onToggle={(event) => { const open = event.currentTarget.open; setOpenGroups((current) => current[group.key] === open ? current : { ...current, [group.key]: open }); }}><summary className="cursor-pointer"><span className="text-sm font-semibold text-console-text">{group.label} <span className="font-normal text-console-muted">({items.length})</span></span><span className="ml-2 text-xs text-console-muted">{group.hint}</span></summary><div className="mt-3 space-y-2">{items.map((parameter) => <ParameterCard key={definitions.indexOf(parameter)} parameter={parameter} definitions={definitions} groups={groupOptions} disabled={disabled} onChange={(next) => replace(parameter, next)} onDelete={() => remove(parameter)} onRequestNewGroup={() => requestNewGroup(parameter)} />)}</div></details></section>; })}
    <Dialog open={newGroupTargetKey !== null} onOpenChange={(open) => { if (!open) { setNewGroupTargetKey(null); setNewGroupError(null); } }}>
      <DialogContent className="sm:max-w-md"><DialogHeader><DialogTitle>新建参数分组</DialogTitle><DialogDescription>新分组会作为折叠区显示，并自动接收当前参数。</DialogDescription></DialogHeader><label className="text-sm text-console-muted">分组名称<input autoFocus aria-label="新分组名称" className={inputClass} value={newGroupName} maxLength={30} placeholder="例如：LoRA 配置" onChange={(event) => { setNewGroupName(event.target.value); setNewGroupError(null); }} /></label><p className="text-xs text-console-muted">{newGroupName.length}/30，至少 2 个字符</p>{newGroupError ? <p role="alert" className="text-sm text-rose-700">{newGroupError}</p> : null}<DialogFooter><ConsoleButton onClick={() => setNewGroupTargetKey(null)}>取消</ConsoleButton><ConsoleButton variant="primary" onClick={createGroup}>创建并移入</ConsoleButton></DialogFooter></DialogContent>
    </Dialog>
    <Dialog open={manageGroupsOpen} onOpenChange={setManageGroupsOpen}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader><DialogTitle>管理参数分组</DialogTitle><DialogDescription>常用参数和其他参数为系统保留分组；中间的用户分组可重命名、排序和删除。</DialogDescription></DialogHeader>
        <div className="space-y-2">{usedGroups.map((group) => {
          const count = definitions.filter((parameter) => trainingParameterGroupFor(parameter).key === group.key).length;
          const movable = usedGroups.filter((item) => item.key !== "common" && item.key !== "other");
          const index = movable.findIndex((item) => item.key === group.key);
          const rawLabel = definitions.find((parameter) => trainingParameterGroupFor(parameter).key === group.key)?.display_group_label ?? group.label;
          const reserved = group.key === "common" || group.key === "other";
          return <div key={group.key} className="flex items-center gap-2 rounded-md border border-console-line bg-console-panel2 p-3">
            <div className="min-w-0 flex-1">{reserved ? <p className="text-sm font-medium text-console-text">{group.label}</p> : <input aria-label={`${group.label} 分组名称`} className={inputClass} value={rawLabel} maxLength={30} onChange={(event) => renameGroup(group.key, event.target.value)} />}<p className="mt-1 text-xs text-console-muted">{count} 个参数 · {group.key === "common" ? "系统保留 · 常驻" : group.key === "other" ? "系统保留 · 默认折叠" : "用户分组 · 默认折叠"}</p></div>
            {!reserved ? <><ConsoleButton variant="ghost" aria-label={`上移分组 ${group.label}`} disabled={index <= 0} onClick={() => moveGroup(group.key, -1)}><ArrowUp className="h-4 w-4" /></ConsoleButton><ConsoleButton variant="ghost" aria-label={`下移分组 ${group.label}`} disabled={index < 0 || index >= movable.length - 1} onClick={() => moveGroup(group.key, 1)}><ArrowDown className="h-4 w-4" /></ConsoleButton><ConsoleButton variant="ghost" aria-label={`删除分组 ${group.label}`} onClick={() => { setManageGroupsOpen(false); setDeleteGroupKey(group.key); }}><Trash2 className="h-4 w-4" />删除</ConsoleButton></> : null}
          </div>;
        })}</div>
        <DialogFooter><ConsoleButton variant="primary" onClick={() => setManageGroupsOpen(false)}>完成</ConsoleButton></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={deleteGroupKey !== null} onOpenChange={(open) => { if (!open) { setDeleteGroupKey(null); setManageGroupsOpen(true); } }}>
      <DialogContent className="sm:max-w-md"><DialogHeader><DialogTitle>确认删除参数分组</DialogTitle><DialogDescription>删除“{deleteGroup?.label}”后，该分组内的 {deleteGroupParameterCount} 个参数将自动移入系统保留的“其他参数”。参数本身不会被删除。</DialogDescription></DialogHeader><DialogFooter><ConsoleButton onClick={() => { setDeleteGroupKey(null); setManageGroupsOpen(true); }}>取消</ConsoleButton><ConsoleButton variant="primary" onClick={confirmDeleteGroup}>删除分组并迁移参数</ConsoleButton></DialogFooter></DialogContent>
    </Dialog>
  </div>;
}
