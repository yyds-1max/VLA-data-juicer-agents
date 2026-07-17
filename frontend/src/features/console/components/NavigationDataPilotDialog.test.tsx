import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import type { NavigationDateSummary } from "../../../api/types";
import type { NavigationDatasetSelection } from "../navigationDataPilotRequest";
import { NavigationDataPilotDialog } from "./NavigationDataPilotDialog";

const dates: NavigationDateSummary[] = [
  {
    date: "20270605",
    clip_count: 2,
    total_duration_ns: 2,
    raw_message_count: 2,
    extracted_clip_count: 0,
    synced_clip_count: 0,
    sync_frame_counts: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
    status: "raw_only",
    clips: ["clip_a", "clip_b"].map((clip) => ({
      date: "20270605",
      clip,
      duration_ns: 1,
      raw_message_count: 1,
      topics: [],
      has_tmp_dir: false,
      has_sync_data: false,
      sequences: [],
      sync_frame_counts: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
      status: "raw_only" as const,
      errors: [],
    })),
  },
  {
    date: "20270606",
    clip_count: 1,
    total_duration_ns: 1,
    raw_message_count: 1,
    extracted_clip_count: 0,
    synced_clip_count: 0,
    sync_frame_counts: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
    status: "raw_only",
    clips: [],
  },
];

function DialogHarness({
  submitting = false,
  onConfirm = vi.fn(),
}: {
  submitting?: boolean;
  onConfirm?: (selection: NavigationDatasetSelection) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <NavigationDataPilotDialog
      dates={dates}
      open={open}
      submitting={submitting}
      onCancel={() => setOpen(false)}
      onConfirm={onConfirm}
      onSelectionChange={vi.fn()}
    />
  );
}

describe("NavigationDataPilotDialog", () => {
  it("starts empty, supports partial clips, and clears clips when the date changes", () => {
    const onConfirm = vi.fn();
    render(<DialogHarness onConfirm={onConfirm} />);

    const confirm = screen.getByRole("button", { name: "确定" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("数据日期"), { target: { value: "20270605" } });
    expect(confirm).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: "clip_a" }));
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenLastCalledWith({
      scope: "clips",
      date: "20270605",
      clips: ["clip_a"],
    });

    fireEvent.change(screen.getByLabelText("数据日期"), { target: { value: "20270606" } });
    expect(confirm).toBeDisabled();
  });

  it("maps selecting every clip to a whole-date selection", () => {
    const onConfirm = vi.fn();
    render(<DialogHarness onConfirm={onConfirm} />);
    fireEvent.change(screen.getByLabelText("数据日期"), { target: { value: "20270605" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(onConfirm).toHaveBeenCalledWith({ scope: "date", date: "20270605" });
  });

  it.each([
    ["取消", () => fireEvent.click(screen.getByRole("button", { name: "取消" }))],
    ["X", () => fireEvent.click(screen.getByRole("button", { name: "关闭数据选择" }))],
    ["Escape", () => fireEvent.keyDown(document, { key: "Escape" })],
  ])("closes through %s", (_label, close) => {
    render(<DialogHarness />);
    close();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the dialog open and flashes its border when the backdrop is clicked", () => {
    render(<DialogHarness />);
    const overlay = screen.getByTestId("navigation-datapilot-overlay");
    const dialog = screen.getByTestId("navigation-datapilot-dialog");

    fireEvent.mouseDown(overlay, { button: 0 });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(dialog.className).toContain("navigation-dialog-attention-a");
  });

  it("does not use a blue focus ring on the close button", () => {
    render(<DialogHarness />);
    expect(screen.getByRole("button", { name: "关闭数据选择" }).className).not.toContain("ring-console-cyan");
  });

  it("disables confirmation and cancellation while submitting without changing labels", () => {
    render(<DialogHarness submitting />);

    expect(screen.getByRole("button", { name: "确定" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "关闭数据选择" })).toBeDisabled();
    expect(screen.queryByText("正在提交…")).not.toBeInTheDocument();
  });
});
