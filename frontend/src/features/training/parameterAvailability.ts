import type { TrainingParameterDefinition } from "../../api/types";

export function isTrainingParameterEnabled(
  definitions: TrainingParameterDefinition[],
  values: Record<string, string | number | boolean>,
  parameterKey: string,
  resolving = new Set<string>(),
): boolean {
  const definition = definitions.find((item) => item.key === parameterKey);
  if (!definition) return false;
  const condition = definition.visible_when;
  if (!condition) return true;
  if (resolving.has(parameterKey)) return false;
  const controller = definitions.find((item) => item.key === condition.parameter_key);
  if (!controller) return false;
  const nextResolving = new Set(resolving).add(parameterKey);
  if (!isTrainingParameterEnabled(definitions, values, controller.key, nextResolving)) return false;
  return (values[controller.key] ?? controller.default) === condition.equals;
}

export function enabledTrainingParameters(
  definitions: TrainingParameterDefinition[],
  values: Record<string, string | number | boolean>,
): TrainingParameterDefinition[] {
  return definitions.filter((definition) => isTrainingParameterEnabled(definitions, values, definition.key));
}
