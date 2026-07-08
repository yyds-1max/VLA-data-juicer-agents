import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PendingHumanDecision } from "../../api/types";
import { HumanDecisionDialog } from "./HumanDecisionDialog";

function decision(overrides: Partial<PendingHumanDecision> = {}): PendingHumanDecision {
  return {
    replyId: "reply-1",
    toolCallId: "tool-call-1",
    requestId: "request-1",
    decisionType: "confirmation",
    summary: "发现潜在风险，需要确认。",
    ...overrides,
  };
}

describe("HumanDecisionDialog", () => {
  it("renders nothing when there is no pending decision", () => {
    const { container } = render(
      <HumanDecisionDialog
        decision={null}
        onConfirm={vi.fn()}
        onStop={vi.fn()}
        onGuide={vi.fn()}
      />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("dialog", { name: "需要确认" })).not.toBeInTheDocument();
  });

  it("shows the summary and fallback text", () => {
    const { rerender } = render(
      <HumanDecisionDialog
        decision={decision()}
        onConfirm={vi.fn()}
        onStop={vi.fn()}
        onGuide={vi.fn()}
      />,
    );

    expect(screen.getByRole("dialog", { name: "需要确认" })).toBeVisible();
    expect(screen.getByText("发现潜在风险，需要确认。")).toBeVisible();

    rerender(
      <HumanDecisionDialog
        decision={decision({ summary: "" })}
        onConfirm={vi.fn()}
        onStop={vi.fn()}
        onGuide={vi.fn()}
      />,
    );

    expect(screen.getByText("请确认是否继续。")).toBeVisible();
  });

  it("triggers confirm and stop callbacks", () => {
    const onConfirm = vi.fn();
    const onStop = vi.fn();

    render(
      <HumanDecisionDialog
        decision={decision()}
        onConfirm={onConfirm}
        onStop={onStop}
        onGuide={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    fireEvent.click(screen.getByRole("button", { name: "停止" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("sends trimmed guidance text and ignores blank guidance", () => {
    const onGuide = vi.fn();

    render(
      <HumanDecisionDialog
        decision={decision()}
        onConfirm={vi.fn()}
        onStop={vi.fn()}
        onGuide={onGuide}
      />,
    );

    const input = screen.getByLabelText("引导文本");
    const send = screen.getByRole("button", { name: "发送" });

    expect(send).toBeDisabled();
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(send);
    expect(onGuide).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "  先汇总风险再继续  " } });
    expect(send).toBeEnabled();
    fireEvent.click(send);

    expect(onGuide).toHaveBeenCalledWith("先汇总风险再继续");
  });
});
