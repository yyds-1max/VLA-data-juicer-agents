import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import {
  DATA_FLOW_STAGE_DESCRIPTIONS,
  DATA_FLOW_STAGES,
  DataFlowTimeline,
  DEFAULT_DATA_FLOW_BATCHES,
} from "./DataFlowTimeline";

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

afterAll(() => {
  vi.unstubAllGlobals();
});

function chooseBatch(batchId: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "选择数据批次" }));
  fireEvent.click(screen.getByRole("option", { name: batchId }));
}

describe("DataFlowTimeline", () => {
  test("renders the fixed eight-stage flow and marks the default current step semantically", () => {
    render(<DataFlowTimeline />);

    const list = screen.getByRole("list");
    const items = within(list).getAllByRole("listitem");

    expect(items).toHaveLength(8);
    expect(items.map((item) => item.textContent?.replace("占位", ""))).toEqual([
      "01原数据",
      "02拆解同步",
      "03标注处理",
      "04AI复核",
      "05人工复核进行中",
      "06模型训练",
      "07测试批复",
      "08部署验证",
    ]);
    expect(screen.getByRole("listitem", { name: "人工复核，进行中" })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(
      screen.getByRole("listitem", { name: "人工复核，进行中" }).querySelector(".dashboard-flow-current-spinner"),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("listitem").filter((item) => item.dataset.flowState === "completed")).toHaveLength(4);
    expect(screen.getAllByRole("listitem").filter((item) => item.dataset.flowState === "pending")).toHaveLength(3);
    expect(screen.getByTestId("data-flow-track")).toBeVisible();
    expect(screen.getByTestId("data-flow-progress")).toHaveStyle({ width: "57.14285714285714%" });
    expect(document.querySelectorAll(".dashboard-flow-sweep")).toHaveLength(1);
    expect(document.querySelector(".dashboard-flow-sweep")).toHaveClass("motion-reduce:hidden");
  });

  test("switches placeholder batches through the shadcn Select and reports the selected batch", () => {
    const onBatchChange = vi.fn();
    render(<DataFlowTimeline onBatchChange={onBatchChange} />);

    chooseBatch("20260621");

    expect(screen.getByRole("combobox", { name: "选择数据批次" })).toHaveTextContent("20260621");
    expect(screen.getByRole("listitem", { name: "模型训练，进行中" })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(onBatchChange).toHaveBeenCalledWith("20260621", DEFAULT_DATA_FLOW_BATCHES[1]);
    expect(screen.getByTestId("data-flow-progress")).toHaveStyle({ width: "71.42857142857143%" });

    chooseBatch("20260618");

    expect(screen.getByRole("listitem", { name: "部署验证，进行中" })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByTestId("data-flow-progress")).toHaveStyle({ width: "100%" });
  });

  test("supports controlled selection without mutating the displayed batch", () => {
    const onBatchChange = vi.fn();
    render(<DataFlowTimeline selectedBatchId="20260623" onBatchChange={onBatchChange} />);

    chooseBatch("20260621");

    expect(onBatchChange).toHaveBeenCalledWith("20260621", DEFAULT_DATA_FLOW_BATCHES[1]);
    expect(screen.getByRole("combobox", { name: "选择数据批次" })).toHaveTextContent("20260623");
    expect(screen.getByRole("listitem", { name: "人工复核，进行中" })).toHaveAttribute(
      "aria-current",
      "step",
    );
  });

  test("shows business-aware stage guidance on mouse hover", async () => {
    render(<DataFlowTimeline />);

    const trigger = screen.getByRole("button", { name: "原数据节点说明" });
    fireEvent.pointerMove(trigger, { pointerType: "mouse" });

    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("01 · 原数据");
    expect(tooltip).toHaveTextContent(DATA_FLOW_STAGE_DESCRIPTIONS.原数据);
    expect(Object.keys(DATA_FLOW_STAGE_DESCRIPTIONS)).toEqual(DATA_FLOW_STAGES);
    expect(DATA_FLOW_STAGE_DESCRIPTIONS.AI复核).toContain("当前尚未接入");
    expect(DATA_FLOW_STAGE_DESCRIPTIONS.模型训练).toContain("前端占位");
    expect(DATA_FLOW_STAGE_DESCRIPTIONS.测试批复).toContain("尚未接入后端");
    expect(DATA_FLOW_STAGE_DESCRIPTIONS.部署验证).toContain("仍为占位");
  });

  test("exposes a keyboard-focusable horizontal region and an empty state", () => {
    const { rerender } = render(<DataFlowTimeline />);

    expect(screen.getByRole("region", { name: "20260623 数据闭环流程，可横向滚动" })).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(screen.getByTestId("data-flow-scroll-region")).toHaveClass("overflow-x-auto");

    rerender(<DataFlowTimeline batches={[]} />);

    expect(screen.getByRole("combobox", { name: "选择数据批次" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("暂无可展示的数据批次");
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  test("exports the expected placeholder batch mapping and fixed stages", () => {
    expect(DEFAULT_DATA_FLOW_BATCHES).toEqual([
      { id: "20260623", currentStage: "人工复核" },
      { id: "20260621", currentStage: "模型训练" },
      { id: "20260618", currentStage: "部署验证" },
    ]);
    expect(DATA_FLOW_STAGES).toEqual([
      "原数据",
      "拆解同步",
      "标注处理",
      "AI复核",
      "人工复核",
      "模型训练",
      "测试批复",
      "部署验证",
    ]);
  });
});
