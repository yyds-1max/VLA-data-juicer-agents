import { describe, expect, it } from "vitest";

import type { TrainingParameterDefinition } from "../../api/types";
import { availableTrainingParameterGroups } from "./parameterGroups";

describe("training parameter groups", () => {
  it("offers only the two reserved groups for an ungrouped new-model parameter", () => {
    const parameter: TrainingParameterDefinition = {
      key: "custom_value",
      label: "自定义参数",
      type: "number",
      default: 0,
      editable: true,
    };

    expect(availableTrainingParameterGroups([parameter]).map((group) => ({ key: group.key, label: group.label, custom: group.custom }))).toEqual([
      { key: "common", label: "常用参数", custom: false },
      { key: "other", label: "其他参数", custom: false },
    ]);
  });
});
