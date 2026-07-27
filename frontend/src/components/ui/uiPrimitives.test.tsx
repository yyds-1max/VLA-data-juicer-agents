import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { Alert, AlertDescription, AlertTitle } from "./alert";
import { Badge } from "./badge";
import { Button } from "./button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./dialog";
import { Progress } from "./progress";

describe("Radix/shadcn UI primitives", () => {
  test("Button and Badge expose native semantics without business state", () => {
    const onClick = vi.fn();

    render(
      <>
        <Button onClick={onClick}>创建任务</Button>
        <Badge variant="outline">待处理</Badge>
      </>,
    );

    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));

    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.getByText("待处理")).toHaveAttribute("data-slot", "badge");
  });

  test("Alert and Progress expose accessible status information", () => {
    render(
      <>
        <Alert>
          <AlertTitle>运行环境不可用</AlertTitle>
          <AlertDescription>请检查服务器部署配置。</AlertDescription>
        </Alert>
        <Progress value={63} aria-label="任务进度" />
      </>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("运行环境不可用");
    expect(screen.getByRole("progressbar", { name: "任务进度" })).toHaveAttribute(
      "aria-valuenow",
      "63",
    );
  });

  test("Dialog has an accessible name and closes with Escape", async () => {
    function DialogHarness() {
      const [open, setOpen] = useState(true);

      return (
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent>
            <DialogTitle>确认操作</DialogTitle>
            <DialogDescription>该操作不会修改业务数据。</DialogDescription>
          </DialogContent>
        </Dialog>
      );
    }

    render(<DialogHarness />);

    expect(screen.getByRole("dialog", { name: "确认操作" })).toBeVisible();
    expect(screen.getByRole("button", { name: "关闭" })).toBeVisible();

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "确认操作" })).not.toBeInTheDocument();
    });
  });
});
