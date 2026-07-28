import type { NavigationDatasetSelectionContext } from "../../api/types";

const MAX_REQUEST_CONTEXT_BYTES = 3_000;

export type NavigationDatasetSelection =
  | { scope: "date"; date: string }
  | { scope: "clips"; date: string; clips: readonly [string, ...string[]] };

export function buildNavigationDatasetRequest(selection: NavigationDatasetSelection): string {
  const { date, clips } = normalizeSelection(selection);

  const lines = ["请处理导航数据。", "", `数据日期：${date}`];

  if (selection.scope === "clips") {
    lines.push("指定 clips：", ...clips.map((clip) => `- ${clip}`));
  }

  return lines.join("\n");
}

export function buildAnnotationProcessingRequest(
  selection: NavigationDatasetSelection,
): string {
  const { date, clips } = normalizeSelection(selection);
  const lines = [
    "请对选中的导航数据执行自动标注并完成后处理。",
    "",
    `数据日期：${date}`,
  ];

  if (selection.scope === "clips") {
    lines.push("指定 clips：", ...clips.map((clip) => `- ${clip}`));
  }

  return lines.join("\n");
}

export function buildNavigationDatasetRequestContext(
  selection: NavigationDatasetSelection,
): NavigationDatasetSelectionContext {
  const { date, clips } = normalizeSelection(selection);
  const context: NavigationDatasetSelectionContext = {
    kind: "navigation_dataset_selection_v1",
    dataset_date: date,
    selection: selection.scope === "date"
      ? { kind: "all_clips" }
      : { kind: "selected_clips", clips },
  };
  if (new TextEncoder().encode(JSON.stringify(context)).length > MAX_REQUEST_CONTEXT_BYTES) {
    throw new Error("选择的 clips 过多，无法在一次任务中提交");
  }
  return context;
}

function normalizeSelection(selection: NavigationDatasetSelection): { date: string; clips: string[] } {
  const date = selection.date.trim();
  if (!/^\d{8}$/.test(date)) {
    throw new Error("导航数据日期必须是 YYYYMMDD 格式");
  }
  if (selection.scope === "date") return { date, clips: [] };

  const clips = [...new Set(selection.clips.map((clip) => clip.trim()))];
  if (
    clips.length === 0 ||
    clips.some((clip) =>
      !clip ||
      clip === "." ||
      clip === ".." ||
      clip.length > 200 ||
      /[\\/\r\n]/.test(clip)
    )
  ) {
    throw new Error("指定 clips 必须包含至少一个合法标识");
  }
  return { date, clips };
}
