import { Link2, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import type { TrainingParameterDefinition } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";

type Props = {
  definitions: TrainingParameterDefinition[];
  disabled?: boolean;
  onChange: (definitions: TrainingParameterDefinition[]) => void;
};

const selectClass = "h-9 min-w-40 rounded-md border border-console-line bg-console-panel px-2 text-sm text-console-text focus:border-console-cyan focus:outline-hidden";

function initialConditionValue(parameter: TrainingParameterDefinition | undefined) {
  if (!parameter) return "";
  return parameter.default;
}

function valueLabel(parameter: TrainingParameterDefinition | undefined, value: string | number | boolean) {
  if (typeof value === "boolean") return value ? "True" : "False";
  if (parameter?.type === "enum") return String(value);
  return String(value);
}

function ConditionValueInput({ parameter, value, onChange }: {
  parameter: TrainingParameterDefinition | undefined;
  value: string | number | boolean;
  onChange: (value: string | number | boolean) => void;
}) {
  if (!parameter) return null;
  if (parameter.type === "boolean") {
    return <select aria-label="条件值" className={selectClass} value={String(value)} onChange={(event) => onChange(event.target.value === "true")}><option value="true">True</option><option value="false">False</option></select>;
  }
  if (parameter.type === "enum") {
    return <select aria-label="条件值" className={selectClass} value={String(value)} onChange={(event) => onChange(event.target.value)}>{parameter.choices?.map((choice) => <option key={choice.value} value={choice.value}>{choice.value}</option>)}</select>;
  }
  return <input
    aria-label="条件值"
    className={selectClass}
    type={parameter.type === "string" ? "text" : "number"}
    step={parameter.type === "number" ? "any" : "1"}
    min={parameter.minimum ?? undefined}
    max={parameter.maximum ?? undefined}
    value={String(value)}
    onChange={(event) => onChange(parameter.type === "integer" ? Number.parseInt(event.target.value, 10) : parameter.type === "number" ? Number(event.target.value) : event.target.value)}
  />;
}

export function parameterDependencySummary(
  definitions: TrainingParameterDefinition[],
  target: TrainingParameterDefinition,
) {
  const condition = target.visible_when;
  if (!condition) return null;
  const controller = definitions.find((item) => item.key === condition.parameter_key);
  if (!controller) return null;
  return `仅当「${controller.label}」等于 ${valueLabel(controller, condition.equals)} 时，「${target.label}」才可设置。`;
}

export function ParameterDependencyDialog({ definitions, disabled = false, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [targetKey, setTargetKey] = useState(definitions[0]?.key ?? "");
  const target = definitions.find((item) => item.key === targetKey) ?? definitions[0];
  const controllers = useMemo(() => definitions.filter((item) => item.key !== target?.key), [definitions, target?.key]);
  const existingController = target?.visible_when?.parameter_key;
  const [controllerKey, setControllerKey] = useState(existingController ?? controllers[0]?.key ?? "");
  const controller = controllers.find((item) => item.key === controllerKey) ?? controllers[0];
  const [conditionValue, setConditionValue] = useState<string | number | boolean>(target?.visible_when?.equals ?? initialConditionValue(controller));

  const loadTarget = (nextTargetKey: string) => {
    const nextTarget = definitions.find((item) => item.key === nextTargetKey) ?? definitions[0];
    const availableControllers = definitions.filter((item) => item.key !== nextTarget?.key);
    const nextController = availableControllers.find((item) => item.key === nextTarget?.visible_when?.parameter_key) ?? availableControllers[0];
    setTargetKey(nextTarget?.key ?? "");
    setControllerKey(nextController?.key ?? "");
    setConditionValue(nextTarget?.visible_when?.equals ?? initialConditionValue(nextController));
  };

  const changeController = (nextControllerKey: string) => {
    const nextController = definitions.find((item) => item.key === nextControllerKey);
    setControllerKey(nextControllerKey);
    setConditionValue(initialConditionValue(nextController));
  };

  const save = () => {
    if (!target || !controller) return;
    onChange(definitions.map((item) => item.key === target.key ? {
      ...item,
      visible_when: { parameter_key: controller.key, equals: conditionValue },
    } : item));
    setOpen(false);
  };

  const remove = () => {
    if (!target) return;
    onChange(definitions.map((item) => item.key === target.key ? { ...item, visible_when: null } : item));
    setOpen(false);
  };

  return <Dialog open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (nextOpen) loadTarget(target?.key ?? definitions[0]?.key ?? ""); }}>
    <ConsoleButton disabled={disabled || definitions.length < 2} onClick={() => setOpen(true)}><Link2 className="h-4 w-4" />设计依赖关系</ConsoleButton>
    <DialogContent className="sm:max-w-3xl">
      <DialogHeader>
        <DialogTitle>设计参数可用条件</DialogTitle>
        <DialogDescription>为目标参数设置一个启用条件。每个参数最多设置一条规则，可继续组合成多级联动。</DialogDescription>
      </DialogHeader>
      <div className="rounded-lg border border-console-line bg-console-panel2 p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm text-console-text">
          <span>仅当</span>
          <select aria-label="条件参数" className={selectClass} value={controller?.key ?? ""} onChange={(event) => changeController(event.target.value)}>{controllers.map((item) => <option key={item.key} value={item.key}>{item.label} · {item.key}</option>)}</select>
          <span>等于</span>
          <ConditionValueInput parameter={controller} value={conditionValue} onChange={setConditionValue} />
          <span>时，</span>
          <select aria-label="目标参数" className={selectClass} value={target?.key ?? ""} onChange={(event) => loadTarget(event.target.value)}>{definitions.map((item) => <option key={item.key} value={item.key}>{item.label} · {item.key}</option>)}</select>
          <span>才可设置。</span>
        </div>
        <p className="mt-3 text-xs text-console-muted">未满足条件时，目标参数仍会显示，但将变灰且不可编辑，并从 RunSpec 和命令 argv 中省略。</p>
      </div>
      {definitions.some((item) => item.visible_when) ? <div><p className="mb-2 text-xs font-medium text-console-muted">已设置的规则</p><ul className="space-y-1 text-sm text-console-text">{definitions.map((item) => { const summary = parameterDependencySummary(definitions, item); return summary ? <li key={item.key}>• {summary}</li> : null; })}</ul></div> : null}
      <DialogFooter>
        <ConsoleButton onClick={() => setOpen(false)}>取消</ConsoleButton>
        {target?.visible_when ? <ConsoleButton onClick={remove}><Trash2 className="h-4 w-4" />移除当前规则</ConsoleButton> : null}
        <ConsoleButton variant="primary" disabled={!target || !controller || (typeof conditionValue === "number" && !Number.isFinite(conditionValue))} onClick={save}>保存规则</ConsoleButton>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
