export type NavigationDatasetSelection =
  | { scope: "date"; date: string }
  | { scope: "clips"; date: string; clips: readonly [string, ...string[]] };

export function buildNavigationDatasetRequest(selection: NavigationDatasetSelection): string {
  const date = selection.date.trim();
  if (!/^\d{8}$/.test(date)) {
    throw new Error("导航数据日期必须是 YYYYMMDD 格式");
  }

  const lines = ["请处理导航数据。", "", `数据日期：${date}`];

  if (selection.scope === "clips") {
    const clips = [...new Set(selection.clips.map((clip) => clip.trim()))];
    if (clips.length === 0 || clips.some((clip) => !clip || /[\r\n]/.test(clip))) {
      throw new Error("指定 clips 必须包含至少一个合法标识");
    }
    lines.push("指定 clips：", ...clips.map((clip) => `- ${clip}`));
  }

  lines.push("", "请先检查当前实际产物状态，再根据检查结果决定从哪一步开始。");
  return lines.join("\n");
}
