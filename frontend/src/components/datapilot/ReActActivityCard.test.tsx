import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { createEmptyRunState } from "../../store/eventReducer";
import { MessageList } from "./MessageList";

function activityRun(status: "running" | "completed") {
  const run = createEmptyRunState();
  run.running = status === "running";
  run.timeline = [
    {
      kind: "activity",
      source: "agentscope",
      text: "正在处理导航数据",
      status,
      runId: "run-1",
      parentRunId: null,
      activityId: "activity-1",
      activityTitle: "正在处理导航数据",
      activityStatus: status,
      activitySteps: [
        {
          id: "step-1",
          sequence: 1,
          status: status === "running" ? "acting" : "completed",
          observation: "发现已有处理方案。",
          analysis: "需要确认当前步骤，再决定后续处理。",
          action: "读取当前计划步骤。",
        },
      ],
    },
  ];
  return run;
}

test("ReAct activity stays expanded while running and collapses after completion", async () => {
  const { rerender } = render(<MessageList messages={[]} run={activityRun("running")} />);

  const runningHeader = screen.getByRole("button", { name: /正在处理导航数据/ });
  expect(runningHeader).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("观察")).toBeVisible();
  expect(screen.getByText("思考")).toBeVisible();
  expect(screen.getByText("行动")).toBeVisible();
  expect(screen.getAllByText("读取当前计划步骤。")).toHaveLength(2);

  rerender(<MessageList messages={[]} run={activityRun("completed")} />);

  const completedHeader = screen.getByRole("button", { name: /已完成 1 个处理步骤/ });
  await waitFor(() => expect(completedHeader).toHaveAttribute("aria-expanded", "false"));
  expect(screen.queryByText("观察")).not.toBeInTheDocument();

  fireEvent.click(completedHeader);
  expect(completedHeader).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("发现已有处理方案。")).toBeVisible();
});
