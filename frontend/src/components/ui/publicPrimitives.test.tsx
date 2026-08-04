import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { CircularProgress } from "./circular-progress";
import { Input } from "./input";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "./pagination";
import {
  Popover,
  PopoverClose,
  PopoverContent,
  PopoverTrigger,
} from "./popover";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./tabs";

describe("public shadcn UI primitives", () => {
  test("Popover opens accessibly, accepts an Input, closes, and restores its trigger state", async () => {
    render(
      <Popover>
        <PopoverTrigger>打开筛选</PopoverTrigger>
        <PopoverContent aria-label="筛选选项">
          <label htmlFor="keyword">关键词</label>
          <Input id="keyword" />
          <PopoverClose>完成</PopoverClose>
        </PopoverContent>
      </Popover>,
    );

    const trigger = screen.getByRole("button", { name: "打开筛选" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);

    expect(screen.getByRole("dialog", { name: "筛选选项" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "关键词" })).toBeVisible();
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "筛选选项" })).not.toBeInTheDocument();
      expect(trigger).toHaveAttribute("aria-expanded", "false");
    });

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "筛选选项" })).not.toBeInTheDocument();
      expect(trigger).toHaveAttribute("aria-expanded", "false");
    });
  });

  test("Tabs styles the Radix data-state contract and links triggers to panels", () => {
    render(
      <Tabs defaultValue="first">
        <TabsList aria-label="示例视图">
          <TabsTrigger value="first">第一项</TabsTrigger>
          <TabsTrigger value="second">第二项</TabsTrigger>
        </TabsList>
        <TabsContent value="first">第一项内容</TabsContent>
        <TabsContent value="second">第二项内容</TabsContent>
      </Tabs>,
    );

    const activeTab = screen.getByRole("tab", { name: "第一项" });
    expect(activeTab).toHaveAttribute("data-state", "active");
    expect(activeTab.className).toContain("data-[state=active]:bg-background");
    expect(activeTab).toHaveAttribute("aria-controls", screen.getByRole("tabpanel").id);
  });

  test("Pagination exposes navigation and current-page semantics", () => {
    render(
      <Pagination>
        <PaginationContent>
          <PaginationItem><PaginationPrevious href="#previous" /></PaginationItem>
          <PaginationItem><PaginationLink href="#page-1" isActive>1</PaginationLink></PaginationItem>
          <PaginationItem><PaginationLink href="#page-2">2</PaginationLink></PaginationItem>
          <PaginationItem><PaginationNext href="#next" /></PaginationItem>
        </PaginationContent>
      </Pagination>,
    );

    expect(screen.getByRole("navigation", { name: "分页导航" })).toBeVisible();
    expect(screen.getByRole("link", { name: "1" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "转到上一页" })).toHaveAttribute("href", "#previous");
    expect(screen.getByRole("link", { name: "转到下一页" })).toHaveAttribute("href", "#next");
  });

  test("CircularProgress clamps values, keeps stable SVG geometry, and exposes progress semantics", () => {
    const { rerender } = render(<CircularProgress value={68} aria-label="训练资源" />);
    const progress = screen.getByRole("progressbar", { name: "训练资源" });
    const indicator = document.querySelector<SVGCircleElement>(
      '[data-slot="circular-progress-indicator"]',
    );

    expect(progress).toHaveAttribute("aria-valuenow", "68");
    expect(progress).toHaveTextContent("68%");
    expect(indicator).toHaveStyle({ strokeDasharray: "100", strokeDashoffset: "32" });
    expect(indicator).toHaveClass("motion-reduce:transition-none");

    rerender(<CircularProgress value={Number.NaN} aria-label="训练资源" />);
    expect(progress).toHaveAttribute("aria-valuenow", "0");
    expect(indicator).toHaveStyle({ strokeDashoffset: "100" });

    rerender(<CircularProgress value={140} aria-label="训练资源" centerLabel="完成" />);
    expect(progress).toHaveAttribute("aria-valuenow", "100");
    expect(progress).toHaveTextContent("完成");
    expect(indicator).toHaveStyle({ strokeDashoffset: "0" });
  });
});
