import "@testing-library/jest-dom/vitest";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ScrambleTitle, scrambleTitleFrame } from "./ScrambleTitle";

function mockReducedMotion(matches: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const mediaQuery = {
    matches,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => true,
  } as MediaQueryList;
  vi.stubGlobal("matchMedia", vi.fn(() => mediaQuery));
  return listeners;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ScrambleTitle", () => {
  test("builds deterministic frames while preserving spaces and the final title", () => {
    expect(scrambleTitleFrame("仪表 盘", 0, 0)).toBe("〇一 六");
    expect(scrambleTitleFrame("仪表 盘", 0, 0)).toBe(scrambleTitleFrame("仪表 盘", 0, 0));
    expect(scrambleTitleFrame("仪表 盘", 0, Number.NaN)).toBe("〇一 六");
    expect(scrambleTitleFrame("仪表 盘", 1, 20)).toBe("仪表 盘");
  });

  test("keeps the final accessible name while the visual text settles", () => {
    vi.useFakeTimers();
    mockReducedMotion(false);
    render(<ScrambleTitle as="h2" text="仪表盘" durationMs={100} stepMs={20} />);

    const heading = screen.getByRole("heading", { name: "仪表盘" });
    expect(heading).toHaveAttribute("data-scramble-state", "running");

    act(() => {
      vi.advanceTimersByTime(120);
    });

    expect(heading).toHaveAttribute("data-scramble-state", "settled");
    expect(heading.querySelector('[data-slot="scramble-title-value"]')).toHaveTextContent("仪表盘");
    expect(screen.getByText("仪表盘")).toBeVisible();
  });

  test("cleans up an earlier sequence when the title changes quickly", () => {
    vi.useFakeTimers();
    mockReducedMotion(false);
    const { rerender } = render(
      <ScrambleTitle as="h2" text="仪表盘" durationMs={200} stepMs={20} />,
    );

    act(() => {
      vi.advanceTimersByTime(40);
    });
    rerender(<ScrambleTitle as="h2" text="模型训练" durationMs={100} stepMs={20} />);
    act(() => {
      vi.advanceTimersByTime(140);
    });

    const heading = screen.getByRole("heading", { name: "模型训练" });
    expect(heading).toHaveAttribute("data-scramble-state", "settled");
    expect(heading.querySelector('[data-slot="scramble-title-value"]')).toHaveTextContent("模型训练");
    expect(screen.queryByRole("heading", { name: "仪表盘" })).not.toBeInTheDocument();
  });

  test("settles immediately when reduced motion is preferred", () => {
    vi.useFakeTimers();
    mockReducedMotion(true);
    render(<ScrambleTitle as="h2" text="仪表盘" />);

    const heading = screen.getByRole("heading", { name: "仪表盘" });
    expect(heading).toHaveAttribute("data-scramble-state", "settled");
    expect(heading.querySelector('[data-slot="scramble-title-value"]')).toHaveTextContent("仪表盘");
    expect(vi.getTimerCount()).toBe(0);
  });
});
