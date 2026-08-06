import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { AnnotationWorkbenchHelp } from "./AnnotationWorkbenchHelp";

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", TestResizeObserver);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

test("opens a categorized, keyboard-dismissable annotation workbench guide", async () => {
  render(<AnnotationWorkbenchHelp />);

  const trigger = screen.getByRole("button", { name: "打开标注台帮助" });
  act(() => trigger.focus());
  fireEvent.click(trigger);

  const guide = await screen.findByRole("dialog", { name: "标注台帮助" });
  expect(within(guide).getByRole("tablist", { name: "帮助分类" })).toHaveClass("flex-col");
  expect(within(guide).getByRole("region", { name: "帮助内容" })).toHaveClass("overflow-y-auto");
  expect(within(guide).getByRole("region", { name: "帮助内容" })).toHaveAttribute("tabindex", "0");
  expect(within(guide).getByRole("tab", { name: "页面构成" })).toHaveAttribute("data-state", "active");
  expect(within(guide).getByText("先认识四个工作区域")).toBeVisible();
  expect(within(guide).getByText(/所属外层 clip/)).toBeVisible();
  expect(within(guide).getByText(/当前外层 clip 内各 Segment/)).toBeVisible();

  fireEvent.mouseDown(within(guide).getByRole("tab", { name: "组件功能" }), { button: 0 });
  expect(within(guide).getByText("常用组件如何配合")).toBeVisible();
  expect(within(guide).getByText(/不会自动判断点是否位于目标框内/)).toBeVisible();

  fireEvent.mouseDown(within(guide).getByRole("tab", { name: "业务流程" }), { button: 0 });
  expect(within(guide).getByText(/全部 Segment 均已提交或跳过/)).toBeVisible();

  fireEvent.mouseDown(within(guide).getByRole("tab", { name: "操作" }), { button: 0 });
  expect(within(guide).getByText(/修改约 700ms 后自动保存/)).toBeVisible();
  expect(within(guide).getByText(/当前外层 clip 内的 Segment 序号/)).toBeVisible();
  expect(within(guide).getByText(/几何数据不会自动合并/)).toBeVisible();

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "标注台帮助" })).not.toBeInTheDocument();
  await waitFor(() => expect(trigger).toHaveFocus());
});
