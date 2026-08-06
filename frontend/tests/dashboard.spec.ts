import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const dashboardSummary = {
  totals: {
    date_count: 14,
    clip_count: 536,
    total_duration_ns: 32_023_000_000_000,
    raw_message_count: 12_739_018,
    extracted_clip_count: 420,
    synced_clip_count: 397,
  },
  sync_distribution: {
    image: 40_604,
    pointcloud: 36_998,
    odom: 28_062,
    grid_map: 36_484,
  },
  dates: [],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/annotation/events/cursor", async (route) => {
    await route.fulfill({ json: { cursor: 0 } });
  });
  await page.route("**/api/annotation/events?after_seq=*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: "retry: 60000\n\n",
    });
  });
  await page.route("**/api/navigation/datasets/summary", async (route) => {
    await route.fulfill({ json: dashboardSummary });
  });
});

const viewports = [
  { name: "1440x1024", width: 1440, height: 1024 },
  { name: "1280x800", width: 1280, height: 800 },
  { name: "1024x768", width: 1024, height: 768 },
  { name: "768x1024", width: 768, height: 1024 },
  { name: "390x844", width: 390, height: 844 },
] as const;

for (const viewport of viewports) {
  test(`dashboard remains usable at ${viewport.name}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "仪表盘" })).toBeVisible();
    await expect(page.getByText("8.9 小时")).toBeVisible();
    await expect(page.getByRole("combobox", { name: "选择数据批次" })).toContainText("20260623");
    await expect(page.getByRole("button", { name: "Open DataPilot" })).toBeVisible();

    const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(pageOverflow).toBeLessThanOrEqual(1);

    if (process.env.DASHBOARD_SKIP_SCREENSHOT !== "1") {
      await page.screenshot({ path: testInfo.outputPath(`dashboard-${viewport.name}.png`), fullPage: true });
    }
  });
}

test("dashboard keyboard states, placeholders, sidebar and links are predictable", async ({ page }) => {
  const consoleErrors: string[] = [];
  const failedResponses: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });

  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.goto("/");

  const collapse = page.getByRole("button", { name: "收起侧边栏" });
  await collapse.focus();
  await expect(collapse).toBeFocused();
  await page.evaluate(() => {
    const windowWithSamples = window as Window & { __sidebarIconSamples?: number[] };
    windowWithSamples.__sidebarIconSamples = [];
    const startedAt = performance.now();
    const sample = () => {
      const icon = document.querySelector<SVGElement>('button[aria-current="page"] svg');
      if (icon) {
        windowWithSamples.__sidebarIconSamples?.push(icon.getBoundingClientRect().x);
      }
      if (performance.now() - startedAt < 320) {
        requestAnimationFrame(sample);
      }
    };
    requestAnimationFrame(sample);
  });
  await collapse.press("Enter");
  await expect(page.getByTestId("console-sidebar")).toHaveAttribute("data-collapsed", "true");
  await expect(page.getByRole("button", { name: "展开侧边栏" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("vla-console-sidebar"))).toBe("collapsed");
  await page.waitForTimeout(340);
  const iconSamples = await page.evaluate(
    () => (window as Window & { __sidebarIconSamples?: number[] }).__sidebarIconSamples ?? [],
  );
  expect(iconSamples.length).toBeGreaterThan(3);
  const iconStart = iconSamples[0];
  const iconEnd = iconSamples.at(-1) ?? iconStart;
  expect(Math.max(...iconSamples)).toBeLessThanOrEqual(Math.max(iconStart, iconEnd) + 3);
  expect(Math.min(...iconSamples)).toBeGreaterThanOrEqual(Math.min(iconStart, iconEnd) - 3);

  const batchSelect = page.getByRole("combobox", { name: "选择数据批次" });
  await batchSelect.focus();
  await batchSelect.press("Enter");
  await expect(page.getByRole("option", { name: "20260621" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("option", { name: "20260621" })).toHaveCount(0);

  await page.getByRole("button", { name: "搜索数据、模型、任务（暂未接入）" }).click();
  await expect(page.getByText("搜索功能暂未接入")).toBeVisible();
  await page.getByRole("button", { name: "通知" }).click();
  await expect(page.getByRole("dialog", { name: "通知中心" })).toBeVisible();
  await expect(page.getByText("暂无通知")).toBeVisible();

  await page.getByRole("button", { name: "查看训练详情" }).click();
  await expect(page).toHaveURL(/\/model$/);
  await expect(page.getByRole("heading", { name: "模型训练" })).toBeVisible();
  expect({ consoleErrors, failedResponses }).toEqual({ consoleErrors: [], failedResponses: [] });
});

test("dashboard has no serious accessibility violations at desktop and mobile widths", async ({ page }) => {
  for (const viewport of [viewports[0], viewports[4]]) {
    await page.setViewportSize(viewport);
    await page.goto("/");
    await expect(page.getByText("8.9 小时")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const blockingViolations = results.violations.filter(
      (violation) => violation.impact === "critical" || violation.impact === "serious",
    );
    expect(blockingViolations).toEqual([]);
  }
});

test("dashboard remains stable at 125 percent browser zoom", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");
  await page.evaluate(() => {
    document.documentElement.style.zoom = "1.25";
  });

  await expect(page.getByRole("heading", { name: "仪表盘" })).toBeVisible();
  await expect(page.getByRole("region", { name: /20260623 数据闭环流程/ })).toBeVisible();
  const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(pageOverflow).toBeLessThanOrEqual(1);
});

test("dashboard respects reduced motion preferences", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");

  await expect(page.getByText("8.9 小时")).toBeVisible();
  const sweepIsHidden = await page.locator(".dashboard-flow-sweep").first().evaluate(
    (element) => window.getComputedStyle(element).display === "none",
  );
  expect(sweepIsHidden).toBe(true);
});

test("dashboard exposes honest summary failure and retry feedback", async ({ page }) => {
  let attempts = 0;
  await page.unroute("**/api/navigation/datasets/summary");
  await page.route("**/api/navigation/datasets/summary", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.fulfill({ status: 503, json: { detail: "summary unavailable" } });
      return;
    }
    await route.fulfill({ json: dashboardSummary });
  });

  await page.goto("/");
  await expect(page.getByRole("alert").first()).toBeVisible();
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("8.9 小时")).toBeVisible();
  expect(attempts).toBe(2);
});
